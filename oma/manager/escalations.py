"""Phase 15 (§19.8): storage for a PauseForOperator escalation, so it can be
listed in the chat UI's pending-items queue (GET /api/pending) later,
not just visible in the one chat reply that triggered it -- same
Redis-backed pattern as manager/sign_off.py's pending contracts, and
for the same reason: Operator may see this queue in a separate, later
request, possibly after closing and reopening the chat window.
"""

from __future__ import annotations

import json

import redis

from infra.redis_client import get_redis_client

_ESCALATION_KEY = "oma:pending_escalation:{task_id}"
_ESCALATION_INDEX_KEY = "oma:pending_escalation:index"
_ESCALATION_TTL_SEC = 86_400  # 24h, matching sign_off.py's own TTL choice


def store_pending_escalation(
    task_id: str, message: str, prior_rounds: list, client: redis.Redis | None = None
) -> None:
    r = client or get_redis_client()
    payload = {
        "task_id": task_id,
        "message": message,
        "prior_rounds": [
            r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in prior_rounds
        ],
    }
    r.set(_ESCALATION_KEY.format(task_id=task_id), json.dumps(payload), ex=_ESCALATION_TTL_SEC)
    r.sadd(_ESCALATION_INDEX_KEY, task_id)


def clear_pending_escalation(task_id: str, client: redis.Redis | None = None) -> None:
    r = client or get_redis_client()
    r.delete(_ESCALATION_KEY.format(task_id=task_id))
    r.srem(_ESCALATION_INDEX_KEY, task_id)


def list_pending_escalations(client: redis.Redis | None = None) -> list[dict]:
    r = client or get_redis_client()
    task_ids = list(r.smembers(_ESCALATION_INDEX_KEY) or [])
    results = []
    for task_id in task_ids:
        raw = r.get(_ESCALATION_KEY.format(task_id=task_id))
        if raw is None:
            r.srem(_ESCALATION_INDEX_KEY, task_id)
            continue
        results.append(json.loads(raw))
    return results
