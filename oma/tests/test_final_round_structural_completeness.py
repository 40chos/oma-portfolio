"""Real, confirmed bugs found live (2026-08-10, task 07141af5's flagship run) via a direct
real-platform verification AFTER all 9 constraints had already reported "satisfied":

1. `oma.equipment` and `oma.service.ticket` had ZERO `ir.ui.view` records anywhere in the final,
   fully-installed module (confirmed via a direct query against the real database) -- no way for
   a real user to browse, create, or edit them through the Odoo UI.
2. `security/ir.model.access.csv` granted `base.group_user` (every internal employee) full
   read/write access to `oma.service.ticket`, silently defeating the genuinely restrictive
   `ir.rule` scoped only to the narrower `group_field_technician` group.

Every existing validator in specialists/build/specialist.py checks a single round's own diff in
isolation; nothing ever checked whole-module structural completeness once a decomposed task's
last constraint is about to pass. See `_validate_final_round_every_standalone_model_has_a_view`
and `_validate_final_round_no_overpermissive_base_group_access` for the full incident.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_final_round_every_standalone_model_has_a_view,
    _validate_final_round_no_overpermissive_base_group_access,
)

# Exact real shape from the live incident: two standalone models (equipment, service ticket)
# with real Many2one relations between them, plus a genuinely embedded child model
# (maintenance history, a One2many comodel) that must NOT be flagged.
_REAL_MODELS_PY = (
    "from odoo import models, fields\n\n"
    "class ServiceTicketMaintenance(models.Model):\n"
    "    _name = 'oma.service.ticket.maintenance'\n"
    "    ticket_id = fields.Many2one('oma.service.ticket')\n\n"
    "class ServiceTicket(models.Model):\n"
    "    _name = 'oma.service.ticket'\n"
    "    equipment_id = fields.Many2one('oma.equipment')\n"
    "    maintenance_history = fields.One2many('oma.service.ticket.maintenance', 'ticket_id')\n\n"
    "class Equipment(models.Model):\n"
    "    _name = 'oma.equipment'\n"
)


def _make_generated(
    models_py: str = "", views_xml: str | None = None,
    security_csv: str = "", security_xml: str | None = None,
) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py=models_py, security_csv=security_csv,
        security_xml=security_xml, views_xml=views_xml, notes="",
    )


def test_catches_the_real_missing_views_for_standalone_models():
    generated = _make_generated(models_py=_REAL_MODELS_PY, views_xml=None)
    raised = False
    try:
        _validate_final_round_every_standalone_model_has_a_view(
            generated, old_files_by_relpath=None, remaining_constraint_labels=[],
        )
    except ValueError as exc:
        raised = True
        assert "oma.equipment" in str(exc)
        assert "oma.service.ticket" in str(exc)
        assert "oma.service.ticket.maintenance" not in str(exc), (
            "the genuinely embedded One2many comodel must never be flagged"
        )
    assert raised, "the real missing-view gap must be caught before the task is declared done"
    print("PASS: catches the real live gap -- standalone models with zero views, never "
          "flagging the genuinely embedded child model")


def test_never_fires_while_constraints_remain():
    generated = _make_generated(models_py=_REAL_MODELS_PY, views_xml=None)
    _validate_final_round_every_standalone_model_has_a_view(
        generated, old_files_by_relpath=None,
        remaining_constraint_labels=["ticket_list_view"],
    )  # must not raise -- an earlier round isn't responsible for a later constraint's own view
    print("PASS: never fires while constraints remain -- an earlier round is never punished "
          "for a later constraint's own responsibility")


def test_no_false_positive_when_views_exist_via_old_files_by_relpath():
    generated = _make_generated(models_py="", views_xml=None)
    old_files_by_relpath = {
        "models/models.py": _REAL_MODELS_PY,
        "views/views.xml": (
            '<odoo><record id="v1" model="ir.ui.view">'
            '<field name="model">oma.equipment</field></record>'
            '<record id="v2" model="ir.ui.view">'
            '<field name="model">oma.service.ticket</field></record></odoo>'
        ),
    }
    _validate_final_round_every_standalone_model_has_a_view(
        generated, old_files_by_relpath, remaining_constraint_labels=[],
    )  # must not raise -- both standalone models genuinely have real views
    print("PASS: no false positive when real views for both standalone models already exist "
          "in the prior committed state")


def test_no_false_positive_when_this_rounds_own_views_xml_covers_it():
    generated = _make_generated(
        models_py=_REAL_MODELS_PY,
        views_xml=(
            '<odoo><record id="v1" model="ir.ui.view">'
            '<field name="model">oma.equipment</field></record>'
            '<record id="v2" model="ir.ui.view">'
            '<field name="model">oma.service.ticket</field></record></odoo>'
        ),
    )
    _validate_final_round_every_standalone_model_has_a_view(
        generated, old_files_by_relpath=None, remaining_constraint_labels=[],
    )
    print("PASS: no false positive when THIS round's own new views_xml covers both models")


_REAL_SECURITY_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
    '    <record id="group_field_technician" model="res.groups">\n'
    '        <field name="name">Field Technician</field>\n'
    "    </record>\n"
    '    <record id="rule_service_ticket_technician_own" model="ir.rule">\n'
    '        <field name="name">Service Ticket: Technicians only see their own</field>\n'
    '        <field name="model_id" ref="oma_x.model_oma_service_ticket"/>\n'
    "        <field name=\"domain_force\">[('technician_id', '=', user.id)]</field>\n"
    "        <field name=\"groups\" eval=\"[(4, ref('oma_x.group_field_technician'))]\"/>\n"
    "    </record>\n</odoo>\n"
)
_REAL_SECURITY_CSV_WITH_HOLE = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    "access_oma_service_ticket,oma.service.ticket,model_oma_service_ticket,base.group_user,1,1,1,0\n"
    "access_oma_service_ticket_technician,oma.service.ticket,model_oma_service_ticket,"
    "oma_x.group_field_technician,1,1,1,0\n"
)
_REAL_SECURITY_CSV_FIXED = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    "access_oma_service_ticket_technician,oma.service.ticket,model_oma_service_ticket,"
    "oma_x.group_field_technician,1,1,1,0\n"
)


def test_catches_the_real_overpermissive_base_group_access_hole():
    generated = _make_generated(
        security_csv=_REAL_SECURITY_CSV_WITH_HOLE, security_xml=_REAL_SECURITY_XML,
    )
    raised = False
    try:
        _validate_final_round_no_overpermissive_base_group_access(
            generated, old_files_by_relpath=None, remaining_constraint_labels=[],
        )
    except ValueError as exc:
        raised = True
        assert "oma.service.ticket" in str(exc)
        assert "group_user" in str(exc)
    assert raised, "the real base.group_user access hole must be caught"
    print("PASS: catches the real live security hole -- base.group_user access alongside a "
          "restrictive rule scoped to a narrower group")


def test_no_false_positive_once_the_hole_is_fixed():
    generated = _make_generated(
        security_csv=_REAL_SECURITY_CSV_FIXED, security_xml=_REAL_SECURITY_XML,
    )
    _validate_final_round_no_overpermissive_base_group_access(
        generated, old_files_by_relpath=None, remaining_constraint_labels=[],
    )  # must not raise -- no broad-group row grants access anymore
    print("PASS: no false positive once the access.csv no longer grants base.group_user access")


def test_never_fires_when_no_restrictive_rule_exists_at_all():
    generated = _make_generated(
        security_csv=_REAL_SECURITY_CSV_WITH_HOLE, security_xml="<odoo></odoo>",
    )
    generated.security_xml = "<odoo></odoo>"
    _validate_final_round_no_overpermissive_base_group_access(
        generated, old_files_by_relpath=None, remaining_constraint_labels=[],
    )  # must not raise -- broad access with no restrictive rule anywhere is a completely normal shape
    print("PASS: never fires when there's no restrictive rule at all -- broad, unrestricted "
          "access to a model with no ir.rule is a totally normal, common shape")


def test_never_fires_while_constraints_remain_for_security_check():
    generated = _make_generated(
        security_csv=_REAL_SECURITY_CSV_WITH_HOLE, security_xml=_REAL_SECURITY_XML,
    )
    _validate_final_round_no_overpermissive_base_group_access(
        generated, old_files_by_relpath=None, remaining_constraint_labels=["ticket_access_rights"],
    )  # must not raise while constraints remain
    print("PASS: security completeness check never fires while constraints remain")


if __name__ == "__main__":
    test_catches_the_real_missing_views_for_standalone_models()
    test_never_fires_while_constraints_remain()
    test_no_false_positive_when_views_exist_via_old_files_by_relpath()
    test_no_false_positive_when_this_rounds_own_views_xml_covers_it()
    test_catches_the_real_overpermissive_base_group_access_hole()
    test_no_false_positive_once_the_hole_is_fixed()
    test_never_fires_when_no_restrictive_rule_exists_at_all()
    test_never_fires_while_constraints_remain_for_security_check()
    print("\nALL FINAL-ROUND STRUCTURAL COMPLETENESS TESTS PASSED")
