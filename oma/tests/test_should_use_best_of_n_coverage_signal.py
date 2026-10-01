"""P10 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md §18,
Phase O, item 3): tests for _should_use_best_of_n()'s new field_type-coverage-driven trigger.
Pure, synchronous, zero LLM/GPU calls -- uses the real coverage_data.json (already confirmed via
tests/test_coverage_signal.py to load correctly in this repo layout) rather than mocking it, since
the whole point is confirming the real, current low-confidence field types actually trigger this.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.coverage_signal import field_type_has_low_coverage_confidence
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.build.specialist import _should_use_best_of_n


def _contract(goal_facts: dict) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="x", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={"a": "pending"}, goal_facts=goal_facts,
    )


def test_a_real_low_confidence_field_type_forces_best_of_n():
    # Binary is confirmed real, live low-confidence (generation_supported=False) in the actual
    # coverage_data.json, per tests/test_coverage_signal.py's own direct check against it.
    assert field_type_has_low_coverage_confidence("Binary") is True, (
        "sanity precondition: Binary must genuinely be flagged low-confidence in the real file "
        "for this test to mean anything"
    )
    contract = _contract({"field_type": "Binary", "is_computed": False})
    assert _should_use_best_of_n(contract) is True
    print("PASS: a real, low-confidence field type (Binary) forces best-of-N even for a plain single-constraint round")


def test_no_field_type_never_forces_best_of_n_via_this_signal():
    contract = _contract({"is_computed": False})
    assert _should_use_best_of_n(contract) is False
    print("PASS: no field_type at all never triggers this signal -- falls through to the existing checks")


def test_an_unknown_field_type_never_forces_best_of_n():
    contract = _contract({"field_type": "TotallyMadeUpType9000", "is_computed": False})
    assert _should_use_best_of_n(contract) is False
    print("PASS: a field_type with no real matching coverage node never forces best-of-N -- absence of signal is never treated as low confidence")


if __name__ == "__main__":
    test_a_real_low_confidence_field_type_forces_best_of_n()
    test_no_field_type_never_forces_best_of_n_via_this_signal()
    test_an_unknown_field_type_never_forces_best_of_n()
    print("\nALL SHOULD-USE-BEST-OF-N COVERAGE-SIGNAL TESTS PASSED")
