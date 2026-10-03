"""Phase 2 tests, revised 2026-07-13 for the two-dedicated-GPU-host
infra change: ModelGatewayClient + call_structured, against the two
real resident inference hosts --
GPU Worker 01 (192.0.2.11:9090, coder only) and
GPU Worker 02 (192.0.2.12:9090, the one shared qwen3.6-27b reasoning
model, served under the host alias `JA-GPU2-27B-INT4-64K`) -- per the
build plan's own instruction to test this against the real gateway
before building anything on top of it, plus a deliberately broken URL
to prove the retry/circuit-breaker/timeout behavior actually works, not
just that it looks right on paper.
"""

import asyncio
import json
import os
import sys
import time
from unittest.mock import patch

import httpx
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra import gateway_client as gw
from infra.gateway_client import (
    MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP,
    CircuitOpenError,
    GatewayUnavailableError,
    LLMRepetitionLoopError,
    LLMRepetitionLoopExhaustedError,
    ModelGatewayClient,
)
from infra.structured_output import call_structured, strip_think_block


CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")
BUILD_MODEL = os.environ.get("OMA_MODEL_BUILD", "qwen3-coder-30b-a3b")


async def _run_smoke_test():
    client = ModelGatewayClient()
    try:
        reasoning_models = await client.list_models(model_hint=CLASSIFIER_MODEL)
        assert "JA-GPU2-27B-INT4-64K" in reasoning_models, (
            f"expected GPU Worker 02's real model id on its /v1/models -- got {reasoning_models}"
        )
        print(f"PASS: GPU Worker 02 (reasoning) list_models() returned: {reasoning_models}")

        coder_models = await client.list_models(model_hint=BUILD_MODEL)
        assert BUILD_MODEL in coder_models, (
            f"expected {BUILD_MODEL!r} on GPU Worker 01's /v1/models -- got {coder_models}"
        )
        print(f"PASS: GPU Worker 01 (coder) list_models() returned: {coder_models}")

        reply = await client.generate(
            model=CLASSIFIER_MODEL,
            messages=[{"role": "user", "content": "Reply with exactly the word: pong"}],
            no_think=True,
            max_tokens=20,
        )
        assert reply.strip(), "expected a non-empty reply"
        cleaned = strip_think_block(reply)
        assert "pong" in cleaned.lower(), (
            f"expected 'pong' in cleaned reply, got raw={reply!r} cleaned={cleaned!r}"
        )
        print(f"PASS: real chat-completion call to {CLASSIFIER_MODEL} (routed to GPU Worker 02, "
              f"sent on the wire as JA-GPU2-27B-INT4-64K) returned (raw={reply!r}, cleaned={cleaned!r})")

        coder_reply = await client.generate(
            model=BUILD_MODEL,
            messages=[{"role": "user", "content": "Reply with exactly the word: pong"}],
            max_tokens=20,
        )
        assert "pong" in coder_reply.lower(), f"expected 'pong' from {BUILD_MODEL}, got {coder_reply!r}"
        print(f"PASS: real chat-completion call to {BUILD_MODEL} (routed to GPU Worker 01) "
              f"returned {coder_reply!r}")
    finally:
        await client.aclose()


async def _run_structured_output_test():
    client = ModelGatewayClient()

    class Color(BaseModel):
        name: str
        is_primary: bool

    try:
        result = await call_structured(
            client=client,
            model=CLASSIFIER_MODEL,
            prompt="Grass is green. Is green a primary color in RGB terms? "
                   "Respond with the color name and whether it's primary.",
            schema=Color,
        )
        assert isinstance(result, Color)
        assert result.name.strip() != ""
        print(f"PASS: call_structured() returned a valid Color: {result}")
    finally:
        await client.aclose()


async def _run_broken_url_test():
    """Prove the client actually fails the way it's supposed to against
    a dead endpoint -- retries with backoff, then raises
    GatewayUnavailableError, in bounded time (not hanging forever).
    """
    client = ModelGatewayClient(
        base_url_override="http://10.255.255.1:1/v1",  # non-routable, guaranteed to time out
    )
    # Use a short timeout so this test doesn't take minutes.
    start = time.monotonic()
    try:
        try:
            await client.generate(
                model="whatever",
                messages=[{"role": "user", "content": "hi"}],
                timeout_sec=2.0,
            )
            raise AssertionError("expected GatewayUnavailableError, call succeeded instead")
        except GatewayUnavailableError:
            elapsed = time.monotonic() - start
            print(f"PASS: broken URL correctly raised GatewayUnavailableError after {elapsed:.1f}s "
                  f"(bounded, not hanging)")
            assert elapsed < 60, "took far too long to give up -- retry/timeout logic is unbounded"
    finally:
        await client.aclose()


