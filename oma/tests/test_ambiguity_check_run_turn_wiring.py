"""P12 Tier B/C item 26 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for the real run_turn() wiring of the new ambiguity-check pre-flight gate. Same mocking
convention as tests/test_turn_start_gateway_outage_handling.py (mock the specific extraction
function via patch("manager.loop.<name>", ...), drive through the real run_turn(), client=None)
-- zero live LLM/GPU calls, the real extraction call is fully intercepted.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.ambiguity_check import AmbiguityReport
from manager.loop import run_turn


def test_blocking_ambiguity_pauses_the_turn_before_any_contract_is_built():
    fake_report = AmbiguityReport(
        has_blocking_ambiguity=True,
        blocking_questions=["Which team should tickets be routed to?"],
        reasoning="Routing target is undefined and is the whole point of the task.",
    )

    async def main():
        with patch(
            "manager.loop.extract_ambiguity_report", new=AsyncMock(return_value=fake_report),
        ):
            return await run_turn(
                "Route support tickets to the right team automatically.",
                client=None, classifier_model="qwen3.6-27b",
            )

    result = asyncio.run(main())
    assert result["status"] == "paused"
    assert result["reason"] == "blocking_ambiguity"
    assert "Which team should tickets be routed to?" in result["message"]
    assert result.get("task_id")
    print("PASS: a genuine blocking-ambiguity report pauses the turn cleanly, before any contract is built")


def test_no_blocking_ambiguity_never_pauses_for_this_reason():
    fake_report = AmbiguityReport(has_blocking_ambiguity=False)

    async def main():
        with patch(
            "manager.loop.extract_ambiguity_report", new=AsyncMock(return_value=fake_report),
        ):
            try:
                return await run_turn(
                    "Add a field to a synthetic test model.\n\nModule: mis_base_extend\n"
                    "Model: oma_ambiguity_test.synthetic\nField: x (Char)\n",
                    client=None, classifier_model="qwen3.6-27b",
                    anticipated_scope={"planning_round_budget": 1},
                )
            except AttributeError:
                # client=None reaching genuinely gateway-dependent work past turn-start is
                # expected and irrelevant here -- what matters is it got PAST this specific gate.
                return {"status": "proceeded_past_ambiguity_gate", "reason": None}

    result = asyncio.run(main())
    assert result.get("reason") != "blocking_ambiguity", (
        f"a report with has_blocking_ambiguity=False must never trigger this pause -- got {result!r}"
    )
    print(f"PASS: no blocking ambiguity never triggers this pause -- got status={result.get('status')!r}")


def test_gateway_outage_during_ambiguity_check_fails_open_never_pauses():
    async def main():
        with patch(
            "manager.loop.extract_ambiguity_report",
            new=AsyncMock(side_effect=__import__("infra.gateway_client", fromlist=["GatewayUnavailableError"]).GatewayUnavailableError("429")),
        ):
            try:
                return await run_turn(
                    "Add a field to a synthetic test model.\n\nModule: mis_base_extend\n"
                    "Model: oma_ambiguity_outage_test.synthetic\nField: x (Char)\n",
                    client=None, classifier_model="qwen3.6-27b",
                    anticipated_scope={"planning_round_budget": 1},
                )
            except AttributeError:
                return {"status": "proceeded_past_ambiguity_gate", "reason": None}

    result = asyncio.run(main())
    assert result.get("reason") not in ("blocking_ambiguity", "gateway_unavailable"), (
        f"a gateway outage during the OPTIONAL ambiguity-check step must gracefully degrade "
        f"(same as existing_module_target's own established pattern), never pause the whole "
        f"turn over it -- got {result!r}"
    )
    print(f"PASS: a gateway outage during the optional ambiguity-check step fails open, never blocks the whole task -- got {result.get('status')!r}/{result.get('reason')!r}")


if __name__ == "__main__":
    test_blocking_ambiguity_pauses_the_turn_before_any_contract_is_built()
    test_no_blocking_ambiguity_never_pauses_for_this_reason()
    test_gateway_outage_during_ambiguity_check_fails_open_never_pauses()
    print("\nALL AMBIGUITY-CHECK RUN-TURN WIRING TESTS PASSED")
