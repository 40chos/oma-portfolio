"""Phase 16 (§20.3): the live trace bus -- a separate, ephemeral,
fire-and-forget channel for live visibility only, using the same Redis
instance/DB index already provisioned for everything else. Never
replaces or duplicates agent_memory_events (Postgres), which stays the
durable, permanent record -- this is purely for a UI watching a task
happen right now.

publish_trace_event() is called, never awaited/blocking-critical: a
Pub/Sub publish failure (dead connection, a missing ACL grant, network
blip) must never break the actual Manager loop. Every call site wraps
its own real work; this function wraps Redis itself, so a caller never
needs its own try/except around this specifically.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

import redis

from infra.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_CHANNEL_TEMPLATE = "oma:trace:{task_id}"

Level = Literal["manager", "specialist"]
Status = Literal["running", "passed", "failed", "paused"]


def publish_trace_event(
    task_id: str,
    event: dict,
    client: redis.Redis | None = None,
) -> None:
    """PUBLISH one trace event to oma:trace:{task_id}. `event` should
    already carry the real shape described in §20.3
    (level/actor/phase/message/status) -- this function adds `ts` if
    the caller didn't set one, and otherwise passes the dict through
    unchanged. Swallows every exception -- a dead Redis connection, a
    missing PUBLISH ACL grant, anything -- logging it once and moving
    on, never propagating into the caller's own real work.
    """
    payload = {"ts": event.get("ts", time.time()), **event}
    try:
        r = client or get_redis_client()
        channel = _CHANNEL_TEMPLATE.format(task_id=task_id)
        r.publish(channel, json.dumps(payload))
        
        # Cache history for UI refresh persistence (24 hour TTL)
        hist_key = f"oma:trace_history:{task_id}"
        r.rpush(hist_key, json.dumps(payload))
        r.expire(hist_key, 86400)
    except Exception:
        logger.warning("publish_trace_event failed for task_id=%s (non-fatal)", task_id, exc_info=True)


def channel_name(task_id: str) -> str:
    """The real channel name a subscriber (GET /api/stream/{task_id})
    needs -- kept as one function so the naming convention lives in
    exactly one place.
    """
    return _CHANNEL_TEMPLATE.format(task_id=task_id)
