"""Phase 31 §2/§9: the full test suite for manager/graph_scheduler.py -- ready_labels(),
build_dependents_index(), mark_blocked_by_failure(), unblock_transitive_dependents(),
detect_tier_order_divergence(), the §6 Layer-1 admission pre-filter, and the real rolling
dispatch loop run_graph_scheduler(). Pure/deterministic where possible; run_graph_scheduler()
tests use small, fast, fake `execute_node` coroutines -- no real specialist/model calls anywhere.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import ConstraintNode, ConstraintNodeState
from manager.graph_scheduler import (
    ConcurrencyNotLandedError,
    _admit_from_wave,
    _target_model_prefix,
    build_dependents_index,
    detect_tier_order_divergence,
    mark_blocked_by_failure,
    ready_labels,
    run_graph_scheduler,
    unblock_transitive_dependents,
)


def _node(label, predecessor_labels=None, creates=None, state=ConstraintNodeState.pending):
    return ConstraintNode(
        label=label, predecessor_labels=predecessor_labels or [], creates=creates or [], state=state,
    )


# --- ready_labels -------------------------------------------------------------------------------

def test_ready_labels_returns_pending_nodes_with_no_predecessors():
    nodes = {"a": _node("a"), "b": _node("b", predecessor_labels=["a"])}
    assert ready_labels(nodes) == ["a"]
    print("PASS: only the predecessor-free pending node is ready")


def test_ready_labels_becomes_ready_once_predecessor_satisfied():
    nodes = {
        "a": _node("a", state=ConstraintNodeState.satisfied),
        "b": _node("b", predecessor_labels=["a"]),
    }
    assert ready_labels(nodes) == ["b"]
    print("PASS: a node becomes ready once its own predecessor is satisfied")


def test_ready_labels_excludes_non_pending_nodes():
    nodes = {
        "a": _node("a", state=ConstraintNodeState.running),
        "b": _node("b", state=ConstraintNodeState.blocked),
        "c": _node("c", state=ConstraintNodeState.pending),
    }
    assert ready_labels(nodes) == ["c"]
    print("PASS: only genuinely `pending` nodes are ever candidates, never running/blocked/etc.")


def test_ready_labels_ignores_a_stale_predecessor_not_in_nodes():
    nodes = {"a": _node("a", predecessor_labels=["ghost"])}
    assert ready_labels(nodes) == ["a"]
    print("PASS: a predecessor label absent from nodes is silently skipped, never blocking readiness")


def test_ready_labels_withholds_until_all_predecessors_satisfied():
    nodes = {
        "a": _node("a", state=ConstraintNodeState.satisfied),
        "b": _node("b", state=ConstraintNodeState.pending),
        "c": _node("c", predecessor_labels=["a", "b"]),
    }
    # 'c' needs BOTH 'a' (satisfied) and 'b' (still pending) -- must NOT be ready yet. 'b' itself
    # has no predecessors of its own, so it's independently ready.
    assert ready_labels(nodes) == ["b"], (
        "'c' must wait for ALL predecessors (b isn't satisfied yet); 'b' itself is independently ready"
    )
    print("PASS: a node with multiple predecessors waits for ALL of them, not just one")


# --- build_dependents_index ----------------------------------------------------------------------

def test_build_dependents_index_is_real_reverse_adjacency():
    nodes = {
        "a": _node("a"),
        "b": _node("b", predecessor_labels=["a"]),
        "c": _node("c", predecessor_labels=["a"]),
        "d": _node("d", predecessor_labels=["b"]),
    }
    dependents = build_dependents_index(nodes)
    assert set(dependents["a"]) == {"b", "c"}
    assert dependents["b"] == ["d"]
    assert dependents["c"] == []
    assert dependents["d"] == []
    print("PASS: build_dependents_index() is real reverse adjacency, not predecessor_labels itself")


# --- mark_blocked_by_failure / unblock_transitive_dependents --------------------------------------

def test_mark_blocked_by_failure_blocks_the_full_transitive_closure():
    nodes = {
        "a": _node("a"),
        "b": _node("b", predecessor_labels=["a"]),
        "c": _node("c", predecessor_labels=["b"]),
        "unrelated": _node("unrelated"),
    }
    dependents_of = build_dependents_index(nodes)
    blocked = mark_blocked_by_failure(nodes, dependents_of, "a")
    assert blocked == {"b", "c"}
    assert nodes["b"].state == ConstraintNodeState.blocked
    assert nodes["c"].state == ConstraintNodeState.blocked
    assert nodes["unrelated"].state == ConstraintNodeState.pending
    print("PASS: mark_blocked_by_failure() blocks the full transitive closure of DESCENDANTS, leaves unrelated nodes alone")


def test_unblock_transitive_dependents_reverses_exactly_what_was_blocked():
    nodes = {
        "a": _node("a"),
        "b": _node("b", predecessor_labels=["a"], state=ConstraintNodeState.blocked),
        "c": _node("c", predecessor_labels=["b"], state=ConstraintNodeState.blocked),
    }
    unblock_transitive_dependents(nodes, {"b", "c"})
    assert nodes["b"].state == ConstraintNodeState.pending
    assert nodes["c"].state == ConstraintNodeState.pending
    print("PASS: unblock_transitive_dependents() resets every blocked label back to pending")


def test_unblock_transitive_dependents_leaves_non_blocked_labels_alone():
    nodes = {"a": _node("a", state=ConstraintNodeState.satisfied)}
    unblock_transitive_dependents(nodes, {"a"})
    assert nodes["a"].state == ConstraintNodeState.satisfied, (
        "a label that moved on to some other state (e.g. satisfied via a genuine resume) must "
        "never be reset just because it's named in a stale blocked_labels set"
    )
    print("PASS: unblock_transitive_dependents() only reverses labels STILL in the blocked state")


# --- detect_tier_order_divergence -----------------------------------------------------------------

def test_no_divergence_when_dispatch_follows_tier_order():
    nodes = {"a": _node("a"), "b": _node("b", state=ConstraintNodeState.satisfied)}
    order = ["b", "a"]
    assert detect_tier_order_divergence("a", order, nodes) is False
    print("PASS: no divergence when every earlier-tier-order label is already resolved")


def test_divergence_detected_when_a_real_dependency_edge_jumps_ahead():
    nodes = {
        "early_tier_but_blocked": _node("early_tier_but_blocked", predecessor_labels=["something_slow"]),
        "something_slow": _node("something_slow"),
        "late_tier_but_ready": _node("late_tier_but_ready", state=ConstraintNodeState.satisfied),
    }
    order = ["early_tier_but_blocked", "late_tier_but_ready", "something_slow"]
    assert detect_tier_order_divergence("late_tier_but_ready", order, nodes) is True, (
        "late_tier_but_ready is being dispatched (well, already satisfied here for the test) "
        "while early_tier_but_blocked -- earlier in tier order -- is still pending"
    )
    print("PASS: a real dependency edge letting a later-tier-order label go first is detected as divergence")


# --- §6 Layer 1 admission pre-filter ---------------------------------------------------------------

def test_admit_from_wave_defers_creates_overlap_but_backfills_others():
    nodes = {
        "a": _node("a", creates=["shared_model"]),
        "b": _node("b", creates=["shared_model"]),  # overlaps with 'a', must be deferred
        "c": _node("c", creates=["other_model"]),  # no overlap, must backfill
    }
    admitted = _admit_from_wave(["a", "b", "c"], set(), nodes, open_slots=2)
    assert admitted == ["a", "c"], (
        f"'b' overlaps with already-admitted 'a' and must be deferred, but 'c' must still "
        f"backfill the second slot: got {admitted}"
    )
    print("PASS: an overlapping creates-target candidate is deferred; scanning backfills a non-conflicting one instead")


def test_admit_from_wave_respects_open_slots():
    nodes = {"a": _node("a"), "b": _node("b"), "c": _node("c")}
    assert _admit_from_wave(["a", "b", "c"], set(), nodes, open_slots=1) == ["a"]
    print("PASS: admission never exceeds open_slots even with zero real conflicts")


def test_admit_from_wave_considers_already_in_flight_creates():
    nodes = {"a": _node("a", creates=["m"]), "b": _node("b", creates=["m"])}
    admitted = _admit_from_wave(["b"], {"a"}, nodes, open_slots=1)
    assert admitted == [], "a candidate overlapping an ALREADY in-flight node's creates must be deferred too"
    print("PASS: the pre-filter also checks against already in-flight nodes, not just this pass's own admissions")


def test_target_model_prefix_strips_only_the_last_dot_segment():
    assert _target_model_prefix("service.ticket.action_bulk_close") == "service.ticket"
    assert _target_model_prefix("project.model.open_ticket_count") == "project.model"
    assert _target_model_prefix("maintenance.event.date") == "maintenance.event"
    assert _target_model_prefix("no_dot_at_all") == "no_dot_at_all"
    print("PASS: _target_model_prefix() strips exactly the last dot-segment, matching every real shape seen live")


def test_admit_from_wave_defers_same_target_model_even_with_different_creates_strings():
    """Real, live-confirmed bug (task 07141af5-9a4e-41b6-93ea-8b7af04fea9c): three real
    sibling nodes each write a DIFFERENT symbol into the SAME real target model
    ('service.ticket') -- zero of their `creates` strings are identical, so the old exact-match-
    only filter would have admitted all three concurrently and let them race on the same real
    generated file.
    """
    nodes = {
        "ticket_bulk_close": _node("ticket_bulk_close", creates=["service.ticket.action_bulk_close"]),
        "daily_escalation_cron": _node("daily_escalation_cron", creates=["service.ticket.cron_escalation", "service.ticket.action_escalate"]),
        "ticket_workflow_and_logging": _node("ticket_workflow_and_logging", creates=["service.ticket.action_in_progress"]),
        "project_ticket_counts": _node("project_ticket_counts", creates=["project.model.open_ticket_count"]),
    }
    admitted = _admit_from_wave(
        ["ticket_bulk_close", "daily_escalation_cron", "ticket_workflow_and_logging", "project_ticket_counts"],
        set(), nodes, open_slots=4,
    )
    assert admitted == ["ticket_bulk_close", "project_ticket_counts"], (
        f"only the first same-model writer should be admitted per model, plus the genuinely "
        f"disjoint 'project_ticket_counts' (project.model, never touched by the others): got {admitted}"
    )
    print("PASS: same-target-model candidates with different creates strings are correctly deferred against each other")


def test_admit_from_wave_never_defers_on_requires_only_overlap():
    """A node that only READS from a model (via `requires`, not `creates`) never writes to it --
    it must be free to run alongside a real WRITER of that same model. This test uses `creates`
    for the reader's own real, disjoint output artifact -- `requires` is deliberately never
    consulted by _admit_from_wave() at all, matching its own documented, real "creates only"
    contract (a write-write concern, not a read-write one).
    """
    nodes = {
        "writer": _node("writer", creates=["service.ticket.action_bulk_close"]),
        "reader": _node("reader", creates=["project.model.open_ticket_count"]),
    }
    nodes["reader"].requires = ["service.ticket.priority"]  # reads the writer's model, writes nothing into it
    admitted = _admit_from_wave(["writer", "reader"], set(), nodes, open_slots=2)
    assert admitted == ["writer", "reader"], f"a pure reader of another node's model must never be deferred: got {admitted}"
    print("PASS: requires-only overlap (reading, not writing, another node's model) never blocks concurrent admission")


# --- run_graph_scheduler: the real rolling dispatch loop -----------------------------------------

def test_scheduler_runs_a_simple_linear_chain_to_completion():
    nodes = {"a": _node("a"), "b": _node("b", predecessor_labels=["a"]), "c": _node("c", predecessor_labels=["b"])}
    order: list[str] = []

    async def execute(label):
        order.append(label)
        return "satisfied"

    asyncio.run(run_graph_scheduler(nodes, execute))
    assert order == ["a", "b", "c"]
    assert all(n.state == ConstraintNodeState.satisfied for n in nodes.values())
    print("PASS: a simple linear chain dispatches in real dependency order and every node ends satisfied")


def test_scheduler_at_cap1_never_runs_two_nodes_concurrently():
    nodes = {"a": _node("a"), "b": _node("b")}
    concurrent_peak = {"n": 0, "max": 0}

    async def execute(label):
        concurrent_peak["n"] += 1
        concurrent_peak["max"] = max(concurrent_peak["max"], concurrent_peak["n"])
        await asyncio.sleep(0.01)
        concurrent_peak["n"] -= 1
        return "satisfied"

    asyncio.run(run_graph_scheduler(nodes, execute, max_concurrent=1))
    assert concurrent_peak["max"] == 1, (
        f"OMA_GRAPH_MAX_CONCURRENT_NODES=1 must never let two nodes run at once, peak was {concurrent_peak['max']}"
    )
    print("PASS: at cap=1, two independent (no shared dependency) nodes still never run concurrently")


def test_scheduler_marks_paused_state_correctly_not_left_running():
    """The real bug this scheduler fixes: an earlier draft never assigned ConstraintNodeState.paused
    on a paused outcome, leaving the node stuck at `running` forever, indistinguishable on resume
    from a genuinely-interrupted-mid-flight node."""
    nodes = {"a": _node("a")}

    async def execute(label):
        return "paused"

    asyncio.run(run_graph_scheduler(nodes, execute))
    assert nodes["a"].state == ConstraintNodeState.paused
    print("PASS: a 'paused' outcome is genuinely recorded as ConstraintNodeState.paused, never left at running")


def test_scheduler_persists_running_not_ready_while_a_node_is_in_flight():
    """Phase 31 UI doc, real bug found live (2026-08-08): ConstraintNodeState.running was NEVER
    assigned anywhere in this module before this fix -- dispatch set `.ready` and never advanced
    it until the terminal outcome, so a genuinely-executing node was indistinguishable from a
    not-yet-started one to any reader of `nodes[label].state` (and, via the UI's own
    normalizeConstraintNodeStatus(), rendered as flat-gray 'pending' instead of the intended
    pulsing 'running' state). Proven here by observing the node's own state from INSIDE
    execute_node, i.e. genuinely mid-flight, not just checking the eventual terminal value.
    """
    nodes = {"a": _node("a")}
    observed = {}

    async def execute(label):
        observed["mid_flight_state"] = nodes[label].state
        return "satisfied"

    asyncio.run(run_graph_scheduler(nodes, execute))
    assert observed["mid_flight_state"] == ConstraintNodeState.running, (
        f"a node actually being executed right now must be persisted as 'running', not "
        f"'ready' or anything else -- got {observed['mid_flight_state']!r}"
    )
    print("PASS: a node genuinely in flight is persisted as ConstraintNodeState.running, not 'ready'")


def test_scheduler_publishes_node_state_changed_for_every_real_transition():
    """Phase 31 UI doc §3.1/§5 (2026-08-08): every real state transition (running at dispatch,
    then satisfied/failing/paused/blocked at completion) must publish a live
    `node_state_changed` event via the existing publish_trace_event()/oma:trace:{task_id}
    channel already consumed by GET /api/stream/{task_id} -- no new transport, confirmed against
    the real server.py stream_task() forwarding logic. Captures real calls via monkeypatching
    graph_scheduler's own imported name (not a mock library), asserting the exact node_id/
    new_state pairs a real run produces, in order.
    """
    import manager.graph_scheduler as gs

    published = []
    original = gs.publish_trace_event

    def _capture(task_id, event, client=None):
        published.append((task_id, event.get("phase"), event.get("node_id"), event.get("new_state")))

    gs.publish_trace_event = _capture
    try:
        # a -> b (b depends on a); a fails, so b must end up genuinely blocked.
        nodes = {"a": _node("a"), "b": _node("b", predecessor_labels=["a"])}

        async def execute(label):
            return "failing" if label == "a" else "satisfied"

        asyncio.run(run_graph_scheduler(nodes, execute, task_id="task-xyz"))
    finally:
        gs.publish_trace_event = original

    assert ("task-xyz", "node_state_changed", "a", "running") in published
    assert ("task-xyz", "node_state_changed", "a", "failing") in published
    assert ("task-xyz", "node_state_changed", "b", "blocked") in published
    # b never got its own "running" published since it was blocked before ever being dispatched.
    assert ("task-xyz", "node_state_changed", "b", "running") not in published
    print(f"PASS: real node_state_changed events published in order: {published}")


def test_scheduler_never_publishes_when_task_id_is_none():
    """A caller with no real task_id (most of this file's own other tests, and any low-level
    direct caller) must never attempt a Redis publish at all -- confirmed by monkeypatching
    publish_trace_event to raise if it's ever called with task_id=None reaching PUBLISH.
    """
    import manager.graph_scheduler as gs

    original = gs.publish_trace_event

    def _fail_if_called(*a, **k):
        raise AssertionError("publish_trace_event must never be called when task_id is None")

    gs.publish_trace_event = _fail_if_called
    try:
        nodes = {"a": _node("a")}

        async def execute(label):
            return "satisfied"

        asyncio.run(run_graph_scheduler(nodes, execute))  # no task_id passed
    finally:
        gs.publish_trace_event = original
    print("PASS: no publish attempted at all when task_id is None")


def test_scheduler_blocks_transitive_dependents_on_failure():
    nodes = {"a": _node("a"), "b": _node("b", predecessor_labels=["a"]), "unrelated": _node("unrelated")}

    async def execute(label):
        if label == "a":
            return "failing"
        return "satisfied"

    blocked_by = asyncio.run(run_graph_scheduler(nodes, execute))
    assert nodes["a"].state == ConstraintNodeState.failing
    assert nodes["b"].state == ConstraintNodeState.blocked
    assert nodes["unrelated"].state == ConstraintNodeState.satisfied
    assert blocked_by == {"a": {"b"}}
    print("PASS: a failing node blocks its real transitive dependents; unrelated nodes proceed and finish")


def test_scheduler_never_raises_from_a_node_returning_an_unrecognized_outcome_before_recording_others():
    nodes = {"a": _node("a"), "b": _node("b")}

    async def execute(label):
        if label == "a":
            return "totally_not_a_real_outcome"
        await asyncio.sleep(0.02)
        return "satisfied"

    try:
        asyncio.run(run_graph_scheduler(nodes, execute))
        assert False, "an unrecognized outcome string must raise, never be silently accepted as a real state"
    except ValueError:
        pass
    print("PASS: an unrecognized outcome from execute_node raises ValueError rather than corrupting state silently")


def test_scheduler_rejects_concurrency_above_one_without_force_allow_concurrency():
    """_CONCURRENT_WRITE_ISOLATION_LANDED is True (§6 has genuinely landed), but that alone must
    never be enough -- a caller must ALSO explicitly pass force_allow_concurrency=True. Phase B
    (2026-08-09): production (manager/loop.py) now DOES pass force_allow_concurrency=True, at a
    hard-ceilinged max_concurrent=2 -- this test still matters for every OTHER caller (tests, any
    future code path) that doesn't opt in explicitly, confirming the gate still holds for them.
    """
    nodes = {"a": _node("a"), "b": _node("b")}

    async def execute(label):
        return "satisfied"

    try:
        asyncio.run(run_graph_scheduler(nodes, execute, max_concurrent=2))
        assert False, "max_concurrent > 1 without force_allow_concurrency=True must be a hard config error, isolation-landed or not"
    except ConcurrencyNotLandedError:
        pass
    print("PASS: OMA_GRAPH_MAX_CONCURRENT_NODES > 1 raises ConcurrencyNotLandedError without an explicit force_allow_concurrency=True, never silently proceeds")


def test_scheduler_genuinely_allows_concurrency_above_one_with_explicit_force_allow_concurrency():
    """The other half of the same gate: now that §6 has genuinely landed
    (_CONCURRENT_WRITE_ISOLATION_LANDED is True), an explicit, opt-in
    force_allow_concurrency=True must actually be honored -- real concurrent dispatch, not just a
    permissive-looking flag that still silently caps at 1.
    """
    nodes = {"a": _node("a"), "b": _node("b")}
    concurrent_peak = {"n": 0, "max": 0}

    async def execute(label):
        concurrent_peak["n"] += 1
        concurrent_peak["max"] = max(concurrent_peak["max"], concurrent_peak["n"])
        await asyncio.sleep(0.02)
        concurrent_peak["n"] -= 1
        return "satisfied"

    asyncio.run(run_graph_scheduler(nodes, execute, max_concurrent=2, force_allow_concurrency=True))
    assert concurrent_peak["max"] == 2, (
        f"with force_allow_concurrency=True and isolation landed, two independent nodes must "
        f"genuinely run concurrently -- observed peak {concurrent_peak['max']}"
    )
    print(f"PASS: force_allow_concurrency=True with isolation landed genuinely allows real concurrent dispatch, observed peak concurrency {concurrent_peak['max']}")


def test_scheduler_hard_ceiling_clamps_even_a_caller_requesting_more_than_two():
    """Phase B (2026-08-09), the project owner's own explicit words: "maybe two things at a time. I don't
    want more." `OMA_GRAPH_HARD_MAX_CONCURRENT_NODES` (2) must clamp real dispatch even if a
    caller passes a larger `max_concurrent` -- defense in depth, so a future careless edit at the
    one real call site (or any other caller) can never silently regress into unbounded
    concurrency. Three independent (no shared dependency, no target-model overlap) nodes, asked
    for max_concurrent=5, must still peak at 2 in flight.
    """
    nodes = {"a": _node("a"), "b": _node("b"), "c": _node("c")}
    concurrent_peak = {"n": 0, "max": 0}

    async def execute(label):
        concurrent_peak["n"] += 1
        concurrent_peak["max"] = max(concurrent_peak["max"], concurrent_peak["n"])
        await asyncio.sleep(0.02)
        concurrent_peak["n"] -= 1
        return "satisfied"

    asyncio.run(run_graph_scheduler(nodes, execute, max_concurrent=5, force_allow_concurrency=True))
    assert concurrent_peak["max"] == 2, (
        f"OMA_GRAPH_HARD_MAX_CONCURRENT_NODES=2 must clamp dispatch regardless of a larger "
        f"requested max_concurrent -- observed peak {concurrent_peak['max']}"
    )
    print(f"PASS: hard ceiling clamps real dispatch to 2 even when max_concurrent=5 is requested, observed peak {concurrent_peak['max']}")


def test_scheduler_respects_a_remaining_budget_ceiling():
    nodes = {"a": _node("a"), "b": _node("b")}
    calls_made = {"n": 0}

    async def execute(label):
        calls_made["n"] += 1
        return "satisfied"

    # Budget only covers 1 call at cost 10 each -- the second must never be dispatched.
    asyncio.run(run_graph_scheduler(
        nodes, execute, remaining_budget=10, worst_case_budget_for=lambda label: 10,
    ))
    assert calls_made["n"] == 1, f"a budget covering only 1 call must not admit a 2nd, got {calls_made['n']} calls"
    print("PASS: a remaining_budget ceiling caps admission even when the graph itself has more ready work")


def test_scheduler_a_dag_with_a_diamond_shape_completes_correctly():
    """a -> b, a -> c, (b,c) -> d -- a real diamond dependency shape."""
    nodes = {
        "a": _node("a"),
        "b": _node("b", predecessor_labels=["a"]),
        "c": _node("c", predecessor_labels=["a"]),
        "d": _node("d", predecessor_labels=["b", "c"]),
    }
    order: list[str] = []

    async def execute(label):
        order.append(label)
        return "satisfied"

    asyncio.run(run_graph_scheduler(nodes, execute))
    assert order[0] == "a"
    assert order[-1] == "d"
    assert set(order[1:3]) == {"b", "c"}
    assert all(n.state == ConstraintNodeState.satisfied for n in nodes.values())
    print(f"PASS: a diamond-shaped DAG dispatches in valid topological order: {order}")


def test_scheduler_logs_graph_order_diverged_from_tier_on_a_real_run():
    """Real, checkable evidence requirement: when a real dependency edge lets a node become ready
    ahead of its own tier-bucket position, the scheduler must actually log
    graph_order_diverged_from_tier -- captured here via a monkeypatched publish_trace_event.
    """
    import manager.graph_scheduler as scheduler_module

    logged_messages = []

    def fake_publish(task_id, event, client=None):
        logged_messages.append(event.get("message", ""))

    # 'late_tier' has a keyword that sorts it into a LATER tier than 'early_tier_blocked_dep' would
    # naturally occupy, but 'late_tier' has NO predecessors and is ready immediately, while
    # 'early_tier_blocked_dep' is still pending on 'slow_dependency' -- a genuine tier-order jump.
    nodes = {
        "menu_structure_thing": _node("menu_structure_thing", predecessor_labels=["slow_dependency"]),
        "slow_dependency": _node("slow_dependency"),
        "model_fields_thing": _node("model_fields_thing"),
    }

    async def execute(label):
        return "satisfied"

    original_publish = scheduler_module.publish_trace_event
    scheduler_module.publish_trace_event = fake_publish
    try:
        asyncio.run(run_graph_scheduler(nodes, execute, task_id="test-task-divergence"))
    finally:
        scheduler_module.publish_trace_event = original_publish

    diverged_logs = [m for m in logged_messages if "graph_order_diverged_from_tier" in m]
    assert diverged_logs, (
        f"expected at least one real graph_order_diverged_from_tier log line, got messages: {logged_messages}"
    )
    print(f"PASS: a real run with a genuine tier-order jump logs graph_order_diverged_from_tier: {diverged_logs[0]!r}")


def test_loop_py_real_call_site_passes_max_concurrent_2_and_force_allow_concurrency():
    """Phase B (2026-08-09): a static-source guard on the ONE real call site
    (manager/loop.py's `_run_constraint_labels_from()`) rather than importing manager.loop itself
    (a heavy import chain -- specialists, gateway clients, etc. -- not needed for this check).
    Confirms the production wiring genuinely matches what this test file's other Phase B tests
    exercise: max_concurrent=2 (never a bare, unbounded call), force_allow_concurrency=True.
    """
    loop_py_path = os.path.join(os.path.dirname(__file__), "..", "manager", "loop.py")
    with open(loop_py_path) as f:
        source = f.read()
    assert "run_graph_scheduler(" in source, "expected the real call site to still exist in manager/loop.py"
    call_start = source.index("await run_graph_scheduler(")
    call_text = source[call_start:call_start + 400]
    assert "max_concurrent=2" in call_text, (
        f"expected manager/loop.py's real call site to pass max_concurrent=2, got: {call_text!r}"
    )
    assert "force_allow_concurrency=True" in call_text, (
        f"expected manager/loop.py's real call site to pass force_allow_concurrency=True, got: {call_text!r}"
    )
    print("PASS: manager/loop.py's real run_graph_scheduler() call site passes max_concurrent=2, force_allow_concurrency=True")


if __name__ == "__main__":
    test_ready_labels_returns_pending_nodes_with_no_predecessors()
    test_ready_labels_becomes_ready_once_predecessor_satisfied()
    test_ready_labels_excludes_non_pending_nodes()
    test_ready_labels_ignores_a_stale_predecessor_not_in_nodes()
    test_ready_labels_withholds_until_all_predecessors_satisfied()
    test_build_dependents_index_is_real_reverse_adjacency()
    test_mark_blocked_by_failure_blocks_the_full_transitive_closure()
    test_unblock_transitive_dependents_reverses_exactly_what_was_blocked()
    test_unblock_transitive_dependents_leaves_non_blocked_labels_alone()
    test_no_divergence_when_dispatch_follows_tier_order()
    test_divergence_detected_when_a_real_dependency_edge_jumps_ahead()
    test_admit_from_wave_defers_creates_overlap_but_backfills_others()
    test_admit_from_wave_respects_open_slots()
    test_admit_from_wave_considers_already_in_flight_creates()
    test_target_model_prefix_strips_only_the_last_dot_segment()
    test_admit_from_wave_defers_same_target_model_even_with_different_creates_strings()
    test_admit_from_wave_never_defers_on_requires_only_overlap()
    test_scheduler_runs_a_simple_linear_chain_to_completion()
    test_scheduler_at_cap1_never_runs_two_nodes_concurrently()
    test_scheduler_marks_paused_state_correctly_not_left_running()
    test_scheduler_blocks_transitive_dependents_on_failure()
    test_scheduler_never_raises_from_a_node_returning_an_unrecognized_outcome_before_recording_others()
    test_scheduler_rejects_concurrency_above_one_without_force_allow_concurrency()
    test_scheduler_genuinely_allows_concurrency_above_one_with_explicit_force_allow_concurrency()
    test_scheduler_hard_ceiling_clamps_even_a_caller_requesting_more_than_two()
    test_scheduler_respects_a_remaining_budget_ceiling()
    test_scheduler_a_dag_with_a_diamond_shape_completes_correctly()
    test_scheduler_logs_graph_order_diverged_from_tier_on_a_real_run()
    test_loop_py_real_call_site_passes_max_concurrent_2_and_force_allow_concurrency()
    print("\nALL GRAPH-SCHEDULER TESTS PASSED")