async def _run_circuit_breaker_test():
    """After enough consecutive failures, the breaker should open and
    subsequent calls should fail fast (CircuitOpenError) rather than
    going through the full retry dance again.
    """
    client = ModelGatewayClient(
        base_url_override="http://10.255.255.1:1/v1",
        max_concurrent=8,
    )
    try:
        # Drive enough failures to trip the breaker (threshold = 5).
        for _ in range(5):
            try:
                await client.generate(
                    model="whatever",
                    messages=[{"role": "user", "content": "hi"}],
                    timeout_sec=1.0,
                )
            except GatewayUnavailableError:
                pass

        start = time.monotonic()
        try:
            await client.generate(
                model="whatever",
                messages=[{"role": "user", "content": "hi"}],
                timeout_sec=1.0,
            )
            raise AssertionError("expected CircuitOpenError")
        except CircuitOpenError:
            elapsed = time.monotonic() - start
            print(f"PASS: circuit breaker correctly open, failed fast in {elapsed:.2f}s "
                  f"(no retry dance)")
            assert elapsed < 1.0, "breaker should fail immediately, not retry"
    finally:
        await client.aclose()


class _FakeRepeatingStreamResponse:
    """Fakes an httpx streaming response whose SSE body is a genuine,
    endless repeat of the same short span -- exactly the shape that
    trips LLMRepetitionLoopError. Records the `temperature` from every
    payload it's called with, so the test can confirm the retry loop
    actually varies it instead of resending an identical, guaranteed-
    to-loop-again request every time.
    """
    status_code = 200  # a real repetition loop is a 200 OK that never stops looping

    def __init__(self, recorded_temperatures: list[float]):
        self._recorded_temperatures = recorded_temperatures

    def __call__(self, method, url, json=None, timeout=None):
        self._recorded_temperatures.append(json.get("temperature"))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        # 40 repeats of a 21-char span -- comfortably past
        # _REPETITION_MIN_REPEATS (6) at _REPETITION_WINDOW_CHARS (120).
        chunk = "same twenty chars!!!" * 40
        for i in range(0, len(chunk), 20):
            piece = chunk[i:i + 20]
            data = json.dumps({"choices": [{"delta": {"content": piece}}]})
            yield f"data: {data}"
        yield "data: [DONE]"


async def _run_repetition_loop_temperature_escalation_test():
    """Real bug found live (2026-07-24, task 020 resume): a genuine
    repetition loop at temperature=0.0 (the Build specialist's default
    for scoped-edit/single-constraint rounds) reproduced BYTE-FOR-BYTE
    on every retry -- confirmed live against the real backend, two
    independent resume attempts both failed at exactly the same
    character count. The extended repetition-loop retry ceiling
    (MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP) was completely inert for
    any such caller: retrying a fully deterministic call with an
    unchanged payload can never produce a different output. This test
    proves the fix -- temperature actually increases across repetition-
    loop retries -- against a fake backend that always loops, so it
    needs no live GPU host and runs in well under a second.
    """
    client = ModelGatewayClient(base_url_override="http://fake-repeats-forever.invalid/v1")
    pool = client._pools[list(client._pools.keys())[0]]
    recorded_temperatures: list[float] = []
    pool.http.stream = _FakeRepeatingStreamResponse(recorded_temperatures)
    try:
        try:
            await client.generate(
                model="whatever",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.0,
                task_id="test-repetition-loop-escalation",
                timeout_sec=5.0,
            )
            raise AssertionError("expected GatewayUnavailableError, call succeeded instead")
        except GatewayUnavailableError:
            pass
        assert len(recorded_temperatures) >= 2, (
            f"expected multiple attempts, got {recorded_temperatures!r}"
        )
        assert recorded_temperatures[0] == 0.0, (
            f"first attempt should use the caller's own temperature -- got {recorded_temperatures!r}"
        )
        assert recorded_temperatures[1] > recorded_temperatures[0], (
            f"retry after a repetition loop must NOT reuse the identical temperature "
            f"(guarantees an identical loop again) -- got {recorded_temperatures!r}"
        )
        assert recorded_temperatures[-1] <= 0.6 + 1e-9, (
            f"temperature escalation must stay capped -- got {recorded_temperatures!r}"
        )
        assert len(recorded_temperatures) == MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP, (
            f"expected the full extended ceiling ({MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP}) "
            f"of attempts, not an early cutoff -- got {len(recorded_temperatures)}: "
            f"{recorded_temperatures!r}"
        )
        # Real bug found live (2026-07-24, same incident): a repetition
        # loop means the backend responded fast and is genuinely
        # healthy -- it must never trip the circuit breaker the way a
        # real timeout/HTTP/connection failure does. Confirmed live this
        # was actively harmful: MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP
        # (6) alone exceeds CIRCUIT_FAILURE_THRESHOLD (5), so a single
        # genuinely-looping call reliably tripped the breaker via its
        # OWN retries, which then immediately blocked
        # specialists/build/specialist.py's context-aware full-rewrite
        # fallback (the very next call to this same backend) with a
        # self-inflicted CircuitOpenError.
        assert not pool.breaker.is_open(), (
            "a pure repetition loop must never trip the circuit breaker -- the backend "
            "was never actually unhealthy"
        )
        print(
            f"PASS: repetition-loop retries escalated temperature across "
            f"{len(recorded_temperatures)} attempts: {recorded_temperatures}, "
            f"circuit breaker correctly stayed closed throughout"
        )
    finally:
        await client.aclose()


