"""Real regression test for a real, live-reproduced architecture defect (2026-08-02): task026
("add a Container count smart button to the project form") failed identically on 4 separate real
submissions across 4 different model backends (local, Haiku, Sonnet-narrow-cloud-escalation,
Sonnet-full-pipeline) -- same exact defect every time, an orphaned `_compute_container_count`
method that `_validate_no_orphaned_compute_methods` (P11 Tier A item 6) correctly rejected.

Root cause, confirmed live via direct instrumentation of the real production pipeline (not
guessed): Build's own raw LLM output correctly wired `compute='_compute_container_count'` on the
`container_count` field every single time (6/6 real candidates captured from the real trace
history, across both real rounds). The field was legitimately removed afterward by
`_autofix_remove_colliding_new_field_declarations` because `project.project.container_count`
already exists as a REAL, live field on the target model (confirmed via a direct live query against
odoo16_dev -- leftover from an earlier real attempt at this same task) -- a genuine, correctly
detected collision. But the OLD version of that autofix only called `_remove_field_declaration()`,
which strips the field's own declaration line and nothing else, leaving the field's paired
`_compute_container_count` method behind as now-completely-orphaned dead code -- which
`_validate_no_orphaned_compute_methods` (a LATER step in the same validator chain) then correctly,
but too late to matter, rejected every round. The real defect was never Build's generation; it was
this autofix's own incomplete cleanup.

This is a general architecture defect, not specific to task026's wording: ANY task whose freshly
generated field happens to collide with an already-real field of the same name on the target model,
where that field is declared with a `compute=` kwarg, would hit the identical failure shape.

Uses this task's own actual generated content shape (captured from the real trace history of task
8d3acc5d-c6f9-4939-839f-855628ddc72b), not a synthetic minimal example -- same discipline as the
P2c oscillation-detector fix's own task005-based regression test.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_remove_colliding_new_field_declarations,
    _validate_no_orphaned_compute_methods,
)

# The real, live-captured shape of task026's own generated models_py (round 1, candidate 1 --
# one of the 6 real candidates independently confirmed to correctly wire compute= before this
# autofix ever runs).
_TASK026_REAL_MODELS_PY = """from odoo import api, fields, models

class Project(models.Model):
    _inherit = 'project.project'

    container_count = fields.Integer(
        compute='_compute_container_count',
        string='Container Count'
    )

    def _compute_container_count(self):
        for project in self:
            container_count = self.env['container'].search_count([
                ('project_id', '=', project.id)
            ])
            project.container_count = container_count

    def action_view_containers(self):
        \"\"\"Open the container list for this project\"\"\"
        containers = self.env['container'].search([('project_id', '=', self.id)])
        action = {
            'name': 'Containers',
            'type': 'ir.actions.act_window',
            'res_model': 'container',
            'view_mode': 'tree,form',
            'domain': [('project_id', '=', self.id)],
            'context': {},
            'target': 'current',
        }
        return action
"""


def _make_generated(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="x", version="1.0", category="Uncategorized", summary="", author="",
            depends=["base", "project"], data=[],
        ),
        models_py=models_py, security_csv="", notes="",
    )


def test_colliding_computed_field_removal_also_removes_its_own_orphaned_method():
    """The real fix: when the colliding field carries a compute= kwarg, the autofix must strip
    both the field AND its own compute method together -- never leave the method behind as dead
    code the next validator step will reject.
    """
    generated = _make_generated(_TASK026_REAL_MODELS_PY)
    assert "def _compute_container_count" in generated.models_py
    assert "container_count = fields.Integer(" in generated.models_py

    _autofix_remove_colliding_new_field_declarations(generated, ["container_count"])

    assert "container_count = fields.Integer(" not in generated.models_py, (
        "the colliding field declaration must still be removed, exactly as before this fix"
    )
    assert "def _compute_container_count" not in generated.models_py, (
        "the field's own now-dead compute method must ALSO be removed -- this is the real fix; "
        "leaving it behind is exactly the bug that made task026 fail identically 4 times"
    )
    # The unrelated action method must survive untouched -- this autofix must never over-strip.
    assert "def action_view_containers" in generated.models_py

    # The real, decisive proof: the validator that used to reject this round must now stay
    # silent, since there's no orphaned method left for it to find.
    _validate_no_orphaned_compute_methods(generated)
    print("PASS: removing a compute-backed colliding field also removes its own orphaned "
          "method, and the round no longer trips the orphaned-compute-method validator")


def test_colliding_plain_field_removal_still_works_unchanged():
    """Regression guard: a colliding field with NO compute= kwarg (the original, already-shipped
    behavior this autofix was built for, per its own 2026-07-24 task012 fix) must still be
    removed via the plain single-declaration path -- this fix must not change that case at all.
    """
    models_py = (
        "from odoo import models, fields\n\n"
        "class CrmLead(models.Model):\n"
        "    _inherit = 'crm.lead'\n\n"
        "    tag_ids = fields.Many2many('crm.tag', string='Tags')\n\n"
        "    def action_do_something(self):\n"
        "        return True\n"
    )
    generated = _make_generated(models_py)
    _autofix_remove_colliding_new_field_declarations(generated, ["tag_ids"])
    assert "tag_ids = fields.Many2many(" not in generated.models_py
    assert "def action_do_something" in generated.models_py
    # No compute method existed for this field -- nothing extra should have been touched.
    _validate_no_orphaned_compute_methods(generated)
    print("PASS: a plain, non-computed colliding field is still removed via the original, "
          "unchanged single-declaration path -- this fix only changes the compute-backed case")


def test_colliding_field_removal_with_multiple_fields_only_strips_methods_for_computed_ones():
    """A round colliding on BOTH a plain field and a compute-backed field at once -- confirms
    the fix correctly distinguishes per-field, not a blanket "strip every method" overreaction.
    """
    models_py = (
        "from odoo import models, fields\n\n"
        "class Project(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    container_count = fields.Integer(compute='_compute_container_count', string='Count')\n"
        "    priority_label = fields.Char(string='Priority Label')\n\n"
        "    def _compute_container_count(self):\n"
        "        for rec in self:\n"
        "            rec.container_count = 0\n\n"
        "    def action_noop(self):\n"
        "        return True\n"
    )
    generated = _make_generated(models_py)
    _autofix_remove_colliding_new_field_declarations(generated, ["container_count", "priority_label"])
    assert "container_count" not in generated.models_py
    assert "priority_label" not in generated.models_py
    assert "_compute_container_count" not in generated.models_py
    assert "def action_noop" in generated.models_py
    _validate_no_orphaned_compute_methods(generated)
    print("PASS: multiple simultaneously-colliding fields are each handled correctly by their "
          "own shape -- computed fields lose their method too, plain fields don't need to")


if __name__ == "__main__":
    test_colliding_computed_field_removal_also_removes_its_own_orphaned_method()
    test_colliding_plain_field_removal_still_works_unchanged()
    test_colliding_field_removal_with_multiple_fields_only_strips_methods_for_computed_ones()
    print("\nALL TASK026 ORPHANED-COMPUTE-FROM-COLLISION-AUTOFIX TESTS PASSED")
