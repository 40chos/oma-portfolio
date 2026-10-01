"""ModelGatewayClient -- the one shared class every other piece of this
system calls through to reach a model. Never a bare httpx/requests call
scattered elsewhere.

Per the build plan's Phase 2 and §2.4 (Nexo's real llm_backends.py
pattern, reimplemented small and direct, no Router hop in between):
  - a shared, pooled httpx.AsyncClient reused across calls
  - jittered exponential-backoff retry (three attempts)
  - a circuit breaker that stops attempting calls to a backend that's
    failed repeatedly within a short window, for a cooldown period
  - a bulkhead semaphore partitioned by BACKEND, not by model tier

Real infra change, 2026-07-13 (the project owner's explicit instruction, following
up on EXECUTION_ROADMAP_2026-07-12.md workstream #1): this system used
to route every model through one single llama-swap host that hot-swapped
whichever model a call asked for -- a real ~10-40s reload tax on every
role switch. That host is retired for anything but the coder role. There
are now two permanently-resident, single-purpose GPU hosts, and this
client picks between them **per call, keyed off the `model` name** --
never per client instance -- because one shared ModelGatewayClient
instance is used process-wide across every specialist (build, manager,
code-review, testing/QA), and each of those still needs to reach a
different physical host depending on which model it asks for:
  - GPU Worker 01 (10.1.19.195:9090) -- coder only (`qwen3-coder-30b-a3b`).
    Never receives a request for anything else.
  - GPU Worker 02 (10.1.19.203:9090) -- the one shared reasoning model
    (`qwen3.6-27b`, logical name) for every other role: manager,
    classifier, code-review, testing/QA routine tier, testing/QA
    escalation tier. `qwen3-14b` and `deepseek-r1-distill-qwen-32b` are
    retired -- every non-coder role now shares this one resident model
    instead of swapping between four.

Confirmed live (2026-07-13) that GPU Worker 02's own `/v1/models` does
NOT expose the model under the logical id `qwen3.6-27b` -- it serves
under the host-specific alias `JA-GPU2-27B-INT4-64K` only. Every other
piece of this codebase (context-window lookups, docstrings, env var
defaults, trace/UI display) keeps using the stable logical name
`qwen3.6-27b`; only this module knows about the wire-level alias, and
only rewrites it in the actual outgoing HTTP payload, immediately before
the request is sent -- see `_wire_model_id()`.

Because the two hosts are now independent physical machines with
independent failure modes, each gets its OWN circuit breaker, bulkhead,
and pooled httpx.AsyncClient (`_BackendPool`) -- a outage on one must
never trip the breaker for, or block the bulkhead of, the other. This
replaces the old single-pool-per-client-instance design, which was only
ever correct when both roles shared one physical host.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field

import httpx

from infra.gpu_capacity_guard import wait_for_ram_headroom
from infra.settings import load_gateway_settings

DEFAULT_TIMEOUT_SEC = 90.0  # generous headroom above real inference latency
# Repetition-loop early-abort gate (2026-07-24 incident, see
# LLMRepetitionLoopError's own docstring): a genuine repeat of a
# ~120-char span 6+ times in a row is never real, useful generation --
# catching it here means a stuck call fails in seconds, not the full
# ~270s asyncio.wait_for() ceiling, on every one of MAX_RETRY_ATTEMPTS.
_REPETITION_WINDOW_CHARS = 120
_REPETITION_MIN_REPEATS = 6
# Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run, project_ticket_counts
# node): the window-based check above requires the exact same 120-char span to recur inside the
# `(_REPETITION_MIN_REPEATS + 1) * window` = 840-char lookback -- confirmed live via
# oma:trace_history that a genuine repetition loop (the same ~480-char JSON `edits: [...]`
# search_replace block, repeated verbatim, dozens of times) ran for 594+ seconds (4000+ streamed
# deltas) without ever tripping it, most likely because a nonzero-temperature retry (this
# specific call's own candidate ran at temperature 0.4, not 0.0) lets each repeated cycle drift
# just enough (whitespace, minor phrasing) to defeat an exact-substring match on any given
# 120-char window, even while the OVERALL output is unmistakably non-convergent. Rather than
# retune window/lookback sizes against a single incident (this project's own prior repetition-
# mitigation experiments -- see OMA_GATEWAY_REPETITION_DETECTION_ENABLED's own history in .env --
# already tried and abandoned several tuned variants that didn't generalize), this is a second,
# orthogonal, deliberately coarse backstop: an absolute total-length ceiling no legitimate single
# structured response from any real call site in this codebase (a scoped-edit's JSON, a full
# module rewrite, a Code-Review verdict) comes anywhere close to in practice, so it only ever
# fires on a genuine runaway, independent of whether the window-based pattern check happens to
# catch the specific repeat shape.
_REPETITION_MAX_TOTAL_CHARS = 40_000

MAX_RETRY_ATTEMPTS = 3
# Real, confirmed finding (2026-07-24, same incident as
# LLMRepetitionLoopError): once a genuinely-looping generation fails
# fast (seconds, not the full ~270s ceiling) instead of hanging,
# additional retries cost almost nothing -- worth spending a few more
# of them specifically to give LLM sampling randomness more chances to
# NOT loop on a retry of the identical prompt, rather than exhausting
# the whole call after only 3 attempts. Scoped separately from the
# base MAX_RETRY_ATTEMPTS (kept at 3 for genuinely slow/networky
# failures, where more retries WOULD still cost real wall-clock time)
# so this only gives extra headroom to the specific failure mode that
# got cheap to retry.
MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP = 6
# Real bug found live (2026-07-24, task 020 resume): the extra retries
# above are worthless on their own for any caller using temperature=0.0
# (the Build specialist's default for every scoped-edit and
# single-constraint round) -- the outgoing payload's `temperature` was
# never varied between attempts, so a genuine repetition loop at
# temperature=0.0 is fully deterministic and reproduces BYTE-FOR-BYTE
# on every retry (confirmed live: two independent resume attempts both
# failed at exactly "725 chars generated", not just a similar failure
# shape). Retrying a deterministic call with identical inputs can never
# produce a different output. On a repetition-loop retry specifically
# (never on an ordinary timeout/HTTP-error retry, where the original
# temperature is still the right choice), the temperature is nudged up
# by this amount each time, capped at _REPETITION_RETRY_MAX_TEMPERATURE
# -- just enough sampling diversity to give the model a genuine chance
# to not loop, without moving so far from the caller's own intended
# temperature that the response quality changes materially.
_REPETITION_RETRY_TEMPERATURE_STEP = 0.2
_REPETITION_RETRY_MAX_TEMPERATURE = 0.6
RETRY_BASE_DELAY_SEC = 1.0
RETRY_MAX_DELAY_SEC = 8.0

CIRCUIT_FAILURE_THRESHOLD = 5      # failures within the window to trip open
CIRCUIT_WINDOW_SEC = 60.0
CIRCUIT_COOLDOWN_SEC = 30.0

# Real, confirmed bug found live (2026-08-07, HUMAN_DECISION deep-push): the coder backend's own
# operator-configured gate ("Coder gate: max concurrent inference is 1 (currently 1). Retry
# shortly; do not fan out parallel streams." -- a real HTTP 429 body, confirmed live) enforces
# genuine single-request concurrency server-side, but this client's own bulkhead defaulted to
# `max_concurrent=8` for every backend uniformly -- confirmed via direct research this is llama-
# swap's own documented per-model semaphore mechanism (a deliberate resource-management feature,
# not a bug or transient overload), and its whole design intent is "the client must never send
# more concurrent requests than the server allows." With max_concurrent=8 against a real
# max_inflight=1 gate, this client's OWN concurrent calls (e.g. an ordinary pytest run making a
# real live GPU call while a separate task relaunch is already mid-flight against the same coder
# backend -- confirmed live, the exact 429 body above was hit by tests/test_build_specialist.py's
# own real end-to-end tests running concurrently with a manual task relaunch) were racing each
# other for the server's own single slot, guaranteeing rejections. A concurrency-limit rejection
# also means the backend responded FAST and is genuinely healthy (same reasoning already applied
# to LLMRepetitionLoopError above) -- it must never count toward the circuit breaker the way a
# real timeout/connection failure does, and is cheap to retry, so it gets its own, more generous
# retry ceiling.
_CODER_BACKEND_MAX_CONCURRENT = 1
MAX_RETRY_ATTEMPTS_AFTER_CONCURRENCY_LIMIT = 6

# Phase 31 §8 (the blocking Step 0 item): a single, cross-cutting cap on ALL local-model calls
# through this client -- both manager/graph_scheduler.py's own node-level fan-out AND the
# pre-existing inner Review‖QA fan-out + turn-start classifier calls (which already run
# concurrently today, unthrottled by anything but each backend's own per-model bulkhead above).
# The real, honest default here should be MEASURED against today's actual peak concurrent local-
# model call count in production -- that is a live-traffic measurement this development session
# has no access to, not something to guess a precise number for. This default (16) is set
# deliberately generous -- comfortably above the per-backend bulkhead ceilings already enforced
# above (max_concurrent=8 default, _CODER_BACKEND_MAX_CONCURRENT=1) so it is not the FIRST thing
# to bind in ordinary operation -- and is explicitly flagged as needing real production
# calibration before this cap is relied upon as a meaningful throttle, not just a circuit-breaker
# backstop. OMA_GRAPH_MAX_CONCURRENT_NODES itself stays at 1 regardless (a separate, deliberate
# gate) -- this semaphore only matters for the ALREADY-concurrent Review‖QA/classifier fan-out.
OMA_LOCAL_INFERENCE_MAX_CONCURRENT = int(os.environ.get("OMA_LOCAL_INFERENCE_MAX_CONCURRENT", "16"))
_local_inference_semaphore = asyncio.Semaphore(OMA_LOCAL_INFERENCE_MAX_CONCURRENT)


def _is_concurrency_limit_error(exc: Exception) -> bool:
    """True only for a real HTTP 429 whose response body names this exact, known concurrency-
    gate shape -- never guessed from the status code alone (a 429 could, in principle, mean
    something else from a different backend/gate in the future). Checks both `str(exc)` (the
    streaming path's own HTTPStatusError, manually raised with the real body text already folded
    into the message -- see `_generate_inner_streaming`) and `exc.response.text` (the
    non-streaming path's own plain `resp.raise_for_status()`, whose default message never
    includes the body).
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    response = exc.response
    if response is None or response.status_code != 429:
        return False
    text = str(exc)
    try:
        text += response.text
    except Exception:
        pass  # body already consumed/unavailable -- str(exc) alone is still checked
    return "concurrency_limit" in text or "max concurrent inference" in text

