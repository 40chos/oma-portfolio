"""Phase 20 (§24.4 Component 1) tests: agent_templates CRUD, real
Postgres, self-cleaning -- same convention as test_manager_memory.py's
test_read_project_memory_against_real_postgres().
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from templates.schema import ApplicabilitySignature, CodeExample, ConstraintSlot, Template, TemplateLevel, TemplateStatus
from templates.store import delete_template, get_template, insert_template, list_templates, record_usage, set_status


def _fresh_template(template_id: str, model_area: str = "res.partner.__test__") -> Template:
    return Template(
        template_id=template_id, version=1, created_from="test-fixture",
        level=TemplateLevel.ATOMIC_SKILL,
        applicability_signature=ApplicabilitySignature(
            task_shape="add_field_to_model", odoo_module_area=model_area,
        ),
        constraint_template=["module_installs_cleanly", "field_exists"],
        slots=[ConstraintSlot(name="field_name", example_value="test_field")],
        known_pitfalls=["saw rc=255 once"],
        code_examples=[
            CodeExample(field_name="test_field", field_type="Char", source_task_id="test-fixture-task",
                        models_py="class X(models.Model):\n    _inherit = 'res.partner'\n    test_field = fields.Char()",
                        views_xml="<odoo/>"),
        ],
    )


def test_insert_and_get_round_trip():
    tid = f"test-{uuid.uuid4().hex[:8]}"
    delete_template(tid)  # in case a prior failed run left it
    try:
        t = _fresh_template(tid)
        insert_template(t)
        got = get_template(tid)
        assert got is not None
        assert got.template_id == tid
        assert got.level == TemplateLevel.ATOMIC_SKILL
        assert got.applicability_signature.odoo_module_area == "res.partner.__test__"
        assert got.constraint_template == ["module_installs_cleanly", "field_exists"]
        assert got.slots[0].name == "field_name"
        assert got.known_pitfalls == ["saw rc=255 once"]
        assert len(got.code_examples) == 1
        assert got.code_examples[0].field_type == "Char"
        assert "test_field = fields.Char()" in got.code_examples[0].models_py
        assert got.status == TemplateStatus.NEEDS_REVIEW
        print(f"PASS: insert/get round-trip preserved all fields for {tid}, including code_examples")
    finally:
        delete_template(tid)


def test_list_templates_filters_by_status_and_area():
    tid_a = f"test-{uuid.uuid4().hex[:8]}"
    tid_b = f"test-{uuid.uuid4().hex[:8]}"
    for tid in (tid_a, tid_b):
        delete_template(tid)
    try:
        insert_template(_fresh_template(tid_a, model_area="hr.employee.__test__"))
        insert_template(_fresh_template(tid_b, model_area="hr.employee.__test__"))
        set_status(tid_a, TemplateStatus.ACTIVE)

        active_only = [t for t in list_templates(status="active", odoo_module_area="hr.employee.__test__")
                       if t.template_id in (tid_a, tid_b)]
        assert [t.template_id for t in active_only] == [tid_a]

        both = [t for t in list_templates(odoo_module_area="hr.employee.__test__")
                if t.template_id in (tid_a, tid_b)]
        assert len(both) == 2
        print(f"PASS: list_templates filtered correctly by status and odoo_module_area")
    finally:
        delete_template(tid_a)
        delete_template(tid_b)


def test_record_usage_updates_counters():
    tid = f"test-{uuid.uuid4().hex[:8]}"
    delete_template(tid)
    try:
        insert_template(_fresh_template(tid))
        record_usage(tid, matched_but_overridden=False)
        record_usage(tid, matched_but_overridden=False)
        record_usage(tid, matched_but_overridden=True)
        got = get_template(tid)
        assert got.success_count == 2
        assert got.fallback_override_count == 1
        assert got.last_used_at is not None
        print(f"PASS: record_usage incremented counters correctly (success=2, override=1)")
    finally:
        delete_template(tid)


if __name__ == "__main__":
    test_insert_and_get_round_trip()
    test_list_templates_filters_by_status_and_area()
    test_record_usage_updates_counters()
    print("\nALL TEMPLATE STORE TESTS PASSED")
