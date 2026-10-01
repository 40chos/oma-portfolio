"""P13 items 12/22/23/24 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
2026-07-29.md §22.2): the trigger condition, cost ceiling, sign-off-interaction decision, AND (as
of 2026-08-02, the project owner's own explicit go-ahead) the real cloud API client for escalating exactly
three named Classifier/Planner/Judge call sites to a cheap cloud-tier model.

This module is the governance layer four things sit on top of:

- Item 22 (trigger condition): `decide_cloud_escalation()` -- eligible call site + mechanism
  explicitly enabled + caller-flagged local-model uncertainty + per-task cost budget available.
  No auto-detected local-model confidence signal exists anywhere in this codebase yet (a real,
  named open gap) -- uncertainty is caller-supplied, never invented here.
- Item 23 (cost/governance mechanics): `CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD`, a hard
  per-task ceiling matching item 12's own re-verified cost estimate ($0.03-$0.05/task); logging
  follows the exact same informational, non-gating shape `manager/learning.py`'s
  `flag_llm_call_count_outlier()` (P1d) already established.
- Item 24 (sign-off interaction, policy decision): resolved as explicit, deliberate scope --
  cloud escalation stays fully INDEPENDENT of the existing tier-3/4 human sign-off gate. No new
  sign-off UX is introduced; a task's own tier-3/4 gate (check_sensitive_paths()) already governs
  whether a human must approve before ANY specialist runs at all, before this mechanism is ever
  reached -- these three call sites are cheap, low-risk classification/routing calls, not new
  write actions, so they do not need a second, parallel approval gate.

Item 12's own required policy surfacing ("must be surfaced to the project owner explicitly... never folded
in silently") is implemented here as a real, inspectable, off-by-default gate
(`cloud_escalation_enabled()`) rather than a comment: escalation is structurally impossible unless
BOTH a real API key is explicitly provisioned AND the mechanism is explicitly turned on -- there is
no code path that silently starts spending money on cloud calls.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Item 12's own confirmed, critique-verified three call sites -- Classifier/Router,
# Planner/Decomposer, and Judge/retry-strategist (item 2's role mapping), one-to-one. Never
# Build, Code-Review, or Testing/QA -- item 12's own strongest finding (Test 2 of the live A/B)
# is that a stronger model in Build's own seat did NOT fix its failure class, so escalating
# generation-layer calls has no evidence behind it and is explicitly out of scope here.
CLOUD_ESCALATION_CALL_SITES = frozenset({
    "classify_capability_class",
    "decompose_into_constraints",
    "select_specialist_for_retry",
})

# Phase 30 §26 item 9 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
# 2026-07-29.md): documentation-only note, no code change -- if Build's generation step is ever
# widened into this mechanism (it is explicitly NOT one of the 3 call sites above today, see the
# comment on CLOUD_ESCALATION_CALL_SITES), do not assume the SAME output-format instructions that
# work for qwen3-coder-30b-a3b transfer identically to claude-sonnet-5 (or whichever cloud model).
# Confirmed live, 2026-08-04 (the scoped Sonnet-5 A/B experiment this section's own §26.0 refers
# to): Sonnet correctly wrote real, non-empty, well-reasoned module content on both test tasks --
# closing the "empty class" failure mode qwen3-coder-30b-a3b showed -- but still guessed a WRONG
# model name (`meerwerk.order` instead of the real `project.meerwerk`) on one of the two, and its
# own `notes` field explicitly said the real target schema/prior module content was never in its
# prompt. This independently reconfirms §26.1/§26.2's own root-cause finding (a context/prompt-
# assembly gap, not a pure model-capability ceiling) rather than contradicting it -- but ALSO
# means a wider cloud-escalation path into Build's generation step must not assume Sonnet needs
# LESS scaffolding than the local model; a real audit of which of the existing prompt's format
# instructions still make sense for the substituted model is required before ever widening this
# mechanism into Build's own seat, exactly as this item originally specified.

# Item 23: matches item 12's own re-verified cost estimate for ~4-6 short classification/routing
# calls per task ($0.03-$0.05/task on Haiku 4.5 / Sonnet 5 intro pricing) -- deliberately set to
# the TOP of that range, not the middle, so an ordinary task's real cost never brushes against
# it; this ceiling exists to catch a genuinely runaway task (an unusually high retry count driving
# many more than the normal 4-6 escalation-eligible calls), not to constrain ordinary usage.
CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD = 0.05


def is_eligible_call_site(call_site: str) -> bool:
    return call_site in CLOUD_ESCALATION_CALL_SITES


def cloud_escalation_enabled() -> bool:
    """Off by default. Requires BOTH a real, explicitly-provisioned API key (never assumed
    present -- read dynamically per this workspace's own secret-hygiene rule, never hardcoded)
    AND an explicit opt-in flag. Either one missing means this mechanism does not exist at
    runtime, at all -- the safe, conservative default this whole feature ships with.
    """
    return (
        bool(os.environ.get("OMA_CLOUD_ESCALATION_API_KEY"))
        and os.environ.get("OMA_CLOUD_ESCALATION_ENABLED", "").strip().lower() in ("1", "true", "yes")
    )


@dataclass
class CloudEscalationDecision:
    should_escalate: bool
    reason: str


def decide_cloud_escalation(
    call_site: str, local_model_uncertain: bool, task_cost_so_far_usd: float,
) -> CloudEscalationDecision:
    """Item 22: the real, measurable trigger condition. Escalates only when ALL of:
    (1) `call_site` is one of the exact 3 named sites -- never a generation-layer call;
    (2) the mechanism is enabled (real key configured AND explicitly turned on);
    (3) the caller has flagged genuine local-model uncertainty for THIS call -- caller-supplied,
        since no auto-detected local-model confidence signal exists anywhere in this codebase yet
        (a real, still-open gap this item's own source text names, not invented here);
    (4) this task's own running cloud-escalation spend hasn't already reached the per-task
        ceiling (item 23).
    Never raises, never makes a network call -- a pure decision function.
    """
    if not is_eligible_call_site(call_site):
        return CloudEscalationDecision(False, "call_site is not one of the 3 eligible sites")
    if not cloud_escalation_enabled():
        return CloudEscalationDecision(False, "cloud escalation not enabled (no API key configured, or not turned on)")
    if not local_model_uncertain:
        return CloudEscalationDecision(False, "no flagged local-model uncertainty for this call")
    if task_cost_so_far_usd >= CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD:
        return CloudEscalationDecision(False, "this task's per-task cloud-escalation cost ceiling is already reached")
    return CloudEscalationDecision(True, "eligible call site, flagged uncertain, budget available")


def build_cloud_escalation_log_event(
    call_site: str, decision: CloudEscalationDecision, task_cost_so_far_usd: float,
) -> dict:
    """Item 23's own logging requirement -- same informational, never-gating shape
    `manager/learning.py`'s `flag_llm_call_count_outlier()` (P1d) already established. Callers
    log this via the existing `append_project_memory(event_type="note", tags=["cloud_escalation"],
    ...)` convention, same as every other P1d-family informational flag -- no new logging
    mechanism, no new Loki/Langfuse schema.
    """
    return {
        "call_site": call_site,
        "should_escalate": decision.should_escalate,
        "reason": decision.reason,
        "task_cost_so_far_usd": task_cost_so_far_usd,
        "cost_ceiling_usd": CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD,
        "message": (
            f"Cloud-tier escalation {'triggered' if decision.should_escalate else 'not triggered'} "
            f"for {call_site!r}: {decision.reason}."
        ),
    }


# --- 2026-08-02: the real cloud API client, per the project owner's own explicit go-ahead ---------------
#
# Real, provisioned secrets (OMA_CLOUD_ESCALATION_API_KEY / OMA_CLOUD_ESCALATION_MODEL=claude-
# sonnet-5) now exist in .env. OMA_CLOUD_ESCALATION_ENABLED is deliberately left unset -- this
# code is built, tested, and ready, but structurally cannot spend anything until the project owner himself
# sets that flag (cloud_escalation_enabled() above already enforces this; nothing below bypasses
# it). Both env vars are read fresh on every call, never cached at import time or logged/printed
# anywhere, per this workspace's own secret-hygiene rule.

_ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_API_VERSION = "2023-06-01"
_DEFAULT_CLOUD_MODEL = "claude-sonnet-5"

# Published Sonnet-tier per-token pricing, used only for a LOCAL cost estimate before the real
# API call returns real token usage -- the real, authoritative cost below is always computed
# from the actual response's own usage.input_tokens/usage.output_tokens, never guessed in
# advance. Matches the same estimate/caveat discipline already used this session for the
# isolated Haiku/Sonnet spot-checks (docs/reports/PHASE30_P14_ITEM3_*_2026-08-02.md).
_CLOUD_INPUT_COST_PER_TOKEN_USD = 3.0 / 1_000_000
_CLOUD_OUTPUT_COST_PER_TOKEN_USD = 15.0 / 1_000_000

# 2026-08-02, real gap found by independent review after the client above shipped:
# decide_cloud_escalation()'s cost-ceiling check only guards against SPEND ALREADY RECORDED --
# it has no pre-flight estimate of the call it's about to make, so an unbounded raw goal/
# request_text embedded straight into a cloud prompt could make a single call cost far more than
# the whole $0.05/task ceiling before that ceiling ever has a chance to react. Fixed with a fixed,
# simple character cap on the caller-supplied task text specifically (not the whole prompt --
# the surrounding instruction text this module's own callers add is already short and fixed-size)
# -- truncation, not a real cost-estimate function, since classify_capability_class and
# decompose_into_constraints_with_artifacts are both classification/decomposition calls that don't
# need the full raw text to work correctly, and a fixed cap is far easier to reason about and test
# than a token-estimator. At ~6000 chars (roughly 1500 input tokens, ~4 chars/token), the worst-
# case single-call input cost is bounded to roughly 1500 * $3/1M =~ $0.0045 -- well under the
# $0.05 ceiling even for one call, leaving real room for the ceiling's own accumulation check to
# still do its job across the handful of calls a real task actually makes.
_MAX_CLOUD_PROMPT_TASK_TEXT_CHARS = 6000


def truncate_for_cloud_prompt(text: str, max_chars: int = _MAX_CLOUD_PROMPT_TASK_TEXT_CHARS) -> str:
    """Caps caller-supplied task text (a goal or request_text) before it's embedded into a cloud
    prompt, so a single escalated call can never carry an unbounded amount of input regardless of
    how large the real task's own text is. Never raises -- text under the cap passes through
    unchanged; text over it is cut with a visible marker so the cloud model knows content was
    removed rather than silently seeing a truncated sentence as complete.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated for cloud escalation]"


class CloudEscalationError(RuntimeError):
    """Raised on a real, genuine cloud API failure (network error, non-200 response, malformed
    body). Callers must catch this and fall back to the local model -- a cloud outage must never
    block a task, matching this whole mechanism's own "fully independent, no new blocking gate"
    design (item 24).
    """


@dataclass
class CloudEscalationResult:
    text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


async def call_cloud_escalation_model(
    prompt: str, max_tokens: int = 1024, timeout_sec: float = 30.0,
) -> CloudEscalationResult:
    """The real Anthropic Messages API call. Reads OMA_CLOUD_ESCALATION_API_KEY/
    OMA_CLOUD_ESCALATION_MODEL from the environment fresh on every call -- never cached at
    import time, never logged, never included in any exception message or return value. Raises
    CloudEscalationError on any real failure (network, non-2xx, malformed response) -- never
    returns a guessed/partial result. The real cost is computed from the response's own actual
    token usage, never estimated in advance.
    """
    api_key = os.environ.get("OMA_CLOUD_ESCALATION_API_KEY")
    if not api_key:
        raise CloudEscalationError("OMA_CLOUD_ESCALATION_API_KEY is not set")
    model = os.environ.get("OMA_CLOUD_ESCALATION_MODEL", _DEFAULT_CLOUD_MODEL)

    try:
        async with httpx.AsyncClient(base_url=_ANTHROPIC_API_BASE_URL, timeout=timeout_sec) as http:
            response = await http.post(
                "/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": _ANTHROPIC_API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
    except httpx.HTTPError as exc:
        raise CloudEscalationError(f"cloud escalation network error: {exc!r}") from exc

    if response.status_code != 200:
        # Never include the raw response body verbatim in the exception -- a provider error
        # response could conceivably echo request headers back; status code and a truncated,
        # sanitized snippet is enough to diagnose without any risk of leaking the key.
        raise CloudEscalationError(
            f"cloud escalation API returned HTTP {response.status_code}"
        )

    try:
        body = response.json()
        content_blocks = body["content"]
        text = "".join(block.get("text", "") for block in content_blocks if block.get("type") == "text")
        usage = body.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
    except (KeyError, ValueError, TypeError) as exc:
        raise CloudEscalationError(f"cloud escalation returned a malformed response: {exc!r}") from exc

    cost_usd = (
        input_tokens * _CLOUD_INPUT_COST_PER_TOKEN_USD
        + output_tokens * _CLOUD_OUTPUT_COST_PER_TOKEN_USD
    )
    return CloudEscalationResult(
        text=text, cost_usd=cost_usd, input_tokens=input_tokens, output_tokens=output_tokens,
    )


# --- Per-task cloud-escalation spend tracker ---------------------------------------------------
# Mirrors infra/gateway_client.py's own ModelGatewayClient._call_stats/pop_call_stats() shape --
# same "bare dict keyed by task_id, popped by the round loop" discipline, not a second, competing
# tracking mechanism. Module-level (not per-client-instance) since the three escalation-eligible
# call sites live in three different files/modules and don't share a single client object today.
_task_cloud_spend: dict[str, float] = {}


def get_cloud_spend_so_far(task_id: str | None) -> float:
    if not task_id:
        return 0.0
    return _task_cloud_spend.get(task_id, 0.0)


def record_cloud_spend(task_id: str | None, cost_usd: float) -> None:
    if not task_id:
        return
    _task_cloud_spend[task_id] = _task_cloud_spend.get(task_id, 0.0) + cost_usd


def clear_cloud_spend(task_id: str | None) -> float:
    """Pops and returns this task's own total cloud-escalation spend -- called once, at task
    completion (same 'pop, don't just peek' discipline as pop_call_stats()), so this module-level
    dict never accumulates entries for tasks that have already finished.
    """
    if not task_id:
        return 0.0
    return _task_cloud_spend.pop(task_id, 0.0)


# Phase 31 §1 (2026-08-07): a real, concrete check-then-act race against `_task_cloud_spend`,
# made urgent (not just theoretical) by §1's own architect-stage upgrade -- up to 3 concurrent
# calls (2 biased drafts + baseline) now genuinely run from this exact call site simultaneously,
# via asyncio.gather, even before any node-level concurrency is ever turned on. Without a lock,
# two concurrent callers can both read the same cost_so_far below the ceiling, both decide to
# escalate, and both record afterward -- together exceeding the per-task ceiling by up to one
# extra call's real cost. This module-level lock guards ONLY the read-ceiling-then-reserve step
# and the two post-call adjustment steps below -- never held across the real network call itself.
_cloud_spend_lock = asyncio.Lock()


def _estimate_worst_case_cloud_cost_usd(prompt: str, max_tokens: int) -> float:
    """A deliberately worst-case (never an underestimate) cost bound for the atomic reservation
    in `maybe_escalate_to_cloud()` below -- input tokens estimated at a conservative ~4
    chars/token (real tokenization is never coarser than this for English/code text, so this
    never undercounts), output tokens assumed to hit `max_tokens` exactly (the real worst case a
    caller could ever see). The real, measured cost from the response's own actual token usage
    replaces this estimate immediately after the call returns (see the refund step below) -- this
    estimate only ever needs to hold the ceiling check honest for the brief window the real call
    is in flight.
    """
    estimated_input_tokens = len(prompt) / 4
    return (
        estimated_input_tokens * _CLOUD_INPUT_COST_PER_TOKEN_USD
        + max_tokens * _CLOUD_OUTPUT_COST_PER_TOKEN_USD
    )


async def maybe_escalate_to_cloud(
    call_site: str, local_model_uncertain: bool, task_id: str | None, prompt: str, max_tokens: int = 1024,
) -> str | None:
    """The one, shared entry point all three eligible call sites use -- ties decide_cloud_
    escalation() + the real client + per-task cost tracking + item 23's own logging together, so
    no call site duplicates this wiring independently. Returns the cloud model's real text on a
    genuine escalation, or None whenever escalation doesn't apply OR the real cloud call itself
    failed -- callers must treat None as "fall back to the local model," never as an error to
    propagate (a cloud outage must never block a task, item 24's own design).
    """
    cost_so_far = get_cloud_spend_so_far(task_id)
    decision = decide_cloud_escalation(call_site, local_model_uncertain, cost_so_far)
    log_event = build_cloud_escalation_log_event(call_site, decision, cost_so_far)
    logger.info(log_event["message"])
    if not decision.should_escalate:
        return None

    # Atomic check-the-ceiling-and-reserve: re-checks the ceiling under the lock (the snapshot
    # `decide_cloud_escalation()` used above may already be stale by the time we get here) and,
    # if room remains, immediately reserves the worst-case estimate -- so a second concurrent
    # caller arriving right behind this one sees the reservation, not the pre-call balance.
    estimated_cost_usd = _estimate_worst_case_cloud_cost_usd(prompt, max_tokens)
    async with _cloud_spend_lock:
        cost_so_far = get_cloud_spend_so_far(task_id)
        if cost_so_far >= CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD:
            logger.info(
                "cloud escalation for %r skipped: ceiling reached by a concurrent reservation "
                "since the initial check", call_site,
            )
            return None
        record_cloud_spend(task_id, estimated_cost_usd)

    try:
        result = await call_cloud_escalation_model(prompt, max_tokens=max_tokens)
    except CloudEscalationError as exc:
        logger.warning("cloud escalation call for %r failed, falling back to local: %r", call_site, exc)
        # The call never happened -- refund the FULL reservation, never leaving a phantom charge
        # against a task that got nothing for it.
        async with _cloud_spend_lock:
            record_cloud_spend(task_id, -estimated_cost_usd)
        return None

    # Replace the worst-case reservation with the real, measured cost -- refunds the difference
    # (always <= 0 since the estimate is a genuine worst case) under one more brief acquisition.
    async with _cloud_spend_lock:
        record_cloud_spend(task_id, result.cost_usd - estimated_cost_usd)
    logger.info(
        "cloud escalation for %r succeeded: task_id=%s cost_usd=%.5f input_tokens=%d "
        "output_tokens=%d task_running_total_usd=%.5f",
        call_site, task_id, result.cost_usd, result.input_tokens, result.output_tokens,
        get_cloud_spend_so_far(task_id),
    )
    return result.text
