"""Phase 29 (2026-07-29): unit test for
_autofix_ensure_compute_method_has_api_depends() -- the real, confirmed
live root cause behind 5+ consecutive round failures on the
school_student task's own automated_tests round, oscillating between 9
and 10 failing tests without ever converging: `age = fields.Integer(
compute='_compute_age', store=True)` had a real, correctly-written
`_compute_age` method body, but NO `@api.depends(...)` decorator at
all. Without it, Odoo's ORM has no dependency-trigger mapping for the
field -- the method is registered but never actually invoked during
create(), so every dynamically-created test record's `age` stayed at
the plain Integer default (0), producing the exact `AssertionError:
0 != 26` seen live, identically, across every round.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_ensure_compute_method_has_api_depends,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Education", summary="x", author="x", depends=["base"], data=[],
)

_REAL_MODELS_PY = (
    "from odoo import models, fields\n\n\n"
    "class Student(models.Model):\n"
    "    _name = 'school.student'\n"
    "    _description = 'Student'\n\n"
    "    date_of_birth = fields.Date()\n"
    "    age = fields.Integer(compute='_compute_age', store=True)\n\n"
    "    def _compute_age(self):\n"
    "        today = fields.Date.today()\n"
    "        for record in self:\n"
    "            if record.date_of_birth:\n"
    "                age = today.year - record.date_of_birth.year\n"
    "                record.age = age\n"
    "            else:\n"
    "                record.age = 0\n"
)


def _make_generated(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(manifest_fields=_MANIFEST.model_copy(deep=True), models_py=models_py, security_csv="x", notes="")


def test_adds_the_real_missing_api_depends_decorator():
    generated = _make_generated(_REAL_MODELS_PY)
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert "@api.depends('date_of_birth')" in generated.models_py
    assert re.search(r"@api\.depends\('date_of_birth'\)\s*\n\s*def _compute_age", generated.models_py)
    assert re.search(r"^from odoo import .*\bapi\b", generated.models_py, re.MULTILINE)
    print("PASS: the real, confirmed live missing @api.depends('date_of_birth') is added, closing the "
          "exact bug behind 5+ consecutive round failures")


def test_never_touches_an_already_correctly_decorated_method():
    already_correct = (
        "from odoo import api, fields, models\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        "    a = fields.Integer()\n"
        "    b = fields.Integer(compute='_compute_b', store=True)\n\n"
        "    @api.depends('a')\n"
        "    def _compute_b(self):\n"
        "        for r in self:\n"
        "            r.b = r.a\n"
    )
    generated = _make_generated(already_correct)
    before = generated.models_py
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert generated.models_py == before
    print("PASS: never touches an already-correctly-decorated compute method")


def test_no_op_when_there_is_no_compute_field_at_all():
    plain = "from odoo import fields, models\n\nclass X(models.Model):\n    _name = 'x.y'\n    a = fields.Integer()\n"
    generated = _make_generated(plain)
    before = generated.models_py
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert generated.models_py == before
    print("PASS: no-op when there is no compute field at all")


def test_falls_back_to_empty_depends_when_no_other_field_is_referenced():
    no_deps = (
        "from odoo import fields, models\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        "    b = fields.Integer(compute='_compute_b', store=True)\n\n"
        "    def _compute_b(self):\n"
        "        for r in self:\n"
        "            r.b = 42\n"
    )
    generated = _make_generated(no_deps)
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert "@api.depends()" in generated.models_py
    print("PASS: falls back to empty @api.depends() when the method references no other real field")


def test_never_invents_a_dependency_the_method_does_not_actually_reference():
    models_py = (
        "from odoo import fields, models\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        "    unrelated_field = fields.Char()\n"
        "    b = fields.Integer(compute='_compute_b', store=True)\n\n"
        "    def _compute_b(self):\n"
        "        for r in self:\n"
        "            r.b = 42\n"
    )
    generated = _make_generated(models_py)
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert "'unrelated_field'" not in generated.models_py
    print("PASS: never adds a dependency the compute method's own body doesn't actually reference")


def test_infers_the_full_two_hop_path_for_the_mapped_idiom():
    """P11 fifth pass item 170: the real, confirmed live bug -- self.line_ids.mapped('price_unit')
    previously inferred only {'line_ids'}, so Odoo recomputed on add/remove of a line but never on
    an existing line's own price_unit changing.
    """
    models_py = (
        "from odoo import fields, models\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        "    line_ids = fields.One2many('x.y.line', 'parent_id')\n"
        "    amount_total = fields.Float(compute='_compute_amount_total', store=True)\n\n"
        "    def _compute_amount_total(self):\n"
        "        for record in self:\n"
        "            record.amount_total = sum(record.line_ids.mapped('price_unit'))\n"
    )
    generated = _make_generated(models_py)
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert "@api.depends('line_ids.price_unit')" in generated.models_py
    assert "@api.depends('line_ids')" not in generated.models_py
    print("PASS item170: the .mapped('price_unit') idiom infers the full 'line_ids.price_unit' two-hop path")


def test_infers_the_full_two_hop_path_for_a_plain_attribute_chain():
    models_py = (
        "from odoo import fields, models\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        "    partner_id = fields.Many2one('res.partner')\n"
        "    display_summary = fields.Char(compute='_compute_display_summary', store=True)\n\n"
        "    def _compute_display_summary(self):\n"
        "        for record in self:\n"
        "            record.display_summary = record.partner_id.name\n"
    )
    generated = _make_generated(models_py)
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert "@api.depends('partner_id.name')" in generated.models_py
    print("PASS item170: a plain attribute chain (self.partner_id.name) infers 'partner_id.name'")


def test_two_hop_inference_never_misreads_a_method_call_as_a_sub_field():
    """A .filtered(...)/.exists()-style call on a relation field must never be misread as if
    'filtered'/'exists' were a real sub-field name -- only .mapped('literal_field') and a genuine
    plain attribute chain qualify.
    """
    models_py = (
        "from odoo import fields, models\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        "    line_ids = fields.One2many('x.y.line', 'parent_id')\n"
        "    active_count = fields.Integer(compute='_compute_active_count', store=True)\n\n"
        "    def _compute_active_count(self):\n"
        "        for record in self:\n"
        "            record.active_count = len(record.line_ids.filtered(lambda l: l.active))\n"
    )
    generated = _make_generated(models_py)
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert "'line_ids.filtered'" not in generated.models_py
    assert "@api.depends('line_ids')" in generated.models_py
    print("PASS item170: a .filtered(...) method call is never misread as a fake two-hop sub-field")


def test_item175_long_api_depends_decorator_past_200_chars_is_still_recognized():
    """P11 fifth pass item 175: the original fixed 200-char lookback slice let a genuinely long,
    correct @api.depends(...) decorator push its own opening paren past the boundary, causing a
    second decorator to be stacked on top of an already-correctly-decorated method.
    """
    # A real, long dependency list -- the decorator line alone is well over 200 characters.
    long_deps = ", ".join(f"'field_{i}'" for i in range(20))
    models_py = (
        "from odoo import api, fields, models\n\n"
        "class X(models.Model):\n"
        "    _name = 'x.y'\n"
        + "".join(f"    field_{i} = fields.Integer()\n" for i in range(20))
        + "    total = fields.Integer(compute='_compute_total', store=True)\n\n"
        f"    @api.depends({long_deps})\n"
        "    def _compute_total(self):\n"
        "        for record in self:\n"
        "            record.total = 0\n"
    )
    assert len(f"    @api.depends({long_deps})\n") > 200, "test fixture must genuinely exceed the old 200-char window"
    generated = _make_generated(models_py)
    before = generated.models_py
    _autofix_ensure_compute_method_has_api_depends(generated)
    assert generated.models_py == before, "an already-correctly-decorated method (long decorator) must never be touched"
    assert generated.models_py.count("@api.depends(") == 1, "must never stack a second decorator on top"
    print("PASS item175: a long, correct @api.depends(...) decorator past 200 chars is still recognized, never doubled")


if __name__ == "__main__":
    test_adds_the_real_missing_api_depends_decorator()
    test_never_touches_an_already_correctly_decorated_method()
    test_no_op_when_there_is_no_compute_field_at_all()
    test_falls_back_to_empty_depends_when_no_other_field_is_referenced()
    test_never_invents_a_dependency_the_method_does_not_actually_reference()
    test_infers_the_full_two_hop_path_for_the_mapped_idiom()
    test_infers_the_full_two_hop_path_for_a_plain_attribute_chain()
    test_two_hop_inference_never_misreads_a_method_call_as_a_sub_field()
    test_item175_long_api_depends_decorator_past_200_chars_is_still_recognized()
    print("\nALL ENSURE-COMPUTE-API-DEPENDS TESTS PASSED")
