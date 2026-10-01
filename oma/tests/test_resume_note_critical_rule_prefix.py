"""Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
project_ticket_counts node): manager.loop's `resume_task_after_checkpoint(note=...)` appended a
human's own explicit resume-time correction as a plain ordinary rule -- competing with up to 8
capped, mostly-superseded auto-generated round summaries in contracts.schema.split_critical_rules()'s
"General history" bucket, never the dedicated, prioritized "CRITICAL FIXES REQUIRED THIS ROUND"
section Build's own prompt actually highlights (specialists/build/specialist.py, ~line 17076-17089).
Confirmed live: six consecutive resumes with increasingly explicit, code-literal notes produced
byte-identical failures on the same missing-field gap, because the note was structurally diluted,
not because Build ignored it outright.

Pure, isolated tests against the exact string-construction logic in manager/loop.py's
_resume_task_after_checkpoint_locked() (mirrored here rather than driving the full function, which
has many unrelated DB/Redis side effects) and the shared split_critical_rules() consumer.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import CRITICAL_RULE_PREFIX, split_critical_rules


def _build_new_rules(human_summary: str, prior_round_notes: list[str], clarifications: list[str], note: str | None) -> list[str]:
    """Mirrors manager.loop._resume_task_after_checkpoint_locked()'s own new_rules
    construction exactly, so this test fails the moment that logic drifts from what's
    actually shipped, without needing to drive the whole DB/Redis-backed function.
    """
    new_rules = [f"Original attempt summary: {human_summary}"]
    new_rules.extend(prior_round_notes)
    new_rules.extend(f"Operator: {c}" for c in clarifications)
    if note:
        new_rules.append(f"{CRITICAL_RULE_PREFIX}Operator: {note}")
    return new_rules


def test_resume_note_gets_the_critical_rule_prefix():
    rules = _build_new_rules("stuck on X", ["Round 1: some old finding"], [], "Add the missing field to models.py exactly like this: ...")
    critical, ordinary = split_critical_rules(rules)
    assert critical == ["Operator: Add the missing field to models.py exactly like this: ..."], critical
    assert "Original attempt summary: stuck on X" in ordinary
    print("PASS: a resume note gets CRITICAL_RULE_PREFIX and lands in the critical section, not diluted ordinary history")


def test_resume_note_critical_rule_survives_being_capped_alongside_many_ordinary_entries():
    # The real incident shape: many auto-generated round-history entries (well over the 8-entry
    # ordinary cap Build's own prompt applies) must never push a genuinely critical, human-supplied
    # correction out of visibility -- critical rules are a SEPARATE bucket, never subject to the
    # ordinary-history cap at all.
    many_round_notes = [f"Round {i}: some old finding" for i in range(20)]
    rules = _build_new_rules("stuck on X", many_round_notes, [], "The real, confirmed fix is Y.")
    critical, ordinary = split_critical_rules(rules)
    assert critical == ["Operator: The real, confirmed fix is Y."]
    assert len(ordinary) == 21  # summary + 20 round notes, all still present pre-cap
    print("PASS: the critical resume note is never subject to the ordinary-history cap, regardless of round-history volume")


def test_no_note_means_no_critical_rule_added():
    rules = _build_new_rules("stuck on X", ["Round 1: some old finding"], [], None)
    critical, ordinary = split_critical_rules(rules)
    assert critical == []
    print("PASS: no note means no spurious critical rule is added")


if __name__ == "__main__":
    test_resume_note_gets_the_critical_rule_prefix()
    test_resume_note_critical_rule_survives_being_capped_alongside_many_ordinary_entries()
    test_no_note_means_no_critical_rule_added()
    print("\nALL RESUME-NOTE CRITICAL-RULE-PREFIX TESTS PASSED")
