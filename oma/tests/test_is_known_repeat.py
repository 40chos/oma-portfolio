"""P13 item 8 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): tests for manager/replanning.py's is_known_repeat() deterministic pre-check gate. Pure,
synchronous, zero-LLM/GPU calls -- no mocks needed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import ConstraintNode, FailureCategory, FailureRecord
from manager.replanning import _build_failure_record, is_known_repeat, revise_contract_from_verification


def test_no_prior_failures_is_never_a_repeat():
    node = ConstraintNode(label="scheduling_fields")
    candidate = FailureRecord(
        round_number=1, failure_category=FailureCategory.missing_required_field,
        location="models.py:10", observed="field priority not declared",
    )
    assert is_known_repeat(candidate, node.failure_records) is False
    print("PASS: a node with no failure history is never flagged as a repeat")


def test_same_category_and_same_underlying_finding_is_a_repeat():
    prior = FailureRecord(
        round_number=1, failure_category=FailureCategory.missing_required_field,
        location="models.py:10", observed="field priority not declared on scheduling model",
    )
    node = ConstraintNode(label="scheduling_fields", failure_records=[prior])
    candidate = FailureRecord(
        round_number=2, failure_category=FailureCategory.missing_required_field,
        location="models.py:10", observed="field priority still not declared on scheduling model",
    )
    assert is_known_repeat(candidate, node.failure_records) is True
    print("PASS: the same underlying finding recurring in the same category is caught")


def test_different_category_is_never_a_repeat_even_with_identical_text():
    prior = FailureRecord(
        round_number=1, failure_category=FailureCategory.scope_violation,
        location="models.py:10", observed="field priority not declared on scheduling model",
    )
    node = ConstraintNode(label="scheduling_fields", failure_records=[prior])
    candidate = FailureRecord(
        round_number=2, failure_category=FailureCategory.missing_required_field,
        location="models.py:10", observed="field priority not declared on scheduling model",
    )
    assert is_known_repeat(candidate, node.failure_records) is False
    print("PASS: identical wording in a DIFFERENT failure_category is not treated as a repeat")


def test_unrelated_finding_same_category_is_not_a_repeat():
    prior = FailureRecord(
        round_number=1, failure_category=FailureCategory.missing_required_field,
        location="views.xml:5", observed="menu action reference missing entirely",
    )
    node = ConstraintNode(label="scheduling_fields", failure_records=[prior])
    candidate = FailureRecord(
        round_number=2, failure_category=FailureCategory.missing_required_field,
        location="models.py:10", observed="field priority not declared on scheduling model",
    )
    assert is_known_repeat(candidate, node.failure_records) is False
    print("PASS: an unrelated finding in the same category is not treated as a repeat")


def test_lookback_window_excludes_older_records():
    old = FailureRecord(
        round_number=1, failure_category=FailureCategory.missing_required_field,
        location="models.py:10", observed="field priority not declared on scheduling model",
    )
    filler1 = FailureRecord(round_number=2, failure_category=FailureCategory.regression)
    filler2 = FailureRecord(round_number=3, failure_category=FailureCategory.xmlid_mismatch)
    filler3 = FailureRecord(round_number=4, failure_category=FailureCategory.hallucinated_finding)
    node = ConstraintNode(label="scheduling_fields", failure_records=[old, filler1, filler2, filler3])
    candidate = FailureRecord(
        round_number=5, failure_category=FailureCategory.missing_required_field,
        location="models.py:10", observed="field priority still not declared on scheduling model",
    )
    assert is_known_repeat(candidate, node.failure_records, lookback=3) is False
    assert is_known_repeat(candidate, node.failure_records, lookback=4) is True
    print("PASS: lookback correctly bounds how far back a repeat is searched for")


def test_task005_real_round4_and_round5_findings_are_caught_as_a_repeat():
    """Real-world regression test using the ACTUAL observed Code-Review finding text from
    task005's real Sonnet 5 round history (docs/reports/PHASE30_P14_ITEM3_SONNET5_FOLLOWUP_
    2026-08-02.md, task_id 607fc63b-5a63-4dcf-87c8-33d299b94577) -- confirms is_known_repeat()
    genuinely catches this exact real shape now, not just a synthetic example. Round 4's and
    round 5's Code-Review findings, pulled verbatim from the real trace, are built into
    FailureRecords via the real _build_failure_record() (never a hand-constructed shortcut) to
    prove the whole real pipeline -- classification, then repeat detection -- catches it.
    """
    round4_finding = (
        "views.xml is empty, so user_id/project_id are not exposed on the meerwerk form view"
    )
    round5_finding = (
        "project_id and user_id are never exposed on project.meerwerk's form view"
    )
    import uuid

    from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract, VerificationResult

    base_contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal=(
            "When I select a project on the meerwerk form, I want the 'Assigned to' field to "
            "automatically fill with the project manager."
        ),
        inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        # The real, confirmed root cause: a plain, non-decomposed task never sets this.
        current_constraint_label=None, constraint_nodes={},
    )
    v4 = VerificationResult(
        task_id=base_contract.task_id, passed=False, reproduction_confirmed=True,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=True, notes="round 4 notes",
        regressed_constraints=[], root_cause="one_off",
    )
    contract_after_round4, _ = revise_contract_from_verification(
        base_contract, v4, round_number=4, code_review_finding=round4_finding,
    )
    round5_candidate = _build_failure_record(
        round_number=5, contract=contract_after_round4,
        verification_result=VerificationResult(
            task_id=base_contract.task_id, passed=False, reproduction_confirmed=True,
            uncovered_paths=[], coverage_diff="", spot_check_mismatch=True, notes="round 5 notes",
            regressed_constraints=[], root_cause="one_off",
        ),
        code_review_finding=round5_finding, field_omission_snippet=None,
    )
    # The real fallback this fix adds: no constraint-node history exists (current_constraint_label
    # is None, matching task005's own real shape), so the flat contract.failure_records is what
    # the round loop's own widened gate now reads from.
    assert is_known_repeat(round5_candidate, contract_after_round4.failure_records) is True
    print("PASS: task005's real round-4/round-5 findings are caught as a known repeat via the flat-list fallback")


if __name__ == "__main__":
    test_no_prior_failures_is_never_a_repeat()
    test_same_category_and_same_underlying_finding_is_a_repeat()
    test_different_category_is_never_a_repeat_even_with_identical_text()
    test_unrelated_finding_same_category_is_not_a_repeat()
    test_lookback_window_excludes_older_records()
    test_task005_real_round4_and_round5_findings_are_caught_as_a_repeat()
    print("\nALL IS_KNOWN_REPEAT TESTS PASSED")
