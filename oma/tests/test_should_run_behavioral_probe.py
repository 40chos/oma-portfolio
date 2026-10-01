"""Real bug found live during the site_50 benchmark's task 006 ("auto-generate a unique
reference number on create"): the behavioral-probe gate in specialists/testing_qa/specialist.py
only ever checked goal_facts.is_computed, never goal_facts.is_sequence_assigned -- so a
specialist that declared a plain Char field and an ir.sequence record but forgot the
create()-override that actually assigns it got a false reproduction_confirmed=True from the
plain existence check alone. Confirmed live via coverage_diff: models.py had only 3 statements
total, no create() override anywhere. Extracted the inline condition into
_should_run_behavioral_probe() specifically so this is directly unit-testable.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.specialist import _should_run_behavioral_probe


def test_runs_for_a_computed_field_when_existence_already_confirmed():
    assert _should_run_behavioral_probe(True, {"is_computed": True}) is True
    print("PASS: is_computed=True + reproduction_confirmed triggers the probe (unchanged original behavior)")


def test_runs_for_a_sequence_assigned_field_when_existence_already_confirmed():
    """The real, headline fix: this is the exact shape task 006 needed."""
    assert _should_run_behavioral_probe(True, {"is_sequence_assigned": True}) is True
    print("PASS: is_sequence_assigned=True + reproduction_confirmed now also triggers the probe")


def test_never_runs_when_reproduction_itself_was_not_confirmed():
    assert _should_run_behavioral_probe(False, {"is_computed": True}) is False
    assert _should_run_behavioral_probe(False, {"is_sequence_assigned": True}) is False
    print("PASS: nothing to probe on a field whose plain existence check already failed")


def test_never_runs_for_an_ordinary_plain_field():
    assert _should_run_behavioral_probe(True, {"is_computed": False, "is_sequence_assigned": False}) is False
    assert _should_run_behavioral_probe(True, {}) is False
    print("PASS: an ordinary, non-computed, non-sequence-assigned field still skips the extra round-trip")


def test_runs_when_both_flags_are_true():
    assert _should_run_behavioral_probe(True, {"is_computed": True, "is_sequence_assigned": True}) is True
    print("PASS: both real signals present still correctly triggers the probe (no double-negative bug)")


def test_handles_none_goal_facts_gracefully():
    assert _should_run_behavioral_probe(True, None) is False
    print("PASS: goal_facts=None (no structured facts extracted) never crashes, just skips the probe")


if __name__ == "__main__":
    test_runs_for_a_computed_field_when_existence_already_confirmed()
    test_runs_for_a_sequence_assigned_field_when_existence_already_confirmed()
    test_never_runs_when_reproduction_itself_was_not_confirmed()
    test_never_runs_for_an_ordinary_plain_field()
    test_runs_when_both_flags_are_true()
    test_handles_none_goal_facts_gracefully()
    print("\nALL _should_run_behavioral_probe TESTS PASSED")
