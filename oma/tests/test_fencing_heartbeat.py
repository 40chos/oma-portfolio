"""Item A (warehouse-arrival fenced write), 2026-08-22 -- unit tests for
the new heartbeat-renewed lease machinery in infra/fencing.py
(acquire_heartbeat_lease, _heartbeat_loop, SupervisedLease.run_protected,
_drain_heartbeat, release_lease).

Full design + its own 5-round sequential audit trail:
docs/planning/DIRECTION3_SUPPLIERS_2026-08-21.md, "Warehouse-Arrival
Fenced Write + Linphone Call-Link -- Final Plan" section, §5.1. Three real
concurrency-safety bugs were found and fixed across that audit (false
cancellation guarantee, structurally-impossible prompt-unblock claim, and
an asyncio simultaneous-completion race that could silently drop a
heartbeat exception) -- these tests assert the CORRECTED design, not the
original flawed one; see fencing.py's own module-level comment above the
heartbeat-lease section for the full account.

Same convention as the existing test_fencing.py: real Redis
(get_redis_client()), not fakeredis/mocks, with unique per-test key
namespaces cleaned up before and after.
"""
import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.fencing import (
    LeaseHandle,
    LeaseLostError,
    SupervisedLease,
    _drain_heartbeat,
    _heartbeat_loop,
    acquire_heartbeat_lease,
    release_lease,
)
from infra.redis_client import get_redis_client


def _cleanup(resource: str, client) -> None:
    for pattern in (f"oma:lease:{resource}", f"oma:lease:{resource}:counter"):
        for key in client.scan_iter(match=pattern):
            client.delete(key)


def _resource() -> str:
    return f"test.picking.{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# 1-2: acquire_heartbeat_lease
# ---------------------------------------------------------------------------

def test_acquire_heartbeat_lease_first_caller_wins():
    client = get_redis_client()
    resource = _resource()
    _cleanup(resource, client)
    try:
        lease = acquire_heartbeat_lease(resource, "task-A", client=client)
        assert lease.acquired is True
        assert lease.fence_token == 1
    finally:
        _cleanup(resource, client)


def test_acquire_heartbeat_lease_second_caller_blocked():
    client = get_redis_client()
    resource = _resource()
    _cleanup(resource, client)
    try:
        lease_a = acquire_heartbeat_lease(resource, "task-A", client=client)
        assert lease_a.acquired is True
        lease_b = acquire_heartbeat_lease(resource, "task-B", client=client)
        assert lease_b.acquired is False
    finally:
        _cleanup(resource, client)


# ---------------------------------------------------------------------------
# 3-4: _heartbeat_loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_loop_renews_ttl():
    client = get_redis_client()
    resource = _resource()
    _cleanup(resource, client)
    try:
        lease = acquire_heartbeat_lease(resource, "task-A", ttl_ms=200, client=client)
        assert lease.acquired is True
        task = asyncio.create_task(_heartbeat_loop(resource, "task-A", 200, 0.05, client))
        await asyncio.sleep(0.25)  # >1 renewal interval, well under the un-renewed TTL's own 200ms lifetime if renewal failed
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # if renewal never fired, the key would have expired by now (200ms TTL, 250ms elapsed)
        assert client.get(f"oma:lease:{resource}") == "task-A"
    finally:
        _cleanup(resource, client)


@pytest.mark.asyncio
async def test_heartbeat_loop_raises_lease_lost_when_ownership_changes():
    client = get_redis_client()
    resource = _resource()
    _cleanup(resource, client)
    try:
        lease = acquire_heartbeat_lease(resource, "task-A", ttl_ms=30_000, client=client)
        assert lease.acquired is True
        # Simulate a different owner winning the key (e.g. TTL expiry + re-acquisition elsewhere).
        client.set(f"oma:lease:{resource}", "task-B")
        with pytest.raises(LeaseLostError):
            await _heartbeat_loop(resource, "task-A", 30_000, 0.05, client)
    finally:
        _cleanup(resource, client)


