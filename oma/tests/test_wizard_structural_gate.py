"""Phase 34 execution (2026-08-11/12): unit tests for
specialists/build/wizard_structural_gate.py -- the two real, general
validators built to safely remove wizard_transient_model from
contracts/unsupported_domains.json's permanent block-list.
"""

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.wizard_structural_gate import (
    validate_wizard_intent_uses_transient_model_not_persistent_model,
    validate_wizard_window_action_opens_as_a_dialog,
    find_new_transient_model_names,
)


@dataclass
class _FakeGenerated:
    models_py: str = ""
    views_xml: str = ""
    extra_data_files: dict | None = None


_WIZARD_GOAL = (
    "When I click 'Create Invoice' for multiple meerwerk records, I want a popup first that "
    "shows me which ones will be included and lets me confirm before the invoices are actually "
    "created."
)


def test_rejects_persistent_model_for_a_wizard_shaped_class_name():
    generated = _FakeGenerated(models_py=(
        "class InvoiceConfirmWizard(models.Model):\n"
        "    _name = 'invoice.confirm.wizard'\n"
        "    meerwerk_ids = fields.Many2many('project.meerwerk')\n"
    ))
    try:
        validate_wizard_intent_uses_transient_model_not_persistent_model(generated, _WIZARD_GOAL)
        assert False, "should have raised"
    except ValueError as e:
        assert "TransientModel" in str(e)
    print("PASS: a wizard-shaped class using models.Model instead of models.TransientModel is rejected")


def test_accepts_correct_transient_model():
    generated = _FakeGenerated(models_py=(
        "class InvoiceConfirmWizard(models.TransientModel):\n"
        "    _name = 'invoice.confirm.wizard'\n"
        "    meerwerk_ids = fields.Many2many('project.meerwerk')\n"
    ))
    validate_wizard_intent_uses_transient_model_not_persistent_model(generated, _WIZARD_GOAL)
    print("PASS: a correctly-declared TransientModel wizard passes")


def test_no_wizard_intent_never_fires():
    generated = _FakeGenerated(models_py="class X(models.Model):\n    _name = 'x.wizard.thing'\n")
    validate_wizard_intent_uses_transient_model_not_persistent_model(generated, "Add a field to res.partner.")
    print("PASS: a goal with no wizard/popup intent never fires this check")


def test_non_wizard_named_new_model_is_untouched():
    generated = _FakeGenerated(models_py="class X(models.Model):\n    _name = 'oma.risk.register'\n")
    validate_wizard_intent_uses_transient_model_not_persistent_model(generated, _WIZARD_GOAL)
    print("PASS: a new model with no wizard-related name is left alone even when the goal has wizard intent")


def test_find_new_transient_model_names():
    generated = _FakeGenerated(models_py=(
        "class A(models.TransientModel):\n    _name = 'a.wizard'\n\n"
        "class B(models.Model):\n    _name = 'b.persistent'\n"
    ))
    assert find_new_transient_model_names(generated) == {"a.wizard"}
    print("PASS: find_new_transient_model_names only returns TransientModel-declared names")


def test_rejects_window_action_missing_target_new():
    generated = _FakeGenerated(
        views_xml=(
            '<record id="action_x" model="ir.actions.act_window">\n'
            '    <field name="res_model">invoice.confirm.wizard</field>\n'
            '    <field name="view_mode">form</field>\n'
            "</record>\n"
        ),
    )
    try:
        validate_wizard_window_action_opens_as_a_dialog(generated, _WIZARD_GOAL, {"invoice.confirm.wizard"})
        assert False, "should have raised"
    except ValueError as e:
        assert "target" in str(e)
    print("PASS: a window action for a wizard model missing target=new is rejected")


def test_accepts_window_action_with_target_new():
    generated = _FakeGenerated(
        views_xml=(
            '<record id="action_x" model="ir.actions.act_window">\n'
            '    <field name="res_model">invoice.confirm.wizard</field>\n'
            '    <field name="view_mode">form</field>\n'
            '    <field name="target">new</field>\n'
            "</record>\n"
        ),
    )
    validate_wizard_window_action_opens_as_a_dialog(generated, _WIZARD_GOAL, {"invoice.confirm.wizard"})
    print("PASS: a window action with target=new passes")


def test_window_action_for_a_non_wizard_model_is_untouched():
    generated = _FakeGenerated(
        views_xml=(
            '<record id="action_x" model="ir.actions.act_window">\n'
            '    <field name="res_model">project.meerwerk</field>\n'
            "</record>\n"
        ),
    )
    validate_wizard_window_action_opens_as_a_dialog(generated, _WIZARD_GOAL, {"invoice.confirm.wizard"})
    print("PASS: a window action for a non-wizard model is never touched, even with wizard intent present")


def test_no_wizard_models_never_fires():
    generated = _FakeGenerated(
        views_xml='<record id="action_x" model="ir.actions.act_window"><field name="res_model">x</field></record>',
    )
    validate_wizard_window_action_opens_as_a_dialog(generated, _WIZARD_GOAL, set())
    print("PASS: no wizard models this round means the check never fires")


if __name__ == "__main__":
    test_rejects_persistent_model_for_a_wizard_shaped_class_name()
    test_accepts_correct_transient_model()
    test_no_wizard_intent_never_fires()
    test_non_wizard_named_new_model_is_untouched()
    test_find_new_transient_model_names()
    test_rejects_window_action_missing_target_new()
    test_accepts_window_action_with_target_new()
    test_window_action_for_a_non_wizard_model_is_untouched()
    test_no_wizard_models_never_fires()
    print("\nALL WIZARD STRUCTURAL GATE TESTS PASSED")
