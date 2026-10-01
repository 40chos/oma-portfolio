"""Phase 36 §13.4 item 2 -- scripts/odoo_kg_install_state_reconciliation_sweep.py.
Mock-driven, same convention as test_security_posture_alert_check.py: no real Neo4j/RPC
calls, a real filesystem tmp_path for the snapshot/log directories.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "odoo_kg_install_state_reconciliation_sweep.py"
_spec = importlib.util.spec_from_file_location("odoo_kg_install_state_reconciliation_sweep", _SCRIPT_PATH)
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


def test_compute_drift_none_when_states_match():
    assert sweep.compute_drift("account", "installed", "installed") is None


def test_compute_drift_none_when_real_state_read_failed():
    # real_state=None means the RPC read itself failed -- nothing trustworthy to correct to.
    assert sweep.compute_drift("account", "installed", None) is None


def test_compute_drift_flags_a_genuine_mismatch():
    drift = sweep.compute_drift("account", "installed", "uninstalled")
    assert drift == {"module": "account", "graph_state": "installed", "real_state": "uninstalled"}


def test_compute_drift_flags_never_confirmed_module():
    # graph_state None (never written) vs a real confirmed state IS drift worth correcting.
    drift = sweep.compute_drift("account", None, "installed")
    assert drift == {"module": "account", "graph_state": None, "real_state": "installed"}


def test_write_reconciliation_log_line_matches_claude_md_schema(tmp_path):
    sweep.write_reconciliation_log_line(
        tmp_path, {"module": "account", "graph_state": "installed", "real_state": "uninstalled"}
    )
    log_files = list(tmp_path.glob("*.jsonl"))
    assert len(log_files) == 1
    line = json.loads(log_files[0].read_text().strip())
    assert line["log_level"] == "WARN"
    assert line["message"] == "install_state_drift_corrected"
    assert line["metadata"]["module"] == "account"
    assert line["agent_id"] == "AGT-009-ODOO_INGESTOR"
    import uuid
    uuid.UUID(line["correlation_id"])
    uuid.UUID(line["execution_id"])


def test_save_snapshot_writes_both_a_timestamped_and_a_latest_copy(tmp_path):
    sweep.save_snapshot(tmp_path, [{"module": "account", "graph_state": "x", "real_state": "y"}], modules_checked=240)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert "install_state_reconciliation_latest.json" in files
    timestamped = [f for f in files if f != "install_state_reconciliation_latest.json"]
    assert len(timestamped) == 1


def test_run_corrects_a_real_drift_and_logs_it(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    with patch("infra.neo4j_client.get_neo4j_driver", return_value="fake-driver"), \
         patch("tools_odoo.graph_queries.get_all_module_names", return_value=(False, ["account"])), \
         patch("tools_odoo.graph_queries.get_module_real_install_state", return_value=(False, "installed")), \
         patch("tools_odoo.odoo_schema_client.get_module_state_fast", return_value="uninstalled"), \
         patch("tools_odoo.graph_queries.update_module_install_state", return_value=True) as mock_update:
        drifts = sweep.run("odoo16_dev", snapshot_dir, log_dir)

    assert drifts == [{"module": "account", "graph_state": "installed", "real_state": "uninstalled"}]
    mock_update.assert_called_once()
    assert mock_update.call_args.args[1] == "account"
    assert mock_update.call_args.args[2] == "uninstalled"
    log_files = list(log_dir.glob("*.jsonl"))
    assert len(log_files) == 1


def test_run_no_drift_when_states_match(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    with patch("infra.neo4j_client.get_neo4j_driver", return_value="fake-driver"), \
         patch("tools_odoo.graph_queries.get_all_module_names", return_value=(False, ["account"])), \
         patch("tools_odoo.graph_queries.get_module_real_install_state", return_value=(False, "installed")), \
         patch("tools_odoo.odoo_schema_client.get_module_state_fast", return_value="installed"), \
         patch("tools_odoo.graph_queries.update_module_install_state") as mock_update:
        drifts = sweep.run("odoo16_dev", snapshot_dir, log_dir)

    assert drifts == []
    mock_update.assert_not_called()
    assert not log_dir.exists() or not list(log_dir.glob("*.jsonl"))


def test_run_dry_run_computes_drift_but_writes_nothing(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    with patch("infra.neo4j_client.get_neo4j_driver", return_value="fake-driver"), \
         patch("tools_odoo.graph_queries.get_all_module_names", return_value=(False, ["account"])), \
         patch("tools_odoo.graph_queries.get_module_real_install_state", return_value=(False, "installed")), \
         patch("tools_odoo.odoo_schema_client.get_module_state_fast", return_value="uninstalled"), \
         patch("tools_odoo.graph_queries.update_module_install_state") as mock_update:
        drifts = sweep.run("odoo16_dev", snapshot_dir, log_dir, dry_run=True)

    assert drifts == [{"module": "account", "graph_state": "installed", "real_state": "uninstalled"}]
    mock_update.assert_not_called()
    assert not log_dir.exists()
    assert not snapshot_dir.exists()


def test_run_returns_empty_and_does_not_raise_when_graph_unreachable(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    with patch("infra.neo4j_client.get_neo4j_driver", side_effect=RuntimeError("down")):
        drifts = sweep.run("odoo16_dev", snapshot_dir, log_dir)

    assert drifts == []


def test_run_returns_empty_when_import_in_progress(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    with patch("infra.neo4j_client.get_neo4j_driver", return_value="fake-driver"), \
         patch("tools_odoo.graph_queries.get_all_module_names", return_value=(True, ["account"])):
        drifts = sweep.run("odoo16_dev", snapshot_dir, log_dir)

    assert drifts == []


def test_run_skips_a_single_module_whose_rpc_read_fails_but_continues_others(tmp_path):
    snapshot_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"

    def fake_get_state(module_name, db):
        if module_name == "account":
            raise RuntimeError("rpc boom")
        return "installed"

    with patch("infra.neo4j_client.get_neo4j_driver", return_value="fake-driver"), \
         patch("tools_odoo.graph_queries.get_all_module_names", return_value=(False, ["account", "sale"])), \
         patch("tools_odoo.graph_queries.get_module_real_install_state", return_value=(False, "installed")), \
         patch("tools_odoo.odoo_schema_client.get_module_state_fast", side_effect=fake_get_state):
        drifts = sweep.run("odoo16_dev", snapshot_dir, log_dir)

    assert drifts == []  # "sale" matched (installed==installed); "account" skipped, not crashed
