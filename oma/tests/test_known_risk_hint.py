"""P13 item 6 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2): tests for manager/memory.py's derive_known_risk_hint() -- the deterministic, zero-LLM half
of the new "why is this task likely to go wrong" task-spec-enrichment element. Pure, synchronous,
no DB/LLM calls -- MemoryRow objects constructed directly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.memory import MemoryRow, derive_known_risk_hint


def _row(event_type="outcome", module="my_module", tags=None, summary="something failed"):
    return MemoryRow(
        event_type=event_type, module=module, tags=tags or [], actor="build",
        summary=summary, verified=True,
    )


def test_no_module_identity_returns_none():
    rows = [_row(tags=["failed"])]
    assert derive_known_risk_hint(rows, None) is None
    print("PASS: no module_identity means no hint, never guessed")


def test_no_matching_rows_returns_none():
    rows = [_row(module="other_module", tags=["failed"])]
    assert derive_known_risk_hint(rows, "my_module") is None
    print("PASS: no matching module means no hint")


def test_non_outcome_event_type_is_ignored():
    rows = [_row(event_type="replan_round", module="my_module", tags=["failed"])]
    assert derive_known_risk_hint(rows, "my_module") is None
    print("PASS: only event_type=='outcome' rows count, matching check_repeated_failures()'s own shape")


def test_outcome_without_failed_tag_is_ignored():
    rows = [_row(event_type="outcome", module="my_module", tags=["passed"])]
    assert derive_known_risk_hint(rows, "my_module") is None
    print("PASS: an outcome row without 'failed' in tags is not treated as a failure record")


def test_real_matching_failed_outcome_produces_a_hint():
    rows = [_row(event_type="outcome", module="my_module", tags=["failed"], summary="cron method used wrong self-reference")]
    hint = derive_known_risk_hint(rows, "my_module")
    assert hint is not None
    assert "my_module" in hint
    assert "cron method used wrong self-reference" in hint
    print("PASS: a real matching failed outcome produces a real, grounded hint")


def test_code_review_verdict_clause_is_stripped_but_real_signal_survives():
    """Real, general bug found live (2026-08-07, HUMAN_DECISION deep-push, confirmed
    independently on both task039 and task019, real task_ids d15e3e66-71fb-4b1a-aa65-4f6c0f743cd7
    and d7e7f077-7046-4cd9-a1c4-633afb0f9e79): a "failed" outcome row's own Code-Review-verdict
    clause is, by construction, never more than an unverified specialist self-report (every such
    row is written with verified=False) -- and this exact shape was independently confirmed,
    live, to self-poison a LATER round by repeating the same specialist's own wrong verdict as if
    it were settled fact. Confirmed via redis on task019: a round whose own constraint didn't even
    cover the flagged method yet still received this exact text as "risk history."

    Deliberately surgical, not a blanket row-drop: the real summary here is a COMBINED string --
    Testing/QA's own genuinely reliable "Reproduction confirmed ... Spot-check matches the self-
    report" (a deterministic check against real Postgres/schema state) followed by Code-Review's
    own unreliable verdict. Only the Code-Review clause must be stripped; the real signal before
    it must still surface.
    """
    rows = [
        _row(
            event_type="outcome", module="project.fieldjob", tags=["failed"],
            summary=(
                "Reproduction confirmed for project.fieldjob.customer_grouping_rule. Spot-check "
                "matches the self-report. Code-Review found 1 blocking issue(s): The method "
                "action_create_batch_invoice is missing; the task goal explicitly requires it to "
                "be implemented on project.fieldjob."
            ),
        ),
    ]
    hint = derive_known_risk_hint(rows, "project.fieldjob")
    assert hint is not None, (
        "the real, deterministic Testing/QA signal preceding the Code-Review clause must still "
        "surface -- the whole row must not be discarded just because part of it is unreliable"
    )
    assert (
        "Reproduction confirmed for project.fieldjob.customer_grouping_rule. Spot-check "
        "matches the self-report." in hint
    ), f"expected the real Testing/QA signal to survive stripping -- got:\n{hint}"
    assert "Code-Review found" not in hint, (
        f"the unreliable Code-Review verdict clause must be stripped -- got:\n{hint}"
    )
    assert "action_create_batch_invoice" not in hint, (
        f"the specific wrong claim from the stripped clause must not leak through -- got:\n{hint}"
    )
    print("PASS: the unreliable Code-Review verdict clause is stripped; the real, preceding "
          "Testing/QA signal from the same combined summary still surfaces")


def test_summary_that_is_entirely_a_code_review_verdict_is_dropped_completely():
    rows = [
        _row(
            event_type="outcome", module="my_module", tags=["failed"],
            summary="Code-Review found 2 blocking issue(s): some wrong verdict text.",
        ),
        _row(
            event_type="outcome", module="my_module", tags=["failed"],
            summary="Install failed: TypeError, model does not exist in registry.",
        ),
    ]
    hint = derive_known_risk_hint(rows, "my_module")
    assert hint is not None
    assert "Install failed" in hint
    assert "Code-Review found" not in hint
    assert "some wrong verdict text" not in hint
    print("PASS: a summary that is ENTIRELY a Code-Review verdict (nothing real precedes it) is "
          "dropped completely; a genuine, unrelated failure summary still surfaces")


def test_limit_caps_number_of_summaries_included():
    rows = [
        _row(event_type="outcome", module="my_module", tags=["failed"], summary=f"failure {i}")
        for i in range(5)
    ]
    hint = derive_known_risk_hint(rows, "my_module", limit=2)
    included_summaries = hint.split(": ", 1)[1].split("; ")
    assert included_summaries == ["failure 0", "failure 1"], included_summaries
    print("PASS: limit caps how many prior-failure summaries are folded into the hint")


if __name__ == "__main__":
    test_no_module_identity_returns_none()
    test_no_matching_rows_returns_none()
    test_non_outcome_event_type_is_ignored()
    test_outcome_without_failed_tag_is_ignored()
    test_real_matching_failed_outcome_produces_a_hint()
    test_code_review_verdict_clause_is_stripped_but_real_signal_survives()
    test_summary_that_is_entirely_a_code_review_verdict_is_dropped_completely()
    test_limit_caps_number_of_summaries_included()
    print("\nALL KNOWN-RISK-HINT DERIVATION TESTS PASSED")
