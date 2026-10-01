"""HUMAN_DECISION escalation push (2026-08-07, task019, real task_id
0c2d4660-5dee-4cef-9468-c09b291b233e): the SAME bug class as
test_goal_facts_cleared_per_sub_contract.py (P12 Tier A item 21), for a field that never got the
same fix -- previous_round_raw_failure_text is an EARLIER constraint's own raw rejection text
(e.g. "['action_create_batch_invoice'] ... belongs to a LATER round's own constraint, not this
one"), rendered verbatim into the NEXT sub-contract's own <previous_attempt_errors> block even
though the round has now genuinely moved on to a DIFFERENT constraint that may require the exact
thing the earlier constraint's message told the model to remove. Confirmed live on task019: round
2's own real focus was 'invoice_action_return' (a later constraint requiring
action_create_batch_invoice to exist), but round 1's stale "remove the extra method(s)" rejection
was still shown as round 2's own feedback, directly contributing to the model omitting the
now-required method entirely. Zero LLM/GPU calls -- _execute_contract() mocked, inspects the real
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


def _make_decomposable_contract_with_stale_failure_text() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A; add method B.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
        # Simulates a resumed/later sub-contract carrying forward an EARLIER constraint's own raw
        # rejection text -- the exact real, confirmed shape of task019's real bug.
        previous_round_raw_failure_text=(
            "this round's own goal explicitly lists ['action_b_method'] as NOT yet in scope, but "
            "models_py adds new method definition(s) beyond the prior committed version: "
            "['action_b'] -- write ONLY the code THIS round's current constraint strictly "
            "requires. Remove the extra method(s); they belong to a LATER round's own "
            "constraint, not this one."
        ),
    )


def test_every_sub_contract_receives_a_freshly_cleared_previous_round_raw_failure_text():
    contract = _make_decomposable_contract_with_stale_failure_text()
    labels = ["constraint_a", "constraint_b"]
    satisfied = {"constraint_a": "pending", "constraint_b": "pending"}
    seen_failure_text = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_failure_text.append(sub_contract.previous_round_raw_failure_text)
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
    assert seen_failure_text == ["", ""], (
        f"every sub_contract must receive a genuinely empty previous_round_raw_failure_text (a "
        f"new constraint's own first attempt has no real prior-attempt feedback of its own yet), "
        f"never the base contract's stale, earlier-constraint-specific rejection text -- got "
        f"{seen_failure_text!r}"
    )
    print("PASS: every sub_contract gets a freshly cleared previous_round_raw_failure_text, "
          "never the base contract's stale, earlier-constraint-specific rejection text")


def test_the_base_contract_passed_in_is_never_itself_mutated():
    contract = _make_decomposable_contract_with_stale_failure_text()
    original_failure_text = contract.previous_round_raw_failure_text
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
    assert contract.previous_round_raw_failure_text == original_failure_text, (
        "model_copy() must never mutate the original contract object"
    )
    print("PASS: the original base contract object passed in is never itself mutated (model_copy creates a real, independent new object)")


if __name__ == "__main__":
    test_every_sub_contract_receives_a_freshly_cleared_previous_round_raw_failure_text()
    test_the_base_contract_passed_in_is_never_itself_mutated()
    print("\nALL PREVIOUS-ROUND-RAW-FAILURE-TEXT-CLEARED-PER-SUB-CONTRACT TESTS PASSED")
