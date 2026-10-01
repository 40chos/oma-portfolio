"""Phase 20 Area 2 (2026-07-19): unit tests for
specialists/code_review/specialist.py's
_filter_hallucinated_scope_findings -- pure logic, no live gateway
needed, same style as today's other isolated test files.

Real bug this fixes: a prose-only prompt instruction (added earlier
the same day, asking Code-Review to "quote the exact phrase" before
flagging something out of scope) was confirmed, via a fresh live
reproduction, to NOT stop the hallucination: a task's real goal
explicitly said "adds a new security group... via its own access rule
row", yet Code-Review invented "violating the explicit round
constraint to exclude security records" on round 1 itself (no prior
rounds to draw the hallucination from), with nothing in the real goal
ever mentioning scope limits, and kept repeating the identical
hallucinated phrase verbatim across every round. Critical subtlety:
contract.rules cannot be trusted as a source of truth here, since
rules accumulate each round's own prior failure text verbatim -- a
round-1 hallucination becomes round 2's own "rules" input,
self-reinforcing indefinitely. Only contract.goal is immutable across
rounds and can be trusted.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from specialists.code_review.specialist import ReviewFinding, _filter_hallucinated_scope_findings


def _make_contract(goal: str, rules: list[str] | None = None) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(),
        specialist_type=SpecialistType.code_review,
        capability_class=CapabilityClass.readonly_investigation,
        tier=AutonomyTier.tier_1_readonly,
        goal=goal,
        inputs=[],
        rules=rules or [],
        deliverables=[],
        compensating_actions=[],
        validation_by="testing_qa",
        pause_if=[],
        turn_budget=10,
    )


def test_downgrades_hallucinated_scope_finding_not_in_real_goal():
    contract = _make_contract(
        "Create a small new Odoo module that defines a brand new custom model called "
        "service.ticket, and adds a new security group called 'Service Ticket Managers' "
        "with full read, write, create, and delete access to this new model via its own "
        "access rule row."
    )
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation="Contains a security access record, violating the explicit round "
                        "constraint to exclude security records.",
        ),
    ]
    result = _filter_hallucinated_scope_findings(findings, contract)
    assert result[0].severity == "info"
    assert "Downgraded" in result[0].explanation
    print("PASS: a hallucinated scope-constraint finding not present in the real goal is downgraded from blocking")


def test_keeps_genuine_finding_when_goal_states_real_scope_limit():
    contract = _make_contract(
        "Add a single new field 'preferred_language' to res.partner. Scope: only add the "
        "field this round, do NOT add any view changes or security records -- those are a "
        "separate, later round."
    )
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation="Contains a security access record, violating the explicit round "
                        "constraint to exclude security records this round.",
        ),
    ]
    result = _filter_hallucinated_scope_findings(findings, contract)
    assert result[0].severity == "blocking"
    print("PASS: a finding is trusted as-is when the real goal genuinely describes a scope limit")


def test_non_scope_blocking_findings_are_never_touched():
    contract = _make_contract("Add a field to res.partner.")
    findings = [
        ReviewFinding(location="models/models.py", severity="blocking", explanation="Invented a nonexistent field on an existing model."),
    ]
    result = _filter_hallucinated_scope_findings(findings, contract)
    assert result[0].severity == "blocking"
    assert result[0].explanation == "Invented a nonexistent field on an existing model."
    print("PASS: a genuine, unrelated blocking finding is never touched by this filter")


def test_info_and_minor_findings_are_never_touched_even_with_scope_language():
    contract = _make_contract("Add a field to res.partner.")
    findings = [
        ReviewFinding(location="x", severity="minor", explanation="Might be slightly out of scope, worth a second look."),
    ]
    result = _filter_hallucinated_scope_findings(findings, contract)
    assert result[0].severity == "minor"
    print("PASS: a non-blocking finding is left untouched regardless of scope-shaped language")


def test_accumulated_rules_never_trusted_as_a_source_of_the_hallucination():
    """The critical subtlety: rules accumulate PRIOR ROUNDS' OWN
    hallucinated text verbatim -- this must never be treated as
    confirming the constraint is real.
    """
    contract = _make_contract(
        "Create a small new Odoo module that adds a new security group with access to a model.",
        rules=[
            "Prior attempt (round 1) failed: Code-Review found: violating the explicit round "
            "constraint to exclude security records.",
        ],
    )
    findings = [
        ReviewFinding(
            location="security/ir.model.access.csv", severity="blocking",
            explanation="Contains a security access record, violating the explicit round constraint.",
        ),
    ]
    result = _filter_hallucinated_scope_findings(findings, contract)
    assert result[0].severity == "info"
    print("PASS: a hallucination echoed back into rules from a prior round is still correctly downgraded")


if __name__ == "__main__":
    test_downgrades_hallucinated_scope_finding_not_in_real_goal()
    test_keeps_genuine_finding_when_goal_states_real_scope_limit()
    test_non_scope_blocking_findings_are_never_touched()
    test_info_and_minor_findings_are_never_touched_even_with_scope_language()
    test_accumulated_rules_never_trusted_as_a_source_of_the_hallucination()
    print("\nALL CODE-REVIEW SCOPE-HALLUCINATION FILTER TESTS PASSED")
