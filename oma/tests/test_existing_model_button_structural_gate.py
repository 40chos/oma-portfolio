"""Phase 33 implementation (2026-08-11): unit tests for
specialists/build/existing_model_button_structural_gate.py -- the two structural
defect checks (§3 item 3), grounded directly in real `gates_disagree` samples
read from production history.
"""

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.existing_model_button_structural_gate import (
    validate_new_model_signal_does_not_use_inherit_without_name,
    validate_enumerated_required_pieces_are_all_present,
)


@dataclass
class _FakeGenerated:
    models_py: str = ""
    views_xml: str = ""


_REAL_CONTAINER_GOAL = (
    "Add a 'Container count' smart button to the project form. There is no existing "
    "container concept or button in project_meerwerk to copy -- build this as a "
    "self-contained new model within this task's own scope, project.container, tracking "
    "one container per project."
)

_REAL_CUSTOMER_OVERVIEW_GOAL = (
    "Add a single Customer Overview smart button on res.partner's form view. Required "
    "pieces (both are needed -- do not omit either): a computed field project_count "
    "showing the number of related projects, and the smart button itself linking to them."
)


def test_real_confirmed_defect_inherit_without_name_on_signaled_new_model():
    generated = _FakeGenerated(models_py=(
        "class ProjectContainer(models.Model):\n"
        "    _inherit = 'project.container'\n"
        "    count = fields.Integer()\n"
    ))
    try:
        validate_new_model_signal_does_not_use_inherit_without_name(generated, _REAL_CONTAINER_GOAL)
        assert False, "should have raised"
    except ValueError as e:
        assert "project.container" in str(e)
        assert "_inherit" in str(e) and "_name" in str(e)
    print("PASS: the real, confirmed inherit-without-name defect on a signaled-new-model is caught")


def test_declaring_name_correctly_is_never_flagged():
    generated = _FakeGenerated(models_py=(
        "class ProjectContainer(models.Model):\n"
        "    _name = 'project.container'\n"
        "    count = fields.Integer()\n"
    ))
    validate_new_model_signal_does_not_use_inherit_without_name(generated, _REAL_CONTAINER_GOAL)
    print("PASS: correctly declaring _name for a signaled-new-model is never flagged")


def test_inherit_on_an_unrelated_model_is_never_flagged():
    generated = _FakeGenerated(models_py=(
        "class ProjectExtension(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    x = fields.Integer()\n"
    ))
    validate_new_model_signal_does_not_use_inherit_without_name(generated, _REAL_CONTAINER_GOAL)
    print("PASS: an _inherit on a model unrelated to the signaled new model is never flagged")


def test_no_new_model_signal_in_goal_never_fires():
    generated = _FakeGenerated(models_py="class X(models.Model):\n    _inherit = 'project.container'\n")
    validate_new_model_signal_does_not_use_inherit_without_name(generated, "Add a field to res.partner.")
    print("PASS: a goal with no 'build this as a new model' signal never fires the check")


def test_real_confirmed_defect_enumerated_required_field_missing():
    generated = _FakeGenerated(models_py="class ResPartner(models.Model):\n    _inherit = 'res.partner'\n")
    try:
        validate_enumerated_required_pieces_are_all_present(generated, _REAL_CUSTOMER_OVERVIEW_GOAL)
        assert False, "should have raised"
    except ValueError as e:
        assert "project_count" in str(e)
    print("PASS: the real, confirmed missing 'project_count' enumerated-required-piece defect is caught")


def test_enumerated_required_field_present_is_never_flagged():
    generated = _FakeGenerated(models_py=(
        "class ResPartner(models.Model):\n"
        "    _inherit = 'res.partner'\n"
        "    project_count = fields.Integer(compute='_compute_project_count')\n"
    ))
    validate_enumerated_required_pieces_are_all_present(generated, _REAL_CUSTOMER_OVERVIEW_GOAL)
    print("PASS: when the enumerated required field genuinely is present, nothing is flagged")


def test_field_present_only_in_views_xml_is_also_accepted():
    generated = _FakeGenerated(views_xml="<field name='project_count'/>")
    validate_enumerated_required_pieces_are_all_present(generated, _REAL_CUSTOMER_OVERVIEW_GOAL)
    print("PASS: a required piece present in views.xml (not just models.py) is also accepted")


def test_no_enumeration_signal_never_fires():
    generated = _FakeGenerated(models_py="class X(models.Model):\n    _name = 'x.y'\n")
    validate_enumerated_required_pieces_are_all_present(generated, "Add a field project_count to res.partner.")
    print("PASS: a goal with no 'required pieces, none may be omitted' signal never fires the check")


if __name__ == "__main__":
    test_real_confirmed_defect_inherit_without_name_on_signaled_new_model()
    test_declaring_name_correctly_is_never_flagged()
    test_inherit_on_an_unrelated_model_is_never_flagged()
    test_no_new_model_signal_in_goal_never_fires()
    test_real_confirmed_defect_enumerated_required_field_missing()
    test_enumerated_required_field_present_is_never_flagged()
    test_field_present_only_in_views_xml_is_also_accepted()
    test_no_enumeration_signal_never_fires()
    print("\nALL EXISTING-MODEL BUTTON STRUCTURAL GATE TESTS PASSED")
