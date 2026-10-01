"""Phase 1, step 5: prove the fencing-token lock actually prevents the
race it's built for, against fake concurrent callers -- no real
specialist exists yet, but Phase 9's Build specialist will depend on
this primitive already being correct.
"""

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from infra.fencing import (
    acquire_db_install_lock,
    acquire_module_lock,
    acquire_module_lock_same_task_aware,
    check_fence,
    release_db_install_lock,
    release_module_lock,
)
from infra.redis_client import get_redis_client


def cleanup(module: str, client) -> None:
    for pattern in (f"oma:lock:{module}", f"oma:fence:{module}:*"):
        for key in client.scan_iter(match=pattern):
            client.delete(key)


def test_basic_acquire_and_check_fence_passes():
    client = get_redis_client()
    module = f"test.basic.{uuid.uuid4()}"
    cleanup(module, client)

    lock = acquire_module_lock(module, "task-A", ttl_ms=30_000, client=client)
    assert lock.acquired
    assert lock.fence_token == 1

    assert check_fence(module, lock.fence_token, client=client) is True
    cleanup(module, client)
    print("PASS: basic acquire + check_fence")


def test_second_caller_blocked_while_lock_held():
    client = get_redis_client()
    module = f"test.mutex.{uuid.uuid4()}"
    cleanup(module, client)

    lock_a = acquire_module_lock(module, "task-A", ttl_ms=30_000, client=client)
    assert lock_a.acquired

    lock_b = acquire_module_lock(module, "task-B", ttl_ms=30_000, client=client)
    assert not lock_b.acquired

    cleanup(module, client)
    print("PASS: second caller correctly blocked while first holds the lock")


def test_expired_lock_stale_writer_rejected_by_fence():
    """The exact race the technical document describes: caller A acquires
    the lock, it expires (crash, GC pause, whatever), caller B acquires a
    new lock and commits its write, and A's now-stale token must fail
    check_fence even though A never learned its lock had expired.
    """
    client = get_redis_client()
    module = f"test.race.{uuid.uuid4()}"
    cleanup(module, client)

    # A acquires with a very short TTL, standing in for "the lock expires
    # mid-write" -- A has no idea this happened.
    lock_a = acquire_module_lock(module, "task-A", ttl_ms=200, client=client)
    assert lock_a.acquired
    assert lock_a.fence_token == 1

    time.sleep(0.4)  # let A's lock actually expire

    # B acquires clean, gets a strictly higher fence token, and commits.
    lock_b = acquire_module_lock(module, "task-B", ttl_ms=30_000, client=client)
    assert lock_b.acquired
    assert lock_b.fence_token == 2

    b_commit_ok = check_fence(module, lock_b.fence_token, client=client)
    assert b_commit_ok is True, "B's fresh, higher token should be allowed to commit"

    # A, unaware its lock expired, now tries to commit its stale write.
    a_commit_ok = check_fence(module, lock_a.fence_token, client=client)
    assert a_commit_ok is False, (
        "A's stale token must be rejected -- this is the entire point of "
        "fencing tokens, since the lock alone can't prevent this race"
    )

    cleanup(module, client)
    print("PASS: expired lock's stale writer correctly rejected by check_fence")


def test_release_only_by_current_owner():
    client = get_redis_client()
    module = f"test.release.{uuid.uuid4()}"
    cleanup(module, client)

    lock_a = acquire_module_lock(module, "task-A", ttl_ms=200, client=client)
    assert lock_a.acquired
    time.sleep(0.4)
    lock_b = acquire_module_lock(module, "task-B", ttl_ms=30_000, client=client)
    assert lock_b.acquired

    # A tries to release what it thinks is still its lock -- must be a
    # no-op, since B now legitimately owns it.
    release_module_lock(module, "task-A", client=client)
    still_locked_for_b = client.get(f"oma:lock:{module}")
    assert still_locked_for_b == "task-B", "A's stale release must not evict B's live lock"

    cleanup(module, client)
    print("PASS: stale release correctly refused to evict the current owner's lock")


def test_same_task_aware_still_rejects_a_genuinely_different_task_immediately():
    """Phase 31 §6/Phase B (2026-08-08): the cross-task protection must be exactly as strict as
    plain acquire_module_lock() -- a real, different task holding the lock is rejected right
    away, never waited on.
    """
    client = get_redis_client()
    module = f"test.same_task_cross_reject.{uuid.uuid4()}"
    cleanup(module, client)

    lock_a = acquire_module_lock(module, "task-A", ttl_ms=30_000, client=client)
    assert lock_a.acquired

    async def run():
        started = time.monotonic()
        result = await acquire_module_lock_same_task_aware(
            module, "task-B", max_wait_sec=1.0, poll_interval_sec=0.1, client=client,
        )
        return result, time.monotonic() - started

    handle, elapsed = asyncio.run(run())
    assert handle.acquired is False, "a genuinely different task must still be rejected"
    assert elapsed < 0.3, (
        f"a cross-task rejection must be immediate, never waited on the full max_wait_sec: took {elapsed:.2f}s"
    )
    cleanup(module, client)
    print(f"PASS: a genuinely different task is rejected immediately ({elapsed:.3f}s), never waited on")


