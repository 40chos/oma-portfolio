"""Phase 35 §15.3/§15.12 item 2: exhaustive unit tests for the
single_new_field deterministic generator, against its full, small,
enumerable parameter space -- per §14.3's own point that a narrow,
well-typed schema is worth building specifically because it makes
exhaustive (not just sampled) testing realistic.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from manager.deterministic_generators.single_new_field import (
    SingleFieldAdditionParams,
    StructuralPreconditionError,
    UnsupportedFieldTypeError,
    render_single_field_addition,
)

SIMPLE_MODULE = '''from odoo import models, fields


class ResPartner(models.Model):
    _inherit = "res.partner"

    existing_field = fields.Char(string="Existing")
'''

MULTI_CLASS_MODULE = SIMPLE_MODULE + '''

class ProjectTask(models.Model):
    _inherit = "project.task"

    other_field = fields.Boolean(default=False)
'''

MIXIN_MODULE = '''from odoo import models, fields


class NoteNote(models.Model):
    _name = "note.note"
    _inherit = ["mail.thread"]
    _description = "A note"

    body = fields.Text()
'''

NO_MATCHING_CLASS_MODULE = SIMPLE_MODULE  # target model won't be res.partner in these tests

DUPLICATE_CLASS_MODULE = SIMPLE_MODULE + '''

class ResPartnerAgain(models.Model):
    _inherit = "res.partner"

    another_field = fields.Char()
'''


# ---- exhaustive coverage of the evidence-backed field-type space ----

_ALL_EVIDENCE_BACKED_TYPES = ["Char", "Boolean", "Integer", "Date", "Text", "Selection"]


@pytest.mark.parametrize("field_type", _ALL_EVIDENCE_BACKED_TYPES)
def test_renders_every_evidence_backed_type_without_error(field_type):
    kwargs = dict(
        module_name="oma_test_module",
        model_name="res.partner",
        field_name="my_new_field",
        field_type=field_type,
    )
    if field_type == "Selection":
        kwargs["selection_options"] = [("a", "Option A"), ("b", "Option B")]
    params = SingleFieldAdditionParams(**kwargs)
    assert params.is_in_evidence_backed_space()

    edit = render_single_field_addition(params, SIMPLE_MODULE)
    assert edit["operation"] == "search_replace"
    assert edit["file"] == "models/models.py"
    assert edit["target"] in SIMPLE_MODULE
    assert edit["content"].startswith(edit["target"])
    assert "my_new_field = fields.%s(" % field_type in edit["content"]
    # target must appear exactly once in the source, per
    # GeneratedModuleEdit.search_replace's own real contract
    assert SIMPLE_MODULE.count(edit["target"]) == 1


def test_selection_field_renders_options_literally():
    params = SingleFieldAdditionParams(
        module_name="oma_x", model_name="res.partner", field_name="status",
        field_type="Selection", selection_options=[("draft", "Draft"), ("done", "Done")],
    )
    edit = render_single_field_addition(params, SIMPLE_MODULE)
    assert "[('draft', 'Draft'), ('done', 'Done')]" in edit["content"]


def test_default_and_help_text_render():
    params = SingleFieldAdditionParams(
        module_name="oma_x", model_name="res.partner", field_name="is_vip",
        field_type="Boolean", default=False, help_text="VIP flag",
    )
    edit = render_single_field_addition(params, SIMPLE_MODULE)
    assert "default=False" in edit["content"]
    assert "help='VIP flag'" in edit["content"]


# ---- schema validation rejects malformed input up front, never guesses ----

def test_rejects_non_identifier_field_name():
    with pytest.raises(ValueError):
        SingleFieldAdditionParams(
            module_name="oma_x", model_name="res.partner", field_name="Not Valid!",
            field_type="Char",
        )


def test_selection_requires_options():
    with pytest.raises(ValueError):
        SingleFieldAdditionParams(
            module_name="oma_x", model_name="res.partner", field_name="status",
            field_type="Selection",
        )


def test_non_selection_rejects_options():
    with pytest.raises(ValueError):
        SingleFieldAdditionParams(
            module_name="oma_x", model_name="res.partner", field_name="x",
            field_type="Char", selection_options=[("a", "A")],
        )


# ---- the evidence-backed-space gate, §15.5 step 1's real check ----

def test_group_restricted_field_is_schema_valid_but_not_evidence_backed():
    params = SingleFieldAdditionParams(
        module_name="oma_x", model_name="res.partner", field_name="secret",
        field_type="Char", security_group="base.group_system",
    )
    assert not params.is_in_evidence_backed_space()
    with pytest.raises(UnsupportedFieldTypeError):
        render_single_field_addition(params, SIMPLE_MODULE)


# ---- structural-drift precondition, §15.3's added discipline ----

def test_multi_class_file_finds_the_right_class_only():
    params = SingleFieldAdditionParams(
        module_name="oma_x", model_name="project.task", field_name="new_one",
        field_type="Char",
    )
    edit = render_single_field_addition(params, MULTI_CLASS_MODULE)
    assert "class ProjectTask" in edit["target"]
    assert "class ResPartner" not in edit["target"]


def test_mixin_style_inherit_list_form_is_found():
    params = SingleFieldAdditionParams(
        module_name="oma_x", model_name="mail.thread", field_name="new_one",
        field_type="Char",
    )
    edit = render_single_field_addition(params, MIXIN_MODULE)
    assert "class NoteNote" in edit["target"]


def test_no_matching_class_raises_structural_precondition_error():
    params = SingleFieldAdditionParams(
        module_name="oma_x", model_name="sale.order", field_name="new_one",
        field_type="Char",
    )
    with pytest.raises(StructuralPreconditionError):
        render_single_field_addition(params, NO_MATCHING_CLASS_MODULE)


def test_duplicate_matching_classes_raises_structural_precondition_error():
    params = SingleFieldAdditionParams(
        module_name="oma_x", model_name="res.partner", field_name="new_one",
        field_type="Char",
    )
    with pytest.raises(StructuralPreconditionError):
        render_single_field_addition(params, DUPLICATE_CLASS_MODULE)


def test_invalid_python_syntax_raises_structural_precondition_error():
    params = SingleFieldAdditionParams(
        module_name="oma_x", model_name="res.partner", field_name="new_one",
        field_type="Char",
    )
    with pytest.raises(StructuralPreconditionError):
        render_single_field_addition(params, "def broken(:\n")
