"""Real, general bug found live (2026-08-07, task039_v11, real task_id
7898f114-78ed-44a8-8fe8-143cead3f631, HUMAN_DECISION deep-push): the existing "NOT yet in
scope" goal_text construction in _run_constraint_labels_from() told a BUILDER not to add the
not-yet-in-scope work, but never told a REVIEWER (Code-Review, Testing/QA, or any other
specialist judging the diff against this same goal_text) that omitting that work is the
required, correct outcome for this round -- not a defect. Confirmed live: Code-Review's own
stated reasoning was "The provided code correctly implements only the storage fields, which
satisfies the 'NOT-yet-in-scope' constraint but fails the initial requirement" -- it read the
original goal's full first paragraph as a "requirement" that the (intentional, correct)
omission still violated, declared the goal "self-contradictory", and blocked a round whose own
code was scope-correct by Code-Review's own admission. This test confirms goal_text now
explicitly tells any reader that the omission is correct and must not be flagged as missing,
incomplete, or contradictory. Zero LLM/GPU calls -- _execute_contract() mocked, inspects the
real sub_contract.goal text it's called with.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import _run_constraint_labels_from


def _make_decomposable_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=(
            "Add credential-storage fields on res.users: external_service_token (Char), "
            "external_service_token_expiry (Datetime), a computed "
            "is_external_service_token_expired (Boolean), and a Mark Refreshed button "
            "(action_mark_refreshed)."
        ),
        inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
    )


def test_goal_text_tells_reviewer_that_omitting_not_yet_in_scope_work_is_correct():
    contract = _make_decomposable_contract()
    labels = ["credential_storage_fields", "mark_refreshed_action", "token_expired_computed"]
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

    round1_goal = seen_goal_texts[0]
    assert "NOT yet in scope for this round" in round1_goal, (
        "expected the existing builder-facing NOT-yet-in-scope sentence to still be present"
    )
    assert "is CORRECT and COMPLETE for this round's own scope" in round1_goal, (
        f"expected the new reviewer-facing sentence confirming omission is correct, not a "
        f"shortfall, to be present in round 1's goal_text -- got:\n{round1_goal}"
    )
    assert "must NOT flag the absence of" in round1_goal, (
        f"expected the new sentence to explicitly instruct reviewers/verifiers not to flag the "
        f"omission as missing/incomplete/contradictory -- got:\n{round1_goal}"
    )
    print("PASS: goal_text explicitly tells any reviewer/verifier that omitting not-yet-in-scope "
          "work is correct and complete for this round, not a defect")


def test_no_reviewer_sentence_when_this_is_the_final_constraint():
    contract = _make_decomposable_contract()
    labels = ["only_constraint"]
    satisfied = {"only_constraint": "pending"}
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
    assert "is CORRECT and COMPLETE for this round's own scope" not in seen_goal_texts[0], (
        "a single-constraint round has no not_yet_labels, so the reviewer-facing sentence must "
        "not be appended (nothing is being intentionally omitted)"
    )
    print("PASS: no spurious reviewer-facing sentence when there are no not-yet-in-scope labels")


if __name__ == "__main__":
    test_goal_text_tells_reviewer_that_omitting_not_yet_in_scope_work_is_correct()
    test_no_reviewer_sentence_when_this_is_the_final_constraint()
    print("\nALL NOT-YET-IN-SCOPE REVIEWER-CLARITY TESTS PASSED")
