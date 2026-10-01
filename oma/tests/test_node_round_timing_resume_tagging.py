"""Real bug found live (2026-08-09, the project owner's own direct report on task
07141af5-9a4e-41b6-93ea-8b7af04fea9c: "timer for different resumes, it's staying the same,
especially for round one... it should be separate sync, not the same timer for everything").

persist_node_round_timing() originally (2026-08-08) ADDED a round+actor's own seconds across
resumes reusing the same round number -- correct for that day's own complaint (a resumed
attempt's timing silently overwrote, and thereby lost, the pre-pause attempt's real time), but
once round_diffs/round_findings/round_checks/round_steps were all tagged with resume_index (this
same day, earlier fix), leaving round_timings un-tagged meant every attempt's own timer for round
N kept summing into ONE shared, ever-growing number -- exactly what "staying the same... not
separate" describes: clicking between "Original attempt" and "Resume #58" in the new attempt
selector showed the identical (combined) time for both, never each attempt's own real seconds.

Fixed by keying one level deeper -- round_timings.{round}.{resume_index}.{actor} -- and reverting
to a plain SET at that fully-qualified key (a given (round, resume_index, actor) triple is now
written at most once ever, since round numbers only repeat ACROSS resumes, never within one).
This test uses REAL Postgres (via append_project_memory()), no mocks, and cleans up after itself.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.tools import _get_conn, append_project_memory, persist_node_round_timing


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


def _make_graph_row(task_id: str, label: str) -> int:
    return append_project_memory(
        event_type="graph_created", actor="manager", task_id=task_id,
        summary="1 independent constraint(s) decomposed for this task.",
        tags=["graph_created"],
        detail={
            "constraint_nodes": {label: {"label": label, "state": "pending", "round_number": 0}},
            "planning_round_budget": 5, "split_lineage": {},
        },
        verified=True,
    )


def test_two_resumes_reusing_round_1_keep_fully_independent_timings():
    task_id = "aaaaaaaa-0000-0000-0000-000000000201"
    label = "ticket_access_rights"
    graph_row_id = _make_graph_row(task_id, label)
    try:
        persist_node_round_timing(task_id, label, 1, "build", 12.5, resume_index=0)
        persist_node_round_timing(task_id, label, 1, "round_total", 20.0, resume_index=0)
        persist_node_round_timing(task_id, label, 1, "build", 8.0, resume_index=58)
        persist_node_round_timing(task_id, label, 1, "round_total", 15.0, resume_index=58)

        detail = _fetch_detail(graph_row_id)
        timings = detail["constraint_nodes"][label]["round_timings"]["1"]
        assert timings["0"]["build"] == 12.5, timings
        assert timings["0"]["round_total"] == 20.0, timings
        assert timings["58"]["build"] == 8.0, timings
        assert timings["58"]["round_total"] == 15.0, timings
        print("PASS: two resumes reusing round 1 keep fully independent, never-mixed timing totals")
    finally:
        _cleanup([graph_row_id])


def test_a_fresh_never_resumed_round_defaults_resume_index_to_zero():
    task_id = "aaaaaaaa-0000-0000-0000-000000000202"
    label = "equipment_registry"
    graph_row_id = _make_graph_row(task_id, label)
    try:
        persist_node_round_timing(task_id, label, 1, "qa", 5.0)
        detail = _fetch_detail(graph_row_id)
        timings = detail["constraint_nodes"][label]["round_timings"]["1"]
        assert timings["0"]["qa"] == 5.0, timings
        print("PASS: a fresh, never-resumed round's timing defaults to resume_index 0")
    finally:
        _cleanup([graph_row_id])


def test_different_actors_same_round_and_attempt_never_clobber_each_other():
    task_id = "aaaaaaaa-0000-0000-0000-000000000203"
    label = "service_ticket_model"
    graph_row_id = _make_graph_row(task_id, label)
    try:
        persist_node_round_timing(task_id, label, 2, "build", 30.0, resume_index=3)
        persist_node_round_timing(task_id, label, 2, "review", 10.0, resume_index=3)
        persist_node_round_timing(task_id, label, 2, "qa", 7.5, resume_index=3)
        persist_node_round_timing(task_id, label, 2, "manager", 2.0, resume_index=3)

        detail = _fetch_detail(graph_row_id)
        timings = detail["constraint_nodes"][label]["round_timings"]["2"]["3"]
        assert timings == {"build": 30.0, "review": 10.0, "qa": 7.5, "manager": 2.0}, timings
        print("PASS: build/review/qa/manager timings for the same round+attempt all coexist without clobbering")
    finally:
        _cleanup([graph_row_id])


def test_a_round_with_preexisting_legacy_flat_timing_self_heals_on_the_next_real_write():
    """End-to-end repro of the real live risk (task 07141af5-9a4e-41b6-93ea-8b7af04fea9c): a round
    already holding OLD flat timing data (written before resume-tagging shipped) must NOT get a
    new {resume_index: {...}} entry blindly merged onto it -- the next real
    persist_node_round_timing() call must detect and migrate the legacy shape first, in the same
    write, never producing a mixed object with both actor keys and a resume_index key at the same
    level.
    """
    task_id = "aaaaaaaa-0000-0000-0000-000000000204"
    label = "ticket_access_rights"
    graph_row_id = _make_graph_row(task_id, label)
    try:
        conn = _get_conn()
        conn.autocommit = True
        cur = conn.cursor()
        import json as _json
        cur.execute(
            "UPDATE agent_memory_events SET detail = jsonb_set(detail, %s, %s::jsonb) WHERE id = %s",
            (["constraint_nodes", label, "round_timings"],
             _json.dumps({"1": {"build": 12.5, "manager": 2.0, "round_total": 20.0}}),
             graph_row_id),
        )
        conn.close()

        persist_node_round_timing(task_id, label, 1, "build", 8.0, resume_index=65)

        detail = _fetch_detail(graph_row_id)
        timings = detail["constraint_nodes"][label]["round_timings"]["1"]
        assert timings["0"] == {"build": 12.5, "manager": 2.0, "round_total": 20.0}, timings
        assert timings["65"] == {"build": 8.0}, timings
        assert set(timings.keys()) == {"0", "65"}, f"expected exactly the migrated legacy group and the new attempt, got {timings}"
        print("PASS: a round already holding legacy flat timing data self-heals into a correctly tagged structure on the very next real write")
    finally:
        _cleanup([graph_row_id])


if __name__ == "__main__":
    test_two_resumes_reusing_round_1_keep_fully_independent_timings()
    test_a_fresh_never_resumed_round_defaults_resume_index_to_zero()
    test_different_actors_same_round_and_attempt_never_clobber_each_other()
    test_a_round_with_preexisting_legacy_flat_timing_self_heals_on_the_next_real_write()
    print("\nALL NODE-ROUND-TIMING RESUME-TAGGING TESTS PASSED")
