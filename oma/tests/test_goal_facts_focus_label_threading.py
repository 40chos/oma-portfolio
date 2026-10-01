"""P12 Tier B/C item 30 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 28): tests for extract_goal_field_facts()'s new current_constraint_label threading.
Real, confirmed gap: for a decomposed sub-contract, `goal` embeds the FULL original multi-field
goal, but the extraction prompt never told the model which field to focus on -- relying
entirely on a downstream name-match guard as the only defense against a misapplied extraction.
Mocks call_structured() directly, zero live LLM/GPU calls.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

import contracts.goal_facts as goal_facts_module
from contracts.goal_facts import GoalFieldFacts, extract_goal_field_facts


def test_no_focus_label_produces_the_original_prompt_shape():
    captured_prompts = []

    async def fake_call_structured(**kwargs):
        captured_prompts.append(kwargs["prompt"])
        return GoalFieldFacts()

    async def run():
        with patch.object(goal_facts_module, "call_structured", new=fake_call_structured):
            return await extract_goal_field_facts("Add field A; add field B.", client=None, model="x")

    asyncio.run(run())
    assert captured_prompts
    assert "This round's own current focus" not in captured_prompts[0]
    print("PASS: omitting current_constraint_label reproduces the exact original prompt shape, no focus note added")


def test_focus_label_is_threaded_into_the_real_prompt():
    captured_prompts = []

    async def fake_call_structured(**kwargs):
        captured_prompts.append(kwargs["prompt"])
        return GoalFieldFacts()

    async def run():
        with patch.object(goal_facts_module, "call_structured", new=fake_call_structured):
            return await extract_goal_field_facts(
                "Add field A; add field B.", client=None, model="x",
                current_constraint_label="add_field_a",
            )

    asyncio.run(run())
    assert captured_prompts
    assert "This round's own current focus is ONLY: 'add_field_a'" in captured_prompts[0]
    print("PASS: a real current_constraint_label is threaded into the real prompt text explicitly")


def test_extraction_result_is_unaffected_by_the_focus_note_wiring_itself():
    fake_facts = GoalFieldFacts(field_name="add_field_a", field_type="Char")

    async def fake_call_structured(**kwargs):
        return fake_facts

    async def run():
        with patch.object(goal_facts_module, "call_structured", new=fake_call_structured):
            return await extract_goal_field_facts(
                "Add field A; add field B.", client=None, model="x",
                current_constraint_label="add_field_a",
            )

    result = asyncio.run(run())
    assert result == fake_facts
    print("PASS: the real extraction result still passes through unchanged, this is purely a prompt-text change")


if __name__ == "__main__":
    test_no_focus_label_produces_the_original_prompt_shape()
    test_focus_label_is_threaded_into_the_real_prompt()
    test_extraction_result_is_unaffected_by_the_focus_note_wiring_itself()
    print("\nALL GOAL-FACTS FOCUS-LABEL THREADING TESTS PASSED")
