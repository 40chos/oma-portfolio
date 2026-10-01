"""Phase 31 §2: the graph-aware scheduler -- replaces manager/loop.py's flat sequential
constraint-label walk (`_run_constraint_labels_from()`'s own `for index in range(...)` loop) with
a real, dependency-satisfaction-based scheduler operating on `ConstraintNode.state` (the ONE
scheduling-authoritative state store -- `TaskContract.constraint_status` stays a derived,
on-demand view, never a second persisted copy; see `_derive_constraint_status_from_node_states()`
in manager/loop.py).

Every function here is pure/unit-testable, no mocks needed, matching contracts/constraint_graph.py's
own established pattern. `run_graph_scheduler()` is the one real, event-driven rolling dispatch
loop tying them together -- callers (manager/loop.py) supply an `execute_node` coroutine per label
that MUST catch every terminal condition internally and return a status string, never raise (this
makes asyncio's default cancel-on-exception behavior structurally irrelevant -- no sibling node is
ever cancelled by another's failure).

OMA_GRAPH_MAX_CONCURRENT_NODES defaults to 1 as an ops-controlled fallback (fully serial). This
module hard-gates any attempt to raise concurrency until the concurrent-write-safety mechanism
(§6, Phase A.5) has landed (`_CONCURRENT_WRITE_ISOLATION_LANDED`), AND caps real concurrency at
`OMA_GRAPH_HARD_MAX_CONCURRENT_NODES` (2) no matter what any caller or env var requests -- Phase B
(2026-08-09), landed: `manager/loop.py` now dispatches at most 2 constraint-graph nodes at once.
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

# Phase B (2026-08-09), the project owner's own explicit words: "maybe two things at a time. I don't want
# more." A hard ceiling enforced in code, independent of whatever OMA_GRAPH_MAX_CONCURRENT_NODES
# (an ops-controlled env var, defaulting to 1 above) or any future caller passes as `max_concurrent`
# -- neither can ever push real dispatch above this number. This is deliberately NOT the same knob
# as the env var: the env var can still be used to run at 1 (fully serial, the safe fallback) or 2,
# but never to accidentally raise real concurrency past what was explicitly asked for here.
OMA_GRAPH_HARD_MAX_CONCURRENT_NODES = 2


def _publish_node_state_changed(task_id: str | None, label: str, new_state: str) -> None:
    """Phase 31 UI doc §3.1/§5 server.py item 1 (2026-08-08): publishes a real, live
    `node_state_changed` event to the SAME Redis channel (`oma:trace:{task_id}`, via the
    existing `publish_trace_event()`) already fed to `GET /api/stream/{task_id}` -- confirmed by
    reading `ui/chat/server.py`'s `stream_task()` end to end: it forwards the raw published JSON
    verbatim, so no new transport, endpoint, or pg_notify plumbing is needed here at all, exactly
    per the design doc's own conclusion. A no-op (task_id=None) for any caller -- tests, mostly --
    that doesn't have a real task_id to publish against.

    Also persists the SAME transition into the `graph_created` event's own durable snapshot
    (manager.tools.update_graph_created_node_state()) -- real bug found live (2026-08-08, task
    05c20568-...): the Redis publish alone only reaches a browser with an OPEN, live SSE
    connection at the exact moment it fires; a page load/reload, or a periodic poll-based
    refresh, reads GET /api/rounds instead, which returned the graph_created row's own frozen,
    decomposition-time snapshot (every node stuck at 'pending') for as long as no round ever
    failed -- confirmed live, a task where every node genuinely passed on its first try showed
    all nodes stuck 'pending' forever, even after real, full completion. Both channels now stay
    in sync: Redis for instant live patching while a browser is watching, Postgres for a correct
    snapshot on every fresh load.
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
    """Phase 31 UI doc §3.1/§5 server.py item 1, closed 2026-08-08 -- real gap found live
    (the project owner's own follow-up after the UI status audit): the frontend has had a real,
    already-built consumer for this event since the initial UI implementation pass (the
    per-node canvas specialist badge, gated on `d.status === 'running' && d.activeSpecialist`,
    and the `scheduleGraphPatch(ev.node_id, {activeSpecialist: ...})` handler in `ensureStream`'s
    `onmessage`), but nothing on the backend ever emitted it -- confirmed by grepping the whole
    codebase for the string literal before this fix, zero matches. That made the frontend code a
    dead path: it could never receive real data, no matter how long a task ran.

    `label` is `None` for the plain, non-decomposed `_execute_contract()` path (no real graph
    node exists to patch) -- a no-op there, same posture as `_publish_node_state_changed()`'s own
    `task_id=None` no-op. `specialist` is one of `'build'` (Build/bug_fix), `'review'` (Code
    Review), `'qa'` (Testing/QA), or `None` to clear it -- matching the three real glyphs/CSS
    classes (`.specialist-chip.build/review/qa`) already defined in `index.html`, never a fourth
    value.
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
    """Phase 31 UI doc §3.1/§5 server.py item 1, closed 2026-08-08 -- same real gap as
    `_publish_node_specialist_changed()` above: the frontend's `roundBadges` per-node "N/M" text
    (`updateNodeBadges()`) already reads `d.roundNumber`, and `ensureStream`'s `onmessage` already
    has a `node_round_advanced` branch calling `scheduleGraphPatch(ev.node_id, {roundNumber: ...})`
    -- neither could ever receive real data before this fix. `label` is `None` for the plain,
    non-decomposed path, a no-op there, same posture as the sibling publish functions above.
    """
    if not task_id or not label:
        return
    publish_trace_event(task_id, {
        "phase": "node_round_advanced", "node_id": label, "round_number": round_number,
        "level": "manager", "actor": "manager", "status": "running",
        "message": f"node_round_advanced: {label!r} -> round {round_number}",
    })
    update_graph_created_node_round(task_id, label, round_number)

# Phase 31 §6: flipped True by the concurrent-write-safety mechanism's own commit -- per-node
# branch isolation + frozen snapshot (tools_odoo/module_dev/vcs.py, real, live-tested against
# actual Gitea: docs/reports/PHASE31_FULL_IMPLEMENTATION_EVIDENCE_2026-08-07.md §4) and the
# serialized-apply + line-hunk patch layer (specialists/build/specialist.py's
# apply_node_result_to_module()/_apply_line_hunks(), wired live into the real write path, not
# orphaned -- same report §4) both landed and were verified.
#
# Phase B (2026-08-09): production now DOES pass force_allow_concurrency=True from the one real
# call site (`manager/loop.py`'s `_run_constraint_labels_from()`), at a hard-ceilinged
# `max_concurrent=2` (see `OMA_GRAPH_HARD_MAX_CONCURRENT_NODES` above) -- the project owner's own explicit,
# tested, and approved decision after the target-model conflict-detection tightening
# (`_admit_from_wave()`/`_target_model_prefix()`) and the DB-install serialization lock
# (`infra/fencing.py`'s `acquire_db_install_lock()`) both landed and were verified (30/30 and
# 12/12 tests respectively; see docs/reports/PHASE_B_CONCURRENCY_IMPLEMENTATION_2026-08-09.md).
_CONCURRENT_WRITE_ISOLATION_LANDED = True


class ConcurrencyNotLandedError(RuntimeError):
    """Raised when a caller requests concurrency above the hard ceiling
    (`OMA_GRAPH_HARD_MAX_CONCURRENT_NODES`) or passes force_allow_concurrency=True without the
    isolation mechanism having landed -- a hard config error, never a silent fallback to cap=1.
    """


def ready_labels(nodes: dict[str, ConstraintNode]) -> list[str]:
    """A label is ready iff its own state is `pending` AND every predecessor named in its
    `predecessor_labels` is `satisfied` (a predecessor absent from `nodes` -- a stale/malformed
    entry -- is silently skipped, never guessed). Calls
    `sort_constraint_labels_by_dependency_tier()` DIRECTLY on the candidate set each time -- no
    precomputed `tier_order` parameter, since the tier sort is cheap/deterministic and a stale
    precomputed order would be a real correctness risk across repeated calls as state changes.
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
    """Reverse adjacency, built once: dependents_of[label] = every OTHER label whose own
    `predecessor_labels` names `label` -- i.e. every real descendant, not ancestor.
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
    """Transitive closure over `dependents_of` (real descendants, never `predecessor_labels`,
    which would give ancestors) -- every label downstream of `failed_label` gets
    `state=blocked`. Returns the set of labels actually blocked, for
    `unblock_transitive_dependents()`'s own later reversal on retry.
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
    """The declared inverse of `mark_blocked_by_failure()` -- called on retry. Resets every label
    in `blocked_labels` STILL in the `blocked` state back to `pending`, so the rolling dispatch
    loop can re-admit it once its own predecessors are satisfied again. A label already moved on
    to some other state by the time this runs (e.g. a genuinely-interrupted resume case) is left
    alone -- this function only ever reverses its own prior blocking, never anything else.
    """
    for label in blocked_labels:
        node = nodes.get(label)
        if node is not None and node.state == ConstraintNodeState.blocked:
            node.state = ConstraintNodeState.pending


