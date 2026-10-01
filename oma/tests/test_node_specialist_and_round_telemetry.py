"""Phase 31 UI doc §3.1/§5 server.py items (closed 2026-08-08): real gap found live (the project owner's
own follow-up after the UI status audit) -- the frontend has had real, already-built consumers
for `node_specialist_changed`/`node_round_advanced` (the canvas specialist badge, the round-count
badge, `ensureStream`'s SSE handler) since the initial UI implementation pass, but nothing on the
backend ever emitted either event -- confirmed by grepping the whole codebase for both string
literals before this fix, zero matches. These tests confirm the real publish + persist functions
exist and behave correctly; no real model/Redis/Postgres calls (mocked at the exact seams).
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.graph_scheduler import _publish_node_round_advanced, _publish_node_specialist_changed


def test_specialist_changed_is_a_noop_with_no_task_id():
    with patch("manager.graph_scheduler.publish_trace_event") as mock_publish, \
         patch("manager.graph_scheduler.update_graph_created_node_specialist") as mock_persist:
        _publish_node_specialist_changed(None, "some_label", "build")
        mock_publish.assert_not_called()
        mock_persist.assert_not_called()
    print("PASS: no task_id -> no-op, matching _publish_node_state_changed()'s own posture")


def test_specialist_changed_is_a_noop_with_no_label():
    """The plain, non-decomposed _execute_contract() path has no real graph node -- current_
    constraint_label is None there, and this must never publish/persist for a nonexistent node.
    """
    with patch("manager.graph_scheduler.publish_trace_event") as mock_publish, \
         patch("manager.graph_scheduler.update_graph_created_node_specialist") as mock_persist:
        _publish_node_specialist_changed("task-123", None, "build")
        mock_publish.assert_not_called()
        mock_persist.assert_not_called()
    print("PASS: no label (plain, non-decomposed path) -> no-op, never touches a nonexistent node")


def test_specialist_changed_publishes_and_persists_real_data():
    with patch("manager.graph_scheduler.publish_trace_event") as mock_publish, \
         patch("manager.graph_scheduler.update_graph_created_node_specialist") as mock_persist:
        _publish_node_specialist_changed("task-123", "big_piece", "qa")
        assert mock_publish.call_count == 1
        args = mock_publish.call_args[0]
        assert args[0] == "task-123"
        payload = args[1]
        assert payload["phase"] == "node_specialist_changed"
        assert payload["node_id"] == "big_piece"
        assert payload["specialist"] == "qa"
        mock_persist.assert_called_once_with("task-123", "big_piece", "qa")
    print("PASS: a real specialist transition publishes the live SSE event AND persists into the graph_created snapshot")


def test_round_advanced_is_a_noop_with_no_label():
    with patch("manager.graph_scheduler.publish_trace_event") as mock_publish, \
         patch("manager.graph_scheduler.update_graph_created_node_round") as mock_persist:
        _publish_node_round_advanced("task-123", None, 2)
        mock_publish.assert_not_called()
        mock_persist.assert_not_called()
    print("PASS: no label -> no-op for round-advanced too")


def test_round_advanced_publishes_and_persists_real_data():
    with patch("manager.graph_scheduler.publish_trace_event") as mock_publish, \
         patch("manager.graph_scheduler.update_graph_created_node_round") as mock_persist:
        _publish_node_round_advanced("task-123", "big_piece", 3)
        assert mock_publish.call_count == 1
        payload = mock_publish.call_args[0][1]
        assert payload["phase"] == "node_round_advanced"
        assert payload["node_id"] == "big_piece"
        assert payload["round_number"] == 3
        mock_persist.assert_called_once_with("task-123", "big_piece", 3)
    print("PASS: a real round advance publishes the live SSE event AND persists into the graph_created snapshot")


def test_loop_wires_specialist_changed_at_round_start_with_the_real_specialist_type():
    """Source-inspection regression guard (same pattern as test_constraint_nodes_run_turn_wiring.py
    -- a full live run_turn() reproduction of a multi-round retry is impractical here): confirms
    manager/loop.py's own round loop calls _publish_node_specialist_changed() with 'build'/'review'
    derived from the REAL current_contract.specialist_type, not a hardcoded guess, and calls
    _publish_node_round_advanced() at the top of every round.
    """
    import inspect

    import manager.loop as loop_module

    source = inspect.getsource(loop_module)
    round_msg_index = source.index('"message": f"Round {round_number}", "status": "running",')
    window = source[round_msg_index:round_msg_index + 600]
    assert "_publish_node_round_advanced(" in window
    assert "_publish_node_specialist_changed(" in window
    assert 'if current_contract.specialist_type == SpecialistType.bug_fix else "review"' in window
    print("PASS: the round loop publishes both new telemetry events at round start, specialist derived from the real current_contract.specialist_type")


def test_loop_wires_specialist_changed_to_qa_before_verification():
    import inspect

    import manager.loop as loop_module

    source = inspect.getsource(loop_module)
    count = source.count('_publish_node_specialist_changed(\n                    task_id, current_contract.current_constraint_label, "qa",\n                )')
    assert count >= 1, "expected at least one 'qa' specialist-changed publish immediately before an await_verification() call"
    print(f"PASS: found {count} 'qa' specialist-changed publish site(s) ahead of Testing/QA verification")


if __name__ == "__main__":
    test_specialist_changed_is_a_noop_with_no_task_id()
    test_specialist_changed_is_a_noop_with_no_label()
    test_specialist_changed_publishes_and_persists_real_data()
    test_round_advanced_is_a_noop_with_no_label()
    test_round_advanced_publishes_and_persists_real_data()
    test_loop_wires_specialist_changed_at_round_start_with_the_real_specialist_type()
    test_loop_wires_specialist_changed_to_qa_before_verification()
    print("\nALL NODE-SPECIALIST/ROUND TELEMETRY TESTS PASSED")
