"""Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
project_ticket_counts node), immediately after the sibling self-referential-depends fix (see
test_compute_field_depends_does_not_include_itself.py) let the field genuinely land: the very
next candidate regressed `_compute_ticket_counts`'s own, previously-correct empty
`@api.depends()` back to `@api.depends('project_id')` on the `project.project`-extending
class -- but `project_id` is `oma.service.ticket`'s own Many2one field pointing AT
`project.project`, not a field of `project.project` itself. Odoo's registry init crashed hard
(`fields.py: resolve_depends` -> `KeyError: 'project_id'`), confirmed live via the real
install traceback and a manager escalation message naming the same defect in plain language.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_compute_field_depends_does_not_reference_other_models_own_fk_field,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, security_csv="id,name\n", notes="",
    )


def test_raises_on_the_real_live_shape():
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    project_id = fields.Many2one('project.project', string='Project', required=True)\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    @api.depends('project_id')\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    try:
        _validate_compute_field_depends_does_not_reference_other_models_own_fk_field(_gen(models_py))
        assert False, "must raise when depends names another model's own FK field"
    except ValueError as exc:
        assert "project_id" in str(exc)
        assert "project.project" in str(exc)
    print("PASS: raises on the real, live cross-model FK-name shape from this incident")


def test_never_raises_on_a_legitimate_own_field_dependency():
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n"
        "    date_deadline = fields.Date(string='Deadline')\n\n"
        "    @api.depends('date_deadline')\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    _validate_compute_field_depends_does_not_reference_other_models_own_fk_field(_gen(models_py))
    print("PASS: never raises when the depends name is declared on the compute field's own model")


def test_never_raises_on_an_unknown_name_never_declared_anywhere():
    # A depends name that is a legitimate native/inherited Odoo field this file never
    # re-declares (e.g. 'user_id' on project.project) must never be flagged -- this
    # validator only catches the specific, confirmed cross-model FK-name collision.
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    @api.depends('user_id')\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    _validate_compute_field_depends_does_not_reference_other_models_own_fk_field(_gen(models_py))
    print("PASS: never raises on a name never declared anywhere in this file")


def test_never_raises_when_the_fk_points_at_a_different_model():
    # The FK field exists elsewhere but points at a DIFFERENT model than the one the compute
    # method belongs to -- must never cross-contaminate.
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    equipment_id = fields.Many2one('oma.equipment', string='Equipment', required=True)\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    @api.depends('equipment_id')\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    _validate_compute_field_depends_does_not_reference_other_models_own_fk_field(_gen(models_py))
    print("PASS: never raises when the FK field points at an unrelated model")


def test_never_raises_on_empty_depends():
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    project_id = fields.Many2one('project.project', string='Project', required=True)\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    @api.depends()\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    _validate_compute_field_depends_does_not_reference_other_models_own_fk_field(_gen(models_py))
    print("PASS: never raises on an empty @api.depends()")


if __name__ == "__main__":
    test_raises_on_the_real_live_shape()
    test_never_raises_on_a_legitimate_own_field_dependency()
    test_never_raises_on_an_unknown_name_never_declared_anywhere()
    test_never_raises_when_the_fk_points_at_a_different_model()
    test_never_raises_on_empty_depends()
    print("\nALL COMPUTE-FIELD-DEPENDS-DOES-NOT-REFERENCE-OTHER-MODELS-FK TESTS PASSED")
