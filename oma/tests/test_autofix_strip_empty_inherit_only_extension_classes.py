"""Real, confirmed bug found live (2026-08-10, task 4724a61f, a small dedicated follow-up
correction task extending the flagship task's own already-installed module -- recurring across 2
straight rounds/resumes of the SAME `equipment_views_menu` constraint, whose own goal explicitly
said not to touch models.py at all this round): `_validate_inherit_only_class_is_not_empty`
already deterministically detects an entirely empty `_inherit`-only class, but only ever as a
hard rejection whose own error message literally suggests the fix in its own text ("...or remove
the empty class entirely"). See specialists/build/specialist.py's
`_autofix_strip_empty_inherit_only_extension_classes` for the full incident.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_strip_empty_inherit_only_extension_classes,
    _validate_inherit_only_class_is_not_empty,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, security_csv="id,name\n", notes="",
    )


def test_strips_the_real_live_empty_inherit_class_shape():
    generated = _gen(
        "from odoo import models, fields, api\n\n\n"
        "class Equipment(models.Model):\n"
        "    _name = 'oma.equipment'\n"
        "    _description = 'Equipment Registry'\n\n"
        "    name = fields.Char(required=True, string='Name')\n\n\n"
        "class ProjectEquipment(models.Model):\n"
        "    _inherit = 'oma.equipment'\n"
        "    \n"
        "    \n"
    )
    _autofix_strip_empty_inherit_only_extension_classes(generated)
    assert "_inherit = 'oma.equipment'" not in generated.models_py, (
        f"expected the empty _inherit extension to be stripped -- got: {generated.models_py!r}"
    )
    assert "class Equipment(models.Model):" in generated.models_py, (
        "the real, complete _name-declared model must survive untouched"
    )
    assert "name = fields.Char(required=True, string='Name')" in generated.models_py
    _validate_inherit_only_class_is_not_empty(generated)  # must now pass cleanly
    print("PASS: strips the real, live empty _inherit class, leaving the genuine model intact, "
          "and the round now passes its own sibling validator")


def test_never_touches_a_complete_inherit_class():
    original = (
        "from odoo import models, fields\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    generated = _gen(original)
    _autofix_strip_empty_inherit_only_extension_classes(generated)
    assert generated.models_py.strip() == original.strip(), (
        "a complete _inherit class with real content must never be touched"
    )
    print("PASS: never touches a complete _inherit class with real fields/methods")


def test_never_touches_a_new_name_declared_model_with_no_fields():
    # A bare _name model with no fields is a different concern -- this fix only ever concerns
    # _inherit-only extensions, never genuinely new models.
    original = (
        "from odoo import models\n\n\n"
        "class Foo(models.Model):\n"
        "    _name = 'oma.foo'\n"
        "    _description = 'Foo'\n"
    )
    generated = _gen(original)
    _autofix_strip_empty_inherit_only_extension_classes(generated)
    assert generated.models_py.strip() == original.strip()
    print("PASS: never touches a bare _name model with no fields -- not this fix's territory")


def test_never_touches_an_inherit_class_with_only_a_method_no_fields():
    original = (
        "from odoo import models\n\n\n"
        "class Bar(models.Model):\n"
        "    _inherit = 'res.partner'\n\n"
        "    def action_do_something(self):\n"
        "        pass\n"
    )
    generated = _gen(original)
    _autofix_strip_empty_inherit_only_extension_classes(generated)
    assert generated.models_py.strip() == original.strip()
    print("PASS: never touches an _inherit class with only a method (no fields) -- a method "
          "alone is enough, not empty")


def test_is_a_noop_on_empty_models_py():
    generated = _gen("")
    _autofix_strip_empty_inherit_only_extension_classes(generated)  # must not raise
    assert generated.models_py == ""
    print("PASS: a no-op when models_py is genuinely empty")


def test_strips_multiple_empty_inherit_classes_keeping_real_content():
    generated = _gen(
        "from odoo import models, fields\n\n\n"
        "class Empty1(models.Model):\n"
        "    _inherit = 'res.partner'\n\n\n"
        "class RealModel(models.Model):\n"
        "    _name = 'oma.real'\n"
        "    name = fields.Char()\n\n\n"
        "class Empty2(models.Model):\n"
        "    _inherit = 'project.project'\n"
    )
    _autofix_strip_empty_inherit_only_extension_classes(generated)
    assert "_inherit = 'res.partner'" not in generated.models_py
    assert "_inherit = 'project.project'" not in generated.models_py
    assert "_name = 'oma.real'" in generated.models_py
    assert "name = fields.Char()" in generated.models_py
    _validate_inherit_only_class_is_not_empty(generated)
    print("PASS: strips multiple empty _inherit classes in the same file, keeping the real model")


if __name__ == "__main__":
    test_strips_the_real_live_empty_inherit_class_shape()
    test_never_touches_a_complete_inherit_class()
    test_never_touches_a_new_name_declared_model_with_no_fields()
    test_never_touches_an_inherit_class_with_only_a_method_no_fields()
    test_is_a_noop_on_empty_models_py()
    test_strips_multiple_empty_inherit_classes_keeping_real_content()
    print("\nALL AUTOFIX-STRIP-EMPTY-INHERIT-ONLY-EXTENSION-CLASSES TESTS PASSED")
