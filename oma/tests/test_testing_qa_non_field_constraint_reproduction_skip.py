"""Phase 29 (2026-07-29): unit test for
_is_non_field_constraint_echoed_as_target() -- the real, confirmed live
root cause behind 2 straight identical "Reproduction FAILED for
school.student.demo_data" round failures on the school_student task's
own demo_data round.

Root cause: `_extract_reproduction_target()` has no real ORM field to
find for a data-loading (`demo_data`) or test-writing
(`automated_tests`) constraint, so it falls back to echoing the round's
own focus label as `field_name` -- `_check_field_exists_on_model()`
then always fails, since `school.student` genuinely has no field
literally named `demo_data`. Same false-negative class as the existing
button-restriction and menu-structure reproduction skips, a third shape
neither of those two covers.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.specialist import _is_non_field_constraint_echoed_as_target

_REAL_DEMO_DATA_GOAL = (
    "Simple Custom Module Task (Ideal for AI Odoo Developer)\n"
    "...\n\n"
    "This round's own NEW focus is ONLY: 'demo_data'. Add ONLY the code this one constraint "
    "strictly requires..."
)


def test_matches_the_real_live_demo_data_echo():
    assert _is_non_field_constraint_echoed_as_target("demo_data", _REAL_DEMO_DATA_GOAL) is True
    print("PASS: the real, confirmed live 'demo_data' echoed-as-field-name case is detected")


def test_matches_automated_tests_the_sibling_shape():
    goal = "This round's own NEW focus is ONLY: 'automated_tests'. Write real tests."
    assert _is_non_field_constraint_echoed_as_target("automated_tests", goal) is True
    print("PASS: the sibling 'automated_tests' shape is also detected (same class of bug)")


def test_never_matches_a_genuine_real_field_name():
    goal = "This round's own NEW focus is ONLY: 'demo_data'. Add ONLY the code this one constraint requires."
    assert _is_non_field_constraint_echoed_as_target("date_of_birth", goal) is False
    print("PASS: a genuine, real field name extracted correctly is never treated as an echo")


def test_never_matches_when_goal_has_no_focus_marker_at_all():
    plain_goal = "Add a new field 'priority' to the model. No decomposition markers here."
    assert _is_non_field_constraint_echoed_as_target("priority", plain_goal) is False
    assert _is_non_field_constraint_echoed_as_target("demo_data", plain_goal) is False
    print("PASS: a plain, non-decomposed goal (no focus marker) never triggers the skip, "
          "preserving exact prior behavior for non-decomposed tasks")


if __name__ == "__main__":
    test_matches_the_real_live_demo_data_echo()
    test_matches_automated_tests_the_sibling_shape()
    test_never_matches_a_genuine_real_field_name()
    test_never_matches_when_goal_has_no_focus_marker_at_all()
    print("\nALL NON-FIELD-CONSTRAINT REPRODUCTION SKIP TESTS PASSED")
