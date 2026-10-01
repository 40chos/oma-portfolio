"""P12 Tier S item 2 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for fold_code_review_findings()'s new fail-closed handling of Code-Review structural
failures (ReviewGenerationError/NoReviewTargetError/CodebaseReadError/ConstitutionNotFoundError).
Before this fix, code_review_output.detail={} on a structural failure was silently
indistinguishable from "reviewed cleanly, zero findings" -- a real fail-open gap. Pure,
deterministic, zero live calls -- constructs SpecialistOutput/VerificationResult directly, no
model gateway involved.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import SpecialistOutput, SpecialistType, VerificationResult
from manager.tools import fold_code_review_findings


def _base_verification_result(passed: bool = True) -> VerificationResult:
    return VerificationResult(
        task_id=uuid.uuid4(),
        passed=passed,
        reproduction_confirmed=True,
        uncovered_paths=[],
        coverage_diff="",
        spot_check_mismatch=False,
        notes="Reproduction confirmed.",
    )


def _code_review_output(detail: dict) -> SpecialistOutput:
    return SpecialistOutput(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.code_review,
        summary="test",
        detail=detail,
        claims_complete=False,
    )


def test_structural_failure_forces_hard_fail_even_with_passing_verification():
    result = _base_verification_result(passed=True)
    code_review_output = _code_review_output(
        {"structural_failure": True, "structural_failure_kind": "ReviewGenerationError"}
    )
    folded = fold_code_review_findings(result, code_review_output)
    assert folded.passed is False
    assert "could not actually produce a real review" in folded.notes
    assert "ReviewGenerationError" in folded.notes
    print("PASS: a structural failure forces passed=False, never silently folded as clean")


def test_empty_detail_dict_with_no_structural_marker_still_treated_as_clean_review():
    # A GENUINE clean review with zero findings still has detail={"findings": []} in real usage
    # (see specialists/code_review/specialist.py's _run_diff_review) -- this confirms the fix
    # doesn't over-correct and start failing every empty-findings case.
    result = _base_verification_result(passed=True)
    code_review_output = _code_review_output({"mode": "diff_review", "module_name": "x", "findings": []})
    folded = fold_code_review_findings(result, code_review_output)
    assert folded.passed is True
    assert folded.notes == result.notes
    print("PASS: a genuine clean review (real empty findings list, no structural marker) still passes through unchanged")


def test_none_code_review_output_is_still_a_pure_noop():
    result = _base_verification_result(passed=True)
    folded = fold_code_review_findings(result, None)
    assert folded is result
    print("PASS: code_review_output=None remains a pure no-op, unchanged by this fix")


def test_real_blocking_findings_still_fail_as_before():
    result = _base_verification_result(passed=True)
    code_review_output = _code_review_output(
        {"findings": [{"severity": "blocking", "explanation": "real problem"}]}
    )
    folded = fold_code_review_findings(result, code_review_output)
    assert folded.passed is False
    assert "real problem" in folded.notes
    print("PASS: genuine blocking findings still fail the round exactly as before this fix")


if __name__ == "__main__":
    test_structural_failure_forces_hard_fail_even_with_passing_verification()
    test_empty_detail_dict_with_no_structural_marker_still_treated_as_clean_review()
    test_none_code_review_output_is_still_a_pure_noop()
    test_real_blocking_findings_still_fail_as_before()
    print("\nALL FOLD-CODE-REVIEW STRUCTURAL-FAILURE TESTS PASSED")