# Real live-token streaming (the project owner's own explicit request, added after
# Phase 17's first pass): individual tokens arrive far too fast to
# publish one Redis event each -- this batches accumulated deltas and
# flushes on whichever comes first, keeping the live UI feeling
# genuinely real-time without hammering Redis pub/sub.
DELTA_FLUSH_INTERVAL_SEC = 0.12
DELTA_FLUSH_CHAR_THRESHOLD = 40

BACKEND_CODER = "gpu_worker_01_coder"          # 10.1.19.195:9090, coder only
BACKEND_REASONING = "gpu_worker_02_reasoning"  # 10.1.19.203:9090, qwen3.6-27b only
# Real infra change, 2026-07-22: GPU Worker 03 (10.1.19.200:9090) --
# confirmed via the fleet's own firewall audit report
# (infra/network/FIREWALL_AUDIT_REPORT.md, vm-z19-n200-gpu-worker-03) --
# already runs a resident Qwen3-9B-int4-32k with `enable_thinking: false`
# baked in as a genuine SERVER-side default (confirmed live via its own
# /running endpoint's launch command), unlike qwen3.6-27b on Worker 02,
# which only ignores the client-side /no_think convention entirely (see
# generate()'s own docstring above). Live-benchmarked same session: a
# realistic ~360-token extraction prompt (matching Testing/QA's own
# real shape) returned in 1.7-2.2s with correct, clean JSON and zero
# visible thinking-token overhead -- versus a real, measured 100.7s
# outlier for the equivalent call on GPU Worker 02 the same night. Used
# ONLY for the specific short, structured extraction calls this was
# benchmarked against (see specialists/testing_qa/specialist.py's own
# _FAST_EXTRACTION_MODEL_* constants) -- never wired in as a blanket
# replacement for the shared reasoning tier, since Manager's own
# decomposition/classification calls need more actual reasoning depth
# than pure field extraction and were never benchmarked against this
# smaller model.
BACKEND_FAST_EXTRACTION = "gpu_worker_03_fast_extraction"
BACKEND_EXTERNAL = "external"  # the sandbox's LiteLLM, from Phase 14 onward

_CODER_MODEL_NAMES = frozenset({"qwen3-coder-30b-a3b"})
_FAST_EXTRACTION_MODEL_NAMES = frozenset({"qwen3-9b-fast-extraction"})

# GPU Worker 02 serves the reasoning model under this host-specific alias,
# not the logical name `qwen3.6-27b` the rest of this codebase uses --
# confirmed live via that host's own /v1/models (2026-07-13).
_WIRE_MODEL_ALIASES = {
    "qwen3.6-27b": "JA-GPU2-27B-INT4-64K",
    "qwen3-9b-fast-extraction": "Qwen3-9B-int4-32k",
}


# Temporary, operator-controlled failover (2026-07-28): GPU Worker 03
# (the fast-extraction host) is under sustained external 429 load,
# confirmed live (repeated 429/timeout on its own /v1/chat/completions
# over 15+ minutes, unrelated to anything this codebase controls).
# Setting this env var reroutes every fast-extraction call to Worker
# 02's shared reasoning model instead, so the pipeline can keep working
# off the SAME physical host manager/classify/code-review/testing-qa
# already use, at the cost of losing fast-extraction's own measured
# speed advantage (~1.7-2.2s vs. 27B's own real, slower response time)
# for the duration. Purely additive -- unset (the default) preserves
# today's exact routing unchanged. Meant to be reverted the moment
# Worker 03 recovers, not a permanent architecture change.
_FAST_EXTRACTION_FAILOVER_ENV = "OMA_FAST_EXTRACTION_FAILOVER_TO_REASONING"


