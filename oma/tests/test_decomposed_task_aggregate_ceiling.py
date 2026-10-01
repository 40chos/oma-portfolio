"""P12 Tier A item 20 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 21): tests for _run_constraint_labels_from()'s new aggregate round/wall-clock
ceiling -- the sibling fix to run_plan()'s own (tests/test_run_plan_aggregate_ceiling.py). Real,
confirmed gap: each sub-contract only ever bounded ITSELF, nothing capped the SUM across many
sub-contracts each individually succeeding. Same mocking pattern as
tests/test_resume_decomposition.py -- _execute_contract() mocked, zero LLM/GPU calls.
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
        goal="Add field A; add field B; add field C.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
    )


def test_stops_before_starting_the_next_constraint_once_aggregate_budget_is_exhausted():
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b", "constraint_c"]
    satisfied = {"constraint_a": "pending", "constraint_b": "pending", "constraint_c": "pending"}
    seen_focuses = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_focuses.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 999, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    assert seen_focuses == ["constraint_a"], (
        f"only constraint_a should ever have started -- its own 999 rounds already exhausts the "
        f"aggregate budget (default 25), so constraint_b must never be reached: got {seen_focuses}"
    )
    assert result["status"] == "paused"
    assert result["reason"] == "decomposed_task_budget_exhausted"
    print("PASS: _run_constraint_labels_from() stops BEFORE starting the next constraint once the aggregate round budget is exhausted by an earlier one")


def test_completes_normally_when_within_the_aggregate_ceiling():
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b"]
    satisfied = {"constraint_a": "pending", "constraint_b": "pending"}
    seen_focuses = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_focuses.append(sub_contract.current_constraint_label)
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

    result = asyncio.run(run())
    assert seen_focuses == ["constraint_a", "constraint_b"]
    assert result["passed"] is True
    print("PASS: a decomposed task comfortably within its own aggregate ceiling completes all constraints normally, unaffected by the new check")


def test_ceiling_accumulates_across_multiple_earlier_sub_contracts():
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b", "constraint_c", "constraint_d"]
    satisfied = {label: "pending" for label in labels}
    seen_focuses = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_focuses.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "rounds_taken": 9, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    # 9 + 9 + 9 = 27 >= 25 (default budget) -- stops before starting the 4th (9*3=27 already
    # exceeds budget after 3 sub-contracts, so only 3 should ever run).
    assert seen_focuses == ["constraint_a", "constraint_b", "constraint_c"]
    assert result["status"] == "paused"
    assert result["reason"] == "decomposed_task_budget_exhausted"
    print("PASS: the ceiling correctly accumulates rounds ACROSS multiple earlier sub-contracts (9+9+9=27 >= 25), not just the most recent one")


if __name__ == "__main__":
    test_stops_before_starting_the_next_constraint_once_aggregate_budget_is_exhausted()
    test_completes_normally_when_within_the_aggregate_ceiling()
    test_ceiling_accumulates_across_multiple_earlier_sub_contracts()
    print("\nALL DECOMPOSED-TASK AGGREGATE-CEILING TESTS PASSED")
