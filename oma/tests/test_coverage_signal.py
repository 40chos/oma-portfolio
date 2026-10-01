"""P10 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md §18,
Phase O): tests for contracts/coverage_signal.py -- the field_type coverage-confidence lookup
against coverage_data.json. Pure, synchronous; uses a small, real-shaped fake coverage_data dict
(not the live file) for deterministic, isolated tests, plus one direct check against the real file.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.coverage_signal import (
    field_type_coverage_signal,
    field_type_has_low_coverage_confidence,
    load_coverage_data,
)

_FAKE_COVERAGE_DATA = {
    "domains": [
        {
            "id": "data_layer", "label": "Data Layer",
            "subgroups": [
                {
                    "id": "field_types", "label": "Field Types",
                    "nodes": [
                        {
                            "id": "char", "label": "Char",
                            "confidence": {"generation": True, "static_validation": False},
                            "combination_tested": True,
                        },
                        {
                            "id": "binary", "label": "Binary",
                            "confidence": {"generation": False},
                            "combination_tested": False,
                        },
                        {
                            "id": "selection", "label": "Selection",
                            "confidence": {"generation": True},
                            "combination_tested": False,
                        },
                    ],
                },
            ],
        },
    ],
}


def test_returns_none_for_an_unset_field_type():
    assert field_type_coverage_signal(None, _FAKE_COVERAGE_DATA) is None
    assert field_type_has_low_coverage_confidence(None, _FAKE_COVERAGE_DATA) is False
    print("PASS: an unset field_type produces no signal, never a guessed low-confidence flag")


def test_returns_none_for_a_field_type_with_no_real_matching_node():
    assert field_type_coverage_signal("Monetary9000", _FAKE_COVERAGE_DATA) is None
    print("PASS: a field_type with no real matching coverage node returns None, never invented")


def test_case_insensitive_lookup_matches_the_real_goal_facts_convention():
    signal = field_type_coverage_signal("Char", _FAKE_COVERAGE_DATA)
    assert signal == {"combination_tested": True, "generation_supported": True}
    print("PASS: 'Char' (goal_facts.field_type's real casing) matches node id 'char'")


def test_fully_confident_field_type_is_never_flagged_low_confidence():
    assert field_type_has_low_coverage_confidence("Char", _FAKE_COVERAGE_DATA) is False
    print("PASS: a field type that's both generation-supported and combination-tested is never flagged")


def test_generation_false_flags_low_confidence():
    assert field_type_has_low_coverage_confidence("Binary", _FAKE_COVERAGE_DATA) is True
    print("PASS: generation_supported=False flags low confidence")


def test_combination_tested_false_alone_never_flags_low_confidence():
    """P14 item 4 (real bug found live, fixed 2026-08-01): combination_tested=False ALONE must
    NOT flag low confidence -- 19/20 real field types have combination_tested=False right now
    (P7 Tier 3 has only graduated 1 real pair total), so treating it as sufficient on its own
    would force-trigger this on ~95% of field-type-bearing tasks, not just genuinely unproven
    ones. 'Selection' here has generation_supported=True but combination_tested=False -- must
    stay False.
    """
    assert field_type_has_low_coverage_confidence("Selection", _FAKE_COVERAGE_DATA) is False
    print("PASS: combination_tested=False alone (with generation_supported=True) never flags low confidence")


def test_load_coverage_data_never_raises_on_a_bad_path():
    from pathlib import Path
    assert load_coverage_data(Path("/nonexistent/path/coverage_data.json")) is None
    print("PASS: a missing/unreadable coverage_data.json returns None, never raises")


def test_real_file_loads_and_resolves_a_real_known_field_type():
    """Direct check against the REAL, live coverage_data.json -- confirms the module's own
    hardcoded path resolves correctly in this real repo layout, not just against a fake dict.
    """
    data = load_coverage_data()
    assert data is not None, "the real coverage_data.json must be loadable in this repo layout"
    signal = field_type_coverage_signal("Char", data)
    assert signal is not None
    assert isinstance(signal["combination_tested"], bool)
    assert isinstance(signal["generation_supported"], bool)
    print("PASS: the real coverage_data.json loads and resolves a real known field type ('Char')")


if __name__ == "__main__":
    test_returns_none_for_an_unset_field_type()
    test_returns_none_for_a_field_type_with_no_real_matching_node()
    test_case_insensitive_lookup_matches_the_real_goal_facts_convention()
    test_fully_confident_field_type_is_never_flagged_low_confidence()
    test_generation_false_flags_low_confidence()
    test_combination_tested_false_alone_never_flags_low_confidence()
    test_load_coverage_data_never_raises_on_a_bad_path()
    test_real_file_loads_and_resolves_a_real_known_field_type()
    print("\nALL COVERAGE-SIGNAL TESTS PASSED")
