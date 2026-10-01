"""Real, general bug found live (2026-08-07, task039, HUMAN_DECISION deep-push, real task_id
3c317ee7-9934-4b71-a3de-46f534bade49): confirmed via direct redis inspection that Build's own real
prompt for the round covering 'action_mark_refreshed' DID have the real, live, correct base view
xmlid available (`<current_views>` listed `base.view_users_form (form)`) -- this was never a
missing-context/grounding gap. The real root cause: the ORIGINAL goal text's own phrasing -- "a
Mark Refreshed button (action_mark_refreshed) that just updates ..." -- reads as ONE combined
concept (a method, informally described as "a button"), not two SEPARATE required deliverables (a
real method AND a real <button> view element referencing it). A side-by-side Sonnet-5 diagnostic
escalation on the exact same goal succeeded on the first attempt specifically because its own
prompt spelled out, in an explicit sentence, that a method with no way to trigger it from the UI
does not satisfy the goal -- local's own goal_text never said this. This test confirms goal_text
now explicitly separates the two required pieces whenever a goal names "a button (method_name)".
Zero LLM/GPU calls -- _execute_contract() mocked, inspects the real sub_contract it's called with.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import _run_constraint_labels_from


def _make_contract_with_button_goal() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=(
            "Add credential-storage fields on res.users: external_service_token (Char), "
            "external_service_token_expiry (Datetime), a computed "
            "is_external_service_token_expired (Boolean), and a Mark Refreshed button "
            "(action_mark_refreshed) that just updates external_service_token_expiry to a new "
            "future value."
        ),
        inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
    )


def test_goal_text_explicitly_separates_method_and_view_element_for_a_button_deliverable():
    contract = _make_contract_with_button_goal()
    labels = ["credential_storage_fields", "mark_refreshed_action"]
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

    for goal_text in seen_goal_texts:
        assert "'action_mark_refreshed'" in goal_text, (
            f"expected the detected button-method name in every round's own goal_text -- got:\n{goal_text}"
        )
        assert "TWO separate, both-required pieces" in goal_text, (
            f"expected the general button/method separation clarification in every round's own "
            f"goal_text (Build reads this on every round of this task, not just the round the "
            f"button constraint belongs to) -- got:\n{goal_text}"
        )
        assert "does NOT satisfy this requirement" in goal_text
    print("PASS: every round's own goal_text explicitly separates the method-vs-view-element "
          "pieces of a 'button (method_name)' deliverable")


def test_no_spurious_append_when_goal_has_no_button_deliverable():
    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A; add field B.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
    )
    labels = ["field_a", "field_b"]
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
    for goal_text in seen_goal_texts:
        assert "TWO separate, both-required pieces" not in goal_text, (
            "a goal with no button-shaped deliverable must never get the clarification appended"
        )
    print("PASS: no spurious button/method clarification for a goal with no button deliverable")


if __name__ == "__main__":
    test_goal_text_explicitly_separates_method_and_view_element_for_a_button_deliverable()
    test_no_spurious_append_when_goal_has_no_button_deliverable()
    print("\nALL BUTTON-METHOD GOAL-TEXT CLARITY TESTS PASSED")
