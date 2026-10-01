"""Real, live-reproduced bug (2026-07-27): a transient gateway outage
(429 Too Many Requests, confirmed live twice in a row against
gpu_worker_03_fast_extraction) hit during run_turn()'s own turn-start
steps (correction detection, capability classification, existing-module
detection -- run concurrently via manager.step_scheduler, BEFORE a
contract or module lock even exist) crashed the entire request with a
raw, uncaught 500, unlike every gateway-dependent call in the round
loop proper (wrapped in run_with_gateway_outage_handling(), which turns
this into a clean, resumable pause). Fixed: existing_module_target's own
optional hint now gracefully degrades to None (matching every other
"can't determine this" case it already handles); correction_detection/
capability_classification (no safe silent fallback -- misrouting a real
task is worse than pausing) are now caught by a broader try/except
around the whole turn-start step group, returning a clean
"paused: gateway_unavailable" response instead of a raw crash.
"""

import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from infra.gateway_client import GatewayUnavailableError
from manager.loop import run_turn


def test_gateway_outage_during_turn_start_pauses_cleanly_not_a_raw_crash():
    """Real, live-reproduced case: correction_detection (or capability_
    classification) raising GatewayUnavailableError during the turn-
    start step group must produce a clean paused/gateway_unavailable
    response, never propagate as a raw, uncaught exception."""
    async def main():
        with patch(
            "manager.loop.handle_correction_detection",
            new=AsyncMock(side_effect=GatewayUnavailableError("429 Too Many Requests (simulated)")),
        ):
            return await run_turn(
                "Add a field to a synthetic test model.\n\nModule: mis_base_extend\n"
                "Model: oma_gateway_outage_test.synthetic\nField: x (Char)\n",
                client=None, classifier_model="qwen3.6-27b",
            )

    result = asyncio.run(main())
    assert result["status"] == "paused"
    assert result["reason"] == "gateway_unavailable"
    assert "unreachable" in result["message"].lower()
    assert result.get("task_id")
    print(f"PASS: a gateway outage during turn-start steps produces a clean pause, never a raw "
          f"crash -- {result['message'][:120]}")


def test_existing_module_target_gracefully_degrades_on_gateway_outage():
    """existing_module_target's own optional hint (a nice-to-have
    detection, already treated as gracefully-degradable to None in
    every other "can't determine this" case) must NOT pause the whole
    task over a transient gateway outage -- unlike correction_detection/
    capability_classification, there's a safe neutral fallback here."""
    async def main():
        with patch(
            "manager.loop.detect_existing_custom_module_target",
            new=AsyncMock(side_effect=GatewayUnavailableError("429 Too Many Requests (simulated)")),
        ), patch.dict(os.environ, {"OMA_ODOO_DB_DUPLICATE_FOR_BUILD": "odoo16_dev_dup_test"}):
            try:
                return await run_turn(
                    "Add a field to a synthetic test model.\n\nModule: mis_base_extend\n"
                    "Model: oma_gateway_outage_test_2.synthetic\nField: x (Char)\n",
                    client=None, classifier_model="qwen3.6-27b",
                    anticipated_scope={"planning_round_budget": 1},
                )
            except AttributeError:
                # A real client=None reaching further, genuinely gateway-
                # dependent work (real code generation) past the turn-start
                # steps under test here is expected and irrelevant to this
                # test's own concern -- what matters is that it got PAST
                # turn-start without the gateway_unavailable pause at all.
                return {"status": "proceeded_past_turn_start", "reason": None}

    result = asyncio.run(main())
    # Must NOT be the gateway_unavailable pause -- the task should
    # proceed normally (existing_module_target degrading to None is
    # invisible to the rest of the turn), reaching whatever its own
    # ordinary next step is instead of pausing over this one optional hint.
    assert result.get("reason") != "gateway_unavailable", (
        f"an outage in the OPTIONAL existing-module-target hint must not pause the whole task -- "
        f"got {result!r}"
    )
    print(f"PASS: existing_module_target's own optional hint gracefully degrades to None on a "
          f"gateway outage, never pausing the whole task -- got status={result.get('status')!r}")


if __name__ == "__main__":
    test_gateway_outage_during_turn_start_pauses_cleanly_not_a_raw_crash()
    test_existing_module_target_gracefully_degrades_on_gateway_outage()
    print("\nALL TURN-START GATEWAY-OUTAGE-HANDLING TESTS PASSED")
