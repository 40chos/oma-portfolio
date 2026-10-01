"""Phase 34 execution (2026-08-11): unit tests for
specialists/build/access_row_enforcement_gate.py -- the deterministic autofix and
hard enforcement gate for model-level access-row changes.
"""

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.access_row_enforcement_gate import (
    autofix_apply_resolved_access_row_changes,
    validate_resolved_access_row_changes_are_applied,
)


@dataclass
class _FakeGenerated:
    security_csv: str = ""


_REAL_GOAL = (
    "Edit the existing, already-installed module oma_add_a_risk_register_383d30e7 directly: "
    "its own security/ir.model.access.csv currently grants base.group_user full access to "
    "oma.risk.register -- narrow that row to read-only, and add a separate read/write row for "
    "a group named 'Settings'."
)

_ORIGINAL_CSV = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    "access_oma_risk_register_user,access.oma.risk.register.user,model_oma_risk_register,"
    "base.group_user,1,1,1,1\n"
)


def _patch_group_checks(monkeypatch, external_id="base.group_system"):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: name in ("base.group_user", "Settings"))
    monkeypatch.setattr(schema_client_module, "resolve_group_external_id_fast", lambda db, name: external_id if name == "Settings" else None)


def test_autofix_narrows_existing_row_and_adds_new_row(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(security_csv=_ORIGINAL_CSV)
    autofix_apply_resolved_access_row_changes(generated, _REAL_GOAL, "oma.risk.register", "odoo16_dev")

    lines = generated.security_csv.splitlines()
    narrowed = next(l for l in lines if "base.group_user" in l)
    cols = narrowed.split(",")
    assert cols[4:8] == ["1", "0", "0", "0"], f"expected narrowed to read-only, got {cols[4:8]}"
    added = next(l for l in lines if "base.group_system" in l)
    added_cols = added.split(",")
    assert added_cols[4:8] == ["1", "1", "0", "0"], f"expected read/write, got {added_cols[4:8]}"
    print("PASS: autofix correctly narrows the existing row and adds the new resolved row")


def test_enforcement_gate_rejects_unapplied_changes(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(security_csv=_ORIGINAL_CSV)  # never autofixed
    try:
        validate_resolved_access_row_changes_are_applied(generated, _REAL_GOAL, "oma.risk.register", "odoo16_dev")
        assert False, "should have raised"
    except ValueError as e:
        assert "base.group_user" in str(e) or "base.group_system" in str(e)
    print("PASS: the hard gate rejects a CSV that never applied the resolved changes")


def test_enforcement_gate_passes_after_autofix(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(security_csv=_ORIGINAL_CSV)
    autofix_apply_resolved_access_row_changes(generated, _REAL_GOAL, "oma.risk.register", "odoo16_dev")
    validate_resolved_access_row_changes_are_applied(generated, _REAL_GOAL, "oma.risk.register", "odoo16_dev")
    print("PASS: the hard gate passes once the autofix has actually applied the changes")


def test_no_access_row_shape_is_a_no_op():
    generated = _FakeGenerated(security_csv=_ORIGINAL_CSV)
    autofix_apply_resolved_access_row_changes(generated, "Add a field x.", "oma.risk.register", "odoo16_dev")
    assert generated.security_csv == _ORIGINAL_CSV
    validate_resolved_access_row_changes_are_applied(generated, "Add a field x.", "oma.risk.register", "odoo16_dev")
    print("PASS: a goal with no access-row shape is a no-op for both the autofix and the gate")


def test_no_module_identity_never_fires():
    generated = _FakeGenerated(security_csv=_ORIGINAL_CSV)
    autofix_apply_resolved_access_row_changes(generated, _REAL_GOAL, None, "odoo16_dev")
    assert generated.security_csv == _ORIGINAL_CSV
    validate_resolved_access_row_changes_are_applied(generated, _REAL_GOAL, None, "odoo16_dev")
    print("PASS: no resolvable target model means neither function fires")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
