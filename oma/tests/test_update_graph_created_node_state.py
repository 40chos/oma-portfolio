"""Real bug found live (2026-08-08, task 05c20568-d4ba-4836-b120-86410bb2abc1, the project owner's own
report): "node status in the task graph never updates on screen after a piece succeeds -- only
after a failure/retry." Root cause: the `graph_created` event (written once, at decomposition
time) is a frozen snapshot -- list_rounds_for_task() synthesizes the UI's graph from it whenever
no real replan_round exists yet, but a task where every node genuinely PASSES on its first try
never writes a replan_round at all (that only fires on failure), so the frozen snapshot's nodes
stayed stuck at 'pending' forever, even after real, full completion.

Fixed by manager.tools.update_graph_created_node_state(), called from manager.graph_scheduler's
own _publish_node_state_changed() (the same hook that already fires for every real transition) --
updates the graph_created row's own constraint_nodes JSON in place, live, via a single atomic
jsonb_set(). Tested directly against real Postgres, no mocks, matching this project's own
established discipline for manager/tools.py and manager/replanning.py.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.replanning import list_rounds_for_task
from manager.tools import append_project_memory, update_graph_created_node_state
from contracts.schema import ConstraintNode, ConstraintNodeState


def _write_graph_created(task_id: str, labels: list[str]) -> None:
    nodes = {
        label: ConstraintNode(label=label, state=ConstraintNodeState.pending, creates=[f"model.{label}"])
        for label in labels
    }
    append_project_memory(
        event_type="graph_created", actor="manager", task_id=task_id, module="res.partner",
        summary=f"{len(labels)} independent constraint(s) decomposed for this task.",
        tags=["graph_created"],
        detail={
            "constraint_nodes": {k: v.model_dump(mode="json") for k, v in nodes.items()},
            "planning_round_budget": 5,
        },
        verified=True,
    )


def test_a_node_reaching_satisfied_is_reflected_live_with_no_replan_round_ever_needed():
    """The exact real scenario: every node passes on the first try, no round ever fails, yet the
    UI's own list_rounds_for_task() must still show the real, current, non-'pending' state.
    """
    task_id = str(uuid.uuid4())
    _write_graph_created(task_id, ["field_a", "field_b", "field_c"])

    update_graph_created_node_state(task_id, "field_a", "running")
    update_graph_created_node_state(task_id, "field_a", "satisfied")
    update_graph_created_node_state(task_id, "field_b", "running")

    rounds = list_rounds_for_task(task_id)
    assert len(rounds) == 1
    cn = rounds[0]["detail"]["new_contract"]["constraint_nodes"]
    assert cn["field_a"]["state"] == "satisfied", (
        f"field_a genuinely passed -- must show 'satisfied', not stuck at 'pending' -- got {cn['field_a']}"
    )
    assert cn["field_b"]["state"] == "running"
    assert cn["field_c"]["state"] == "pending", "a node that never transitioned must stay accurately 'pending'"
    print("PASS: node state updates are reflected live in list_rounds_for_task() with no replan_round ever needed")


def test_updating_one_label_never_disturbs_a_sibling_labels_own_state():
    task_id = str(uuid.uuid4())
    _write_graph_created(task_id, ["field_a", "field_b"])
    update_graph_created_node_state(task_id, "field_a", "failing")
    update_graph_created_node_state(task_id, "field_b", "satisfied")
    update_graph_created_node_state(task_id, "field_a", "pending")  # e.g. unblocked on retry

    rounds = list_rounds_for_task(task_id)
    cn = rounds[0]["detail"]["new_contract"]["constraint_nodes"]
    assert cn["field_a"]["state"] == "pending"
    assert cn["field_b"]["state"] == "satisfied", "updating field_a must never disturb field_b's own state"
    print("PASS: concurrent per-label updates never clobber each other")


def test_never_raises_when_no_graph_created_row_exists_yet():
    """A non-decomposed, single-constraint task never emits graph_created at all -- this must be
    a silent, safe no-op, never a crash that could break the real scheduler's own dispatch loop.
    """
    task_id = str(uuid.uuid4())
    update_graph_created_node_state(task_id, "field_a", "running")  # must not raise
    assert list_rounds_for_task(task_id) == []
    print("PASS: updating a task with no graph_created row is a safe, silent no-op, never raises")


if __name__ == "__main__":
    test_a_node_reaching_satisfied_is_reflected_live_with_no_replan_round_ever_needed()
    test_updating_one_label_never_disturbs_a_sibling_labels_own_state()
    test_never_raises_when_no_graph_created_row_exists_yet()
    print("\nALL update_graph_created_node_state TESTS PASSED")
