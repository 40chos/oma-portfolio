"""Phase 30, P1d (Phase L, §15): unit tests for ModelGatewayClient's
real per-task LLM call count/duration tracking
(_record_call_stat/pop_call_stats) -- the real visibility mechanism
this priority exists to build, explicitly scoped to never gate/block
anything (see manager/loop.py's own wiring, which only ever logs/flags).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.gateway_client import ModelGatewayClient


def test_pop_call_stats_returns_zeros_for_an_unseen_task():
    client = ModelGatewayClient()
    assert client.pop_call_stats("never-called") == {"count": 0, "duration_sec": 0.0}
    print("PASS: pop_call_stats returns real zeros, never crashes, for a task with no recorded calls")


def test_record_call_stat_accumulates_across_multiple_calls():
    client = ModelGatewayClient()
    client._record_call_stat("task-1", 1.5)
    client._record_call_stat("task-1", 2.5)
    client._record_call_stat("task-1", 1.0)
    stats = client.pop_call_stats("task-1")
    assert stats == {"count": 3, "duration_sec": 5.0}
    print("PASS: multiple calls for the same task accumulate correctly")


def test_pop_clears_the_stats_so_the_next_round_starts_fresh():
    client = ModelGatewayClient()
    client._record_call_stat("task-2", 1.0)
    first_pop = client.pop_call_stats("task-2")
    second_pop = client.pop_call_stats("task-2")
    assert first_pop == {"count": 1, "duration_sec": 1.0}
    assert second_pop == {"count": 0, "duration_sec": 0.0}, (
        "pop must CLEAR the stats -- otherwise every round after the first would double-count "
        "the previous round's own calls"
    )
    print("PASS: pop_call_stats clears state, so per-round stats never leak into the next round")


def test_stats_are_isolated_per_task():
    client = ModelGatewayClient()
    client._record_call_stat("task-a", 1.0)
    client._record_call_stat("task-b", 100.0)
    assert client.pop_call_stats("task-a") == {"count": 1, "duration_sec": 1.0}
    assert client.pop_call_stats("task-b") == {"count": 1, "duration_sec": 100.0}
    print("PASS: call stats for different tasks never leak into each other")


def test_pop_llm_call_stats_adapter_in_manager_loop():
    """manager.loop._pop_llm_call_stats() is the thin adapter that
    feeds ReplanRound's own field names -- tested here (not mocked)
    against the real ModelGatewayClient methods above, confirming the
    two layers actually agree on shape.
    """
    from manager.loop import _pop_llm_call_stats

    client = ModelGatewayClient()
    client._record_call_stat("t-adapter", 2.0)
    client._record_call_stat("t-adapter", 3.0)
    result = _pop_llm_call_stats(client, "t-adapter")
    assert result == {"llm_call_count": 2, "llm_duration_sec": 5.0}
    print("PASS: manager.loop._pop_llm_call_stats() correctly adapts pop_call_stats() to ReplanRound's field names")


if __name__ == "__main__":
    test_pop_call_stats_returns_zeros_for_an_unseen_task()
    test_record_call_stat_accumulates_across_multiple_calls()
    test_pop_clears_the_stats_so_the_next_round_starts_fresh()
    test_stats_are_isolated_per_task()
    test_pop_llm_call_stats_adapter_in_manager_loop()
    print("\nALL GATEWAY-CLIENT CALL-STATS TESTS PASSED")
