"""Phase 20 (§24.4 Component 4) tests: goal-text parsing (pure, no DB)
and a real end-to-end harvest against fixture rows (self-cleaning, same
convention as test_manager_memory.py) -- proves harvest_field_add_templates()
can turn durable agent_memory_events history into a real agent_templates
row without needing anything to run live.
"""

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

from infra.settings import load_postgres_settings
from templates.harvest import _parse_goal, harvest_field_add_templates
from templates.store import delete_template, get_template, list_templates


def test_parse_goal_single_field():
    goal = ("Create a small new Odoo module that adds a single new field badge_expiry_date (Date) "
            "to the hr.employee model. Add it to the employee form view.")
    parsed = _parse_goal(goal)
    assert parsed == ("hr.employee", [("badge_expiry_date", "Date")])
    print(f"PASS: single-field goal parsed correctly: {parsed}")


def test_parse_goal_multi_field():
    goal = ("Create a small new Odoo module that adds two new fields to the project.task model: "
            "estimated_hours (Float) and actual_hours (Float). Add both to the task form view.")
    parsed = _parse_goal(goal)
    assert parsed == ("project.task", [("estimated_hours", "Float"), ("actual_hours", "Float")])
    print(f"PASS: multi-field goal parsed correctly: {parsed}")


def test_parse_goal_no_match_returns_none():
    assert _parse_goal("Fix the invoicing dashboard's broken totals widget.") is None
    print("PASS: unrelated goal text returns None, not a false match")


def _insert_fixture_task(task_id: str, goal: str):
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO agent_memory_events (event_type, task_id, actor, summary, detail) "
        "VALUES ('task_created', %s, 'operator', %s, '{}'::jsonb)",
        (task_id, goal),
    )
    cur.execute(
        "INSERT INTO agent_memory_events (event_type, task_id, actor, summary, detail, verified) "
        "VALUES ('outcome', %s, 'bug_fix', 'test fixture outcome', %s::jsonb, true)",
        (task_id, json.dumps({"passed": True, "rounds_taken": 1})),
    )
    cur.close()
    conn.close()


def _cleanup_fixture_task(task_id: str):
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DELETE FROM agent_memory_events WHERE task_id = %s", (task_id,))
    cur.close()
    conn.close()


def test_harvest_creates_and_is_idempotent():
    marker_model = f"x.harvesttest{uuid.uuid4().hex[:6]}"
    task_id_1 = str(uuid.uuid4())
    task_id_2 = str(uuid.uuid4())
    goal_tmpl = (
        "Create a small new Odoo module that adds a single new field probe_field (Char) "
        f"to the {marker_model} model. Add it to the probe form view."
    )
    created_template_ids = []
    try:
        _insert_fixture_task(task_id_1, goal_tmpl)
        _insert_fixture_task(task_id_2, goal_tmpl)

        result_1 = harvest_field_add_templates()
        matches = [t for t in list_templates(odoo_module_area=marker_model)]
        assert len(matches) == 1, f"expected exactly 1 harvested template for {marker_model}, got {len(matches)}"
        t = matches[0]
        created_template_ids.append(t.template_id)
        assert t.success_count == 2, "expected both fixture instances counted"
        assert t.status.value == "active", "2 verified instances should meet the auto-promote margin"
        assert t.template_id in result_1["templates_created_ids"]
        # These fixture task_ids have no real Gitea branch -- code_examples
        # must come back empty, never fabricated, per §24.14.7's "a missing
        # example is honest; a fabricated one is not" rule.
        assert t.code_examples == [], "no real Gitea commit exists for these fixture task_ids -- must not fabricate"
        print(f"PASS: harvest created 1 Level-0 template for {marker_model}, "
              f"success_count=2, auto-promoted to active, code_examples honestly empty (no real commit)")

        # Idempotency: running harvest again must not create a duplicate.
        result_2 = harvest_field_add_templates()
        assert t.template_id not in result_2["templates_created_ids"]
        matches_again = list_templates(odoo_module_area=marker_model)
        assert len(matches_again) == 1, "re-running harvest must not duplicate an existing template"
        print("PASS: re-running harvest is idempotent -- no duplicate template created")
    finally:
        _cleanup_fixture_task(task_id_1)
        _cleanup_fixture_task(task_id_2)
        for tid in created_template_ids:
            delete_template(tid)


if __name__ == "__main__":
    test_parse_goal_single_field()
    test_parse_goal_multi_field()
    test_parse_goal_no_match_returns_none()
    test_harvest_creates_and_is_idempotent()
    print("\nALL TEMPLATE HARVEST TESTS PASSED")
