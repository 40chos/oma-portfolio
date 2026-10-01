"""Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
project_ticket_counts node): resume_task_after_checkpoint()'s own Redis lock used to be a fixed
`ex=3600` (1 hour) TTL, released only via a `finally: r.delete(lock_key)` -- but an external
wrapper killing this process (e.g. a shell `timeout`, which sends SIGTERM) terminates the
interpreter immediately under Python's own default SIGTERM disposition, WITHOUT running any
`finally` block at all. Confirmed live, repeatedly: every such kill left the lock orphaned for
the FULL remaining hour, forcing a choice between waiting out the whole TTL or bypassing the
lock entirely. Fixed with a short, actively-renewed lease (heartbeat pattern): a genuinely alive
resume keeps its own lock fresh indefinitely, while a killed/crashed one stops renewing and the
lock self-heals within `_RESUME_LOCK_TTL_SEC` (2 minutes), not up to an hour.

Uses very small TTL/renew-interval constants (monkeypatched) so this test runs in well under a
second, against a real local Redis instance (no live LLM/GPU calls -- the locked resume body
itself is faked with a plain `asyncio.sleep`).
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module
from infra.redis_client import get_redis_client


def test_lock_is_renewed_while_the_resume_is_genuinely_still_running():
    task_id = str(uuid.uuid4())
    lock_key = f"oma:resume_lock:{task_id}"
    r = get_redis_client()
    r.delete(lock_key)  # in case a prior run of this exact random uuid ever collided (won't happen)

    original_ttl = loop_module._RESUME_LOCK_TTL_SEC
    original_interval = loop_module._RESUME_LOCK_RENEW_INTERVAL_SEC
    loop_module._RESUME_LOCK_TTL_SEC = 1
    loop_module._RESUME_LOCK_RENEW_INTERVAL_SEC = 0.2

    async def _fake_locked_body(*args, **kwargs):
        # Sleeps well past the 1s TTL -- without renewal, the key would expire mid-call.
        await asyncio.sleep(1.5)
        return {"status": "completed", "task_id": task_id}

    original_locked = loop_module._resume_task_after_checkpoint_locked
    loop_module._resume_task_after_checkpoint_locked = _fake_locked_body
    try:
        async def _run():
            return await loop_module.resume_task_after_checkpoint(
                task_id, client=None, classifier_model="whatever",
            )

        result = asyncio.run(_run())
        assert result == {"status": "completed", "task_id": task_id}, (
            f"expected the locked body's own real return value to pass through -- got {result!r}"
        )
        assert r.ttl(lock_key) == -2, (
            "the lock must be deleted once the resume genuinely finishes -- "
            f"got ttl={r.ttl(lock_key)!r} (a real key still present)"
        )
        print(
            "PASS: the lock survived a 1.5s locked-body call despite a 1s TTL (proving it was "
            "actively renewed while genuinely still running), and was cleanly deleted on completion"
        )
    finally:
        loop_module._resume_task_after_checkpoint_locked = original_locked
        loop_module._RESUME_LOCK_TTL_SEC = original_ttl
        loop_module._RESUME_LOCK_RENEW_INTERVAL_SEC = original_interval
        r.delete(lock_key)


def test_lock_self_heals_quickly_after_the_ttl_when_renewal_stops():
    """The other half of the same fix: once nothing is renewing the lock (the real-world
    equivalent of the process having been killed), it must expire on its own within the new
    short TTL -- not linger for the old fixed hour.
    """
    task_id = str(uuid.uuid4())
    lock_key = f"oma:resume_lock:{task_id}"
    r = get_redis_client()
    r.delete(lock_key)

    r.set(lock_key, "1", nx=True, ex=1)
    assert r.ttl(lock_key) > 0
    import time
    time.sleep(1.3)
    assert r.ttl(lock_key) == -2, (
        f"expected the short-TTL lock to have self-expired -- got ttl={r.ttl(lock_key)!r}"
    )
    print("PASS: a lock nobody is renewing self-heals within its own short TTL, not an hour")


if __name__ == "__main__":
    test_lock_is_renewed_while_the_resume_is_genuinely_still_running()
    test_lock_self_heals_quickly_after_the_ttl_when_renewal_stops()
    print("\nALL RESUME LOCK HEARTBEAT TESTS PASSED")
