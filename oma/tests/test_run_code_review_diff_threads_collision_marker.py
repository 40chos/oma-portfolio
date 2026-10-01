"""Real, general bug found live (2026-08-07, task041, real task_id
11e44739-a0b4-4e61-8e8f-8eb07490af13, HUMAN_DECISION deep-push): Build's own collision-autofix
(specialists/build/specialist.py) correctly strips a redundant new-field declaration when the
field already exists as a REAL, LIVE, FUNCTIONING field on the actual target model, and records
it via `_ALREADY_SATISFIED_BY_COLLISION_MARKER` in `generated.notes` -- manager/tools.py's own
`_extract_collision_confirmed_field_names()` already threads this to testing_qa's reproduction
extraction (fixed earlier for task 004), but it was NEVER threaded to Code-Review, whose own diff
review has no way to know a field's absence from models.py is a CORRECT, deliberate omission
rather than a shortfall. Confirmed live: Code-Review flagged "project_count is completely
missing" as blocking, 4 consecutive relaunches (v6-v9), even though the field already exists and
already works (owned by `mis_base_extend`, a real `compute='_compute_project_count'` method
confirmed via direct SSH source read -- not a dead field). This test confirms
run_code_review_diff() now appends a clarifying sentence to the goal text Code-Review reads,
naming exactly which field(s) are already satisfied by a real collision and instructing it not to
flag their absence. Zero LLM/GPU calls -- the code_review specialist's own run() is mocked.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import (
    AutonomyTier, CapabilityClass, SpecialistOutput, SpecialistType, TaskContract,
)
from manager.tools import run_code_review_diff


def _make_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add a Customer Overview smart button with a project_count field on res.partner.",
        inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10, constraint_status={},
    )


def test_collision_confirmed_fields_are_appended_to_the_goal_code_review_reads():
    contract = _make_contract()
    seen_goals = []

    class _FakeSpecialist:
        async def run(self, review_contract):
            seen_goals.append(review_contract.goal)
            return SpecialistOutput(
                task_id=review_contract.task_id, specialist_type=SpecialistType.code_review,
                summary="ok", detail={"findings": []}, claims_complete=True,
            )

    async def run():
        with patch("manager.tools.registry.get", return_value=_FakeSpecialist()):
            return await run_code_review_diff(
                contract, "some_module",
                self_report_uncertain="blah\nALREADY_SATISFIED_BY_REAL_TARGET_COLLISION: ['project_count']",
            )

    asyncio.run(run())
    assert len(seen_goals) == 1
    goal = seen_goals[0]
    assert "'project_count'" in goal, f"expected the collision-confirmed field name in goal text, got:\n{goal}"
    assert "already exist as REAL, LIVE, FUNCTIONING" in goal
    assert "Do NOT flag" in goal
    print("PASS: collision-confirmed field list is appended to the goal text Code-Review reads")


def test_no_spurious_append_when_no_collision_marker_present():
    contract = _make_contract()
    seen_goals = []

    class _FakeSpecialist:
        async def run(self, review_contract):
            seen_goals.append(review_contract.goal)
            return SpecialistOutput(
                task_id=review_contract.task_id, specialist_type=SpecialistType.code_review,
                summary="ok", detail={"findings": []}, claims_complete=True,
            )

    async def run():
        with patch("manager.tools.registry.get", return_value=_FakeSpecialist()):
            return await run_code_review_diff(contract, "some_module", self_report_uncertain=None)

    asyncio.run(run())
    assert seen_goals == [contract.goal], (
        "goal text must be left completely unchanged when there is no real collision marker "
        f"to report -- got:\n{seen_goals[0]}"
    )
    print("PASS: no spurious goal-text mutation when self_report_uncertain has no collision marker")


if __name__ == "__main__":
    test_collision_confirmed_fields_are_appended_to_the_goal_code_review_reads()
    test_no_spurious_append_when_no_collision_marker_present()
    print("\nALL RUN-CODE-REVIEW-DIFF-COLLISION-MARKER-THREADING TESTS PASSED")
