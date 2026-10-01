"""Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
ticket_workflow_and_logging node, recurring twice in a row: first `action_assign`/
`action_resolve`/`action_start_work`, then `action_bulk_close`): `_validate_no_duplicate_method_
definitions` correctly, deterministically caught this exact mistake every time, but only ever as
a hard rejection -- nothing ever actually FIXED it, so the model burned round after round
re-generating the identical duplicate rather than converging. See specialists/build/specialist.py's
_autofix_dedupe_duplicate_method_definitions for the full incident.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_dedupe_duplicate_method_definitions,
    _validate_no_duplicate_method_definitions,
)


def _make_generated(models_py: str) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py=models_py, security_csv="x",
        security_xml=None, views_xml=None, notes="",
    )


def test_dedupes_a_real_plain_duplicate_method_keeping_the_first():
    generated = _make_generated(
        "from odoo import models\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    def action_bulk_close(self):\n"
        "        self.write({'state': 'closed'})\n\n"
        "    def action_bulk_close(self):\n"
        "        pass\n"
    )
    _autofix_dedupe_duplicate_method_definitions(generated)
    assert generated.models_py.count("def action_bulk_close(self):") == 1, (
        f"expected exactly one surviving definition -- got: {generated.models_py!r}"
    )
    assert "self.write({'state': 'closed'})" in generated.models_py, (
        "the FIRST occurrence's own content must survive, not the second's"
    )
    _validate_no_duplicate_method_definitions(generated)  # must now pass cleanly
    print("PASS: a real plain duplicate method is deduped, keeping the first occurrence's content")


def test_dedupes_a_decorated_duplicate_method_keeping_its_own_decorator():
    generated = _make_generated(
        "from odoo import models, api\n\n"
        "class ProjectProject(models.Model):\n"
        "    _inherit = 'project.project'\n\n"
        "    @api.depends('service_ticket_ids.state')\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n\n"
        "    def _compute_ticket_counts(self):\n"
        "        pass\n"
    )
    _autofix_dedupe_duplicate_method_definitions(generated)
    assert generated.models_py.count("def _compute_ticket_counts(self):") == 1
    assert "@api.depends('service_ticket_ids.state')" in generated.models_py, (
        "the first occurrence's own preceding decorator must be preserved, not stripped"
    )
    _validate_no_duplicate_method_definitions(generated)
    print("PASS: a decorated duplicate method keeps its own decorator when deduped")


def test_never_touches_the_same_method_name_in_two_different_classes():
    generated = _make_generated(
        "from odoo import models\n\n"
        "class Equipment(models.Model):\n"
        "    _name = 'oma.equipment'\n\n"
        "    @api.model\n"
        "    def create(self, vals):\n"
        "        return super().create(vals)\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        return super().create(vals_list)\n"
    )
    original = generated.models_py
    _autofix_dedupe_duplicate_method_definitions(generated)
    assert generated.models_py == original, (
        "the SAME method name declared once each in TWO DIFFERENT classes is completely "
        "legitimate and must never be touched"
    )
    print("PASS: never touches the same method name declared once each in two different classes")


def test_is_a_noop_when_nothing_is_actually_duplicated():
    generated = _make_generated(
        "from odoo import models\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    def action_bulk_close(self):\n"
        "        pass\n\n"
        "    def action_assign(self):\n"
        "        pass\n"
    )
    original = generated.models_py
    _autofix_dedupe_duplicate_method_definitions(generated)
    assert generated.models_py == original
    print("PASS: a no-op when nothing is genuinely duplicated")


def test_is_a_noop_when_models_py_is_empty():
    generated = _make_generated("")
    _autofix_dedupe_duplicate_method_definitions(generated)  # must not raise
    assert generated.models_py == ""
    print("PASS: a no-op when models_py is genuinely empty")


def test_dedupes_three_or_more_occurrences_of_the_same_method():
    generated = _make_generated(
        "from odoo import models\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    def action_assign(self):\n"
        "        return 'first'\n\n"
        "    def action_assign(self):\n"
        "        return 'second'\n\n"
        "    def action_assign(self):\n"
        "        return 'third'\n"
    )
    _autofix_dedupe_duplicate_method_definitions(generated)
    assert generated.models_py.count("def action_assign(self):") == 1
    assert "return 'first'" in generated.models_py
    assert "return 'second'" not in generated.models_py
    assert "return 'third'" not in generated.models_py
    _validate_no_duplicate_method_definitions(generated)
    print("PASS: three-or-more duplicate occurrences all collapse to just the first")


if __name__ == "__main__":
    test_dedupes_a_real_plain_duplicate_method_keeping_the_first()
    test_dedupes_a_decorated_duplicate_method_keeping_its_own_decorator()
    test_never_touches_the_same_method_name_in_two_different_classes()
    test_is_a_noop_when_nothing_is_actually_duplicated()
    test_is_a_noop_when_models_py_is_empty()
    test_dedupes_three_or_more_occurrences_of_the_same_method()
    print("\nALL DEDUPE-DUPLICATE-METHOD-DEFINITIONS TESTS PASSED")
