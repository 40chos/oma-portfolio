"""Phase 12: real tests for the tier-3/4 pre-sign-off gate -- a real
gap found while building the chat UI's plan/act approval pattern
(PAUSE_SIGN_OFF_REQUIRED existed since Phase 6 but was never actually
wired into the loop). Tests against real Postgres (memory writes) and
real Redis (pending-contract storage) -- no mocks.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

import psycopg2
import psycopg2.extras

import uuid

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistOutput, SpecialistType, TaskContract
from infra.gateway_client import ModelGatewayClient
from infra.settings import load_postgres_settings
from manager.loop import resume_after_sign_off, run_plan, run_turn
from manager.sign_off import get_pending_contract, list_pending_task_ids
from specialists import registry

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


class ConfigurableFakeSpecialist:
    def __init__(self, claims_complete: bool):
        self._claims_complete = claims_complete

    async def run(self, contract: TaskContract) -> SpecialistOutput:
        return SpecialistOutput(
            task_id=contract.task_id,
            specialist_type=contract.specialist_type,
            summary=f"[FAKE {'PASS' if self._claims_complete else 'FAIL'}] {contract.goal}",
            detail={"uncovered_paths": [], "coverage_diff": "100% (fake)"},
            claims_complete=self._claims_complete,
            artifacts=[],
        )


def _get_decision_rows(task_id: str) -> list[dict]:
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agent_memory_events WHERE task_id = %s", (task_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


async def _run_tier4_task_pauses_before_touching_any_specialist(client):
    """A tier-4 task (touches_schema_or_permissions=True) must pause
    for sign-off BEFORE anything is delegated -- confirmed by
    registering NOTHING for bug_fix/code_review/testing_qa at all; if
    delegation were ever attempted, this would raise
    SpecialistNotAvailableError instead of returning a clean pause.
    """
    registry.clear()
    message = "Modify the ir.model.access.csv permissions for account.move."
    result = await run_turn(
        message, client, CLASSIFIER_MODEL,
        anticipated_scope={"touches_schema_or_permissions": True},
    )

    assert result["status"] == "paused"
    assert result["reason"] == "sign_off_required"
    assert result["contract"].tier.value == 4
    assert "task_id" in result

    payload = get_pending_contract(result["task_id"])
    assert payload is not None, "the pending contract must actually be stored in Redis"
    assert payload["contract"]["tier"] == 4
    assert result["task_id"] in list_pending_task_ids()
    print(f"PASS: a tier-4 task pauses for sign-off with NOTHING registered for any specialist -- "
          f"confirmed no delegation was ever attempted (task {result['task_id']})")
    return result["task_id"]


async def _resume_with_rejection_cancels_cleanly(client, task_id: str):
    result = await resume_after_sign_off(task_id, approved=False, client=client, classifier_model=CLASSIFIER_MODEL)
    assert result["status"] == "rejected"
    assert get_pending_contract(task_id) is None, "a resolved sign-off must be cleared from pending storage"

    rows = _get_decision_rows(task_id)
    rejected_rows = [r for r in rows if r["event_type"] == "decision" and "rejected" in (r["tags"] or [])]
    assert rejected_rows, f"expected a 'decision'/'rejected' memory row for task {task_id}"
    print(f"PASS: rejecting a tier-4 sign-off cancels cleanly -- pending contract cleared, "
          f"real 'rejected' decision row written to memory (row #{rejected_rows[0]['id']})")


async def _resume_with_approval_actually_executes(client):
    """A fresh tier-4 task, this time approved -- must actually run
    through delegation and verification for real, using exactly the
    contract that was shown for sign-off (never re-classified).
    """
    registry.clear()
    message = "Modify the ir.model.access.csv permissions for a second, distinct model."
    proposal = await run_turn(
        message, client, CLASSIFIER_MODEL,
        anticipated_scope={"touches_schema_or_permissions": True},
    )
    assert proposal["status"] == "paused"
    task_id = proposal["task_id"]

    registry.register(SpecialistType.bug_fix, ConfigurableFakeSpecialist(claims_complete=True))
    registry.register(SpecialistType.testing_qa, ConfigurableFakeSpecialist(claims_complete=True))

    result = await resume_after_sign_off(task_id, approved=True, client=client, classifier_model=CLASSIFIER_MODEL)

    assert result["status"] == "completed"
    assert result["passed"] is True
    assert result["tier"] == 4
    assert get_pending_contract(task_id) is None

    rows = _get_decision_rows(task_id)
    outcome_rows = [r for r in rows if r["event_type"] == "outcome"]
    assert outcome_rows, f"expected a real 'outcome' memory row for approved task {task_id}"
    print(f"PASS: approving a tier-4 sign-off actually executes the SAME contract shown for approval -- "
          f"delegated for real, verified, outcome written to memory (row #{outcome_rows[0]['id']})")
    registry.clear()


async def _resume_unknown_task_id_returns_a_clean_error(client):
    result = await resume_after_sign_off("00000000-0000-0000-0000-000000000000", approved=True, client=client, classifier_model=CLASSIFIER_MODEL)
    assert result["status"] == "error"
    print("PASS: resuming an unknown/expired task_id returns a clean error, not a crash")


async def _run_plan_tier4_item_pauses_before_touching_any_specialist(client):
    """Real, confirmed bug found live (2026-07-23, Phase 22 plan):
    run_turn()'s own tier-3/4 gate never applied to run_plan()'s outer-
    plan executor at all -- a tier-4 plan item would have gone straight
    through to a specialist with no sign-off. Same proof technique as
    the run_turn() test above: register NOTHING for any specialist, so
    if delegation were ever attempted this would raise
    SpecialistNotAvailableError instead of returning a clean pause.
    """
    registry.clear()
    item = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_4_presign_off,
        goal="Modify the ir.model.access.csv permissions for a run_plan sign-off regression test.",
        inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10, plan_item_id="signoff-plan-1",
    )
    result = await run_plan("run_plan sign-off gate regression test", [item], client, CLASSIFIER_MODEL)

    assert result["status"] == "paused"
    assert result["paused_on"] == "signoff-plan-1"
    pause_result = result["pause_result"]
    assert pause_result["reason"] == "sign_off_required"
    assert pause_result["contract"].tier.value == 4

    task_id = pause_result["task_id"]
    payload = get_pending_contract(task_id)
    assert payload is not None, "the pending contract must actually be stored in Redis"
    assert payload["contract"]["tier"] == 4
    assert task_id in list_pending_task_ids()
    print(f"PASS: a tier-4 run_plan() item pauses for sign-off with NOTHING registered for any "
          f"specialist -- confirmed no delegation was ever attempted (task {task_id})")


async def _diagnose_first_task_pauses_before_touching_any_specialist(client):
    """Phase 22 (2026-07-23): the new diagnose-then-confirm opt-in mode
    -- orthogonal to tier (a plain tier-1 task here, NOT sensitive),
    triggered via anticipated_scope["diagnose_first"]. Same proof
    technique as every other pause test in this file: register NOTHING
    for any specialist, so if delegation were ever attempted this
    would raise SpecialistNotAvailableError instead of a clean pause.
    Confirms the reason/task-state are DISTINCT from the plain
    sign_off_required case, and that a real diagnosis was generated.
    """
    registry.clear()
    message = "Add a new text field called internal_note to the res.partner model."
    result = await run_turn(
        message, client, CLASSIFIER_MODEL,
        anticipated_scope={"diagnose_first": True},
    )

    assert result["status"] == "paused"
    assert result["reason"] == "diagnosis_confirmation"
    assert result["contract"].tier.value == 1, "this is a plain, non-sensitive task -- tier gate must not fire"
    assert "diagnosis" in result
    assert result["diagnosis"]["understanding"]
    assert result["diagnosis"]["plan"]

    payload = get_pending_contract(result["task_id"])
    assert payload is not None, "the pending contract must actually be stored in Redis"
    assert payload["diagnosis"] is not None
    assert result["task_id"] in list_pending_task_ids()
    print(f"PASS: a diagnose-first task pauses for plan confirmation with NOTHING registered for any "
          f"specialist -- confirmed no delegation was ever attempted, real diagnosis generated "
          f"(task {result['task_id']}): understanding={payload['diagnosis']['understanding']!r}")
    return result["task_id"]


async def _diagnosis_resume_with_rejection_uses_correct_wording(client, task_id: str):
    result = await resume_after_sign_off(task_id, approved=False, client=client, classifier_model=CLASSIFIER_MODEL)
    assert result["status"] == "rejected"
    assert get_pending_contract(task_id) is None

    rows = _get_decision_rows(task_id)
    rejected_rows = [r for r in rows if r["event_type"] == "decision" and "diagnosis_confirmation" in (r["tags"] or [])]
    assert rejected_rows, f"expected a 'decision'/'diagnosis_confirmation' memory row for task {task_id}"
    assert "plan" in rejected_rows[0]["summary"].lower() or "proposed" in rejected_rows[0]["summary"].lower(), (
        "rejection wording for a diagnosis-flow task must talk about a rejected PLAN, not a tier"
    )
    print(f"PASS: rejecting a diagnose-first confirmation cancels cleanly with the correct, distinct "
          f"wording (not the tier-based sign-off phrasing) -- row #{rejected_rows[0]['id']}")


async def _diagnosis_resume_with_approval_actually_executes(client):
    registry.clear()
    message = "Add a new text field called internal_note2 to the res.partner model."
    proposal = await run_turn(
        message, client, CLASSIFIER_MODEL,
        anticipated_scope={"diagnose_first": True},
    )
    assert proposal["status"] == "paused"
    task_id = proposal["task_id"]

    registry.register(SpecialistType.bug_fix, ConfigurableFakeSpecialist(claims_complete=True))
    registry.register(SpecialistType.testing_qa, ConfigurableFakeSpecialist(claims_complete=True))

    result = await resume_after_sign_off(task_id, approved=True, client=client, classifier_model=CLASSIFIER_MODEL)

    assert result["status"] == "completed"
    assert result["passed"] is True
    assert get_pending_contract(task_id) is None

    rows = _get_decision_rows(task_id)
    outcome_rows = [r for r in rows if r["event_type"] == "outcome"]
    assert outcome_rows, f"expected a real 'outcome' memory row for approved diagnosis-flow task {task_id}"
    print(f"PASS: approving a diagnose-first confirmation actually executes the SAME contract shown -- "
          f"delegated for real, verified, outcome written to memory (row #{outcome_rows[0]['id']})")
    registry.clear()


async def main():
    client = ModelGatewayClient()
    try:
        task_id = await _run_tier4_task_pauses_before_touching_any_specialist(client)
        await _resume_with_rejection_cancels_cleanly(client, task_id)
        await _resume_with_approval_actually_executes(client)
        await _resume_unknown_task_id_returns_a_clean_error(client)
        await _run_plan_tier4_item_pauses_before_touching_any_specialist(client)
        diag_task_id = await _diagnose_first_task_pauses_before_touching_any_specialist(client)
        await _diagnosis_resume_with_rejection_uses_correct_wording(client, diag_task_id)
        await _diagnosis_resume_with_approval_actually_executes(client)
    finally:
        await client.aclose()
    print("\nALL MANAGER SIGN-OFF TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
