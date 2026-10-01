"""P13 item 13 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): tests confirming revise_contract_from_verification() renders prior-round failure content
as coming from an external role (Testing/QA), never as Build's own continued self-assessment --
direct application of the Self-Correction Illusion finding (item 8). Prompt-construction detail
only, no new data channel: the underlying root_cause/notes content must survive unchanged so every
existing recurrence-detection/marker-string consumer still matches.

Pure, synchronous, zero LLM/GPU calls.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract, VerificationResult
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


def test_root_cause_branch_attributes_to_testing_qa_not_build_itself():
    contract = _make_contract()
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=True,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes="field priority still not declared", regressed_constraints=[], root_cause="skill_gap",
    )
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=2)
    rule = new_contract.rules[-1]
    assert rule.startswith("Testing/QA's own evaluation of your round 2 work found")
    assert "'skill_gap'" in rule
    assert "field priority still not declared" in rule
    assert "Prior attempt" not in rule
    print("PASS: the root_cause branch attributes the finding to Testing/QA, never 'Prior attempt'")


def test_generic_notes_branch_also_attributes_to_testing_qa():
    contract = _make_contract()
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=True,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes="an ordinary failure with no root_cause", regressed_constraints=[], root_cause=None,
    )
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=1)
    rule = new_contract.rules[-1]
    assert rule.startswith("Testing/QA's own evaluation of your round 1 work found:")
    assert "an ordinary failure with no root_cause" in rule
    assert "Prior attempt" not in rule
    print("PASS: the generic notes branch (no root_cause) also attributes to Testing/QA")


def test_recurrence_detection_still_fires_despite_the_reworded_prose():
    """The wrapper prose changed; the recurrence heuristic reads the raw notes content, so an
    identical underlying finding recurring must still get CRITICAL_RULE_PREFIX escalation.
    """
    notes = "field priority still not declared on the scheduling model, same as before"
    contract = _make_contract(rules=[f"Testing/QA's own evaluation of your round 1 work found: {notes}"])
    v = VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=True,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        notes=notes, regressed_constraints=[], root_cause=None,
    )
    new_contract, _ = revise_contract_from_verification(contract, v, round_number=2)
    rule = new_contract.rules[-1]
    assert "CRITICAL" in rule, "an identical recurring finding must still be escalated despite the reworded wrapper prose"
    print("PASS: recurrence detection still fires correctly with the new external-role wrapper prose")


if __name__ == "__main__":
    test_root_cause_branch_attributes_to_testing_qa_not_build_itself()
    test_generic_notes_branch_also_attributes_to_testing_qa()
    test_recurrence_detection_still_fires_despite_the_reworded_prose()
    print("\nALL EXTERNAL-ROLE RETRY-FRAMING TESTS PASSED")
