"""Phase 35 §18.3: tests for manager/threshold_calibration_log.py -- real, direct calls against
real Postgres (matching this codebase's own established no-mocks pattern for
append_project_memory, e.g. tests/test_dashboard.py), plus a fail-open test with a genuinely
broken store.
"""

import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.threshold_calibration_log import log_gate_decision


def test_real_gate_decision_is_durably_logged_and_readable_back():
    import psycopg2
    from infra.settings import load_postgres_settings

    marker = uuid.uuid4().hex[:12]
    task_id = str(uuid.uuid4())  # agent_memory_events.task_id is a real uuid column

    log_gate_decision(
        gate_name="decomposition_blast_radius", metric_name="affected_module_count",
        metric_value=7, threshold=10, verdict="no_signal", task_id=task_id,
        extra={"marker": marker},
    )

    s = load_postgres_settings()
    conn = psycopg2.connect(s.dsn)
    cur = conn.cursor()
    cur.execute(
        "SELECT event_type, summary, detail FROM agent_memory_events WHERE task_id = %s", (task_id,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    event_type, summary, detail = rows[0]
    assert event_type == "decision"
    assert "decomposition_blast_radius" in summary
    assert detail["metric_value"] == 7
    assert detail["threshold"] == 10
    assert detail["verdict"] == "no_signal"
    assert detail["marker"] == marker


def test_never_raises_when_the_underlying_store_is_broken():
    with patch("manager.tools.append_project_memory", side_effect=RuntimeError("db down")):
        log_gate_decision(  # must not raise
            gate_name="x", metric_name="y", metric_value=1, threshold=2, verdict="z",
        )


def test_optional_task_id_and_extra_are_handled():
    import psycopg2
    from infra.settings import load_postgres_settings

    marker = uuid.uuid4().hex[:12]
    log_gate_decision(
        gate_name="intake_grounding_gate", metric_name="confidence", metric_value=0.85,
        threshold=None, verdict="no_signal", extra={"marker": marker},
    )

    s = load_postgres_settings()
    conn = psycopg2.connect(s.dsn)
    cur = conn.cursor()
    cur.execute(
        "SELECT detail FROM agent_memory_events WHERE tags @> %s ORDER BY id DESC LIMIT 5",
        (["threshold_calibration", "intake_grounding_gate"],),
    )
    rows = cur.fetchall()
    matching = [r for r in rows if r[0].get("marker") == marker]
    assert len(matching) == 1
    assert matching[0][0]["threshold"] is None
