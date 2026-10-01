"""Phase 28C (2026-07-29): unit tests for
_autofix_unquoted_domain_field_name() -- the real, confirmed live gap
found on the school_student task's own record_rules round.

Root cause, confirmed by directly reconstructing the raw streamed
completion from OMA's own trace history: Build repeatedly generated
`domain_force` as `[(id, '!=', False)]` -- a bare, UNQUOTED Python
identifier `id` where Odoo's own domain-tuple syntax requires a quoted
string field name (`[('id', '!=', False)]`). This exact malformed
string, embedded inside a JSON-escaped structured-output field,
consistently preceded the model entering a genuine token-repetition
loop across 5 straight generation attempts, aborted early by the
gateway's own repetition guard -- the confusing nested-quote escaping
this shape forces very likely destabilized the model's own output.
`_DOMAIN_FIELD_NAME_RE` (the existing validator) only ever matches a
QUOTED field name, so this malformed shape was invisible to it too.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_unquoted_domain_field_name,
)


def _make_generated(security_xml: str | None) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="id,name\n",
        security_xml=security_xml, views_xml=None, notes="",
    )


def test_fixes_the_real_live_unquoted_id_domain():
    generated = _make_generated(
        '<odoo>\n'
        '    <record id="rule_school_student_user" model="ir.rule">\n'
        '        <field name="name">School Student: Users can see all</field>\n'
        '        <field name="model_id" ref="model_school_student"/>\n'
        '        <field name="domain_force">[(id, \'!=\', False)]</field>\n'
        '        <field name="groups" eval="[(4, ref(\'group_school_user\'))]"/>\n'
        '    </record>\n'
        '</odoo>'
    )
    _autofix_unquoted_domain_field_name(generated)
    assert "[('id', '!=', False)]" in generated.security_xml
    assert "[(id, '!=', False)]" not in generated.security_xml
    print("PASS: the exact real unquoted-'id' domain found live is quoted correctly, closing the "
          "real repeated repetition-loop trigger on school_student's record_rules round")


def test_never_touches_an_already_quoted_domain():
    generated = _make_generated(
        '<odoo>\n'
        '    <record id="rule_x" model="ir.rule">\n'
        '        <field name="domain_force">[(\'create_uid\', \'=\', user.id)]</field>\n'
        '    </record>\n'
        '</odoo>'
    )
    before = generated.security_xml
    _autofix_unquoted_domain_field_name(generated)
    assert generated.security_xml == before
    print("PASS: an already-correctly-quoted domain is never touched")


def test_fixes_multiple_unquoted_fields_in_one_domain():
    generated = _make_generated(
        '<odoo>\n'
        '    <record id="rule_x" model="ir.rule">\n'
        '        <field name="domain_force">[(state, \'=\', \'done\'), (active, \'=\', True)]</field>\n'
        '    </record>\n'
        '</odoo>'
    )
    _autofix_unquoted_domain_field_name(generated)
    assert "('state', '=', 'done')" in generated.security_xml
    assert "('active', '=', True)" in generated.security_xml
    print("PASS: multiple unquoted field names within the same domain are all fixed")


def test_is_a_noop_when_no_security_xml():
    generated = _make_generated(None)
    _autofix_unquoted_domain_field_name(generated)  # must not raise
    print("PASS: a no-op when there's no security_xml at all")


if __name__ == "__main__":
    test_fixes_the_real_live_unquoted_id_domain()
    test_never_touches_an_already_quoted_domain()
    test_fixes_multiple_unquoted_fields_in_one_domain()
    test_is_a_noop_when_no_security_xml()
    print("\nALL UNQUOTED-DOMAIN-FIELD TESTS PASSED")
