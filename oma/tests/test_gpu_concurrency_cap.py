"""P13 item 10(e) (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): tests for infra/gpu_capacity_guard.py's new MAX_CONCURRENT_BUILD_ROUNDS constant and
build_concurrency_semaphore() helper. Pure, mostly synchronous -- the one async test drives a real
asyncio.Semaphore with no network/LLM/GPU calls involved.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.gpu_capacity_guard import MAX_CONCURRENT_BUILD_ROUNDS, build_concurrency_semaphore


def test_default_cap_is_a_small_positive_integer():
    assert isinstance(MAX_CONCURRENT_BUILD_ROUNDS, int)
    assert 1 <= MAX_CONCURRENT_BUILD_ROUNDS <= 4, (
        "the recommended cap is 2 -- this should stay small, never accidentally large"
    )
    print(f"PASS: MAX_CONCURRENT_BUILD_ROUNDS is {MAX_CONCURRENT_BUILD_ROUNDS}, a small positive integer")


def test_zero_or_negative_max_concurrent_raises():
    try:
        build_concurrency_semaphore(0)
        assert False, "max_concurrent=0 must raise, never silently create a permanently-locked semaphore"
    except ValueError:
        pass
    try:
        build_concurrency_semaphore(-1)
        assert False, "a negative max_concurrent must raise"
    except ValueError:
        pass
    print("PASS: a non-positive max_concurrent raises ValueError instead of a silently-deadlocked semaphore")


def test_semaphore_genuinely_bounds_concurrency():
    async def run():
        sem = build_concurrency_semaphore(max_concurrent=2)
        concurrent_now = 0
        max_seen = 0

        async def worker():
            nonlocal concurrent_now, max_seen
            async with sem:
                concurrent_now += 1
                max_seen = max(max_seen, concurrent_now)
                await asyncio.sleep(0.05)
                concurrent_now -= 1

        await asyncio.gather(*(worker() for _ in range(6)))
        return max_seen

    max_seen = asyncio.run(run())
    assert max_seen <= 2, f"semaphore must never allow more than 2 concurrent holders, saw {max_seen}"
    print(f"PASS: 6 concurrent workers against a cap of 2 never exceeded {max_seen} simultaneous holders")


if __name__ == "__main__":
    test_default_cap_is_a_small_positive_integer()
    test_zero_or_negative_max_concurrent_raises()
    test_semaphore_genuinely_bounds_concurrency()
    print("\nALL GPU CONCURRENCY CAP TESTS PASSED")
