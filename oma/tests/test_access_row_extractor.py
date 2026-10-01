"""Phase 34 execution (2026-08-11): unit tests for
tools_odoo/access_row_extractor.py -- the model-level access-row (ir.model.access.csv)
extraction-and-validation mechanism, using the real Batch M goal text as fixtures.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.access_row_extractor import (
    extract_access_row_request,
    resolve_access_row_request,
    ResolvedAccessRowChanges,
    AccessRowResolutionFailure,
    format_resolved_access_row_block,
)

_REAL_GOAL = (
    "Edit the existing, already-installed module oma_add_a_risk_register_383d30e7 directly: "
    "its own security/ir.model.access.csv currently grants base.group_user full access to "
    "oma.risk.register -- narrow that row to read-only, and add a separate read/write row for "
    "a group named 'Settings'."
)


def test_extracts_narrow_and_add_from_real_goal():
    extracted = extract_access_row_request(_REAL_GOAL)
    assert extracted is not None
    assert extracted.narrow_group_ref == "base.group_user"
    assert extracted.narrow_target_perms == (True, False, False, False)
    assert extracted.add_group_ref == "Settings"
    assert extracted.add_perms == (True, True, False, False)
    print("PASS: extracts both the narrow instruction and the add instruction from the real goal")


def test_returns_none_for_a_goal_with_no_access_row_shape():
    assert extract_access_row_request("Add a field x to res.partner.") is None
    print("PASS: a goal with no access-row shape extracts nothing")


def test_resolves_xmlid_group_ref_and_display_name_group_ref(monkeypatch):
    extracted = extract_access_row_request(_REAL_GOAL)

    def fake_check_group_exists(db, name):
        return name in ("base.group_user", "Settings")

    def fake_resolve_external_id(db, name):
        return "base.group_system" if name == "Settings" else None

    resolved = resolve_access_row_request(
        extracted, "oma.risk.register", "odoo16_dev",
        check_group_exists_fn=fake_check_group_exists,
        resolve_group_external_id_fn=fake_resolve_external_id,
    )
    assert isinstance(resolved, ResolvedAccessRowChanges)
    assert resolved.narrow.group_ref == "base.group_user"
    assert resolved.narrow.perm_read is True and resolved.narrow.perm_write is False
    assert resolved.add.group_ref == "base.group_system"
    assert resolved.add.perm_read is True and resolved.add.perm_write is True
    block = format_resolved_access_row_block(resolved)
    assert "base.group_user" in block and "base.group_system" in block
    print("PASS: an already-xmlid group ref is validated as-is; a display name resolves to its real external ID")


def test_fails_loudly_when_narrow_group_does_not_exist(monkeypatch):
    extracted = extract_access_row_request(_REAL_GOAL)
    resolved = resolve_access_row_request(
        extracted, "oma.risk.register", "odoo16_dev",
        check_group_exists_fn=lambda db, name: False,
    )
    assert isinstance(resolved, AccessRowResolutionFailure)
    print("PASS: a nonexistent group to narrow fails loudly instead of proceeding")


def test_only_add_instruction_present_still_extracts():
    goal = "Add a separate read-only row for the existing group_field_technician group on oma.equipment."
    extracted = extract_access_row_request(goal)
    assert extracted is not None
    assert extracted.add_group_ref == "group_field_technician"
    assert extracted.add_perms == (True, False, False, False)
    assert extracted.narrow_group_ref is None
    print("PASS: a goal with only an add-row instruction (no narrow) extracts correctly")


if __name__ == "__main__":
    test_extracts_narrow_and_add_from_real_goal()
    test_returns_none_for_a_goal_with_no_access_row_shape()
    test_resolves_xmlid_group_ref_and_display_name_group_ref(None)
    test_fails_loudly_when_narrow_group_does_not_exist(None)
    test_only_add_instruction_present_still_extracts()
    print("\nALL ACCESS ROW EXTRACTOR TESTS PASSED")
