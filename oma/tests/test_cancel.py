"""Phase 16 (§20.7 test 2): a real cancel, mid-flight, against the real
Manager loop. Real Postgres/Redis throughout; a fake specialist stands
in only to engineer a controlled multi-round failure with a real async
delay per round (so there's a genuine window to request the cancel
concurrently -- "mid-flight," not before the loop ever started) -- the
loop, the cancel flag, and the memory write are all real.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistOutput, SpecialistType, TaskContract
from infra.gateway_client import ModelGatewayClient
from manager.charter import load_manager_constitution, load_sensitive_paths
from manager.loop import _execute_contract
from manager.task_state import is_cancel_requested, request_cancel
from specialists import registry

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


class SlowAlwaysFails:
    """Always fails, with a real async delay per round -- gives the
    concurrently-scheduled cancel request a genuine window to land
    before the next round's cancel check runs.
    """
    def __init__(self):
        self.calls = 0

    async def run(self, contract: TaskContract) -> SpecialistOutput:
        self.calls += 1
        await asyncio.sleep(0.6)
        return SpecialistOutput(
            task_id=contract.task_id, specialist_type=contract.specialist_type,
            summary=f"[SLOW FAIL call {self.calls}]",
            detail={"uncovered_paths": [], "coverage_diff": "N/A"},
            claims_complete=False, artifacts=[],
        )


def _read_cancelled_row(task_id: str) -> dict | None:
    import psycopg2
    import psycopg2.extras
    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM agent_memory_events WHERE event_type='decision' AND task_id=%s "
            "AND 'cancelled_by_operator' = ANY(tags) ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def _run_real_cancel_test():
    """Calls manager.loop._execute_contract() directly with a pre-minted
    TaskContract (so its real task_id is known up front, letting the
    test target the cancel precisely) -- request_cancel() fires
    concurrently, during round 1's real 0.6s delay, proving the
    cooperative check stops the loop at the NEXT safe boundary: not
    immediately (round 1 still completes), not never (round 2 never
    starts).
    """
    registry.clear()
    specialist = SlowAlwaysFails()
    registry.register(SpecialistType.code_review, specialist)

    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.code_review,
        capability_class=CapabilityClass.readonly_investigation, tier=AutonomyTier.tier_1_readonly,
        goal="Do a full read-only quality audit of the custom Odoo codebase.",
        inputs=["full_codebase_audit:garazd_product_label"], rules=[], deliverables=[],
        compensating_actions=[], validation_by=None, pause_if=[], turn_budget=10,
        planning_round_budget=5, round_wall_clock_cap_seconds=999999,
    )
    task_id = str(contract.task_id)

    client = ModelGatewayClient()
    try:
        async def _drive():
            return await _execute_contract(
                contract, f"task:{task_id}", client, CLASSIFIER_MODEL,
                correction_result={"status": "not_a_correction"}, module_for_repeat_check=None,
                memory_block="", constitution_text=load_manager_constitution(),
                sensitive_paths_raw=load_sensitive_paths(),
            )

        async def _cancel_mid_flight():
            # Wait for round 1 to genuinely start (a real, observable
            # side channel: the fake specialist's own call count), then
            # request the cancel while round 1's real delay is still in
            # flight -- a genuine mid-flight cancel.
            while specialist.calls == 0:
                await asyncio.sleep(0.02)
            request_cancel(task_id)

        result, _ = await asyncio.gather(_drive(), _cancel_mid_flight())

        assert result["status"] == "cancelled", f"expected a real cooperative cancel, got: {result}"
        assert result["reason"] == "cancelled_by_operator"
        assert specialist.calls == 1, (
            f"the cancel must stop the loop at the NEXT safe boundary (before round 2 starts), "
            f"not immediately and not never -- expected exactly 1 real specialist call, got {specialist.calls}"
        )
        assert not is_cancel_requested(task_id), "the cancel flag must be cleared once honored"

        row = _read_cancelled_row(task_id)
        assert row is not None, "a real cancelled_by_operator decision memory row must be written"
        print(f"PASS: a real multi-round task was cancelled mid-flight (round 1 completed for real, "
              f"round 2 never started) -- {specialist.calls} real specialist call(s), a real "
              f"cancelled_by_operator memory row written (row #{row['id']})")
    finally:
        await client.aclose()
        registry.clear()


if __name__ == "__main__":
    asyncio.run(_run_real_cancel_test())
    print("\nALL CANCEL TESTS PASSED")
