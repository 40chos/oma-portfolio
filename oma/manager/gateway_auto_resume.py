"""Real, confirmed gap found live (2026-08-17, overnight `record_rule_row_level_security`
certification run): every `GatewayOutagePause` message (`manager/gateway_orchestration.py`)
tells Operator/the human "I'll resume automatically once the gateway is back" -- but nothing in this
codebase actually did that. Confirmed via a direct grep sweep: no background watcher, cron, or
poller anywhere queries `PAUSE_GATEWAY_UNAVAILABLE` tasks and calls `resume_task_after_
checkpoint()` on their behalf. The ONLY way a paused-for-gateway task ever resumed, all night,
was a human (or, tonight, this agent) manually noticing the pause and calling `/continue`.

This is especially costly for `LLMRepetitionLoopExhaustedError` (a `GatewayUnavailableError`
subclass, so it shares this same pause path) -- its own message explicitly says "a plain retry
may still succeed (sampling is stochastic)," which is only true if something ACTUALLY retries
it; genuine, real evidence tonight (`record_rule_row_level_security` certification push, 6 of 9
consecutive real attempts hit this exact error) showed no retry ever happened on its own.

Also covers `PAUSE_TASK_CUT_OFF` (`manager/compensations.py`'s `TaskCutOffPause`) -- found live
the same night, immediately after deploying the first version of this fix: the SAME
`LLMRepetitionLoopExhaustedError` can ALSO surface after at least one real step already
executed (a `PartialTaskFailure`, compensations already run to undo the partial work), a
structurally different pause reason from a clean gateway-outage pause, but equally real and
equally never auto-resumed before tonight. `manager/loop.py`'s `_write_task_cut_off_checkpoint()`
(the sibling of `_write_gateway_unavailable_checkpoint()`) now writes the `round_checkpoint`
this path was ALSO missing, so the same `resume_task_after_checkpoint()` mechanism works for
both pause reasons identically.

Closes the gap for real: a lightweight, periodic background poller (started once in `ui/chat/
server.py`'s `lifespan()`, same convention as the existing orphan-resume sweep) that finds every
task still genuinely paused for a gateway/repetition-loop reason, waits out an exponential
backoff per task (so a real, sustained outage isn't hammered), and calls the exact same
`resume_task_after_checkpoint()` a human's `/continue` click already uses -- reusing that
function's own existing idempotency lock (`oma:resume_lock:{task_id}`, SET NX) means this
poller can NEVER race a concurrent manual `/continue` call; whichever caller gets there first
wins, the other cleanly no-ops. Bounded: gives up automatic resume after
`_MAX_AUTO_RESUME_ATTEMPTS`, leaving the task genuinely paused for a human from then on --
matches every other escalation-cap convention already in this codebase (never retries forever).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from infra.redis_client import get_redis_client
from manager.task_state import (
    PAUSE_GATEWAY_UNAVAILABLE,
    PAUSE_TASK_CUT_OFF,
    get_task_state,
    is_cancel_requested,
)

logger = logging.getLogger(__name__)

# Both real pause reasons record_gateway_pause() gets called for -- see this module's own
# docstring for why PAUSE_TASK_CUT_OFF joined PAUSE_GATEWAY_UNAVAILABLE here.
_AUTO_RESUMABLE_PAUSE_STATES = frozenset({PAUSE_GATEWAY_UNAVAILABLE, PAUSE_TASK_CUT_OFF})

_TRACKING_KEY_TEMPLATE = "oma:gateway_auto_resume:{task_id}"
_TRACKING_TTL_SEC = 86_400  # matches task_state.py's own pause-state TTL

# Deliberately much coarser than infra/gateway_client.py's own RETRY_BASE_DELAY_SEC/
# RETRY_MAX_DELAY_SEC (1-8s) -- those retry a single streaming call; this retries a whole
# round-level resume, which is expensive and, for a genuine sustained outage, pointless to
# hammer. First retry at 1 minute, doubling, capped at 15 minutes -- roughly an hour of total
# coverage across _MAX_AUTO_RESUME_ATTEMPTS before giving up automatically.
_BASE_BACKOFF_SEC = 60.0
_MAX_BACKOFF_SEC = 900.0
_MAX_AUTO_RESUME_ATTEMPTS = 6
_DEFAULT_POLL_INTERVAL_SEC = 30.0


def _backoff_seconds(attempts: int) -> float:
    return min(_MAX_BACKOFF_SEC, _BASE_BACKOFF_SEC * (2 ** attempts))


def record_gateway_pause(task_id: str, module_name: str | None = None, client=None) -> None:
    """Called at the exact point a task is paused for a gateway/repetition-loop reason
    (`manager/gateway_orchestration.py`'s own `run_with_gateway_outage_handling`) -- schedules
    this task for the background auto-resume poller below. Never raises: recording the
    schedule is a real speed/reliability optimization, never a correctness requirement -- a
    task that fails to get recorded here just falls back to needing a manual `/continue`,
    exactly today's existing behavior, not a regression.

    Real, confirmed correctness requirement (found during this same fix's own review, before
    ever shipping): `resume_task_after_checkpoint()` does NOT raise on a re-pause -- a re-pause
    for the SAME reason calls back into this exact function as a side effect of the SAME resume
    attempt (see `_attempt_one_resume()`'s own docstring). If this function always started a
    fresh entry at `attempts=0`, the poller's own bounded-retry cap
    (`_MAX_AUTO_RESUME_ATTEMPTS`) could NEVER actually trigger -- every re-pause would silently
    reset the counter, turning a deliberately BOUNDED retry policy into an unbounded one.
    Preserves and increments the existing entry's own attempt count across repeated pauses of
    the same task instead of overwriting it, so the cap means what it says.
    """
    try:
        r = client or get_redis_client()
        key = _TRACKING_KEY_TEMPLATE.format(task_id=task_id)
        attempts = 0
        existing = r.get(key)
        if existing:
            try:
                attempts = json.loads(existing).get("attempts", 0)
            except (json.JSONDecodeError, AttributeError):
                attempts = 0
        payload = json.dumps({
            "attempts": attempts, "next_retry_at": time.time() + _backoff_seconds(attempts),
            "module_name": module_name,
        })
        r.set(key, payload, ex=_TRACKING_TTL_SEC)
    except Exception:  # noqa: BLE001 -- scheduling must never break the real pause path
        logger.warning("Task %s: failed to schedule for gateway auto-resume", task_id, exc_info=True)


def clear_gateway_auto_resume_tracking(task_id: str, client=None) -> None:
    """Called once a task is genuinely running again (`manager/loop.py`'s own
    `mark_task_running()`, fired on every fresh AND resumed task) -- the tracking entry (if any)
    is now stale; if this exact task pauses again, `record_gateway_pause()` will create a fresh
    one with its own fresh backoff. Never raises, same reasoning as the sibling function above.
    """
    try:
        r = client or get_redis_client()
        r.delete(_TRACKING_KEY_TEMPLATE.format(task_id=task_id))
    except Exception:  # noqa: BLE001
        logger.warning("Task %s: failed to clear gateway auto-resume tracking", task_id, exc_info=True)


async def _attempt_one_resume(task_id: str, client, classifier_model: str, manager_model: str | None) -> None:
    """`resume_task_after_checkpoint()` does NOT raise on a re-pause -- `run_turn()`'s own
    `except GatewayOutagePause` handlers catch it internally, write a fresh `round_checkpoint`,
    and return a normal result dict (see `_write_gateway_unavailable_checkpoint()`'s own
    docstring for the real incident that shaped this contract). That means a re-pause for the
    SAME reason already re-invoked `record_gateway_pause()` as a side effect, inside this same
    call, with its own fresh backoff -- this function must never touch the tracking key itself
    on that path, only observe whether one is still needed afterward (the caller's job).
    """
    from manager.loop import resume_task_after_checkpoint  # local import: avoids a module-level
    # cycle (manager.loop already imports manager.gateway_orchestration, which imports this
    # module for record_gateway_pause()).

    await resume_task_after_checkpoint(task_id, client, classifier_model, manager_model=manager_model)


async def run_auto_resume_loop(
    client, classifier_model: str, manager_model: str | None = None,
    poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
) -> None:
    """The real background poller -- started once, for the process's whole lifetime, in
    `ui/chat/server.py`'s `lifespan()`. Runs until cancelled (server shutdown). Every failure
    mode inside one cycle is caught and logged, never allowed to kill the loop itself -- this
    subsystem existing at all is strictly additive; its own failure must never make automatic
    resume WORSE than the pre-existing "needs a manual /continue" baseline.
    """
    r = get_redis_client()
    while True:
        try:
            now = time.time()
            for key in list(r.scan_iter(_TRACKING_KEY_TEMPLATE.format(task_id="*"))):
                task_id = key.split(":")[-1]
                try:
                    raw = r.get(key)
                    if not raw:
                        continue
                    tracking = json.loads(raw)
                    if now < tracking.get("next_retry_at", 0):
                        continue
                    if is_cancel_requested(task_id, client=r):
                        r.delete(key)
                        continue
                    if get_task_state(task_id, client=r) not in _AUTO_RESUMABLE_PAUSE_STATES:
                        # Already resumed (manually, or a prior cycle) or otherwise moved on --
                        # this tracking entry is stale.
                        r.delete(key)
                        continue
                    attempts = tracking.get("attempts", 0)
                    if attempts >= _MAX_AUTO_RESUME_ATTEMPTS:
                        logger.info(
                            "Task %s: giving up automatic gateway resume after %d attempts -- "
                            "left genuinely paused for a human.", task_id, attempts,
                        )
                        r.delete(key)
                        continue
                    # Persist the incremented attempt count BEFORE attempting: if this resume
                    # re-pauses for the same reason, record_gateway_pause() (called as a side
                    # effect from inside the attempt itself) preserves whatever is already
                    # here -- so the increment must happen first, not after, for the cap above
                    # to ever actually apply across repeated pauses.
                    tracking["attempts"] = attempts + 1
                    r.set(key, json.dumps(tracking), ex=_TRACKING_TTL_SEC)
                    logger.info("Task %s: attempting automatic gateway resume (attempt %d)", task_id, attempts + 1)
                    await _attempt_one_resume(task_id, client, classifier_model, manager_model)
                    if get_task_state(task_id, client=r) not in _AUTO_RESUMABLE_PAUSE_STATES:
                        # Resumed successfully, or paused for a genuinely different reason
                        # (ambiguity/sign-off/etc.) -- either way, nothing left to auto-resume.
                        r.delete(key)
                    # else: still paused for the same reason -- record_gateway_pause() already
                    # rescheduled this key with a fresh next_retry_at as a side effect of the
                    # attempt above; leave it alone.
                except Exception:  # noqa: BLE001 -- one task's failure must never break the sweep
                    # A genuinely unexpected crash (not a normal re-pause -- that path never
                    # raises, see _attempt_one_resume()'s own docstring). The attempt count was
                    # already incremented and persisted above, before the attempt; only the
                    # backoff timer needs rescheduling here, never a second increment.
                    logger.warning("Task %s: automatic gateway resume attempt itself failed", task_id, exc_info=True)
                    try:
                        raw = r.get(key)
                        if raw:
                            tracking = json.loads(raw)
                            tracking["next_retry_at"] = now + _backoff_seconds(tracking.get("attempts", 0))
                            r.set(key, json.dumps(tracking), ex=_TRACKING_TTL_SEC)
                    except Exception:  # noqa: BLE001 -- rescheduling itself must never crash the loop
                        logger.warning("Task %s: failed to reschedule after a failed auto-resume attempt", task_id, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad cycle must never kill the whole poller
            logger.warning("Gateway auto-resume sweep cycle itself failed", exc_info=True)
        await asyncio.sleep(poll_interval_sec)
