"""Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run, ticket_bulk_close
node, recurring across 2 separate rounds even after an explicit correction note): a new
inheriting view record reused the SAME id as the view it inherits from --
`<record id="view_service_ticket_tree" model="ir.ui.view"><field name="inherit_id"
ref="oma_build_a_complete_field_ab52b7f8.view_service_ticket_tree"/>...</record>`. A view record
cannot inherit from itself -- Odoo's own registry load raises a confusing, late
`ValueError: External ID not found in the system: ...` deep inside module loading. See
specialists/build/specialist.py's _validate_no_view_record_self_inherits for the full incident.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_no_view_record_self_inherits,
)


def _make_generated(
    views_xml: str | None = None,
    security_xml: str | None = None,
    extra_data_files: dict | None = None,
) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="id,name\n",
        views_xml=views_xml, security_xml=security_xml, notes="",
        extra_data_files=extra_data_files,
    )


def test_catches_a_real_self_inheriting_view_record_with_module_prefixed_ref():
    generated = _make_generated(views_xml=(
        '<odoo>\n'
        '    <record id="view_service_ticket_tree" model="ir.ui.view">\n'
        '        <field name="name">oma.service.ticket.tree.bulk</field>\n'
        '        <field name="model">oma.service.ticket</field>\n'
        '        <field name="inherit_id" ref="oma_build_a_complete_field_ab52b7f8.view_service_ticket_tree"/>\n'
        '        <field name="arch" type="xml">\n'
        '            <xpath expr="//tree" position="inside"/>\n'
        "        </field>\n"
        "    </record>\n</odoo>"
    ))
    raised = False
    try:
        _validate_no_view_record_self_inherits(generated)
    except ValueError as exc:
        raised = True
        assert "view_service_ticket_tree" in str(exc)
    assert raised, "a genuine self-inheriting view record must be caught before it ever reaches Odoo's own install"
    print("PASS: a real self-inheriting view record (module-prefixed ref) is caught deterministically")


def test_catches_a_real_self_inheriting_view_record_with_bare_ref():
    generated = _make_generated(views_xml=(
        '<odoo>\n'
        '    <record id="view_x" model="ir.ui.view">\n'
        '        <field name="inherit_id" ref="view_x"/>\n'
        "    </record>\n</odoo>"
    ))
    raised = False
    try:
        _validate_no_view_record_self_inherits(generated)
    except ValueError:
        raised = True
    assert raised, "a bare (non-module-prefixed) self-referencing ref must also be caught"
    print("PASS: a bare self-referencing ref is also caught")


def test_does_not_false_positive_on_a_real_legitimate_inherit():
    generated = _make_generated(views_xml=(
        '<odoo>\n'
        '    <record id="view_service_ticket_tree_bulk_close_inherit" model="ir.ui.view">\n'
        '        <field name="name">oma.service.ticket.tree.bulk</field>\n'
        '        <field name="model">oma.service.ticket</field>\n'
        '        <field name="inherit_id" ref="oma_build_a_complete_field_ab52b7f8.view_service_ticket_tree"/>\n'
        '        <field name="arch" type="xml">\n'
        '            <xpath expr="//tree" position="inside"/>\n'
        "        </field>\n"
        "    </record>\n</odoo>"
    ))
    _validate_no_view_record_self_inherits(generated)  # must not raise
    print("PASS: no false positive when the new record's own id genuinely differs from its "
          "inherit_id target")


def test_does_not_false_positive_on_a_record_with_no_inherit_id_at_all():
    generated = _make_generated(views_xml=(
        '<odoo>\n'
        '    <record id="view_service_ticket_tree" model="ir.ui.view">\n'
        '        <field name="name">oma.service.ticket.tree</field>\n'
        '        <field name="model">oma.service.ticket</field>\n'
        "    </record>\n</odoo>"
    ))
    _validate_no_view_record_self_inherits(generated)  # must not raise
    print("PASS: no false positive on a plain, non-inheriting view record")


def test_checks_extra_data_files_too():
    generated = _make_generated(extra_data_files={
        "data/extra_views.xml": (
            '<odoo>\n'
            '    <record id="view_y" model="ir.ui.view">\n'
            '        <field name="inherit_id" ref="module.view_y"/>\n'
            "    </record>\n</odoo>"
        ),
    })
    raised = False
    try:
        _validate_no_view_record_self_inherits(generated)
    except ValueError:
        raised = True
    assert raised, "a self-inheriting view record inside extra_data_files must also be caught"
    print("PASS: extra_data_files entries are also checked")


if __name__ == "__main__":
    test_catches_a_real_self_inheriting_view_record_with_module_prefixed_ref()
    test_catches_a_real_self_inheriting_view_record_with_bare_ref()
    test_does_not_false_positive_on_a_real_legitimate_inherit()
    test_does_not_false_positive_on_a_record_with_no_inherit_id_at_all()
    test_checks_extra_data_files_too()
    print("\nALL VIEW-RECORD-SELF-INHERIT TESTS PASSED")
