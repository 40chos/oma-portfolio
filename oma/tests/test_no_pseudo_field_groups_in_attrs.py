"""P11 finding #2 (docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md §1.2,
confirmation 1, task 1cb2cbde-14c0-43f5-ba4b-8911b28f5a06): tests for
_validate_no_pseudo_field_groups_in_attrs_domain() -- a real, deterministic guard against Build
writing an attrs/invisible domain condition that compares a nonexistent 'groups' field instead of
using the real groups="module.xmlid" XML attribute. Confirmed via a real, live Odoo install crash
log ("Field 'groups' used in attrs (...) must be present in view but is missing"), not a guess.

Pure, synchronous, zero LLM/GPU calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_no_pseudo_field_groups_in_attrs_domain,
)


def _manifest():
    return ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )


def test_rejects_the_real_confirmed_crash_shape():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(),
        models_py="from odoo import models\n\nclass X(models.Model):\n    _inherit = 'res.partner'\n",
        views_xml=(
            '<odoo><record id="view_partner_form_x" model="ir.ui.view">'
            '<field name="arch" type="xml"><form>'
            '<div attrs="{\'invisible\': [(\'groups\', \'!=\', \'base.group_system\')]}">'
            '<field name="x"/></div></form></field></record></odoo>'
        ),
        security_csv="", notes="",
    )
    raised = False
    try:
        _validate_no_pseudo_field_groups_in_attrs_domain(generated)
    except ValueError as e:
        raised = True
        assert "groups" in str(e)
        assert "groups=\"module.xmlid\"" in str(e)
    assert raised, "the exact real, confirmed crash shape must be rejected before write"
    print("PASS: the real, confirmed live crash shape is rejected with a real, actionable message")


def test_real_groups_xml_attribute_is_never_flagged():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(),
        models_py="from odoo import models\n\nclass X(models.Model):\n    _inherit = 'res.partner'\n",
        views_xml=(
            '<odoo><record id="view_partner_form_x" model="ir.ui.view">'
            '<field name="arch" type="xml"><form>'
            '<field name="internal_credit_note" groups="base.group_system"/>'
            '</form></field></record></odoo>'
        ),
        security_csv="", notes="",
    )
    _validate_no_pseudo_field_groups_in_attrs_domain(generated)  # must not raise
    print("PASS: the real, correct groups=\"module.xmlid\" XML attribute is never flagged")


def test_an_ordinary_attrs_domain_on_a_real_field_is_never_flagged():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(),
        models_py="from odoo import models, fields\n\nclass X(models.Model):\n    _inherit = 'res.partner'\n    same_vat_partner_id = fields.Many2one('res.partner')\n",
        views_xml=(
            '<odoo><record id="view_partner_form_x" model="ir.ui.view">'
            '<field name="arch" type="xml"><form>'
            '<div attrs="{\'invisible\': [(\'same_vat_partner_id\', \'=\', False)]}">'
            '<field name="same_vat_partner_id"/></div></form></field></record></odoo>'
        ),
        security_csv="", notes="",
    )
    _validate_no_pseudo_field_groups_in_attrs_domain(generated)  # must not raise
    print("PASS: an ordinary attrs domain comparing a real field is never flagged")


def test_checks_security_xml_and_extra_data_files_too():
    bad_snippet = (
        '<odoo><record id="x" model="ir.ui.view">'
        '<field name="arch" type="xml"><form>'
        '<field name="y" attrs="{\'invisible\': [(\'groups\', \'=\', \'base.group_user\')]}"/>'
        '</form></field></record></odoo>'
    )
    security_case = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=bad_snippet,
    )
    raised = False
    try:
        _validate_no_pseudo_field_groups_in_attrs_domain(security_case)
    except ValueError:
        raised = True
    assert raised, "security_xml must be checked too, not just views_xml"

    extra_case = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        extra_data_files={"report_template.xml": bad_snippet},
    )
    raised = False
    try:
        _validate_no_pseudo_field_groups_in_attrs_domain(extra_case)
    except ValueError:
        raised = True
    assert raised, "extra_data_files must be checked too, not just views_xml/security_xml"
    print("PASS: security_xml and extra_data_files are both checked, not just views_xml")


if __name__ == "__main__":
    test_rejects_the_real_confirmed_crash_shape()
    test_real_groups_xml_attribute_is_never_flagged()
    test_an_ordinary_attrs_domain_on_a_real_field_is_never_flagged()
    test_checks_security_xml_and_extra_data_files_too()
    print("\nALL PSEUDO-FIELD-GROUPS-IN-ATTRS TESTS PASSED")
