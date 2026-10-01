"""Real regression test for the decomposition constraint-cap bug found
live (2026-07-13): decompose_into_constraints() capped its own output
at 8 labels -- both via an explicit prompt instruction ("Return at
most 8") and a defensive list slice -- silently dropping any real,
independent requirement beyond the 8th. Confirmed live: Operator's actual
10+-requirement Odoo Service Management goal produced exactly 8
labels, covering only the model/field-level requirements; multi-
employee independent assignment and the entire separate service-visits
feature were never attempted at all, with no error or escalation.

This test locks in the raised cap and confirms the prompt text embeds
the SAME constant the slice enforces (so they can never drift apart
again), without needing a real model gateway call.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from manager.replanning import _MAX_DECOMPOSED_CONSTRAINTS, _ConstraintList, decompose_into_constraints


def test_max_decomposed_constraints_raised_above_the_bug_threshold():
    assert _MAX_DECOMPOSED_CONSTRAINTS >= 16, (
        "the cap that silently dropped 2 of Operator's real requirements (employee assignment, "
        "service visits) from an 8-model-field-plus-2-feature goal must stay well above 8"
    )
    print(f"PASS: _MAX_DECOMPOSED_CONSTRAINTS is {_MAX_DECOMPOSED_CONSTRAINTS}, above the bug threshold")


def test_decompose_into_constraints_does_not_truncate_a_realistic_long_goal():
    """A goal with more independent requirements than the OLD cap (8)
    must not lose any of them -- mocks call_structured to return exactly
    what a real decomposition of Operator's goal looks like (10 labels),
    and asserts none are silently dropped.
    """
    ten_labels = [
        "service_issue_project_link", "project_multiple_issues", "issue_title_field",
        "issue_status_field", "issue_priority_field", "issue_description_field",
        "issue_attachments", "issue_product_link", "employee_assignment", "service_visits",
    ]

    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(return_value=_ConstraintList(constraints=ten_labels)),
        ):
            return await decompose_into_constraints(
                "Develop a custom Odoo Service Management module...", client=None, model="x",
            )

    labels = asyncio.run(run())
    assert labels == ten_labels, (
        f"a 10-requirement goal must keep all 10 labels now that the cap is above 8: got {labels}"
    )
    print("PASS: decompose_into_constraints() no longer truncates a realistic 10-requirement goal")


def test_decompose_prompt_warns_against_over_granularity_and_generic_labels():
    """Real, second bug found live (2026-07-13), minutes after raising
    the cap above: with the cap gone, decompose_into_constraints() over-
    corrected -- it split one cohesive new model's own display fields
    (customer, project, date, completion status on a service visit)
    into FOUR separate constraints (visit_customer_display,
    visit_project_display, visit_datetime_display,
    visit_completion_status) instead of one, and invented two generic,
    non-field-level labels ('custom_module_development',
    'minimal_custom_code') that restate build guidance rather than
    naming anything a reproduction check could ever verify -- these
    would have gotten stuck forever since check_field_exists_on_model()
    has nothing concrete to check them against. Fixed by adding two
    explicit anti-patterns to the prompt. This test locks in that both
    corrective instructions are actually present in the prompt sent to
    the model (asserted via a captured `prompt` kwarg), so a future
    edit can't silently drop them.
    """
    captured = {}

    async def fake_call_structured(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return _ConstraintList(constraints=["one_constraint"])

    async def run():
        with patch("manager.replanning.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            return await decompose_into_constraints("some goal", client=None, model="x")

    asyncio.run(run())

    prompt = captured["prompt"]
    assert "display" in prompt and "ONE requirement" in prompt, (
        "prompt must warn against splitting one new model's own fields into multiple constraints"
    )
    assert "keep custom code minimal" in prompt or "guidance, not" in prompt, (
        "prompt must warn against inventing non-field-level generic labels"
    )
    print("PASS: decompose_into_constraints() prompt warns against over-granular field-splitting "
          "and generic non-verifiable labels")


def test_decompose_into_constraints_collapses_llm_over_split_of_a_single_field_goal():
    """Real, severe bug found live (2026-07-25, task 001's 10th fresh
    submission): despite the exact anti-pattern warning locked in by
    test_decompose_prompt_warns_against_over_granularity_and_generic_labels()
    above, the LLM still split a genuinely single-field task ("add a
    text field to crm.lead") into 3 fake constraints (confirmed live via
    Gitea commit history -- separate sequential sub-contract commits
    each touching only one file: models.py, then views.xml, then the
    manifest). Consequence, confirmed live: manager/loop.py routes any
    3+-label result through `_run_decomposed_task()`, whose own
    `constraint_status` always carries every label at once for every
    sub-contract -- which made specialists/build/specialist.py's
    `_validate_goal_named_field_is_declared()` (`len(constraint_status)
    >= 2: return`) permanently skip validation, silently letting the
    one real field go undeclared for 4+ rounds until it finally
    surfaced as a real sandbox install crash ("Field 'special_
    instructions' does not exist in model 'crm.lead'").

    Fixed at the source: a goal whose own metadata names at most ONE
    real field (this project's own `Field:`/`Field name:` convention)
    cannot legitimately decompose into 3+ independent, separately-
    verifiable requirements -- an LLM result that size is itself the
    over-splitting signal, collapsed back to derive_constraint_labels()'s
    own deterministic single-label result instead of trusted as-is.
    """
    goal = (
        "I want to see a text field on the lead form called 'Special instructions' where the "
        "salesperson can type a short note about what the customer said during the first call. "
        "Only show it on the form, not the list.\n\n"
        "Module: mis_base_extend\n"
        "Model: crm.lead\n"
        "Field: special_instructions (Text)\n"
        "View: Inherit crm.lead.all.activities.form.view, add after description field inside Notes tab\n"
        "Security: visible to all internal users"
    )
    over_split_labels = ["add_special_instructions_field", "update_crm_lead_form_view", "add_security_access"]

    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(return_value=_ConstraintList(constraints=over_split_labels)),
        ):
            return await decompose_into_constraints(goal, client=None, model="x")

    labels = asyncio.run(run())
    assert labels == ["i_want_see"], (
        f"a real single-field goal must collapse back to derive_constraint_labels()'s own single "
        f"label when the LLM over-splits it into 3+, never keep the over-split result: got {labels}"
    )
    print("PASS: an LLM over-split of a genuinely single-field goal collapses back to one label")


def test_decompose_into_constraints_keeps_a_genuine_multi_field_split():
    """The collapse above must never fire for a goal that legitimately
    names multiple distinct fields (2+ `Field:` metadata lines) -- a
    real multi-field/multi-model task keeps whatever real decomposition
    the LLM produced, unaffected by this fix.
    """
    goal = (
        "Add two fields.\n\n"
        "Field: first_field (Text)\n"
        "Field: second_field (Boolean)\n"
        "Model: crm.lead"
    )
    real_labels = ["first_field_addition", "second_field_addition", "shared_view_wiring"]

    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(return_value=_ConstraintList(constraints=real_labels)),
        ):
            return await decompose_into_constraints(goal, client=None, model="x")

    labels = asyncio.run(run())
    assert labels == real_labels, (
        f"a goal naming 2+ real fields must keep its real LLM decomposition unchanged: got {labels}"
    )
    print("PASS: a genuine multi-field goal's real decomposition is never collapsed")


if __name__ == "__main__":
    test_max_decomposed_constraints_raised_above_the_bug_threshold()
    test_decompose_into_constraints_does_not_truncate_a_realistic_long_goal()
    test_decompose_prompt_warns_against_over_granularity_and_generic_labels()
    test_decompose_into_constraints_collapses_llm_over_split_of_a_single_field_goal()
    test_decompose_into_constraints_keeps_a_genuine_multi_field_split()
    print("\nALL DECOMPOSE-CONSTRAINT-CAP TESTS PASSED")
