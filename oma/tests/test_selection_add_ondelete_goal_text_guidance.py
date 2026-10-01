"""Real, general bug found live (2026-08-08, site_50 benchmark task 011, real task_id
45ad0f11-d321-4c8c-ac11-f2e7a67fae39): "I need an intermediate state called 'Paid' between
Invoiced and Done" -- Build generated `state = fields.Selection(selection_add=[('paid', 'Paid')])`
extending an existing, required Selection field with NO `ondelete=` kwarg, hitting Odoo's own
deterministic registry-build-time rejection ("required selection fields must define an ondelete
policy") -- and repeated the IDENTICAL mistake on round 2, never including it. Fixed by appending
explicit, general guidance to goal_text whenever the goal describes adding a new workflow state/
stage/status value. Zero LLM/GPU calls -- _execute_contract() mocked, matching this file's
established sibling (test_button_method_goal_text_clarifies_two_pieces.py).
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import _run_constraint_labels_from


def _run_with_goal(goal: str) -> list[str]:
    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=goal, inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
    )
    labels = ["only_constraint"]
    satisfied = {label: "pending" for label in labels}
    seen_goal_texts = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_goal_texts.append(sub_contract.goal)
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    asyncio.run(run())
    return seen_goal_texts


def test_goal_text_gets_ondelete_guidance_for_a_new_intermediate_state():
    goal = (
        "The meerwerk record currently goes from Invoiced directly to Done. I need an "
        "intermediate state called 'Paid' between Invoiced and Done, so we can mark when the "
        "invoice is actually paid."
    )
    for goal_text in _run_with_goal(goal):
        assert "ondelete=" in goal_text, (
            f"expected the general selection_add/ondelete guidance in goal_text -- got:\n{goal_text}"
        )
        assert "required selection fields must define an ondelete policy" in goal_text
    print("PASS: a goal describing a new intermediate workflow state gets the ondelete= guidance")


def test_goal_text_gets_ondelete_guidance_for_a_new_stage_phrasing():
    goal = "Add a new stage called 'Review' to the ticket workflow."
    for goal_text in _run_with_goal(goal):
        assert "ondelete=" in goal_text
    print("PASS: 'new stage' phrasing also triggers the guidance")


def test_no_spurious_ondelete_guidance_for_an_unrelated_goal():
    goal = "Add a plain text field 'notes' to project.project."
    for goal_text in _run_with_goal(goal):
        assert "ondelete=" not in goal_text or "fields.Selection" not in goal_text, (
            "a goal with no new-state/selection-add shape must never get this guidance appended"
        )
        assert "registry validation HARD-REJECTS" not in goal_text
    print("PASS: no spurious ondelete= guidance for a goal unrelated to workflow states")


if __name__ == "__main__":
    test_goal_text_gets_ondelete_guidance_for_a_new_intermediate_state()
    test_goal_text_gets_ondelete_guidance_for_a_new_stage_phrasing()
    test_no_spurious_ondelete_guidance_for_an_unrelated_goal()
    print("\nALL SELECTION-ADD ONDELETE GOAL-TEXT GUIDANCE TESTS PASSED")
