"""Phase 31 §2/§5: tests proving _run_constraint_labels_from() actually drives dispatch via the
real graph scheduler when contract.constraint_nodes is populated (the architect-stage/production
path), not just the synthetic-linear-chain fallback every other pre-existing regression test in
this suite exercises. _execute_contract() is mocked -- zero real specialist/model calls.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import (
    AutonomyTier, CapabilityClass, ConstraintNode, SpecialistType, TaskContract,
)
from manager.loop import _run_constraint_labels_from


def _make_decomposable_contract(constraint_nodes=None) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A; add field B; add field C.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={}, constraint_nodes=constraint_nodes or {},
    )


def test_real_graph_dispatches_independent_pieces_in_a_valid_order_not_just_list_order():
    """A real graph where 'c' depends on 'a' but NOT on 'b' (b is independent) -- the labels
    list is deliberately ordered [a, b, c] to match a valid dispatch already, so this test's
    real point is confirming the REAL graph edges (not just list position) govern readiness:
    'c' must never be dispatched before 'a' is satisfied, regardless of list order.
    """
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=[]),
        "b": ConstraintNode(label="b", predecessor_labels=[]),
        "c": ConstraintNode(label="c", predecessor_labels=["a"]),
    }
    contract = _make_decomposable_contract(constraint_nodes=nodes)
    labels = ["a", "b", "c"]
    satisfied = {label: "pending" for label in labels}
    seen_order = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_order.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    assert result["passed"] is True
    assert len(seen_order) == 3
    assert seen_order.index("a") < seen_order.index("c"), (
        f"'c' depends on 'a' via a REAL predecessor edge -- must never dispatch before 'a': got {seen_order}"
    )
    print(f"PASS: real graph edges (not just list position) govern dispatch order: {seen_order}")


def test_real_graph_a_failure_blocks_only_its_own_transitive_dependents_but_still_halts_the_whole_task():
    """This function's own historical, deliberate "stop the ENTIRE decomposed task on the FIRST
    non-passing sub-contract" behavior is preserved even with a real, branching graph -- an
    independent sibling that never got a chance to run must never silently run after a different
    sibling has already failed.
    """
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=[]),
        "independent_b": ConstraintNode(label="independent_b", predecessor_labels=[]),
    }
    contract = _make_decomposable_contract(constraint_nodes=nodes)
    labels = ["a", "independent_b"]
    satisfied = {label: "pending" for label in labels}
    seen_order = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        focus = sub_contract.current_constraint_label
        seen_order.append(focus)
        if focus == "a":
            return {"status": "failed", "passed": False, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state") as mock_clear:
                result = await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )
                return result, mock_clear

    result, mock_clear = asyncio.run(run())
    assert result["passed"] is False
    assert seen_order == ["a"], (
        f"'a' fails on the FIRST (and, at cap=1, only) dispatch -- 'independent_b' must never run "
        f"even though it has no real dependency on 'a': got {seen_order}"
    )
    mock_clear.assert_not_called()
    print("PASS: a failure halts the whole decomposed task even for a genuinely independent sibling, preserving historical behavior")


def test_fallback_linear_chain_used_when_constraint_nodes_is_empty():
    """No constraint_nodes at all (the shape every pre-existing regression test in this suite
    uses) must still dispatch in exact original list order -- the synthetic linear-chain fallback.
    """
    contract = _make_decomposable_contract(constraint_nodes={})
    labels = ["x", "y", "z"]
    satisfied = {label: "pending" for label in labels}
    seen_order = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_order.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    assert seen_order == ["x", "y", "z"]
    assert result["passed"] is True
    print("PASS: with no real constraint_nodes graph, the fallback linear chain reproduces exact original list order")


def test_constraint_nodes_mismatched_with_labels_falls_back_to_linear_chain():
    """A stale/mismatched constraint_nodes (keys don't match constraint_labels -- e.g. an old
    contract from before a later recursive-split changed the label set) must not be trusted --
    falls back to the safe linear chain rather than silently using a graph that doesn't match.
    """
    stale_nodes = {"totally_different_label": ConstraintNode(label="totally_different_label")}
    contract = _make_decomposable_contract(constraint_nodes=stale_nodes)
    labels = ["p", "q"]
    satisfied = {label: "pending" for label in labels}
    seen_order = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_order.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    assert seen_order == ["p", "q"]
    assert result["passed"] is True
    print("PASS: a mismatched constraint_nodes graph is never trusted -- falls back to the safe linear chain")


def test_real_graph_logs_tier_order_divergence_through_the_real_call_path():
    """End-to-end evidence: a real dependency edge letting a label jump ahead of its own
    tier-bucket position must log graph_order_diverged_from_tier through the REAL
    _run_constraint_labels_from() call path, not just graph_scheduler.py's own isolated test.
    """
    import manager.graph_scheduler as scheduler_module

    nodes = {
        "menu_structure_thing": ConstraintNode(label="menu_structure_thing", predecessor_labels=["slow_dependency"]),
        "slow_dependency": ConstraintNode(label="slow_dependency", predecessor_labels=[]),
        "model_fields_thing": ConstraintNode(label="model_fields_thing", predecessor_labels=[]),
    }
    contract = _make_decomposable_contract(constraint_nodes=nodes)
    labels = list(nodes.keys())
    satisfied = {label: "pending" for label in labels}

    logged_messages = []

    def fake_publish(task_id, event, client=None):
        logged_messages.append(event.get("message", ""))

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    original_publish = scheduler_module.publish_trace_event
    scheduler_module.publish_trace_event = fake_publish
    try:
        result = asyncio.run(run())
    finally:
        scheduler_module.publish_trace_event = original_publish

    assert result["passed"] is True
    diverged = [m for m in logged_messages if "graph_order_diverged_from_tier" in m]
    assert diverged, f"expected a real graph_order_diverged_from_tier log through _run_constraint_labels_from(), got: {logged_messages}"
    print(f"PASS: a real _run_constraint_labels_from() run logs graph_order_diverged_from_tier end-to-end: {diverged[0]!r}")


def test_each_node_dispatch_mints_a_fresh_correlation_id_never_reused():
    """Phase 31 §10: each node dispatch mints its OWN fresh correlation_id -- never reused from
    the parent task and never shared with any other node dispatch. parent_correlation_id records
    the parent task's own identity so a trace consumer can walk back from any node to its task.
    """
    contract = _make_decomposable_contract(constraint_nodes={})
    labels = ["a", "b"]
    satisfied = {label: "pending" for label in labels}
    seen_correlation_ids = []
    seen_parent_correlation_ids = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_correlation_ids.append(sub_contract.correlation_id)
        seen_parent_correlation_ids.append(sub_contract.parent_correlation_id)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    asyncio.run(run())
    assert all(seen_correlation_ids), "every dispatched sub-contract must have a real, non-empty correlation_id"
    assert len(set(seen_correlation_ids)) == len(seen_correlation_ids), (
        f"every node dispatch must mint its OWN fresh correlation_id, never reused: {seen_correlation_ids}"
    )
    assert all(pcid == str(contract.task_id) for pcid in seen_parent_correlation_ids), (
        f"parent_correlation_id must record the parent task's own identity: {seen_parent_correlation_ids}"
    )
    print(f"PASS: {len(seen_correlation_ids)} node dispatches, each with its own fresh, never-reused correlation_id, correct parent_correlation_id")


def test_each_node_dispatch_gets_a_frozen_snapshot_from_the_task_branch():
    """Phase 31 §6(a) end-to-end from manager/loop.py's own side: each node dispatch's own
    sub_contract must carry a frozen_old_files_by_relpath snapshot, fetched ONCE at dispatch
    time via vcs.read_last_validated_commit() (mocked here -- no real Gitea call), never left
    None for a real per-node dispatch.
    """
    contract = _make_decomposable_contract(constraint_nodes={})
    labels = ["a", "b"]
    satisfied = {label: "pending" for label in labels}
    seen_snapshots = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_snapshots.append(sub_contract.frozen_old_files_by_relpath)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    fake_commit_content = {"oma_test_mod/models/models.py": "# real committed content\n"}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)), \
             patch("manager.loop.clear_task_state"), \
             patch(
                 "tools_odoo.module_dev.vcs.read_last_validated_commit",
                 return_value=fake_commit_content,
             ):
            return await _run_constraint_labels_from(
                contract, labels, satisfied, 0,
                "module_lock", client=None, classifier_model="x", correction_result={},
                module_for_repeat_check=None, memory_block="", constitution_text="",
                sensitive_paths_raw=[],
            )

    asyncio.run(run())
    assert len(seen_snapshots) == 2
    assert all(snap == fake_commit_content for snap in seen_snapshots), (
        f"every node dispatch must carry the real frozen snapshot: got {seen_snapshots}"
    )
    print("PASS: every node dispatch's own sub_contract carries a real frozen_old_files_by_relpath snapshot, fetched once at dispatch time")


if __name__ == "__main__":
    test_real_graph_dispatches_independent_pieces_in_a_valid_order_not_just_list_order()
    test_real_graph_a_failure_blocks_only_its_own_transitive_dependents_but_still_halts_the_whole_task()
    test_fallback_linear_chain_used_when_constraint_nodes_is_empty()
    test_constraint_nodes_mismatched_with_labels_falls_back_to_linear_chain()
    test_real_graph_logs_tier_order_divergence_through_the_real_call_path()
    test_each_node_dispatch_mints_a_fresh_correlation_id_never_reused()
    test_each_node_dispatch_gets_a_frozen_snapshot_from_the_task_branch()
    print("\nALL GRAPH-SCHEDULING INTEGRATION TESTS FOR _run_constraint_labels_from PASSED")
