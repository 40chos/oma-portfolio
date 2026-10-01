"""Gateway-unavailability orchestration as real MANAGER-LOOP behavior --
Phase 6 step 3. The ModelGatewayClient's own retry/circuit-breaker
(Phase 2) is a different layer: it decides when to give up on a single
call. This module decides what the LOOP does once the client has
already given up -- flip the task's Redis state, release its module
lock so unrelated work isn't blocked by an outage, and produce a
message to Operator worded distinctly from an ambiguity-pause or a
tier-3/4 sign-off pause.
"""

from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

from infra.fencing import release_module_lock
from infra.gateway_client import GatewayUnavailableError, LLMRepetitionLoopExhaustedError
from manager.task_state import PAUSE_GATEWAY_UNAVAILABLE, STATE_RUNNING, set_task_state

T = TypeVar("T")


class GatewayOutagePause(Exception):
    """Raised (not swallowed) so the loop's own phase-five step knows to
    stop and report -- distinct from a normal exception, this carries
    the exact message to show Operator.
    """
    def __init__(self, task_id: str, module_name: str, message: str, original: Exception | None = None):
        super().__init__(message)
        self.task_id = task_id
        self.module_name = module_name
        self.message = message
        self.original = original


async def run_with_gateway_outage_handling(
    task_id: str,
    module_name: str,
    call: Callable[[], Awaitable[T]],
) -> T:
    """Wraps a single gateway-dependent call (e.g. delegate_to_specialist,
    await_verification). On GatewayUnavailableError: flips Redis task
    state to paused:gateway_unavailable, releases module_name's fencing
    lock (so unrelated work isn't blocked by this outage), and raises
    GatewayOutagePause with language distinct from an ambiguity-pause or
    a tier-3/4 sign-off pause.
    """
    try:
        return await call()
    except GatewayUnavailableError as exc:
        set_task_state(task_id, PAUSE_GATEWAY_UNAVAILABLE)
        release_module_lock(module_name, task_id)
        # Real, confirmed gap found live (2026-08-17): every message below promises Operator this
        # will "resume automatically once the gateway is back," but nothing actually did that
        # until now -- see manager/gateway_auto_resume.py's own module docstring for the full
        # incident. Schedules this task for the real background poller; never affects this
        # pause's own real behavior if scheduling itself somehow fails (that function's own
        # contract).
        from manager.gateway_auto_resume import record_gateway_pause

        record_gateway_pause(task_id, module_name)
        # Real bug found live (2026-07-24, task 020 resume): this used to
        # discard `exc` entirely (`from None`, no reference kept anywhere),
        # so every gateway-outage pause showed Operator the exact same generic
        # "unreachable" text regardless of the REAL cause underneath --
        # RAM-ceiling wait timeout, a genuine repetition loop, an HTTP 4xx,
        # a real connection refusal, etc. all looked identical from the UI,
        # making it impossible to tell a real infra outage (nothing to do
        # but wait) apart from a live, diagnosable bug (same class of gap
        # already fixed once this session in compensations.handle_partial_task_failure()).
        # `original` below preserves it for callers/logs without changing
        # the human-facing message's tone.
        #
        # Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
        # daily_escalation_cron node, resumes r205 and r206): this message unconditionally said
        # "it's an infrastructure outage... I'll resume automatically once the gateway is back"
        # for EVERY GatewayUnavailableError, including a repetition loop that had exhausted every
        # one of its own temperature-escalated retries -- confirmed live, TWICE, via an immediate
        # direct health check against both real backends (200 OK within seconds) that the gateway
        # was never actually down. Retrying identically (as this same "wait for the outage to
        # clear" framing invites) just reproduced the identical repetition failure. Branching on
        # the new, more specific `LLMRepetitionLoopExhaustedError` (see infra/gateway_client.py's
        # own docstring) gives Operator the ACCURATE picture -- the model itself is struggling with
        # this round's own specific content, not the infrastructure -- without touching the
        # unchanged, still-correct message for a genuine transport/HTTP outage.
        if isinstance(exc, LLMRepetitionLoopExhaustedError):
            message = (
                "This round's own content generation kept hitting a repetition loop even after "
                "several attempts with escalating temperature -- this is NOT an infrastructure "
                "outage (the model gateway itself is healthy and reachable); the model is "
                "genuinely struggling to generate this specific round's content without "
                "repeating itself. I've released the lock on this module so it doesn't block "
                "other work. A plain retry may still succeed (sampling is stochastic), but if "
                "this keeps recurring, the round's own ask may need to be simplified or split."
                f" Real error: {type(exc).__name__}: {exc}"
            )
        else:
            message = (
                "The model gateway is currently unreachable, so I've paused this task "
                "rather than keep retrying blindly. This isn't a question for you and it "
                "isn't waiting on your sign-off -- it's an infrastructure outage. I've "
                "released the lock on this module so it doesn't block other work. I'll "
                "resume automatically once the gateway is back; let me know if you'd "
                "like me to try something else in the meantime."
                f" Real error: {type(exc).__name__}: {exc}"
            )
        raise GatewayOutagePause(task_id, module_name, message, original=exc) from exc


def mark_task_running(task_id: str) -> None:
    set_task_state(task_id, STATE_RUNNING)