def detect_tier_order_divergence(
    label: str, original_tier_order: list[str], nodes: dict[str, ConstraintNode],
) -> bool:
    """The one disclosed, logged divergence from today's flat tier-order walk: True iff `label`
    is about to be dispatched while some OTHER label earlier in `original_tier_order` is still
    `pending` -- i.e. a real dependency edge let this label become ready ahead of its own
    tier-bucket position. At OMA_GRAPH_MAX_CONCURRENT_NODES=1 this is the ONLY behavioral
    difference from the old flat walk; everything else about cap=1 dispatch order is identical.
    """
    label_index = original_tier_order.index(label)
    return any(
        nodes[earlier_label].state == ConstraintNodeState.pending
        for earlier_label in original_tier_order[:label_index]
        if earlier_label in nodes
    )


def _target_model_prefix(artifact_name: str) -> str:
    """Real fix, 2026-08-09 (the project owner's own explicit request to make concurrency "very, very
    smart... take into account all these files that do not contradict"). `creates` entries are
    freeform dotted strings (`<model>.<symbol>`, e.g. `'service.ticket.action_bulk_close'`) --
    exact-string overlap (the original Layer 1 filter, below) only catches two nodes claiming
    the IDENTICAL artifact, never two nodes writing DIFFERENT symbols into the SAME real target
    file. Confirmed live via direct analysis of a real, currently-running task's own data
    (07141af5-9a4e-41b6-93ea-8b7af04fea9c): `ticket_bulk_close` creates
    `'service.ticket.action_bulk_close'`, `daily_escalation_cron` creates
    `'service.ticket.cron_escalation'`/`'service.ticket.action_escalate'`, and
    `ticket_workflow_and_logging` creates `'service.ticket.state.*'`/`'action_in_progress'`/etc --
    zero of these strings are IDENTICAL to each other, so the exact-match filter alone would
    admit all three concurrently even though they all write into the same real `service.ticket`
    model's own generated file. Odoo model names are themselves dotted (`res.partner`,
    `service.ticket`, `project.task`) -- stripping only the LAST dot-segment off a `creates`
    string (the real symbol/method/field being added) reliably recovers the real target model
    for every shape observed in this project's own real data (confirmed against every example in
    the incident above: `'service.ticket.action_bulk_close'` -> `'service.ticket'`,
    `'project.model.open_ticket_count'` -> `'project.model'`). A string with no dot at all (rare,
    a malformed/degenerate artifact name) returns itself unchanged -- conservative: two identical
    dotless names still collide, which is correct; two DIFFERENT dotless names are simply treated
    as different targets, same as today's exact-match behavior, never a new false negative.
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
    """Phase 31 §6 Layer 1: a free, non-overlapping-`creates`-target admission pre-filter, WITH
    same-iteration backfill -- a candidate whose `creates` overlaps an already-in-flight (or
    already-admitted-this-pass) node's own `creates` is deferred, but scanning continues for
    other non-conflicting candidates up to `open_slots` (a deferred candidate never shrinks
    admission). Inert at OMA_GRAPH_MAX_CONCURRENT_NODES=1 (open_slots is never >1 there), but a
    real, load-bearing filter once Phase B ever raises the cap.

    Real fix, 2026-08-09: two candidates are now ALSO deferred against each other when they share
    a real target model (via `_target_model_prefix()`, above), not only on exact `creates`
    string equality -- see that function's own docstring for the real, live-confirmed gap this
    closes. Deliberately keyed off `creates` only, never `requires` -- a node that only READS
    from a model (e.g. `project_ticket_counts` requiring `service.ticket` fields to compute a
    count) never writes to that model's own file, so it genuinely can run alongside a real writer
    of that model; only two real WRITERS of the same target ever need to serialize.
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
    """The real, event-driven rolling dispatch loop. Maintains `in_flight: dict[str, asyncio.Task]`;
    each iteration recomputes the ready wave, admits up to `open_slots` non-conflicting labels
    (§6 Layer 1, with same-iteration backfill), dispatches them, and on each completion updates
    state IMMEDIATELY (never batch-deferred) -- `satisfied`/`failing`/`paused` all handled, with
    `failing` triggering `mark_blocked_by_failure()` and `paused` genuinely recorded as `paused`
    (never left at `running`, the real bug this replaces: an earlier draft never assigned this,
    leaving paused nodes indistinguishable on resume from a genuinely-interrupted node).

    `execute_node(label)` MUST catch every terminal condition internally and return one of
    "satisfied" / "failing" / "paused" -- never raise. A real exception escaping it is NOT caught
    here, matching the design's own "no silent downgrade" discipline; it propagates to this
    function's own caller exactly like an uncaught exception always would.

    Returns `blocked_by`: `{failed_label: set_of_labels_it_blocked}` for every failure this run
    produced -- callers needing to retry a specific failed label pass its own recorded set
    straight to `unblock_transitive_dependents()`.
    """
    effective_max_concurrent = OMA_GRAPH_MAX_CONCURRENT_NODES if max_concurrent is None else max_concurrent
    effective_max_concurrent = min(effective_max_concurrent, OMA_GRAPH_HARD_MAX_CONCURRENT_NODES)
    if effective_max_concurrent > 1 and not (force_allow_concurrency and _CONCURRENT_WRITE_ISOLATION_LANDED):
        raise ConcurrencyNotLandedError(
            f"OMA_GRAPH_MAX_CONCURRENT_NODES={effective_max_concurrent} requested but concurrent "
            "node execution is not enabled for production use (Phase B, §9, is a separate, later, "
            "measured decision) -- this is a deliberate gate, not a bug."
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
            # Real bug found live building the Phase 31 UI (2026-08-08): this used to set
            # `ConstraintNodeState.ready` here and never assign `.running` anywhere in this
            # module (confirmed: `grep -rn ConstraintNodeState.running manager/` returned
            # nothing before this fix) -- so a node genuinely mid-execution persisted as
            # `ready`, which the UI's `normalizeConstraintNodeStatus()` maps to flat-gray
            # `pending`, identical to a node that hasn't started at all. `ready_labels()` only
            # ever reads OTHER nodes' `pending`/`satisfied` state to decide readiness, and this
            # label is excluded from re-dispatch purely via the `in_flight` dict membership
            # check below -- never by its own `.state` value -- so persisting `running` instead
            # of `ready` here is safe and changes no scheduling behavior, only what's genuinely
            # true gets recorded.
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
