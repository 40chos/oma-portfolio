"""P12 Tier S/A item 10 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for the new, additive RoundFeedback typed record and its two structured helpers
(contracts/schema.py) -- same_underlying_finding_structured() and dedupe_round_feedback().
Deliberately new, standalone infrastructure -- not yet wired into TaskContract.rules itself
(a separate, larger migration). Pure, deterministic, zero live calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import RoundFeedback, dedupe_round_feedback, same_underlying_finding_structured


def _fb(source="code_review", severity="critical", text="x", round_number=1, finding_class=None) -> RoundFeedback:
    return RoundFeedback(source=source, severity=severity, text=text, round_number=round_number, finding_class=finding_class)


# --- same_underlying_finding_structured() -----------------------------------

def test_exact_finding_class_match_is_same_finding():
    a = _fb(finding_class="security_csv_empty", text="totally different wording A")
    b = _fb(finding_class="security_csv_empty", text="totally different wording B")
    assert same_underlying_finding_structured(a, b) is True
    print("PASS: two records with the same finding_class are the same underlying finding, regardless of text")


def test_different_finding_class_is_never_same_finding():
    a = _fb(finding_class="security_csv_empty", text="shared words shared words")
    b = _fb(finding_class="missing_field_age", text="shared words shared words")
    assert same_underlying_finding_structured(a, b) is False
    print("PASS: different finding_class values are never the same finding, even with identical text")


def test_missing_finding_class_falls_back_to_word_overlap_heuristic():
    a = _fb(finding_class=None, text="the action_accept method is not defined on the base model")
    b = _fb(finding_class=None, text="action_accept is not defined in the base model project.fieldjob")
    assert same_underlying_finding_structured(a, b) is True
    print("PASS: when finding_class is absent on both sides, falls back to the existing word-overlap heuristic")


def test_different_source_is_never_same_finding_even_with_identical_class():
    a = _fb(source="build", finding_class="x", text="t")
    b = _fb(source="code_review", finding_class="x", text="t")
    assert same_underlying_finding_structured(a, b) is False
    print("PASS: a Build note and a Code-Review finding are never the same underlying finding, regardless of everything else")


# --- dedupe_round_feedback() -------------------------------------------------

def test_dedupe_keeps_only_most_recent_of_a_critical_duplicate_group():
    entries = [
        _fb(severity="critical", finding_class="x", round_number=1, text="first"),
        _fb(severity="critical", finding_class="x", round_number=3, text="most recent"),
        _fb(severity="critical", finding_class="x", round_number=2, text="middle"),
    ]
    result = dedupe_round_feedback(entries)
    assert len(result) == 1
    assert result[0].round_number == 3
    print("PASS: a group of critical duplicates collapses to only the highest round_number entry")


def test_dedupe_never_touches_info_entries():
    entries = [
        _fb(severity="info", finding_class="x", round_number=1),
        _fb(severity="info", finding_class="x", round_number=2),
    ]
    result = dedupe_round_feedback(entries)
    assert len(result) == 2
    print("PASS: info entries are never collapsed, only critical -- no supersession semantics for info")


def test_dedupe_leaves_non_duplicate_critical_entries_untouched():
    entries = [
        _fb(severity="critical", finding_class="a", round_number=1),
        _fb(severity="critical", finding_class="b", round_number=1),
    ]
    result = dedupe_round_feedback(entries)
    assert len(result) == 2
    print("PASS: two genuinely different critical entries both survive untouched")

def test_dedupe_preserves_relative_order_of_survivors():
    entries = [
        _fb(severity="info", finding_class="i1", round_number=1, text="info1"),
        _fb(severity="critical", finding_class="x", round_number=1, text="old"),
        _fb(severity="info", finding_class="i2", round_number=2, text="info2"),
        _fb(severity="critical", finding_class="x", round_number=2, text="new"),
    ]
    result = dedupe_round_feedback(entries)
    assert [e.text for e in result] == ["info1", "info2", "new"]
    print("PASS: dedupe preserves the original relative order of every surviving entry")


def test_dedupe_empty_list_is_a_pure_no_op():
    assert dedupe_round_feedback([]) == []
    print("PASS: an empty list is a pure no-op")


if __name__ == "__main__":
    test_exact_finding_class_match_is_same_finding()
    test_different_finding_class_is_never_same_finding()
    test_missing_finding_class_falls_back_to_word_overlap_heuristic()
    test_different_source_is_never_same_finding_even_with_identical_class()
    test_dedupe_keeps_only_most_recent_of_a_critical_duplicate_group()
    test_dedupe_never_touches_info_entries()
    test_dedupe_leaves_non_duplicate_critical_entries_untouched()
    test_dedupe_preserves_relative_order_of_survivors()
    test_dedupe_empty_list_is_a_pure_no_op()
    print("\nALL ROUND-FEEDBACK TESTS PASSED")
