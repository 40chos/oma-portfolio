"""P14 item 2 follow-up fix (docs/reports/PHASE30_P14_ITEM2_BENCHMARK_2026-08-02.md): the new
autofix that widens an existing security_csv row's perm_unlink from 0 to 1 when an ir.rule in the
same module references that group, closing the real root cause behind 4 of 12 benchmark
escalations (tasks 008/020/024/029) without needing an extra LLM round.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_widen_baseline_access_row_denied_by_referencing_rule,
    _validate_record_rule_permission_not_capped_by_access_csv,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)

_RULE_XML = (
    '<record id="rule_delete_restrict" model="ir.rule">'
    '<field name="model_id" ref="model_project_fieldjob"/>'
    '<field name="groups" eval="[(4, ref(\'oma_x.group_admin\'))]"/>'
    '<field name="perm_read">0</field>'
    '<field name="perm_write">0</field>'
    '<field name="perm_create">0</field>'
    '<field name="perm_unlink">1</field>'
    "</record>"
)


def _gen(security_csv: str, security_xml: str = _RULE_XML) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py="", views_xml=None, security_csv=security_csv,
        security_xml=security_xml, extra_data_files=None, notes="",
    )


def test_widens_denied_perm_unlink_for_rule_referenced_group():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_project_fieldjob,oma_x.group_admin,1,1,1,0\n"
    )
    g = _gen(csv)
    _autofix_widen_baseline_access_row_denied_by_referencing_rule(g)
    row = [l for l in g.security_csv.splitlines() if l.startswith("access_x")][0]
    assert row.split(",")[7] == "1"
    print("PASS widens denied perm_unlink")


def test_leaves_unrelated_group_row_untouched():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_project_fieldjob,oma_x.group_admin,1,1,1,0\n"
        "access_y,y,model_res_partner,base.group_user,1,1,1,0\n"
    )
    g = _gen(csv)
    _autofix_widen_baseline_access_row_denied_by_referencing_rule(g)
    other_row = [l for l in g.security_csv.splitlines() if l.startswith("access_y")][0]
    assert other_row.split(",")[7] == "0"
    print("PASS leaves unrelated row untouched")


def test_never_touches_already_granted_row():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_project_fieldjob,oma_x.group_admin,1,1,1,1\n"
    )
    g = _gen(csv)
    before = g.security_csv
    _autofix_widen_baseline_access_row_denied_by_referencing_rule(g)
    assert g.security_csv == before
    print("PASS no-op when already granted")


def test_noop_without_ir_rule():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_project_fieldjob,oma_x.group_admin,1,1,1,0\n"
    )
    g = _gen(csv, security_xml="")
    before = g.security_csv
    _autofix_widen_baseline_access_row_denied_by_referencing_rule(g)
    assert g.security_csv == before
    print("PASS no-op without ir.rule")


_ROW_RESTRICTING_RULE_XML = (
    '<record id="rule_service_ticket_technician_own" model="ir.rule">'
    '<field name="model_id" ref="model_oma_service_ticket"/>'
    "<field name=\"domain_force\">[('technician_id', '=', user.id)]</field>"
    '<field name="groups" eval="[(4, ref(\'oma_x.group_field_technician\'))]"/>'
    "</record>"
)


def test_never_widens_perm_unlink_for_a_row_restricting_rule():
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, ticket_access_rights node): this autofix kept silently widening
    group_field_technician's perm_unlink from 0 back to 1 moments after two sibling autofixes
    (_autofix_ensure_baseline_access_row_for_rule_referenced_group,
    _autofix_zero_unlink_for_rule_scoped_group_access_row) correctly zeroed/created it with
    perm_unlink=0, in the same validator chain -- confirmed via direct before/after debug
    logging at each autofix's own call site. The goal's own language for a row-restricted role
    is "see and update," never "delete" -- a rule whose domain_force references the CURRENT
    USER (`user.id`/`uid`) is exactly that shape and must never have its group's perm_unlink
    widened, regardless of what value the CSV currently has.
    """
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_oma_service_ticket_group_field_technician,oma.service.ticket,"
        "model_oma_service_ticket,oma_x.group_field_technician,1,1,1,0\n"
    )
    g = _gen(csv, security_xml=_ROW_RESTRICTING_RULE_XML)
    before = g.security_csv
    _autofix_widen_baseline_access_row_denied_by_referencing_rule(g)
    assert g.security_csv == before, (
        f"expected no widening for a row-restricting rule's own group -- got: {g.security_csv!r}"
    )
    print("PASS: never widens perm_unlink for a row-restricting rule's own group, closing the "
          "real live gap found on task 07141af5's ticket_access_rights node (this exact "
          "autofix silently undid two sibling fixes moments after they ran)")


def test_still_widens_perm_unlink_for_an_elevating_rule_with_a_domain_force():
    """The sibling, genuinely different shape this function must still handle: an elevating
    rule (domain_force=[], matching every row -- no user-reference at all) still gets its
    group's perm_unlink widened, exactly as before.
    """
    elevating_rule_xml = (
        '<record id="rule_admin_delete" model="ir.rule">'
        '<field name="model_id" ref="model_project_fieldjob"/>'
        '<field name="domain_force">[]</field>'
        '<field name="groups" eval="[(4, ref(\'oma_x.group_admin\'))]"/>'
        "</record>"
    )
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_project_fieldjob,oma_x.group_admin,1,1,1,0\n"
    )
    g = _gen(csv, security_xml=elevating_rule_xml)
    _autofix_widen_baseline_access_row_denied_by_referencing_rule(g)
    row = [l for l in g.security_csv.splitlines() if l.startswith("access_x")][0]
    assert row.split(",")[7] == "1", "an elevating rule (bare domain_force=[]) must still widen"
    print("PASS: still widens perm_unlink for an elevating rule with an explicit domain_force=[]")


def test_validator_no_longer_fires_after_autofix_runs_first():
    csv = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x,x,model_project_fieldjob,oma_x.group_admin,1,1,1,0\n"
    )
    g = _gen(csv)
    _autofix_widen_baseline_access_row_denied_by_referencing_rule(g)
    _validate_record_rule_permission_not_capped_by_access_csv(g)  # must not raise
    print("PASS validator clean after autofix")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} TESTS PASSED")
