"""Phase 30, P3 (Phase G, §10, closes Problem G): tests for the hard,
deterministic pre-flight governance gate. `_predicate_matches()` is
pure and tested directly (no DB); `check_hard_governance_gates()`'s
real DB-reading half was verified live against the real,
just-migrated accounting rule rows instead (see docs/reports/
PHASE30_P3_GOVERNANCE_GATE_VERIFICATION_2026-07-30.md) -- these tests
lock in the predicate-evaluation logic itself, independent of DB state.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.governance import _predicate_matches

_ACCOUNTING_PREDICATE = {
    "type": "goal_text_matches_any",
    "patterns": [r"\baccount\.[a-z_]+\b", r"\baccounting\b"],
    "carve_out_patterns": [r"\barchiv", r"\bdeactivat", r"\bactive\s*=\s*false", r"\bsoft.?delet"],
}


def test_matches_the_real_flagship_historical_goal_with_no_model_header():
    """The exact real goal text (rejected by Operator 7+ times per the
    plan's own §1.7 evidence) that resolve_module_identity() -- the
    insertion point the plan's own text implied reusing -- returns None
    for, since it has no 'Model:' metadata line at all.
    """
    goal = "Modify the ir.model.access.csv permissions for account.move."
    assert _predicate_matches(goal, _ACCOUNTING_PREDICATE) is True
    print("PASS: matches the real flagship historical goal despite having no Model: header")


def test_matches_a_structured_model_header_goal_too():
    goal = "Field: internal_tracking_code\nModel: account.move\nModule: mis_base_extend"
    assert _predicate_matches(goal, _ACCOUNTING_PREDICATE) is True
    print("PASS: also matches a goal that DOES use the structured Model: convention")


def test_does_not_match_an_unrelated_ordinary_goal():
    goal = "Add a discount_reason field to sale.order to record why a discount was applied."
    assert _predicate_matches(goal, _ACCOUNTING_PREDICATE) is False
    print("PASS: an ordinary, unrelated goal is never falsely blocked")


def test_carve_out_exempts_a_clearly_reversible_archival_change():
    """Plan's own step 4 refinement: a fully reversible change (archiving
    an old record) must not be gated exactly as hard as a genuinely
    risky posting write.
    """
    goal = "Archive old account.move records older than 5 years by setting active=False."
    assert _predicate_matches(goal, _ACCOUNTING_PREDICATE) is False
    print("PASS: a clearly reversible/archival accounting change is correctly exempted")


def test_carve_out_does_not_exempt_a_genuine_posting_or_permission_change():
    goal = "Modify the ir.model.access.csv permissions for account.move."
    # Confirm the carve-out patterns themselves don't appear in this real
    # goal (a real, not just structural, negative -- this goal never
    # mentions archiving/deactivating anything).
    assert not any(w in goal.lower() for w in ("archiv", "deactivat", "active=false", "soft delet"))
    assert _predicate_matches(goal, _ACCOUNTING_PREDICATE) is True
    print("PASS: a genuine permission/posting change on account.move is still hard-gated")


def test_unrecognized_predicate_type_fails_open_never_blocks():
    predicate = {"type": "some_future_type_this_version_does_not_understand"}
    assert _predicate_matches("Modify account.move permissions", predicate) is False
    print("PASS: an unrecognized predicate type fails open rather than blocking blindly")


if __name__ == "__main__":
    test_matches_the_real_flagship_historical_goal_with_no_model_header()
    test_matches_a_structured_model_header_goal_too()
    test_does_not_match_an_unrelated_ordinary_goal()
    test_carve_out_exempts_a_clearly_reversible_archival_change()
    test_carve_out_does_not_exempt_a_genuine_posting_or_permission_change()
    test_unrecognized_predicate_type_fails_open_never_blocks()
    print("\nALL GOVERNANCE-HARD-GATE TESTS PASSED")