async def _run_repetition_loop_exhaustion_raises_the_specific_exception_type_test():
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    daily_escalation_cron node, resumes r205 and r206): a repetition loop that exhausts every
    one of its own temperature-escalated retries used to raise the SAME generic
    GatewayUnavailableError as a genuine transport/HTTP failure -- the manager's own downstream
    message then told the user "it's an infrastructure outage... I'll resume automatically once
    the gateway is back," which was factually wrong both times (confirmed live via an immediate
    real health check showing the backend was never down). Proves the fix: exhaustion on a
    repetition loop now raises the more specific `LLMRepetitionLoopExhaustedError` (still a
    `GatewayUnavailableError` subclass, so every existing `except GatewayUnavailableError` call
    site keeps working unchanged).
    """
    client = ModelGatewayClient(base_url_override="http://fake-repeats-forever.invalid/v1")
    pool = client._pools[list(client._pools.keys())[0]]
    recorded_temperatures: list[float] = []
    pool.http.stream = _FakeRepeatingStreamResponse(recorded_temperatures)
    try:
        try:
            await client.generate(
                model="whatever",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.0,
                task_id="test-repetition-loop-exhaustion-type",
                timeout_sec=5.0,
            )
            raise AssertionError("expected LLMRepetitionLoopExhaustedError, call succeeded instead")
        except LLMRepetitionLoopExhaustedError as exc:
            assert "not a genuine infrastructure outage" in str(exc).lower(), (
                f"expected the exception to explicitly deny an infrastructure outage -- got: {exc}"
            )
            assert "gateway is currently unreachable" not in str(exc).lower(), (
                f"must never claim the gateway itself is unreachable -- got: {exc}"
            )
            print(f"PASS: repetition-loop exhaustion raises the specific "
                  f"LLMRepetitionLoopExhaustedError, not a bare GatewayUnavailableError: {exc}")
        except GatewayUnavailableError:
            raise AssertionError(
                "expected the more specific LLMRepetitionLoopExhaustedError, got a bare "
                "GatewayUnavailableError instead -- the classification fix regressed"
            )
    finally:
        await client.aclose()


class _FakeDriftingRepeatingStreamResponse:
    """Fakes a genuine, non-convergent loop that the window-based check above CANNOT catch --
    each ~480-char cycle is functionally identical (same structure/content) but has ONE
    character that differs from cycle to cycle (a stand-in for the real, live-observed cause:
    a nonzero-temperature retry lets whitespace/minor phrasing drift just enough between
    repeats to defeat an exact-substring match on any given window), so `tail in prior_span`
    never matches and `repeat_run` never advances. Only the new, orthogonal absolute-length
    backstop (`_REPETITION_MAX_TOTAL_CHARS`) can catch this shape. Records every payload's own
    `temperature` (like `_FakeRepeatingStreamResponse` above) so a test can confirm the SAME
    automatic recovery chain the window-based check already gets -- escalating temperature and
    retrying, not just detecting and giving up -- also fires for this backstop.
    """
    status_code = 200

    def __init__(self, recorded_temperatures: list[float] | None = None):
        self._recorded_temperatures = recorded_temperatures

    def __call__(self, method, url, json=None, timeout=None):
        if self._recorded_temperatures is not None:
            self._recorded_temperatures.append(json.get("temperature"))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        cycle_template = (
            '  "edits": [{{"file": "models/models.py", "operation": "search_replace", '
            '"target": "x{:03d}", "content": "y"}}]\n'
        )
        # Comfortably past _REPETITION_MAX_TOTAL_CHARS, each cycle uniquely numbered so no
        # 120-char window ever repeats verbatim.
        cycles_needed = (gw._REPETITION_MAX_TOTAL_CHARS // len(cycle_template.format(0))) + 5
        for i in range(cycles_needed):
            cycle = cycle_template.format(i % 1000)
            for j in range(0, len(cycle), 20):
                piece = cycle[j:j + 20]
                data = json.dumps({"choices": [{"delta": {"content": piece}}]})
                yield f"data: {data}"
        yield "data: [DONE]"


async def _run_repetition_max_total_chars_backstop_test():
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): the window-based check (_REPETITION_WINDOW_CHARS/
    _REPETITION_MIN_REPEATS) missed a genuine, non-convergent loop that ran 594+ seconds
    (4000+ streamed deltas) in the real system, confirmed live via oma:trace_history --
    each repeated cycle apparently drifted just enough (temperature was nonzero for this
    candidate) to defeat an exact-substring match on any 120-char window. This test proves
    the new, orthogonal backstop (`_REPETITION_MAX_TOTAL_CHARS`) catches exactly this shape,
    aborting well before any external timeout would, using a fake backend that never
    literally repeats any 120-char span but never stops generating either.

    Critically (the project owner's own explicit follow-up ask, 2026-08-09): detecting the loop is not
    enough on its own -- the system must actually DO something automatically, not just flag
    it. Because this backstop raises the exact same `LLMRepetitionLoopError` type the
    window-based check already raises, it is caught by the SAME existing, already-proven
    automatic-recovery machinery in `generate()`'s own retry loop (see
    `_run_repetition_loop_temperature_escalation_test` above): every attempt after a detected
    loop escalates temperature (so a retry is never a guaranteed repeat of the identical
    doomed request), extends the retry ceiling to
    `MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP`, and never trips the circuit breaker (a
    repetition loop means the backend is healthy, just stuck on this one prompt). This test
    asserts that full chain actually ran -- multiple real retry attempts with escalating
    temperature -- not merely that one exception got raised and swallowed.
    """
    client = ModelGatewayClient(base_url_override="http://fake-drifts-forever.invalid/v1")
    pool = client._pools[list(client._pools.keys())[0]]
    recorded_temperatures: list[float] = []
    pool.http.stream = _FakeDriftingRepeatingStreamResponse(recorded_temperatures)
    try:
        try:
            await client.generate(
                model="whatever",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.4,
                task_id="test-repetition-max-total-chars-backstop",
                timeout_sec=5.0,
            )
            raise AssertionError("expected GatewayUnavailableError, call succeeded instead")
        except GatewayUnavailableError as exc:
            assert isinstance(exc.__cause__, LLMRepetitionLoopError), (
                f"expected the absolute-length backstop to raise LLMRepetitionLoopError, "
                f"got {exc.__cause__!r}"
            )
            assert str(gw._REPETITION_MAX_TOTAL_CHARS) in str(exc.__cause__), (
                f"error message should name the ceiling that fired -- got {exc.__cause__}"
            )
        # The automatic-response assertions: this is the part that proves the system DID
        # something about the loop, not just detected and gave up after one attempt.
        assert len(recorded_temperatures) == MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP, (
            f"the backstop must trigger the SAME automatic retry-with-escalation chain as the "
            f"window-based check -- expected {MAX_RETRY_ATTEMPTS_AFTER_REPETITION_LOOP} real "
            f"attempts, got {len(recorded_temperatures)}: {recorded_temperatures!r}"
        )
        assert recorded_temperatures[0] == 0.4, (
            f"first attempt should use the caller's own temperature -- got {recorded_temperatures!r}"
        )
        assert recorded_temperatures[-1] > recorded_temperatures[0], (
            f"the retry chain must actually escalate temperature (never resend the identical "
            f"doomed request) -- got {recorded_temperatures!r}"
        )
        assert not pool.breaker.is_open(), (
            "a pure repetition loop must never trip the circuit breaker -- the fake backend "
            "was never actually unhealthy, just stuck on one prompt"
        )
        print(
            "PASS: a genuinely non-convergent loop that drifts just enough to defeat the "
            f"window-based check is still caught by the absolute-length backstop, AND "
            f"automatically triggers the same retry-with-temperature-escalation recovery "
            f"chain across {len(recorded_temperatures)} real attempts "
            f"({recorded_temperatures}), circuit breaker correctly stayed closed"
        )
    finally:
        await client.aclose()


