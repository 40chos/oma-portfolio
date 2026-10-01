"""P11 Tier S (docs/planning/PHASE30_ADDENDUM_GENERIC_FIX_BUILDOUT_2026-07-30.md §2.1a/§2.1b,
docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md §18b.2 items 1-2):
unit tests for the two Tier S items, the required-before-anything-else prerequisite for the rest
of P11's validator/autofix sweep.

Item 1: `_validate_sequence_assigned_field_is_not_numeric()` (new, flag-only) -- `ir.sequence.
next_by_code()`/`.next_by_id()` always return `str` in Odoo 16; assigning that value into a field
declared `Integer`/`Float` is always wrong.

Item 2: `_autofix_goal_named_sequence_field_missing()`'s own generation path should always prefer
`Char` for a brand-new sequence-backed skeleton, even when the goal explicitly states a numeric
type -- verified already true of the current, real code (it unconditionally emits `fields.Char`,
no numeric-type branch exists to begin with), locked in here as a regression guard, not built as
new behavior.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_goal_named_sequence_field_missing,
    _validate_sequence_assigned_field_is_not_numeric,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _generated(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(manifest_fields=_MANIFEST, models_py=models_py, security_csv="x", notes="")


# --- Item 1: _validate_sequence_assigned_field_is_not_numeric ---

def test_raises_when_sequence_value_assigned_into_integer_field_via_vals():
    models_py = (
        "class ProjectPunchlist(models.Model):\n"
        "    _name = 'project.punchlist'\n"
        "    item_number = fields.Integer(string='Item Number')\n"
        "\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        for vals in vals_list:\n"
        "            vals['item_number'] = self.env['ir.sequence'].next_by_code('project.punchlist')\n"
        "        return super().create(vals_list)\n"
    )
    try:
        _validate_sequence_assigned_field_is_not_numeric(_generated(models_py))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "item_number" in str(exc) and "Integer" in str(exc)
    print("PASS: a sequence value assigned into a vals[...] Integer field raises, naming the real field/type")


def test_raises_for_float_field_via_self_attr_assignment():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    ref_code = fields.Float(string='Ref')\n"
        "\n"
        "    def _assign_ref(self):\n"
        "        self.ref_code = self.env['ir.sequence'].next_by_id(42)\n"
    )
    try:
        _validate_sequence_assigned_field_is_not_numeric(_generated(models_py))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "ref_code" in str(exc) and "Float" in str(exc)
    print("PASS: self.<field> = ... form is also caught, for a Float field via next_by_id()")


def test_never_raises_when_field_is_char():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    name = fields.Char(string='Name', default='New')\n"
        "\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        for vals in vals_list:\n"
        "            vals['name'] = self.env['ir.sequence'].next_by_code('x.model') or 'New'\n"
        "        return super().create(vals_list)\n"
    )
    _validate_sequence_assigned_field_is_not_numeric(_generated(models_py))  # must not raise
    print("PASS: the correct, textbook Char-typed sequence idiom never raises")


def test_never_raises_when_target_field_unresolvable():
    # Assigned through an intermediate variable this simple regex walk can't trace -- deliberately
    # skipped, never guessed at.
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    code = fields.Integer(string='Code')\n"
        "\n"
        "    def _assign(self):\n"
        "        val = self.env['ir.sequence'].next_by_code('x.model')\n"
        "        self.code = val\n"
    )
    _validate_sequence_assigned_field_is_not_numeric(_generated(models_py))  # must not raise
    print("PASS: an intermediate-variable assignment (not directly traceable) is never guessed at")


def test_never_raises_when_no_sequence_assignment_present_at_all():
    models_py = (
        "class X(models.Model):\n"
        "    _name = 'x.model'\n"
        "    amount = fields.Integer(string='Amount')\n"
    )
    _validate_sequence_assigned_field_is_not_numeric(_generated(models_py))  # must not raise
    print("PASS: an ordinary Integer field with no sequence assignment anywhere never raises")


# --- Item 2: _autofix_goal_named_sequence_field_missing always prefers Char ---

def test_autofix_always_generates_char_even_when_goal_explicitly_states_integer():
    """The addendum's own §2.1b concern: a goal explicitly requesting a numeric type for a
    sequence-backed field must not make this autofix honor that hint. Verified here against the
    REAL current code: it has no numeric-type branch at all, unconditionally generates Char --
    this test locks that behavior in as a regression guard.
    """
    goal = (
        "Field name: item_number (Integer)\n"
        "Sequence code: project.punchlist\n"
        "Add an auto-incrementing item number to project.punchlist, generated from a sequence."
    )
    models_py = (
        "class ProjectPunchlist(models.Model):\n"
        "    _name = 'project.punchlist'\n"
    )
    generated = _generated(models_py)
    goal_facts = {"field_name": "item_number", "is_sequence_assigned": True}
    _autofix_goal_named_sequence_field_missing(generated, goal, {}, goal_facts)
    assert "item_number = fields.Char(" in generated.models_py, generated.models_py
    assert "fields.Integer(" not in generated.models_py
    print("PASS: the autofix's own brand-new skeleton is Char even when the goal explicitly says (Integer)")


if __name__ == "__main__":
    test_raises_when_sequence_value_assigned_into_integer_field_via_vals()
    test_raises_for_float_field_via_self_attr_assignment()
    test_never_raises_when_field_is_char()
    test_never_raises_when_target_field_unresolvable()
    test_never_raises_when_no_sequence_assignment_present_at_all()
    test_autofix_always_generates_char_even_when_goal_explicitly_states_integer()
    print("\nALL P11 TIER S TESTS PASSED")
