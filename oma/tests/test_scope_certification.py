"""Phase 32 implementation (2026-08-11): unit tests for
manager/scope_certification.py -- the bake-in / consecutive-clean-run
certification mechanism from Phase 32 sections 2 and 3.2. Runs against a
temp state file (never the real state/scope_certification.json) so tests
never interfere with real accumulated task history.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.scope_certification as cert_module
from manager.scope_certification import (
    classify_scope,
    get_scope_state,
    is_scope_certified,
    record_task_outcome,
    reset_scope_backlog,
)


def _use_temp_state(tmp_path, monkeypatch):
    state_path = os.path.join(str(tmp_path), "scope_certification.json")
    monkeypatch.setattr(cert_module, "_STATE_PATH", state_path)
    monkeypatch.setattr(cert_module, "_STATE_DIR", str(tmp_path))
    return state_path


def test_classify_scope_single_new_field():
    assert classify_scope("Add a new computed field 'total_hours' to the ticket") == "single_new_field"


def test_classify_scope_workflow_with_buttons():
    assert classify_scope('Add "Start" and "Resolve" buttons and a daily escalation cron') == "workflow_with_custom_buttons_or_cron"


def test_classify_scope_new_model_with_crud():
    assert classify_scope("Create a new model oma.equipment with ir.model.access rows and a security group") == "new_model_with_crud_and_security"


def test_classify_scope_new_self_contained_module_matches_the_mined_safe_pattern():
    """Mined 2026-08-11 from real production history: this exact phrasing shape passed 29/29
    real runs. Must classify into its own bucket, not the broader new_model_with_crud_and_security
    bucket (which also matches, but is checked second)."""
    goal = (
        "Create a small new Odoo module that defines a brand new custom model called "
        "warranty.claim (a simple warranty claim record with a claim_number and description field)"
    )
    assert classify_scope(goal) == "new_self_contained_module_basic_model_or_group"


def test_classify_scope_new_self_contained_module_group_variant():
    goal = "Create a small new Odoo module that adds a brand new security group called 'Tasks Editors'"
    assert classify_scope(goal) == "new_self_contained_module_basic_model_or_group"


def test_classify_scope_quick_filter_is_its_own_bucket_not_workflow():
    """Phase 33 §3 item 5: a quick-filter/search-domain goal must not land in the
    much harder workflow_with_custom_buttons_or_cron bucket just because it also
    says the word 'button' or 'filter'."""
    goal = "In the meerwerk list, I want a quick filter button called 'Accepted' that shows only accepted records."
    assert classify_scope(goal) == "quick_filter_search_construction"


def test_classify_scope_edit_existing_module_is_its_own_bucket_not_new_model():
    goal = "Edit the existing, already-installed module oma_build_a_complete_field_ab52b7f8 directly"
    assert classify_scope(goal) == "edit_existing_module_access_or_files"


def test_classify_scope_cross_module():
    assert classify_scope("Modify behavior across the project and helpdesk modules") == "cross_module_modifications"


def test_classify_scope_uncategorized_when_nothing_matches():
    assert classify_scope("Rename a label in the settings menu") == "uncategorized"


def test_classify_scope_record_rule_row_level_security():
    """2026-08-15: the first of the 25-direction roadmap's 18 not-yet-implemented buckets --
    was previously falling into 'uncategorized' with no dedicated tracking."""
    assert classify_scope(
        "Add a record rule so salespeople can only see crm.lead records assigned to them"
    ) == "record_rule_row_level_security"
    assert classify_scope(
        "Restrict row-level security on the project.task model for the 'Contractor' group"
    ) == "record_rule_row_level_security"


def test_classify_scope_record_rule_checked_before_edit_existing_module():
    goal = "Edit the existing project module: add a record rule restricting task visibility"
    assert classify_scope(goal) == "record_rule_row_level_security"


def test_classify_scope_field_level_validation_constraint():
    """2026-08-15: second of the 25-direction roadmap's not-yet-implemented buckets --
    previously fell into 'uncategorized', or worse, was swallowed by single_new_field
    (since a validation-constraint goal is usually ALSO phrased as 'add a field...')."""
    assert classify_scope(
        "Add a validation constraint so the end_date field must be after the start_date field"
    ) == "field_level_validation_constraint"
    assert classify_scope(
        "Add a field 'discount' and use api.constrains to validate it never exceeds 50%"
    ) == "field_level_validation_constraint"


def test_classify_scope_related_dot_notation_field():
    """2026-08-15: third of the 25-direction roadmap's not-yet-implemented buckets."""
    assert classify_scope(
        "Add a related field on sale.order that shows partner_id.email using dot-notation"
    ) == "related_dot_notation_field"


