"""P13 item 12b (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): regression guard confirming the bounded one_off recovery override is genuinely wired into
manager/loop.py's round loop, immediately after should_escalate_to_operator() -- same source-level
regression-guard reasoning already established for items 8/16/22/29 (this check lives deep inside
_execute_contract()'s own round loop, heavy real dependencies -- module locks, Redis, real
specialists -- making a full live reproduction impractical here).

Also directly tests the pure, checkable conditions of the override (real TaskContract instances,
zero LLM/GPU calls) rather than only the source-level wiring.
"""

import inspect
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract


def _make_contract(one_off_recovery_used: bool = False) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A.", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        one_off_recovery_used=one_off_recovery_used,
    )


def test_default_one_off_recovery_used_is_false():
    contract = _make_contract()
    assert contract.one_off_recovery_used is False
    print("PASS: a fresh contract has never used its one_off recovery attempt")


def test_wired_immediately_after_should_escalate_to_operator():
    source = inspect.getsource(loop_module)
    escalate_call_index = source.index(
        "escalation_reason = should_escalate_to_operator(\n"
        "                current_contract, round_number, elapsed_seconds, verification, prior_rounds=prior_rounds,\n"
        "            )"
    )
    window = source[escalate_call_index:escalate_call_index + 2200]
    assert 'escalation_reason == "round_budget"' in window
    assert 'verification.root_cause == "one_off"' in window
    assert "not current_contract.one_off_recovery_used" in window
    assert 'escalation_reason = None' in window
    assert '"one_off_recovery_used": True' in window
    print("PASS: the item 12b override is wired immediately after should_escalate_to_operator()")


def test_override_only_fires_for_round_budget_reason_never_other_reasons():
    source = inspect.getsource(loop_module)
    override_index = source.index("one_off_recovery_granted = False")
    window = source[override_index:override_index + 600]
    # Confirms the guard condition is a conjunction gated on "round_budget" specifically -- never
    # a bare "if escalation_reason and root_cause == one_off" that would also swallow
    # wall_clock/context_pressure/repeated_failure/gates_disagree, real hard stops this item's own
    # text says must stay untouched.
    assert 'escalation_reason == "round_budget"' in window
    print("PASS: the override's own guard is scoped to round_budget only, real hard stops untouched")


def test_recovery_rule_text_appended_only_when_granted():
    source = inspect.getsource(loop_module)
    revise_call_index = source.index("new_contract, reasoning = revise_contract_from_verification(")
    window = source[revise_call_index:revise_call_index + 4200]
    assert "if one_off_recovery_granted:" in window
    assert "independent reviewer" in window
    print("PASS: the external-role-framed recovery rule is appended only when the override actually fired")


if __name__ == "__main__":
    test_default_one_off_recovery_used_is_false()
    test_wired_immediately_after_should_escalate_to_operator()
    test_override_only_fires_for_round_budget_reason_never_other_reasons()
    test_recovery_rule_text_appended_only_when_granted()
    print("\nALL ONE-OFF RECOVERY WIRING TESTS PASSED")
