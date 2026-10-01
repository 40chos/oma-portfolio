"""Phase 28C (2026-07-29): unit tests for
_validate_security_csv_group_id_refers_to_a_defined_group() -- the
real, confirmed live gap found on the school_student task's own
security_groups round, immediately AFTER the wrong-module-prefix bug
was fixed.

Root cause: _validate_self_referenced_xmlids_are_defined()'s own
docstring already documented this exact gap and explicitly promised
"covered by the CSV-side validators instead" -- but no such validator
actually existed. Confirmed live: security_csv's group_id:id column
correctly referenced this module's own group_school_admin/
group_school_user (module-qualified correctly this time), but NEITHER
had a matching <record model="res.groups"> anywhere in security_xml --
a real Odoo install crash ("No matching record found for external id
... in field 'Group'"), never caught pre-write until this.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_synthesize_missing_res_groups_record_for_csv_referenced_group,
    _validate_security_csv_group_id_refers_to_a_defined_group,
)

_MODULE_NAME = "oma_simple_custom_module_task_595ad7bc"


def _make_generated(security_xml: str | None, security_csv: str) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv=security_csv,
        security_xml=security_xml, views_xml=None, notes="",
    )


def test_catches_the_real_live_undefined_groups():
    generated = _make_generated(
        security_xml=None,  # no res.groups records defined at all
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            f"access_admin,x,model_school_student,{_MODULE_NAME}.group_school_admin,1,1,1,1\n"
            f"access_user,x,model_school_student,{_MODULE_NAME}.group_school_user,1,1,0,0\n"
        ),
    )
    raised = False
    try:
        _validate_security_csv_group_id_refers_to_a_defined_group(generated, _MODULE_NAME)
    except ValueError as exc:
        raised = True
        assert "group_school_admin" in str(exc)
        assert "group_school_user" in str(exc)
    assert raised, (
        "CSV rows referencing groups with no matching <record model='res.groups'> must be "
        "caught before they ever reach Odoo's own install"
    )
    print("PASS: the exact real live gap (module-qualified group refs with no matching record) "
          "is caught deterministically, closing the documented-but-never-built CSV-side validator")


def test_does_not_false_positive_when_groups_are_genuinely_defined():
    generated = _make_generated(
        security_xml=(
            '<odoo>\n'
            '    <record id="group_school_admin" model="res.groups">\n'
            '        <field name="name">Admin</field>\n'
            '    </record>\n'
            f'    <record id="{_MODULE_NAME}.group_school_user" model="res.groups">\n'
            '        <field name="name">User</field>\n'
            '    </record>\n'
            '</odoo>'
        ),
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            f"access_admin,x,model_school_student,{_MODULE_NAME}.group_school_admin,1,1,1,1\n"
            f"access_user,x,model_school_student,group_school_user,1,1,0,0\n"
        ),
    )
    _validate_security_csv_group_id_refers_to_a_defined_group(generated, _MODULE_NAME)  # must not raise
    print("PASS: no false positive when every referenced group is genuinely defined (bare or "
          "self-prefixed <record> both recognized)")


def test_never_flags_a_genuine_external_group_reference():
    generated = _make_generated(
        security_xml=None,
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_admin,x,model_school_student,base.group_user,1,1,1,1\n"
        ),
    )
    _validate_security_csv_group_id_refers_to_a_defined_group(generated, _MODULE_NAME)  # must not raise
    print("PASS: a group prefixed with a genuinely different module (a real external reference) "
          "is never flagged -- that's the live registry's own concern, not this check's")


def test_is_a_noop_when_csv_has_no_group_id_column():
    generated = _make_generated(security_xml=None, security_csv="id,name,model_id:id\naccess_x,x,model_y\n")
    _validate_security_csv_group_id_refers_to_a_defined_group(generated, _MODULE_NAME)  # must not raise
    print("PASS: a no-op for a CSV with no group_id:id column at all")


def test_autofix_synthesizes_the_real_missing_group_record_closing_the_live_gap():
    """Real bug found live (2026-08-08, task e89150c8-69de-4cad-8450-69eda822cfcb's flagship
    run, ticket_manager_access node): security_csv referenced
    'group_service_ticket_manager' (correctly self-prefixed), but no matching <record
    model="res.groups"> existed anywhere in security_xml -- recurred identically across two
    consecutive rounds and escalated the whole task to a human decision for something entirely
    mechanical (the CSV cell already states the group's own intended existence and name).
    """
    module_name = "oma_build_a_complete_field_664d1b63"
    generated = _make_generated(
        security_xml='<odoo>\n</odoo>',
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            f"access_manager,x,model_oma_service_ticket,{module_name}.group_service_ticket_manager,1,1,1,1\n"
        ),
    )
    _autofix_synthesize_missing_res_groups_record_for_csv_referenced_group(generated, module_name)
    assert '<record id="group_service_ticket_manager" model="res.groups">' in generated.security_xml, (
        f"expected a synthesized res.groups record for the CSV-referenced group -- got "
        f"{generated.security_xml!r}"
    )
    assert "<field name=\"name\">Service Ticket Manager</field>" in generated.security_xml
    # The validator this autofix runs ahead of must now be satisfied.
    _validate_security_csv_group_id_refers_to_a_defined_group(generated, module_name)  # must not raise
    print("PASS: the missing res.groups record is synthesized deterministically from the CSV's "
          "own group_id:id cell, closing the real live gap found on task e89150c8's "
          "ticket_manager_access node -- no more escalating a purely mechanical omission to a "
          "human decision")


def test_autofix_adds_manifest_reference_when_it_creates_security_xml_for_the_first_time():
    """Real bug found live (2026-08-08, same task e89150c8, service_ticket_model node,
    immediately after the first version of this autofix deployed): security_xml started out
    None (this round's own LLM output never touched it, only security_csv), so
    _autofix_manifest_missing_security_xml_reference() -- which runs BEFORE this autofix in the
    real call sequence -- correctly saw nothing to reference and no-op'd. This autofix then
    synthesized security_xml's first-ever real content, but the manifest's own 'data' list never
    got 'security/security.xml' added -- confirmed live via a real sandbox crash ("No matching
    record found for external id ... group_service_ticket_manager") even though the group
    record itself was, by then, genuinely present in the file. The manifest reference must be
    added here too, regardless of autofix ordering.
    """
    module_name = "oma_build_a_complete_field_664d1b63"
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=["security/ir.model.access.csv"],
    )
    generated = GeneratedModuleFiles(
        manifest_fields=manifest, models_py="",
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            f"access_ticket,x,model_oma_service_ticket,{module_name}.group_service_ticket_manager,1,1,1,0\n"
        ),
        security_xml=None, views_xml=None, notes="",
    )
    _autofix_synthesize_missing_res_groups_record_for_csv_referenced_group(generated, module_name)
    assert "security/security.xml" in generated.manifest_fields.data, (
        f"expected the manifest to reference the newly-synthesized security.xml -- got "
        f"data={generated.manifest_fields.data!r}"
    )
    print("PASS: the manifest gets 'security/security.xml' added the moment this autofix "
          "creates that file's first-ever real content, closing the real live gap found on "
          "task e89150c8's service_ticket_model node")


def test_autofix_never_touches_a_genuinely_external_group_reference():
    module_name = "oma_build_a_complete_field_664d1b63"
    generated = _make_generated(
        security_xml=None,
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_admin,x,model_x,base.group_user,1,1,1,1\n"
        ),
    )
    _autofix_synthesize_missing_res_groups_record_for_csv_referenced_group(generated, module_name)
    assert generated.security_xml is None, (
        "a genuinely external group reference (a different module's real group) must never get "
        "a synthesized local record -- that's not this module's group to define"
    )
    print("PASS: never synthesizes a record for a genuinely external group reference")


def test_autofix_is_a_noop_when_the_group_is_already_defined():
    module_name = "oma_build_a_complete_field_664d1b63"
    original_xml = (
        '<odoo>\n'
        '    <record id="group_service_ticket_manager" model="res.groups">\n'
        '        <field name="name">Manager</field>\n'
        '    </record>\n'
        '</odoo>'
    )
    generated = _make_generated(
        security_xml=original_xml,
        security_csv=(
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            f"access_manager,x,model_x,{module_name}.group_service_ticket_manager,1,1,1,1\n"
        ),
    )
    _autofix_synthesize_missing_res_groups_record_for_csv_referenced_group(generated, module_name)
    assert generated.security_xml == original_xml, (
        "must never add a duplicate record when the group is already genuinely defined"
    )
    print("PASS: a no-op when every CSV-referenced group is already genuinely defined")


if __name__ == "__main__":
    test_catches_the_real_live_undefined_groups()
    test_does_not_false_positive_when_groups_are_genuinely_defined()
    test_never_flags_a_genuine_external_group_reference()
    test_is_a_noop_when_csv_has_no_group_id_column()
    test_autofix_synthesizes_the_real_missing_group_record_closing_the_live_gap()
    test_autofix_adds_manifest_reference_when_it_creates_security_xml_for_the_first_time()
    test_autofix_never_touches_a_genuinely_external_group_reference()
    test_autofix_is_a_noop_when_the_group_is_already_defined()
    print("\nALL CSV-GROUP-ID-DEFINED TESTS PASSED")