def test_classify_scope_validation_constraint_and_related_field_checked_before_single_new_field():
    """Both new buckets are typically ALSO phrased as 'add a field...' -- must be checked
    before the broader single_new_field bucket so they're tracked separately, not swallowed."""
    assert classify_scope(
        "Add a field 'total' with a validation constraint ensuring it is never negative"
    ) == "field_level_validation_constraint"
    assert classify_scope(
        "Add a related field 'partner_email' related to 'partner_id.email'"
    ) == "related_dot_notation_field"


def test_classify_scope_menu_visibility_restriction():
    """2026-08-15: fourth of the 25-direction roadmap's not-yet-implemented buckets
    (row 8, N=13 historical, 62% pass -- one of the higher historical rates among the
    untracked directions)."""
    assert classify_scope(
        "Restrict menu visibility for the Accounting menu to the Finance Manager group"
    ) == "menu_visibility_restriction"
    assert classify_scope(
        "Hide the Settings menu from users who are not in the base.group_system group"
    ) == "menu_visibility_restriction"


def test_classify_scope_custom_security_group_field_or_button_restriction():
    """2026-08-16: fifth new bucket -- roadmap Tier 0a ('Field/ACL visibility restriction on an
    existing model', N=90 historical, the single highest-volume untracked shape on the whole
    25-direction table) / Tier 1 item 4 ('New custom security-group creation'). Deliberately
    distinct from record_rule_row_level_security: this shape has no record-rule component at
    all, just a new group applied to restrict a field or button's visibility -- avoiding the
    real ask_operator/gates_disagree scope-mismatch this session found record_rule_row_level_
    security's own bundled goal shape triggers."""
    assert classify_scope(
        "Add a new custom security group named 'Payroll Viewer' and restrict the salary field "
        "on hr.employee so only members of that group can see it"
    ) == "custom_security_group_field_or_button_restriction"
    assert classify_scope(
        "Create a security group 'Approver' and apply it to restrict the Approve button on "
        "purchase.order so only members can click it"
    ) == "custom_security_group_field_or_button_restriction"


def test_classify_scope_custom_security_group_restriction_checked_before_new_model_bucket():
    """A goal naming a new security group with no 'new model'/'record rule' wording would
    otherwise be swallowed by the broader new_model_with_crud_and_security bucket's bare
    `security group` keyword -- must be tracked separately since it's a distinct, narrower,
    single-round-friendly shape."""
    goal = (
        "Add a new security group called 'Confidential Notes Viewer' and restrict the "
        "internal_notes field on crm.lead so only that group can view it"
    )
    assert classify_scope(goal) == "custom_security_group_field_or_button_restriction"


def test_classify_scope_record_rule_still_wins_when_goal_bundles_both():
    """A goal that bundles BOTH a new security group AND a record rule (this session's own
    record_rule_row_level_security wave-goal template) must still classify as
    record_rule_row_level_security, not the new narrower bucket -- record_rule_row_level_
    security is checked first in the pattern list precisely for this reason."""
    goal = (
        "Add a new security group named 'Shipping Reviewer' scoped to the stock.picking model, "
        "with a real ir.model.access.csv row granting read/write/create access. Add a record "
        "rule that applies ONLY to members of the 'Shipping Reviewer' group, restricting them "
        "to see only pickings belonging to their own company."
    )
    assert classify_scope(goal) == "record_rule_row_level_security"


