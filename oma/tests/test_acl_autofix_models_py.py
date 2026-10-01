"""Phase 33 follow-up (2026-08-11): unit tests for
specialists/build/acl_code_generator.py's
autofix_apply_resolved_acl_target_to_models_py() -- the deterministic field-level
(data-layer) ACL fix, added after Code-Review correctly rejected a view-level-
only restriction live as insufficient for real Odoo security.
"""

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.acl_code_generator import autofix_apply_resolved_acl_target_to_models_py


@dataclass
class _FakeGenerated:
    models_py: str = ""


_REAL_GOAL = (
    "On the warranty claim form, only Administrators should see the internal cost estimate "
    "field.\n\nModule: oma_create_a_small_new_c1e5cf10\nModel: warranty.claim (inherit)\n"
    "Field: internal_cost_estimate (Text)\n"
)


def _patch_group_checks(monkeypatch, external_id="base.group_system"):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: name == "Settings")
    monkeypatch.setattr(schema_client_module, "resolve_group_external_id_fast", lambda db, name: external_id)


def test_applies_the_real_external_id_to_the_matching_field_declaration(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(models_py=(
        "class WarrantyClaim(models.Model):\n"
        "    _inherit = 'warranty.claim'\n"
        "    internal_cost_estimate = fields.Text(string='Internal Cost Estimate')\n"
    ))
    autofix_apply_resolved_acl_target_to_models_py(generated, _REAL_GOAL, "warranty.claim", "odoo16_dev")
    assert 'groups="base.group_system"' in generated.models_py
    assert "internal_cost_estimate" in generated.models_py
    print("PASS: applies the real external ID to the exact matching field declaration")


def test_never_double_restricts_an_already_restricted_field(monkeypatch):
    _patch_group_checks(monkeypatch)
    original = (
        "class WarrantyClaim(models.Model):\n"
        "    _inherit = 'warranty.claim'\n"
        "    internal_cost_estimate = fields.Text(groups=\"some.other_group\")\n"
    )
    generated = _FakeGenerated(models_py=original)
    autofix_apply_resolved_acl_target_to_models_py(generated, _REAL_GOAL, "warranty.claim", "odoo16_dev")
    assert generated.models_py == original
    print("PASS: a field that already has SOME groups= kwarg is left untouched by this autofix")


def test_no_acl_shape_is_a_no_op():
    generated = _FakeGenerated(models_py="    x = fields.Char()\n")
    autofix_apply_resolved_acl_target_to_models_py(generated, "Add a field x.", "warranty.claim", "odoo16_dev")
    assert generated.models_py == "    x = fields.Char()\n"
    print("PASS: a goal with no ACL shape leaves models_py untouched")


def test_no_matching_field_declaration_is_a_no_op(monkeypatch):
    _patch_group_checks(monkeypatch)
    generated = _FakeGenerated(models_py="class X(models.Model):\n    _inherit = 'warranty.claim'\n")
    autofix_apply_resolved_acl_target_to_models_py(generated, _REAL_GOAL, "warranty.claim", "odoo16_dev")
    assert "groups=" not in generated.models_py
    print("PASS: no matching field declaration in models_py means no change is made")


def test_unresolvable_group_is_a_no_op(monkeypatch):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: False)
    generated = _FakeGenerated(models_py="    internal_cost_estimate = fields.Text()\n")
    autofix_apply_resolved_acl_target_to_models_py(generated, _REAL_GOAL, "warranty.claim", "odoo16_dev")
    assert "groups=" not in generated.models_py
    print("PASS: an unresolvable group leaves models_py untouched, never guesses")


def test_button_target_is_a_no_op_here(monkeypatch):
    """This autofix is specifically for FIELD data-layer restriction -- an
    action/button target has no equivalent field declaration to patch here."""
    _patch_group_checks(monkeypatch)
    goal = (
        "On the customer list view, only Administrators should have access to a Send Reminder "
        "action button in the row.\n\nModel: res.partner (inherit)\n"
    )
    generated = _FakeGenerated(models_py="    action_send_reminder = fields.Char()\n")
    autofix_apply_resolved_acl_target_to_models_py(generated, goal, "res.partner", "odoo16_dev")
    assert "groups=" not in generated.models_py
    print("PASS: an action/button target is left alone by this field-specific autofix")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
