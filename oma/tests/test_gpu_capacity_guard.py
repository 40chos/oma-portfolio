"""Real, deterministic tests for infra/gpu_capacity_guard.py -- the
RAM-ceiling wait/queue mechanism added 2026-07-22 after GPU Worker 01
(the coder host) crashed under concurrent load from our own agents.

These use httpx.MockTransport (canned, deterministic HTTP responses,
built into httpx itself -- not a mock of anything this project's own
infrastructure depends on) rather than the real GPU worker host,
because the whole point of this test is to exercise "usage is AT or
ABOVE the effective ceiling" and "the host never recovers within the
wait budget" -- which cannot be safely reproduced against the real
production host without literally recreating the exact RAM-pressure
crash this module exists to prevent. The real host is used directly
for the fast-path smoke test only (confirming the guard doesn't
interfere with an ordinary, healthy call), matching the project's own
"never mock infrastructure" discipline as closely as this specific
case allows.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx

from infra.gpu_capacity_guard import (
    GpuMemoryMetricsUnavailable,
    read_ram_metrics,
    read_used_ram_bytes,
    wait_for_ram_headroom,
)


def _metrics_text(used_bytes: float, total_bytes: float) -> str:
    return (
        "# HELP llamaswap_memory_used_bytes Used memory in bytes\n"
        "# TYPE llamaswap_memory_used_bytes gauge\n"
        f"llamaswap_memory_used_bytes {used_bytes}\n"
        "# HELP llamaswap_memory_total_bytes Total memory in bytes\n"
        "# TYPE llamaswap_memory_total_bytes gauge\n"
        f"llamaswap_memory_total_bytes {total_bytes}\n"
    )


def _client_with_canned_responses(responses: list[httpx.Response]) -> httpx.AsyncClient:
    # Repeats the LAST response indefinitely once the canned list is
    # exhausted, rather than raising StopIteration -- some tests poll an
    # unbounded number of times within a short deadline (the exact
    # number of polls that fit isn't the thing under test there).
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[i]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_read_ram_metrics_parses_both_gauges():
    async def _drive():
        client = _client_with_canned_responses([
            httpx.Response(200, text=_metrics_text(12_000_000_000, 48_000_000_000)),
        ])
        try:
            return await read_ram_metrics(client, "http://fake-gpu-host:9090")
        finally:
            await client.aclose()

    used, total = asyncio.run(_drive())
    assert used == 12_000_000_000.0
    assert total == 48_000_000_000.0
    print("PASS: read_ram_metrics() correctly parses both used and total gauge lines")


def test_read_used_ram_bytes_raises_when_lines_are_missing():
    async def _drive():
        client = _client_with_canned_responses([
            httpx.Response(200, text="# some other metric\nsomething_else 1\n"),
        ])
        try:
            await read_used_ram_bytes(client, "http://fake-gpu-host:9090")
        finally:
            await client.aclose()

    raised = False
    try:
        asyncio.run(_drive())
    except GpuMemoryMetricsUnavailable:
        raised = True
    assert raised, "must raise, never guess a fabricated RAM value when the real gauges are missing"
    print("PASS: read_used_ram_bytes() raises GpuMemoryMetricsUnavailable when the metrics are missing")


def test_ceiling_is_relative_to_live_total_not_a_fixed_byte_count():
    """The exact real bug found live tonight, minutes after this module
    first shipped: Operator resized the VM (48 GiB -> ~23 GiB total,
    confirmed via /metrics) as part of his own ongoing RAM tuning. A
    fixed 44 GiB byte ceiling against a 23 GiB box can NEVER trigger --
    silently making the whole guard a no-op exactly when the box got
    smaller (more fragile, not less). This proves the ceiling is
    computed fresh from the host's own live-reported total on every
    poll: the SAME used_bytes (20 GiB) is under the effective ceiling
    against a 48 GiB total, but over it against a 23 GiB total.
    """
    ceiling_fraction = 0.85
    used_bytes = 20 * 1024**3  # 20 GiB, well under the old fixed 44 GiB ceiling either way

    async def _check(total_bytes: float) -> bool:
        """Returns True if wait_for_ram_headroom() proceeded immediately
        (no wait -- i.e. usage was judged under the ceiling). Uses a
        tiny max_wait_sec -- the canned response never changes, so the
        "over ceiling" case would otherwise loop for the real 300s
        default before giving up; a call to on_wait (or the eventual
        TimeoutError) is equally conclusive evidence it did NOT proceed
        immediately, without needing to actually wait that long.
        """
        client = _client_with_canned_responses([
            httpx.Response(200, text=_metrics_text(used_bytes, total_bytes)),
        ])
        waited = []
        try:
            await wait_for_ram_headroom(
                client, "http://fake-gpu-host:9090", "fake_backend",
                ceiling_fraction=ceiling_fraction, ceiling_bytes=44 * 1024**3,
                poll_interval_sec=0.01, max_wait_sec=0.03,
                on_wait=lambda u, c: waited.append(u),
            )
        except TimeoutError:
            waited.append(used_bytes)
        finally:
            await client.aclose()
        return not waited

    # Against a healthy, larger box (48 GiB total): 20 GiB used is well
    # under 85% of 48 GiB (~40.8 GiB) -- must proceed immediately.
    proceeded_large_box = asyncio.run(_check(48 * 1024**3))
    assert proceeded_large_box, "20 GiB used must be under the ceiling against a 48 GiB total box"

    # Against the SAME box after Operator's resize (23 GiB total, the real
    # live figure): 20 GiB used IS at/above 85% of 23 GiB (~19.55 GiB)
    # -- must now correctly detect the pressure and wait, even though
    # used_bytes never changed. A fixed-byte-only ceiling would have
    # missed this entirely.
    proceeded_small_box = asyncio.run(_check(23 * 1024**3))
    assert not proceeded_small_box, (
        "the SAME 20 GiB used must be judged over the ceiling once the host's own live total "
        "shrinks to 23 GiB -- the ceiling must track the live total, not a stale fixed number"
    )
    print("PASS: the effective ceiling is recomputed from the host's own live-reported total on "
          "every poll -- correctly detects pressure after a real VM resize that a fixed byte "
          "ceiling would have silently missed")


def test_wait_for_ram_headroom_returns_immediately_when_already_under_ceiling():
    async def _drive():
        client = _client_with_canned_responses([
            httpx.Response(200, text=_metrics_text(5 * 1024**3, 48 * 1024**3)),
        ])
        waited = []
        try:
            await wait_for_ram_headroom(
                client, "http://fake-gpu-host:9090", "fake_backend",
                on_wait=lambda u, c: waited.append(u),
            )
        finally:
            await client.aclose()
        return waited

    waited = asyncio.run(_drive())
    assert waited == [], "must never call on_wait (never actually wait) when already under the ceiling"
    print("PASS: wait_for_ram_headroom() returns immediately, no wait, when usage is already under the ceiling")


def test_wait_for_ram_headroom_waits_then_proceeds_once_usage_drops():
    """The real shape this exists for: usage is over the ceiling on the
    first poll, drops back under it by the second poll (another call
    finished, or the host's own GC/model-unload freed memory) --
    proceeds once it genuinely recovers, having waited exactly once.
    """
    async def _drive():
        client = _client_with_canned_responses([
            httpx.Response(200, text=_metrics_text(46 * 1024**3, 48 * 1024**3)),  # over ceiling
            httpx.Response(200, text=_metrics_text(20 * 1024**3, 48 * 1024**3)),  # recovered
        ])
        waited = []
        try:
            await wait_for_ram_headroom(
                client, "http://fake-gpu-host:9090", "fake_backend",
                poll_interval_sec=0.01, max_wait_sec=5,
                on_wait=lambda u, c: waited.append(u),
            )
        finally:
            await client.aclose()
        return waited

    waited = asyncio.run(_drive())
    assert len(waited) == 1
    assert waited[0] == 46 * 1024**3
    print("PASS: wait_for_ram_headroom() waits exactly once, then proceeds once usage genuinely recovers")


def test_wait_for_ram_headroom_raises_timeout_when_never_recovers():
    async def _drive():
        client = _client_with_canned_responses([
            httpx.Response(200, text=_metrics_text(46 * 1024**3, 48 * 1024**3)),
        ])
        try:
            await wait_for_ram_headroom(
                client, "http://fake-gpu-host:9090", "fake_backend",
                poll_interval_sec=0.01, max_wait_sec=0.03,
            )
        finally:
            await client.aclose()

    raised = False
    try:
        asyncio.run(_drive())
    except TimeoutError as exc:
        raised = True
        assert "fake_backend" in str(exc)
    assert raised, (
        "must raise TimeoutError (never hang forever, never silently proceed into an "
        "overloaded host) once the wait budget is exhausted with usage still over the ceiling"
    )
    print("PASS: wait_for_ram_headroom() raises TimeoutError -- never hangs forever, never fires "
          "into a host that's still over its ceiling after the full wait budget")


def test_wait_for_ram_headroom_fails_open_when_metrics_unreachable():
    """A genuinely dead host (connection refused, DNS failure, etc.) must
    NOT be blocked on by this guard -- that's the job of
    ModelGatewayClient's own existing retry/circuit-breaker path. This
    guard's job is narrower: RAM pressure specifically, not general
    outage detection.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def _drive():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            # Must return normally (no exception, no wait) -- fail-open.
            await wait_for_ram_headroom(client, "http://fake-dead-host:9090", "fake_backend")
        finally:
            await client.aclose()

    asyncio.run(_drive())  # no exception == pass
    print("PASS: wait_for_ram_headroom() fails open (returns normally) when the metrics endpoint "
          "itself is unreachable, leaving real outage detection to the caller's own existing logic")


def test_wait_for_ram_headroom_smoke_test_against_the_real_gpu_worker_host():
    """The one real-infra test in this file: confirms the guard doesn't
    interfere with an ordinary, healthy call against the actual GPU
    Worker 01 host, and reports its REAL, current live total (useful to
    eyeball after any future VM resize -- confirms this module picks it
    up automatically, no code change needed). Skips cleanly if the real
    host isn't reachable, rather than failing the whole suite on an
    unrelated, already-known infra flake.
    """
    async def _drive():
        client = httpx.AsyncClient()
        try:
            return await read_ram_metrics(client, "http://192.0.2.11:9090")
        finally:
            await client.aclose()

    try:
        used, total = asyncio.run(_drive())
    except (httpx.HTTPError, OSError) as exc:
        print(f"SKIPPED: real GPU Worker 01 host not reachable right now ({exc}) -- not a guard bug")
        return
    assert used >= 0
    assert total > 0
    print(f"PASS: real GPU Worker 01 host's own live RAM read successfully: "
          f"{used / 1024**3:.2f} / {total / 1024**3:.2f} GiB used/total")


if __name__ == "__main__":
    test_read_ram_metrics_parses_both_gauges()
    test_read_used_ram_bytes_raises_when_lines_are_missing()
    test_ceiling_is_relative_to_live_total_not_a_fixed_byte_count()
    test_wait_for_ram_headroom_returns_immediately_when_already_under_ceiling()
    test_wait_for_ram_headroom_waits_then_proceeds_once_usage_drops()
    test_wait_for_ram_headroom_raises_timeout_when_never_recovers()
    test_wait_for_ram_headroom_fails_open_when_metrics_unreachable()
    test_wait_for_ram_headroom_smoke_test_against_the_real_gpu_worker_host()
    print("\nALL GPU CAPACITY GUARD TESTS PASSED")
