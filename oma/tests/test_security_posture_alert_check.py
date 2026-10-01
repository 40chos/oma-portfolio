"""Phase 36 §4.5 item 1 -- scripts/security_posture_alert_check.py. Mock-driven, same
convention as tests/test_graph_queries.py and tests/test_code_review_graph_wiring.py: no
real Neo4j instance, a real filesystem tmp_path for the snapshot/log directories (this
script's own real I/O surface, worth exercising for real rather than mocking).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "security_posture_alert_check.py"
_spec = importlib.util.spec_from_file_location("security_posture_alert_check", _SCRIPT_PATH)
security_posture_alert_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(security_posture_alert_check)


def test_compute_newly_ungated_only_returns_false_to_true_transitions():
    prior = {"res.partner", "sale.order"}
    current = {"res.partner", "crm.lead"}  # sale.order dropped, crm.lead newly appeared

    result = security_posture_alert_check.compute_newly_ungated(prior, current)

    assert result == ["crm.lead"]  # not sale.order (that's a fixed rule, not an alert case)


def test_compute_newly_ungated_empty_when_nothing_new():
    assert security_posture_alert_check.compute_newly_ungated({"a"}, {"a"}) == []


def test_load_prior_ungated_set_empty_when_no_snapshot_file(tmp_path):
    result = security_posture_alert_check.load_prior_ungated_set(tmp_path)
    assert result == set()


def test_load_prior_ungated_set_empty_when_snapshot_malformed(tmp_path):
    (tmp_path / "security_posture_snapshot_latest.json").write_text("not valid json{{{")
    result = security_posture_alert_check.load_prior_ungated_set(tmp_path)
    assert result == set()


def test_save_snapshot_then_load_prior_ungated_set_round_trips(tmp_path):
    security_posture_alert_check.save_snapshot(tmp_path, ["res.partner", "crm.lead"])

    loaded = security_posture_alert_check.load_prior_ungated_set(tmp_path)

    assert loaded == {"res.partner", "crm.lead"}


def test_save_snapshot_writes_both_a_timestamped_and_a_latest_copy(tmp_path):
    security_posture_alert_check.save_snapshot(tmp_path, ["res.partner"])

    files = sorted(p.name for p in tmp_path.iterdir())
    assert "security_posture_snapshot_latest.json" in files
    timestamped = [f for f in files if f != "security_posture_snapshot_latest.json"]
    assert len(timestamped) == 1
    assert timestamped[0].startswith("security_posture_snapshot_")


def test_write_security_log_line_writes_a_security_level_json_line_matching_claude_md_schema(tmp_path):
    security_posture_alert_check.write_security_log_line(tmp_path, "res.partner", "base")

    log_files = list(tmp_path.glob("*.jsonl"))
    assert len(log_files) == 1
    line = json.loads(log_files[0].read_text().strip())

    assert line["log_level"] == "SECURITY"
    assert line["message"] == "model_access_control_newly_ungated"
    assert line["metadata"]["technical_name"] == "res.partner"
    assert line["metadata"]["owner_module"] == "base"
    assert line["agent_id"] == "AGT-009-ODOO_INGESTOR"
    # correlation_id / execution_id must be present and look like real UUIDs
    import uuid
    uuid.UUID(line["correlation_id"])
    uuid.UUID(line["execution_id"])


def test_run_first_ever_run_alerts_on_nothing_but_saves_a_baseline_snapshot(tmp_path):
    """First run ever (no prior snapshot) must not flood an alert for every currently-
    ungated model -- only a transition observed AFTER a baseline exists should alert."""
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_all_ungated_models",
             return_value=(False, ["res.partner", "sale.order"]),
         ):
        newly_ungated = security_posture_alert_check.run(snapshot_dir, log_dir)

    assert newly_ungated == []
    assert not list(log_dir.glob("*.jsonl")) if log_dir.exists() else True
    loaded = security_posture_alert_check.load_prior_ungated_set(snapshot_dir)
    assert loaded == {"res.partner", "sale.order"}


def test_run_second_run_alerts_only_on_the_new_transition(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    security_posture_alert_check.save_snapshot(snapshot_dir, ["res.partner"])

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_all_ungated_models",
             return_value=(False, ["res.partner", "crm.lead"]),
         ), \
         patch.object(security_posture_alert_check, "_resolve_owner_module", return_value="crm"):
        newly_ungated = security_posture_alert_check.run(snapshot_dir, log_dir)

    assert newly_ungated == ["crm.lead"]
    log_files = list(log_dir.glob("*.jsonl"))
    assert len(log_files) == 1
    line = json.loads(log_files[0].read_text().strip())
    assert line["metadata"]["technical_name"] == "crm.lead"


def test_run_same_ungated_set_twice_does_not_re_alert(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    security_posture_alert_check.save_snapshot(snapshot_dir, ["res.partner"])

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_all_ungated_models",
             return_value=(False, ["res.partner"]),
         ):
        newly_ungated = security_posture_alert_check.run(snapshot_dir, log_dir)

    assert newly_ungated == []
    assert not log_dir.exists() or not list(log_dir.glob("*.jsonl"))


def test_run_dry_run_computes_diff_but_writes_nothing(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    security_posture_alert_check.save_snapshot(snapshot_dir, ["res.partner"])

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_all_ungated_models",
             return_value=(False, ["res.partner", "crm.lead"]),
         ):
        newly_ungated = security_posture_alert_check.run(snapshot_dir, log_dir, dry_run=True)

    assert newly_ungated == ["crm.lead"]
    assert not log_dir.exists()
    # snapshot must be unchanged (still only res.partner) since dry-run never persists
    assert security_posture_alert_check.load_prior_ungated_set(snapshot_dir) == {"res.partner"}


def test_run_returns_empty_and_does_not_raise_when_graph_unreachable(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    with patch("infra.neo4j_client.get_neo4j_read_driver", side_effect=RuntimeError("down")):
        newly_ungated = security_posture_alert_check.run(snapshot_dir, log_dir)

    assert newly_ungated == []


def test_run_returns_empty_when_import_in_progress(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_all_ungated_models",
             return_value=(True, ["res.partner"]),
         ):
        newly_ungated = security_posture_alert_check.run(snapshot_dir, log_dir)

    assert newly_ungated == []
    assert security_posture_alert_check.load_prior_ungated_set(snapshot_dir) == set()  # no snapshot written either
