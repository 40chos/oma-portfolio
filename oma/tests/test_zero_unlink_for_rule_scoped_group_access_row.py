"""Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
flagship run, ticket_access_rights node), read end-to-end across 15+ real Code-Review rounds:
a security_csv row granting group_field_technician (a group already row-restricted by its own
ir.rule) perm_unlink=1 directly contradicts the goal's own stated scope for that role ("see and
update", never "delete"). An earlier version of this autofix stripped the WHOLE row whenever
ANY of create/write/unlink was granted -- which itself caused Code-Review's own "Field
Technicians are missing from the access control list" complaint two rounds later, since that
role's own create/write access is genuinely needed. Narrowed to zero out ONLY perm_unlink.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_zero_unlink_for_rule_scoped_group_access_row,
)


def _make_generated(security_csv: str, security_xml: str) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="class X(models.Model):\n    _name = 'x'\n",
        security_csv=security_csv, security_xml=security_xml, views_xml=None, notes="",
    )


_REAL_SECURITY_XML = (
    '<odoo>'
    '<record id="group_field_technician" model="res.groups">'
    '<field name="name">Field Technician</field></record>'
    '<record id="group_operations_manager" model="res.groups">'
    '<field name="name">Operations Manager</field></record>'
    '<record id="rule_service_ticket_technician_own" model="ir.rule">'
    '<field name="name">Service Ticket: Technicians only see their own</field>'
    '<field name="model_id" ref="oma_x.model_oma_service_ticket"/>'
    '<field name="domain_force">[(\'technician_id\', \'=\', user.id)]</field>'
    '<field name="groups" eval="[(4, ref(\'oma_x.group_field_technician\'))]"/>'
    '</record>'
    '</odoo>'
)


def test_zeroes_perm_unlink_but_keeps_create_and_write_closing_the_real_live_gap():
    security_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_service_ticket_group_field_technician,oma.service.ticket,"
        "model_oma_service_ticket,oma_x.group_field_technician,1,1,1,1\n"
    )
    generated = _make_generated(security_csv, _REAL_SECURITY_XML)
    _autofix_zero_unlink_for_rule_scoped_group_access_row(generated)
    lines = generated.security_csv.splitlines()
    row = lines[1].split(",")
    assert row[4:7] == ["1", "1", "1"], f"expected read/write/create to stay 1 -- got {row!r}"
    assert row[7] == "0", f"expected perm_unlink zeroed to 0 -- got {row!r}"
    print("PASS: perm_unlink is zeroed while create/write/read stay intact, closing the real "
          "live gap found on task 07141af5's ticket_access_rights node -- the earlier "
          "whole-row-strip version of this fix caused its own 'Field Technicians missing from "
          "the access control list' regression two rounds later")


def test_never_touches_a_group_with_no_ir_rule_scoping_it():
    """The operations-manager group is deliberately UNRESTRICTED (no ir.rule at all) -- whether
    IT should keep perm_unlink=1 is a separate, goal-specific judgment this rule-scoped-group
    check must never make.
    """
    security_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_service_ticket_manager,oma.service.ticket,model_oma_service_ticket,"
        "oma_x.group_operations_manager,1,1,1,1\n"
    )
    generated = _make_generated(security_csv, _REAL_SECURITY_XML)
    _autofix_zero_unlink_for_rule_scoped_group_access_row(generated)
    assert generated.security_csv == security_csv, (
        "a group never scoped by any ir.rule must never have its perm_unlink touched"
    )
    print("PASS: never touches a group with no ir.rule scoping it down")


def test_never_touches_a_row_that_already_has_perm_unlink_zero():
    security_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_x,oma.service.ticket,model_oma_service_ticket,"
        "oma_x.group_field_technician,1,1,1,0\n"
    )
    generated = _make_generated(security_csv, _REAL_SECURITY_XML)
    _autofix_zero_unlink_for_rule_scoped_group_access_row(generated)
    assert generated.security_csv == security_csv, (
        "a row already correct (perm_unlink=0) must never be rewritten"
    )
    print("PASS: never touches a row that already has perm_unlink=0")


def test_is_a_noop_when_there_is_no_security_xml_or_csv():
    generated = _make_generated("", "")
    generated.security_xml = None
    generated.security_csv = ""
    _autofix_zero_unlink_for_rule_scoped_group_access_row(generated)
    assert generated.security_csv == ""
    print("PASS: a no-op when there is no security_xml or security_csv content at all")


def test_falls_back_to_old_files_when_this_round_never_touched_security_xml():
    """A scoped-edit round that only regenerates ir.model.access.csv (not security.xml) leaves
    `generated.security_xml` empty this round -- this autofix must still see the real ir.rule
    via the round's own current committed security.xml, passed through `old_files_by_relpath`.
    """
    security_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_service_ticket_group_field_technician,oma.service.ticket,"
        "model_oma_service_ticket,oma_x.group_field_technician,1,1,1,1\n"
    )
    generated = _make_generated(security_csv, security_xml=None)
    old_files_by_relpath = {"oma_x/security/security.xml": _REAL_SECURITY_XML}
    _autofix_zero_unlink_for_rule_scoped_group_access_row(generated, old_files_by_relpath)
    assert generated.security_csv.splitlines()[1].split(",")[7] == "0", (
        f"expected the fallback to old_files_by_relpath's own real security.xml to still zero "
        f"perm_unlink -- got: {generated.security_csv!r}"
    )
    print("PASS: falls back to old_files_by_relpath's real security.xml when this round's own "
          "diff never touched it")


def test_is_a_noop_when_no_ir_rule_has_a_groups_field():
    """A GLOBAL ir.rule (no `groups` field at all) applies to everyone -- not a role-scoped
    rule this autofix should ever key off of.
    """
    global_rule_xml = (
        '<odoo><record id="rule_global" model="ir.rule">'
        '<field name="name">Global</field>'
        '<field name="model_id" ref="oma_x.model_oma_service_ticket"/>'
        '<field name="domain_force">[(1, \'=\', 1)]</field>'
        '</record></odoo>'
    )
    security_csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_x,oma.service.ticket,model_oma_service_ticket,"
        "oma_x.group_field_technician,1,1,1,1\n"
    )
    generated = _make_generated(security_csv, global_rule_xml)
    _autofix_zero_unlink_for_rule_scoped_group_access_row(generated)
    assert generated.security_csv == security_csv, (
        "a global (non-group-scoped) ir.rule must never trigger touching any access row"
    )
    print("PASS: a global ir.rule with no groups field never triggers any change")


if __name__ == "__main__":
    test_zeroes_perm_unlink_but_keeps_create_and_write_closing_the_real_live_gap()
    test_never_touches_a_group_with_no_ir_rule_scoping_it()
    test_never_touches_a_row_that_already_has_perm_unlink_zero()
    test_is_a_noop_when_there_is_no_security_xml_or_csv()
    test_falls_back_to_old_files_when_this_round_never_touched_security_xml()
    test_is_a_noop_when_no_ir_rule_has_a_groups_field()
    print("\nALL ZERO-UNLINK-FOR-RULE-SCOPED-GROUP TESTS PASSED")
