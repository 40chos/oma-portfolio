"""The fencing-token lock primitive, per the technical document's §6 and
the build plan's Phase 1 step 5 -- built and tested here, in full, before
any real specialist exists to use it (Phase 9's Build specialist is
required to call check_fence() immediately before every real Odoo write,
with no exceptions).

Pattern (Kleppmann's canonical fix for the "lock expired mid-write" race):
  - acquire_module_lock: SET NX PX for the mutual-exclusion lock itself,
    INCR for a monotonically increasing fence counter. The counter keeps
    advancing even across lock expiry/re-acquisition -- that's the whole
    point, since it's what lets a late write from a previous lock holder
    be detected as stale.
  - check_fence: the *protected resource* (never the lock itself) compares
    the caller's token against the highest token any caller has already
    been allowed to commit with. A token lower than that is rejected,
    even if the caller still believes it holds the lock.

This module owns both halves so a caller never has to reason about the
race directly -- it just calls acquire, does its work, and calls
check_fence right before the real write.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import redis

from infra.redis_client import get_redis_client

_logger = logging.getLogger(__name__)

_LOCK_KEY = "oma:lock:{module}"
_FENCE_COUNTER_KEY = "oma:fence:{module}:counter"
_FENCE_COMMITTED_KEY = "oma:fence:{module}:committed"

# Atomic compare-and-advance: only accept a token >= whatever's already
# been committed, and only ever move the committed marker forward.
_CHECK_FENCE_LUA = """
local committed_key = KEYS[1]
local token = tonumber(ARGV[1])
local committed = tonumber(redis.call('GET', committed_key) or '0')
if token < committed then
    return 0
