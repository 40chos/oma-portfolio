"""P12 Tier A item 21 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 24): tests confirming _run_constraint_labels_from() now clears goal_facts on every
new sub_contract -- real, confirmed gap: stale goal_facts extracted for an EARLIER constraint
silently survived onto a LATER, differently-focused sub-contract, and the extraction guard
elsewhere (`and not contract.goal_facts:`) never re-fired once any non-empty goal_facts existed,
so every constraint after the first was checked against the wrong (first constraint's own)
extracted facts. Zero LLM/GPU calls -- _execute_contract() mocked, inspects the real
sub_contract it's called with.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import _run_constraint_labels_from


def _make_decomposable_contract_with_stale_goal_facts() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A; add field B.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
        # Simulates a resumed/later sub-contract carrying forward whatever was extracted for
        # an EARLIER constraint -- the exact real, confirmed shape of the bug.
        goal_facts={"field_name": "field_a", "field_type": "Char"},
    )


def test_every_sub_contract_receives_a_freshly_cleared_goal_facts():
    contract = _make_decomposable_contract_with_stale_goal_facts()
    labels = ["constraint_a", "constraint_b"]
    satisfied = {"constraint_a": "pending", "constraint_b": "pending"}
    seen_goal_facts = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_goal_facts.append(dict(sub_contract.goal_facts))
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
    assert seen_goal_facts == [{}, {}], (
        f"every sub_contract must receive a genuinely empty goal_facts (so extraction re-fires "
        f"against ITS OWN narrowed goal text), not the base contract's stale, earlier-extracted "
        f"facts -- got {seen_goal_facts}"
    )
    print("PASS: every sub_contract gets a freshly cleared goal_facts, never the base contract's stale, earlier-constraint-extracted facts")


def test_the_base_contract_passed_in_is_never_itself_mutated():
    contract = _make_decomposable_contract_with_stale_goal_facts()
    original_goal_facts = dict(contract.goal_facts)
    labels = ["constraint_a"]
    satisfied = {"constraint_a": "pending"}

    async def fake_execute_contract(sub_contract, *args, **kwargs):
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
    assert contract.goal_facts == original_goal_facts, "model_copy() must never mutate the original contract object"
    print("PASS: the original base contract object passed in is never itself mutated (model_copy creates a real, independent new object)")


if __name__ == "__main__":
    test_every_sub_contract_receives_a_freshly_cleared_goal_facts()
    test_the_base_contract_passed_in_is_never_itself_mutated()
    print("\nALL GOAL-FACTS-CLEARED-PER-SUB-CONTRACT TESTS PASSED")
