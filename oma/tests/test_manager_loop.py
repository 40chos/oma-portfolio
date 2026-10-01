"""Phase 6 tests: the full phased loop, tying phases 3/4/5 together --
including step 8's explicit requirement: re-run Phase 5's
fake-specialist round-trip through the FULL loop, not in isolation, and
confirm a message that should route to the fake specialist actually
does, gets a fake verified result back, and gets written to memory
correctly.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import psycopg2.extras

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistOutput, SpecialistType, TaskContract
from infra.gateway_client import ModelGatewayClient
from infra.redis_client import get_redis_client
from infra.settings import load_postgres_settings
from manager.charter import load_manager_constitution, load_sensitive_paths
from manager.loop import _execute_contract, run_turn, select_specialist_and_validator
from manager.task_state import PAUSE_ROUND_BUDGET_EXHAUSTED, STATE_RUNNING
from specialists import registry

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


class ConfigurableFakeSpecialist:
    """A fake specialist whose verdict is configurable per instance --
    lets the same mechanism prove both the passed=True and passed=False
    paths through the full loop.
    """
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


def _get_outcome_row(task_id: str) -> dict | None:
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM agent_memory_events WHERE task_id = %s AND event_type = 'outcome'",
            (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def _run_full_loop_success_path(client):
    registry.clear()
    registry.register(SpecialistType.bug_fix, ConfigurableFakeSpecialist(claims_complete=True))
    registry.register(SpecialistType.testing_qa, ConfigurableFakeSpecialist(claims_complete=True))

    message = "Add a 'preferred_language' field to contacts, visible on the contact form."
    result = await run_turn(message, client, CLASSIFIER_MODEL)

    assert result["status"] == "completed"
    assert result["passed"] is True
    assert result["capability_class"] == "module_dev"
    assert result["constitution_loaded_chars"] > 0, "MANAGER_CONSTITUTION.md must be loaded in full every turn"
    assert result["sensitive_paths_rule_count"] > 0, "sensitive_paths.yaml must be loaded in full every turn"

    row = _get_outcome_row(result["task_id"])
    assert row is not None, "the outcome must actually be written to agent_memory_events"
    assert row["detail"]["passed"] is True
    assert "succeeded" in row["tags"]
    print(f"PASS: full loop, success path -- task {result['task_id']} routed to the fake specialist, "
          f"got a fake verified PASS result back, and the outcome was correctly written to memory "
          f"(row #{row['id']})")

    registry.clear()


async def _run_full_loop_failure_path(client):
    """A read-only audit message -- per Phase 12's routing fix, this
    correctly goes to code_review (never bug_fix, which explicitly
    refuses capability_class=readonly), with validation_by=None (no
    second specialist independently verifies a pure audit report).
    Deliberately does NOT register anything for bug_fix or testing_qa,
    to prove neither is ever touched for this task shape.

    Phase 15 (§19.6): a failure no longer comes back as a single
    {"status": "completed", "passed": False} result -- it now escalates
    to Operator via PauseForOperator once the round budget is exhausted.
    planning_round_budget=1 (via anticipated_scope, the same
    caller-supplied-hint pattern as contract_inputs) keeps this test
    fast: round 1 fails, round_number(1) >= planning_round_budget(1)
    immediately, so it escalates without ever attempting a retry.
    """
    registry.clear()
    registry.register(SpecialistType.code_review, ConfigurableFakeSpecialist(claims_complete=False))

    message = "Do a full read-only quality audit of the custom Odoo codebase."
    result = await run_turn(
        message, client, CLASSIFIER_MODEL,
        anticipated_scope={"planning_round_budget": 1},
    )

    assert result["status"] == "paused"
    assert result["reason"] == "ask_operator"
    assert "prior_rounds" in result
    print(f"PASS: full loop, failure path -- task {result['task_id']} got a fake verified FAIL result, "
          f"and with planning_round_budget=1 correctly escalated to Operator (PauseForOperator) rather than "
          f"retrying blindly or reporting a bare 'completed, passed=False' -- message: {result['message']!r}")

    registry.clear()


async def _run_escalation_marks_redis_state_paused_not_running(client):
    """Real, confirmed bug found live (2026-07-22, Operator demo prep): the
    PauseForOperator handler in manager.loop._execute_contract() released
    the module lock and stored the pending escalation, but never
    transitioned oma:task:<task_id>:state away from STATE_RUNNING (set
    by mark_task_running() at the top of every round) -- only a human
    manually clicking Pause in the UI ever did that. Confirmed live via
    direct Redis/Postgres inspection: two genuinely, correctly escalated
    tasks -- "waiting on Operator", exactly as intended -- both still showed
    oma:task:<id>:state == "running" hours/moments later. The service's
    own startup reconciliation sweep (ui/chat/server.py's lifespan())
    treats ANY such stale "running" key with no recent trace activity as
    an orphaned, process-killed task and writes a false
    "terminated_stuck_call" decision row, corrupting the dashboard's
    displayed status for a task that was never actually stuck at all --
    it had correctly, deliberately stopped to wait for a human.

    This test drives a REAL escalation directly through
    _execute_contract() (same component _run_full_loop_failure_path
    exercises via run_turn(), just called one layer lower -- bypassing
    Phase 1/2's correction/ambiguity-detection gate, which this session's
    live project-memory content made non-deterministic for run_turn()'s
    own generic "quality audit" phrasing, unrelated to what this test
    actually verifies) with planning_round_budget=1, so round 1 fails
    and escalates immediately without ever attempting a retry. Confirms
    Redis is left in a genuine paused:* state, matching what the manual
    Pause button already produces -- never STATE_RUNNING once a real
    PauseForOperator has fired.
    """
    registry.clear()
    registry.register(SpecialistType.code_review, ConfigurableFakeSpecialist(claims_complete=False))

    task_id = str(uuid.uuid4())
    contract = TaskContract(
        task_id=task_id, specialist_type=SpecialistType.code_review,
        capability_class=CapabilityClass.readonly_investigation, tier=AutonomyTier.tier_2_notify_after,
        goal="Do a full read-only quality audit of the custom Odoo codebase, second probe.",
        inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by=None, pause_if=[], turn_budget=10, planning_round_budget=1,
    )
    result = await _execute_contract(
        contract, f"task:{task_id}", client, CLASSIFIER_MODEL,
        {"status": "not_a_correction"}, None, "", "constitution text", [],
    )
    assert result["status"] == "paused"
    assert result["reason"] == "ask_operator"
    redis_client = get_redis_client()
    state = redis_client.get(f"oma:task:{task_id}:state")
    assert state != STATE_RUNNING, (
        f"a genuinely escalated task must never be left at STATE_RUNNING in Redis -- the next "
        f"service restart's startup sweep would mistake it for an orphaned/crashed process and "
        f"overwrite its real status with a false 'stuck call' decision. Got state={state!r}"
    )
    assert state == PAUSE_ROUND_BUDGET_EXHAUSTED, (
        f"expected the same paused state the manual Pause button already sets, got {state!r}"
    )
    print(
        f"PASS: task {task_id} escalated via PauseForOperator and Redis correctly shows "
        f"{state!r}, not STATE_RUNNING -- safe from the startup-sweep false-positive"
    )

    registry.clear()


async def _run_unregistered_specialist_raises_clearly():
    """If the loop ever tried to route to a specialist type with
    nothing registered, it must fail loudly (SpecialistNotAvailableError
    propagating), never silently proceed as if delegation succeeded.
    """
    registry.clear()
    client = ModelGatewayClient()
    try:
        raised = False
        try:
            await run_turn("Add a field to contacts.", client, CLASSIFIER_MODEL)
        except registry.SpecialistNotAvailableError:
            raised = True
        assert raised, "expected SpecialistNotAvailableError when nothing is registered"
        print("PASS: with nothing registered, the loop fails loudly rather than silently proceeding")
    finally:
        await client.aclose()


def test_select_specialist_and_validator_routes_by_capability_class():
    assert select_specialist_and_validator("readonly") == (SpecialistType.code_review, None)
    assert select_specialist_and_validator("module_dev") == (SpecialistType.bug_fix, "testing_qa")
    assert select_specialist_and_validator("data_change") == (SpecialistType.bug_fix, "testing_qa")
    print("PASS: select_specialist_and_validator() routes readonly -> code_review (validation_by=None), "
          "module_dev/data_change -> bug_fix (validation_by=testing_qa)")


async def _run_readonly_task_never_touches_bug_fix_or_testing_qa(client):
    """The actual bug this phase fixes, proven end to end: a real,
    live-classified readonly-shaped message must route to code_review
    and complete successfully WITHOUT anything registered for bug_fix
    or testing_qa at all -- if the old hardcoded routing were still in
    place, this would raise SpecialistNotAvailableError (bug_fix never
    registered) instead of completing.
    """
    registry.clear()
    registry.register(SpecialistType.code_review, ConfigurableFakeSpecialist(claims_complete=True))

    message = "Do a full read-only quality audit of the custom Odoo codebase -- look for hardcoded values, dead code, and inconsistent patterns."
    result = await run_turn(message, client, CLASSIFIER_MODEL)

    assert result["status"] == "completed", (
        f"expected a completed result with only code_review registered; got {result!r}"
    )
    assert result["capability_class"] == "readonly"
    assert result["passed"] is True
    assert result["verification"].spot_check_mismatch is False
    assert result["verification"].uncovered_paths == []
    print("PASS: a real, live-classified readonly task completes successfully with ONLY code_review "
          "registered -- bug_fix and testing_qa are never touched at all for this task shape, "
          "confirming the routing fix actually works end to end")

    registry.clear()


def test_constitution_and_sensitive_paths_load_in_full():
    text = load_manager_constitution()
    rules = load_sensitive_paths()
    assert "Autonomy tiers" in text and len(text) > 500
    assert len(rules) >= 9
    print("PASS: both charter files load in full, substantial real content (not summarized)")


async def main():
    client = ModelGatewayClient()
    try:
        await _run_full_loop_success_path(client)
        await _run_full_loop_failure_path(client)
        await _run_escalation_marks_redis_state_paused_not_running(client)
        await _run_readonly_task_never_touches_bug_fix_or_testing_qa(client)
    finally:
        await client.aclose()
    await _run_unregistered_specialist_raises_clearly()
    test_select_specialist_and_validator_routes_by_capability_class()
    test_constitution_and_sensitive_paths_load_in_full()
    print("\nALL MANAGER LOOP TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