class _FakeBadRequestResponse:
    """Fakes a streaming response that returns a real HTTP 400 with a
    real, specific JSON error body -- the shape vLLM/llama-swap
    actually return (e.g. a malformed request or a context-length
    error), never just a bare status line.
    """
    status_code = 400
    reason_phrase = "Bad Request"
    url = "http://fake/v1/chat/completions"
    request = None

    def __call__(self, method, url, json=None, timeout=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aread(self):
        return b'{"error": {"message": "This is the REAL reason the request was rejected"}}'

    async def aiter_lines(self):
        return
        yield  # pragma: no cover -- makes this a generator function; never reached


async def _run_streaming_400_error_includes_real_body_test():
    """Real bug found live (2026-07-24, 50-task sequential re-run, task
    001): a genuine backend 400 surfaced everywhere in this codebase as
    only "Client error '400 Bad Request' for url '...'" -- plain
    `resp.raise_for_status()` on a streaming response discards the
    actual response BODY, which vLLM/llama-swap fill with a real,
    specific reason (bad request shape, context-length exceeded, an
    invalid sampling parameter). Same class of gap already fixed twice
    this session at higher layers (manager/compensations.py, manager/
    gateway_orchestration.py) -- this is the actual root call site
    underneath both. This test proves the real body now reaches the
    raised exception's own message.
    """
    client = ModelGatewayClient(base_url_override="http://fake-400.invalid/v1")
    pool = client._pools[list(client._pools.keys())[0]]
    pool.http.stream = _FakeBadRequestResponse()
    try:
        try:
            await client.generate(
                model="whatever",
                messages=[{"role": "user", "content": "hi"}],
                task_id="test-400-body",
                timeout_sec=5.0,
            )
            raise AssertionError("expected GatewayUnavailableError, call succeeded instead")
        except GatewayUnavailableError as exc:
            assert "This is the REAL reason the request was rejected" in str(exc), (
                f"expected the real response body in the raised error -- got: {exc}"
            )
            print("PASS: a streaming 400 error now includes the real response body, "
                  "not just a bare status line")
    finally:
        await client.aclose()


class _FakeReadErrorThenSuccessResponse:
    """Fakes a real SSE stream connection dropping mid-read (httpx.ReadError -- confirmed live,
    2026-08-07, this is httpx's own documented failure mode for a broken streaming connection,
    NOT a subclass of ConnectError or TimeoutException) on the first N calls, then succeeding --
    proves the retry loop now actually retries this failure mode instead of letting it escape
    uncaught.
    """

    def __init__(self, fail_times: int):
        self._fail_times = fail_times
        self.call_count = 0

    def __call__(self, method, url, json=None, timeout=None):
        self.call_count += 1
        return self

    async def __aenter__(self):
        if self.call_count <= self._fail_times:
            raise httpx.ReadError("simulated dropped SSE connection mid-read")
        return self

    async def __aexit__(self, *exc_info):
        return False

    @property
    def status_code(self):
        return 200

    async def aiter_lines(self):
        data = json.dumps({"choices": [{"delta": {"content": "pong"}}]})
        yield f"data: {data}"
        yield "data: [DONE]"


async def _run_read_error_is_retried_test():
    """Real, confirmed live bug (2026-08-07, HUMAN_DECISION deep-push): a real task relaunch
    failed with the bare message "generate() (streaming) failed after retries against backend
    'gpu_worker_01_coder': ReadError:" -- an empty message because httpx.ReadError was never
    caught by name anywhere in the retry loop's own except clause (only TimeoutException/
    HTTPStatusError/ConnectError), so it propagated straight out UNRETRIED on the very first
    occurrence. Confirmed via direct interpreter check: httpx.ReadError is NOT a subclass of
    httpx.ConnectError or httpx.TimeoutException -- it's a sibling under httpx.NetworkError.
    Fixed by catching httpx.TransportError (the shared parent of all of these), matching httpx's
    own documented broad-retry-coverage pattern.
    """
    client = ModelGatewayClient(base_url_override="http://fake-readerror.invalid/v1")
    pool = client._pools[list(client._pools.keys())[0]]
    fake = _FakeReadErrorThenSuccessResponse(fail_times=2)
    pool.http.stream = fake
    try:
        reply = await client.generate(
            model="whatever",
            messages=[{"role": "user", "content": "hi"}],
            task_id="test-read-error-retry",
            timeout_sec=5.0,
        )
        assert reply == "pong", f"expected the eventual successful reply -- got {reply!r}"
        assert fake.call_count == 3, (
            f"expected exactly 2 failed attempts (ReadError) then 1 successful retry -- "
            f"got {fake.call_count} total calls"
        )
        print(f"PASS: httpx.ReadError is now correctly retried (not silently uncaught) -- "
              f"succeeded on attempt {fake.call_count} after 2 simulated dropped connections")
    finally:
        await client.aclose()


class _FakeConcurrencyLimitResponse:
    """Fakes the real, live-confirmed 429 body from the coder backend's own operator-configured
    gate: {"error": {"message": "Coder gate: max concurrent inference is 1 (currently 1). Retry
    shortly; do not fan out parallel streams.", "type": "concurrency_limit", "code":
    "too_many_requests", "max_inflight": 1, "inflight": 1}}. Always fails, on every call --
    proves the retry loop gives this shape its own extended ceiling and never trips the breaker.
    """
    status_code = 429
    reason_phrase = "Too Many Requests"
    url = "http://fake/v1/chat/completions"
    request = None

    def __init__(self):
        self.call_count = 0

    def __call__(self, method, url, json=None, timeout=None):
        self.call_count += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aread(self):
        return (
            b'{"error": {"message": "Coder gate: max concurrent inference is 1 (currently 1). '
            b'Retry shortly; do not fan out parallel streams.", "type": "concurrency_limit", '
            b'"code": "too_many_requests", "max_inflight": 1, "inflight": 1}}'
        )

    async def aiter_lines(self):
        return
        yield  # pragma: no cover -- makes this a generator function; never reached


async def _run_concurrency_limit_does_not_trip_breaker_test():
    """Real, confirmed live bug (2026-08-07, HUMAN_DECISION deep-push, real task_ids across
    task019/026/039): the coder backend's own real 429 body explicitly says "Retry shortly; do
    not fan out parallel streams" -- a concurrency-limit rejection means the backend responded
    FAST and is genuinely healthy, just busy, the same reasoning already established for
    LLMRepetitionLoopError. Confirmed live this was NOT already handled: an ordinary
    HTTPStatusError (429 included) called pool.breaker.record_failure() every time, so repeated
    concurrency contention could trip the circuit breaker over a backend that was never actually
    unhealthy.
    """
    from infra.gateway_client import MAX_RETRY_ATTEMPTS_AFTER_CONCURRENCY_LIMIT

    client = ModelGatewayClient(base_url_override="http://fake-429.invalid/v1")
    pool = client._pools[list(client._pools.keys())[0]]
    fake = _FakeConcurrencyLimitResponse()
    pool.http.stream = fake
    try:
        try:
            await client.generate(
                model="whatever",
                messages=[{"role": "user", "content": "hi"}],
                task_id="test-concurrency-limit",
                timeout_sec=5.0,
            )
            raise AssertionError("expected GatewayUnavailableError, call succeeded instead")
        except GatewayUnavailableError:
            pass
        assert fake.call_count == MAX_RETRY_ATTEMPTS_AFTER_CONCURRENCY_LIMIT, (
            f"expected the full extended ceiling ({MAX_RETRY_ATTEMPTS_AFTER_CONCURRENCY_LIMIT}) "
            f"of attempts for a concurrency-limit rejection -- got {fake.call_count}"
        )
        assert not pool.breaker.is_open(), (
            "a pure concurrency-limit rejection must never trip the circuit breaker -- the "
            "backend was never actually unhealthy, just busy"
        )
        print(
            f"PASS: a concurrency-limit 429 gets the extended retry ceiling "
            f"({fake.call_count} attempts) and never trips the circuit breaker"
        )
    finally:
        await client.aclose()


def test_read_error_is_retried_not_silently_uncaught():
    asyncio.run(_run_read_error_is_retried_test())


def test_concurrency_limit_429_does_not_trip_breaker():
    asyncio.run(_run_concurrency_limit_does_not_trip_breaker_test())


def test_coder_backend_pool_defaults_to_single_concurrency():
    """Real, confirmed live bug (2026-08-07): the coder backend's own operator-configured gate
    genuinely enforces max_inflight=1 server-side, but this client's bulkhead defaulted to
    max_concurrent=8 uniformly across every backend -- confirmed via direct research this is
    llama-swap's own documented per-model concurrency semaphore (a deliberate resource-management
    feature the client is expected to respect, not a transient overload). With max_concurrent=8
    against a real max_inflight=1 gate, this client's OWN concurrent calls were guaranteed to
    race each other for the server's single slot -- confirmed live: a real pytest run making a
    genuine GPU call hit the exact "Coder gate" 429 while a separate task relaunch was already
    mid-flight against the same backend.
    """
    from infra.gateway_client import BACKEND_CODER, BACKEND_REASONING, _CODER_BACKEND_MAX_CONCURRENT

    client = ModelGatewayClient()
    try:
        coder_pool = client._pools[BACKEND_CODER]
        assert coder_pool.bulkhead.max_concurrent == _CODER_BACKEND_MAX_CONCURRENT == 1, (
            f"the coder backend's own bulkhead must match its real, confirmed server-side "
            f"max_inflight=1 gate -- got {coder_pool.bulkhead.max_concurrent}"
        )
        reasoning_pool = client._pools[BACKEND_REASONING]
        assert reasoning_pool.bulkhead.max_concurrent == 8, (
            "the reasoning backend's own default concurrency must be untouched by the "
            "coder-specific override"
        )
        print("PASS: the coder backend pool defaults to max_concurrent=1 (matching its real "
              "server-side gate); the reasoning backend pool is unaffected")
    finally:
        asyncio.run(client.aclose())


def test_local_inference_semaphore_genuinely_bounds_concurrency():
    """Phase 31 §8: OMA_LOCAL_INFERENCE_MAX_CONCURRENT is a single, cross-cutting cap on ALL
    local-model calls through generate() -- OUTER to (never a replacement for) each backend's
    own per-model bulkhead. Proven here with a lowered, test-local semaphore (never the real
    16-default, which would make a fast unit test either trivial or slow) and a mocked, delayed
    _generate_inner -- zero real network/GPU calls.
    """
    import infra.gateway_client as gateway_client_module

    client = ModelGatewayClient()
    original_semaphore = gateway_client_module._local_inference_semaphore
    gateway_client_module._local_inference_semaphore = asyncio.Semaphore(2)
    peak = {"n": 0, "max": 0}

    async def fake_generate_inner(pool, model, messages, temperature, max_tokens, timeout_sec, response_format):
        peak["n"] += 1
        peak["max"] = max(peak["max"], peak["n"])
        await asyncio.sleep(0.05)
        peak["n"] -= 1
        return "ok"

    async def run():
        with patch.object(client, "_generate_inner", side_effect=fake_generate_inner):
            await asyncio.gather(*(
                client.generate(model=BUILD_MODEL, messages=[{"role": "user", "content": "hi"}])
                for _ in range(6)
            ))

    try:
        asyncio.run(run())
    finally:
        gateway_client_module._local_inference_semaphore = original_semaphore
        asyncio.run(client.aclose())

    assert peak["max"] <= 2, (
        f"6 concurrent generate() calls against a semaphore lowered to 2 must never let more than "
        f"2 real calls run at once -- observed peak concurrency {peak['max']}"
    )
    print(f"PASS: 6 concurrent generate() calls, real observed peak concurrency {peak['max']} <= the lowered cap of 2")


def test_local_inference_semaphore_default_is_a_positive_integer_env_configurable():
    from infra.gateway_client import OMA_LOCAL_INFERENCE_MAX_CONCURRENT
    assert isinstance(OMA_LOCAL_INFERENCE_MAX_CONCURRENT, int)
    assert OMA_LOCAL_INFERENCE_MAX_CONCURRENT > 0
    print(f"PASS: OMA_LOCAL_INFERENCE_MAX_CONCURRENT is a positive integer ({OMA_LOCAL_INFERENCE_MAX_CONCURRENT})")


class _FakeRecordingStreamResponse:
    """Fakes a normal, single-shot streaming reply while recording the
    full outgoing JSON payload -- used to prove which fields actually
    reach the wire, without needing a real backend.
    """
    status_code = 200

    def __init__(self, recorded_payloads: list[dict]):
        self._recorded_payloads = recorded_payloads

    def __call__(self, method, url, json=None, timeout=None):
        self._recorded_payloads.append(json)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        data = json.dumps({"choices": [{"delta": {"content": "pong"}}]})
        yield f"data: {data}"
        yield "data: [DONE]"


async def _run_repetition_detection_payload_test():
    """Real, live-confirmed finding (2026-08-05): this backend is
    genuinely vLLM (system_fingerprint literally reads
    "vllm-0.24.0-...", and vLLM's own real 400 validation errors fire
    for both `repetition_penalty` and `repetition_detection` -- proving
    the params are schema-recognized, not silently ignored the way a
    garbage `dry_multiplier` was). This is a pivot away from the
    llama.cpp-only DRY sampler the initial research report suggested
    (that report's premise -- llama.cpp backend, confirmed via /props --
    did not hold up under direct verification) to vLLM's own native
    `repetition_detection` SamplingParam instead. Gated behind
    OMA_GATEWAY_REPETITION_DETECTION_ENABLED (off by default, matching
    every other experimental flag in this codebase) and scoped to the
    coder model only, since that's the only model this failure mode has
    ever actually been observed on. This test proves both halves: off
    by default, and scoped to the coder model even when on.
    """
    from infra import gateway_client as gw

    async def _one_call(model: str, env_value: str | None) -> dict | None:
        old = os.environ.get(gw._REPETITION_DETECTION_ENV)
        if env_value is None:
            os.environ.pop(gw._REPETITION_DETECTION_ENV, None)
        else:
            os.environ[gw._REPETITION_DETECTION_ENV] = env_value
        client = ModelGatewayClient(base_url_override="http://fake-recording.invalid/v1")
        pool = client._pools[list(client._pools.keys())[0]]
        recorded: list[dict] = []
        pool.http.stream = _FakeRecordingStreamResponse(recorded)
        try:
            await client.generate(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                task_id="test-repetition-detection-payload",
                timeout_sec=5.0,
            )
        finally:
            await client.aclose()
            if old is None:
                os.environ.pop(gw._REPETITION_DETECTION_ENV, None)
            else:
                os.environ[gw._REPETITION_DETECTION_ENV] = old
        assert len(recorded) == 1
        return recorded[0].get("repetition_detection")

    default_off = await _one_call("qwen3-coder-30b-a3b", None)
    assert default_off is None, (
        f"repetition_detection must be OFF by default (unset env) -- got {default_off!r}"
    )

    off_explicit = await _one_call("qwen3-coder-30b-a3b", "0")
    assert off_explicit is None, (
        f"repetition_detection must stay off when the flag is explicitly '0' -- got {off_explicit!r}"
    )

    on_coder = await _one_call("qwen3-coder-30b-a3b", "1")
    assert on_coder == {
        "min_pattern_size": gw._REPETITION_DETECTION_MIN_PATTERN_SIZE,
        "max_pattern_size": gw._REPETITION_DETECTION_MAX_PATTERN_SIZE,
        "min_count": gw._REPETITION_DETECTION_MIN_COUNT,
    }, f"expected the real repetition_detection dict on the coder model when flagged on -- got {on_coder!r}"

    on_but_wrong_model = await _one_call("qwen3.6-27b", "1")
    assert on_but_wrong_model is None, (
        f"repetition_detection must stay scoped to the coder model even when the flag is on, "
        f"since the reasoning tier has no reported occurrence of this failure mode -- "
        f"got {on_but_wrong_model!r}"
    )

    print("PASS: repetition_detection is off by default, off when explicitly '0', present with the "
          "correct real values on the coder model when flagged on, and still absent for a non-coder "
          "model even when the flag is on")


async def _run_repetition_prevention_payload_test():
    """Step 3, 2026-08-05: repetition_detection alone did not prevent a single real incident
    tonight (0/5), even after correcting it to vLLM's own maintainer-validated hardened profile --
    it stops a loop once it's forming, it never biases generation away from forming one. This adds
    Qwen's own official sampling recommendation for qwen3-coder-30b-a3b specifically
    (top_p=0.8, top_k=20, repetition_penalty=1.05) as a genuinely additive, prevention-oriented
    lever -- proves it's off by default, present with the right vendor-sourced values on the coder
    model when flagged on, absent for a non-coder model, and deliberately does NOT include
    `temperature` (this codebase's callers choose that deliberately per call, e.g. 0.0 for
    deterministic internal-loop patches -- see _REPETITION_PREVENTION_ENV's own comment for why
    forcing it globally would be a broader change than this fix calls for).
    """
    from infra import gateway_client as gw

    async def _one_call(model: str, env_value: str | None) -> dict:
        old = os.environ.get(gw._REPETITION_PREVENTION_ENV)
        if env_value is None:
            os.environ.pop(gw._REPETITION_PREVENTION_ENV, None)
        else:
            os.environ[gw._REPETITION_PREVENTION_ENV] = env_value
        client = ModelGatewayClient(base_url_override="http://fake-recording.invalid/v1")
        pool = client._pools[list(client._pools.keys())[0]]
        recorded: list[dict] = []
        pool.http.stream = _FakeRecordingStreamResponse(recorded)
        try:
            await client.generate(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.0,
                task_id="test-repetition-prevention-payload",
                timeout_sec=5.0,
            )
        finally:
            await client.aclose()
            if old is None:
                os.environ.pop(gw._REPETITION_PREVENTION_ENV, None)
            else:
                os.environ[gw._REPETITION_PREVENTION_ENV] = old
        assert len(recorded) == 1
        payload = recorded[0]
        return {k: payload.get(k) for k in ("temperature", "top_p", "top_k", "repetition_penalty")}

    default_off = await _one_call("qwen3-coder-30b-a3b", None)
    assert default_off == {"temperature": 0.0, "top_p": None, "top_k": None, "repetition_penalty": None}, (
        f"prevention params must be OFF by default -- got {default_off!r}"
    )

    on_coder = await _one_call("qwen3-coder-30b-a3b", "1")
    assert on_coder == {
        "temperature": 0.0,  # caller's own temperature preserved, NOT forced to Qwen's 0.7
        "top_p": gw._REPETITION_PREVENTION_TOP_P,
        "top_k": gw._REPETITION_PREVENTION_TOP_K,
        "repetition_penalty": gw._REPETITION_PREVENTION_REPETITION_PENALTY,
    }, f"expected Qwen's real vendor values, caller's own temperature preserved -- got {on_coder!r}"

    on_but_wrong_model = await _one_call("qwen3.6-27b", "1")
    assert on_but_wrong_model == {"temperature": 0.0, "top_p": None, "top_k": None, "repetition_penalty": None}, (
        f"prevention params must stay scoped to the coder model even when flagged on -- "
        f"got {on_but_wrong_model!r}"
    )

    print("PASS: repetition-prevention params (top_p/top_k/repetition_penalty) are off by default, "
          "present with Qwen's real vendor values on the coder model when flagged on, absent for a "
          "non-coder model, and never override the caller's own temperature")


def test_strip_think_block():
    raw = "<think>let me consider this carefully...</think>The answer is 42."
    assert strip_think_block(raw) == "The answer is 42."
    raw_no_think = "Just a plain answer."
    assert strip_think_block(raw_no_think) == "Just a plain answer."
    # The real artifact observed from qwen3-14b with /no_think on this
    # dev host: an unmatched closing </think> tag with no visible
    # opening one (the template opens it as part of the invisible
    # prompt prefix).
    real_artifact = ": pong\n\n</think>\n\npong"
    assert strip_think_block(real_artifact) == "pong"
    print("PASS: strip_think_block correctly removes both full <think> pairs "
          "and the unmatched-closing-tag artifact")


if __name__ == "__main__":
    test_strip_think_block()
    asyncio.run(_run_smoke_test())
    asyncio.run(_run_structured_output_test())
    asyncio.run(_run_broken_url_test())
    asyncio.run(_run_circuit_breaker_test())
    asyncio.run(_run_repetition_loop_temperature_escalation_test())
    asyncio.run(_run_repetition_loop_exhaustion_raises_the_specific_exception_type_test())
    asyncio.run(_run_repetition_max_total_chars_backstop_test())
    asyncio.run(_run_streaming_400_error_includes_real_body_test())
    asyncio.run(_run_repetition_detection_payload_test())
    asyncio.run(_run_repetition_prevention_payload_test())
    asyncio.run(_run_read_error_is_retried_test())
    asyncio.run(_run_concurrency_limit_does_not_trip_breaker_test())
    test_coder_backend_pool_defaults_to_single_concurrency()
    print("\nALL GATEWAY CLIENT TESTS PASSED")
