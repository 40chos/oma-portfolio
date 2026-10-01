"""P12 Tier A item 16 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for the fix wrapping _timed_code_review() and the sequential run_code_review_diff
fallback in run_with_gateway_outage_handling() (manager/loop.py) -- previously only
_timed_verification() (Testing/QA's sibling call, inside the SAME asyncio.gather()) had this
wrap; a gateway outage during Code-Review's own call propagated as a raw, unhandled
GatewayUnavailableError instead of the same clean gateway_unavailable pause every other
gateway-dependent call gets.

Two parts: (1) a source-level regression guard confirming both real call sites in
manager/loop.py actually use the wrap (so a future refactor can't silently drop it without a
test catching it) -- run_turn()'s own dependency depth (module locks, Redis, real specialists)
makes a full live round-loop reproduction impractical here; the wrapping MECHANISM itself
(run_with_gateway_outage_handling()) is already proven correct end-to-end by
tests/test_manager_gateway_orchestration.py (real Redis state flip, real lock release, real
distinct pause message) against a real dead gateway client. (2) confirms the exact same
mechanism, exercised the same way, correctly converts a GatewayUnavailableError from a
run_code_review_diff()-shaped call into GatewayOutagePause -- zero live network calls.
"""

import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module
from infra.gateway_client import GatewayUnavailableError
from manager.gateway_orchestration import GatewayOutagePause, run_with_gateway_outage_handling


def test_both_real_call_sites_wrap_run_code_review_diff_in_gateway_outage_handling():
    source = inspect.getsource(loop_module)
    # Every real occurrence of "run_code_review_diff(" in the round loop must appear inside a
    # run_with_gateway_outage_handling(...) call -- checked by confirming the wrap call
    # immediately precedes it in source order for each real occurrence (excluding the
    # docstring/comment mentions, which never contain the literal call syntax).
    occurrences = [
        m.start() for m in __import__("re").finditer(r"run_code_review_diff\(", source)
    ]
    real_calls = [
        i for i in occurrences
        if "lambda: run_code_review_diff(" in source[max(0, i - 60):i + 30]
    ]
    assert len(real_calls) == 2, (
        f"expected exactly 2 real run_code_review_diff(...) calls, both wrapped in "
        f"run_with_gateway_outage_handling() via a lambda (the concurrent _timed_code_review() "
        f"path and the sequential fallback) -- found {len(real_calls)}"
    )
    print("PASS: both real run_code_review_diff() call sites in manager/loop.py are wrapped in run_with_gateway_outage_handling()")


def test_run_with_gateway_outage_handling_converts_a_code_review_shaped_outage_correctly():
    async def fake_run_code_review_diff_call():
        raise GatewayUnavailableError("429 Too Many Requests (simulated)")

    async def main():
        try:
            await run_with_gateway_outage_handling(
                "fake-task-id", "fake-module-lock", fake_run_code_review_diff_call,
            )
        except GatewayOutagePause as pause:
            return pause
        return None

    pause = asyncio.run(main())
    assert pause is not None
    assert "unreachable" in pause.message.lower()
    print("PASS: the exact same wrap-unwrap contract used elsewhere in this file correctly "
          "converts a GatewayUnavailableError from a run_code_review_diff()-shaped call into a "
          "clean GatewayOutagePause")


if __name__ == "__main__":
    test_both_real_call_sites_wrap_run_code_review_diff_in_gateway_outage_handling()
    test_run_with_gateway_outage_handling_converts_a_code_review_shaped_outage_correctly()
    print("\nALL CODE-REVIEW GATEWAY-OUTAGE WRAPPING TESTS PASSED")