def _fast_extraction_failover_active() -> bool:
    import os

    return os.environ.get(_FAST_EXTRACTION_FAILOVER_ENV, "").strip().lower() in ("1", "true", "yes")


# Repetition-loop mitigation experiment (2026-08-05, per the project owner's own
# request, following up on docs/planning/
# PHASE30_REPETITION_LOOP_MITIGATION_RESEARCH_2026-08-05.md): that
# report's first suggestion was llama.cpp's DRY sampler, on the
# initially-reported (but NOT independently confirmed) belief that this
# backend is llama.cpp/llama-server. Live-verified here instead
# (2026-08-05) via three independent checks against the real coder host
# -- (1) every chat-completion response's own `system_fingerprint` reads
# literally "vllm-0.24.0-...", (2) an invalid `repetition_penalty: -5`
# returns vLLM's own real 400 validation message
# ("repetition_penalty must be greater than zero"), (3) an invalid
# `repetition_detection.min_count: 1` returns vLLM's own real internal
# Pydantic validation error, sourced from
# vllm/entrypoints/serve/utils/api_utils.py -- confirming this backend
# is genuinely vLLM, not llama.cpp. DRY does not exist on vLLM (a
# garbage `dry_multiplier: "not_a_number"` was silently accepted and
# ignored, as an unrecognized extra field always is, rather than
# rejected). Pivoted to vLLM's own native `repetition_detection`
# SamplingParam instead (added ~v0.17.0, confirmed present on this
# v0.24.0 deployment by the validation-error check above) -- the
# report's own §3.1 lever for "if vLLM," and its higher-confidence
# option regardless: a scheduler-level early stop, not a sampling bias,
# so it carries no code-quality risk the way DRY's continuous
# suppression theoretically could (though DRY was also assessed
# low-risk by the report). Scoped to the coder model only -- the actual
# documented incidents (2026-07-24, and tonight's task007/016/026/029)
# are all Build/coder-model generations; the reasoning tier has no
# reported occurrence of this failure mode in this session's history,
# so this stays a targeted fix, not a blanket change to every call in
# the system. Off by default (unset), one env var to flip, one env var
# to revert -- same reversible-experiment discipline as the OMA_MODEL_
# BUILD 27B A/B test earlier tonight.
_REPETITION_DETECTION_ENV = "OMA_GATEWAY_REPETITION_DETECTION_ENABLED"
# Revised 2026-08-05 (docs/planning/PHASE30_VLLM_REPETITION_LOOP_DEEP_DIVE_2026-08-05.md), after
# the original 8/100/3 profile above genuinely missed both of the first 2 real incidents it was
# live-tested against (task026, task029) -- the app-level char-window detector caught both, not
# this. Root-caused directly against the real captured transcripts (Redis oma:trace_history,
# reconstructed from the "delta" stream events), not guessed: in both incidents the model was
# re-emitting an entire large JSON search_replace edit object from scratch each cycle instead of
# stopping after one -- the repeating UNIT was ~125 tokens (task026) and ~412 tokens (task029),
# both well past the old max_pattern_size=100, so the detector mathematically could never see a
# qualifying match (vLLM's algorithm only looks for exact repeats within [min_pattern_size,
# max_pattern_size] -- a real, structural blind spot per vllm/v1/core/sched/utils.py, not a config
# nudge). Confirmed live: neither incident used response_format (both genuinely free-form JSON-
# via-prompt calls), so vLLM's own auto-hardened structured-output profile (max_pattern_size=20,
# min_count=5, vLLM PRs #40097/#40099) never applied to them either -- there was no better
# server-side default being silently overridden here.
#
# Switched to that exact same maintainer-validated profile instead of guessing a new one --
# directly verified it's the right shape against task026's own real transcript first, not applied
# on faith: a short (~12-token) JSON-boilerplate phrase ('"operation": "search_replace",\n
# "target": "') that recurs verbatim at the start of every edit object regardless of how much the
# actual payload drifts occurred 12 times in that one transcript alone -- comfortably within a
# max_pattern_size=20 window and far past min_count=5. A narrower, higher-min_count profile is
# actually the more robust choice for exactly this failure shape: it targets the small,
# structurally-guaranteed-identical scaffolding tokens that survive drift, rather than requiring
# the whole (often-drifting, often-growing) payload to match exactly the way the old 100-token
# ceiling implicitly demanded. min_pattern_size=1 (not 8) so a genuine single-token loop (e.g. a
# repeated word, like the earlier live "banana" isolation test) is still caught too.
_REPETITION_DETECTION_MIN_PATTERN_SIZE = 1
_REPETITION_DETECTION_MAX_PATTERN_SIZE = 20
_REPETITION_DETECTION_MIN_COUNT = 5


def _repetition_detection_active() -> bool:
    import os

    return os.environ.get(_REPETITION_DETECTION_ENV, "").strip().lower() in ("1", "true", "yes")


def _repetition_detection_param(model: str) -> dict | None:
    """vLLM's own `repetition_detection` SamplingParam dict for the
    outgoing payload, or None if the experiment flag is off or `model`
    isn't the coder model this was scoped to -- see
    `_REPETITION_DETECTION_ENV`'s own comment above for the full
    rationale."""
    if not _repetition_detection_active():
        return None
    if model not in _CODER_MODEL_NAMES:
        return None
    return {
        "min_pattern_size": _REPETITION_DETECTION_MIN_PATTERN_SIZE,
        "max_pattern_size": _REPETITION_DETECTION_MAX_PATTERN_SIZE,
        "min_count": _REPETITION_DETECTION_MIN_COUNT,
    }


# Step 3, 2026-08-05 (same deep-dive report): detection-only did NOT prevent a single real
# incident tonight (0/5, even after correcting to the maintainer's own hardened profile above) --
# repetition_detection stops a loop once it's already happening, it never biases generation away
# from forming one. Qwen's own official model-card recommendation for qwen3-coder-30b-a3b
# specifically (not a generic-code-model guess) is temperature=0.7, top_p=0.8, top_k=20,
# repetition_penalty=1.05. Deliberately NOT including temperature here, unlike the report's own
# suggestion: this codebase's callers already choose temperature deliberately per call (e.g.
# temperature=0.0 for the exact "internal loop patch" call sites that hit every real incident
# tonight, specifically FOR determinism) -- forcing 0.7 globally would be a much broader behavior
# change than this fix calls for, well beyond "never force a generic fix onto a specific problem."
# top_p/top_k/repetition_penalty are prevention-oriented (bias the distribution during sampling)
# rather than detection-oriented, so they're additive to, not a replacement for,
# _repetition_detection_param() above -- both can be active at once. Live-smoke-tested first
# (2026-08-05) against the exact real coder host for the CUDA scatter-assert crash reported in
# vLLM issue #28307 for this exact model family (repetition_penalty specifically) -- confirmed
# clean, HTTP 200, on this deployment's vLLM 0.24.0. Same flag/model-scoping and off-by-default
# discipline as repetition_detection above.
_REPETITION_PREVENTION_ENV = "OMA_GATEWAY_REPETITION_PREVENTION_ENABLED"
_REPETITION_PREVENTION_TOP_P = 0.8
_REPETITION_PREVENTION_TOP_K = 20
_REPETITION_PREVENTION_REPETITION_PENALTY = 1.05