def test_certification_reached_after_n_clean_runs(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for i in range(cert_module._DEFAULT_N_REQUIRED):
        record_task_outcome(
            scope, task_id=f"t{i}", qualifying_failure=False,
            goal_text=f"add a distinct field number {i % cert_module._DIVERSITY_FLOOR_MIN_DISTINCT}",
        )
    assert is_scope_certified(scope) is True
    print("PASS: N consecutive clean runs certifies the scope")


def test_one_short_of_n_is_not_yet_certified(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for i in range(cert_module._DEFAULT_N_REQUIRED - 1):
        record_task_outcome(scope, task_id=f"t{i}", qualifying_failure=False)
    assert is_scope_certified(scope) is False
    print("PASS: N-1 clean runs is not yet certified")


def test_qualifying_failure_resets_streak_and_doubles_threshold(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for i in range(cert_module._DEFAULT_N_REQUIRED):
        record_task_outcome(
            scope, task_id=f"t{i}", qualifying_failure=False,
            goal_text=f"add a distinct field number {i % cert_module._DIVERSITY_FLOOR_MIN_DISTINCT}",
        )
    assert is_scope_certified(scope) is True

    state = record_task_outcome(scope, task_id="fail-1", qualifying_failure=True)
    assert state.consecutive_clean_runs == 0
    assert state.n_required == cert_module._DEFAULT_N_REQUIRED * 2
    assert is_scope_certified(scope) is False, "certification must be revoked immediately on a qualifying failure"
    print("PASS: a qualifying failure resets the streak, revokes certification, and doubles the threshold")


def test_escalate_bar_false_resets_streak_without_doubling_threshold(tmp_path, monkeypatch):
    """Real overnight incident (2026-08-14): a stale-registry-only warning (real, but a
    timing/propagation-lag fact about the environment, not evidence the generated code is
    unreliable) doubled a direction's bar three times in one night purely from the same
    root cause recurring. escalate_bar=False is the fix -- a caller passes it when it can
    show the qualifying failure came from a signal that says nothing about generation
    quality; the streak still resets (honest), but the bar stays put.
    """
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for i in range(cert_module._DEFAULT_N_REQUIRED):
        record_task_outcome(
            scope, task_id=f"t{i}", qualifying_failure=False,
            goal_text=f"add a distinct field number {i % cert_module._DIVERSITY_FLOOR_MIN_DISTINCT}",
        )
    assert is_scope_certified(scope) is True

    state = record_task_outcome(scope, task_id="stale-1", qualifying_failure=True, escalate_bar=False)
    assert state.consecutive_clean_runs == 0, "the streak must still reset -- this run didn't cleanly confirm"
    assert state.n_required == cert_module._DEFAULT_N_REQUIRED, "the bar must NOT double for a non-generation-quality signal"
    assert state.escalations_used == 0, "no escalation should be consumed either"
    assert is_scope_certified(scope) is False, "certification is still revoked -- this is a real reset, just not punitive"
    print("PASS: escalate_bar=False resets the streak honestly without doubling the required bar")


def test_escalate_bar_true_is_the_default_and_matches_old_behavior(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    state = record_task_outcome(scope, task_id="fail-1", qualifying_failure=True)
    assert state.n_required == cert_module._DEFAULT_N_REQUIRED * 2, "default must preserve the original escalating behavior"
    print("PASS: escalate_bar defaults to True, zero behavior change for existing callers")


def test_repeated_non_escalating_failures_never_inflate_the_bar(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for i in range(5):
        state = record_task_outcome(scope, task_id=f"stale-{i}", qualifying_failure=True, escalate_bar=False)
    assert state.n_required == cert_module._DEFAULT_N_REQUIRED
    assert state.escalations_used == 0
    assert state.backlog is False
    print("PASS: repeated environmental-only failures never inflate the bar or exhaust the escalation cap")


def test_escalation_cap_reaches_visible_backlog_not_infinite_retry(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for escalation in range(cert_module._MAX_ESCALATIONS):
        record_task_outcome(scope, task_id=f"fail-{escalation}", qualifying_failure=True)
    state = get_scope_state(scope)
    assert state.backlog is False, "the cap-th failure itself should still just escalate"
    assert state.escalations_used == cert_module._MAX_ESCALATIONS

    state = record_task_outcome(scope, task_id="fail-final", qualifying_failure=True)
    assert state.backlog is True, "exhausting the escalation cap must land in a visible backlog state"
    assert is_scope_certified(scope) is False
    print("PASS: exhausting the escalation cap lands in a visible backlog state, never an infinite silent retry")


def test_reset_scope_backlog_is_the_only_way_out(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for escalation in range(cert_module._MAX_ESCALATIONS + 1):
        record_task_outcome(scope, task_id=f"fail-{escalation}", qualifying_failure=True)
    assert get_scope_state(scope).backlog is True

    reset_scope_backlog(scope)
    state = get_scope_state(scope)
    assert state.backlog is False
    assert state.n_required == cert_module._DEFAULT_N_REQUIRED
    assert state.escalations_used == 0
    print("PASS: reset_scope_backlog() is the one explicit, human-only way out of backlog")


def test_certification_expires_after_the_window(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=100)
    for i in range(cert_module._DEFAULT_N_REQUIRED):
        record_task_outcome(scope, task_id=f"t{i}", qualifying_failure=False, now=long_ago)
    assert is_scope_certified(scope, expiry_days=30) is False
    print("PASS: an old certification past the expiry window is no longer considered certified")


def test_unrelated_scope_is_never_certified_by_another_scopes_history(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    for i in range(cert_module._DEFAULT_N_REQUIRED):
        record_task_outcome("single_new_field", task_id=f"t{i}", qualifying_failure=False)
    assert is_scope_certified("workflow_with_custom_buttons_or_cron") is False
    print("PASS: certifying one scope never certifies an unrelated scope")


if __name__ == "__main__":
    print("(this file requires pytest's tmp_path/monkeypatch fixtures; run via pytest)")
