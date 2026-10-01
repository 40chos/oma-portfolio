"""Phase 12: the tier-3/4 pre-sign-off gate -- a real gap found while
building the chat UI's "plan/act-style approval" pattern (one of the
build plan's three named UI patterns for this phase). `PAUSE_SIGN_OFF_REQUIRED`
existed as a task-state constant since Phase 6 but was never actually
wired into the loop: a tier-3/4 task would proceed straight through
delegate_to_specialist() without ever pausing for Operator's sign-off,
directly contradicting MANAGER_CONSTITUTION.md's own tier definitions
("tier_3_pause_before", "tier_4_presign_off").

The pending contract is stored in Redis (not just held in the async
call stack) specifically so it survives across the request/response
boundary of a real chat UI -- Operator sees the proposal in one HTTP
request, and approves/rejects it in a LATER, separate request, quite
possibly after closing and reopening the chat window.
"""

from __future__ import annotations

import json

import redis

from contracts.schema import TaskContract
from infra.redis_client import get_redis_client

_PENDING_KEY = "oma:pending_contract:{task_id}"
_PENDING_INDEX_KEY = "oma:pending_contract:index"  # a set of task_ids, for listing
_PENDING_TTL_SEC = 86_400  # 24h, matching task_state.py's own TTL choice


def store_pending_contract(
    task_id: str,
    contract: TaskContract,
    module_lock_name: str,
    correction_result: dict,
    module_for_repeat_check: str | None = None,
    client: redis.Redis | None = None,
    diagnosis: str | None = None,
) -> None:
    """`diagnosis` (Phase 22, 2026-07-23): optional, for the
    diagnose-then-confirm pause (manager/loop.py's pause_if-driven
    gate) -- a readonly_investigation specialist's own findings/
    proposed plan, shown to Operator alongside the real contract he's
    approving. None for the ordinary tier-3/4 sign-off case, which has
    no separate diagnosis step at all. Additive-only: every existing
    caller (the plain sign-off gate) keeps working unchanged.
    """
    r = client or get_redis_client()
    payload = {
        "contract": contract.model_dump(mode="json"),
        "module_lock_name": module_lock_name,
        "correction_result": correction_result,
        "module_for_repeat_check": module_for_repeat_check,
        "diagnosis": diagnosis,
    }
    r.set(_PENDING_KEY.format(task_id=task_id), json.dumps(payload), ex=_PENDING_TTL_SEC)
    r.sadd(_PENDING_INDEX_KEY, task_id)


def get_pending_contract(task_id: str, client: redis.Redis | None = None) -> dict | None:
    """Returns the raw stored payload (contract dict, module_lock_name,
    correction_result) -- callers that need a real TaskContract object
    should construct one via TaskContract.model_validate(payload["contract"]).
    """
    r = client or get_redis_client()
    raw = r.get(_PENDING_KEY.format(task_id=task_id))
    if raw is None:
        return None
    return json.loads(raw)


def clear_pending_contract(task_id: str, client: redis.Redis | None = None) -> None:
    r = client or get_redis_client()
    r.delete(_PENDING_KEY.format(task_id=task_id))
    r.srem(_PENDING_INDEX_KEY, task_id)


def list_pending_task_ids(client: redis.Redis | None = None) -> list[str]:
    """For the chat UI's pending-items queue -- every task_id currently
    awaiting Operator's tier-3/4 sign-off. A task_id whose key already
    expired (TTL) is pruned from the index lazily here, since Redis
    doesn't clean up SREM entries on its own when the paired key expires.
    """
    r = client or get_redis_client()
    task_ids = list(r.smembers(_PENDING_INDEX_KEY) or [])
    live = []
    for task_id in task_ids:
        if r.exists(_PENDING_KEY.format(task_id=task_id)):
            live.append(task_id)
        else:
            r.srem(_PENDING_INDEX_KEY, task_id)
    return live
