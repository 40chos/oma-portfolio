"""Dependency-aware scheduler for constraint graphs.

Replaces a flat sequential walk over constraint labels with a real,
dependency-satisfaction-based scheduler operating on `ConstraintNode.state` —
the single scheduling-authoritative state store. `TaskContract.constraint_status`
stays a derived, on-demand view, never a second persisted copy.

Every function here is pure and unit-testable. `run_graph_scheduler()` is the one
event-driven rolling dispatch loop tying them together — callers supply an
`execute_node` coroutine per label that must catch every terminal condition
internally and return a status string, never raise. This makes asyncio's
default cancel-on-exception behavior irrelevant: no sibling node is ever
cancelled by another's failure.

Concurrency is capped in two layers: an ops-controlled env var
(`OMA_GRAPH_MAX_CONCURRENT_NODES`, default 1 — fully serial) and a hard ceiling
(`OMA_GRAPH_HARD_MAX_CONCURRENT_NODES`) that no caller or env var can exceed,
gated on a concurrent-write-safety mechanism (per-node branch isolation plus a
serialized-apply patch layer) having actually landed and been verified.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable

from contracts.schema import ConstraintNode, ConstraintNodeState
from manager.replanning import sort_constraint_labels_by_dependency_tier
from manager.tools import (
    update_graph_created_node_round,
    update_graph_created_node_specialist,
    update_graph_created_node_state,
)
from manager.trace import publish_trace_event

OMA_GRAPH_MAX_CONCURRENT_NODES = int(os.environ.get("OMA_GRAPH_MAX_CONCURRENT_NODES", "1"))

# Deliberately a separate constant from the env var above: the env var can still be
# used to run at 1 (fully serial) or 2, but neither it nor any caller can push real
# dispatch concurrency past this ceiling.
OMA_GRAPH_HARD_MAX_CONCURRENT_NODES = 2


def _publish_node_state_changed(task_id: str | None, label: str, new_state: str) -> None:
    """Publishes a live `node_state_changed` event to this task's Redis trace channel
    (consumed by the SSE stream), and persists the same transition into the durable
    Postgres snapshot. Both channels matter: Redis for instant live patching while a
    browser is watching, Postgres for a correct snapshot on a fresh page load — a
    periodic poll or reload reads the Postgres snapshot, not the Redis stream, so a
    transition that only updated Redis would appear stuck on refresh. A no-op when
    task_id is None (e.g. in tests with no real task to publish against).
    """
    if not task_id:
        return
    publish_trace_event(task_id, {
        "phase": "node_state_changed", "node_id": label, "new_state": new_state,
        "level": "manager", "actor": "manager", "status": "running",
        "message": f"node_state_changed: {label!r} -> {new_state}",
    })
    update_graph_created_node_state(task_id, label, new_state)


def _publish_node_specialist_changed(task_id: str | None, label: str | None, specialist: str | None) -> None:
    """Publishes which specialist (build/review/qa) is currently active on a node, for
    the live per-node specialist badge in the UI. `label` is None for the plain,
    non-decomposed execution path (no graph node to patch) — a no-op there, same as
    `_publish_node_state_changed()`'s own task_id=None no-op.
    """
    if not task_id or not label:
        return
    publish_trace_event(task_id, {
        "phase": "node_specialist_changed", "node_id": label, "specialist": specialist,
        "level": "manager", "actor": "manager", "status": "running",
        "message": f"node_specialist_changed: {label!r} -> {specialist!r}",
    })
    update_graph_created_node_specialist(task_id, label, specialist)


def _publish_node_round_advanced(task_id: str | None, label: str | None, round_number: int) -> None:
    """Publishes a node's current round number, for the live per-node "N/M" round
    counter in the UI. Same None-label no-op posture as the sibling publish functions
    above.
    """
    if not task_id or not label:
        return
    publish_trace_event(task_id, {
        "phase": "node_round_advanced", "node_id": label, "round_number": round_number,
        "level": "manager", "actor": "manager", "status": "running",
        "message": f"node_round_advanced: {label!r} -> round {round_number}",
    })
    update_graph_created_node_round(task_id, label, round_number)


# Flipped True once the concurrent-write-safety mechanism (per-node branch isolation
# with a frozen snapshot, plus a serialized-apply line-hunk patch layer) landed and was
# verified end to end against real infrastructure, not just unit-tested in isolation.
_CONCURRENT_WRITE_ISOLATION_LANDED = True


class ConcurrencyNotLandedError(RuntimeError):
    """Raised when a caller requests concurrency above the hard ceiling
    (`OMA_GRAPH_HARD_MAX_CONCURRENT_NODES`) or passes force_allow_concurrency=True
    without the isolation mechanism having landed — a hard config error, never a
    silent fallback to cap=1.
    """


def ready_labels(nodes: dict[str, ConstraintNode]) -> list[str]:
    """A label is ready iff its own state is `pending` AND every predecessor named in
    its `predecessor_labels` is `satisfied`. A predecessor absent from `nodes` (a
    stale/malformed entry) is silently skipped rather than guessed. The dependency-tier
    sort runs fresh on each call, since a precomputed order would go stale as state
    changes between calls.
    """
    candidates = [
        label for label, node in nodes.items()
        if node.state == ConstraintNodeState.pending
        and all(
            nodes[predecessor].state == ConstraintNodeState.satisfied
            for predecessor in node.predecessor_labels
            if predecessor in nodes
        )
    ]
    return sort_constraint_labels_by_dependency_tier(candidates)


def build_dependents_index(nodes: dict[str, ConstraintNode]) -> dict[str, list[str]]:
    """Reverse adjacency, built once: dependents_of[label] = every other label whose
    own `predecessor_labels` names `label` — i.e. every descendant, not ancestor.
    """
    dependents: dict[str, list[str]] = {label: [] for label in nodes}
    for label, node in nodes.items():
        for predecessor in node.predecessor_labels:
            if predecessor in dependents:
                dependents[predecessor].append(label)
    return dependents


def mark_blocked_by_failure(
    nodes: dict[str, ConstraintNode],
    dependents_of: dict[str, list[str]],
    failed_label: str,
    task_id: str | None = None,
) -> set[str]:
    """Transitive closure over `dependents_of` (descendants, never `predecessor_labels`,
    which would walk ancestors instead) — every label downstream of `failed_label` gets
    `state=blocked`. Returns the set of labels actually blocked, for
    `unblock_transitive_dependents()`'s later reversal on retry.
    """
    blocked: set[str] = set()
    frontier = list(dependents_of.get(failed_label, []))
    while frontier:
        label = frontier.pop()
        if label in blocked or label not in nodes:
            continue
        blocked.add(label)
        nodes[label].state = ConstraintNodeState.blocked
        _publish_node_state_changed(task_id, label, "blocked")
        frontier.extend(dependents_of.get(label, []))
    return blocked


def unblock_transitive_dependents(nodes: dict[str, ConstraintNode], blocked_labels: set[str]) -> None:
    """The inverse of `mark_blocked_by_failure()`, called on retry. Resets every label
    in `blocked_labels` still in the `blocked` state back to `pending`, so the rolling
    dispatch loop can re-admit it once its predecessors are satisfied again. A label
    already moved on to some other state is left alone — this only ever reverses its
    own prior blocking.
    """
    for label in blocked_labels:
        node = nodes.get(label)
        if node is not None and node.state == ConstraintNodeState.blocked:
            node.state = ConstraintNodeState.pending


def detect_tier_order_divergence(
    label: str, original_tier_order: list[str], nodes: dict[str, ConstraintNode],
) -> bool:
    """True iff `label` is about to be dispatched while some other label earlier in
    `original_tier_order` is still `pending` — i.e. a real dependency edge let this
    label become ready ahead of its own tier-bucket position. At
    OMA_GRAPH_MAX_CONCURRENT_NODES=1 this is the only behavioral difference from a
    flat tier-order walk; everything else about serial dispatch order is identical.
    """
    label_index = original_tier_order.index(label)
    return any(
        nodes[earlier_label].state == ConstraintNodeState.pending
        for earlier_label in original_tier_order[:label_index]
        if earlier_label in nodes
    )


def _target_model_prefix(artifact_name: str) -> str:
    """`creates` entries are freeform dotted strings (`<model>.<symbol>`, e.g.
    `'service.ticket.action_bulk_close'`). Exact-string overlap alone only catches two
    nodes claiming the identical artifact, never two nodes writing different symbols
    into the same target file (e.g. two nodes both touching `service.ticket` via
    different methods). Stripping only the last dot-segment off a `creates` string
    reliably recovers the target model for every observed shape (e.g.
    `'service.ticket.action_bulk_close'` -> `'service.ticket'`). A string with no dot
    at all (a malformed/degenerate artifact name) returns itself unchanged —
    conservative: two identical dotless names still collide, two different ones are
    simply treated as different targets, matching plain exact-match behavior.
    """
    if "." not in artifact_name:
        return artifact_name
    return artifact_name.rsplit(".", 1)[0]


def _admit_from_wave(
    wave: list[str],
    in_flight_labels: set[str],
    nodes: dict[str, ConstraintNode],
    open_slots: int,
) -> list[str]:
    """A free, non-overlapping-`creates`-target admission pre-filter, with
    same-iteration backfill — a candidate whose `creates` overlaps an already-in-flight
    (or already-admitted-this-pass) node's own `creates` is deferred, but scanning
    continues for other non-conflicting candidates up to `open_slots` (a deferred
    candidate never shrinks admission). Inert at
    OMA_GRAPH_MAX_CONCURRENT_NODES=1 (open_slots is never >1 there), but load-bearing
    once concurrency is raised.

    Two candidates are also deferred against each other when they share a target model
    (via `_target_model_prefix()`), not only on exact `creates` string equality — two
    nodes writing different symbols into the same model's generated file still need to
    serialize. Deliberately keyed off `creates` only, never `requires` — a node that
    only reads from a model never writes to that model's own file, so it can genuinely
    run alongside a real writer of that model; only two writers of the same target ever
    need to serialize.
    """
    reserved_creates: set[str] = set()
    reserved_models: set[str] = set()
    for label in in_flight_labels:
        node_creates = nodes[label].creates
        reserved_creates.update(node_creates)
        reserved_models.update(_target_model_prefix(c) for c in node_creates)
    admitted: list[str] = []
    for label in wave:
        if len(admitted) >= open_slots:
            break
        candidate_creates = set(nodes[label].creates)
        candidate_models = {_target_model_prefix(c) for c in candidate_creates}
        if candidate_creates & reserved_creates or candidate_models & reserved_models:
            continue
        admitted.append(label)
        reserved_creates.update(candidate_creates)
        reserved_models.update(candidate_models)
    return admitted


async def run_graph_scheduler(
    nodes: dict[str, ConstraintNode],
    execute_node: Callable[[str], Awaitable[str]],
    *,
    task_id: str | None = None,
    max_concurrent: int | None = None,
    force_allow_concurrency: bool = False,
    remaining_budget: int | None = None,
    worst_case_budget_for: Callable[[str], int] | None = None,
) -> dict[str, set[str]]:
    """The event-driven rolling dispatch loop. Maintains `in_flight: dict[str,
    asyncio.Task]`; each iteration recomputes the ready wave, admits up to
    `open_slots` non-conflicting labels (with same-iteration backfill), dispatches
    them, and updates state immediately on each completion — `satisfied`, `failing`,
    and `paused` are all handled distinctly, with `failing` triggering
    `mark_blocked_by_failure()` and `paused` genuinely recorded as `paused` rather than
    left at `running`, so a paused node is distinguishable on resume from one that was
    genuinely interrupted mid-execution.

    `execute_node(label)` must catch every terminal condition internally and return
    one of "satisfied" / "failing" / "paused" — never raise. A real exception escaping
    it is not caught here; it propagates to this function's own caller exactly like
    any uncaught exception would, by design — no silent downgrade to a "failed" status.

    Returns `blocked_by`: `{failed_label: set_of_labels_it_blocked}` for every failure
    this run produced — callers retrying a specific failed label pass its own recorded
    set straight to `unblock_transitive_dependents()`.
    """
    effective_max_concurrent = OMA_GRAPH_MAX_CONCURRENT_NODES if max_concurrent is None else max_concurrent
    effective_max_concurrent = min(effective_max_concurrent, OMA_GRAPH_HARD_MAX_CONCURRENT_NODES)
    if effective_max_concurrent > 1 and not (force_allow_concurrency and _CONCURRENT_WRITE_ISOLATION_LANDED):
        raise ConcurrencyNotLandedError(
            f"OMA_GRAPH_MAX_CONCURRENT_NODES={effective_max_concurrent} requested but concurrent "
            "node execution is not enabled for production use — this is a deliberate gate, not a bug."
        )

    original_tier_order = sort_constraint_labels_by_dependency_tier(list(nodes.keys()))
    dependents_of = build_dependents_index(nodes)
    blocked_by: dict[str, set[str]] = {}
    in_flight: dict[str, asyncio.Task] = {}

    while True:
        wave = [label for label in ready_labels(nodes) if label not in in_flight]
        open_slots = effective_max_concurrent - len(in_flight)
        if remaining_budget is not None and worst_case_budget_for is not None and wave:
            worst_case = max(worst_case_budget_for(label) for label in wave)
            budget_slots = max(1, remaining_budget // worst_case) if remaining_budget > 0 else 0
            open_slots = min(open_slots, budget_slots)
        admitted = _admit_from_wave(wave, set(in_flight.keys()), nodes, max(0, open_slots))

        for label in admitted:
            if detect_tier_order_divergence(label, original_tier_order, nodes):
                publish_trace_event(task_id or "", {
                    "level": "manager", "actor": "manager", "phase": "delegate",
                    "message": f"graph_order_diverged_from_tier: dispatching {label!r} ahead of its own tier-bucket position, following a real dependency edge instead",
                    "status": "running",
                })
            nodes[label].state = ConstraintNodeState.running
            _publish_node_state_changed(task_id, label, "running")
            in_flight[label] = asyncio.ensure_future(execute_node(label))

        if not in_flight:
            break

        done, _pending = await asyncio.wait(in_flight.values(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            completed_label = next(label for label, t in in_flight.items() if t is task)
            del in_flight[completed_label]
            outcome = task.result()
            if outcome == "satisfied":
                nodes[completed_label].state = ConstraintNodeState.satisfied
                _publish_node_state_changed(task_id, completed_label, "satisfied")
            elif outcome == "failing":
                nodes[completed_label].state = ConstraintNodeState.failing
                _publish_node_state_changed(task_id, completed_label, "failing")
                blocked_by[completed_label] = mark_blocked_by_failure(
                    nodes, dependents_of, completed_label, task_id=task_id,
                )
            elif outcome == "paused":
                nodes[completed_label].state = ConstraintNodeState.paused
                _publish_node_state_changed(task_id, completed_label, "paused")
            else:
                raise ValueError(
                    f"execute_node({completed_label!r}) returned unrecognized outcome {outcome!r} -- "
                    "must be 'satisfied', 'failing', or 'paused'"
                )
            if remaining_budget is not None and worst_case_budget_for is not None:
                remaining_budget -= worst_case_budget_for(completed_label)

    return blocked_by
