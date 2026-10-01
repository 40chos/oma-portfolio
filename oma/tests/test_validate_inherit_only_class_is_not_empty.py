"""Real, confirmed structural gap found live (2026-08-09, task 07141af5's flagship run,
project_ticket_counts node): a class extending an existing Odoo model purely via
`_inherit = '...'` (e.g. adding smart-button fields to `project.project`) can be generated with
an entirely EMPTY body -- just the `_inherit` line, zero fields, zero methods -- and nothing in
specialists/build/specialist.py's existing validator family caught it, because the shared
"is this a new model" detector (`_NEW_MODEL_NAME_RE`) only matches `_name = '...'`, and the
existing dropped-class restoration safety net explicitly excludes `_inherit`-only classes from
its own judgment (whether such a class is still needed is genuinely ambiguous -- but an entirely
EMPTY one is not: a real `_inherit` extension always exists to add something).

Confirmed live: without this validator, the empty stub silently committed as the round's own new
baseline, and every subsequent round's own views.xml validation failed on "field doesn't exist"
with no signal at all pointing at the real defect -- several resume rounds were needed before the
actual cause (an empty class) was found by direct inspection.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_inherit_only_class_is_not_empty,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, security_csv="id,name\n", notes="",
    )


def test_raises_on_the_real_live_empty_inherit_class_shape():
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class Equipment(models.Model):\n"
        "    _name = 'oma.equipment'\n"
        "    _description = 'Equipment Registry'\n\n"
        "    name = fields.Char(required=True, string='Name')\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n"
        "    \n"
        "    \n"
    )
    try:
        _validate_inherit_only_class_is_not_empty(_gen(models_py))
        assert False, "must raise on an entirely empty _inherit class body"
    except ValueError as exc:
        assert "project.project" in str(exc)
        assert "EMPTY body" in str(exc)
    print("PASS: raises on the real, live empty-_inherit-class shape from this incident")


def test_never_raises_on_a_complete_inherit_class():
    models_py = (
        "from odoo import models, fields\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    _validate_inherit_only_class_is_not_empty(_gen(models_py))
    print("PASS: never raises on a complete _inherit class with a real field and method")


def test_never_raises_on_a_new_name_declared_model_with_no_fields():
    # A bare _name model with no fields is a different concern entirely (covered, if at all, by
    # a different validator) -- this check only ever concerns _inherit-only extensions.
    models_py = (
        "from odoo import models\n\n\n"
        "class Foo(models.Model):\n"
        "    _name = 'oma.foo'\n"
        "    _description = 'Foo'\n"
    )
    _validate_inherit_only_class_is_not_empty(_gen(models_py))
    print("PASS: never raises on a bare _name model -- not this validator's territory")


def test_never_raises_on_an_inherit_class_with_only_a_method_no_fields():
    models_py = (
        "from odoo import models\n\n\n"
        "class Bar(models.Model):\n"
        "    _inherit = 'res.partner'\n\n"
        "    def action_do_something(self):\n"
        "        pass\n"
    )
    _validate_inherit_only_class_is_not_empty(_gen(models_py))
    print("PASS: never raises when only a method (no fields) is present -- a method alone is enough")


def test_never_raises_on_empty_models_py():
    _validate_inherit_only_class_is_not_empty(_gen(""))
    print("PASS: never raises when models_py is empty entirely")


if __name__ == "__main__":
    test_raises_on_the_real_live_empty_inherit_class_shape()
    test_never_raises_on_a_complete_inherit_class()
    test_never_raises_on_a_new_name_declared_model_with_no_fields()
    test_never_raises_on_an_inherit_class_with_only_a_method_no_fields()
    test_never_raises_on_empty_models_py()
    print("\nALL INHERIT-ONLY-CLASS-NOT-EMPTY TESTS PASSED")
