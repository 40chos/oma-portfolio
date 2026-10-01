"""Phase 33 implementation (2026-08-11): unit tests for
tools_odoo/acl_request_extractor.py -- the extraction-and-validation step for
visibility/ACL requests (§3 item 2a), using the real historical goal texts mined
from agent_memory_events as fixtures.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.acl_request_extractor import (
    extract_acl_request,
    resolve_acl_request,
    format_resolved_acl_target_block,
    format_acl_resolution_failure_block,
    resolve_current_acl_target_block,
)


class _FakeContract:
    def __init__(self, goal, module_identity):
        self.goal = goal
        self.module_identity = module_identity


def test_extracts_real_confirmed_goal_shape_internal_approver():
    goal = "On the meerwerk form, only System Administrators should see who approved this record internally."
    extracted = extract_acl_request(goal)
    assert extracted is not None
    assert extracted.role_phrase == "System Administrators"
    assert "approved" in extracted.target_description
    print("PASS: extracts the real 'internal approver' goal shape")


def test_extracts_real_confirmed_goal_shape_send_reminder_button():
    goal = "On the customer list view, only System Administrators should have access to a Send Reminder action button in the row."
    extracted = extract_acl_request(goal)
    assert extracted is not None
    assert extracted.role_phrase == "System Administrators"
    assert "reminder" in extracted.target_description.lower()
    print("PASS: extracts the real 'Send Reminder button' goal shape")


def test_returns_none_for_a_goal_with_no_acl_shape():
    goal = "Add a computed field 'total_hours' to oma.service.ticket."
    assert extract_acl_request(goal) is None
    print("PASS: a goal with no ACL-restriction shape extracts nothing")


def test_resolves_when_the_exact_role_phrase_is_a_real_group():
    extracted = extract_acl_request("Only Managers should see the internal notes field.")
    resolved = resolve_acl_request(
        extracted, "project.meerwerk", "odoo16_dev",
        check_group_exists_fn=lambda db, name: name == "Managers",
        check_field_exists_fn=lambda db, model, field: True,
        resolve_group_external_id_fn=lambda db, name: "some_module.group_managers",
    )
    from tools_odoo.acl_request_extractor import ResolvedAclTarget
    assert isinstance(resolved, ResolvedAclTarget)
    assert resolved.resolved_group_name == "Managers"
    assert resolved.resolved_group_external_id == "some_module.group_managers"
    block = format_resolved_acl_target_block(resolved)
    assert "some_module.group_managers" in block and "project.meerwerk" in block
    print("PASS: an exact-match real group resolves directly, including its real external ID")


def test_resolves_via_known_alias_when_exact_phrase_fails(monkeypatch=None):
    extracted = extract_acl_request("Only System Administrators should see the credit note field.")

    def fake_check_group(db, name):
        return name == "Administration / Settings"  # the exact phrase fails; the alias succeeds

    resolved = resolve_acl_request(
        extracted, "res.partner", "odoo16_dev", check_group_exists_fn=fake_check_group,
        resolve_group_external_id_fn=lambda db, name: "base.group_system",
    )
    from tools_odoo.acl_request_extractor import ResolvedAclTarget
    assert isinstance(resolved, ResolvedAclTarget)
    assert resolved.resolved_group_name == "Administration / Settings"
    assert resolved.resolved_group_external_id == "base.group_system"
    print("PASS: falls back to a known alias only after the exact phrase fails against the live check")


def test_fails_loudly_when_group_exists_but_has_no_resolvable_external_id():
    """Real, confirmed gap found live (2026-08-11): a bare display name is not valid
    `groups=` syntax -- a group with no resolvable external ID must be treated as a
    resolution FAILURE, never proceed with only the display name."""
    extracted = extract_acl_request("Only Managers should see the internal notes field.")
    resolved = resolve_acl_request(
        extracted, "project.meerwerk", "odoo16_dev",
        check_group_exists_fn=lambda db, name: name == "Managers",
        resolve_group_external_id_fn=lambda db, name: None,
    )
    from tools_odoo.acl_request_extractor import AclResolutionFailure
    assert isinstance(resolved, AclResolutionFailure)
    assert "external ID" in resolved.reason
    print("PASS: a group with no resolvable external ID fails loudly instead of proceeding with a display name")


def test_fails_loudly_never_guesses_when_no_real_group_matches():
    extracted = extract_acl_request("Only Wizards of Oz should see the magic field.")
    resolved = resolve_acl_request(
        extracted, "res.partner", "odoo16_dev", check_group_exists_fn=lambda db, name: False,
    )
    from tools_odoo.acl_request_extractor import AclResolutionFailure
    assert isinstance(resolved, AclResolutionFailure)
    assert "does not resolve" in resolved.reason
    block = format_acl_resolution_failure_block(resolved)
    assert "never guess" in block.lower() or "ask a human" in block.lower()
    print("PASS: an unresolvable role phrase fails loudly instead of guessing a group name")


def test_unavailable_live_check_is_reported_as_a_failure_not_silently_skipped():
    extracted = extract_acl_request("Only Managers should see the internal notes field.")
    resolved = resolve_acl_request(
        extracted, "project.meerwerk", "odoo16_dev", check_group_exists_fn=lambda db, name: None,
    )
    from tools_odoo.acl_request_extractor import AclResolutionFailure
    assert isinstance(resolved, AclResolutionFailure)
    assert "could not confirm" in resolved.reason
    print("PASS: a live check returning 'unknown' (None) is reported as a failure, never treated as success")


def test_resolve_current_acl_target_block_end_to_end_success(monkeypatch):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: name == "Settings")
    monkeypatch.setattr(schema_client_module, "resolve_group_external_id_fast", lambda db, name: "base.group_system")

    contract = _FakeContract(
        goal="Only Administrators should see the internal notes field.", module_identity="res.partner",
    )
    block = resolve_current_acl_target_block(contract, "odoo16_dev")
    assert "RESOLVED ACL TARGET" in block
    assert "base.group_system" in block
    print("PASS: end-to-end block generation resolves via the real check functions by default")


def test_resolve_current_acl_target_block_empty_when_no_acl_shape(monkeypatch):
    contract = _FakeContract(goal="Add a computed field to res.partner.", module_identity="res.partner")
    assert resolve_current_acl_target_block(contract, "odoo16_dev") == ""
    print("PASS: a goal with no ACL shape renders no block at all")


def test_resolve_current_acl_target_block_empty_when_no_module_identity(monkeypatch):
    contract = _FakeContract(goal="Only Managers should see the internal notes field.", module_identity=None)
    assert resolve_current_acl_target_block(contract, "odoo16_dev") == ""
    print("PASS: no resolvable target model renders no block at all, never raises")


def test_resolve_current_acl_target_block_failure_notice_when_unresolvable(monkeypatch):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: False)

    contract = _FakeContract(
        goal="Only Wizards of Oz should see the magic field.", module_identity="res.partner",
    )
    block = resolve_current_acl_target_block(contract, "odoo16_dev")
    assert "COULD NOT BE VALIDATED" in block
    print("PASS: an unresolvable role renders the explicit failure notice, not silence or a guess")


if __name__ == "__main__":
    test_extracts_real_confirmed_goal_shape_internal_approver()
    test_extracts_real_confirmed_goal_shape_send_reminder_button()
    test_returns_none_for_a_goal_with_no_acl_shape()
    test_resolves_when_the_exact_role_phrase_is_a_real_group()
    test_resolves_via_known_alias_when_exact_phrase_fails()
    test_fails_loudly_never_guesses_when_no_real_group_matches()
    test_unavailable_live_check_is_reported_as_a_failure_not_silently_skipped()
    print("\nALL ACL REQUEST EXTRACTOR TESTS PASSED")
