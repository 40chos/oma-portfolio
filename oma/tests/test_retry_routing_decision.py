"""P12 Tier S/A item 9 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
exhaustive unit tests for _retry_routing_decision() -- the pure, deterministic routing table
extracted from select_specialist_for_retry(), covering all four cells of its own documented
transition table. Zero live calls, zero async, direct function calls only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import CapabilityClass, SpecialistType
from manager.replanning import _retry_routing_decision


def test_code_review_readonly_investigation_stays_on_code_review_forever():
    result = _retry_routing_decision(
        SpecialistType.code_review, CapabilityClass.readonly_investigation.value, same_theme=False,
    )
    assert result == SpecialistType.code_review
    print("PASS: a standalone readonly-investigation task stays on code_review forever")


def test_code_review_module_dev_always_bounces_back_to_bug_fix():
    result = _retry_routing_decision(
        SpecialistType.code_review, CapabilityClass.module_development.value, same_theme=False,
    )
    assert result == SpecialistType.bug_fix
    print("PASS: a module_dev task on a temporary code_review detour always bounces back to bug_fix -- the Phase 16 fix")


def test_code_review_none_original_capability_class_bounces_back_to_bug_fix():
    result = _retry_routing_decision(SpecialistType.code_review, None, same_theme=False)
    assert result == SpecialistType.bug_fix
    print("PASS: an unknown/None original_capability_class defaults to the bug_fix bounce-back (never silently stuck on code_review)")


def test_code_review_ignores_same_theme_entirely():
    # same_theme is only ever consulted for the NON-code_review branch -- confirm the
    # code_review branch's outcome is identical regardless of same_theme's value.
    a = _retry_routing_decision(SpecialistType.code_review, CapabilityClass.readonly_investigation.value, same_theme=True)
    b = _retry_routing_decision(SpecialistType.code_review, CapabilityClass.readonly_investigation.value, same_theme=False)
    assert a == b == SpecialistType.code_review
    print("PASS: the code_review branch's outcome never depends on same_theme")


def test_bug_fix_with_recurring_theme_detours_to_code_review():
    result = _retry_routing_decision(SpecialistType.bug_fix, None, same_theme=True)
    assert result == SpecialistType.code_review
    print("PASS: bug_fix with a genuinely recurring finding theme detours to code_review")


def test_bug_fix_with_no_recurring_theme_stays_on_bug_fix():
    result = _retry_routing_decision(SpecialistType.bug_fix, None, same_theme=False)
    assert result == SpecialistType.bug_fix
    print("PASS: bug_fix with no recurring theme signal stays on bug_fix (the default, no-switch case)")


def test_non_code_review_ignores_original_capability_class_entirely():
    # original_capability_class is only ever consulted for the code_review branch -- confirm
    # the non-code_review branch's outcome is identical regardless of its value.
    a = _retry_routing_decision(SpecialistType.bug_fix, CapabilityClass.readonly_investigation.value, same_theme=True)
    b = _retry_routing_decision(SpecialistType.bug_fix, None, same_theme=True)
    assert a == b == SpecialistType.code_review
    print("PASS: the non-code_review branch's outcome never depends on original_capability_class")


if __name__ == "__main__":
    test_code_review_readonly_investigation_stays_on_code_review_forever()
    test_code_review_module_dev_always_bounces_back_to_bug_fix()
    test_code_review_none_original_capability_class_bounces_back_to_bug_fix()
    test_code_review_ignores_same_theme_entirely()
    test_bug_fix_with_recurring_theme_detours_to_code_review()
    test_bug_fix_with_no_recurring_theme_stays_on_bug_fix()
    test_non_code_review_ignores_original_capability_class_entirely()
    print("\nALL RETRY-ROUTING-DECISION TESTS PASSED")
