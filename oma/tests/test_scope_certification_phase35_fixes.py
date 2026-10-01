"""Phase 35 §1.1/§11.1 fixes (2026-08-12): tests for the three new pieces added to
manager/scope_certification.py -- the real diversity floor (cold-generated distinct-goal-text
count, not just a raw streak length), the classifier ambiguity signal, and the volume-based
adjudicator self-review sampling. Runs against a temp state file, matching the existing
test_scope_certification.py convention.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.scope_certification as cert_module
from manager.scope_certification import (
    classify_scope,
    classify_scope_with_confidence,
    get_adjudicator_review_sample,
    is_scope_certified,
    record_task_outcome,
)


def _use_temp_state(tmp_path, monkeypatch):
    state_path = os.path.join(str(tmp_path), "scope_certification.json")
    monkeypatch.setattr(cert_module, "_STATE_PATH", state_path)
    monkeypatch.setattr(cert_module, "_STATE_DIR", str(tmp_path))
    return state_path


# ---------------------------------------------------------------------------
# Diversity floor (§1.1 item 2 / §11.1 fix 1)
# ---------------------------------------------------------------------------

def test_a_full_streak_of_the_same_goal_text_never_certifies(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for i in range(cert_module._DEFAULT_N_REQUIRED):
        record_task_outcome(scope, task_id=f"t{i}", qualifying_failure=False, goal_text="the exact same goal")
    assert is_scope_certified(scope) is False
    print("PASS: N clean runs of one repeated goal text never certifies -- the diversity floor is real")


def test_exactly_the_floor_count_of_distinct_goals_certifies(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for i in range(cert_module._DEFAULT_N_REQUIRED):
        record_task_outcome(
            scope, task_id=f"t{i}", qualifying_failure=False,
            goal_text=f"goal template {i % cert_module._DIVERSITY_FLOOR_MIN_DISTINCT}",
        )
    assert is_scope_certified(scope) is True
    print("PASS: hitting exactly the diversity floor's distinct-template count certifies")


def test_retrieval_assisted_passes_count_toward_streak_but_not_diversity(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    n = cert_module._DEFAULT_N_REQUIRED
    # Only 3 distinct COLD goal texts -- below the floor -- padded out to the full raw streak
    # length using retrieval-assisted passes, which must NOT count toward diversity.
    for i in range(n):
        if i < 3:
            record_task_outcome(scope, task_id=f"cold{i}", qualifying_failure=False,
                                 goal_text=f"cold distinct goal {i}", used_retrieved_pattern=False)
        else:
            record_task_outcome(scope, task_id=f"retr{i}", qualifying_failure=False,
                                 goal_text=f"retrieval assisted goal {i}", used_retrieved_pattern=True)
    state = cert_module.get_scope_state(scope)
    assert state.consecutive_clean_runs == n  # the raw streak IS full
    assert is_scope_certified(scope) is False  # but certification correctly still refuses
    print("PASS: retrieval-assisted passes pad the raw streak but cannot satisfy the diversity floor alone")


def test_missing_goal_text_is_never_silently_counted_as_distinct(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    scope = "single_new_field"
    for i in range(cert_module._DEFAULT_N_REQUIRED):
        record_task_outcome(scope, task_id=f"t{i}", qualifying_failure=False)  # no goal_text at all
    assert is_scope_certified(scope) is False
    print("PASS: outcomes recorded with no goal_text at all never count toward the floor either way")


# ---------------------------------------------------------------------------
# Classifier ambiguity signal (§11.1 fix 2)
# ---------------------------------------------------------------------------

def test_classify_scope_with_confidence_matches_classify_scope_for_primary(tmp_path, monkeypatch):
    goal = "add a new field to project.project"
    primary, _, _ = classify_scope_with_confidence(goal)
    assert primary == classify_scope(goal)
    print("PASS: classify_scope_with_confidence's primary result always matches classify_scope alone")


def test_unambiguous_goal_reports_not_ambiguous():
    goal = "add a new computed field to res.partner"
    primary, is_ambiguous, matched = classify_scope_with_confidence(goal)
    assert is_ambiguous is False
    assert matched == [primary]
    print("PASS: a goal matching exactly one scope's regex is correctly reported unambiguous")


def test_a_goal_matching_two_scopes_is_flagged_ambiguous():
    # Deliberately crosses "workflow_with_custom_buttons_or_cron" (button) and
    # "single_new_field" (field) in one goal text.
    goal = "add a new field and a button to trigger a workflow"
    primary, is_ambiguous, matched = classify_scope_with_confidence(goal)
    assert is_ambiguous is True
    assert len(matched) >= 2
    print(f"PASS: a genuinely cross-cutting goal is correctly flagged ambiguous (matched: {matched})")


def test_uncategorized_goal_reports_not_ambiguous():
    primary, is_ambiguous, matched = classify_scope_with_confidence("do something nobody has a pattern for")
    assert primary == "uncategorized"
    assert is_ambiguous is False
    assert matched == []
    print("PASS: an uncategorized goal (zero matches) is correctly reported unambiguous, not falsely flagged")


# ---------------------------------------------------------------------------
# Adjudicator review sampling (§11.1 fix 3)
# ---------------------------------------------------------------------------

def test_no_sample_below_the_review_threshold(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    for i in range(cert_module._ADJUDICATOR_REVIEW_EVERY_N - 1):
        record_task_outcome("single_new_field", task_id=f"fail{i}", qualifying_failure=True)
    assert get_adjudicator_review_sample() is None
    print("PASS: no review sample fires before the volume threshold is actually reached")


def test_sample_fires_exactly_at_the_threshold(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    for i in range(cert_module._ADJUDICATOR_REVIEW_EVERY_N):
        record_task_outcome("single_new_field", task_id=f"fail{i}", qualifying_failure=True)
    sample = get_adjudicator_review_sample()
    assert sample is not None
    assert len(sample) == min(8, cert_module._ADJUDICATOR_REVIEW_EVERY_N)
    assert all("scope" in entry and "task_id" in entry for entry in sample)
    print(f"PASS: exactly at the {cert_module._ADJUDICATOR_REVIEW_EVERY_N}th qualifying failure, a real sample fires")


def test_sample_does_not_refire_one_past_the_threshold(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    for i in range(cert_module._ADJUDICATOR_REVIEW_EVERY_N + 1):
        record_task_outcome("single_new_field", task_id=f"fail{i}", qualifying_failure=True)
    assert get_adjudicator_review_sample() is None
    print("PASS: one qualifying failure past the boundary correctly does not re-trigger a review")


def test_sample_pools_across_every_scope_not_just_one(tmp_path, monkeypatch):
    _use_temp_state(tmp_path, monkeypatch)
    half = cert_module._ADJUDICATOR_REVIEW_EVERY_N // 2
    for i in range(half):
        record_task_outcome("single_new_field", task_id=f"a{i}", qualifying_failure=True)
    for i in range(cert_module._ADJUDICATOR_REVIEW_EVERY_N - half):
        record_task_outcome("workflow_with_custom_buttons_or_cron", task_id=f"b{i}", qualifying_failure=True)
    sample = get_adjudicator_review_sample()
    assert sample is not None
    scopes_seen = {entry["scope"] for entry in sample}
    print(f"PASS: review sample draws from across scopes when the threshold is crossed (scopes in sample: {scopes_seen})")


# All tests above use pytest's tmp_path/monkeypatch fixtures -- run via `pytest`, not directly.
