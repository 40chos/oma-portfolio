"""Real bug found live (2026-08-09, the project owner's own direct report on task
07141af5-9a4e-41b6-93ea-8b7af04fea9c: "it should replace our previous round one... but I want it
to collect into one single picture... resume one, resume two, resume three... so we could sort
out all this resume, but not replace our existing rounds, it's very important, do not mess up
it"). `_execute_contract()`'s own round loop deliberately restarts its round counter at 1 on every
resume (a genuine, intentional restart of THAT attempt's own numbering, not a bug) -- but
`persist_node_round_diff()`/`persist_node_round_findings()`/`persist_node_round_checks()` used to
plain-SET `round_{diffs,findings,checks}.{round_number}`, so a resumed attempt reusing round
number 1 silently overwrote the pre-pause attempt's own real diff/findings/checks with no trace
they ever existed.

Fixed by switching these three from SET to APPEND -- the same "growing array per round number"
pattern `round_steps` already used -- with every entry tagged `resume_index` (`contract.
resumed_from_checkpoint_count`) so the UI can group and label multiple attempts at the same round
number instead of losing all but the last. This test uses REAL Postgres (via
append_project_memory(), the only sanctioned insert path), no mocks, and cleans up after itself --
same convention as test_node_telemetry_survives_replan_round.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.tools import (
    _get_conn,
    _migrate_legacy_round_attempts,
    append_project_memory,
    persist_node_round_checks,
    persist_node_round_diff,
    persist_node_round_findings,
    persist_node_round_step,
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


def test_a_second_resume_reusing_round_1_appends_alongside_the_first_attempt_never_overwrites():
    task_id = "aaaaaaaa-0000-0000-0000-000000000101"
    label = "ticket_access_rights"
    graph_row_id = _make_graph_row(task_id, label)
    try:
        # Original attempt (resume_index=0) writes round 1's own real diff/findings/checks.
        persist_node_round_diff(task_id, label, 1, [{"path": "security/ir.model.access.csv", "before": "a", "after": "b"}], resume_index=0)
        persist_node_round_findings(task_id, label, 1, [{"location": "csv", "severity": "blocking", "text": "base.group_user has full CRUD"}], resume_index=0)
        persist_node_round_checks(task_id, label, 1, [{"text": "field techs see only their own tickets", "pass": False}], resume_index=0)

        # Operator resumes (continuation #58) -- round counter genuinely restarts at 1 for THIS
        # attempt, but must not destroy the original attempt's own round-1 content above.
        persist_node_round_diff(task_id, label, 1, [{"path": "security/ir.model.access.csv", "before": "b", "after": "c"}], resume_index=58)
        persist_node_round_findings(task_id, label, 1, [{"location": "csv", "severity": "blocking", "text": "operations_manager group missing"}], resume_index=58)
        persist_node_round_checks(task_id, label, 1, [{"text": "operations managers have full access", "pass": False}], resume_index=58)

        detail = _fetch_detail(graph_row_id)
        node = detail["constraint_nodes"][label]

        diffs_round_1 = node["round_diffs"]["1"]
        assert len(diffs_round_1) == 2, f"expected both attempts' own diffs preserved, got {diffs_round_1}"
        assert diffs_round_1[0]["resume_index"] == 0 and diffs_round_1[0]["diff"][0]["after"] == "b"
        assert diffs_round_1[1]["resume_index"] == 58 and diffs_round_1[1]["diff"][0]["after"] == "c"

        findings_round_1 = node["round_findings"]["1"]
        assert len(findings_round_1) == 2
        assert findings_round_1[0]["resume_index"] == 0
        assert findings_round_1[1]["resume_index"] == 58
        assert "operations_manager" in findings_round_1[1]["findings"][0]["text"]

        checks_round_1 = node["round_checks"]["1"]
        assert len(checks_round_1) == 2
        assert checks_round_1[0]["resume_index"] == 0
        assert checks_round_1[1]["resume_index"] == 58

        print("PASS: a second resume reusing round 1 appends its own tagged diff/findings/checks "
              "alongside the first attempt's, never overwriting it")
    finally:
        _cleanup([graph_row_id])


def test_round_steps_are_tagged_with_resume_index_too():
    task_id = "aaaaaaaa-0000-0000-0000-000000000102"
    label = "ticket_access_rights"
    graph_row_id = _make_graph_row(task_id, label)
    try:
        persist_node_round_step(task_id, label, 1, "build", "Writing a scoped edit", "passed", resume_index=0)
        persist_node_round_step(task_id, label, 1, "build", "Writing a scoped edit", "passed", resume_index=58)

        detail = _fetch_detail(graph_row_id)
        steps_round_1 = detail["constraint_nodes"][label]["round_steps"]["1"]
        assert len(steps_round_1) == 2
        assert [s["resume_index"] for s in steps_round_1] == [0, 58]
        print("PASS: round_steps entries carry resume_index too, so the UI can group a round's "
              "narrative by which resume attempt produced it")
    finally:
        _cleanup([graph_row_id])


def test_a_fresh_never_resumed_round_defaults_resume_index_to_zero():
    task_id = "aaaaaaaa-0000-0000-0000-000000000103"
    label = "equipment_registry"
    graph_row_id = _make_graph_row(task_id, label)
    try:
        persist_node_round_diff(task_id, label, 1, [{"path": "models/models.py"}])
        detail = _fetch_detail(graph_row_id)
        entry = detail["constraint_nodes"][label]["round_diffs"]["1"][0]
        assert entry["resume_index"] == 0, "the overwhelming normal (never-resumed) case must default to resume_index 0"
        print("PASS: a fresh, never-resumed round defaults resume_index to 0")
    finally:
        _cleanup([graph_row_id])


def test_migrate_legacy_round_attempts_collapses_bare_entries_into_one_resume_zero_group():
    """_migrate_legacy_round_attempts()'s own unit-level contract: a purely legacy (pre-fix,
    never-tagged) array collapses into one resume_index:0 group; an already-fully-tagged array
    passes through unchanged; a genuinely MIXED array (the real corruption found live -- bare
    legacy entries alongside real tagged ones, from a blind append onto old data) collapses only
    the bare entries into one resume_index:0 group, bare-group first, tagged entries preserved
    in their original order and never duplicated or dropped.
    """
    assert _migrate_legacy_round_attempts(None, "diff") == []
    assert _migrate_legacy_round_attempts([], "diff") == []

    legacy_only = [{"path": "a.py"}, {"path": "b.py"}]
    assert _migrate_legacy_round_attempts(legacy_only, "diff") == [
        {"resume_index": 0, "diff": legacy_only}
    ]

    already_tagged = [{"resume_index": 0, "diff": [{"path": "a.py"}]}, {"resume_index": 5, "diff": [{"path": "b.py"}]}]
    assert _migrate_legacy_round_attempts(already_tagged, "diff") == already_tagged

    mixed = [{"path": "a.py"}, {"resume_index": 58, "diff": [{"path": "b.py"}]}]
    result = _migrate_legacy_round_attempts(mixed, "diff")
    assert result == [
        {"resume_index": 0, "diff": [{"path": "a.py"}]},
        {"resume_index": 58, "diff": [{"path": "b.py"}]},
    ], f"expected the bare entry collapsed into its own resume_index:0 group, ahead of the real tagged entry, got {result}"
    print("PASS: _migrate_legacy_round_attempts() correctly repairs legacy-only, already-tagged, and genuinely mixed (corrupted) shapes")


def test_a_round_with_preexisting_legacy_data_self_heals_on_the_next_real_append():
    """End-to-end repro of the real live incident (task 07141af5-9a4e-41b6-93ea-8b7af04fea9c,
    ticket_access_rights, round 1): a round already holding OLD flat diff data (written before
    the resume-tagging fix shipped) must NOT get a new tagged entry blindly appended onto it,
    corrupting the array -- the next real persist_node_round_diff() call must detect and repair
    the legacy shape first, in the same write.
    """
    task_id = "aaaaaaaa-0000-0000-0000-000000000104"
    label = "ticket_access_rights"
    graph_row_id = _make_graph_row(task_id, label)
    try:
        # Simulate a round already holding pre-fix legacy data: round_diffs["1"] as the bare
        # value itself (a plain list of per-file hunks), not wrapped in {resume_index, diff}.
        conn = _get_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "UPDATE agent_memory_events SET detail = jsonb_set(detail, %s, %s::jsonb) WHERE id = %s",
            (["constraint_nodes", label, "round_diffs"],
             __import__("json").dumps({"1": [{"file": "security/ir.model.access.csv", "lines": []}]}),
             graph_row_id),
        )
        conn.close()

        # A later resume writes its own real round-1 diff, reusing the same round number.
        persist_node_round_diff(task_id, label, 1, [{"file": "security/ir.model.access.csv", "lines": ["+new"]}], resume_index=65)

        detail = _fetch_detail(graph_row_id)
        diffs_round_1 = detail["constraint_nodes"][label]["round_diffs"]["1"]
        assert len(diffs_round_1) == 2, f"expected the legacy entry repaired into its own group plus the new real entry, got {diffs_round_1}"
        assert diffs_round_1[0] == {"resume_index": 0, "diff": [{"file": "security/ir.model.access.csv", "lines": []}]}
        assert diffs_round_1[1] == {"resume_index": 65, "diff": [{"file": "security/ir.model.access.csv", "lines": ["+new"]}]}
        print("PASS: a round already holding legacy (pre-fix) data self-heals into a correctly tagged array on the very next real write, never corrupted further")
    finally:
        _cleanup([graph_row_id])


if __name__ == "__main__":
    test_a_second_resume_reusing_round_1_appends_alongside_the_first_attempt_never_overwrites()
    test_round_steps_are_tagged_with_resume_index_too()
    test_a_fresh_never_resumed_round_defaults_resume_index_to_zero()
    test_migrate_legacy_round_attempts_collapses_bare_entries_into_one_resume_zero_group()
    test_a_round_with_preexisting_legacy_data_self_heals_on_the_next_real_append()
    print("\nALL NODE-ROUND-HISTORY RESUME-TAGGING TESTS PASSED")