def _repetition_prevention_active() -> bool:
    import os

    return os.environ.get(_REPETITION_PREVENTION_ENV, "").strip().lower() in ("1", "true", "yes")


def _repetition_prevention_params(model: str) -> dict:
    """Qwen's own official sampling-parameter additions for
    qwen3-coder-30b-a3b, or {} if the experiment flag is off or `model`
    isn't the coder model -- see `_REPETITION_PREVENTION_ENV`'s own
    comment above. Deliberately excludes `temperature`, which stays
    caller-controlled -- see that same comment for why."""
    if not _repetition_prevention_active():
        return {}
    if model not in _CODER_MODEL_NAMES:
        return {}
    return {
        "top_p": _REPETITION_PREVENTION_TOP_P,
        "top_k": _REPETITION_PREVENTION_TOP_K,
        "repetition_penalty": _REPETITION_PREVENTION_REPETITION_PENALTY,
    }


def backend_for_model(model: str) -> str:
    """Which resident GPU host a given model name must be routed to.
    Coder goes to Worker 01, the fast-extraction model goes to Worker
    03, everything else goes to Worker 02's shared reasoning model."""
    if model in _CODER_MODEL_NAMES:
        return BACKEND_CODER
    if model in _FAST_EXTRACTION_MODEL_NAMES:
        if _fast_extraction_failover_active():
            return BACKEND_REASONING
        return BACKEND_FAST_EXTRACTION
    return BACKEND_REASONING


def _wire_model_id(model: str) -> str:
    """The literal id to put in the outgoing HTTP payload's `model`
    field -- translates a logical model name to a host-specific serving
    alias where one exists, otherwise passes it through unchanged."""
    if model in _FAST_EXTRACTION_MODEL_NAMES and _fast_extraction_failover_active():
        return _WIRE_MODEL_ALIASES["qwen3.6-27b"]
    return _WIRE_MODEL_ALIASES.get(model, model)


class GatewayUnavailableError(RuntimeError):
    """Raised when the circuit breaker is open or all retries are exhausted."""


class CircuitOpenError(GatewayUnavailableError):
    pass


