"""Phase 33 implementation (2026-08-11): unit tests for
specialists/build/acl_code_generator.py -- the mechanical code-generation half
(§3 item 2b) of the ACL fix. Only ever consumes a validated ResolvedAclTarget,
and always uses the group's real external ID (never the display name -- see
acl_enforcement_gate.py's own docstring for the real, confirmed live bug this
distinction closes).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.acl_request_extractor import ResolvedAclTarget
from specialists.build.acl_code_generator import (
    generate_field_groups_kwarg_snippet,
    generate_button_groups_attribute_snippet,
)


def test_generates_field_groups_kwarg_for_a_simple_declaration():
    resolved = ResolvedAclTarget(
        model_name="project.fieldjob", resolved_group_name="Administration / Settings",
        resolved_group_external_id="base.group_system",
        field_or_action_name="internal_approver_id", is_field=True,
    )
    line = "    internal_approver_id = fields.Many2one('res.users', string='Approved by')"
    result = generate_field_groups_kwarg_snippet(resolved, line)
    assert result == "    internal_approver_id = fields.Many2one('res.users', string='Approved by', groups=\"base.group_system\")"
    print("PASS: generates a correct groups= kwarg using the real external ID, not the display name")


def test_refuses_to_double_restrict_a_field():
    resolved = ResolvedAclTarget(
        model_name="res.partner", resolved_group_name="Managers", resolved_group_external_id="base.group_user",
        field_or_action_name="x", is_field=True,
    )
    line = "    x = fields.Char(groups=\"base.group_user\")"
    try:
        generate_field_groups_kwarg_snippet(resolved, line)
        assert False, "should have raised"
    except ValueError as e:
        assert "already has" in str(e)
    print("PASS: refuses to add a second groups= kwarg to an already-restricted field")


def test_refuses_a_button_target_on_the_field_generator():
    resolved = ResolvedAclTarget(
        model_name="res.partner", resolved_group_name="Managers", resolved_group_external_id="base.group_user",
        field_or_action_name="action_send", is_field=False,
    )
    try:
        generate_field_groups_kwarg_snippet(resolved, "    x = fields.Char()")
        assert False, "should have raised"
    except ValueError as e:
        assert "field target" in str(e)
    print("PASS: refuses an action/button target on the field-specific generator")


def test_generates_button_groups_attribute_self_closing_tag():
    resolved = ResolvedAclTarget(
        model_name="res.partner", resolved_group_name="Administration / Settings",
        resolved_group_external_id="base.group_system",
        field_or_action_name="action_send_reminder", is_field=False,
    )
    line = '<button name="action_send_reminder" string="Send Reminder" type="object"/>'
    result = generate_button_groups_attribute_snippet(resolved, line)
    assert result == '<button name="action_send_reminder" string="Send Reminder" type="object" groups="base.group_system"/>'
    print("PASS: generates a correct groups= attribute using the real external ID for a self-closing button tag")


def test_generates_button_groups_attribute_open_tag():
    resolved = ResolvedAclTarget(
        model_name="res.partner", resolved_group_name="Managers", resolved_group_external_id="base.group_user",
        field_or_action_name="action_x", is_field=False,
    )
    line = '<button name="action_x" string="X">'
    result = generate_button_groups_attribute_snippet(resolved, line)
    assert result == '<button name="action_x" string="X" groups="base.group_user">'
    print("PASS: generates a correct groups= attribute for a non-self-closing button tag")


def test_refuses_to_double_restrict_a_button():
    resolved = ResolvedAclTarget(
        model_name="res.partner", resolved_group_name="Managers", resolved_group_external_id="base.group_user",
        field_or_action_name="action_x", is_field=False,
    )
    line = '<button name="action_x" groups="base.group_user"/>'
    try:
        generate_button_groups_attribute_snippet(resolved, line)
        assert False, "should have raised"
    except ValueError as e:
        assert "already has" in str(e)
    print("PASS: refuses to add a second groups= attribute to an already-restricted button")


if __name__ == "__main__":
    test_generates_field_groups_kwarg_for_a_simple_declaration()
    test_refuses_to_double_restrict_a_field()
    test_refuses_a_button_target_on_the_field_generator()
    test_generates_button_groups_attribute_self_closing_tag()
    test_generates_button_groups_attribute_open_tag()
    test_refuses_to_double_restrict_a_button()
    print("\nALL ACL CODE GENERATOR TESTS PASSED")
