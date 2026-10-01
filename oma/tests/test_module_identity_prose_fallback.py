"""P12 Tier A item 17 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for resolve_module_identity()'s new prose fallback -- real, confirmed gap: the function
previously returned None for any goal without a literal 'Model: <name>' metadata line,
degrading the fencing lock to always-unique-per-task (defeating its whole purpose) for a
genuine, real prose-only goal shape. Pure, deterministic, zero live calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.module_identity import resolve_module_identity


def test_flagship_example_from_the_plan_text_now_resolves_correctly():
    result = resolve_module_identity("Modify the ir.model.access.csv permissions for account.move.")
    assert result == "account.move", (
        f"this exact goal is the plan's own flagship real example, previously returning None -- "
        f"got {result!r}"
    )
    print("PASS: the flagship 'ir.model.access.csv permissions for account.move' example now correctly resolves to account.move, not ir.model.access.csv")


def test_structured_model_line_still_takes_priority_over_prose():
    goal = "Model: project.meerwerk (inherit)\nAlso touches res.partner in passing."
    result = resolve_module_identity(goal)
    assert result == "project.meerwerk", "the structured Model: line must still win over any prose fallback"
    print("PASS: an existing structured Model: line still takes priority, unchanged behavior")


def test_hint_still_wins_over_everything():
    result = resolve_module_identity("Modify account.move permissions.", hint="explicit.hint")
    assert result == "explicit.hint"
    print("PASS: an explicit hint still wins over both the structured line and the prose fallback")


def test_purely_descriptive_goal_with_no_dotted_identifier_returns_none():
    result = resolve_module_identity("Add a field to the customer form.")
    assert result is None
    print("PASS: a goal with no real dotted model-shaped identifier at all still correctly returns None")


def test_file_path_mentioned_alongside_a_real_model_does_not_win():
    result = resolve_module_identity("Update oma_x/models/models.py -- also touches project.meerwerk directly.")
    assert result == "project.meerwerk"
    print("PASS: a file path (models.py) mentioned first does not win over a real model name later in the same goal")


def test_file_path_with_no_real_model_present_returns_none():
    result = resolve_module_identity("Update oma_x/models/models.py to fix a typo.")
    assert result is None
    print("PASS: a goal naming only a file path, no real model identifier at all, correctly returns None rather than guessing the file")


if __name__ == "__main__":
    test_flagship_example_from_the_plan_text_now_resolves_correctly()
    test_structured_model_line_still_takes_priority_over_prose()
    test_hint_still_wins_over_everything()
    test_purely_descriptive_goal_with_no_dotted_identifier_returns_none()
    test_file_path_mentioned_alongside_a_real_model_does_not_win()
    test_file_path_with_no_real_model_present_returns_none()
    print("\nALL MODULE-IDENTITY PROSE-FALLBACK TESTS PASSED")
