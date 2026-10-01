"""Phase 35 §17.11: unit tests for manager/graph_governance_flags.py -- the staged rollout
mechanism for every new graph-consultation gate. Runs against a temp state file (never the real
state/graph_governance_flags.json) so tests never interfere with real gate state.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import manager.graph_governance_flags as flags_module
from manager.graph_governance_flags import GateMode, all_gate_states, get_gate_mode, set_gate_mode


def _use_temp_state(tmp_path, monkeypatch):
    state_path = os.path.join(str(tmp_path), "graph_governance_flags.json")
    monkeypatch.setattr(flags_module, "_STATE_PATH", state_path)
    monkeypatch.setattr(flags_module, "_STATE_DIR", str(tmp_path))
    return state_path


def test_unknown_gate_never_silently_accepted(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        get_gate_mode("not_a_real_gate")
    with pytest.raises(ValueError):
        set_gate_mode("not_a_real_gate", GateMode.ENFORCED, approved_by="test-user")


def test_every_known_gate_defaults_to_log_only(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    for gate_name in flags_module._KNOWN_GATES:
        assert get_gate_mode(gate_name) == GateMode.LOG_ONLY


def test_missing_state_file_defaults_to_log_only_not_enforced(tmp_path, monkeypatch):
    state_path = _use_temp_state(tmp_path, monkeypatch)
    assert not os.path.exists(state_path)
    assert get_gate_mode("intake_grounding_gate") == GateMode.LOG_ONLY


def test_corrupt_state_file_fails_open_to_log_only_never_enforced(tmp_path, monkeypatch):
    state_path = _use_temp_state(tmp_path, monkeypatch)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        f.write("{ not valid json")
    # Must never crash the real task turn calling this, and must never fail open to enforced.
    assert get_gate_mode("intake_grounding_gate") == GateMode.LOG_ONLY


def test_set_gate_mode_to_enforced_requires_and_records_approval(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    set_gate_mode("post_install_graph_diff", GateMode.ENFORCED, approved_by="test-user")
    assert get_gate_mode("post_install_graph_diff") == GateMode.ENFORCED
    states = all_gate_states()
    entry = states["post_install_graph_diff"]
    assert entry.mode == GateMode.ENFORCED
    assert entry.graduation_approved_by == "test-user"
    assert entry.enforced_since is not None


def test_other_gates_unaffected_by_one_gates_promotion(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    set_gate_mode("post_install_graph_diff", GateMode.ENFORCED, approved_by="test-user")
    assert get_gate_mode("intake_grounding_gate") == GateMode.LOG_ONLY
    assert get_gate_mode("decomposition_blast_radius") == GateMode.LOG_ONLY


def test_disabled_mode_is_a_real_distinct_state(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    set_gate_mode("rollback_dry_run_probe", GateMode.DISABLED, approved_by="test-user")
    assert get_gate_mode("rollback_dry_run_probe") == GateMode.DISABLED


def test_all_gate_states_covers_every_known_gate(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    states = all_gate_states()
    assert set(states.keys()) == flags_module._KNOWN_GATES
