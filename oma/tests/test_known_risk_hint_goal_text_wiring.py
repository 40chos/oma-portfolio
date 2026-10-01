"""P13 item 6: confirms _run_constraint_labels_from() actually folds contract.known_risk_hint
into each sub_contract's rendered goal_text when set, and omits it entirely when unset (never
inventing a hint). Zero LLM/GPU calls -- _execute_contract() mocked, inspects the real sub_contract
goal it's called with, same pattern as tests/test_goal_facts_cleared_per_sub_contract.py (P12 item 21).
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import _run_constraint_labels_from


def _make_contract(known_risk_hint) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={}, known_risk_hint=known_risk_hint,
    )


def _run_one_round(contract):
    labels = ["constraint_a"]
    satisfied = {"constraint_a": "pending"}
    seen_goals = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_goals.append(sub_contract.goal)
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
    return seen_goals[0]


def test_a_real_known_risk_hint_is_folded_into_goal_text():
    contract = _make_contract("This module has a real prior-failure history: cron self-reference bug.")
    goal = _run_one_round(contract)
    assert "This module has a real prior-failure history: cron self-reference bug." in goal
    print("PASS: a real known_risk_hint is folded into the rendered goal_text")


def test_no_known_risk_hint_adds_nothing():
    contract = _make_contract(None)
    goal = _run_one_round(contract)
    assert "prior-failure" not in goal
    print("PASS: an unset known_risk_hint adds no hint text, never invents one")


if __name__ == "__main__":
    test_a_real_known_risk_hint_is_folded_into_goal_text()
    test_no_known_risk_hint_adds_nothing()
    print("\nALL KNOWN-RISK-HINT GOAL-TEXT WIRING TESTS PASSED")
