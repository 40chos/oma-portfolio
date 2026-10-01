"""P11 finding #15 (docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md §1.15,
confirmed twice, same session): tests for summarize_escalation_for_operator()'s switch from
generate_checked() (raw free text) to call_structured(use_grammar=True) -- the already-proven
fix for a model narrating its full reasoning as plain prose with no <think> tags at all (so
strip_think_block() cannot help). Mocks call_structured() directly, zero live LLM/GPU calls.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, patch

import manager.replanning as replanning_module
from contracts.schema import AutonomyTier, CapabilityClass, ReplanRound, SpecialistType, TaskContract, VerificationResult
from manager.replanning import _EscalationSummary, summarize_escalation_for_operator


def _make_round(round_number: int) -> ReplanRound:
    contract = TaskContract(
        task_id=__import__("uuid").uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="test goal", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )
    verification = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes="round failed", regressed_constraints=[], root_cause=None,
    )
    return ReplanRound(
        round_number=round_number, previous_contract_task_id=str(contract.task_id),
        verification_result=verification, revision_reasoning="test", new_contract=contract,
    )


def test_uses_call_structured_with_grammar_not_generate_checked():
    captured_kwargs = {}

    async def fake_call_structured(**kwargs):
        captured_kwargs.update(kwargs)
        return _EscalationSummary(summary="A real, clean 3-sentence explanation.")

    async def run():
        with patch.object(replanning_module, "call_structured", new=fake_call_structured):
            return await summarize_escalation_for_operator(
                "test goal", [_make_round(1)], "round_budget", client=None, model="x",
            )

    result = asyncio.run(run())
    assert result == "A real, clean 3-sentence explanation."
    assert captured_kwargs["use_grammar"] is True
    assert captured_kwargs["schema"] is _EscalationSummary
    print("PASS: summarize_escalation_for_operator() now uses call_structured(use_grammar=True)")


def test_result_never_contains_a_reasoning_preamble_by_construction():
    """The schema itself is a single `summary: str` field -- there is no way for a reasoning
    preamble to leak through it the way it did through a raw free-text response, since
    call_structured() only ever returns a validated instance of the schema.
    """
    async def fake_call_structured(**kwargs):
        return _EscalationSummary(summary="Clean answer only.")

    async def run():
        with patch.object(replanning_module, "call_structured", new=fake_call_structured):
            return await summarize_escalation_for_operator(
                "test goal", [_make_round(1), _make_round(2)], "repeated_failure", client=None, model="x",
            )

    result = asyncio.run(run())
    assert "thinking process" not in result.lower()
    assert result == "Clean answer only."
    print("PASS: the returned summary structurally cannot carry a leaked reasoning preamble")


def test_falls_back_honestly_on_a_genuine_failure():
    async def failing_call_structured(**kwargs):
        raise RuntimeError("gateway exploded")

    async def run():
        with patch.object(replanning_module, "call_structured", new=failing_call_structured):
            return await summarize_escalation_for_operator(
                "test goal", [_make_round(1)], "round_budget", client=None, model="x",
            )

    result = asyncio.run(run())
    assert "round failed" in result  # the last round's own real notes, not a generic placeholder
    print("PASS: a genuine generation failure still falls back to the honest, real-data summary")


if __name__ == "__main__":
    test_uses_call_structured_with_grammar_not_generate_checked()
    test_result_never_contains_a_reasoning_preamble_by_construction()
    test_falls_back_honestly_on_a_genuine_failure()
    print("\nALL SUMMARIZE-ESCALATION-FOR-OPERATOR GRAMMAR-SWITCH TESTS PASSED")
