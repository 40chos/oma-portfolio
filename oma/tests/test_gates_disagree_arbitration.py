"""P12 Tier S/A item 7 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for fold_code_review_findings()'s new arbitration marker (VerificationResult.gates_disagree)
and should_escalate_to_operator()'s new wiring of it -- replaces the previous unconditional
single-vote override with a real distinction between "Code-Review overrides an already-weak
Testing/QA pass" (the common case, unchanged) and "Code-Review overrides a STRONGLY
corroborated Testing/QA pass" (a genuine two-gate disagreement, now flagged and escalated
directly instead of burning ordinary retries). Pure, deterministic, zero live calls.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import (
    AutonomyTier, CapabilityClass, SpecialistType, SpecialistOutput, TaskContract, VerificationResult,
)
from manager.replanning import should_escalate_to_operator
from manager.tools import fold_code_review_findings


def _verification_result(**overrides) -> VerificationResult:
    base = dict(
        task_id=uuid.uuid4(), passed=True, reproduction_confirmed=True, uncovered_paths=[],
        coverage_diff="", spot_check_mismatch=False, notes="Reproduction confirmed.",
    )
    base.update(overrides)
    return VerificationResult(**base)


def _code_review_output(blocking_explanations: list[str]) -> SpecialistOutput:
    return SpecialistOutput(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.code_review, summary="test",
        detail={"findings": [{"severity": "blocking", "explanation": e} for e in blocking_explanations]},
        claims_complete=False,
    )


def _contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal="x", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        planning_round_budget=100, round_wall_clock_cap_seconds=99999,
    )


def test_strongly_corroborated_pass_overridden_by_blocking_finding_sets_gates_disagree():
    result = _verification_result(passed=True, reproduction_confirmed=True, spot_check_mismatch=False)
    code_review_output = _code_review_output(["real problem found"])
    folded = fold_code_review_findings(result, code_review_output)
    assert folded.passed is False
    assert folded.gates_disagree is True
    assert "genuine two-gate disagreement" in folded.notes
    print("PASS: a strongly-corroborated Testing/QA pass overridden by a blocking finding sets gates_disagree=True")


def test_weak_pass_overridden_by_blocking_finding_does_not_set_gates_disagree():
    result = _verification_result(passed=True, reproduction_confirmed=False, spot_check_mismatch=False)
    code_review_output = _code_review_output(["real problem found"])
    folded = fold_code_review_findings(result, code_review_output)
    assert folded.passed is False
    assert folded.gates_disagree is False
    print("PASS: an already-weak (unconfirmed reproduction) Testing/QA pass overridden by a blocking finding does NOT set gates_disagree -- the ordinary, ungated case")


def test_spot_check_mismatch_also_prevents_gates_disagree():
    result = _verification_result(passed=True, reproduction_confirmed=True, spot_check_mismatch=True)
    code_review_output = _code_review_output(["real problem found"])
    folded = fold_code_review_findings(result, code_review_output)
    assert folded.gates_disagree is False
    print("PASS: a spot-check mismatch also disqualifies 'strongly corroborated', same as an unconfirmed reproduction")


def test_failing_testing_qa_result_never_sets_gates_disagree():
    result = _verification_result(passed=False, reproduction_confirmed=True, spot_check_mismatch=False)
    code_review_output = _code_review_output(["real problem found"])
    folded = fold_code_review_findings(result, code_review_output)
    assert folded.gates_disagree is False
    print("PASS: Testing/QA already failing on its own is never treated as a 'disagreement' -- nothing to disagree about")


def test_no_blocking_findings_is_still_a_pure_no_op():
    result = _verification_result(passed=True, reproduction_confirmed=True, spot_check_mismatch=False)
    code_review_output = _code_review_output([])
    folded = fold_code_review_findings(result, code_review_output)
    assert folded is result
    assert folded.gates_disagree is False
    print("PASS: zero blocking findings remains a pure no-op, gates_disagree stays False")


def test_should_escalate_to_operator_escalates_immediately_on_gates_disagree():
    contract = _contract()
    result = _verification_result(passed=False, gates_disagree=True)
    reason = should_escalate_to_operator(contract, round_number=1, elapsed_seconds=0.0, verification_result=result)
    assert reason == "gates_disagree"
    print("PASS: should_escalate_to_operator() escalates immediately on round 1 when gates_disagree is True")


def test_should_escalate_to_operator_does_not_escalate_on_ordinary_blocking_finding():
    contract = _contract()
    result = _verification_result(passed=False, gates_disagree=False, root_cause="one_off")
    reason = should_escalate_to_operator(contract, round_number=1, elapsed_seconds=0.0, verification_result=result)
    assert reason is None
    print("PASS: an ordinary blocking-finding failure (gates_disagree=False) does not trigger the new escalation path")


if __name__ == "__main__":
    test_strongly_corroborated_pass_overridden_by_blocking_finding_sets_gates_disagree()
    test_weak_pass_overridden_by_blocking_finding_does_not_set_gates_disagree()
    test_spot_check_mismatch_also_prevents_gates_disagree()
    test_failing_testing_qa_result_never_sets_gates_disagree()
    test_no_blocking_findings_is_still_a_pure_no_op()
    test_should_escalate_to_operator_escalates_immediately_on_gates_disagree()
    test_should_escalate_to_operator_does_not_escalate_on_ordinary_blocking_finding()
    print("\nALL GATES-DISAGREE ARBITRATION TESTS PASSED")
