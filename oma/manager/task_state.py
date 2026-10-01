"""Per-task pause state in Redis -- a small, shared primitive used by
the gateway-outage orchestration (Phase 6 step 3) and, later, by
ambiguity-pause and tier-3/4 sign-off-pause states. Each pause reason
gets its own distinct string so a caller (or Operator) can tell them apart
-- "the gateway is down" reads very differently from "I need your
sign-off" or "I have a clarifying question," and they must never be
worded the same way.
"""

from __future__ import annotations

from infra.redis_client import get_redis_client

PAUSE_GATEWAY_UNAVAILABLE = "paused:gateway_unavailable"
PAUSE_AMBIGUITY = "paused:ambiguity"
PAUSE_SIGN_OFF_REQUIRED = "paused:sign_off_required"
PAUSE_REPEATED_FAILURE = "paused:repeated_failure"
PAUSE_TASK_CUT_OFF = "paused:task_cut_off"
# Phase 17 (§21.5.4): Operator explicitly clicked Pause on an already-
# escalated task -- distinct from the escalation itself (the round loop
# already exited when the round budget was hit; this just marks it
# "intentionally frozen, resumable" rather than "actively waiting on
# you," so the dashboard can render the two differently).
PAUSE_ROUND_BUDGET_EXHAUSTED = "paused:round_budget"
# Phase 22 (2026-07-23): distinct from PAUSE_SIGN_OFF_REQUIRED on
# purpose -- that one means "this touches something sensitive, confirm
# the risk before I proceed"; this one means "here's what I found and
# propose to do, confirm the PLAN before I proceed" -- opt-in, any
# task, any tier, driven by contract.pause_if rather than tier_value.
PAUSE_DIAGNOSIS_REVIEW = "paused:diagnosis_review"
STATE_RUNNING = "running"

_KEY_TEMPLATE = "oma:task:{task_id}:state"


def set_task_state(task_id: str, state: str, client=None) -> None:
    r = client or get_redis_client()
    r.set(_KEY_TEMPLATE.format(task_id=task_id), state, ex=86_400)  # 24h TTL, avoid unbounded growth


def get_task_state(task_id: str, client=None) -> str | None:
    r = client or get_redis_client()
    return r.get(_KEY_TEMPLATE.format(task_id=task_id))


def clear_task_state(task_id: str, client=None) -> None:
    r = client or get_redis_client()
    r.delete(_KEY_TEMPLATE.format(task_id=task_id))


_CANCEL_KEY_TEMPLATE = "oma:task:{task_id}:cancel_requested"


def request_cancel(task_id: str, client=None) -> None:
    """Phase 16 (§20.6): a real, user-initiated cancel -- distinct from
    the automatic PauseForOperator escalation. A separate key from the
    pause-reason state above, since "Operator wants this stopped" and "the
    Manager is currently paused for some reason" are independent facts
    (a running task can be cancelled; a paused one has nothing to
    cancel mid-flight, but the flag is still checked and honored if it
    later resumes).
    """
    r = client or get_redis_client()
    r.set(_CANCEL_KEY_TEMPLATE.format(task_id=task_id), "1", ex=86_400)


def is_cancel_requested(task_id: str, client=None) -> bool:
    r = client or get_redis_client()
    return r.get(_CANCEL_KEY_TEMPLATE.format(task_id=task_id)) is not None


def clear_cancel_request(task_id: str, client=None) -> None:
    r = client or get_redis_client()
    r.delete(_CANCEL_KEY_TEMPLATE.format(task_id=task_id))
