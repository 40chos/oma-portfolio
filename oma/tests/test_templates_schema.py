"""Phase 20 (§24.5/§24.12.4) tests: Template's own validation invariant
-- Level 0 templates must never carry composed_from, Level >= 1
templates must always carry it (§24.12.2's "a Level-2 template is never
practiced from scratch" rule, enforced structurally here, not just by
convention).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from templates.schema import (
    ApplicabilitySignature,
    CodeExample,
    Template,
    TemplateLevel,
    TemplateStatus,
)


def _sig():
    return ApplicabilitySignature(task_shape="add_field_to_model", odoo_module_area="res.partner")


def test_level0_template_rejects_composed_from():
    with pytest.raises(ValueError, match="cannot themselves be composed"):
        Template(
            template_id="t1", version=1, created_from="hand_authored",
            level=TemplateLevel.ATOMIC_SKILL, applicability_signature=_sig(),
            constraint_template=["x"], composed_from=["other_template"],
        )
    print("PASS: Level 0 template with composed_from set raises")


def test_level1_template_requires_composed_from():
    with pytest.raises(ValueError, match="no composed_from"):
        Template(
            template_id="t2", version=1, created_from="hand_authored",
            level=TemplateLevel.PIPELINE, applicability_signature=_sig(),
            constraint_template=["x"], composed_from=[],
        )
    print("PASS: Level 1 template with empty composed_from raises")


def test_level0_template_constructs_cleanly():
    t = Template(
        template_id="t3", version=1, created_from="task-123",
        level=TemplateLevel.ATOMIC_SKILL, applicability_signature=_sig(),
        constraint_template=["module_installs_cleanly"],
    )
    assert t.status == TemplateStatus.NEEDS_REVIEW
    assert t.override_rate == 0.0
    print("PASS: Level 0 template constructs with defaults (needs_review, 0.0 override_rate)")


def test_level1_template_constructs_cleanly_with_composed_from():
    t = Template(
        template_id="t4", version=1, created_from="task-456",
        level=TemplateLevel.PIPELINE, applicability_signature=_sig(),
        constraint_template=["module_installs_cleanly"], composed_from=["t3"],
    )
    assert t.composed_from == ["t3"]
    print("PASS: Level 1 template constructs cleanly with composed_from set")


def test_override_rate_reflects_fallback_overrides():
    t = Template(
        template_id="t5", version=1, created_from="task-789",
        level=TemplateLevel.ATOMIC_SKILL, applicability_signature=_sig(),
        constraint_template=["x"], success_count=3, fallback_override_count=1,
    )
    assert t.override_rate == 0.25
    print(f"PASS: override_rate computed correctly: {t.override_rate}")


def test_code_examples_hold_real_multiple_variations():
    examples = [
        CodeExample(field_name="secondary_email", field_type="Char", source_task_id="task-1",
                    models_py="class X: pass", views_xml="<odoo/>"),
        CodeExample(field_name="account_manager_id", field_type="Many2one", source_task_id="task-2",
                    models_py="class Y: pass", views_xml=None),
    ]
    t = Template(
        template_id="t6", version=1, created_from="task-1",
        level=TemplateLevel.ATOMIC_SKILL, applicability_signature=_sig(),
        constraint_template=["x"], code_examples=examples,
    )
    assert len(t.code_examples) == 2
    assert {e.field_type for e in t.code_examples} == {"Char", "Many2one"}
    assert t.code_examples[1].views_xml is None
    print("PASS: Template keeps multiple distinct real code examples, not just one")


if __name__ == "__main__":
    test_level0_template_rejects_composed_from()
    test_level1_template_requires_composed_from()
    test_level0_template_constructs_cleanly()
    test_level1_template_constructs_cleanly_with_composed_from()
    test_override_rate_reflects_fallback_overrides()
    test_code_examples_hold_real_multiple_variations()
    print("\nALL TEMPLATE SCHEMA TESTS PASSED")
