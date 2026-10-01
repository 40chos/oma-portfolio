"""P12 Tier A item 22 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 25): regression guard confirming recent_round_summaries now folds in
manager_history_summary at the mid-loop-compression classification call site inside
run_turn(). Real, confirmed gap: prior_rounds is reset to [] on mid-loop compression, but the
real compressed summary of exactly those dropped rounds was never folded into
classify_root_cause()'s own context, so root-cause classification lost all visibility into any
round before the most recent compression.

This logic lives entirely inside run_turn()'s own local scope (recent_round_summaries/
manager_history_summary are function-local variables, not standalone testable units) --
run_turn() itself has extensive real dependencies (module locks, Redis, classification calls),
making a full live reproduction impractical here (same reasoning already established this
session for item 16's gateway-outage wrapping fix). This test confirms the fix is genuinely
present in source, at the correct call site, with the correct conditional guard -- a real
regression guard, not a live behavioral proof. Zero LLM/GPU calls -- pure source inspection.
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module


def test_recent_round_summaries_construction_folds_in_manager_history_summary():
    source = inspect.getsource(loop_module)
    match = re.search(
        r"recent_round_summaries = \[.*?\]\s*\n(.*?)\n\s*async def _step_root_cause",
        source, re.DOTALL,
    )
    assert match, "could not locate the recent_round_summaries construction block followed by _step_root_cause"
    block = match.group(1)
    assert "manager_history_summary" in block, (
        "the block between recent_round_summaries's initial construction and its use in "
        "_step_root_cause must reference manager_history_summary -- the fix that folds "
        "compressed round history back in"
    )
    assert "if manager_history_summary" in block, (
        "must be conditional on manager_history_summary actually having real content -- never "
        "unconditionally prepend an empty/None value"
    )
    print("PASS: recent_round_summaries construction genuinely folds in manager_history_summary, guarded on it being non-empty")


def test_classify_root_cause_call_site_still_receives_recent_round_summaries():
    source = inspect.getsource(loop_module)
    assert "recent_round_summaries=recent_round_summaries" in source, (
        "classify_root_cause()'s own call must still pass the (now-augmented) "
        "recent_round_summaries through -- confirms this fix didn't accidentally break the "
        "existing wiring while adding the new content"
    )
    print("PASS: classify_root_cause() still receives recent_round_summaries at its real call site")


if __name__ == "__main__":
    test_recent_round_summaries_construction_folds_in_manager_history_summary()
    test_classify_root_cause_call_site_still_receives_recent_round_summaries()
    print("\nALL RECENT-ROUND-SUMMARIES COMPRESSED-HISTORY TESTS PASSED")
