"""RAM-capacity guard for the two resident GPU worker hosts.

Real, concrete follow-up to the 2026-07-22 GPU Worker 01 (coder,
internal GPU host) outage: Operator's own diagnosis was that concurrent load from
our own agents pushed the host's system RAM past what was available
(the GPU model itself plus other services left less headroom than
expected), crashing the whole host -- mid demo-prep, for 30+ minutes.
Operator's own infra-level fix (a swap disk, a host-level block above what
was then a 44GB VM, capping every other VM's RAM so the box's own
total could never exceed 64GB) is the primary defense. This module is
the SECOND, cooperative layer, on our own side: never let OUR OWN
calls be the ones that push a resident host over its ceiling in the
first place.

Real, confirmed bug found live the SAME night, minutes after this
module first shipped: the first version used a fixed absolute ceiling
(44 GiB, matching the VM's size AT THAT MOMENT). Operator then resized the
VM again as part of his own ongoing tuning -- confirmed live via
/metrics, `llamaswap_memory_total_bytes` dropped to ~23 GiB shortly
after. A fixed 44 GiB ceiling against a 23 GiB box can never trigger --
usage can never exceed a ceiling that's above the box's own total
capacity, silently making the whole guard a no-op exactly when the box
got SMALLER (more fragile, not less). Fixed: the ceiling is now
computed as a FRACTION of the host's own live-reported total
(`llamaswap_memory_total_bytes`), not a fixed byte count -- it
self-adapts to any future resize with zero code/config change, the
same way any real admission-control system would. An absolute byte
ceiling is still accepted as an extra, independent hard cap (defense
in depth if the fraction-based number would ever be too permissive),
but the fraction is the primary, live-adapting signal.

Researched (July 2026): vLLM, the actual inference engine behind
llama-swap on both hosts, has no built-in admission control or queueing
for HOST system-memory pressure -- it manages its own GPU KV-cache
allocation internally, but has no concept of the surrounding VM's RAM
ceiling at all. This genuinely has to be built at the caller level;
there is no existing tool that already does it for us.

Mechanism: llama-swap already exposes a live Prometheus-format
/metrics endpoint (confirmed live, no new infra needed) including
`llamaswap_memory_used_bytes` and `llamaswap_memory_total_bytes`.
Before ModelGatewayClient dispatches any call, it polls this and WAITS
(never fires blindly) if usage is at or above the effective ceiling,
giving the host a chance to recover (model unload, GC, another call
finishing) before adding more load. Fails OPEN if the metrics endpoint
itself can't be reached -- a truly dead host is already caught by
ModelGatewayClient's own existing retry/circuit-breaker path
(infra/gateway_client.py), so this guard's own job stays narrow: catch
RAM PRESSURE specifically, before it becomes a crash, not duplicate
outage detection that already exists.
"""

from __future__ import annotations

import asyncio
import time

import httpx

# Primary signal: a fraction of the host's own LIVE-reported total --
# self-adapts to any VM resize, unlike a fixed byte count.
DEFAULT_RAM_CEILING_FRACTION = 0.85
# Secondary, independent hard cap (defense in depth) -- whichever of
# the two effective ceilings is LOWER wins, so this never silently
# permits more than this absolute amount even if the fraction-based
# number would allow it (e.g. a much larger host in the future).
DEFAULT_RAM_CEILING_BYTES = 44 * 1024**3
DEFAULT_POLL_INTERVAL_SEC = 10.0
DEFAULT_MAX_WAIT_SEC = 300.0  # 5 minutes -- beyond this, treat it like a real outage, not a queue

# P13 item 10(e) (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
# §22.2): the one genuinely new piece of GPU-concurrency governance concurrent Build dispatch
# (item 10(b)) will need -- an explicit ceiling on how many simultaneous Build rounds may run at
# once, rather than relying on wait_for_ram_headroom() alone to throttle purely via blocking.
# `wait_for_ram_headroom()`'s own RAM-ceiling protection already, automatically applies to any
# future concurrent Build calls (every model call already routes through it) -- this constant and
# helper are additive, narrowly scoped to the cap itself, not a restatement of that protection.
#
# Deliberately shipped as a standalone, real, tested primitive with NO current caller: item 10(b)
# (concurrent generation of independent sub-constraints) is the actual consumer, and is itself not
# yet built here -- see the P13 tracker doc for why (git-worktree isolation against OMA's real
# scaffold/lint/install toolchain is explicitly unverified, item 19, and building concurrent
# dispatch on top of an unverified isolation mechanism would be exactly the kind of untested,
# overclaiming work this project's own discipline avoids). Matches the same incremental-build
# precedent as P12 item 27's tools_odoo/committed_symbols.py and P13 item 4's
# contracts/constraint_graph.py -- built and unit-tested standalone, wired in once its real
# consumer exists.
MAX_CONCURRENT_BUILD_ROUNDS = 2