def test_same_task_aware_waits_for_its_own_sibling_then_acquires():
    """The real fix itself: a SAME-task_id collision -- two sibling nodes of one decomposed task
    -- waits for the sibling to release, then genuinely acquires, instead of hard-failing.
    """
    client = get_redis_client()
    module = f"test.same_task_wait.{uuid.uuid4()}"
    cleanup(module, client)
    task_id = "task-shared"

    lock_first = acquire_module_lock(module, task_id, ttl_ms=30_000, client=client)
    assert lock_first.acquired

    async def release_after_delay():
        await asyncio.sleep(0.3)
        release_module_lock(module, task_id, client=client)

    async def run():
        started = time.monotonic()
        release_task = asyncio.create_task(release_after_delay())
        result = await acquire_module_lock_same_task_aware(
            module, task_id, max_wait_sec=5.0, poll_interval_sec=0.05, client=client,
        )
        await release_task
        return result, time.monotonic() - started

    handle, elapsed = asyncio.run(run())
    assert handle.acquired is True, "the same task's own sibling must eventually acquire once the lock frees up, never hard-fail"
    assert elapsed >= 0.25, f"must have genuinely waited for the sibling's release, not returned instantly: {elapsed:.2f}s"
    cleanup(module, client)
    print(f"PASS: a same-task collision waits for its own sibling to release ({elapsed:.2f}s), then genuinely acquires")


def test_same_task_aware_gives_up_after_max_wait_if_sibling_never_releases():
    client = get_redis_client()
    module = f"test.same_task_timeout.{uuid.uuid4()}"
    cleanup(module, client)
    task_id = "task-stuck"

    lock_first = acquire_module_lock(module, task_id, ttl_ms=30_000, client=client)
    assert lock_first.acquired

    async def run():
        started = time.monotonic()
        result = await acquire_module_lock_same_task_aware(
            module, task_id, max_wait_sec=0.3, poll_interval_sec=0.05, client=client,
        )
        return result, time.monotonic() - started

    handle, elapsed = asyncio.run(run())
    assert handle.acquired is False, "a sibling that never releases must eventually give up, never hang forever"
    assert elapsed >= 0.25, f"must have genuinely waited close to max_wait_sec before giving up: {elapsed:.2f}s"
    cleanup(module, client)
    print(f"PASS: a same-task collision gives up after max_wait_sec ({elapsed:.2f}s) if the sibling never releases, never hangs forever")


def test_same_task_aware_never_blocks_the_event_loop_while_waiting():
    """Real, important safety property: the wait must be a genuine `await asyncio.sleep`, not a
    blocking `time.sleep` -- otherwise it would freeze every OTHER concurrent node/task sharing
    this same process's event loop, defeating the entire point of node-level concurrency.
    """
    client = get_redis_client()
    module = f"test.same_task_non_blocking.{uuid.uuid4()}"
    cleanup(module, client)
    task_id = "task-shared-nb"

    lock_first = acquire_module_lock(module, task_id, ttl_ms=30_000, client=client)
    assert lock_first.acquired

    other_task_ticks = {"n": 0}

    async def other_concurrent_work():
        for _ in range(10):
            await asyncio.sleep(0.05)
            other_task_ticks["n"] += 1

    async def release_after_delay():
        await asyncio.sleep(0.4)
        release_module_lock(module, task_id, client=client)

    async def run():
        return await asyncio.gather(
            acquire_module_lock_same_task_aware(module, task_id, max_wait_sec=2.0, poll_interval_sec=0.05, client=client),
            other_concurrent_work(),
            release_after_delay(),
        )

    asyncio.run(run())
    assert other_task_ticks["n"] >= 6, (
        f"a genuinely concurrent, unrelated coroutine must keep making real progress while this "
        f"one waits -- got only {other_task_ticks['n']} ticks, suggesting the wait blocked the event loop"
    )
    cleanup(module, client)
    print(f"PASS: waiting for a same-task sibling does not block the event loop -- {other_task_ticks['n']} ticks of unrelated concurrent work still ran")


# --- acquire_db_install_lock / release_db_install_lock (2026-08-09) --------------------------

