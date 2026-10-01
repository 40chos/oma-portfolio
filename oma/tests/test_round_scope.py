"""P12 Tier S/A item 6 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for contracts/scope.py's RoundScope/derive_round_scope(), and for the three
Code-Review filter functions it was built to feed (replacing the triplicated
`_REVIEW_NOT_YET_IN_SCOPE_RE.search(goal or "")` regex re-derivation). Pure, deterministic,
zero live calls -- direct Pydantic construction only.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from contracts.scope import RoundScope, derive_round_scope
from specialists.code_review.specialist import (
    ReviewFinding,
    _filter_hallucinated_empty_security_csv_findings,
    _filter_hallucinated_out_of_scope_field_required_findings,
    _filter_hallucinated_stale_scope_exclusion_findings,
)


def _contract(**overrides) -> TaskContract:
    base = dict(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal="x", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )
    base.update(overrides)
    return TaskContract(**base)


def _finding(explanation: str, severity: str = "blocking") -> ReviewFinding:
    return ReviewFinding(location="models/models.py", explanation=explanation, severity=severity)


# --- derive_round_scope() ---------------------------------------------------

def test_derive_round_scope_reads_structured_fields_directly():
    contract = _contract(
        current_constraint_label="computed_age_field",
        remaining_constraint_labels=["Security_Groups", " record_rules "],
        constraint_status={"student_model_fields": "satisfied", "security_groups": "pending"},
    )
    scope = derive_round_scope(contract)
    assert scope.current_focus == "computed_age_field"
    assert scope.not_yet_in_scope == ["security_groups", "record_rules"]
    assert scope.is_decomposed is True
    print("PASS: derive_round_scope() reads current_constraint_label/remaining_constraint_labels directly, lower-cased and stripped")


def test_derive_round_scope_non_decomposed_task_is_empty_and_flagged():
    contract = _contract()
    scope = derive_round_scope(contract)
    assert scope.current_focus is None
    assert scope.not_yet_in_scope == []
    assert scope.is_decomposed is False
    print("PASS: a plain, non-decomposed task gets an empty RoundScope with is_decomposed=False")


# --- _filter_hallucinated_out_of_scope_field_required_findings() -----------

def test_out_of_scope_field_required_downgraded_via_scope_not_regex():
    scope = RoundScope(current_focus="student_model_fields", not_yet_in_scope=["computed_age_field"], is_decomposed=True)
    findings = [_finding("Field 'age' is missing from the model definition, violating the round constraint")]
    filtered = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal="irrelevant prose", scope=scope)
    assert filtered[0].severity == "info"
    print("PASS: out-of-scope-field-required finding downgraded using RoundScope, independent of goal prose")


def test_out_of_scope_field_required_not_downgraded_when_field_not_deferred():
    scope = RoundScope(current_focus="x", not_yet_in_scope=["security_groups"], is_decomposed=True)
    findings = [_finding("Field 'age' is missing from the model definition")]
    filtered = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal="irrelevant", scope=scope)
    assert filtered[0].severity == "blocking"
    print("PASS: a genuine missing-field finding for a field NOT deferred is left untouched")


def test_out_of_scope_field_required_falls_back_to_regex_when_scope_is_none():
    goal = "This round's own NEW focus is ONLY: 'student_model_fields'.\nFields NOT yet in scope for this round: ['computed_age_field']"
    findings = [_finding("Field 'age' is missing from the model definition, violating the round constraint")]
    filtered = _filter_hallucinated_out_of_scope_field_required_findings(findings, goal=goal, scope=None)
    assert filtered[0].severity == "info"
    print("PASS: with scope=None, the original goal-regex fallback still works unchanged")


# --- _filter_hallucinated_stale_scope_exclusion_findings() -----------------

def test_stale_scope_exclusion_downgraded_via_scope():
    scope = RoundScope(current_focus="x", not_yet_in_scope=["security_groups", "record_rules"], is_decomposed=True)
    findings = [_finding("'student_views' is explicitly NOT in scope for this round")]
    filtered = _filter_hallucinated_stale_scope_exclusion_findings(findings, goal="irrelevant", scope=scope)
    assert filtered[0].severity == "info"
    print("PASS: a stale scope-exclusion claim (label not genuinely deferred) is downgraded via RoundScope")


def test_stale_scope_exclusion_left_alone_when_label_genuinely_deferred():
    scope = RoundScope(current_focus="x", not_yet_in_scope=["student_views"], is_decomposed=True)
    findings = [_finding("'student_views' is explicitly NOT in scope for this round")]
    filtered = _filter_hallucinated_stale_scope_exclusion_findings(findings, goal="irrelevant", scope=scope)
    assert filtered[0].severity == "blocking"
    print("PASS: a genuinely still-deferred label's exclusion claim is left untouched")


def test_stale_scope_exclusion_non_decomposed_task_is_a_no_op():
    scope = RoundScope(current_focus=None, not_yet_in_scope=[], is_decomposed=False)
    findings = [_finding("'student_views' is explicitly NOT in scope for this round")]
    filtered = _filter_hallucinated_stale_scope_exclusion_findings(findings, goal="irrelevant", scope=scope)
    assert filtered[0].severity == "blocking", "a non-decomposed task never had scope-exclusion prose to begin with"
    print("PASS: a non-decomposed task's scope is a pure no-op, same as the original goal-regex 'no match' path")


# --- _filter_hallucinated_empty_security_csv_findings() --------------------

def test_empty_security_csv_downgrade_uses_scope_for_decomposition_check():
    models_py = "class StudentModel(models.Model):\n    _name = 'student.model'\n"
    files = {"models/models.py": models_py, "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"}
    scope = RoundScope(current_focus="student_model_fields", not_yet_in_scope=["security_groups"], is_decomposed=True)
    findings = [_finding("The access control file is empty, which will cause a module installation error")]
    filtered = _filter_hallucinated_empty_security_csv_findings(findings, files, goal="irrelevant prose", scope=scope)
    assert filtered[0].severity == "info"
    print("PASS: header-only security CSV downgrade for a decomposed, non-security-focused round uses RoundScope, not goal regex")


def test_empty_security_csv_not_downgraded_when_not_decomposed():
    models_py = "class StudentModel(models.Model):\n    _name = 'student.model'\n"
    files = {"models/models.py": models_py, "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"}
    scope = RoundScope(current_focus=None, not_yet_in_scope=[], is_decomposed=False)
    findings = [_finding("The access control file is empty, which will cause a module installation error")]
    filtered = _filter_hallucinated_empty_security_csv_findings(findings, files, goal="irrelevant", scope=scope)
    assert filtered[0].severity == "blocking", "a genuinely new model, NOT part of a decomposed deferral, really does need real access rows"
    print("PASS: a non-decomposed new-model round's genuinely-required access rows finding is left untouched")


if __name__ == "__main__":
    test_derive_round_scope_reads_structured_fields_directly()
    test_derive_round_scope_non_decomposed_task_is_empty_and_flagged()
    test_out_of_scope_field_required_downgraded_via_scope_not_regex()
    test_out_of_scope_field_required_not_downgraded_when_field_not_deferred()
    test_out_of_scope_field_required_falls_back_to_regex_when_scope_is_none()
    test_stale_scope_exclusion_downgraded_via_scope()
    test_stale_scope_exclusion_left_alone_when_label_genuinely_deferred()
    test_stale_scope_exclusion_non_decomposed_task_is_a_no_op()
    test_empty_security_csv_downgrade_uses_scope_for_decomposition_check()
    test_empty_security_csv_not_downgraded_when_not_decomposed()
    print("\nALL ROUND-SCOPE TESTS PASSED")


# --- _filter_hallucinated_functionally_empty_findings() (P12 item 8) -------

from specialists.code_review.specialist import _filter_hallucinated_functionally_empty_findings


def test_functionally_empty_claim_downgraded_when_focus_keywords_present_in_files():
    scope = RoundScope(current_focus="computed_age_field", not_yet_in_scope=[], is_decomposed=True)
    files = {"models/models.py": "computed_age_field = fields.Integer(compute='_compute_age')\n"}
    findings = [_finding("This round's diff is functionally empty -- it adds nothing that satisfies the goal")]
    filtered = _filter_hallucinated_functionally_empty_findings(findings, files, scope)
    assert filtered[0].severity == "info"
    print("PASS: a 'functionally empty' claim is downgraded when the round's own focus keywords genuinely appear in the real generated content")


def test_functionally_empty_claim_survives_a_novel_rewording():
    scope = RoundScope(current_focus="computed_age_field", not_yet_in_scope=[], is_decomposed=True)
    files = {"models/models.py": "computed_age_field = fields.Integer(compute='_compute_age')\n"}
    findings = [_finding("This change does not meaningfully progress toward the stated goal")]
    filtered = _filter_hallucinated_functionally_empty_findings(findings, files, scope)
    assert filtered[0].severity == "info"
    print("PASS: a differently-worded 'no progress' claim is caught too, via structural evidence rather than an exact phrase match")


def test_functionally_empty_claim_not_downgraded_when_focus_keywords_absent():
    scope = RoundScope(current_focus="computed_age_field", not_yet_in_scope=[], is_decomposed=True)
    files = {"models/models.py": "class Foo(models.Model):\n    _name = 'x.y'\n"}
    findings = [_finding("This round's diff is functionally empty -- it adds nothing that satisfies the goal")]
    filtered = _filter_hallucinated_functionally_empty_findings(findings, files, scope)
    assert filtered[0].severity == "blocking"
    print("PASS: a genuinely empty diff (focus keywords really absent) is left untouched, conservative default")


def test_unrelated_blocking_finding_never_touched():
    scope = RoundScope(current_focus="computed_age_field", not_yet_in_scope=[], is_decomposed=True)
    files = {"models/models.py": "computed_age_field = fields.Integer(compute='_compute_age')\n"}
    findings = [_finding("Hardcoded API key found in source")]
    filtered = _filter_hallucinated_functionally_empty_findings(findings, files, scope)
    assert filtered[0].severity == "blocking"
    print("PASS: a genuinely unrelated blocking finding (not an emptiness claim) is never touched")


def test_no_scope_is_a_no_op():
    files = {"models/models.py": "computed_age_field = fields.Integer(compute='_compute_age')\n"}
    findings = [_finding("This round's diff is functionally empty")]
    filtered = _filter_hallucinated_functionally_empty_findings(findings, files, None)
    assert filtered[0].severity == "blocking"
    print("PASS: scope=None is a no-op, same conservative default as every other RoundScope-consuming filter")


if __name__ == "__main__":
    test_functionally_empty_claim_downgraded_when_focus_keywords_present_in_files()
    test_functionally_empty_claim_survives_a_novel_rewording()
    test_functionally_empty_claim_not_downgraded_when_focus_keywords_absent()
    test_unrelated_blocking_finding_never_touched()
    test_no_scope_is_a_no_op()
    print("ALL FUNCTIONALLY-EMPTY-FINDINGS TESTS PASSED")
