"""Generic, dependency-aware step scheduler (Phase 22 follow-up, 2026-07-23).

Replaces hand-picked `asyncio.gather()` pairs -- verified independent
once, by a human, and frozen in code forever -- with a small,
declarative scheduler: each step declares which context keys it reads
and which keys it writes. The scheduler repeatedly runs every step
whose reads are already available, in parallel via `asyncio.gather()`,
until nothing more is ready. Adding, removing, or reordering steps
never requires re-verifying independence by hand again -- the
scheduler derives what's safe to parallelize from the declarations,
every time, for every task, automatically.

Deliberately NOT a general DAG engine (no persistence, no retries, no
cross-process execution, no LLM-emitted graphs) -- researched directly
against how LangGraph/Prefect/Ray/LLMCompiler solve this same problem
(see docs/planning/PHASE22_PARALLEL_EXECUTION_AND_SYSTEM_HARDENING_2026-07-23.md's
own follow-up section): at this system's actual scale (a handful of
steps per specialist, dozens-hundreds of tasks/night, not thousands/sec),
every mature framework still pushes the same "declare your reads/writes"
burden onto the developer -- none of them do free static analysis of
arbitrary LLM prompt content to infer independence, because that isn't
a solved problem. The value here is maintainability (never re-verify
independence by hand again), not raw throughput at a scale this system
doesn't operate at.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class Step:
    """One unit of work in a step graph.

    `reads`: the context keys this step's `run()` needs before it can
    execute -- an empty frozenset means "runnable immediately."
    `writes`: the context keys this step's `run()` result MUST contain
    -- checked after every run, so a step whose real behavior drifted
    out of sync with its own declaration (e.g. it now also depends on
    something new) fails loudly (`SchedulerError`) instead of silently
    running too early with stale/missing input.
    `run`: an async callable taking the CURRENT context dict (read-only
    by convention -- steps must return their outputs, never mutate the
    dict in place, so two steps running concurrently can never race on
    it) and returning a dict containing at least every key in `writes`.
    """

    name: str
    reads: frozenset[str]
    writes: frozenset[str]
    run: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class SchedulerError(RuntimeError):
    pass


async def run_steps(steps: list[Step], context: dict[str, Any]) -> dict[str, Any]:
    """Runs `steps` to completion, executing every currently-unblocked
    batch concurrently via `asyncio.gather()`. `context` starts with
    whatever's already known (e.g. `{"contract": ..., "module_name": ...}`)
    and grows as steps complete. Returns the final, fully-populated
    context (the same dict object, mutated in place at the top level
    only -- individual steps never see or touch it directly, only
    their own `run(context)` argument, which IS this same dict, read
    from but never written to by convention above).

    Steps with no ordering constraint between them run in the same
    `asyncio.gather()` batch. This is bulk-synchronous ("superstep")
    scheduling, the same model LangGraph's own execution semantics
    use, NOT fully greedy per-task scheduling: a step waits for every
    wave that any of its dependencies belong to to finish COMPLETELY
    before it can start, even if its own specific dependency finished
    early -- if wave 1 contains both a 0.1s step and an unrelated 2s
    step, a wave-2 step needing only the fast one still can't start
    until the slow one also finishes (see
    `test_dependent_step_waits_for_a_full_wave_not_per_predecessor`).
    Acceptable, deliberate tradeoff at this system's actual step-graph
    depth (2-4 items per wave); a fully greedy scheduler would be
    meaningfully more complex for a benefit this system's real shape
    doesn't need.
    """
    remaining = list(steps)
    available = set(context.keys())
    while remaining:
        ready = [s for s in remaining if s.reads <= available]
        if not ready:
            blocked = [(s.name, sorted(s.reads - available)) for s in remaining]
            raise SchedulerError(
                f"no step is runnable -- either a real cycle between two steps, or a "
                f"step declares a read that nothing in this graph ever writes: {blocked}"
            )
        results = await asyncio.gather(*(step.run(context) for step in ready))
        for step, result in zip(ready, results):
            missing = step.writes - result.keys()
            if missing:
                raise SchedulerError(
                    f"step {step.name!r} declared writes={sorted(step.writes)} but its "
                    f"actual result is missing {sorted(missing)} -- the step's own "
                    f"declaration has drifted out of sync with what it really produces"
                )
            for key in step.writes:
                context[key] = result[key]
            available.update(step.writes)
        remaining = [s for s in remaining if s not in ready]
    return context