# ---------------------------------------------------------------------------
# 5: run_protected -- prompt unblock on lease loss (round 2's fix)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_protected_unblocks_promptly_on_lease_loss():
    client = get_redis_client()
    resource = _resource()
    _cleanup(resource, client)
    try:
        async with SupervisedLease(resource, "task-A", ttl_ms=30_000, renew_interval_sec=3600, client=client) as lease:
            assert lease.acquired is True

            import threading

            work_event = threading.Event()
            late_result_holder = {}

            def blocking_call():
                # A REAL blocking call in a REAL OS thread, mirroring
                # production's asyncio.to_thread(_call) shape exactly --
                # cancelling the asyncio Task wrapping this only detaches
                # the awaiter, it does NOT stop this thread (the whole
                # point of round 1's fix: threads can't be force-cancelled
                # in Python). A plain coroutine-based mock would be
                # genuinely interruptible by Task.cancel(), which does
                # NOT match what the real _call() does -- this thread-based
                # mock is required to test the real, documented "abandoned,
                # not cancelled" property honestly.
                work_event.wait(timeout=5.0)
                late_result_holder["ran"] = True
                return {"ok": True}

            # Real SupervisedLease instance under test: swap its real heartbeat
            # task for one that raises LeaseLostError almost immediately, while
            # the blocking call is still waiting -- so run_protected must return
            # via the heartbeat-lost branch, not the work branch.
            sl = SupervisedLease(resource, "task-A", ttl_ms=30_000, renew_interval_sec=3600, client=client)
            sl.handle = LeaseHandle(resource=resource, task_id="task-A", fence_token=1, acquired=True)

            async def _immediate_lease_loss():
                raise LeaseLostError("forced for test")

            sl._heartbeat_task = asyncio.create_task(_immediate_lease_loss())

            with pytest.raises(LeaseLostError):
                await asyncio.wait_for(
                    sl.run_protected(asyncio.to_thread(blocking_call)), timeout=2.0,
                )

            # The late result must not have landed yet -- run_protected returned
            # before the blocking thread's event was ever set.
            assert "ran" not in late_result_holder

            # Now let the abandoned thread "finish late" and confirm nothing raises.
            work_event.set()
            await asyncio.sleep(0.1)
            assert late_result_holder.get("ran") is True
    finally:
        _cleanup(resource, client)


