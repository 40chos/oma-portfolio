"""Phase 29 (2026-07-29): unit test for
_autofix_ensure_baseline_access_row_for_rule_referenced_group() -- the
real, confirmed live root cause behind 2 straight identical Code-Review
findings on the school_student task's own demo_data round: security.xml
correctly defined `rule_school_student_delete` scoped to the
`group_school_admin` group, but ir.model.access.csv had no row for that
group at all, leaving admins with zero base access rights -- an `ir.rule`
can only ever NARROW access already granted via access.csv, never grant
it on its own, so the whole rule was silently pointless.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_ensure_baseline_access_row_for_rule_referenced_group,
)

_MODULE_NAME = "oma_simple_custom_module_task_595ad7bc"
_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)

_REAL_SECURITY_XML = (
    "<odoo><data>"
    '<record id="group_school_admin" model="res.groups"><field name="name">School / Admin</field></record>'
    '<record id="rule_school_student_delete" model="ir.rule">'
    '<field name="name">School Student: Admins can delete</field>'
    f'<field name="model_id" ref="{_MODULE_NAME}.model_school_student"/>'
    '<field name="domain_force">[]</field>'
    f"<field name=\"groups\" eval=\"[(4, ref('{_MODULE_NAME}.group_school_admin'))]\"/>"
    "</record></data></odoo>"
)
_SECURITY_CSV_MISSING_ADMIN_ROW = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    "access_school_student,school.student,model_school_student,base.group_user,1,1,1,0\n"
)


def _make_generated(security_csv: str, security_xml: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py="", security_csv=security_csv,
        security_xml=security_xml, views_xml=None, notes="",
    )


def test_adds_the_real_missing_admin_access_row():
    generated = _make_generated(_SECURITY_CSV_MISSING_ADMIN_ROW, _REAL_SECURITY_XML)
    _autofix_ensure_baseline_access_row_for_rule_referenced_group(generated)
    assert f"{_MODULE_NAME}.group_school_admin" in generated.security_csv
    assert "security/ir.model.access.csv" in generated.manifest_fields.data
    print("PASS: the real, confirmed live missing admin access row is added")


def test_never_duplicates_an_existing_row():
    csv_already_has_it = _SECURITY_CSV_MISSING_ADMIN_ROW + (
        f"access_admin,school.student,model_school_student,{_MODULE_NAME}.group_school_admin,1,1,1,1\n"
    )
    generated = _make_generated(csv_already_has_it, _REAL_SECURITY_XML)
    before = generated.security_csv
    _autofix_ensure_baseline_access_row_for_rule_referenced_group(generated)
    assert generated.security_csv == before, "must never add a duplicate row when one already exists"
    print("PASS: never duplicates a row for a (model, group) pair that already has one")


def test_never_touches_content_with_no_ir_rule_at_all():
    plain_xml = '<odoo><record id="group_x" model="res.groups"><field name="name">X</field></record></odoo>'
    generated = _make_generated(_SECURITY_CSV_MISSING_ADMIN_ROW, plain_xml)
    before = generated.security_csv
    _autofix_ensure_baseline_access_row_for_rule_referenced_group(generated)
    assert generated.security_csv == before, "must never touch content with no ir.rule record at all"
    print("PASS: never touches security_xml with no ir.rule at all")


def test_no_crash_on_empty_content():
    generated = _make_generated("", "")
    _autofix_ensure_baseline_access_row_for_rule_referenced_group(generated)
    print("PASS: no-op, no crash on empty security_xml/security_csv")


def test_item172_double_quoted_ref_form_also_triggers_the_fix():
    """P11 fifth pass item 172: the original regex only matched the single-quoted ref('...') call
    form -- a record rule using the double-quoted form (ref("...")) never triggered this autofix.
    """
    double_quoted_xml = (
        "<odoo><data>"
        '<record id="group_school_admin" model="res.groups"><field name="name">School / Admin</field></record>'
        '<record id="rule_school_student_delete" model="ir.rule">'
        '<field name="name">School Student: Admins can delete</field>'
        f'<field name="model_id" ref="{_MODULE_NAME}.model_school_student"/>'
        '<field name="domain_force">[]</field>'
        f'<field name="groups" eval=\'[(4, ref("{_MODULE_NAME}.group_school_admin"))]\'/>'
        "</record></data></odoo>"
    )
    generated = _make_generated(_SECURITY_CSV_MISSING_ADMIN_ROW, double_quoted_xml)
    _autofix_ensure_baseline_access_row_for_rule_referenced_group(generated)
    assert f"{_MODULE_NAME}.group_school_admin" in generated.security_csv
    print("PASS item172: the double-quoted ref(\"...\") form also triggers the fix")


def test_item172_model_id_ref_is_never_mistaken_for_a_group():
    """The real risk this fix's own reused regex choice (_XML_REF_CALL_RE, not the broader
    _find_all_xml_refs()) avoids: the SAME block's own model_id ref="..." ATTRIBUTE must never be
    picked up as if it were a group reference.
    """
    generated = _make_generated(_SECURITY_CSV_MISSING_ADMIN_ROW, _REAL_SECURITY_XML)
    _autofix_ensure_baseline_access_row_for_rule_referenced_group(generated)
    # no row's own group_id:id column should ever be the model xmlid itself
    for line in generated.security_csv.splitlines()[1:]:
        cols = line.split(",")
        if len(cols) > 3:
            assert cols[3] != f"{_MODULE_NAME}.model_school_student"
    print("PASS item172: the model_id ref is never mistaken for a group reference")


_TICKET_MODULE = "oma_build_a_complete_field_ab52b7f8"
_RESTRICTING_SECURITY_XML = (
    "<odoo>"
    '<record id="group_field_technician" model="res.groups">'
    '<field name="name">Field Technician</field></record>'
    '<record id="rule_service_ticket_technician_own" model="ir.rule">'
    '<field name="name">Service Ticket: Technicians only see their own</field>'
    f'<field name="model_id" ref="{_TICKET_MODULE}.model_oma_service_ticket"/>'
    "<field name=\"domain_force\">[('technician_id', '=', user.id)]</field>"
    f"<field name=\"groups\" eval=\"[(4, ref('{_TICKET_MODULE}.group_field_technician'))]\"/>"
    "</record></odoo>"
)
_TICKET_CSV_WITH_BASE_ACCESS = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    "access_oma_service_ticket,oma.service.ticket,model_oma_service_ticket,base.group_user,1,1,1,0\n"
)


def test_adds_a_row_with_perm_unlink_zero_for_a_row_restricting_rule():
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, ticket_access_rights node), read end-to-end across 15+ real Code-Review
    rounds: this autofix originally either (a) unconditionally granted perm_unlink=1 -- wrong,
    since the goal's own language for a row-restricted role is "see and update," never
    "delete" -- or, in an intermediate fix, (b) skipped adding the row entirely whenever
    baseline access already existed via another group -- which caused Code-Review's own
    "Field Technicians are missing from the access control list" complaint, since that role
    genuinely DOES need its own dedicated row (an ir.rule only narrows rows for a group that
    already has a row of its own; it grants nothing on its own). The correct, final shape: a
    row-restricting rule's own group ALWAYS gets its own baseline row, with perm_unlink=0.
    """
    generated = _make_generated(_TICKET_CSV_WITH_BASE_ACCESS, _RESTRICTING_SECURITY_XML)
    _autofix_ensure_baseline_access_row_for_rule_referenced_group(generated)
    matching_rows = [
        line for line in generated.security_csv.splitlines()
        if f"{_TICKET_MODULE}.group_field_technician" in line
    ]
    assert len(matching_rows) == 1, (
        f"expected exactly one new row for group_field_technician -- got: "
        f"{generated.security_csv!r}"
    )
    cols = matching_rows[0].split(",")
    assert cols[4:7] == ["1", "1", "1"], f"expected read/write/create=1 -- got {cols!r}"
    assert cols[7] == "0", f"expected perm_unlink=0 for a row-restricting rule -- got {cols!r}"
    print("PASS: a row-restricting rule's own group always gets its own baseline row with "
          "perm_unlink=0, closing the real live gap found on task 07141af5's "
          "ticket_access_rights node (this exact fix oscillated between two wrong shapes "
          "across 15+ real rounds before landing here)")