end
redis.call('SET', committed_key, token)
return 1
"""


@dataclass(frozen=True)
class LockHandle:
    module: str
    task_id: str
    fence_token: int
    acquired: bool


class ModuleLockError(RuntimeError):
    pass


def acquire_module_lock(
    module_name: str,
    task_id: str,
    ttl_ms: int = 30_000,
    client: redis.Redis | None = None,
) -> LockHandle:
    """Acquire the mutual-exclusion lock for a module and mint a fence
    token for this acquisition. The lock itself can expire (ttl_ms) --
    that's expected and survivable, because check_fence(), not the lock,
    is what actually protects the resource.
    """
    r = client or get_redis_client()
    lock_key = _LOCK_KEY.format(module=module_name)
    counter_key = _FENCE_COUNTER_KEY.format(module=module_name)

    acquired = bool(r.set(lock_key, task_id, nx=True, px=ttl_ms))
    # The fence counter advances on every acquisition attempt that wins
    # the lock -- monotonic per module, independent of TTL expiry.
    if not acquired:
        return LockHandle(module=module_name, task_id=task_id, fence_token=-1, acquired=False)

    fence_token = r.incr(counter_key)
    return LockHandle(module=module_name, task_id=task_id, fence_token=fence_token, acquired=True)


async def acquire_module_lock_same_task_aware(
    module_name: str,
    task_id: str,
    ttl_ms: int = 30_000,
    max_wait_sec: float = 60.0,
    poll_interval_sec: float = 0.5,
    client: redis.Redis | None = None,
) -> LockHandle:
    """Phase 31 §6/Phase B (2026-08-08): the real fix for a genuine bug found live during the
    Phase B controlled concurrency experiment -- `acquire_module_lock()`'s own `SET NX` is a
    plain mutual-exclusion lock with NO concept of "the current holder is actually my own sibling
    node, not a different task." Before this fix, two concurrent nodes of the SAME decomposed
    task (Phase 31 §2's own graph scheduler, at OMA_GRAPH_MAX_CONCURRENT_NODES > 1) racing on the
    same generated module would have the SECOND one hard-rejected outright
    (`specialists/build/specialist.py`'s own `lock_rejected: True` failure path) even though
    they belong to the exact same task and are not a genuine cross-task collision at all --
    confirmed live: `"Model 'product.template' is currently locked by another task -- refusing
    to proceed concurrently"` fired for two sibling nodes of ONE task.

    Fixed here, not by making the lock silently reentrant (letting both siblings hold it and
    proceed fully concurrently would reopen exactly the concurrent-generation race §6's own
    per-node branch isolation + serialized apply were built to prevent -- see this module's own
    fencing discipline) -- instead, a same-task collision now WAITS (bounded, async, real
    `asyncio.sleep` between polls so this never blocks the event loop the way a blocking
    `time.sleep` retry loop would) for the sibling to finish and release, then re-acquires,
    rather than failing the round outright. A genuinely DIFFERENT task's collision is untouched
    -- detected on the FIRST failed attempt (by checking who the real, current lock holder is)
    and returned as an immediate rejection, exactly as `acquire_module_lock()` always did; this
    function only ever waits for a lock held by `task_id` itself.

    `max_wait_sec` bounds the wait -- if the sibling doesn't release within it (a genuinely stuck
    or unusually slow round), this returns the same `acquired=False` rejection the caller already
    knows how to handle, never hangs forever.
    """
    r = client or get_redis_client()
    lock_key = _LOCK_KEY.format(module=module_name)
    deadline = time.monotonic() + max_wait_sec

    while True:
        handle = acquire_module_lock(module_name, task_id, ttl_ms=ttl_ms, client=r)
        if handle.acquired:
            return handle

        current_holder = r.get(lock_key)
        if isinstance(current_holder, bytes):
            current_holder = current_holder.decode()
        if current_holder is not None and current_holder != task_id:
            # A genuinely different task holds it -- fail immediately, exactly as
            # acquire_module_lock() always has. Never waited on for a moment, by design.
            return handle

        # Either our own sibling still holds it, or the lock key vanished between our failed SET
        # and this GET (a real, benign TTL-expiry race) -- either way, worth one more attempt
        # rather than a hard failure.
        if time.monotonic() >= deadline:
            return handle
        await asyncio.sleep(poll_interval_sec)


def check_fence(module_name: str, fence_token: int, client: redis.Redis | None = None) -> bool:
    """Run immediately before any real write. Returns True only if this
    token is still allowed to commit -- i.e. no caller with a higher
    token has already committed since this one was issued.
    """
    r = client or get_redis_client()
    committed_key = _FENCE_COMMITTED_KEY.format(module=module_name)
    result = r.eval(_CHECK_FENCE_LUA, 1, committed_key, fence_token)
    return bool(result)


def release_module_lock(module_name: str, task_id: str, client: redis.Redis | None = None) -> None:
    """Best-effort release. Only releases if this task_id still owns it --
    never blindly deletes, since a stale caller shouldn't be able to
    release a lock a newer caller has since acquired.
    """
    r = client or get_redis_client()
    lock_key = _LOCK_KEY.format(module=module_name)
    current = r.get(lock_key)
    if current == task_id:
        r.delete(lock_key)


_DB_INSTALL_LOCK_PREFIX = "db_install"


def acquire_db_install_lock(
    db: str,
    task_id: str,
    ttl_ms: int = 600_000,
    max_wait_sec: float = 600.0,
    poll_interval_sec: float = 1.0,
    client: redis.Redis | None = None,
) -> LockHandle:
    """Real fix, 2026-08-09 (the project owner's own explicit request: "smart" concurrency that "takes into
    account... installation of sandbox platform"). Closes the ONE genuinely open gap named in
    `tools_odoo/module_dev/toolchain.py's own `install_module()` comment (Phase 31 §9/Phase B,
    2026-08-08): two constraint nodes correctly non-colliding on the per-target-model lock above
    (different real Odoo models) can still launch two `odoo-bin -i` registry-bootstrap
    subprocesses CONCURRENTLY against the SAME physical database -- a real, live-confirmed
    `psycopg2.errors.SerializationFailure` on shared core metadata tables (ir_model_fields/
    ir_model_data/etc.), regardless of which model each install targets. That comment explicitly
    (and correctly) rejects widening `acquire_module_lock()`'s own per-module scope to cover this
    -- doing so would re-serialize genuinely independent nodes' own expensive LLM generation just
    to protect a real but much cheaper ~10-50s install step. This is a deliberately SEPARATE lock
    namespace (`{_DB_INSTALL_LOCK_PREFIX}:{{db}}`, never `{{module}}` -- reuses the exact same
    proven SET-NX-PX + real Redis key machinery `acquire_module_lock()` already provides, just
    keyed by the real shared resource -- the physical database's own registry bootstrap -- not
    the generated module's own git-tracked files) so a caller only ever waits behind another
    install targeting the SAME real db, never a sibling targeting a different one.

    Synchronous (blocking `time.sleep` poll, not `asyncio.sleep`) on purpose: `install_module()`
    itself is synchronous (its own existing SerializationFailure retry-backoff already blocks the
    same way, right next to this call's own use site) -- matching the surrounding code's real
    concurrency model instead of introducing an async/sync mismatch. `ttl_ms` defaults generously
    above `install_module()`'s own real worst-case subprocess timeout (300s plain / 480s
    test-enabled) so a slow-but-genuinely-still-running install is never preempted mid-write by
    its own lock expiring. On a genuine `max_wait_sec` timeout (something is stuck, or this is a
    very long real queue), returns `acquired=False` rather than blocking forever -- the caller
    proceeds with the install anyway, falling back to the EXISTING retry-on-SerializationFailure
    safety net, so this is a real, load-bearing speed/collision-avoidance optimization for the
    common case, never a new way to deadlock or hard-fail a round that would have succeeded
    without it.
    """
    r = client or get_redis_client()
    lock_name = f"{_DB_INSTALL_LOCK_PREFIX}:{db}"
    deadline = time.monotonic() + max_wait_sec
    while True:
        handle = acquire_module_lock(lock_name, task_id, ttl_ms=ttl_ms, client=r)
        if handle.acquired or time.monotonic() >= deadline:
            return handle
        time.sleep(poll_interval_sec)


def release_db_install_lock(db: str, task_id: str, client: redis.Redis | None = None) -> None:
    """The `acquire_db_install_lock()` counterpart -- see that function's own docstring for the
    real incident this closes. Same best-effort, only-release-if-still-owner discipline as
    `release_module_lock()` (reused directly, just with the namespaced db-install lock name).
    """
    release_module_lock(f"{_DB_INSTALL_LOCK_PREFIX}:{db}", task_id, client=client)


# ---------------------------------------------------------------------------
# Heartbeat-renewed lease (Item A, 2026-08-22 -- warehouse-arrival fenced
# write). Extends this module rather than duplicating it, per this file's
# own stated design intent ("this module owns both halves so a caller never
# has to reason about the race directly"). None of the locks above are
# heartbeat-renewed (all fixed-TTL SET NX PX); this is new machinery for a
# caller that needs to hold a lock across a longer-than-TTL operation
# (confirm_arrived_fenced()'s XML-RPC round trip) without picking an
# unreasonably long fixed TTL.
#
# Full design + its own 5-round sequential audit trail:
# docs/planning/DIRECTION3_SUPPLIERS_2026-08-21.md, "Warehouse-Arrival
# Fenced Write + Linphone Call-Link -- Final Plan" section, Step 4 (final
# plan). Three real concurrency-safety bugs were found and fixed across
# that audit, in order:
#   round 1: SupervisedLease's original "lease loss cancels the protected
#     work" claim is false -- asyncio.to_thread-wrapped blocking XML-RPC
#     calls cannot be cancelled once dispatched (documented CPython
#     behavior, python/cpython#107505, closed "not planned"). Fixed by
#     making the real safety net confirm_arrived_fenced()'s own Postgres
#     row-lock + already_done idempotency check (never cancellation), and
#     bounding the abandoned thread via TimeoutTransport at the XML-RPC
#     client layer (see an XML-RPC caller's own _call() construction for an example).
#   round 2: the "prompt unblock via __aexit__" claim was also false --
#     `async with`'s body must finish before __aexit__ runs at all, so
#     nothing inside __aexit__ can race a still-pending body statement.
#     Fixed by moving the race to the only place a real await point exists:
#     `run_protected()`, called AT the with-block body, not from __aexit__.
#   round 3: `asyncio.wait(..., return_when=FIRST_COMPLETED)`'s `done` set
#     is not guaranteed to contain only one task -- if the heartbeat task
#     and the work task finish in the same event-loop pass, both land in
#     `done` together, and code that only checks "did the task I expected
#     win" leaves the other task's exception unretrieved (a real
#     "Task exception was never retrieved" warning at GC time). Fixed via
#     `_drain_heartbeat()`, called unconditionally from both
#     `run_protected()` and `__aexit__()` whenever the heartbeat task is in
#     `done`, regardless of which branch is otherwise taken.
# Rounds 4 and 5 both came back clean (genuine convergence, not a round-cap
# stop) -- the code below matches the final, round-3-corrected design.
# ---------------------------------------------------------------------------

_LEASE_KEY = "oma:lease:{resource}"
_LEASE_COUNTER_KEY = "oma:lease:{resource}:counter"


@dataclass(frozen=True)
class LeaseHandle:
    resource: str
    task_id: str
    fence_token: int
    acquired: bool


class LeaseLostError(RuntimeError):
    """Raised inside the supervised heartbeat task when this task_id no
    longer owns the lease (someone else's SET NX won it, or it expired and
    was re-acquired elsewhere)."""


def acquire_heartbeat_lease(
    resource: str, task_id: str, ttl_ms: int = 15_000,
    client: redis.Redis | None = None,
) -> LeaseHandle:
    """Synchronous acquire (mirrors acquire_module_lock's own sync SET NX
    PX pattern exactly -- only the RENEWAL loop needs to be async). `resource`
    should be a stable, real identifier for the thing being protected -- for
    Item A, f"picking:{picking_id}", never the fence_token or anything
    per-attempt.
    """
    r = client or get_redis_client()
    lock_key = _LEASE_KEY.format(resource=resource)
    counter_key = _LEASE_COUNTER_KEY.format(resource=resource)
    acquired = bool(r.set(lock_key, task_id, nx=True, px=ttl_ms))
    if not acquired:
        return LeaseHandle(resource=resource, task_id=task_id, fence_token=-1, acquired=False)
    fence_token = r.incr(counter_key)
    return LeaseHandle(resource=resource, task_id=task_id, fence_token=fence_token, acquired=True)


async def _heartbeat_loop(
    resource: str, task_id: str, ttl_ms: int, renew_interval_sec: float,
    client: redis.Redis,
) -> None:
    """Runs forever until cancelled by the caller (SupervisedLease's own
    __aexit__, or run_protected's cancellation of itself once it has
    finished racing) or raises LeaseLostError if ownership was lost -- this
    function NEVER silently stops renewing without one of those two
    outcomes, so the LEASE is never left un-supervised.

    This loop guarantees the *lease* is never unsupervised. It does NOT and
    cannot guarantee the caller's protected work is stopped -- that is
    bounded and made safe by other mechanisms; see
    SupervisedLease.run_protected's docstring for the property this loop is
    actually raced against.
    """
    lock_key = _LEASE_KEY.format(resource=resource)
    while True:
        await asyncio.sleep(renew_interval_sec)
        current = client.get(lock_key)
        if isinstance(current, bytes):
            current = current.decode()
        if current != task_id:
            raise LeaseLostError(f"lease for {resource!r} lost (held by {current!r}, expected {task_id!r})")
        client.pexpire(lock_key, ttl_ms)


def _reap_abandoned_work(task: "asyncio.Task") -> None:
    """Done-callback attached to a protected-work task that run_protected
    has already given up on (lease lost first). Its only job is to consume
    the task's eventual result/exception so asyncio never logs an
    'exception was never retrieved' warning for it, and to log that a
    late, abandoned result arrived."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _logger.warning("abandoned protected-work task %r finished with exception: %r", task.get_name(), exc)
    else:
        _logger.info("abandoned protected-work task %r finished late with result: %r", task.get_name(), task.result())


def _drain_heartbeat(task: "asyncio.Task | None"):
    """Round 3's fix. The single place that knows how to safely retrieve a
    heartbeat task's outcome without raising it -- used from BOTH
    `run_protected` (right after `asyncio.wait` returns, whenever the
    heartbeat task shows up in `done` alongside the work task) and
    `__aexit__` (whenever the heartbeat task is already done by the time
    teardown runs, for the exact same reason). `asyncio.wait(...,
    return_when=FIRST_COMPLETED)` only guarantees `done` is non-empty, not
    that it contains exactly one task -- if the heartbeat task and the work
    task finish in the same event-loop pass, both land in `done` together,
    and whichever code path only checks "did MY expected task win" leaves
    the other task's exception unretrieved, which asyncio logs as 'Task
    exception was never retrieved' at GC time. Calling `task.exception()`
    is safe and non-raising (it RETURNS the exception object rather than
    raising it) as long as the task wasn't cancelled -- hence the
    cancelled() guard, since `.exception()`/`.result()` on a cancelled task
    itself raises CancelledError. Idempotent: safe to call more than once
    on the same already-drained task."""
    if task is None or not task.done() or task.cancelled():
        return None
    return task.exception()


class SupervisedLease:
    """Async context manager PLUS an explicit work-racing method. Acquires
    the lease in __aenter__ and spawns the heartbeat loop as a supervised
    asyncio.Task. Guarantees on __aexit__ that the heartbeat task is always
    cancelled/awaited-or-drained (no leaked task, no silently stopped
    renewal, no unretrieved exception) and the Redis lease is released.

    `run_protected(coro)`:
      - wraps `coro` (e.g. `asyncio.to_thread(_call)`) in its own Task,
      - awaits `asyncio.wait({self._heartbeat_task, work_task},
        return_when=asyncio.FIRST_COMPLETED)`,
      - `done` from that `asyncio.wait` call is not guaranteed to contain
        only one task -- if the heartbeat task also finished in the same
        pass, its exception is drained (via `_drain_heartbeat`, never
        re-raised at this point) BEFORE branching on the outcome, so it is
        never left unretrieved regardless of which branch is taken below.
      - if `work_task` finished (whether or not the heartbeat also did):
        returns its result (or re-raises its exception) normally -- the
        work outcome wins on a tie, matching the existing contract that
        `run_protected` reports `coro`'s outcome.
      - if only the heartbeat task finished (raised LeaseLostError, and
        `work_task` is still pending): cancels `work_task` and attaches
        `_reap_abandoned_work` as its done-callback (so its eventual
        completion is consumed, not left as an unretrieved-exception
        warning), then raises the heartbeat task's `LeaseLostError`
        immediately -- this return/raise is PROMPT, bounded by nothing
        except the two tasks' actual race, not by how long the abandoned
        work keeps running.

    What this still does NOT and cannot guarantee, stated honestly:
    cancelling `work_task` only detaches the *awaiting* asyncio Task from
    the underlying OS thread started by `asyncio.to_thread(_call)` --
    `_call()` is a plain synchronous function doing a blocking Odoo XML-RPC
    round-trip, and Python has no mechanism to forcibly stop a running
    thread (documented CPython behavior, python/cpython#107505, closed "not
    planned"; the same reason trio/anyio's own analogous flag is named
    `abandon_on_cancel`, not `cancel`). So on lease loss: `run_protected`
    itself returns/raises promptly and the caller can react immediately,
    but if the XML-RPC request has already been dispatched, it keeps
    running in its thread and MAY still land at Odoo afterward -- the
    thread is abandoned, not cancelled. This is bounded, not eliminated:
    `_call()`'s `ServerProxy` uses `TimeoutTransport` so an abandoned thread terminates within a
    fixed worst-case bound rather than running forever. THE ACTUAL
    CORRECTNESS GUARANTEE against a late-landing abandoned write is
    `confirm_arrived_fenced()`'s own `SELECT ... FOR UPDATE` row lock +
    idempotent `already_done` check, not cancellation of the thread --
    standard distributed-systems doctrine (idempotent server-side write,
    not client-side abort, is what makes an unreliable in-flight call
    safe).

    Usage -- note `run_protected` replaces a bare
    `await asyncio.to_thread(_call)` inside the with-block body:
        async with SupervisedLease("picking:4821", task_id) as lease:
            if not lease.acquired:
                return {"ok": False, "reason": "locked", ...}
            result = await lease.run_protected(
                asyncio.to_thread(_call_confirm_arrived_fenced, ..., lease.fence_token)
            )
    """

    def __init__(self, resource: str, task_id: str, ttl_ms: int = 15_000,
                 renew_interval_sec: float = 5.0, client: redis.Redis | None = None):
        self.resource = resource
        self.task_id = task_id
        self.ttl_ms = ttl_ms
        self.renew_interval_sec = renew_interval_sec
        self.client = client or get_redis_client()
        self.handle: LeaseHandle | None = None
        self._heartbeat_task: "asyncio.Task | None" = None

    @property
    def acquired(self) -> bool:
        return bool(self.handle and self.handle.acquired)

    @property
    def fence_token(self) -> int:
        if self.handle is None:
            raise RuntimeError("fence_token accessed before __aenter__ ran")
        return self.handle.fence_token

    async def __aenter__(self) -> "SupervisedLease":
        # Real bug found and fixed here, 2026-08-22 (during unit-test
        # writing, not caught by the design's own 5-round audit): the
        # design's own docstring/usage always calls `lease.acquired` and
        # `lease.run_protected(...)`/`lease.fence_token` on the `as lease`
        # binding -- but returning `self.handle` (a plain LeaseHandle
        # dataclass, no methods) instead of `self` would make
        # `lease.run_protected(...)` raise AttributeError on the very
        # first real call. Returns `self` -- the `acquired`/`fence_token`
        # properties above delegate to `self.handle` so every documented
        # call-site usage (`lease.acquired`, `lease.fence_token`,
        # `lease.run_protected(...)`) actually works.
        self.handle = acquire_heartbeat_lease(
            self.resource, self.task_id, ttl_ms=self.ttl_ms, client=self.client,
        )
        if self.handle.acquired:
            self._heartbeat_task = asyncio.create_task(
                _heartbeat_loop(self.resource, self.task_id, self.ttl_ms,
                                 self.renew_interval_sec, self.client),
                name=f"oma_lease_heartbeat:{self.resource}",
            )
        return self

    async def run_protected(self, coro):
        """Race `coro` against this lease's heartbeat task. See the class
        docstring above for the full contract. Must only be called after
        __aenter__ returned `acquired=True`; raises RuntimeError otherwise
        (a bug at the call site, not an expected runtime outcome)."""
        if self._heartbeat_task is None:
            raise RuntimeError("run_protected() called without an acquired lease")

        work_task = asyncio.ensure_future(coro)
        done, _pending = await asyncio.wait(
            {self._heartbeat_task, work_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # `done` can legitimately contain BOTH tasks if they finished in
        # the same event-loop pass. Drain the heartbeat task's outcome
        # unconditionally, before branching, so it is never left
        # unretrieved regardless of which branch below is taken.
        heartbeat_exc = None
        if self._heartbeat_task in done:
            heartbeat_exc = _drain_heartbeat(self._heartbeat_task)

        if work_task in done:
            return work_task.result()  # re-raises if coro itself raised; work wins on a tie

        # Only the heartbeat task finished -- it must have raised
        # LeaseLostError (that's the only way _heartbeat_loop ever
        # completes on its own; heartbeat_exc holds it, already drained
        # above). Detach from work_task promptly.
        work_task.cancel()
        work_task.add_done_callback(_reap_abandoned_work)
        raise heartbeat_exc

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._heartbeat_task is not None:
            if not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except (asyncio.CancelledError, LeaseLostError):
                    pass
            else:
                # The heartbeat task may already be done here -- e.g. it
                # finished in the same asyncio.wait() pass as work_task
                # inside run_protected. Its exception must still be
                # retrieved or asyncio logs "Task exception was never
                # retrieved" at GC time. _drain_heartbeat is idempotent, so
                # this is safe to call even if run_protected already
                # drained it moments ago.
                _drain_heartbeat(self._heartbeat_task)
        if self.handle is not None and self.handle.acquired:
            release_lease(self.resource, self.task_id, client=self.client)


def release_lease(resource: str, task_id: str, client: redis.Redis | None = None) -> None:
    """The heartbeat-lease counterpart to `release_module_lock()` -- a
    small, structurally-identical twin against `_LEASE_KEY` (not
    `_LOCK_KEY`), since the two live in different key namespaces. Only
    releases if this task_id still owns it -- never blindly deletes."""
    r = client or get_redis_client()
    lock_key = _LEASE_KEY.format(resource=resource)
    current = r.get(lock_key)
    if current == task_id:
        r.delete(lock_key)
