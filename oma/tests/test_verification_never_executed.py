"""P12 Tier A item 19 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 18): tests for the new detect_verification_never_executed() oscillation-family
detector and should_escalate_to_operator()'s new wiring of it. Real, confirmed gap: none of the
three existing detectors (detect_oscillation/detect_shape_oscillation/detect_failure_diversity)
ever fire for a structurally dead-end task -- it silently rides the generic round_budget
escalation with no distinguishing signal. Pure, deterministic, zero live calls.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import (
    AutonomyTier, CapabilityClass, ReplanRound, SpecialistType, TaskContract, VerificationResult,
)
from manager.replanning import detect_verification_never_executed, should_escalate_to_operator


def _contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal="x", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        planning_round_budget=100, round_wall_clock_cap_seconds=99999,
    )


def _round(round_number: int, reproduction_confirmed: bool, uncovered_paths=None, coverage_diff="") -> ReplanRound:
    contract = _contract()
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=reproduction_confirmed,
        uncovered_paths=uncovered_paths or [], coverage_diff=coverage_diff, spot_check_mismatch=False,
        notes="stuck",
    )
    return ReplanRound(
        round_number=round_number, previous_contract_task_id=str(contract.task_id),
        verification_result=v, revision_reasoning="x", new_contract=contract,
    )


def test_fires_when_three_rounds_all_show_the_never_executed_signature():
    rounds = [_round(i, reproduction_confirmed=False) for i in range(1, 4)]
    assert detect_verification_never_executed(rounds) is True
    print("PASS: 3 rounds with reproduction_confirmed=False and empty uncovered_paths/coverage_diff fires")


def test_does_not_fire_with_fewer_than_the_window_size():
    rounds = [_round(i, reproduction_confirmed=False) for i in range(1, 3)]
    assert detect_verification_never_executed(rounds) is False
    print("PASS: fewer than the window size (2 rounds) never fires")


def test_does_not_fire_when_a_round_actually_had_real_coverage_data():
    rounds = [
        _round(1, reproduction_confirmed=False),
        _round(2, reproduction_confirmed=False, uncovered_paths=["models.py:10"]),
        _round(3, reproduction_confirmed=False),
    ]
    assert detect_verification_never_executed(rounds) is False
    print("PASS: a round with real uncovered_paths data (verification genuinely ran) breaks the signature")


def test_does_not_fire_when_reproduction_was_ever_confirmed():
    rounds = [
        _round(1, reproduction_confirmed=False),
        _round(2, reproduction_confirmed=True),
        _round(3, reproduction_confirmed=False),
    ]
    assert detect_verification_never_executed(rounds) is False
    print("PASS: a round where reproduction WAS confirmed breaks the never-executed signature")


def test_should_escalate_to_operator_fires_verification_never_executed():
    contract = _contract()
    prior_rounds = [_round(i, reproduction_confirmed=False) for i in range(1, 4)]
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False, uncovered_paths=[],
        coverage_diff="", spot_check_mismatch=False, notes="stuck",
    )
    reason = should_escalate_to_operator(contract, round_number=3, elapsed_seconds=0.0, verification_result=v, prior_rounds=prior_rounds)
    assert reason == "verification_never_executed"
    print("PASS: should_escalate_to_operator() escalates with the specific verification_never_executed reason")


def test_should_escalate_to_operator_without_prior_rounds_never_fires_this_check():
    contract = _contract()
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False, uncovered_paths=[],
        coverage_diff="", spot_check_mismatch=False, notes="stuck", root_cause="one_off",
    )
    reason = should_escalate_to_operator(contract, round_number=1, elapsed_seconds=0.0, verification_result=v)
    assert reason is None
    print("PASS: omitting prior_rounds (backward-compatible default) never triggers this new check")


if __name__ == "__main__":
    test_fires_when_three_rounds_all_show_the_never_executed_signature()
    test_does_not_fire_with_fewer_than_the_window_size()
    test_does_not_fire_when_a_round_actually_had_real_coverage_data()
    test_does_not_fire_when_reproduction_was_ever_confirmed()
    test_should_escalate_to_operator_fires_verification_never_executed()
    test_should_escalate_to_operator_without_prior_rounds_never_fires_this_check()
    print("\nALL VERIFICATION-NEVER-EXECUTED TESTS PASSED")
