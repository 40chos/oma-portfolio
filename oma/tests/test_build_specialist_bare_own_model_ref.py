"""Phase 28C (2026-07-29): unit tests for
_autofix_xml_bare_model_ref_qualifies_own_new_model() -- the real,
confirmed live gap found on the school_student task's own record_rules
round, and the single largest historical pattern in this project's own
proposed-rule backlog (69 of 637 rows share this exact shape).

Root cause: the existing sibling autofix (_autofix_xml_bare_model_ref_
qualifies_owning_module) only resolves via a LIVE registry lookup --
structurally unable to help when this round's own target model was
created by an earlier round but the module is currently `uninstalled`
(a normal, real consequence of Odoo rolling back a failed install), so
the model genuinely doesn't exist in the live registry at the exact
moment the check runs, even though it unquestionably will once this
round's own content installs. This fix needs no DB access: a bare
`ref="model_XXX"` where model_XXX matches a `_name` this SAME round's
own models_py defines is direct, textual proof of ownership.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_xml_bare_model_ref_qualifies_own_new_model,
)

_MODULE_NAME = "oma_simple_custom_module_task_595ad7bc"
_MODELS_PY = "from odoo import models, fields\n\nclass Student(models.Model):\n    _name = 'school.student'\n"


def _make_generated(security_xml: str | None = None, views_xml: str | None = None) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py=_MODELS_PY, security_csv="id,name\n",
        security_xml=security_xml, views_xml=views_xml, notes="",
    )


def test_fixes_the_real_live_bare_own_model_ref():
    generated = _make_generated(
        security_xml=(
            '<odoo>\n'
            '    <record id="rule_school_student_delete" model="ir.rule">\n'
            '        <field name="name">Students: Delete Access</field>\n'
            '        <field name="model_id" ref="model_school_student"/>\n'
            '        <field name="domain_force">[]</field>\n'
            '    </record>\n'
            '</odoo>'
        ),
    )
    _autofix_xml_bare_model_ref_qualifies_own_new_model(generated, _MODULE_NAME)
    assert f'ref="{_MODULE_NAME}.model_school_student"' in generated.security_xml
    assert 'ref="model_school_student"' not in generated.security_xml
    print("PASS: the exact real bare own-model ref (the single largest historical pattern in the "
          "project's own proposed-rule backlog) is qualified with zero DB dependency")


def test_qualifies_in_views_xml_too():
    generated = _make_generated(
        views_xml='<odoo><menuitem id="m" action="a" /><record id="a" model="ir.actions.act_window">'
                   '<field name="res_model" ref="model_school_student"/></record></odoo>',
    )
    _autofix_xml_bare_model_ref_qualifies_own_new_model(generated, _MODULE_NAME)
    assert f'{_MODULE_NAME}.model_school_student' in generated.views_xml
    print("PASS: works in views_xml as well as security_xml")


def test_never_touches_a_ref_not_matching_any_own_model():
    generated = _make_generated(
        security_xml='<odoo><record id="r" model="ir.rule">'
                     '<field name="model_id" ref="model_res_partner"/></record></odoo>',
    )
    before = generated.security_xml
    _autofix_xml_bare_model_ref_qualifies_own_new_model(generated, _MODULE_NAME)
    assert generated.security_xml == before
    print("PASS: a ref that doesn't match any model THIS round's own models_py defines is left "
          "alone for the DB-dependent sibling autofix to handle")


def test_is_a_noop_when_no_new_model_defined():
    generated = _make_generated(security_xml='<odoo><record id="r" model="ir.rule">'
                                              '<field name="model_id" ref="model_school_student"/></record></odoo>')
    generated.models_py = "from odoo import models\n\nclass X(models.Model):\n    _inherit = 'res.partner'\n"
    before = generated.security_xml
    _autofix_xml_bare_model_ref_qualifies_own_new_model(generated, _MODULE_NAME)
    assert generated.security_xml == before
    print("PASS: a no-op when this round's own models_py defines no new model at all")


if __name__ == "__main__":
    test_fixes_the_real_live_bare_own_model_ref()
    test_qualifies_in_views_xml_too()
    test_never_touches_a_ref_not_matching_any_own_model()
    test_is_a_noop_when_no_new_model_defined()
    print("\nALL BARE-OWN-MODEL-REF TESTS PASSED")
