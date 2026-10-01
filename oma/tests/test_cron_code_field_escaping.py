"""P11 finding #4 (docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md §1.4, 2
confirmations, real tasks 2be2d9b1-... / 7fe925df-...): tests for
_autofix_escape_angle_brackets_in_cron_code_field() -- XML-escaping raw `<`/`>` inside an
ir.cron `<field name="code">` block, confirmed via a real, live lxml.etree.XMLSyntaxError on
every install attempt for both confirmed tasks. Pure, synchronous, zero LLM/GPU calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_escape_angle_brackets_in_cron_code_field,
)


def _manifest():
    return ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )


def test_escapes_the_real_confirmed_crash_shape():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="ir_cron_expire" model="ir.cron">'
            '<field name="code">if loyalty_points < 0 and points > 100:\n'
            '    record.action_flag()</field></record></odoo>'
        ),
    )
    _autofix_escape_angle_brackets_in_cron_code_field(generated)
    assert "&lt;" in generated.security_xml
    assert "&gt;" in generated.security_xml
    assert "loyalty_points < 0" not in generated.security_xml
    assert "points > 100" not in generated.security_xml
    print("PASS: raw < and > inside a real cron code field are both escaped")


def test_is_idempotent_on_already_escaped_content():
    already_escaped = (
        '<odoo><record id="x" model="ir.cron">'
        '<field name="code">if x &lt; 0:\n    pass</field></record></odoo>'
    )
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=already_escaped,
    )
    _autofix_escape_angle_brackets_in_cron_code_field(generated)
    assert generated.security_xml == already_escaped, "already-escaped content must be left byte-identical"
    print("PASS: already-escaped content is untouched -- safe to run every round")


def test_views_xml_outside_a_code_field_is_never_touched():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml=(
            '<odoo><record id="x" model="ir.ui.view">'
            '<field name="arch" type="xml"><form><field name="y"/></form></field>'
            '</record></odoo>'
        ), security_csv="", notes="",
    )
    original = generated.views_xml
    _autofix_escape_angle_brackets_in_cron_code_field(generated)
    assert generated.views_xml == original, "content outside a name=\"code\" field must never be touched"
    print("PASS: ordinary views_xml with no code field is completely untouched")


def test_no_code_field_present_is_a_safe_no_op():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml="",
    )
    _autofix_escape_angle_brackets_in_cron_code_field(generated)  # must not raise
    print("PASS: empty/no security_xml content is a safe no-op")


if __name__ == "__main__":
    test_escapes_the_real_confirmed_crash_shape()
    test_is_idempotent_on_already_escaped_content()
    test_views_xml_outside_a_code_field_is_never_touched()
    test_no_code_field_present_is_a_safe_no_op()
    print("\nALL CRON CODE-FIELD ESCAPING TESTS PASSED")
