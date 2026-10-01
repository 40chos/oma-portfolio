"""Phase 16: GET /api/tasks' real aggregation query, run against real
Postgres/Redis and the real Manager loop -- no mocks.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from contracts.schema import SpecialistType
from infra.gateway_client import ModelGatewayClient
from manager.dashboard import list_recent_tasks
from manager.loop import run_turn
from specialists import registry

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


async def _run_test():
    registry.clear()
    registry.register(SpecialistType.code_review, _RealPassFake())
    client = ModelGatewayClient()
    try:
        message = f"Do a full read-only quality audit of the custom Odoo codebase. marker={uuid.uuid4().hex[:8]}"
        result = await run_turn(
            message, client, CLASSIFIER_MODEL,
            anticipated_scope={"contract_inputs": ["full_codebase_audit:garazd_product_label"]},
        )
        assert result["status"] == "completed" and result["passed"] is True

        dashboard = list_recent_tasks(limit=100)
        all_items = dashboard["needs_attention"] + dashboard["in_progress"] + dashboard["completed"]
        matching = [i for i in all_items if i["task_id"] == result["task_id"]]
        assert matching, f"task {result['task_id']} must appear somewhere in the dashboard"
        item = matching[0]
        assert item["kind"] == "standalone"
        assert item["status"] == "passed"
        assert message.split(" marker=")[0] in item["title"] or "marker=" in item["title"], (
            f"the dashboard's title for a standalone task must be the real original goal text, "
            f"got: {item['title']!r}"
        )
        # It must land in the "completed" section specifically, not just anywhere.
        assert any(i["task_id"] == result["task_id"] for i in dashboard["completed"]), (
            f"a passed standalone task must be in the completed section: {dashboard}"
        )
        print(f"PASS: a real, passed standalone task appears in GET /api/tasks' real dashboard "
              f"query, correctly in the 'completed' section, with its real original goal text as "
              f"the title (not a placeholder) -- task {result['task_id']}")
    finally:
        await client.aclose()
        registry.clear()


def _test_dismissed_escalation_shows_a_real_terminal_state():
    """Real regression test for a live bug found in the Phase 16 QA
    pass: dismissing an escalation (POST /api/escalations/{id}/dismiss)
    only ever cleared the ephemeral Redis pending-escalation entry --
    no terminal Postgres row was ever written for a PauseForOperator
    escalation. Once dismissed, the dashboard had nothing left to say
    the task ever paused, so it fell back to the stale "running" Redis
    flag from mark_task_running() and showed the task as perpetually
    in-progress, confirmed live via the actual chat UI.

    Fixed: dismissal now writes a real decision event (tags=
    ["escalation_dismissed"]), and manager.dashboard._cancelled_row_for_task()
    recognizes it as a real terminal state alongside cancelled_by_operator.
    """
    import uuid

    from manager.escalations import clear_pending_escalation, store_pending_escalation
    from manager.gateway_orchestration import mark_task_running
    from manager.tools import append_project_memory

    task_id = str(uuid.uuid4())
    append_project_memory(
        event_type="task_created", actor="manager", task_id=task_id,
        module=None, summary="Escalation-dismiss dashboard regression test", tags=["module_dev"],
        detail={"inputs": []},
    )
    mark_task_running(task_id)
    store_pending_escalation(task_id, "Real test escalation.", [])

    # Before dismissal: genuinely paused/needs-attention.
    before = list_recent_tasks(limit=200)
    before_item = next(i for i in before["needs_attention"] if i["task_id"] == task_id)
    assert before_item["status"] == "paused"

    # The real dismiss action -- same two calls the actual endpoint makes.
    clear_pending_escalation(task_id)
    append_project_memory(
        event_type="decision", actor="operator", task_id=task_id, module=None,
        summary="Operator dismissed this escalation without a further retry.",
        tags=["escalation_dismissed"], detail={}, verified=True,
    )

    after = list_recent_tasks(limit=200)
    all_after = after["needs_attention"] + after["in_progress"] + after["completed"]
    after_item = next(i for i in all_after if i["task_id"] == task_id)
    assert after_item["status"] == "failed", f"expected a real terminal state after dismissal, got: {after_item}"
    assert any(i["task_id"] == task_id for i in after["completed"]), (
        f"a dismissed escalation must land in 'completed' (terminal, non-retrying), not linger as "
        f"'in_progress': {after}"
    )
    print("PASS: dismissing an escalation now writes a real terminal Postgres decision row -- "
          "the dashboard correctly shows it as completed/failed, not stuck showing 'running' forever")


def _test_decomposed_task_intermediate_outcome_does_not_show_as_completed():
    """Real bug found live (2026-07-12) verifying Operator's original
    8-constraint service-management task after the scaffold-boilerplate
    fix: manager.loop._execute_contract() writes a real, durable
    `outcome` Postgres row every time ANY round passes, and
    manager.loop._run_decomposed_task() reuses the SAME task_id across
    every sub-contract of a multi-constraint task. Since
    manager.dashboard._standalone_task_status() used to treat ANY
    outcome row as proof the whole task was COMPLETED, a decomposed
    task's first constraint passing made the dashboard show the WHOLE
    8-constraint task as done while constraints 2-8 were still actively
    running underneath -- confirmed live via /api/tasks showing
    "completed"/"passed" while Redis genuinely said "running" and a
    fresh trace event was streaming that same second.

    Fixed two ways: (1) _standalone_task_status() now only trusts an
    outcome row as terminal when Redis does NOT say "running";
    (2) manager.task_state.clear_task_state() (which existed but had
    zero callers anywhere) is now actually called at _run_decomposed_task()'s
    real final return and at run_turn()'s own non-decomposed completion,
    so a genuinely finished task doesn't hang showing "running" for its
    full 24h Redis TTL either.
    """
    import uuid

    from manager.gateway_orchestration import mark_task_running
    from manager.task_state import clear_task_state
    from manager.tools import append_project_memory

    task_id = str(uuid.uuid4())
    append_project_memory(
        event_type="task_created", actor="manager", task_id=task_id,
        module=None, summary="Decomposed-task dashboard regression test", tags=["module_dev"],
        detail={"inputs": []},
    )
    mark_task_running(task_id)
    # Simulates _execute_contract()'s own outcome-row write for sub-contract
    # 1 of N passing -- the exact shape a decomposed task's intermediate
    # pass produces.
    append_project_memory(
        event_type="outcome", actor="manager", task_id=task_id, module=None,
        summary="Sub-contract 1/8 passed.", tags=["module_dev"],
        detail={"passed": True, "root_cause": None, "rounds_taken": 3, "capability_class": "module_dev"},
        verified=True,
    )

    # While Redis still says "running" (later sub-contracts genuinely still
    # in flight), the task must NOT show as completed despite the real,
    # durable outcome row that already exists.
    mid_flight = list_recent_tasks(limit=200)
    mid_flight_all = mid_flight["needs_attention"] + mid_flight["in_progress"] + mid_flight["completed"]
    mid_item = next(i for i in mid_flight_all if i["task_id"] == task_id)
    assert mid_item["status"] != "passed", (
        f"an intermediate sub-contract's own outcome row must not make a still-running decomposed "
        f"task show as fully completed: {mid_item}"
    )
    assert not any(i["task_id"] == task_id for i in mid_flight["completed"]), (
        f"task must not be in 'completed' while Redis still says running: {mid_flight}"
    )

    # Once the decomposition genuinely finishes (clear_task_state(), the
    # real fix's own call), the same outcome row now correctly means done.
    clear_task_state(task_id)
    after = list_recent_tasks(limit=200)
    after_all = after["needs_attention"] + after["in_progress"] + after["completed"]
    after_item = next(i for i in after_all if i["task_id"] == task_id)
    assert after_item["status"] == "passed", f"expected completed/passed once genuinely done: {after_item}"
    assert any(i["task_id"] == task_id for i in after["completed"]), after
    print("PASS: an intermediate sub-contract's own outcome row no longer makes a still-running "
          "decomposed task show as fully completed -- only clear_task_state() at genuine completion does")


def _test_decomposed_task_paused_on_gateway_outage_does_not_show_as_completed():
    """Real, more general bug in the SAME family as the test above,
    found live (2026-07-21, Phase 20 Area 2, task #43, an uninterrupted
    7-task retest pass run specifically to get an honest snapshot). The
    earlier fix only ever checked Redis state `!= STATE_RUNNING` -- true
    for a genuinely finished task, but ALSO true for every `paused:*`
    state (gateway outage, ambiguity, sign-off, repeated failure,
    round-budget-exhausted, task-cut-off). Confirmed live: constraint 1
    wrote a real `outcome=succeeded` row, constraint 2 then hit a
    genuine LLM gateway failure mid-round
    (run_with_gateway_outage_handling() correctly set Redis state to
    PAUSE_GATEWAY_UNAVAILABLE), and /api/tasks reported the WHOLE task
    COMPLETED/passed using constraint 1's own stale intermediate outcome
    row -- independently verified against the live DB that the claimed
    access rule/group from constraint 2 never actually got created.

    Fixed to the precise, general condition: an outcome row is only
    trusted as the task's real final word when Redis holds NO state at
    all for this task_id, not merely "not literally running".
    """
    import uuid

    from manager.gateway_orchestration import mark_task_running
    from manager.task_state import PAUSE_GATEWAY_UNAVAILABLE, set_task_state
    from manager.tools import append_project_memory

    task_id = str(uuid.uuid4())
    append_project_memory(
        event_type="task_created", actor="manager", task_id=task_id,
        module=None, summary="Gateway-outage-pause dashboard regression test", tags=["module_dev"],
        detail={"inputs": []},
    )
    mark_task_running(task_id)
    # Simulates _execute_contract()'s own outcome-row write for
    # constraint 1/2 passing -- the exact shape a decomposed task's
    # intermediate pass produces.
    append_project_memory(
        event_type="outcome", actor="manager", task_id=task_id, module=None,
        summary="Sub-contract 1/2 passed.", tags=["module_dev"],
        detail={"passed": True, "root_cause": None, "rounds_taken": 1, "capability_class": "module_dev"},
        verified=True,
    )
    # Simulates run_with_gateway_outage_handling()'s own real call
    # during constraint 2's round -- Redis state is now "paused:
    # gateway_unavailable", genuinely NOT "running", but also genuinely
    # NOT done.
    set_task_state(task_id, PAUSE_GATEWAY_UNAVAILABLE)

    result = list_recent_tasks(limit=200)
    all_items = result["needs_attention"] + result["in_progress"] + result["completed"]
    item = next(i for i in all_items if i["task_id"] == task_id)
    assert item["status"] != "passed", (
        f"a decomposed task paused on a gateway outage mid-constraint must never show as passed "
        f"using an earlier constraint's own stale outcome row: {item}"
    )
    assert not any(i["task_id"] == task_id for i in result["completed"]), (
        f"task must not be in 'completed' while genuinely paused on a gateway outage: {result}"
    )
    assert any(i["task_id"] == task_id for i in result["needs_attention"]), (
        f"a gateway-outage pause must correctly land in needs_attention, not silently disappear: {result}"
    )
    print("PASS: a decomposed task paused on a gateway outage mid-constraint no longer shows as "
          "falsely completed/passed using an earlier constraint's own stale intermediate outcome row")


def _test_terminated_stuck_call_row_does_not_outrank_a_later_real_resume():
    """Real bug found live (2026-07-21, live during a Operator demo, task
    'warranty.claim'): a `terminated_stuck_call` decision row (written
    when an external service restart orphans an in-flight resume -- see
    ui/chat/server.py's own startup cleanup) is real and durable, but
    only up until the task is legitimately resumed again. Confirmed
    live: after restarting the service to clear a stale
    `oma:resume_lock:*` key (the documented, correct remedy for that
    specific problem) and firing a fresh POST /api/tasks/{id}/continue,
    the dashboard kept reporting COMPLETED/failed with the OLD "stuck
    on a hung call" message even while the fresh resume was genuinely,
    actively running (confirmed via direct DB inspection: the most
    recent durable event was a "resumed" branch_message, no new
    termination row existed at all).

    Fixed: a cancelled/terminated row is only trusted as the task's
    final word when Redis does NOT currently say the task is running --
    a fresh resume's own mark_task_running() call is newer, more
    current evidence than an older cancellation row.
    """
    import uuid

    from manager.gateway_orchestration import mark_task_running
    from manager.task_state import clear_task_state
    from manager.tools import append_project_memory

    task_id = str(uuid.uuid4())
    append_project_memory(
        event_type="task_created", actor="manager", task_id=task_id,
        module=None, summary="Stuck-call-then-resumed dashboard regression test", tags=["module_dev"],
        detail={"inputs": []},
    )
    mark_task_running(task_id)
    # Simulates ui/chat/server.py's own startup cleanup: it clears the
    # Redis "running" flag FIRST, THEN writes the terminated_stuck_call
    # decision row (see that code's own ordering) -- so immediately
    # after a real stuck-call termination, Redis state is genuinely
    # absent, not "running".
    clear_task_state(task_id)
    append_project_memory(
        event_type="decision", actor="manager", task_id=task_id, module=None,
        summary="Stopped -- was stuck on a hung call: orphaned by an external service restart.",
        tags=["cancelled_by_operator", "terminated_stuck_call"], detail={}, verified=True,
    )

    # Immediately after the stuck-call termination, with no resume yet:
    # genuinely completed/failed.
    stuck = list_recent_tasks(limit=200)
    stuck_all = stuck["needs_attention"] + stuck["in_progress"] + stuck["completed"]
    stuck_item = next(i for i in stuck_all if i["task_id"] == task_id)
    assert stuck_item["status"] == "failed", f"expected failed right after the stuck-call row: {stuck_item}"
    assert any(i["task_id"] == task_id for i in stuck["completed"]), stuck

    # A real, fresh resume (POST /api/tasks/{id}/continue's own first
    # step) calls mark_task_running() again -- this is newer evidence
    # than the earlier stuck-call row.
    mark_task_running(task_id)

    resumed = list_recent_tasks(limit=200)
    resumed_all = resumed["needs_attention"] + resumed["in_progress"] + resumed["completed"]
    resumed_item = next(i for i in resumed_all if i["task_id"] == task_id)
    assert resumed_item["status"] == "running", (
        f"a task legitimately resumed after a stuck-call termination must show as running again, "
        f"not stuck showing the old 'Stopped -- was stuck on a hung call' message forever: {resumed_item}"
    )
    assert any(i["task_id"] == task_id for i in resumed["in_progress"]), resumed
    print("PASS: a legitimate resume after a stuck-call termination correctly shows as running again, "
          "the old cancellation row no longer outranks the fresh mark_task_running() signal")


class _RealPassFake:
    """A fake code_review specialist that always claims success -- used
    here only to keep this test fast and deterministic; the dashboard
    QUERY being tested is 100% real (real Postgres, real Redis, real
    task_created event, real outcome row).
    """
    async def run(self, contract):
        from contracts.schema import SpecialistOutput
        return SpecialistOutput(
            task_id=contract.task_id, specialist_type=contract.specialist_type,
            summary="Real audit completed for dashboard test.",
            detail={"findings": []}, claims_complete=True, artifacts=[],
        )


if __name__ == "__main__":
    asyncio.run(_run_test())
    _test_dismissed_escalation_shows_a_real_terminal_state()
    _test_decomposed_task_intermediate_outcome_does_not_show_as_completed()
    _test_decomposed_task_paused_on_gateway_outage_does_not_show_as_completed()
    _test_terminated_stuck_call_row_does_not_outrank_a_later_real_resume()
    print("\nALL DASHBOARD TESTS PASSED")
