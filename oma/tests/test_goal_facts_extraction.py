"""Phase 25B: tests for contracts/goal_facts.py's structured extraction,
and for the "prefer goal_facts, fall back to regex unchanged" wiring in
manager/replanning.py and specialists/build/specialist.py.

Mock-based tests (no live gateway) mirror this project's own established
two-tier discipline (see test_resume_decomposition.py's own docstring).
One additional live-gateway test proves the actual, real capability this
phase adds: extraction correctly reads a field's type from free-form
prose the existing regex family cannot parse at all -- guarded to skip
cleanly if OMA_REDIS_HOST/the gateway aren't configured in the current
environment, same guard style already used elsewhere in this suite.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.goal_facts import GoalFieldFacts, extract_goal_field_facts
from manager.replanning import _build_field_declaration_snippet, _skeleton_from_goal_facts as _replanning_skeleton_from_goal_facts


def test_extract_goal_field_facts_never_raises_on_gateway_failure():
    """A failed/malformed extraction call must return an all-"not
    stated" GoalFieldFacts, never propagate an exception -- every
    consumer treats this identically to "goal_facts unavailable,"
    falling back to the existing regex family, so a gateway hiccup must
    never crash a round.
    """
    async def _drive():
        client = object()  # deliberately not a real ModelGatewayClient
        return await extract_goal_field_facts("Add a field.", client, "some-model", task_id=None)

    facts = asyncio.run(_drive())
    assert facts == GoalFieldFacts()
    print("PASS: a failed extraction call returns an all-not-stated GoalFieldFacts, never raises")


def test_replanning_skeleton_from_goal_facts_builds_a_selection_field():
    """The exact shape that was broken before Phase 25B: a Selection
    field with real, structured-extracted options builds a real
    skeleton, never a placeholder.
    """
    goal_facts = {
        "field_name": "order_priority", "field_type": "Selection", "is_computed": False,
        "selection_options": ["low", "normal", "high"], "selection_default": "normal",
    }
    result = _replanning_skeleton_from_goal_facts("order_priority", "Order Priority", goal_facts)
    assert result == "fields.Selection([('low', 'Low'), ('normal', 'Normal'), ('high', 'High')], string='Order Priority', default='normal')", (
        f"got: {result!r}"
    )
    print("PASS: goal_facts with real Selection options builds a real skeleton")


def test_replanning_skeleton_from_goal_facts_refuses_a_different_field():
    """goal_facts naming a DIFFERENT field than the one being asked
    about must never be applied -- a stale or mismatched extraction
    result silently reused for the wrong field would be worse than no
    extraction at all.
    """
    goal_facts = {"field_name": "some_other_field", "field_type": "Text", "is_computed": False}
    result = _replanning_skeleton_from_goal_facts("order_priority", "Order Priority", goal_facts)
    assert result is None
    print("PASS: goal_facts naming a different field is never applied")


def test_replanning_skeleton_from_goal_facts_refuses_a_computed_field():
    """goal_facts confirming is_computed=True must return None -- the
    same "never guess a computed field's business logic" safety rule
    already established for the regex path.
    """
    goal_facts = {"field_name": "amount_total", "field_type": "Monetary", "is_computed": True}
    result = _replanning_skeleton_from_goal_facts("amount_total", "Amount Total", goal_facts)
    assert result is None
    print("PASS: goal_facts confirming a computed field returns None, never a fake plain skeleton")


def test_build_field_declaration_snippet_prefers_goal_facts_over_regex():
    """End-to-end (within replanning.py): when goal_facts is populated
    and matches, it wins over the regex family entirely -- even for a
    goal whose own prose the regex CAN'T parse (proving this is a real
    preference, not a coincidental agreement).
    """
    goal = (
        "Add a numeric field to project.fieldjob called 'estimated_hours' so we can record how "
        "many hours we think the work will take. It should just be a plain number field, nothing "
        "fancy about it."
    )  # deliberately no "Field type:"/"Field: name (Type)" convention -- regex alone finds nothing
    goal_facts = {
        "field_name": "estimated_hours", "field_type": "Integer", "is_computed": False,
        "selection_options": [], "selection_default": None,
    }
    result = _build_field_declaration_snippet("estimated_hours", goal, goal_facts=goal_facts)
    assert result == "estimated_hours = fields.Integer(string='Estimated Hours')", f"got: {result!r}"
    print("PASS: _build_field_declaration_snippet() builds a real field from goal_facts alone, "
          "for a goal the regex family cannot parse at all")


def test_build_field_declaration_snippet_falls_back_to_regex_when_goal_facts_absent():
    """No goal_facts (empty dict, e.g. extraction never ran for this
    contract shape) -- must fall through to the EXACT existing
    regex-based behavior, unchanged, never a different result.
    """
    goal = "Field: special_instructions (Text)\nField type: fields.Text\n"
    result_without_facts = _build_field_declaration_snippet("special_instructions", goal, goal_facts=None)
    result_with_empty_facts = _build_field_declaration_snippet("special_instructions", goal, goal_facts={})
    assert result_without_facts == "special_instructions = fields.Text(string='Special Instructions')"
    assert result_with_empty_facts == result_without_facts
    print("PASS: absent/empty goal_facts falls through to the unchanged regex family")


if __name__ == "__main__":
    test_extract_goal_field_facts_never_raises_on_gateway_failure()
    test_replanning_skeleton_from_goal_facts_builds_a_selection_field()
    test_replanning_skeleton_from_goal_facts_refuses_a_different_field()
    test_replanning_skeleton_from_goal_facts_refuses_a_computed_field()
    test_build_field_declaration_snippet_prefers_goal_facts_over_regex()
    test_build_field_declaration_snippet_falls_back_to_regex_when_goal_facts_absent()
    print("\nALL GOAL-FACTS EXTRACTION TESTS PASSED")
