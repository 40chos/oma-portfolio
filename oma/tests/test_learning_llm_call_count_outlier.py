"""Phase 30, P1d (Phase L, §15): unit tests for
median_llm_calls_per_round()/flag_llm_call_count_outlier() -- real,
per-capability_class historical median comparison, explicitly scoped to
INFORMATIONAL flagging only (never a gate/pause). Requires a real,
live Postgres connection (same convention as this project's other
DB-touching tests) -- run with the project's .env sourced.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.learning import flag_llm_call_count_outlier, median_llm_calls_per_round


def test_median_returns_none_for_a_capability_class_with_no_real_data():
    result = median_llm_calls_per_round("this_capability_class_definitely_does_not_exist_anywhere")
    assert result is None, "must never fabricate a median from zero real data points"
    print("PASS: returns None (never a fabricated number) when there's no real historical data")


def test_flag_returns_none_when_there_is_no_historical_median_to_compare_against():
    result = flag_llm_call_count_outlier(999, "this_capability_class_definitely_does_not_exist_anywhere")
    assert result is None, "with no real median available, there's nothing honest to flag against"
    print("PASS: flag_llm_call_count_outlier returns None when no real median exists yet")


def test_flag_never_fires_below_the_multiplier_threshold(monkeypatch):
    import manager.learning as learning_module

    monkeypatch.setattr(learning_module, "median_llm_calls_per_round", lambda cc, min_samples=5: 10.0)
    assert flag_llm_call_count_outlier(29, "module_dev") is None, "just under 3x (30) must not fire"
    print("PASS: never flags a round below the real 3x threshold")


def test_flag_fires_at_or_above_the_multiplier_threshold(monkeypatch):
    import manager.learning as learning_module

    monkeypatch.setattr(learning_module, "median_llm_calls_per_round", lambda cc, min_samples=5: 10.0)
    result = flag_llm_call_count_outlier(30, "module_dev")
    assert result is not None
    assert result["llm_call_count"] == 30
    assert result["historical_median"] == 10.0
    assert "Informational only" in result["message"], (
        "the message itself must say this is informational -- never phrased as a block/gate"
    )
    print("PASS: flags at the real 3x threshold, with an explicitly informational message")


if __name__ == "__main__":
    test_median_returns_none_for_a_capability_class_with_no_real_data()
    test_flag_returns_none_when_there_is_no_historical_median_to_compare_against()
    print("(remaining tests require pytest's monkeypatch fixture; run via pytest)")