class LLMRepetitionLoopExhaustedError(GatewayUnavailableError):
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    daily_escalation_cron node, resumes r205 and r206): a repetition loop that exhausts every
    one of its own extended, temperature-escalated retry attempts (see
    MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP) used to fall through to the SAME generic
    `raise GatewayUnavailableError(...)` this file already uses for a genuine transport/HTTP
    failure -- the manager's own downstream escalation summary then told the user "The model
    gateway is currently unreachable... it's an infrastructure outage... I'll resume
    automatically once the gateway is back", which is factually wrong: the gateway was never
    down (confirmed live both times via a direct, immediate `/v1/models` health check against
    both backends, returning 200 within seconds of the failure) -- the model itself was
    structurally struggling to generate this round's own specific content without repeating,
    even after 6 attempts with escalating temperature (confirmed live: nearly-identical repeat
    counts, "10717 chars" then "10715 chars", at the same generation step, "Writing the module's
    manifest and model code", both times).

    Subclasses `GatewayUnavailableError` (not a new, unrelated exception) so every EXISTING
    `except GatewayUnavailableError` call site keeps working completely unchanged -- this is
    purely an ADDITIVE, more specific signal for any caller (present or future) that wants to
    react differently to "this round's own content is too hard for the model to generate without
    looping" than to "the backend is genuinely unreachable," without weakening backward
    compatibility for callers that don't care about the distinction.
    """


class LLMRepetitionLoopError(RuntimeError):
    """Real, confirmed incident (2026-07-24): a Code-Review generation
    got stuck in a token-repetition loop -- streaming real bytes the
    whole time (so httpx's own per-byte timeout reset never fired) and
    staying under the existing asyncio.wait_for() hard ceiling
    (timeout_sec * 3, ~270s) on EVERY one of MAX_RETRY_ATTEMPTS
    retries, so the task could sit for 10+ minutes before finally
    surfacing as a plain GatewayUnavailableError with zero signal
    about what actually went wrong. This is a distinct, detectable
    failure MODE, not just "slow" -- a real, useful generation is
    never the same ~120-char span verbatim 6+ times in a row. Caught
    inside the streaming loop itself, independent of and much faster
    than the timeout ceiling (typically within a few seconds of the
    loop actually starting, not 270s), and reported distinctly so a
    caller can tell "the model is stuck repeating itself" apart from
    "the network/backend is genuinely slow or down."""


@dataclass
class _CircuitBreaker:
    failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD
    window_sec: float = CIRCUIT_WINDOW_SEC
    cooldown_sec: float = CIRCUIT_COOLDOWN_SEC

    _failure_timestamps: list[float] = field(default_factory=list)
    _opened_at: float | None = None

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failure_timestamps = [
            t for t in self._failure_timestamps if now - t < self.window_sec
        ]
        self._failure_timestamps.append(now)
        if len(self._failure_timestamps) >= self.failure_threshold:
            self._opened_at = now

    def record_success(self) -> None:
        self._failure_timestamps.clear()
        self._opened_at = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown_sec:
            # Cooldown elapsed -- half-open: let the next call try again,
            # clearing the open state optimistically. If it fails, a new
            # failure gets recorded and the breaker can re-open.
            self._opened_at = None
            self._failure_timestamps.clear()
            return False
        return True


@dataclass
class _Bulkhead:
    max_concurrent: int = 8
    _semaphore: asyncio.Semaphore | None = None

    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        return self._semaphore


@dataclass
class _BackendPool:
    """One physical host's worth of connection pool + failure-isolation
    state. Each of BACKEND_CODER / BACKEND_REASONING gets its own, so an
    outage on one GPU worker can never trip the breaker or block the
    bulkhead for the other."""
    name: str
    base_url: str
    http: httpx.AsyncClient
    breaker: _CircuitBreaker = field(default_factory=_CircuitBreaker)
    bulkhead: _Bulkhead = field(default_factory=_Bulkhead)


class ModelGatewayClient:
    """Call any model on the configured gateway. One instance should be
    shared across the whole process (reused connection pools, shared
    circuit breaker / bulkhead state per backend). Routes each call to
    the correct GPU host automatically, based on the `model` name --
    see `backend_for_model()` and the module docstring for why.
    """

    def __init__(
        self,
        max_concurrent: int = 8,
        base_url_override: str | None = None,
    ):
        # Phase 30, P1d (Phase L, §15): real, per-task LLM call count and
        # wall-clock duration -- explicitly scoped to VISIBILITY, never a
        # cost ceiling or automatic cutoff (see this phase's own "does
        # not propose a cost ceiling" scoping note). generate() is this
        # whole system's own single choke point (see its own docstring),
        # so counting here, once, covers every specialist and Manager
        # call alike with no per-call-site plumbing needed. Keyed by
        # task_id, popped (read + cleared) by the round loop at the end
        # of each round -- a bare dict, not persisted here; the round
        # loop is what writes it into ReplanRound/agent_memory_events,
        # matching this phase's own "reuse existing storage, don't invent
        # a dashboard" discipline.
        self._call_stats: dict[str, dict] = {}
        settings = load_gateway_settings()
        self.api_key = settings.api_key
        self._ram_ceiling_fraction = settings.ram_ceiling_fraction
        self._ram_ceiling_bytes = settings.ram_ceiling_bytes
        self._ram_check_poll_interval_sec = settings.ram_check_poll_interval_sec
        self._ram_check_max_wait_sec = settings.ram_check_max_wait_sec

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        def _make_pool(name: str, base_url: str, pool_max_concurrent: int | None = None) -> _BackendPool:
            base_url = base_url.rstrip("/")
            return _BackendPool(
                name=name,
                base_url=base_url,
                http=httpx.AsyncClient(
                    base_url=base_url,
                    headers=headers,
                    timeout=httpx.Timeout(DEFAULT_TIMEOUT_SEC),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                ),
                bulkhead=_Bulkhead(max_concurrent=pool_max_concurrent or max_concurrent),
            )

        if base_url_override is not None:
            # Test-only path (deliberately broken/dead URLs to exercise
            # retry/breaker behavior) -- every backend points at the same
            # override, and since it's one shared pool, a breaker trip
            # against it is visible regardless of which model a test asks
            # for. Production code never sets this.
            override_pool = _make_pool("test_override", base_url_override)
            self._pools = {
                BACKEND_CODER: override_pool,
                BACKEND_REASONING: override_pool,
                BACKEND_FAST_EXTRACTION: override_pool,
            }
        else:
            self._pools = {
                # Real, confirmed live bug (2026-08-07): the coder backend's own operator-
                # configured gate genuinely enforces max_inflight=1 server-side (a real 429 body:
                # "Coder gate: max concurrent inference is 1 (currently 1). Retry shortly; do not
                # fan out parallel streams.") -- this client's bulkhead must match it exactly,
                # never send more than the server itself will ever actually process at once. See
                # `_CODER_BACKEND_MAX_CONCURRENT`'s own comment above for the full evidence chain.
                BACKEND_CODER: _make_pool(
                    BACKEND_CODER, settings.coder_base_url,
                    pool_max_concurrent=_CODER_BACKEND_MAX_CONCURRENT,
                ),
                BACKEND_REASONING: _make_pool(BACKEND_REASONING, settings.reasoning_base_url),
                BACKEND_FAST_EXTRACTION: _make_pool(
                    BACKEND_FAST_EXTRACTION, settings.fast_extraction_base_url
                ),
            }

    def _pool_for_model(self, model: str) -> _BackendPool:
        return self._pools[backend_for_model(model)]

    async def aclose(self) -> None:
        seen: set[int] = set()
        for pool in self._pools.values():
            if id(pool.http) in seen:
                continue  # override path shares one http client across both entries
            seen.add(id(pool.http))
            await pool.http.aclose()

    async def list_models(self, model_hint: str = "qwen3.6-27b") -> list[str]:
        """Lists models on whichever backend `model_hint` would route to
        (defaults to the reasoning host). Pass a coder model name to
        inspect GPU Worker 01 instead."""
        pool = self._pool_for_model(model_hint)
        return await self._list_models_on(pool)

    async def _list_models_on(self, pool: "_BackendPool") -> list[str]:
        resp = await pool.http.get("/models")
        resp.raise_for_status()
        return [m["id"] for m in resp.json()["data"]]

    async def generate(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        no_think: bool = False,
        task_id: str | None = None,
        actor: str | None = None,
        call_label: str | None = None,
        response_format: dict | None = None,
        node_id: str | None = None,
    ) -> str:
        """A single chat-completion call, with retry/breaker/bulkhead
        wrapping. Returns the raw response content (callers needing
        structured output should use call_structured() instead, which
        wraps this).

        no_think: qwen3.6-27b (the one shared reasoning model, since
        2026-07-13 -- see module docstring) defaults to an internal
        "thinking mode" (per §0.5.2) and, per a confirmed finding
        (manager/session.py), ignores the `/no_think` convention
        entirely -- it always emits a <think> trace regardless of this
        flag. Pass True anyway for cheap/direct calls (classification,
        structured extraction) to match the convention other tooling
        expects; callers still need strip_think_block() (and, where it
        matters, an explicit completeness check for a truncated
        </think>) on the result either way, never assume no_think
        actually skipped the trace.

        task_id/actor/call_label (Phase 17 follow-up, added after
        the project owner's own explicit request: "I don't want only to see final
        summarization... I want to see everything, which prompt our
        agent gave to which agent, everything"): when task_id is
        supplied, this single, universal choke point -- every LLM call
        in the whole system, Manager and every specialist alike, funnels
        through generate() via generate_checked()/call_structured() --
        publishes the REAL, full prompt and REAL, full response as a
        real trace event on the SAME oma:trace:{task_id} channel the
        rest of the live UI already listens on. Optional and additive:
        every existing call site keeps working with zero behavior change
        if task_id is omitted (backend-only tests, tooling scripts).

        response_format (Phase 18, §22.12 Component 8): an OpenAI-
        compatible `response_format` dict (e.g.
        `{"type": "json_schema", "json_schema": {"name": ..., "schema": ...}}`),
        passed straight through to the vLLM-backed serving request when
        given. Confirmed live against the real coder inference host
        (GPU Worker 01, `10.1.19.195:9090`, vLLM behind `llama-swap`)
        that this stack accepts and correctly honors
        `response_format: json_schema` end to end for
        `qwen3-coder-30b-a3b` specifically -- this is a
        cheaper, earlier filter for malformed/truncated structured output,
        never a replacement for this project's own pre-write validators,
        which still run unchanged on whatever comes back. None (the
        default) preserves every existing call site's exact prior
        behavior -- unconstrained free-form generation.
        """
        # Applied here (not inside _generate_inner) so the "running" trace
        # event below shows the exact real payload about to be sent,
        # /no_think suffix included -- not a slightly-different copy.
        if no_think and messages:
            messages = [dict(m) for m in messages]
            for m in reversed(messages):
                if m.get("role") == "user":
                    m["content"] = f"{m['content']} /no_think"
                    break

        if task_id:
            from manager.trace import publish_trace_event
            publish_trace_event(task_id, {
                "level": "manager" if not actor or actor == "manager" else "specialist",
                "actor": actor or "manager", "phase": "llm_call", "status": "running",
                "message": call_label or f"Calling {model}…",
                "node_id": node_id,
                "llm_call": {"model": model, "messages": messages, "call_label": call_label},
            })

        pool = self._pool_for_model(model)
        _call_started_at = time.monotonic()

        try:
            # Phase 31 §8: the single, cross-cutting local-inference concurrency cap -- OUTER to
            # (strictly more restrictive than, never a replacement for) each backend's own
            # per-model bulkhead above. Applied here so it governs every real call this universal
            # choke point makes, both streaming and non-streaming, regardless of caller.
            async with _local_inference_semaphore:
                if task_id:
                    # Real token streaming (the project owner's own explicit follow-up:
                    # "like Claude does it in the chat" -- chunk by chunk as
                    # the model actually generates, not a replay of an
                    # already-finished response) -- only taken when someone
                    # is genuinely able to watch (task_id present); a bare
                    # tooling/test call with no task_id stays on the plain,
                    # slightly cheaper non-streaming path below, unchanged.
                    result = await self._generate_inner_streaming(
                        pool, model, messages, temperature, max_tokens, timeout_sec,
                        task_id, actor, call_label, response_format, node_id,
                    )
                else:
                    result = await self._generate_inner(
                        pool, model, messages, temperature, max_tokens, timeout_sec, response_format,
                    )
        except Exception as exc:
            if task_id:
                self._record_call_stat(task_id, time.monotonic() - _call_started_at)
                from manager.trace import publish_trace_event
                publish_trace_event(task_id, {
                    "level": "manager" if not actor or actor == "manager" else "specialist",
                    "actor": actor or "manager", "phase": "llm_call", "status": "failed",
                    "message": f"{model} call failed: {exc}",
                    "node_id": node_id,
                    # messages included here too -- the historical-replay
                    # path (loadHistoricalTraces()) only keeps the
                    # passed/failed copy of each call, never the
                    # transient running one, so the prompt has to travel
                    # on this event to survive a page reload.
                    "llm_call": {"model": model, "messages": messages, "call_label": call_label, "error": str(exc)},
                })
            raise

        if task_id:
            self._record_call_stat(task_id, time.monotonic() - _call_started_at)
            from manager.trace import publish_trace_event
            publish_trace_event(task_id, {
                "level": "manager" if not actor or actor == "manager" else "specialist",
                "actor": actor or "manager", "phase": "llm_call", "status": "passed",
                "message": call_label or f"{model} responded",
                "node_id": node_id,
                "llm_call": {"model": model, "messages": messages, "response": result, "call_label": call_label},
            })
        return result

    def _record_call_stat(self, task_id: str, duration_sec: float) -> None:
        """Phase 30, P1d: counts every real attempt (success or failure)
        -- a retried/failed call still consumes real GPU time and is
        real resource cost, exactly what this visibility phase exists to
        surface. See `pop_call_stats()` for the read side.
        """
        stat = self._call_stats.setdefault(task_id, {"count": 0, "duration_sec": 0.0})
        stat["count"] += 1
        stat["duration_sec"] += duration_sec

    def pop_call_stats(self, task_id: str) -> dict:
        """Reads and clears this task's accumulated call count/duration
        -- 'pop' so the round loop calling this once per round naturally
        gets PER-ROUND stats (not a cumulative whole-task total), with no
        extra bookkeeping required at any calling site. Returns zeros
        (never a crash) for a task_id that made no calls since the last
        pop, or that never made any at all.
        """
        return self._call_stats.pop(task_id, {"count": 0, "duration_sec": 0.0})

    async def _wait_for_ram_or_raise(
        self, pool: "_BackendPool", task_id: str | None = None, actor: str | None = None,
    ) -> None:
        """Real infra change, 2026-07-22: GPU Worker 01 (coder) crashed
        earlier tonight under concurrent load from our own agents --
        system RAM exceeded what was actually available on the host,
        taking the whole box down for 30+ minutes mid demo-prep. Operator's
        own infra-level fix (a swap disk, a host-level block above
        44GB, capping every other VM so the box's own total can never
        exceed 64GB) is the primary defense; this is the cooperative,
        application-level second layer -- never let OUR OWN calls be
        the ones that push a resident host over its ceiling. See
        infra/gpu_capacity_guard.py's own module docstring for why this
        has to be built here (vLLM has no built-in host-RAM admission
        control) and its fail-open behavior.

        Deliberately called from INSIDE _generate_inner()/
        _generate_inner_streaming(), AFTER their own `pool.breaker.
        is_open()` check, not from generate() before dispatch -- a real
        regression was caught live in this session's own testing:
        checking RAM before the breaker-open check added a real network
        round-trip (up to 5s) even when the breaker was ALREADY open and
        should fail instantly, defeating the whole "fail fast on a known-
        dead backend" design the circuit breaker exists for. RAM
        pressure is only worth checking when we're actually about to
        make a real, fresh attempt.
        """
        def _on_wait(used_bytes: float, ceiling_bytes: float) -> None:
            if not task_id:
                return
            from manager.trace import publish_trace_event
            publish_trace_event(task_id, {
                "level": "manager" if not actor or actor == "manager" else "specialist",
                "actor": actor or "manager", "phase": "gpu_ram_guard", "status": "running",
                "message": (
                    f"{pool.name} RAM at {used_bytes / 1024**3:.1f} GiB, at/above the "
                    f"{ceiling_bytes / 1024**3:.1f} GiB ceiling -- waiting for headroom before "
                    f"dispatching, rather than firing into an already-strained host."
                ),
            })
        try:
            await wait_for_ram_headroom(
                pool.http, pool.base_url, pool.name,
                ceiling_fraction=self._ram_ceiling_fraction,
                ceiling_bytes=self._ram_ceiling_bytes,
                poll_interval_sec=self._ram_check_poll_interval_sec,
                max_wait_sec=self._ram_check_max_wait_sec,
                on_wait=_on_wait,
            )
        except TimeoutError as exc:
            raise GatewayUnavailableError(
                f"{pool.name} stayed at/above its RAM ceiling for the full wait budget "
                f"({self._ram_check_max_wait_sec:.0f}s) -- refusing to dispatch: {exc}"
            ) from exc

    async def _generate_inner(
        self, pool: "_BackendPool", model: str, messages: list[dict], temperature: float,
        max_tokens: int | None, timeout_sec: float,
        response_format: dict | None = None,
    ) -> str:
        # no_think's /no_think suffix is applied by the caller (generate())
        # before this is reached -- see the comment there.
        if pool.breaker.is_open():
            raise CircuitOpenError(
                f"Circuit open for backend {pool.name!r} -- too many recent failures, "
                f"cooling down."
            )
        await self._wait_for_ram_or_raise(pool)

        payload = {
            "model": _wire_model_id(model),
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        _rep_detect = _repetition_detection_param(model)
        if _rep_detect is not None:
            payload["repetition_detection"] = _rep_detect
        payload.update(_repetition_prevention_params(model))

        last_exc: Exception | None = None
        max_attempts = MAX_RETRY_ATTEMPTS
        async with pool.bulkhead.semaphore():
            attempt = 1
            while attempt <= max_attempts:
                try:
                    resp = await pool.http.post(
                        "/chat/completions",
                        json=payload,
                        timeout=httpx.Timeout(timeout_sec),
                    )
                    resp.raise_for_status()
                    pool.breaker.record_success()
                    return resp.json()["choices"][0]["message"]["content"]
                # Real, confirmed live gap (2026-08-07): `httpx.ReadError` (a real, common failure
                # for a dropped connection mid-response) is NOT a subclass of `httpx.ConnectError`
                # or `httpx.TimeoutException` -- it was silently uncaught here, propagating straight
                # out as an unretried, bare "ReadError:" failure. `httpx.TransportError` is the
                # shared parent of ConnectError/ReadError/WriteError/TimeoutException/ProtocolError
                # -- catching it (plus HTTPStatusError separately, for status-code-specific logic
                # below) is httpx's own documented pattern for broad, general retry coverage.
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    last_exc = exc
                    is_concurrency_limit = _is_concurrency_limit_error(exc)
                    if is_concurrency_limit:
                        # Real, confirmed live gap (2026-08-07): a concurrency-limit rejection
                        # means the backend responded FAST and is genuinely healthy, just busy --
                        # same reasoning as LLMRepetitionLoopError below; must never count toward
                        # the circuit breaker, and gets a more generous retry ceiling since each
                        # attempt is cheap.
                        if max_attempts < MAX_RETRY_ATTEMPTS_AFTER_CONCURRENCY_LIMIT:
                            max_attempts = MAX_RETRY_ATTEMPTS_AFTER_CONCURRENCY_LIMIT
                    else:
                        pool.breaker.record_failure()
                        if pool.breaker.is_open():
                            break  # No point retrying further -- the breaker just tripped.
                    if attempt < max_attempts:
                        delay = min(
                            RETRY_MAX_DELAY_SEC,
                            RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)),
                        )
                        delay += random.uniform(0, delay * 0.25)  # jitter
                        await asyncio.sleep(delay)
                    attempt += 1

        raise GatewayUnavailableError(
            f"generate() failed after retries against backend {pool.name!r}: {last_exc}"
        ) from last_exc

    async def _generate_inner_streaming(
        self, pool: "_BackendPool", model: str, messages: list[dict], temperature: float,
        max_tokens: int | None, timeout_sec: float,
        task_id: str, actor: str | None, call_label: str | None,
        response_format: dict | None = None,
        node_id: str | None = None,
    ) -> str:
        """Real streaming variant of _generate_inner(): the SAME retry/
        breaker/bulkhead discipline, but reads the OpenAI-compatible
        SSE stream (`stream: true`) line by line and publishes each
        accumulated chunk of real, actually-generated text live on
        oma:trace:{task_id} as it arrives -- not the finished response
        replayed after the fact. A retry after a failed/partial stream
        starts a genuinely fresh stream (and the UI sees a fresh
        sequence of deltas from empty) rather than trying to splice
        together a partial one, since a partial generation isn't a
        valid prefix of what a fresh attempt will produce.
        """
        from manager.trace import publish_trace_event

        if pool.breaker.is_open():
            raise CircuitOpenError(
                f"Circuit open for backend {pool.name!r} -- too many recent failures, "
                f"cooling down."
            )
        await self._wait_for_ram_or_raise(pool, task_id, actor)

        payload = {
            "model": _wire_model_id(model),
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        _rep_detect = _repetition_detection_param(model)
        if _rep_detect is not None:
            payload["repetition_detection"] = _rep_detect
        payload.update(_repetition_prevention_params(model))

        level = "manager" if not actor or actor == "manager" else "specialist"

        async def _read_stream() -> str:
            full_text = ""
            buffer = ""
            last_flush = time.monotonic()
            repeat_run = 0
            async with pool.http.stream(
                "POST", "/chat/completions", json=payload, timeout=httpx.Timeout(timeout_sec),
            ) as resp:
                if resp.status_code >= 400:
                    # Real bug found live (2026-07-24, 50-task sequential
                    # re-run, task 001): plain `resp.raise_for_status()`
                    # on a streaming response discards the actual response
                    # BODY -- vLLM/llama-swap return a real, specific JSON
                    # error explaining WHY a request was rejected (bad
                    # request shape, context-length exceeded, an invalid
                    # sampling parameter, etc.), but every caller only
                    # ever saw the generic "Client error '400 Bad Request'
                    # for url '...'" text, with zero way to tell a
                    # malformed-request bug in THIS codebase apart from a
                    # genuine, unrelated backend issue. Same class of gap
                    # already fixed twice this session at higher layers
                    # (manager/compensations.py, manager/gateway_
                    # orchestration.py) -- this is the actual root call
                    # site underneath both of those. Body must be read
                    # explicitly before the response is closed, since a
                    # streaming response's body isn't automatically
                    # buffered the way a plain request's is.
                    body_text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise httpx.HTTPStatusError(
                        f"{resp.status_code} {resp.reason_phrase} for url {resp.url!r} -- "
                        f"real response body: {body_text[:2000]}",
                        request=resp.request, response=resp,
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {}).get("content", "") or ""
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if not delta:
                        continue
                    full_text += delta
                    buffer += delta
                    # Repetition-loop gate: cheap substring search on
                    # every delta, not just periodically -- a stuck
                    # model can burn through its whole repeat count in
                    # well under a second once it starts looping, so
                    # this can't wait for the next scheduled flush.
                    # Deliberately a substring search (not a fixed-
                    # boundary equality check) since deltas arrive in
                    # small, arbitrary-sized increments (often a few
                    # characters at a time) that won't line up on an
                    # exact multiple of the window size -- an equality
                    # check at a fixed offset would almost never catch
                    # a real periodic repeat in practice.
                    window = _REPETITION_WINDOW_CHARS
                    if len(full_text) >= window * 2:
                        tail = full_text[-window:]
                        prior_span = full_text[-(_REPETITION_MIN_REPEATS + 1) * window : -window]
                        if tail in prior_span:
                            repeat_run += 1
                            if repeat_run >= _REPETITION_MIN_REPEATS:
                                raise LLMRepetitionLoopError(
                                    f"{call_label}: model {model!r} repeated the same "
                                    f"{window}-char span {repeat_run}+ times after "
                                    f"{len(full_text)} chars generated -- aborting early rather "
                                    f"than waiting out the full timeout."
                                )
                        else:
                            repeat_run = 0
                    if len(full_text) >= _REPETITION_MAX_TOTAL_CHARS:
                        raise LLMRepetitionLoopError(
                            f"{call_label}: model {model!r} generated {len(full_text)} chars "
                            f"without finishing -- no legitimate single response from this "
                            f"codebase's own real call sites comes anywhere close to "
                            f"{_REPETITION_MAX_TOTAL_CHARS} chars; this is a coarse, orthogonal "
                            f"backstop against a genuine non-convergent loop the window-based "
                            f"check above didn't catch (e.g. content drifting just enough between "
                            f"repeats at a nonzero temperature to defeat an exact-substring match), "
                            f"aborting early rather than waiting out the full timeout."
                        )
                    now = time.monotonic()
                    if len(buffer) >= DELTA_FLUSH_CHAR_THRESHOLD or (now - last_flush) >= DELTA_FLUSH_INTERVAL_SEC:
                        publish_trace_event(task_id, {
                            "level": level, "actor": actor or "manager", "phase": "llm_call",
                            "status": "delta", "message": call_label,
                            "node_id": node_id,
                            "llm_call": {"model": model, "delta": buffer, "call_label": call_label},
                        })
                        buffer = ""
                        last_flush = now
                if buffer:
                    publish_trace_event(task_id, {
                        "level": level, "actor": actor or "manager", "phase": "llm_call",
                        "status": "delta", "message": call_label,
                        "node_id": node_id,
                        "llm_call": {"model": model, "delta": buffer, "call_label": call_label},
                    })
            return full_text

        last_exc: Exception | None = None
        max_attempts = MAX_RETRY_ATTEMPTS
        async with pool.bulkhead.semaphore():
            attempt = 1
            while attempt <= max_attempts:
                try:
                    # Real, confirmed bug found live (a task genuinely
                    # hung for 16+ minutes on a single call, blocking its
                    # whole round loop -- cooperative cancel could never
                    # reach a round boundary to even check the cancel
                    # flag): httpx's own per-request timeout only resets
                    # on ANY received byte, including keep-alive noise --
                    # a stream that's technically "alive" but never making
                    # real progress can run far past timeout_sec with
                    # httpx's own timeout never firing. asyncio.wait_for()
                    # is a genuine, independent hard ceiling that doesn't
                    # depend on httpx's own byte-level timeout semantics.
                    # A generous multiplier (not timeout_sec itself) since
                    # real, legitimately slow generations observed live
                    # this session (large Code-Review outputs) took well
                    # over timeout_sec already and are not failures.
                    #
                    # Real, confirmed follow-up (2026-08-09, the project owner's own explicit ask, task
                    # 07141af5's flagship run): for the largest prompts, `timeout_sec` itself can
                    # already reach 600s (`_scoped_edit_timeout_sec_for_prompt()`'s own cap in
                    # specialists/build/specialist.py), making this multiplier's own ceiling up to
                    # 1800s (30 minutes) for a single attempt -- confirmed live as one contributing
                    # factor in a round that ran long enough to outlast an external wrapper's own
                    # shorter timeout. Capped at 900s absolute: comfortably above every real,
                    # legitimately slow call observed this session, and now that
                    # `_REPETITION_MAX_TOTAL_CHARS` above independently aborts a genuine
                    # non-convergent loop within roughly 1-3 minutes regardless of `timeout_sec`,
                    # this ceiling only ever matters for a backend that is genuinely still
                    # producing real, growing, non-repeating content -- 900s is still generous for
                    # that case, just no longer a full 30-minute worst case.
                    hard_ceiling_sec = min(900.0, timeout_sec * 3)
                    full_text = await asyncio.wait_for(_read_stream(), timeout=hard_ceiling_sec)
                    pool.breaker.record_success()
                    return full_text
                except LLMRepetitionLoopError as exc:
                    last_exc = exc
                    # Real bug found live (2026-07-24, task 020 resume):
                    # a repetition loop means the backend responded FAST
                    # and is genuinely healthy -- it's a content/sampling
                    # problem with this specific prompt, not a backend-
                    # availability problem, so it must never count toward
                    # the circuit breaker the way a real timeout/HTTP/
                    # connection failure does. Confirmed live this was
                    # actively harmful: MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP
                    # (6) alone exceeds CIRCUIT_FAILURE_THRESHOLD (5), so a
                    # single genuinely-looping call reliably tripped the
                    # breaker via its OWN retries -- which then immediately
                    # blocked the specialists/build/specialist.py fallback
                    # (the very next call to this same backend, right after
                    # exhausting retries here) with a self-inflicted
                    # CircuitOpenError, even though the backend never
                    # stopped being healthy for even one second. Not
                    # calling record_failure() here leaves the breaker's
                    # state entirely to real transport/HTTP failures, where
                    # it's actually diagnostic of backend health.
                    #
                    # Extend the ceiling (once) the first time this specific
                    # call hits a genuine repetition loop -- see
                    # MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP's own comment:
                    # a fast-failing attempt costs almost nothing, so it's
                    # worth giving sampling randomness more chances rather
                    # than exhausting the whole call after the base count.
                    if max_attempts < MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP:
                        max_attempts = MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP
                    # Nudge temperature up so the retry isn't a guaranteed
                    # byte-for-byte replay of the same loop -- see
                    # _REPETITION_RETRY_TEMPERATURE_STEP's own comment above.
                    current_temperature = payload.get("temperature", 0.0) or 0.0
                    if current_temperature < _REPETITION_RETRY_MAX_TEMPERATURE:
                        payload["temperature"] = min(
                            _REPETITION_RETRY_MAX_TEMPERATURE,
                            current_temperature + _REPETITION_RETRY_TEMPERATURE_STEP,
                        )
                    if pool.breaker.is_open():
                        break
                    if attempt < max_attempts:
                        delay = min(
                            RETRY_MAX_DELAY_SEC,
                            RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)),
                        )
                        delay += random.uniform(0, delay * 0.25)
                        await asyncio.sleep(delay)
                    attempt += 1
                    continue
                except asyncio.TimeoutError as exc:
                    last_exc = exc
                    pool.breaker.record_failure()
                    if pool.breaker.is_open():
                        break
                    if attempt < max_attempts:
                        delay = min(
                            RETRY_MAX_DELAY_SEC,
                            RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)),
                        )
                        delay += random.uniform(0, delay * 0.25)
                        await asyncio.sleep(delay)
                    attempt += 1
                    continue
                # Real, confirmed live gap (2026-08-07, real error text: "generate() (streaming)
                # failed after retries against backend 'gpu_worker_01_coder': ReadError:" -- an
                # empty message since the exception type itself was never caught by name, only
                # ever escaping this loop entirely uncaught): `httpx.ReadError` (a genuinely common
                # failure for an SSE stream connection dropping mid-read -- confirmed via direct
                # research this is httpx's own documented behavior for a broken streaming
                # connection) is NOT a subclass of ConnectError or TimeoutException, so it was
                # silently never retried at all, unlike every other transport failure. Catching
                # `httpx.TransportError` (the shared parent of ConnectError/ReadError/WriteError/
                # TimeoutException/ProtocolError) instead of an incomplete manual list is httpx's
                # own documented pattern for broad, general retry coverage.
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    last_exc = exc
                    is_concurrency_limit = _is_concurrency_limit_error(exc)
                    if is_concurrency_limit:
                        # Real, confirmed live gap (2026-08-07): the coder backend's own real 429
                        # body explicitly says "Retry shortly; do not fan out parallel streams" --
                        # a concurrency-limit rejection means the backend responded FAST and is
                        # genuinely healthy, just busy (same reasoning as LLMRepetitionLoopError
                        # above); must never count toward the circuit breaker, and gets a more
                        # generous retry ceiling since each attempt is cheap.
                        if max_attempts < MAX_RETRY_ATTEMPTS_AFTER_CONCURRENCY_LIMIT:
                            max_attempts = MAX_RETRY_ATTEMPTS_AFTER_CONCURRENCY_LIMIT
                    else:
                        pool.breaker.record_failure()
                        if pool.breaker.is_open():
                            break
                    if attempt < max_attempts:
                        delay = min(
                            RETRY_MAX_DELAY_SEC,
                            RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)),
                        )
                        delay += random.uniform(0, delay * 0.25)
                        await asyncio.sleep(delay)
                    attempt += 1

        if isinstance(last_exc, LLMRepetitionLoopError):
            raise LLMRepetitionLoopExhaustedError(
                f"generate() (streaming) exhausted every retry attempt on a repetition loop "
                f"against backend {pool.name!r} -- this is NOT a genuine infrastructure outage "
                f"(the backend itself is healthy; the model repeatedly failed to generate this "
                f"specific content without looping, even after {max_attempts} attempts with "
                f"escalating temperature): {last_exc}"
            ) from last_exc
        raise GatewayUnavailableError(
            f"generate() (streaming) failed after retries against backend {pool.name!r}: {last_exc}"
        ) from last_exc