def test_db_install_lock_basic_acquire_and_release():
    client = get_redis_client()
    db = f"test.db.{uuid.uuid4()}"
    handle = acquire_db_install_lock(db, "task-A", client=client)
    assert handle.acquired
    release_db_install_lock(db, "task-A", client=client)
    # A fresh caller can acquire immediately once released -- proves release genuinely worked.
    handle2 = acquire_db_install_lock(db, "task-B", max_wait_sec=0.5, poll_interval_sec=0.05, client=client)
    assert handle2.acquired
    release_db_install_lock(db, "task-B", client=client)
    print("PASS: basic db-install-lock acquire + release round-trip")


def test_db_install_lock_uses_a_separate_namespace_from_the_module_lock():
    """Real, load-bearing property this whole mechanism depends on: a db-install lock for db `X`
    must never collide with (or be confused with) a real per-module lock for a module also named
    `X` -- two structurally different real resources, deliberately kept in separate Redis key
    namespaces (see acquire_db_install_lock()'s own docstring).
    """
    client = get_redis_client()
    shared_name = f"test.shared.{uuid.uuid4()}"
    module_lock = acquire_module_lock(shared_name, "task-module", ttl_ms=30_000, client=client)
    assert module_lock.acquired
    # The db-install lock for the SAME literal string must still be free -- different namespace.
    db_lock = acquire_db_install_lock(shared_name, "task-db", max_wait_sec=0.5, poll_interval_sec=0.05, client=client)
    assert db_lock.acquired, "a db-install lock must never be blocked by an unrelated per-module lock of the same name"
    release_module_lock(shared_name, "task-module", client=client)
    release_db_install_lock(shared_name, "task-db", client=client)
    print("PASS: db-install locks and per-module locks never collide, even when named identically")


def test_db_install_lock_second_caller_waits_then_acquires_after_release():
    client = get_redis_client()
    db = f"test.db.wait.{uuid.uuid4()}"
    first = acquire_db_install_lock(db, "task-first", ttl_ms=30_000, client=client)
    assert first.acquired

    import threading

    result = {}

    def release_after_delay():
        time.sleep(0.3)
        release_db_install_lock(db, "task-first", client=client)

    t = threading.Thread(target=release_after_delay)
    t.start()
    t0 = time.monotonic()
    second = acquire_db_install_lock(db, "task-second", max_wait_sec=2.0, poll_interval_sec=0.05, client=client)
    elapsed = time.monotonic() - t0
    t.join()

    assert second.acquired, "the second caller must acquire once the first genuinely releases, not time out"
    assert elapsed >= 0.25, f"must have genuinely waited for the release, not raced past it: {elapsed:.2f}s"
    release_db_install_lock(db, "task-second", client=client)
    print(f"PASS: a second caller targeting the same db waits ({elapsed:.2f}s) then acquires once the first releases")


def test_db_install_lock_gives_up_after_max_wait_if_never_released():
    client = get_redis_client()
    db = f"test.db.stuck.{uuid.uuid4()}"
    first = acquire_db_install_lock(db, "task-stuck-holder", ttl_ms=30_000, client=client)
    assert first.acquired

    t0 = time.monotonic()
    second = acquire_db_install_lock(db, "task-stuck-waiter", max_wait_sec=0.3, poll_interval_sec=0.05, client=client)
    elapsed = time.monotonic() - t0

    assert second.acquired is False, "must give up (never hang forever) if the holder never releases within max_wait_sec"
    assert elapsed >= 0.25, f"must have genuinely waited close to max_wait_sec before giving up: {elapsed:.2f}s"
    release_db_install_lock(db, "task-stuck-holder", client=client)
    print(f"PASS: a db-install lock wait gives up after max_wait_sec ({elapsed:.2f}s) if never released, never hangs forever")


if __name__ == "__main__":
    test_basic_acquire_and_check_fence_passes()
    test_second_caller_blocked_while_lock_held()
    test_expired_lock_stale_writer_rejected_by_fence()
    test_release_only_by_current_owner()
    test_same_task_aware_still_rejects_a_genuinely_different_task_immediately()
    test_same_task_aware_waits_for_its_own_sibling_then_acquires()
    test_same_task_aware_gives_up_after_max_wait_if_sibling_never_releases()
    test_same_task_aware_never_blocks_the_event_loop_while_waiting()
    test_db_install_lock_basic_acquire_and_release()
    test_db_install_lock_uses_a_separate_namespace_from_the_module_lock()
    test_db_install_lock_second_caller_waits_then_acquires_after_release()
    test_db_install_lock_gives_up_after_max_wait_if_never_released()
    print("\nALL FENCING TESTS PASSED")
