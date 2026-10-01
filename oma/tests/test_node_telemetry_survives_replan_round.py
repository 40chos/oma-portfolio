"""Real bug found live (2026-08-08, the project owner's own follow-up after independently verifying the
specialist/round telemetry work): manager/tools.py's update_graph_created_node_state/specialist/
round() only ever patched the `graph_created` event's own snapshot. The instant any real
`replan_round` event exists for a task (the first retry), list_rounds_for_task() permanently
switches over to reading THAT row instead -- which is itself just another frozen, point-in-time
snapshot, never patched further. So a task that retries even once loses live per-node updates for
the rest of its life, silently reverting to whatever state existed at the moment that round was
revised -- confirmed live: a node that had genuinely moved on to 'failing'/'blocked' read back
'pending'/round 0/no specialist from GET /api/rounds afterward.

Real fix: _update_node_field_live() (manager/tools.py) now patches BOTH the graph_created
snapshot AND the single latest replan_round row for the task, unconditionally, every time. This
test uses REAL Postgres (via append_project_memory(), the only sanctioned insert path -- see
test_manager_tools.py's own grep-verified guard) -- no mocks -- and cleans up after itself.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.tools import (
    _get_conn,
    append_project_memory,
    update_graph_created_node_round,
    update_graph_created_node_specialist,
    update_graph_created_node_state,
)


def _fetch_detail(row_id: int) -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT detail FROM agent_memory_events WHERE id = %s", (row_id,))
        return cur.fetchone()[0]
    finally:
        conn.close()


def _cleanup(row_ids: list[int]) -> None:
    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for row_id in row_ids:
            cur.execute("DELETE FROM agent_memory_events WHERE id = %s", (row_id,))
        cur.close()
    finally:
        conn.close()


def test_live_update_reaches_both_graph_created_and_the_latest_replan_round_row():
    task_id = "aaaaaaaa-0000-0000-0000-000000000001"
    label = "some_piece"

    graph_row_id = append_project_memory(
        event_type="graph_created", actor="manager", task_id=task_id,
        summary="1 independent constraint(s) decomposed for this task.",
        tags=["graph_created"],
        detail={
            "constraint_nodes": {label: {"label": label, "state": "pending", "round_number": 0}},
            "planning_round_budget": 5,
            "split_lineage": {},
        },
        verified=True,
    )
    replan_row_id = append_project_memory(
        event_type="replan_round", actor="manager", task_id=task_id,
        summary="Round 1 revised: real test fixture.",
        detail={
            "round_number": 1,
            "new_contract": {
                "constraint_nodes": {label: {"label": label, "state": "pending", "round_number": 0}},
                "planning_round_budget": 5,
            },
        },
        verified=True,
    )

    try:
        update_graph_created_node_state(task_id, label, "failing")
        update_graph_created_node_specialist(task_id, label, "qa")
        update_graph_created_node_round(task_id, label, 3)

        graph_detail = _fetch_detail(graph_row_id)
        graph_node = graph_detail["constraint_nodes"][label]
        assert graph_node["state"] == "failing"
        assert graph_node["active_specialist"] == "qa"
        assert graph_node["round_number"] == 3

        replan_detail = _fetch_detail(replan_row_id)
        replan_node = replan_detail["new_contract"]["constraint_nodes"][label]
        assert replan_node["state"] == "failing", (
            f"the replan_round row (the one list_rounds_for_task() actually surfaces once it "
            f"exists) must ALSO receive the live update -- got {replan_node}"
        )
        assert replan_node["active_specialist"] == "qa"
        assert replan_node["round_number"] == 3
        print("PASS: a live node update patches BOTH the graph_created snapshot AND the latest "
              "replan_round row -- the frozen post-retry snapshot no longer stays stuck forever")
    finally:
        _cleanup([graph_row_id, replan_row_id])


def test_update_never_fabricates_a_label_that_does_not_exist_in_a_given_row():
    """create_missing=false: a label present in graph_created but NOT (yet) in the latest
    replan_round row's own constraint_nodes (e.g. a label from a later recursive split, taken
    after that round's own snapshot was written) must be left completely untouched there, never
    inserted as a malformed partial entry.
    """
    task_id = "aaaaaaaa-0000-0000-0000-000000000002"
    known_label = "known_piece"
    new_label = "brand_new_piece_not_in_replan_snapshot"

    graph_row_id = append_project_memory(
        event_type="graph_created", actor="manager", task_id=task_id,
        summary="2 independent constraint(s) decomposed for this task.",
        detail={
            "constraint_nodes": {
                known_label: {"label": known_label, "state": "pending"},
                new_label: {"label": new_label, "state": "pending"},
            },
            "planning_round_budget": 5, "split_lineage": {},
        },
        verified=True,
    )
    replan_row_id = append_project_memory(
        event_type="replan_round", actor="manager", task_id=task_id,
        summary="Round 1 revised: real test fixture (pre-dates the new_label split).",
        detail={
            "round_number": 1,
            "new_contract": {
                "constraint_nodes": {known_label: {"label": known_label, "state": "pending"}},
                "planning_round_budget": 5,
            },
        },
        verified=True,
    )

    try:
        update_graph_created_node_state(task_id, new_label, "running")

        graph_detail = _fetch_detail(graph_row_id)
        assert graph_detail["constraint_nodes"][new_label]["state"] == "running"

        replan_detail = _fetch_detail(replan_row_id)
        assert new_label not in replan_detail["new_contract"]["constraint_nodes"], (
            "a label absent from this replan_round's own snapshot must never be fabricated into "
            "it -- create_missing=false must leave the row completely untouched for that label"
        )
        print("PASS: a label absent from the replan_round's own snapshot is never fabricated "
              "into it -- the WHERE-clause 'has key' guard skips the update entirely for that row")
    finally:
        _cleanup([graph_row_id, replan_row_id])


def test_sync_live_node_telemetry_onto_real_constraint_node_objects_does_not_crash():
    """Real bug found live (2026-08-08, task 657697fc-f701-4932-a172-b0132da93cfa's flagship
    run): manager/loop.py always calls sync_live_node_telemetry_into_new_snapshot() with
    `new_contract.constraint_nodes` -- a dict of REAL Pydantic `ConstraintNode` objects, not
    plain dicts (see manager/loop.py:3663). `active_specialist` was being live-patched into the
    JSONB snapshot via raw SQL for a long time (manager/tools.py's _update_node_field_live()) but
    was never declared as a field on `ConstraintNode` itself -- so the very first setattr() this
    function performed for that field raised `ValueError: "ConstraintNode" object has no field
    "active_specialist"`, silently aborting telemetry sync (caught by this function's own broad
    except) for EVERY node in the same call, not just the one that happened to have the field
    set. Confirmed live via this exact traceback recurring on every replan_round created for the
    rest of that task's life. Fixed by declaring `active_specialist` on `ConstraintNode`
    (contracts/schema.py) like every other live-patched field already is.
    """
    from contracts.schema import ConstraintNode
    from manager.tools import sync_live_node_telemetry_into_new_snapshot

    task_id = "aaaaaaaa-0000-0000-0000-000000000003"
    label = "some_piece"

    graph_row_id = append_project_memory(
        event_type="graph_created", actor="manager", task_id=task_id,
        summary="1 independent constraint(s) decomposed for this task.",
        detail={
            "constraint_nodes": {
                label: {
                    "label": label, "state": "failing", "active_specialist": "qa",
                    "round_number": 3,
                },
            },
            "planning_round_budget": 5, "split_lineage": {},
        },
        verified=True,
    )
    real_nodes = {label: ConstraintNode(label=label, state="pending", round_number=0)}

    try:
        result = sync_live_node_telemetry_into_new_snapshot(task_id, real_nodes)
        assert result[label].active_specialist == "qa", (
            f"expected the live active_specialist to be synced onto the real ConstraintNode "
            f"object without raising -- got {result[label]!r}"
        )
        assert result[label].state == "failing"
        assert result[label].round_number == 3
        print("PASS: syncing live telemetry onto real ConstraintNode objects (not just plain "
              "dicts) no longer crashes on the undeclared active_specialist field, closing the "
              "real gap found live on task 657697fc")
    finally:
        _cleanup([graph_row_id])


if __name__ == "__main__":
    test_live_update_reaches_both_graph_created_and_the_latest_replan_round_row()
    test_update_never_fabricates_a_label_that_does_not_exist_in_a_given_row()
    test_sync_live_node_telemetry_onto_real_constraint_node_objects_does_not_crash()
    print("\nALL NODE-TELEMETRY-SURVIVES-REPLAN-ROUND TESTS PASSED")
