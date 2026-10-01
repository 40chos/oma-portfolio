"""Real, confirmed N+1 performance bug found live (2026-07-30):
list_custom_models_fast() used to issue 1 + 2*N sequential XML-RPC
round-trips (a search_count AND a separate ir.model.read call PER
oma_*-owned model row) -- its own cost scaled with how many real oma_*
models have EVER existed in this shared dev database (hundreds, after
748+ real historical tasks), not with anything about the current call.
Confirmed live: a single real call, via _validate_inherit_target_
resolved(), measured 196.9s -- the actual dominant cause of a real
benchmark task's own slowdown. Fixed to 3 total round-trips regardless
of N. These tests lock in that the batched rewrite preserves the exact
same filtering semantics as the original N+1 version, using a mocked
XML-RPC proxy (no live connection needed) -- confirmed byte-identical
output against the real live database separately, live, before this
fix was deployed (0.25-0.33s warm vs the original 196.9s).
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.odoo_schema_client import list_custom_models_fast


def _mock_proxy_for(recs, other_owner_rows, model_rows):
    proxy = MagicMock()

    def execute_kw(db, uid, key, model, method, args, kwargs=None):
        if model == "ir.model.data" and method == "search_read" and args[0][0][0] == "module":
            return recs
        if model == "ir.model.data" and method == "search_read":
            return other_owner_rows
        if model == "ir.model" and method == "read":
            return model_rows
        raise AssertionError(f"unexpected call: {model}.{method}({args})")

    proxy.execute_kw.side_effect = execute_kw
    return proxy


def test_only_exclusively_owned_models_are_returned():
    """3 candidate rows; res_id=2 is ALSO owned by a non-oma_ module (a
    real, standard Odoo model) -- must be excluded, matching the
    original per-row search_count > 0 exclusion.
    """
    recs = [
        {"res_id": 1, "module": "oma_task_a"},
        {"res_id": 2, "module": "oma_task_b"},
        {"res_id": 3, "module": "oma_task_c"},
    ]
    other_owner_rows = [{"res_id": 2}]  # only res_id 2 has a real, non-oma_ owner too
    model_rows = [
        {"id": 1, "model": "x.model_a"},
        {"id": 3, "model": "x.model_c"},
    ]
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(1, "key")):
        with patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_mock_proxy_for(recs, other_owner_rows, model_rows)):
            result = list_custom_models_fast("odoo16_dev")

    assert result == [("x.model_a", "oma_task_a"), ("x.model_c", "oma_task_c")], result
    print("PASS: only exclusively-oma_-owned models are returned, matching original semantics")


def test_returns_none_when_nothing_found():
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(1, "key")):
        with patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_mock_proxy_for([], [], [])):
            result = list_custom_models_fast("odoo16_dev")
    assert result is None
    print("PASS: returns None (never an empty list) when no candidate rows exist at all")


def test_returns_none_when_every_candidate_has_another_owner():
    recs = [{"res_id": 1, "module": "oma_task_a"}]
    other_owner_rows = [{"res_id": 1}]
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(1, "key")):
        with patch("tools_odoo.odoo_schema_client._models_proxy", return_value=_mock_proxy_for(recs, other_owner_rows, [])):
            result = list_custom_models_fast("odoo16_dev")
    assert result is None
    print("PASS: returns None when every candidate is excluded by a real other owner")


def test_makes_exactly_three_round_trips_regardless_of_n():
    """The whole point of the fix -- N candidate rows must still cost
    exactly 3 execute_kw calls, not 1 + 2*N.
    """
    recs = [{"res_id": i, "module": f"oma_task_{i}"} for i in range(1, 51)]
    model_rows = [{"id": i, "model": f"x.model_{i}"} for i in range(1, 51)]
    with patch("tools_odoo.odoo_schema_client._get_or_create_shared_key", return_value=(1, "key")):
        proxy = _mock_proxy_for(recs, [], model_rows)
        with patch("tools_odoo.odoo_schema_client._models_proxy", return_value=proxy):
            result = list_custom_models_fast("odoo16_dev")
    assert len(result) == 50
    assert proxy.execute_kw.call_count == 3, (
        f"expected exactly 3 XML-RPC round-trips for 50 candidates, got {proxy.execute_kw.call_count} "
        f"-- the whole point of this fix is no longer scaling with N"
    )
    print("PASS: exactly 3 round-trips regardless of N (50 candidates here) -- the N+1 pattern is gone")


if __name__ == "__main__":
    test_only_exclusively_owned_models_are_returned()
    test_returns_none_when_nothing_found()
    test_returns_none_when_every_candidate_has_another_owner()
    test_makes_exactly_three_round_trips_regardless_of_n()
    print("\nALL LIST-CUSTOM-MODELS-FAST-BATCHING TESTS PASSED")
