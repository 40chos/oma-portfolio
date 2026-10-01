"""P12 Tier S item 3 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for should_escalate_to_operator()'s new wire-in of check_repeated_failures_across_modules()
(manager/learning.py) -- fully built and tested since Phase 15 but never actually called from any
live path before this fix. Real Postgres writes via append_project_memory() (the same convention
tests/test_replanning.py's own existing cross-module test already uses) -- zero LLM/GPU calls
anywhere in this file, should_escalate_to_operator() itself is pure/synchronous.

P14 item 7 (docs/planning/PHASE30_P14_ITEM7_ONE_OFF_LATENT_STRUCTURE_2026-08-01.md, 2026-08-01):
the gate was extended to also cover root_cause == "one_off" (previously scoped to
"pattern_worth_a_rule" only, which this file's own original test asserted -- see the rewritten
test below), guarded by a new generic-failure-text exclusion so the real, generic
"Sandbox install failed"/"Sandbox pre-flight" wrapper text (the report's own §3 finding) can never
produce a meaningless cross-module "match" between genuinely unrelated failures.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract, VerificationResult
from manager.replanning import should_escalate_to_operator
from manager.tools import append_project_memory


def _contract(goal: str = "x") -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        planning_round_budget=100, round_wall_clock_cap_seconds=99999,
    )


def _verification_result(contract: TaskContract, root_cause: str | None, notes: str) -> VerificationResult:
    return VerificationResult(
        task_id=contract.task_id, passed=False, reproduction_confirmed=False,
        uncovered_paths=[], coverage_diff="", spot_check_mismatch=False,
        root_cause=root_cause, notes=notes,
    )


def test_pattern_worth_a_rule_escalates_on_real_cross_module_recurrence():
    sig = f"generated models_py assigns test-sig-{uuid.uuid4().hex[:8]} more than once"
    module_a = f"module_a_{uuid.uuid4().hex[:8]}"
    module_b = f"module_b_{uuid.uuid4().hex[:8]}"
    append_project_memory(event_type="outcome", actor="testing_qa", task_id=str(uuid.uuid4()), module=module_a,
                           summary=sig, tags=["failed"], detail={"root_cause": "pattern_worth_a_rule"}, verified=True)
    append_project_memory(event_type="outcome", actor="testing_qa", task_id=str(uuid.uuid4()), module=module_b,
                           summary=sig, tags=["failed"], detail={"root_cause": "pattern_worth_a_rule"}, verified=True)

    contract = _contract()
    v = _verification_result(contract, root_cause="pattern_worth_a_rule", notes=sig)
    reason = should_escalate_to_operator(contract, round_number=1, elapsed_seconds=0.0, verification_result=v)
    assert reason == "repeated_failure"
    print("PASS: a real cross-module pattern_worth_a_rule recurrence now escalates via should_escalate_to_operator()")


def test_one_off_root_cause_now_triggers_cross_module_check_on_specific_recurrence():
    sig = f"models_py names a field 'special_field_{uuid.uuid4().hex[:8]}' that is never declared"
    module_a = f"module_a_{uuid.uuid4().hex[:8]}"
    module_b = f"module_b_{uuid.uuid4().hex[:8]}"
    append_project_memory(event_type="outcome", actor="testing_qa", task_id=str(uuid.uuid4()), module=module_a,
                           summary=sig, tags=["failed"], detail={"root_cause": "one_off"}, verified=True)
    append_project_memory(event_type="outcome", actor="testing_qa", task_id=str(uuid.uuid4()), module=module_b,
                           summary=sig, tags=["failed"], detail={"root_cause": "one_off"}, verified=True)

    contract = _contract()
    v = _verification_result(contract, root_cause="one_off", notes=sig)
    reason = should_escalate_to_operator(contract, round_number=1, elapsed_seconds=0.0, verification_result=v)
    assert reason == "repeated_failure"
    print("PASS: P14 item 7 -- root_cause='one_off' with a real, specific cross-module recurrence now escalates too")


def test_one_off_generic_sandbox_wrapper_text_never_triggers():
    sig = f"Sandbox install failed: Install failed (rc=255) -- see log_tail for detail. {uuid.uuid4().hex[:8]}"
    module_a = f"module_a_{uuid.uuid4().hex[:8]}"
    module_b = f"module_b_{uuid.uuid4().hex[:8]}"
    append_project_memory(event_type="outcome", actor="testing_qa", task_id=str(uuid.uuid4()), module=module_a,
                           summary=sig, tags=["failed"], detail={"root_cause": "one_off"}, verified=True)
    append_project_memory(event_type="outcome", actor="testing_qa", task_id=str(uuid.uuid4()), module=module_b,
                           summary=sig, tags=["failed"], detail={"root_cause": "one_off"}, verified=True)

    contract = _contract()
    v = _verification_result(contract, root_cause="one_off", notes=sig)
    reason = should_escalate_to_operator(contract, round_number=1, elapsed_seconds=0.0, verification_result=v)
    assert reason is None
    print("PASS: generic 'Sandbox install failed' wrapper text is excluded even with 2 real prior matches -- avoids meaningless cross-module matches on boilerplate")


def test_pattern_worth_a_rule_with_no_real_recurrence_does_not_escalate():
    sig = f"genuinely novel pattern text {uuid.uuid4().hex[:8]}"
    contract = _contract()
    v = _verification_result(contract, root_cause="pattern_worth_a_rule", notes=sig)
    reason = should_escalate_to_operator(contract, round_number=1, elapsed_seconds=0.0, verification_result=v)
    assert reason is None
    print("PASS: pattern_worth_a_rule with no real prior recurrence does not escalate (min_prior_failures not met)")


def test_is_generic_failure_notes_helper():
    from manager.replanning import _is_generic_failure_notes
    assert _is_generic_failure_notes("Sandbox install failed: Install failed (rc=255)") is True
    assert _is_generic_failure_notes("Sandbox pre-flight failed: some exc text") is True
    assert _is_generic_failure_notes("  Sandbox install failed: leading whitespace") is True
    assert _is_generic_failure_notes("models_py assigns ['_inherit'] more than once") is False
    assert _is_generic_failure_notes("") is False
    print("PASS: _is_generic_failure_notes() matches only the real, established generic-wrapper prefixes")


if __name__ == "__main__":
    test_pattern_worth_a_rule_escalates_on_real_cross_module_recurrence()
    test_one_off_root_cause_now_triggers_cross_module_check_on_specific_recurrence()
    test_one_off_generic_sandbox_wrapper_text_never_triggers()
    test_pattern_worth_a_rule_with_no_real_recurrence_does_not_escalate()
    test_is_generic_failure_notes_helper()
    print("\nALL SHOULD-ESCALATE CROSS-MODULE TESTS PASSED")
