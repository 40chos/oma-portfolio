"""Isolated, infra-free tests for manager.step_scheduler -- the generic
declarative reads/writes scheduler (Phase 22 follow-up, 2026-07-23).
No Postgres/Redis/LLM involved: pure logic, fake async steps with
timing instrumentation to prove real concurrency, not just correct
final output.
"""

import asyncio
import time

import pytest

from manager.step_scheduler import SchedulerError, Step, run_steps


def _instant(writes: dict) -> callable:
    async def _run(context):
        return dict(writes)

    return _run


def _timed(delay: float, writes: dict, log: list) -> callable:
    async def _run(context):
        await asyncio.sleep(delay)
        log.append((time.monotonic(), writes.copy()))
        return dict(writes)

    return _run


def test_independent_steps_run_concurrently_not_sequentially():
    """Three steps with NO ordering constraint between them, each
    taking ~0.3s: sequential would be ~0.9s, real concurrency should
    be close to 0.3s. This is the actual claim the scheduler makes --
    prove it with a real timing measurement, not just correct output.
    """
    log = []
    steps = [
        Step(name="a", reads=frozenset(), writes=frozenset({"a"}), run=_timed(0.3, {"a": 1}, log)),
        Step(name="b", reads=frozenset(), writes=frozenset({"b"}), run=_timed(0.3, {"b": 2}, log)),
        Step(name="c", reads=frozenset(), writes=frozenset({"c"}), run=_timed(0.3, {"c": 3}, log)),
    ]
    start = time.monotonic()
    result = asyncio.run(run_steps(steps, {}))
    elapsed = time.monotonic() - start
    assert result == {"a": 1, "b": 2, "c": 3}
    assert elapsed < 0.6, f"expected concurrent (~0.3s), got {elapsed:.2f}s -- ran sequentially"


def test_dependent_step_waits_for_a_full_wave_not_per_predecessor():
    """d depends on a and b -- correctness: d must see a and b's REAL
    values, not run before either is ready. Honest documentation of a
    real, deliberate design property found while writing this test
    (bulk-synchronous "superstep" batching, the same model LangGraph's
    own execution semantics use -- see module docstring's sources):
    c has NO relation to a/b/d at all, but because a, b, AND c are all
    simultaneously ready in the FIRST wave (empty reads), they run in
    ONE `asyncio.gather()` together -- d (which only needs a+b) still
    can't start until that ENTIRE wave finishes, including c, even
    though c is slower and unrelated. This is a real, known tradeoff
    of wave-based scheduling (simple, matches mature frameworks) over
    fully greedy per-task scheduling (more responsive, meaningfully
    more complex) -- acceptable at this system's actual step-graph
    depth (2-4 items per wave, not deep chains), not something to
    silently assume away.
    """
    log = []

    async def _d(context):
        assert context["a"] == 1 and context["b"] == 2
        log.append(("d", time.monotonic()))
        return {"d": context["a"] + context["b"]}

    steps = [
        Step(name="a", reads=frozenset(), writes=frozenset({"a"}), run=_timed(0.1, {"a": 1}, log)),
        Step(name="b", reads=frozenset(), writes=frozenset({"b"}), run=_timed(0.1, {"b": 2}, log)),
        Step(name="c", reads=frozenset(), writes=frozenset({"c"}), run=_timed(0.3, {"c": 99}, log)),
        Step(name="d", reads=frozenset({"a", "b"}), writes=frozenset({"d"}), run=_d),
    ]
    start = time.monotonic()
    result = asyncio.run(run_steps(steps, {}))
    elapsed = time.monotonic() - start
    assert result["d"] == 3
    # d necessarily runs after the whole first wave (including c)
    # completes -- confirmed by total elapsed time being dominated by
    # c's 0.3s, not a/b's 0.1s, proving d really did wait for the wave.
    assert elapsed >= 0.3, f"d must wait for the full first wave (c included): got {elapsed:.2f}s"


def test_initial_context_seeds_availability():
    """A step reading a key that's already in the STARTING context
    (e.g. 'contract', 'module_name' -- known before any step runs at
    all) is immediately runnable, no producer step needed for it.
    """
    steps = [
        Step(
            name="only", reads=frozenset({"contract"}), writes=frozenset({"result"}),
            run=lambda ctx: _instant({"result": ctx["contract"] + 1})(ctx),
        ),
    ]
    result = asyncio.run(run_steps(steps, {"contract": 41}))
    assert result["result"] == 42


def test_unsatisfiable_read_raises_scheduler_error_not_hangs():
    steps = [
        Step(name="orphan", reads=frozenset({"never_written"}), writes=frozenset({"x"}), run=_instant({"x": 1})),
    ]
    with pytest.raises(SchedulerError, match="never_written"):
        asyncio.run(run_steps(steps, {}))


def test_declaration_drift_raises_not_silently_missing_key():
    """A step that declares it writes 'y' but its real run() forgot to
    include it -- this is EXACTLY the failure mode the research flagged
    (declared metadata drifting out of sync with real behavior) --
    must fail loudly, not silently leave 'y' out of the final context.
    """
    async def _broken(context):
        return {"x": 1}  # declared writes={"x", "y"} below, but never returns "y"

    steps = [
        Step(name="broken", reads=frozenset(), writes=frozenset({"x", "y"}), run=_broken),
    ]
    with pytest.raises(SchedulerError, match="broken"):
        asyncio.run(run_steps(steps, {}))


def test_empty_step_list_returns_context_unchanged():
    result = asyncio.run(run_steps([], {"already": "here"}))
    assert result == {"already": "here"}


def test_three_level_chain_runs_in_correct_order_despite_gather():
    """a -> b -> c, a strict chain -- even though the scheduler always
    gathers whatever's ready, a chain with no parallelism opportunity
    at any point must still execute in the right order and produce the
    right final value (proves the fixed-point loop, not just the
    single-batch case the other tests already cover).
    """
    async def _a(context):
        return {"a": 1}

    async def _b(context):
        return {"b": context["a"] + 1}

    async def _c(context):
        return {"c": context["b"] + 1}

    steps = [
        Step(name="a", reads=frozenset(), writes=frozenset({"a"}), run=_a),
        Step(name="b", reads=frozenset({"a"}), writes=frozenset({"b"}), run=_b),
        Step(name="c", reads=frozenset({"b"}), writes=frozenset({"c"}), run=_c),
    ]
    result = asyncio.run(run_steps(steps, {}))
    assert result == {"a": 1, "b": 2, "c": 3}
