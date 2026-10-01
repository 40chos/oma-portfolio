"""Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run, project_ticket_counts
node), the very next round after the sibling fuzzy-word-match bug fix let the field genuinely land
for the first time: `@api.depends('open_ticket_count', 'overdue_ticket_count')` decorating
`_compute_ticket_counts` -- the SAME method that computes those exact two fields. A compute field
can never legitimately depend on itself (or a sibling field computed by the same method) for
recomputation purposes -- Odoo's own registry init (`ir_model.mark_modified()` ->
`records.modified(fnames)` -> `_modified()`) crashed hard building the self-referential dependency
graph, confirmed live via the real install traceback, and recurred identically across 3 consecutive
rounds -- a genuine, repeatable generation mistake, not noise.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_compute_field_depends_does_not_include_itself,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, security_csv="id,name\n", notes="",
    )


def test_raises_on_the_real_live_self_referential_depends_shape():
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n"
        "    overdue_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    @api.depends('open_ticket_count', 'overdue_ticket_count')\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    try:
        _validate_compute_field_depends_does_not_include_itself(_gen(models_py))
        assert False, "must raise when a compute field's own @api.depends lists itself"
    except ValueError as exc:
        assert "_compute_ticket_counts" in str(exc)
        assert "open_ticket_count" in str(exc)
    print("PASS: raises on the real, live self-referential @api.depends shape from this incident")


def test_never_raises_on_a_legitimate_different_field_dependency():
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
        "    @api.depends('name')\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    _validate_compute_field_depends_does_not_include_itself(_gen(models_py))
    print("PASS: never raises when @api.depends lists genuinely different fields")


def test_never_raises_on_empty_depends():
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class Foo(models.Model):\n"
        "    _name = 'oma.foo'\n\n"
        "    bar = fields.Integer(compute='_compute_bar')\n\n"
        "    @api.depends()\n"
        "    def _compute_bar(self):\n"
        "        pass\n"
    )
    _validate_compute_field_depends_does_not_include_itself(_gen(models_py))
    print("PASS: never raises on an empty @api.depends()")


def test_never_raises_when_no_depends_decorator_found_at_all():
    # A compute field whose method has no @api.depends at all (or one this regex can't locate,
    # e.g. separated by another decorator) must never be treated as a false positive -- this
    # validator is conservative, only ever flagging what it can positively confirm.
    models_py = (
        "from odoo import models, fields\n\n\n"
        "class Foo(models.Model):\n"
        "    _name = 'oma.foo'\n\n"
        "    bar = fields.Integer(compute='_compute_bar')\n\n"
        "    def _compute_bar(self):\n"
        "        pass\n"
    )
    _validate_compute_field_depends_does_not_include_itself(_gen(models_py))
    print("PASS: never raises when no @api.depends decorator is found for the compute method")


def test_never_confuses_a_sibling_methods_own_self_reference():
    # Two different compute methods, each with their own (legitimate) depends -- must never
    # cross-contaminate.
    models_py = (
        "from odoo import models, fields, api\n\n\n"
        "class Foo(models.Model):\n"
        "    _name = 'oma.foo'\n\n"
        "    a = fields.Integer(compute='_compute_a')\n"
        "    b = fields.Integer(compute='_compute_b')\n\n"
        "    @api.depends('name')\n"
        "    def _compute_a(self):\n"
        "        pass\n\n"
        "    @api.depends('a')\n"
        "    def _compute_b(self):\n"
        "        pass\n"
    )
    _validate_compute_field_depends_does_not_include_itself(_gen(models_py))
    print("PASS: never raises when one compute method legitimately depends on a DIFFERENT compute field")


if __name__ == "__main__":
    test_raises_on_the_real_live_self_referential_depends_shape()
    test_never_raises_on_a_legitimate_different_field_dependency()
    test_never_raises_on_empty_depends()
    test_never_raises_when_no_depends_decorator_found_at_all()
    test_never_confuses_a_sibling_methods_own_self_reference()
    print("\nALL COMPUTE-FIELD-DEPENDS-DOES-NOT-INCLUDE-ITSELF TESTS PASSED")
