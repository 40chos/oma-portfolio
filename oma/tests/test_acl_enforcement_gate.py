"""Phase 33 follow-up (2026-08-11): unit tests for
specialists/build/acl_enforcement_gate.py -- the hard-failing validator added
after live retests showed the context-only ACL block was not enough (Build
stopped guessing a fake group but still applied zero restriction at all), and
then strengthened again after a second live retest showed a bare display name
("Settings") is not valid `groups=` syntax at all -- Build must be checked
against the group's real external ID ("base.group_system"), never the display
name.
"""

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.acl_enforcement_gate import validate_resolved_acl_target_is_actually_applied


@dataclass
class _FakeGenerated:
    models_py: str = ""
    views_xml: str = ""


_REAL_GOAL = (
    "On the customer (contact) form, only Administrators should see the internal credit note "
    "field.\n\nModule: mis_base_extend\nModel: res.partner (inherit)\nField: internal_credit_note (Text)\n"
)


def _patch_group_checks(monkeypatch, external_id="base.group_system"):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: name == "Settings")
    monkeypatch.setattr(schema_client_module, "resolve_group_external_id_fast", lambda db, name: external_id)


def test_real_confirmed_gap_unrestricted_field_is_hard_rejected(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(models_py="    internal_credit_note = fields.Text()\n")
    try:
        validate_resolved_acl_target_is_actually_applied(generated, _REAL_GOAL, "res.partner", "odoo16_dev")
        assert False, "should have raised"
    except ValueError as e:
        assert "internal_credit_note" in str(e)
        assert "base.group_system" in str(e)
    print("PASS: the real, confirmed 'context provided but never applied' gap is now hard-rejected")


def test_display_name_alone_is_not_accepted_as_compliance(monkeypatch):
    """Real, confirmed live gap: Build satisfied an earlier, weaker version of this
    gate by adding SOME groups= value, but the wrong one (a plausible-looking
    fabricated external ID) -- checking for the EXACT resolved external ID, not
    just "any groups= present," is what actually closes this."""
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(models_py='    internal_credit_note = fields.Text(groups="Settings")\n')
    try:
        validate_resolved_acl_target_is_actually_applied(generated, _REAL_GOAL, "res.partner", "odoo16_dev")
        assert False, "should have raised"
    except ValueError as e:
        assert "base.group_system" in str(e)
    print("PASS: a groups= value using the display name instead of the real external ID is still rejected")


def test_wrong_group_external_id_is_still_rejected(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(models_py='    internal_credit_note = fields.Text(groups="base.group_user")\n')
    try:
        validate_resolved_acl_target_is_actually_applied(generated, _REAL_GOAL, "res.partner", "odoo16_dev")
        assert False, "should have raised"
    except ValueError:
        pass
    print("PASS: a real-looking but WRONG external ID (not the resolved one) is still rejected")


def test_field_declaration_with_correct_external_id_passes(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(models_py='    internal_credit_note = fields.Text(groups="base.group_system")\n')
    validate_resolved_acl_target_is_actually_applied(generated, _REAL_GOAL, "res.partner", "odoo16_dev")
    print("PASS: a field declaration using the EXACT resolved external ID passes")


def test_view_level_only_restriction_is_no_longer_sufficient_for_a_field_target(monkeypatch):
    """Real, confirmed live gap (2026-08-11): Code-Review correctly rejected a
    view-level-only restriction as insufficient -- it still leaves the field
    readable via the ORM/API/other views. A field target now REQUIRES the
    field-declaration restriction specifically."""
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(
        models_py="    internal_credit_note = fields.Text()\n",
        views_xml='<field name="internal_credit_note" groups="base.group_system"/>',
    )
    try:
        validate_resolved_acl_target_is_actually_applied(generated, _REAL_GOAL, "res.partner", "odoo16_dev")
        assert False, "should have raised"
    except ValueError as e:
        assert "data-layer" in str(e) or "ORM" in str(e)
    print("PASS: a view-level-only restriction on a field target no longer satisfies the gate")


def test_field_level_restriction_satisfies_the_gate_even_without_any_view_change(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(models_py='    internal_credit_note = fields.Text(groups="base.group_system")\n')
    validate_resolved_acl_target_is_actually_applied(generated, _REAL_GOAL, "res.partner", "odoo16_dev")
    print("PASS: the field-declaration restriction alone satisfies the gate for a field target")


def test_action_button_target_still_accepts_view_level_restriction(monkeypatch):
    """An action/button target has no equivalent 'data layer' the way a field
    does -- view-level restriction is the correct, sufficient mechanism there."""
    _patch_group_checks(monkeypatch)
    goal = (
        "On the customer list view, only Administrators should have access to a Send Reminder "
        "action button in the row.\n\nModel: res.partner (inherit)\n"
    )
    generated = _FakeGenerated(
        models_py="    def action_send_reminder(self):\n        pass\n",
        views_xml='<button name="action_send_reminder" groups="base.group_system"/>',
    )
    validate_resolved_acl_target_is_actually_applied(generated, goal, "res.partner", "odoo16_dev")
    print("PASS: an action/button target is still satisfied by a view-level restriction")


def test_no_acl_shape_in_goal_never_fires():
    generated = _FakeGenerated(models_py="    x = fields.Char()\n")
    validate_resolved_acl_target_is_actually_applied(generated, "Add a field x to res.partner.", "res.partner", "odoo16_dev")
    print("PASS: a goal with no ACL-restriction shape never fires this gate")


def test_unresolvable_group_never_hard_fails_here(monkeypatch):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: False)

    generated = _FakeGenerated(models_py="    internal_credit_note = fields.Text()\n")
    # Extraction can't validate a real group -- that failure is surfaced separately
    # via the context block; this gate only enforces a CONFIRMED, resolved case.
    validate_resolved_acl_target_is_actually_applied(generated, _REAL_GOAL, "res.partner", "odoo16_dev")
    print("PASS: an unresolvable group is not this gate's job -- it never hard-fails on its own")


def test_no_module_identity_never_fires():
    generated = _FakeGenerated(models_py="    internal_credit_note = fields.Text()\n")
    validate_resolved_acl_target_is_actually_applied(generated, _REAL_GOAL, None, "odoo16_dev")
    print("PASS: no resolvable target model means this gate never fires, never raises")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
