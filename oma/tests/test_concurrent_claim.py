"""Phase 35 §17.3.2 / §18.1: tests for manager/concurrent_claim.py -- the task-level admission-
control claim system. Runs against the real dev Redis instance (same pattern as
tests/test_fencing.py), using unique UUID-namespaced targets with explicit cleanup, never a mock.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.redis_client import get_redis_client
from manager.concurrent_claim import acquire_task_claim, release_task_claim


def cleanup(*targets, client):
    for target in targets:
        for pattern in (f"oma:lock:taskclaim:{target}", f"oma:fence:taskclaim:{target}:*"):
            for key in client.scan_iter(match=pattern):
                client.delete(key)


def test_single_target_acquire_and_release():
    client = get_redis_client()
    target = f"crm.lead.{uuid.uuid4()}"
    cleanup(target, client=client)

    result = acquire_task_claim([target], "task-A", ttl_ms=10_000, client=client)
    assert result.acquired is True
    assert result.claimed_targets == [target]

    release_task_claim([target], "task-A", client=client)
    # A different task can now claim the same target.
    result2 = acquire_task_claim([target], "task-B", ttl_ms=10_000, client=client)
    assert result2.acquired is True
    release_task_claim([target], "task-B", client=client)


def test_second_task_collides_on_the_same_target():
    client = get_redis_client()
    target = f"crm.lead.{uuid.uuid4()}"
    cleanup(target, client=client)

    first = acquire_task_claim([target], "task-A", ttl_ms=10_000, client=client)
    assert first.acquired is True

    second = acquire_task_claim([target], "task-B", ttl_ms=10_000, client=client)
    assert second.acquired is False
    assert second.colliding_target == target
    assert second.colliding_task_id == "task-A"

    release_task_claim([target], "task-A", client=client)


def test_all_or_nothing_releases_partial_claim_on_collision():
    client = get_redis_client()
    target_a = f"crm.lead.{uuid.uuid4()}"
    target_b = f"res.partner.{uuid.uuid4()}"
    cleanup(target_a, target_b, client=client)

    # task-X already holds target_b.
    pre = acquire_task_claim([target_b], "task-X", ttl_ms=10_000, client=client)
    assert pre.acquired is True

    # task-Y wants BOTH target_a (free) and target_b (taken) -- must get neither.
    result = acquire_task_claim([target_a, target_b], "task-Y", ttl_ms=10_000, client=client)
    assert result.acquired is False

    # target_a must have been released back, not left dangling under task-Y.
    retry = acquire_task_claim([target_a], "task-Z", ttl_ms=10_000, client=client)
    assert retry.acquired is True

    release_task_claim([target_a, target_b], "task-Z", client=client)
    release_task_claim([target_a, target_b], "task-X", client=client)


def test_release_only_by_current_owner_does_not_release_a_newer_claim():
    client = get_redis_client()
    target = f"crm.lead.{uuid.uuid4()}"
    cleanup(target, client=client)

    acquire_task_claim([target], "task-A", ttl_ms=1_000, client=client)
    import time
    time.sleep(1.2)  # let task-A's claim expire
    second = acquire_task_claim([target], "task-B", ttl_ms=10_000, client=client)
    assert second.acquired is True

    # task-A's stale release must not release task-B's real, current claim.
    release_task_claim([target], "task-A", client=client)
    third = acquire_task_claim([target], "task-C", ttl_ms=10_000, client=client)
    assert third.acquired is False  # still held by task-B

    release_task_claim([target], "task-B", client=client)


def test_empty_target_list_is_trivially_acquired():
    client = get_redis_client()
    result = acquire_task_claim([], "task-A", client=client)
    assert result.acquired is True
    assert result.claimed_targets == []


def test_claim_uses_a_distinct_namespace_from_the_real_module_write_lock():
    client = get_redis_client()
    target = f"crm.lead.{uuid.uuid4()}"
    cleanup(target, client=client)

    from infra.fencing import acquire_module_lock, release_module_lock

    # A real module-write lock on the SAME name must not collide with a task claim on it --
    # they must live in genuinely separate Redis key spaces.
    module_lock = acquire_module_lock(target, "build-task", ttl_ms=10_000, client=client)
    assert module_lock.acquired is True

    claim = acquire_task_claim([target], "claim-task", ttl_ms=10_000, client=client)
    assert claim.acquired is True

    release_module_lock(target, "build-task", client=client)
    release_task_claim([target], "claim-task", client=client)