def test_still_adds_a_row_with_perm_unlink_one_for_an_elevating_rule():
    """The sibling, genuinely different shape this function was originally built for: a rule
    with NO user-reference in its domain (domain_force=[], matching every row -- the rule
    exists purely to gate an elevated permission, e.g. "admins can delete") still gets the
    original full perm_unlink=1 grant. See test_adds_the_real_missing_admin_access_row above
    for the exact real incident this covers.
    """
    generated = _make_generated(_SECURITY_CSV_MISSING_ADMIN_ROW, _REAL_SECURITY_XML)
    _autofix_ensure_baseline_access_row_for_rule_referenced_group(generated)
    matching_rows = [
        line for line in generated.security_csv.splitlines()
        if f"{_MODULE_NAME}.group_school_admin" in line
    ]
    assert len(matching_rows) == 1
    assert matching_rows[0].split(",")[7] == "1", "an elevating rule's group still gets perm_unlink=1"
    print("PASS: an elevating (non-user-referencing) rule's own group still gets the original "
          "full perm_unlink=1 grant")


if __name__ == "__main__":
    test_adds_the_real_missing_admin_access_row()
    test_never_duplicates_an_existing_row()
    test_never_touches_content_with_no_ir_rule_at_all()
    test_no_crash_on_empty_content()
    test_item172_double_quoted_ref_form_also_triggers_the_fix()
    test_item172_model_id_ref_is_never_mistaken_for_a_group()
    test_adds_a_row_with_perm_unlink_zero_for_a_row_restricting_rule()
    test_still_adds_a_row_with_perm_unlink_one_for_an_elevating_rule()
    print("\nALL BASELINE-ACCESS-ROW-FOR-RULE-GROUP TESTS PASSED")
