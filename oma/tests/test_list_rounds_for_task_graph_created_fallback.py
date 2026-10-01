"""Real, confirmed UI gap found live (2026-08-08, the project owner's own report: "see it's running but
don't see graph"): GET /api/rounds/{task_id} (manager.replanning.list_rounds_for_task()) used to
return ONLY replan_round rows -- written exclusively when a round FAILS and gets revised. A task
cleanly executing its first round (the common, good case) had zero rows here, so the UI's own
buildGraphForTask() never had real structure to render from until/unless something failed.

Fixed by also reading the new `graph_created` event (manager/loop.py, written once, immediately
after the real decomposition graph is built, before round 1 of any node starts) and synthesizing
a round-shaped entry with the SAME detail.new_contract.constraint_nodes/planning_round_budget
shape a real replan_round row has -- confirmed here directly against real Postgres, no mocks,
matching this project's own established test discipline for manager/replanning.py.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.replanning import list_rounds_for_task
from manager.tools import append_project_memory
from contracts.schema import ConstraintNode, ConstraintNodeState


def test_graph_created_is_used_as_a_synthetic_round_when_no_real_replan_round_exists():
    task_id = str(uuid.uuid4())
    nodes = {
        "field_a": ConstraintNode(label="field_a", state=ConstraintNodeState.pending, creates=["model.field_a"]),
        "field_b": ConstraintNode(label="field_b", state=ConstraintNodeState.pending, creates=["model.field_b"]),
    }
    append_project_memory(
        event_type="graph_created", actor="manager", task_id=task_id, module="res.partner",
        summary="2 independent constraint(s) decomposed for this task.",
        tags=["graph_created"],
        detail={
            "constraint_nodes": {k: v.model_dump(mode="json") for k, v in nodes.items()},
            "planning_round_budget": 5,
        },
        verified=True,
    )

    rounds = list_rounds_for_task(task_id)
    assert len(rounds) == 1, f"expected exactly one synthetic round from graph_created, got {len(rounds)}"
    cn = rounds[0]["detail"]["new_contract"]["constraint_nodes"]
    assert set(cn.keys()) == {"field_a", "field_b"}
    assert rounds[0]["detail"]["new_contract"]["planning_round_budget"] == 5
    print("PASS: a task with only a graph_created event (no real replan_round yet) still returns "
          "a real, correctly-shaped synthetic round for the UI's own buildGraphForTask() to render")


def test_a_real_replan_round_always_wins_over_the_synthetic_graph_created_entry():
    """Once a genuine revision happens, that's the more current, authoritative structure -- the
    synthetic graph_created entry must never linger alongside or instead of real round history.
    """
    task_id = str(uuid.uuid4())
    append_project_memory(
        event_type="graph_created", actor="manager", task_id=task_id, module="res.partner",
        summary="1 independent constraint(s) decomposed for this task.", tags=["graph_created"],
        detail={"constraint_nodes": {"field_a": {"label": "field_a"}}, "planning_round_budget": 5},
        verified=True,
    )
    append_project_memory(
        event_type="replan_round", actor="manager", task_id=task_id, module="res.partner",
        summary="Round 1 revised: real failure.", tags=["replan_round"],
        detail={"new_contract": {"constraint_nodes": {"field_a": {"label": "field_a", "state": "failing"}}, "planning_round_budget": 5}},
        verified=True,
    )

    rounds = list_rounds_for_task(task_id)
    assert len(rounds) == 1, f"a real replan_round must fully supersede the synthetic entry, got {len(rounds)} rows"
    assert rounds[0]["detail"]["new_contract"]["constraint_nodes"]["field_a"]["state"] == "failing", (
        "the REAL replan_round's own constraint_nodes must win, never the stale graph_created snapshot"
    )
    print("PASS: a real replan_round always wins over (and excludes) the synthetic graph_created entry")


def test_no_rows_at_all_when_neither_event_exists():
    task_id = str(uuid.uuid4())
    rounds = list_rounds_for_task(task_id)
    assert rounds == []
    print("PASS: a task_id with no rounds and no graph_created event still returns an empty list, not an error")


if __name__ == "__main__":
    test_graph_created_is_used_as_a_synthetic_round_when_no_real_replan_round_exists()
    test_a_real_replan_round_always_wins_over_the_synthetic_graph_created_entry()
    test_no_rows_at_all_when_neither_event_exists()
    print("\nALL list_rounds_for_task GRAPH_CREATED FALLBACK TESTS PASSED")
