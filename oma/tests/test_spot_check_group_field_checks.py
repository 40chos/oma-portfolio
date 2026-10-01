"""Phase 20 Area 2 (2026-07-19): unit tests for the three new
tools_odoo/spot_check.py functions -- check_group_exists,
check_group_model_access, check_field_group_restricted -- against a
monkeypatched _run_odoo_shell_script, no live SSH/DB needed.

Real bug this fixes: the first version of check_field_group_restricted
queried `ir_model_fields_group_rel`, a table Odoo's own source code
labels "# CLEANME unimplemented field (empty table)" -- Odoo never
writes to it. The real mechanism, confirmed by reading odoo/models.py
directly, is the field object's own in-memory `.groups` string
attribute, checked via `user_has_groups()` at read/write time with no
DB table involved at all. This caused a real false-alarm: an entire
deep-reverify pass came back 0/9 passed because this check wrongly
rejected genuinely-correct Build-generated code.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.spot_check as spot_check_module
from tools_odoo.spot_check import (
    check_field_group_restricted,
    check_group_exists,
    check_group_model_access,
)


def test_check_group_exists_true(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: "GROUP_CHECK found=True\n")
    assert check_group_exists("odoo16_dev", "Real Group") is True
    print("PASS: check_group_exists returns True when the group is found")


def test_check_group_exists_false(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: "GROUP_CHECK found=False\n")
    assert check_group_exists("odoo16_dev", "Ghost Group") is False
    print("PASS: check_group_exists returns False when the group is not found")


def test_check_group_exists_none_on_uncertainty(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: None)
    assert check_group_exists("odoo16_dev", "Any Group") is None
    print("PASS: check_group_exists returns None (never guesses) on genuine uncertainty")


def test_check_group_model_access_matches(monkeypatch):
    monkeypatch.setattr(
        spot_check_module, "_run_odoo_shell_script",
        lambda db, script, timeout=90: "ACCESS_CHECK rows=[(True, True, False, False)]\n",
    )
    result = check_group_model_access("odoo16_dev", "Real Group", "res.partner", True, True, False, False)
    assert result == (True, "group 'Real Group' on model 'res.partner' matches all claimed permissions")
    print("PASS: check_group_model_access confirms matching real permissions")


def test_check_group_model_access_mismatch(monkeypatch):
    monkeypatch.setattr(
        spot_check_module, "_run_odoo_shell_script",
        lambda db, script, timeout=90: "ACCESS_CHECK rows=[(True, True, True, False)]\n",
    )
    result = check_group_model_access("odoo16_dev", "Real Group", "res.partner", True, True, True, True)
    assert result[0] is False
    assert "delete" in result[1]
    print("PASS: check_group_model_access reports a real permission mismatch (delete missing)")


def test_check_group_model_access_no_rows(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: "ACCESS_CHECK rows=[]\n")
    result = check_group_model_access("odoo16_dev", "Real Group", "res.partner", True, None, None, None)
    assert result[0] is False
    assert "NO access rows" in result[1]
    print("PASS: check_group_model_access reports no access rows at all as a real failure")


def test_check_group_model_access_aggregates_multiple_rows_with_or(monkeypatch):
    """Odoo's own real semantics: multiple ir.model.access rows for the
    same group+model combine with OR (any one grant is enough).
    """
    monkeypatch.setattr(
        spot_check_module, "_run_odoo_shell_script",
        lambda db, script, timeout=90: "ACCESS_CHECK rows=[(True, False, False, False), (False, True, False, False)]\n",
    )
    result = check_group_model_access("odoo16_dev", "Real Group", "res.partner", True, True, False, False)
    assert result[0] is True
    print("PASS: multiple real access rows for the same group+model are OR-aggregated, matching real Odoo semantics")


def test_check_field_group_restricted_correctly_restricted(monkeypatch):
    """Real, confirmed correction (2026-07-19): reads the field's real
    in-memory `.groups` attribute, NOT the unimplemented
    ir_model_fields_group_rel table.
    """
    monkeypatch.setattr(
        spot_check_module, "_run_odoo_shell_script",
        lambda db, script, timeout=90: "FIELD_GROUPS=['Badge Expiry Viewers']\n",
    )
    result = check_field_group_restricted("odoo16_dev", "hr.employee", "badge_expiry_date", "Badge Expiry Viewers")
    assert result == (True, "hr.employee.badge_expiry_date is correctly restricted to 'Badge Expiry Viewers'")
    print("PASS: a field with the field.groups attribute correctly set is confirmed restricted")


def test_check_field_group_restricted_no_restriction(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: "FIELD_GROUPS=[]\n")
    result = check_field_group_restricted("odoo16_dev", "res.partner", "name", "Any Group")
    assert result[0] is False
    assert "NO group restriction" in result[1]
    print("PASS: a field with no groups set at all is correctly reported as unrestricted")


def test_check_field_group_restricted_wrong_group(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: "FIELD_GROUPS=['Other Group']\n")
    result = check_field_group_restricted("odoo16_dev", "res.partner", "secondary_email", "Real Group")
    assert result[0] is False
    print("PASS: a field restricted to a different group than claimed is correctly reported as a mismatch")


def test_check_field_group_restricted_field_missing_returns_none(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: "FIELD_GROUPS=NONE\n")
    result = check_field_group_restricted("odoo16_dev", "res.partner", "nonexistent_field", "Any Group")
    assert result is None
    print("PASS: a field that doesn't exist at all returns None (not this check's job -- field-existence is separate)")


if __name__ == "__main__":
    test_check_group_exists_true()
    test_check_group_exists_false()
    test_check_group_exists_none_on_uncertainty()
    test_check_group_model_access_matches()
    test_check_group_model_access_mismatch()
    test_check_group_model_access_no_rows()
    test_check_group_model_access_aggregates_multiple_rows_with_or()
    test_check_field_group_restricted_correctly_restricted()
    test_check_field_group_restricted_no_restriction()
    test_check_field_group_restricted_wrong_group()
    test_check_field_group_restricted_field_missing_returns_none()
    print("\nALL SPOT-CHECK GROUP/FIELD TESTS PASSED")
