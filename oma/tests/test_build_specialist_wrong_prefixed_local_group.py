"""Phase 28C (2026-07-29): unit tests for
_autofix_wrong_module_prefixed_local_group_id() -- the real, confirmed
live gap found on the school_student task's own security_groups round.

Root cause: Build defined a real `res.groups` record in this round's
own security_xml but wrote both the record's own id AND the
security_csv's `group_id:id` reference to it with a hallucinated
module prefix -- `school.group_school_admin` ("school" is not a real
module, almost certainly confused with the `school.student` MODEL
name). Confirmed live via a real sandbox install crash: "No matching
record found for external id 'school.group_school_admin' in field
'Group'".
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_wrong_module_prefixed_local_group_id,
)

_MODULE_NAME = "oma_simple_custom_module_task_595ad7bc"


def _make_generated(security_xml: str | None = None, security_csv: str | None = None) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv=security_csv or "id,name\n",
        security_xml=security_xml, views_xml=None, notes="",
    )


def test_fixes_the_real_live_wrong_prefix_shape():
    generated = _make_generated(
        security_xml=(
            '<odoo>\n'
            '    <record id="school.group_school_admin" model="res.groups">\n'
            '        <field name="name">Admin</field>\n'
            '    </record>\n'
            '</odoo>'
        ),
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_school_student_admin,school.student.admin,model_school_student,"
            "school.group_school_admin,1,1,1,1\n"
        ),
    )
    _autofix_wrong_module_prefixed_local_group_id(generated, _MODULE_NAME)
    assert 'id="group_school_admin" model="res.groups"' in generated.security_xml
    assert "school.group_school_admin" not in generated.security_xml
    assert "school.group_school_admin" not in generated.security_csv
    assert f",{_MODULE_NAME}.group_school_admin," in generated.security_csv
    print("PASS: the exact real hallucinated 'school.' module prefix on a locally-defined group "
          "is corrected in both security_xml and security_csv, closing the real live crash")


def test_never_touches_a_correctly_prefixed_group():
    generated = _make_generated(
        security_xml=(
            f'<odoo>\n'
            f'    <record id="{_MODULE_NAME}.group_school_admin" model="res.groups">\n'
            f'        <field name="name">Admin</field>\n'
            f'    </record>\n'
            f'</odoo>'
        ),
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            f"access_x,x,model_school_student,{_MODULE_NAME}.group_school_admin,1,1,1,1\n"
        ),
    )
    before_xml, before_csv = generated.security_xml, generated.security_csv
    _autofix_wrong_module_prefixed_local_group_id(generated, _MODULE_NAME)
    assert generated.security_xml == before_xml
    assert generated.security_csv == before_csv
    print("PASS: a group already correctly prefixed with this module's own name is never touched")


def test_never_touches_a_bare_unprefixed_group():
    generated = _make_generated(
        security_xml=(
            '<odoo>\n'
            '    <record id="group_school_admin" model="res.groups">\n'
            '        <field name="name">Admin</field>\n'
            '    </record>\n'
            '</odoo>'
        ),
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_x,x,model_school_student,group_school_admin,1,1,1,1\n"
        ),
    )
    before_xml, before_csv = generated.security_xml, generated.security_csv
    _autofix_wrong_module_prefixed_local_group_id(generated, _MODULE_NAME)
    assert generated.security_xml == before_xml
    assert generated.security_csv == before_csv
    print("PASS: an already-bare group id is never touched -- nothing to fix")


def test_fixes_a_csv_only_wrong_prefix_pointing_at_a_correctly_bare_defined_group():
    """Real, confirmed bug found live (2026-07-29, same task, security_
    groups round, immediately after the first version of this fix
    deployed): a SECOND, distinct wrong-prefix shape -- security_xml
    correctly defined `<record id="group_school_admin">` (bare, no
    prefix at all, nothing for the first fix's own loop to find), but
    security_csv independently referenced it as `school_student.
    group_school_admin` -- "school_student" is this module's own
    MANIFEST DISPLAY NAME, not its real technical module name, a
    plausible but wrong prefix Build invented independently of
    anything in security_xml. Confirmed live: real Odoo sandbox install
    crashed with "No matching record found for external id
    'school_student.group_school_admin' in field 'Group'".
    """
    generated = _make_generated(
        security_xml=(
            '<odoo>\n'
            '    <record id="group_school_admin" model="res.groups">\n'
            '        <field name="name">Admin</field>\n'
            '    </record>\n'
            '    <record id="group_school_user" model="res.groups">\n'
            '        <field name="name">User</field>\n'
            '    </record>\n'
            '</odoo>'
        ),
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_admin,x,model_school_student,school_student.group_school_admin,1,1,1,1\n"
            "access_user,x,model_school_student,school_student.group_school_user,1,1,0,0\n"
        ),
    )
    before_xml = generated.security_xml
    _autofix_wrong_module_prefixed_local_group_id(generated, _MODULE_NAME)
    assert generated.security_xml == before_xml, "the already-correct bare XML records are untouched"
    assert "school_student.group_school_admin" not in generated.security_csv
    assert "school_student.group_school_user" not in generated.security_csv
    assert f"{_MODULE_NAME}.group_school_admin" in generated.security_csv
    assert f"{_MODULE_NAME}.group_school_user" in generated.security_csv
    print("PASS: a CSV-only wrong prefix (the module's own display name, not its real technical "
          "name) pointing at an otherwise-correctly bare-defined group is corrected, closing the "
          "real live crash the first version of this fix left open")


def test_fixes_a_wrong_prefixed_ref_attribute_inside_an_ir_rule():
    """Real, confirmed bug found live (2026-07-29, same task, record_
    rules round, immediately after the second version of this fix
    deployed): a THIRD instance of the identical wrong-display-name-
    prefix mistake, this time inside an <ir.rule>'s own `ref="..."`
    attribute (e.g. a `groups` field eval referencing `ref="school_
    student.group_school_admin"`) -- a completely different XML syntax
    location than either the group's own <record id=...> or a CSV
    cell. Confirmed live: real Odoo install crashed with "generated XML
    references external id(s) ['school_student.group_school_admin']
    ... that do not exist in the real Odoo registry".
    """
    generated = _make_generated(
        security_xml=(
            '<odoo>\n'
            '    <record id="group_school_admin" model="res.groups">\n'
            '        <field name="name">Admin</field>\n'
            '    </record>\n'
            '    <record id="rule_school_student_delete" model="ir.rule">\n'
            '        <field name="name">Only admins can delete students</field>\n'
            '        <field name="model_id" ref="model_school_student"/>\n'
            '        <field name="groups" eval="[(4, ref(\'school_student.group_school_admin\'))]"/>\n'
            '        <field name="perm_read">0</field>\n'
            '        <field name="perm_unlink">1</field>\n'
            '    </record>\n'
            '</odoo>'
        ),
    )
    _autofix_wrong_module_prefixed_local_group_id(generated, _MODULE_NAME)
    assert "school_student.group_school_admin" not in generated.security_xml
    assert f"{_MODULE_NAME}.group_school_admin" in generated.security_xml
    print("PASS: a wrong-prefixed ref=\"...\" attribute inside an ir.rule is also corrected, "
          "closing the real live crash the second version of this fix left open")


def test_is_a_noop_when_no_security_xml():
    generated = _make_generated(security_xml=None)
    _autofix_wrong_module_prefixed_local_group_id(generated, _MODULE_NAME)  # must not raise
    print("PASS: a no-op when there's no security_xml at all")


if __name__ == "__main__":
    test_fixes_the_real_live_wrong_prefix_shape()
    test_never_touches_a_correctly_prefixed_group()
    test_never_touches_a_bare_unprefixed_group()
    test_fixes_a_csv_only_wrong_prefix_pointing_at_a_correctly_bare_defined_group()
    test_fixes_a_wrong_prefixed_ref_attribute_inside_an_ir_rule()
    test_is_a_noop_when_no_security_xml()
    print("\nALL WRONG-PREFIXED-LOCAL-GROUP TESTS PASSED")