def build_concurrency_semaphore(max_concurrent: int = MAX_CONCURRENT_BUILD_ROUNDS) -> asyncio.Semaphore:
    """A real asyncio.Semaphore bounding how many concurrent Build rounds may run at once.
    `max_concurrent` must be a positive integer -- a semaphore of 0 would deadlock every caller
    forever, which is never the intended behavior of a concurrency CAP (a cap of "don't run
    concurrently at all" is expressed by not dispatching concurrently in the first place, not by
    a semaphore that can never be acquired).
    """
    if max_concurrent < 1:
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
    return asyncio.Semaphore(max_concurrent)


class GpuMemoryMetricsUnavailable(RuntimeError):
    """Raised internally when /metrics can't be read at all, or doesn't
    contain the expected gauges. Callers treat this as "skip the check,
    proceed to the real call" -- see the module docstring's fail-open
    rationale.
    """


def _parse_metric(text: str, metric_name: str) -> float:
    for line in text.splitlines():
        if line.startswith(metric_name):
            try:
                return float(line.rsplit(" ", 1)[-1])
            except ValueError:
                break
    raise GpuMemoryMetricsUnavailable(f"no parseable {metric_name!r} line in /metrics")


async def read_ram_metrics(http_client: httpx.AsyncClient, base_url: str) -> tuple[float, float]:
    """Reads llama-swap's own live `llamaswap_memory_used_bytes` and
    `llamaswap_memory_total_bytes` gauges from its Prometheus /metrics
    endpoint. Returns (used_bytes, total_bytes). Raises
    GpuMemoryMetricsUnavailable if the endpoint is unreachable or either
    expected line is missing -- never guesses a fabricated number.
    """
    resp = await http_client.get(f"{base_url}/metrics", timeout=httpx.Timeout(5.0))
    resp.raise_for_status()
    text = resp.text
    return _parse_metric(text, "llamaswap_memory_used_bytes"), _parse_metric(text, "llamaswap_memory_total_bytes")


async def read_used_ram_bytes(http_client: httpx.AsyncClient, base_url: str) -> float:
    """Used-bytes only, for callers (and existing tests) that don't need
    the total. Prefer read_ram_metrics() for anything computing a
    ceiling -- see the module docstring for why a fixed byte ceiling on
    its own is fragile.
    """
    used, _total = await read_ram_metrics(http_client, base_url)
    return used


def _effective_ceiling(total_bytes: float, ceiling_fraction: float, ceiling_bytes: float) -> float:
    return min(total_bytes * ceiling_fraction, ceiling_bytes)


async def wait_for_ram_headroom(
    http_client: httpx.AsyncClient,
    base_url: str,
    backend_name: str,
    ceiling_fraction: float = DEFAULT_RAM_CEILING_FRACTION,
    ceiling_bytes: float = DEFAULT_RAM_CEILING_BYTES,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
    on_wait: "callable | None" = None,
) -> None:
    """Blocks (async, non-busy -- real asyncio.sleep between polls, never
    burns CPU) until `base_url`'s own live RAM usage is below the
    EFFECTIVE ceiling -- min(live_total_bytes * ceiling_fraction,
    ceiling_bytes), recomputed fresh on every poll from the host's own
    current /metrics, so a VM resize is picked up automatically, never
    requiring a code or config change. Polls every `poll_interval_sec`,
    for up to `max_wait_sec` total.

    Returns immediately (no wait at all) if usage is already under the
    effective ceiling on the first check, or if the metrics endpoint
    can't be read at all (fail-open).

    Raises TimeoutError if the ceiling is STILL exceeded after
    max_wait_sec -- the caller (ModelGatewayClient) turns this into the
    same GatewayUnavailableError/pause path an ordinary outage already
    uses, so sustained RAM pressure surfaces to Operator exactly like a
    real outage: an honest, visible pause, never a silent hang and
    never a call fired blindly into an already-strained host.

    `on_wait`, if given, is called with (used_bytes, effective_ceiling)
    each time a wait actually happens (never on the fast, no-wait path)
    -- lets the caller publish a real, visible trace event without this
    module needing to know about task_id/trace plumbing itself.
    """
    deadline = time.monotonic() + max_wait_sec
    while True:
        try:
            used, total = await read_ram_metrics(http_client, base_url)
        except (GpuMemoryMetricsUnavailable, httpx.HTTPError):
            return  # fail-open -- a truly dead host is caught by the real call's own retry logic
        effective_ceiling = _effective_ceiling(total, ceiling_fraction, ceiling_bytes)
        if used < effective_ceiling:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{backend_name} RAM usage ({used / 1024**3:.1f} GiB) still at/above its "
                f"effective ceiling ({effective_ceiling / 1024**3:.1f} GiB, "
                f"{ceiling_fraction:.0%} of {total / 1024**3:.1f} GiB total) after waiting "
                f"{max_wait_sec:.0f}s"
            )
        if on_wait is not None:
            on_wait(used, effective_ceiling)
        await asyncio.sleep(poll_interval_sec)