# ---------------------------------------------------------------------------
# 5b: simultaneous completion -- round 3's fix
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_protected_drains_heartbeat_exception_on_simultaneous_completion(monkeypatch):
    """asyncio.Task is a C-implemented, immutable type in this interpreter
    build -- individual Task instance methods (.exception/.cancel) cannot
    be monkeypatched directly (TypeError: cannot set attribute of
    immutable type). Spy at the module boundary instead: infra.fencing's
    own `_drain_heartbeat` is a plain, patchable Python function and is
    THE single place production code retrieves a heartbeat task's outcome
    (per its own docstring) -- wrapping it proves the production code
    path actually drained the task, without depending on patching a
    builtin C type."""
    import infra.fencing as fencing_mod

    client = get_redis_client()
    resource = _resource()
    _cleanup(resource, client)
    try:
        sl = SupervisedLease(resource, "task-A", ttl_ms=30_000, renew_interval_sec=3600, client=client)
        sl.handle = LeaseHandle(resource=resource, task_id="task-A", fence_token=1, acquired=True)

        async def _raise_lease_lost():
            raise LeaseLostError("simultaneous-completion tie")

        heartbeat_task = asyncio.create_task(_raise_lease_lost())
        try:
            await heartbeat_task
        except LeaseLostError:
            pass
        assert heartbeat_task.done()
        sl._heartbeat_task = heartbeat_task

        async def _work_ok():
            return {"ok": True, "reason": "confirmed"}

        work_task = asyncio.create_task(_work_ok())
        await work_task
        assert work_task.done()

        real_wait = asyncio.wait

        async def _fake_wait(tasks, return_when):
            # `tasks` here is run_protected's OWN {self._heartbeat_task,
            # work_task} set -- work_task is a NEW Task object
            # (asyncio.ensure_future(coro), created inside run_protected),
            # not the pre-existing `work_task` variable in this test's own
            # scope. Return the real `tasks` argument, not a
            # reconstructed set with a stale reference. One `sleep(0)`
            # gives the newly-created task (which just awaits our
            # already-completed `work_task`) one scheduling tick to
            # actually finish before we report it as done.
            await asyncio.sleep(0)
            return set(tasks), set()

        drain_calls = []
        real_drain = fencing_mod._drain_heartbeat

        def _spy_drain(task):
            drain_calls.append(task)
            return real_drain(task)

        monkeypatch.setattr(asyncio, "wait", _fake_wait)
        monkeypatch.setattr(fencing_mod, "_drain_heartbeat", _spy_drain)

        async def _identity_coro():
            return await work_task

        result = await sl.run_protected(_identity_coro())
        monkeypatch.setattr(asyncio, "wait", real_wait)

        # (a) work result wins the tie, no raise.
        assert result == {"ok": True, "reason": "confirmed"}
        # (b) the heartbeat task's exception was actually retrieved by the
        # production code path (drained), proven via the spy on _drain_heartbeat.
        assert heartbeat_task in drain_calls
        # (c) retrieving it again afterward doesn't raise/warn.
        assert isinstance(heartbeat_task.exception(), LeaseLostError)

        # (d) mirror scenario for __aexit__: a lease whose heartbeat task is
        # already done (pre-settled with LeaseLostError) must be drained, not
        # cancelled (cancelling a done task is a no-op, but we assert it via
        # .cancelled() staying False -- Task.cancel() can't be spied
        # directly for the same immutable-type reason noted above).
        sl2 = SupervisedLease(resource, "task-A", ttl_ms=30_000, renew_interval_sec=3600, client=client)
        sl2.handle = LeaseHandle(resource=resource, task_id="task-A", fence_token=1, acquired=True)

        async def _raise_lease_lost2():
            raise LeaseLostError("mirror scenario")

        heartbeat_task2 = asyncio.create_task(_raise_lease_lost2())
        try:
            await heartbeat_task2
        except LeaseLostError:
            pass
        sl2._heartbeat_task = heartbeat_task2

        drain_calls.clear()
        await sl2.__aexit__(None, None, None)

        assert heartbeat_task2.cancelled() is False  # cancel() never attempted on an already-done task
        assert heartbeat_task2 in drain_calls
    finally:
        _cleanup(resource, client)


# ---------------------------------------------------------------------------
# 6-7: SupervisedLease release discipline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervised_lease_releases_on_normal_exit():
    client = get_redis_client()
    resource = _resource()
    _cleanup(resource, client)
    try:
        async with SupervisedLease(resource, "task-A", ttl_ms=30_000, renew_interval_sec=3600, client=client) as lease:
            assert lease.acquired is True
            result = await lease.run_protected(_ok_coro())
            assert result == {"ok": True}

        # Lease released -- a fresh acquire for the same resource succeeds immediately.
        fresh = acquire_heartbeat_lease(resource, "task-B", client=client)
        assert fresh.acquired is True
    finally:
        _cleanup(resource, client)


async def _ok_coro():
    return {"ok": True}


def test_release_lease_does_not_release_if_not_owner():
    client = get_redis_client()
    resource = _resource()
    _cleanup(resource, client)
    try:
        acquire_heartbeat_lease(resource, "task-A", client=client)
        # A different, stale task_id tries to release -- must be a no-op.
        release_lease(resource, "task-STALE", client=client)
        assert client.get(f"oma:lease:{resource}") == "task-A"
    finally:
        _cleanup(resource, client)


def test_drain_heartbeat_returns_none_for_cancelled_or_missing_task():
    assert _drain_heartbeat(None) is None


@pytest.mark.asyncio
async def test_drain_heartbeat_returns_none_for_cancelled_task():
    async def _never():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never())
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled()
    assert _drain_heartbeat(task) is None
