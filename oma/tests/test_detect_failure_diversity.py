"""Phase 30, P2c (§13, item 4): tests for detect_failure_diversity() --
the real, confirmed blind spot in BOTH existing oscillation detectors
(detect_oscillation(), detect_shape_oscillation()), which are built
around RECURRENCE and never fire for genuine "problem-hopping": a task
that fails a different way every round, never repeating a shape.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, ReplanRound, SpecialistType, TaskContract, VerificationResult
from manager.replanning import detect_failure_diversity


def _make_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix, goal="test goal",
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )


def _round(round_number: int, root_cause: str | None) -> ReplanRound:
    contract = _make_contract()
    verification = VerificationResult(
        task_id=uuid.uuid4(), passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes="round failed", regressed_constraints=[], root_cause=root_cause,
    )
    return ReplanRound(
        round_number=round_number, previous_contract_task_id=str(contract.task_id),
        verification_result=verification, revision_reasoning="test round", new_contract=contract,
    )


def test_fires_on_the_real_worked_example_three_distinct_never_repeating():
    """The plan's own real worked example: a task hitting a double-cancel
    bug, a missed-reminder bug, and a duplicate-invoice bug -- 3 distinct
    shapes, none repeating. Corrected from an original 4+ proposal that
    a later check found would have missed this exact real case.
    """
    rounds = [
        _round(1, "double_cancel_bug"),
        _round(2, "missed_reminder_bug"),
        _round(3, "duplicate_invoice_bug"),
    ]
    assert detect_failure_diversity(rounds) is True
    print("PASS: fires on the real 3-distinct-never-repeating worked example")


def test_does_not_fire_when_a_cause_repeats():
    rounds = [
        _round(1, "install_failure"),
        _round(2, "field_declaration_bug"),
        _round(3, "install_failure"),  # repeats round 1 -- this is oscillation-adjacent, not pure diversity
    ]
    assert detect_failure_diversity(rounds) is False
    print("PASS: does not fire once any root_cause repeats -- that's a different signal's job")


def test_does_not_fire_below_the_threshold():
    rounds = [_round(1, "install_failure"), _round(2, "field_declaration_bug")]
    assert detect_failure_diversity(rounds) is False
    print("PASS: 2 distinct causes (below the 3+ threshold) does not fire")


def test_unclassified_rounds_are_never_counted_as_a_distinct_cause():
    rounds = [
        _round(1, None), _round(2, None),
        _round(3, "install_failure"), _round(4, "field_declaration_bug"), _round(5, "security_issue"),
    ]
    assert detect_failure_diversity(rounds) is True
    print("PASS: None (unclassified) rounds are excluded, never inflating the distinct count")


def test_fires_on_four_or_more_distinct_causes_too():
    rounds = [_round(i, f"cause_{i}") for i in range(1, 6)]
    assert detect_failure_diversity(rounds) is True
    print("PASS: also fires with more than the minimum 3 distinct, never-repeating causes")


if __name__ == "__main__":
    test_fires_on_the_real_worked_example_three_distinct_never_repeating()
    test_does_not_fire_when_a_cause_repeats()
    test_does_not_fire_below_the_threshold()
    test_unclassified_rounds_are_never_counted_as_a_distinct_cause()
    test_fires_on_four_or_more_distinct_causes_too()
    print("\nALL DETECT-FAILURE-DIVERSITY TESTS PASSED")
