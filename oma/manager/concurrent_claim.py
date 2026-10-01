"""Phase 35 §17.3.2 / §18.1: concurrent-task admission control -- a NEW consultation point,
claiming the target model(s) a task is ABOUT to work on before dispatch, so two genuinely
different tasks racing on overlapping scope collide at claim time (cheap, pre-work) rather than
discovering the collision after both have already spent real LLM generation time.

Deliberately NOT a new Redis/Lua subsystem: reuses infra/fencing.py's `acquire_module_lock()` /
`release_module_lock()` directly -- the exact same real, tested, already-production-proven
SET-NX-PX + monotonic fence-counter primitive -- under a distinct key namespace
("taskclaim:<target>" instead of "<module>"), so a task-level claim can never collide with or be
confused for a real module-write lock, while costing zero new lock/Lua code.

Per §18.1's own correction: atomic acquisition alone does not, by itself, protect a write commit
from a resurrected/stale claimant -- that protection already exists, separately, via
infra.fencing.check_fence() at the real write-commit path (specialists/build/specialist.py's own
existing calls), which this module does not touch or duplicate. This module is purely the
ADMISSION layer: deciding whether a new task should even start working on a target, not a second
copy of the write-time fencing check.

§17.10 explicitly lists live claim RENEWAL (a heartbeat coroutine extending an active claim's TTL
for a still-running task) as out of scope for v1 -- this module ships acquire/release only, with a
single generous flat TTL bounding a claim's own lifetime, matching that explicit scope decision
rather than inventing a heartbeat mechanism ahead of it.
"""

from __future__ import annotations

from dataclasses import dataclass

import redis

from infra.fencing import LockHandle, acquire_module_lock, release_module_lock
from infra.redis_client import get_redis_client

_CLAIM_KEY_PREFIX = "taskclaim"

# §18.0.1a: Bucket A placeholder, not yet independently calibrated -- a single flat TTL bounding
# a claim's own lifetime in the absence of live renewal (§17.10), chosen generously above this
# session's own observed real task durations (300-900s typical, up to ~20min) with headroom for
# a genuinely slow round, but bounded so an abandoned claim (a crashed process, an unhandled
# exception before release) self-heals without manual intervention.
_DEFAULT_CLAIM_TTL_MS = 30 * 60 * 1000  # 30 minutes


@dataclass(frozen=True)
class ClaimResult:
    acquired: bool
    task_id: str
    claimed_targets: list[str]
    colliding_target: str | None
    colliding_task_id: str | None


def acquire_task_claim(
    target_keys: list[str], task_id: str, ttl_ms: int = _DEFAULT_CLAIM_TTL_MS,
    client: redis.Redis | None = None,
) -> ClaimResult:
    """All-or-nothing: claims every target in `target_keys`, or none of them. Prevents the
    partial-claim deadlock class where task A holds target 1 waiting for target 2 while task B
    holds target 2 waiting for target 1 -- rather than build real deadlock detection for v1
    (out of scope, §17.10), this sidesteps the whole class by never holding a partial claim.

    Returns ClaimResult with `colliding_target`/`colliding_task_id` naming exactly what blocked
    acquisition, so a caller (or a human reading the trace log) can see precisely what collided,
    not just that something did.
    """
    r = client or get_redis_client()
    if not target_keys:
        return ClaimResult(acquired=True, task_id=task_id, claimed_targets=[], colliding_target=None, colliding_task_id=None)

    acquired_so_far: list[str] = []
    for target in sorted(set(target_keys)):
        claim_name = f"{_CLAIM_KEY_PREFIX}:{target}"
        handle: LockHandle = acquire_module_lock(claim_name, task_id, ttl_ms=ttl_ms, client=r)
        if not handle.acquired:
            current_holder = r.get(f"oma:lock:{claim_name}")
            if isinstance(current_holder, bytes):
                current_holder = current_holder.decode()
            for already in acquired_so_far:
                release_module_lock(f"{_CLAIM_KEY_PREFIX}:{already}", task_id, client=r)
            return ClaimResult(
                acquired=False, task_id=task_id, claimed_targets=[],
                colliding_target=target, colliding_task_id=current_holder,
            )
        acquired_so_far.append(target)
    return ClaimResult(
        acquired=True, task_id=task_id, claimed_targets=acquired_so_far,
        colliding_target=None, colliding_task_id=None,
    )


def release_task_claim(target_keys: list[str], task_id: str, client: redis.Redis | None = None) -> None:
    """Best-effort release of every claimed target -- reuses release_module_lock()'s own
    only-release-if-still-owner discipline, so a stale/expired caller can never release a claim
    a newer claimant has since acquired."""
    r = client or get_redis_client()
    for target in sorted(set(target_keys)):
        release_module_lock(f"{_CLAIM_KEY_PREFIX}:{target}", task_id, client=r)
