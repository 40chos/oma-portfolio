"""P13 item 7 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): tests for manager/replanning.py's revise_contract_from_verification() now populating
TaskContract.failure_records (and the matching ConstraintNode.failure_records, when a node exists
for the current constraint label) from the exact same evidence that already builds `rules`.

Pure, synchronous, zero LLM/GPU calls -- revise_contract_from_verification() itself makes no
gateway calls (confirmed by its own signature/docstring).
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import (
    AutonomyTier,
    CapabilityClass,
    ConstraintNode,
    FailureCategory,
    SpecialistType,
    TaskContract,
    VerificationResult,
)
from manager.replanning import revise_contract_from_verification


def _make_contract(**overrides) -> TaskContract:
    base = dict(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A.", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )
    base.update(overrides)
    return TaskContract(**base)


def _make_verification(**overrides) -> VerificationResult:
    base = dict(
        task_id=uuid.uuid4(), passed=False, reproduction_confirmed=True,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes="round failed", regressed_constraints=[], root_cause=None,
    )
    base.update(overrides)
    return VerificationResult(**base)


def test_a_real_failure_record_is_appended_to_the_flat_list():
    contract = _make_contract()
    v = _make_verification(notes="something real went wrong")
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=1)
    assert len(new_contract.failure_records) == 1
    record = new_contract.failure_records[0]
    assert record.round_number == 1
    assert record.observed == "something real went wrong"
    print("PASS: revise_contract_from_verification() appends one real FailureRecord to the flat list")


def test_code_review_finding_produces_scope_violation_category():
    contract = _make_contract()
    v = _make_verification()
    new_contract, _ = revise_contract_from_verification(
        contract, v, round_number=2,
        code_review_finding="This field is NOT yet in scope for this round.",
    )
    record = new_contract.failure_records[0]
    assert record.failure_category == FailureCategory.scope_violation
    assert record.source == "code_review"
    print("PASS: a code_review_finding naming out-of-scope work classifies as scope_violation")


def test_regressed_constraints_produce_regression_category():
    contract = _make_contract()
    v = _make_verification(regressed_constraints=["scheduling_fields"])
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=3)
    record = new_contract.failure_records[0]
    assert record.failure_category == FailureCategory.regression
    assert "scheduling_fields" in record.concrete_alternative
    print("PASS: regressed_constraints classifies as regression, with a real concrete_alternative")


def test_unclear_signal_stays_unclassified_never_guessed():
    contract = _make_contract()
    v = _make_verification(reproduction_confirmed=True, notes="an ordinary ambiguous failure")
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=1)
    record = new_contract.failure_records[0]
    assert record.failure_category == FailureCategory.unclassified
    print("PASS: an ambiguous failure with no clear signal stays unclassified, never guessed")


def test_matching_constraint_node_also_receives_the_failure_record():
    node = ConstraintNode(label="add_field_a", creates=["model.field_a"])
    contract = _make_contract(
        current_constraint_label="add_field_a", constraint_nodes={"add_field_a": node},
    )
    v = _make_verification(notes="field a still missing")
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=1)
    updated_node = new_contract.constraint_nodes["add_field_a"]
    assert len(updated_node.failure_records) == 1
    assert updated_node.failure_records[0].observed == "field a still missing"
    # the flat, contract-level list is a SEPARATE copy, not a shared reference
    assert len(new_contract.failure_records) == 1
    print("PASS: a matching ConstraintNode also receives the same FailureRecord, per item 7's own design")


def test_no_matching_constraint_node_leaves_constraint_nodes_untouched():
    contract = _make_contract(current_constraint_label="add_field_b", constraint_nodes={})
    v = _make_verification()
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=1)
    assert new_contract.constraint_nodes == {}
    assert len(new_contract.failure_records) == 1
    print("PASS: with no matching ConstraintNode, constraint_nodes stays empty but the flat list still gets the record")


def test_business_rule_probe_failure_produces_business_rule_mismatch_category():
    """P13 item 12a integration (added 2026-08-02): a failed business-rule probe
    (specialists/testing_qa/specialist.py's _run_business_rule_probes()) carries a distinctive
    marker in verification_result.notes -- must classify as business_rule_mismatch, with
    concrete_alternative set to the specific interpretation the probe checked, BEFORE falling
    through to the generic reproduction_gap/unclassified path -- even when reproduction_confirmed
    is True (the claimed field genuinely exists; the specific interpretation of it is wrong).
    """
    contract = _make_contract()
    v = _make_verification(
        reproduction_confirmed=True,
        notes=(
            "Reproduction confirmed for sale.order.amount_total. Spot-check matches the "
            "self-report. Business rule check FAILED: 'the order total includes tax' -- real "
            "observed result {'amount_total': 100.0}: the observed total excludes tax entirely."
        ),
    )
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=1)
    record = new_contract.failure_records[0]
    assert record.failure_category == FailureCategory.business_rule_mismatch
    assert record.concrete_alternative == "the order total includes tax"
    assert "amount_total" in record.observed
    assert record.source == "testing_qa"
    print("PASS: a failed business-rule probe classifies as business_rule_mismatch with a real concrete_alternative")


def test_original_contract_is_never_mutated():
    contract = _make_contract()
    original_failure_records = list(contract.failure_records)
    v = _make_verification()
    revise_contract_from_verification(contract, v, round_number=1)
    assert contract.failure_records == original_failure_records
    print("PASS: the original contract object passed in is never itself mutated")


if __name__ == "__main__":
    test_a_real_failure_record_is_appended_to_the_flat_list()
    test_code_review_finding_produces_scope_violation_category()
    test_regressed_constraints_produce_regression_category()
    test_unclear_signal_stays_unclassified_never_guessed()
    test_matching_constraint_node_also_receives_the_failure_record()
    test_no_matching_constraint_node_leaves_constraint_nodes_untouched()
    test_business_rule_probe_failure_produces_business_rule_mismatch_category()
    test_original_contract_is_never_mutated()
    print("\nALL FAILURE-RECORD WIRING TESTS PASSED")
