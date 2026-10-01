"""Phase 31 §8/§9/§10: closes the telemetry gaps flagged in independent full-suite verification
(2026-08-07) -- real tests proving `graph_cycle_detected` (the corrected name),
`architect_draft_selected`/`architect_drafts_merged`, and the three recursion-cap events
(`architect_recursion_depth_capped`, `architect_recursion_call_budget_exhausted`,
`architect_recursion_node_cap_reached`) are all real, wired, and actually fire -- not just that
the underlying caps stop splitting (already proven in tests/test_recursive_decomposition.py).
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import ConstraintNode
from manager.loop import detect_and_collapse_cycles_with_telemetry
from manager.replanning import (
    _ConstraintArtifacts,
    _ConstraintDecomposition,
    _RecursionBudget,
    critique_and_merge_constraint_drafts,
    maybe_decompose_piece_further,
)


def _capture_trace_events(module):
    logged = []

    def fake_publish(task_id, event, client=None):
        logged.append(event.get("message", ""))

    return logged, fake_publish


# --- graph_cycle_detected (renamed from the undocumented graph_cycle_collapsed) -----------------

def test_graph_cycle_detected_fires_with_correct_name_and_carries_synthetic_edges():
    import manager.loop as loop_module

    logged, fake_publish = _capture_trace_events(loop_module)
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=["c"]),
        "b": ConstraintNode(label="b", predecessor_labels=["a"]),
        "c": ConstraintNode(label="c", predecessor_labels=["b"]),
    }
    original_publish = loop_module.publish_trace_event
    loop_module.publish_trace_event = fake_publish
    try:
        detect_and_collapse_cycles_with_telemetry(nodes, "task-1")
    finally:
        loop_module.publish_trace_event = original_publish

    cycle_logs = [m for m in logged if "graph_cycle_detected" in m]
    assert cycle_logs, f"expected a graph_cycle_detected log, got: {logged}"
    assert "graph_cycle_collapsed" not in " ".join(logged), "the old, undocumented event name must never fire"
    assert "synthetic edges" in cycle_logs[0]
    print(f"PASS: graph_cycle_detected fires with the design-doc-correct name and names the synthetic edges: {cycle_logs[0]!r}")


def test_graph_cycle_detected_never_fires_for_an_acyclic_graph():
    import manager.loop as loop_module

    logged, fake_publish = _capture_trace_events(loop_module)
    nodes = {"a": ConstraintNode(label="a"), "b": ConstraintNode(label="b", predecessor_labels=["a"])}
    original_publish = loop_module.publish_trace_event
    loop_module.publish_trace_event = fake_publish
    try:
        detect_and_collapse_cycles_with_telemetry(nodes, "task-1")
    finally:
        loop_module.publish_trace_event = original_publish

    assert not logged, f"a real DAG must never log graph_cycle_detected: got {logged}"
    print("PASS: a real DAG (no cycle) never fires graph_cycle_detected")


# --- architect_draft_selected / architect_drafts_merged -----------------------------------------

def _artifacts(*labels):
    return [_ConstraintArtifacts(label=label) for label in labels]


def test_architect_draft_selected_fires_when_critic_picks_one_draft_verbatim():
    import manager.replanning as replanning_module

    logged, fake_publish = _capture_trace_events(replanning_module)
    baseline = _artifacts("a", "b")
    draft2 = _artifacts("c")

    async def fake_call_structured(**kwargs):
        return _ConstraintDecomposition(constraints=baseline)  # critic picks baseline verbatim

    async def run():
        with patch("manager.replanning.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            return await critique_and_merge_constraint_drafts("goal", [baseline, draft2], client=None, model="x", task_id="t1")

    original_publish = replanning_module.publish_trace_event
    replanning_module.publish_trace_event = fake_publish
    try:
        asyncio.run(run())
    finally:
        replanning_module.publish_trace_event = original_publish

    selected_logs = [m for m in logged if "architect_draft_selected" in m]
    assert selected_logs, f"expected architect_draft_selected, got: {logged}"
    assert "draft 1/2" in selected_logs[0]
    print(f"PASS: architect_draft_selected fires with the correct draft index when the critic picks one verbatim: {selected_logs[0]!r}")


def test_architect_drafts_merged_fires_when_critic_synthesizes_a_new_list():
    import manager.replanning as replanning_module

    logged, fake_publish = _capture_trace_events(replanning_module)
    baseline = _artifacts("a", "b")
    draft2 = _artifacts("c")

    async def fake_call_structured(**kwargs):
        return _ConstraintDecomposition(constraints=_artifacts("merged_x", "merged_y"))

    async def run():
        with patch("manager.replanning.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            return await critique_and_merge_constraint_drafts("goal", [baseline, draft2], client=None, model="x", task_id="t1")

    original_publish = replanning_module.publish_trace_event
    replanning_module.publish_trace_event = fake_publish
    try:
        result = asyncio.run(run())
    finally:
        replanning_module.publish_trace_event = original_publish

    merged_logs = [m for m in logged if "architect_drafts_merged" in m]
    assert merged_logs, f"expected architect_drafts_merged, got: {logged}"
    assert [i.label for i in result] == ["merged_x", "merged_y"]
    print(f"PASS: architect_drafts_merged fires when the critic's own output matches no single input draft: {merged_logs[0]!r}")


# --- recursion-cap telemetry ----------------------------------------------------------------------

def test_architect_recursion_depth_capped_fires():
    import manager.replanning as replanning_module

    logged, fake_publish = _capture_trace_events(replanning_module)
    piece = _ConstraintArtifacts(label="deep_piece", creates=["a", "b"])
    budget = _RecursionBudget(max_depth=1, max_calls=100, max_nodes=100, starting_node_count=1)

    original_publish = replanning_module.publish_trace_event
    replanning_module.publish_trace_event = fake_publish
    try:
        result = asyncio.run(maybe_decompose_piece_further(
            piece, "goal", client=None, model="x", budget=budget, depth=1,
        ))
    finally:
        replanning_module.publish_trace_event = original_publish

    assert result == [piece]
    capped_logs = [m for m in logged if "architect_recursion_depth_capped" in m]
    assert capped_logs, f"expected architect_recursion_depth_capped, got: {logged}"
    print(f"PASS: architect_recursion_depth_capped fires when the depth cap stops splitting: {capped_logs[0]!r}")


def test_architect_recursion_call_budget_exhausted_fires():
    import manager.replanning as replanning_module

    logged, fake_publish = _capture_trace_events(replanning_module)
    piece = _ConstraintArtifacts(label="piece", creates=["a", "b"])
    budget = _RecursionBudget(max_depth=10, max_calls=0, max_nodes=100, starting_node_count=1)

    original_publish = replanning_module.publish_trace_event
    replanning_module.publish_trace_event = fake_publish
    try:
        result = asyncio.run(maybe_decompose_piece_further(
            piece, "goal", client=None, model="x", budget=budget,
        ))
    finally:
        replanning_module.publish_trace_event = original_publish

    assert result == [piece]
    exhausted_logs = [m for m in logged if "architect_recursion_call_budget_exhausted" in m]
    assert exhausted_logs, f"expected architect_recursion_call_budget_exhausted, got: {logged}"
    print(f"PASS: architect_recursion_call_budget_exhausted fires when the call budget is already spent: {exhausted_logs[0]!r}")


def test_architect_recursion_node_cap_reached_fires():
    import manager.replanning as replanning_module

    logged, fake_publish = _capture_trace_events(replanning_module)
    piece = _ConstraintArtifacts(label="piece", creates=["a", "b"])
    budget = _RecursionBudget(max_depth=10, max_calls=100, max_nodes=1, starting_node_count=1)

    original_publish = replanning_module.publish_trace_event
    replanning_module.publish_trace_event = fake_publish
    try:
        result = asyncio.run(maybe_decompose_piece_further(
            piece, "goal", client=None, model="x", budget=budget,
        ))
    finally:
        replanning_module.publish_trace_event = original_publish

    assert result == [piece]
    node_cap_logs = [m for m in logged if "architect_recursion_node_cap_reached" in m]
    assert node_cap_logs, f"expected architect_recursion_node_cap_reached, got: {logged}"
    print(f"PASS: architect_recursion_node_cap_reached fires when the node ceiling is already at/over budget: {node_cap_logs[0]!r}")


def test_no_cap_telemetry_fires_for_an_atomic_tier0_piece():
    """A Tier-0-atomic piece (signal count <=1) stops for a completely different, legitimate
    reason -- none of the three cap events must fire for it.
    """
    import manager.replanning as replanning_module

    logged, fake_publish = _capture_trace_events(replanning_module)
    piece = _ConstraintArtifacts(label="atomic", creates=["a"])
    budget = _RecursionBudget(max_depth=1, max_calls=1, max_nodes=1, starting_node_count=1)

    original_publish = replanning_module.publish_trace_event
    replanning_module.publish_trace_event = fake_publish
    try:
        asyncio.run(maybe_decompose_piece_further(piece, "goal", client=None, model="x", budget=budget))
    finally:
        replanning_module.publish_trace_event = original_publish

    assert not logged, f"a Tier-0-atomic piece must never log any recursion-cap event: got {logged}"
    print("PASS: no cap-telemetry event fires when a piece is simply Tier-0-atomic, never a cap")


if __name__ == "__main__":
    test_graph_cycle_detected_fires_with_correct_name_and_carries_synthetic_edges()
    test_graph_cycle_detected_never_fires_for_an_acyclic_graph()
    test_architect_draft_selected_fires_when_critic_picks_one_draft_verbatim()
    test_architect_drafts_merged_fires_when_critic_synthesizes_a_new_list()
    test_architect_recursion_depth_capped_fires()
    test_architect_recursion_call_budget_exhausted_fires()
    test_architect_recursion_node_cap_reached_fires()
    test_no_cap_telemetry_fires_for_an_atomic_tier0_piece()
    print("\nALL PHASE 31 TELEMETRY-GAP TESTS PASSED")
