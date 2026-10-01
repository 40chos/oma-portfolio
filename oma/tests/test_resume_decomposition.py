"""Real, direct tests for the resume-decomposition bug root-caused live
(2026-07-12): resume_task_after_checkpoint() used to call
_execute_contract() for just the one sub-contract a checkpoint happened
to capture, with zero decomposition awareness -- so once ANY constraint
paused mid-decomposition, every subsequent /continue re-ran that SAME
already-checkpointed constraint forever, never advancing. Confirmed
live: 7 consecutive "constraint passes" on a real 8-constraint task
were all silently re-verifying the first constraint; the module on
disk never grew past its own first field despite the DB history
showing what looked like 7 real successive passes.

These tests exercise _run_constraint_labels_from() directly -- the
shared sequencing logic both a fresh _run_decomposed_task() run and a
real resume now drive -- by mocking _execute_contract() itself, so no
real specialists/model gateway calls are needed. Plain `def test_...()`
functions, no async test runner beyond asyncio.run() per this project's
own established two-tier discipline (see test_constraint_pinning.py).

Phase 25A rewrite (2026-07-25): the original version of this file tested
_reconstruct_resume_order()'s OLD behavior -- recovering constraint order
by regex-parsing it back out of goal prose, specifically because
constraint_status's own dict key order could not be trusted after a
Postgres jsonb round-trip. That mechanism is gone: contract.constraint_order
(contracts/schema.py) is now a real, explicit list[str] field, set once
and carried forward unchanged by every model_copy() -- JSON arrays,
unlike JSON objects, DO preserve order, so no reconstruction-from-prose
is needed at all. These tests now exercise the field directly instead of
the regex it replaces.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import _reconstruct_resume_order, _run_constraint_labels_from, _run_decomposed_task


def _make_decomposable_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A; add field B; add field C.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_status={},
    )


def _focus_of(sub_contract: TaskContract) -> str:
    """Phase 25A: reads the real structured field directly -- no more
    regex-matching it back out of sub_contract.goal.
    """
    assert sub_contract.current_constraint_label, (
        f"sub_contract.current_constraint_label was never set: goal={sub_contract.goal!r}"
    )
    return sub_contract.current_constraint_label


def test_run_constraint_labels_from_continues_past_a_resumed_index():
    """The actual fix: starting mid-way through constraint_labels (as a
    real resume now does, instead of always restarting from index 0 or
    getting permanently stuck re-running the checkpointed constraint)
    must genuinely execute only the REMAINING constraints, in order,
    never re-visiting the ones already marked "satisfied".
    """
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b", "constraint_c"]
    satisfied = {"constraint_a": "satisfied", "constraint_b": "pending", "constraint_c": "pending"}
    seen_focuses = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_focuses.append(_focus_of(sub_contract))
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state") as mock_clear:
                result = await _run_constraint_labels_from(
                    contract, labels, satisfied, 1,  # resume starting at index 1 ("constraint_b")
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )
                return result, mock_clear

    result, mock_clear = asyncio.run(run())

    assert seen_focuses == ["constraint_b", "constraint_c"], (
        f"resuming from index 1 must execute ONLY constraint_b then constraint_c, never re-running "
        f"the already-satisfied constraint_a: got {seen_focuses}"
    )
    assert result["passed"] is True
    assert result["decomposition"]["completed_sub_contracts"] == 3
    mock_clear.assert_called_once()
    print("PASS: _run_constraint_labels_from() resuming mid-decomposition executes only the "
          "remaining constraints in order, never re-running an already-satisfied one")


def test_run_constraint_labels_from_stops_on_first_failure_without_finalizing():
    """A genuine failure/pause partway through the remaining constraints
    must stop there (return early) and must NOT call clear_task_state()
    -- the task genuinely isn't done yet.
    """
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b", "constraint_c"]
    satisfied = {"constraint_a": "satisfied", "constraint_b": "pending", "constraint_c": "pending"}
    seen_focuses = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        focus = _focus_of(sub_contract)
        seen_focuses.append(focus)
        if focus == "constraint_b":
            return {"status": "paused", "passed": False, "task_id": str(sub_contract.task_id)}
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state") as mock_clear:
                result = await _run_constraint_labels_from(
                    contract, labels, satisfied, 1,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )
                return result, mock_clear

    result, mock_clear = asyncio.run(run())

    assert seen_focuses == ["constraint_b"], (
        f"a pause on constraint_b must stop the loop immediately -- constraint_c must never run: "
        f"got {seen_focuses}"
    )
    assert result["passed"] is False
    assert result["decomposition"]["completed_sub_contracts"] == 1
    mock_clear.assert_not_called()
    print("PASS: a pause partway through resumed constraints stops the loop and does not "
          "prematurely clear task state")


def test_run_constraint_labels_from_names_not_yet_in_scope_constraints():
    """Real, general bug found live (2026-07-12): the sub-contract goal
    only ever named ALREADY-satisfied constraints explicitly ("don't
    remove these"); there was no symmetric list naming constraints that
    are NOT yet in scope ("don't add these yet"). Confirmed live: Build
    kept bundling later constraints' fields into the current round for
    20+ rounds across 3 escalation cycles despite the "focus ONLY on X"
    instruction, because the full original multi-requirement goal text
    was still right there in the prompt pulling attention toward
    everything at once. This test locks in the fix: the goal_text for
    each sub-contract must explicitly name every constraint label other
    than the current focus and any already-satisfied ones -- AND (Phase
    25A) that the same fact is also set as a real structured field.
    """
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b", "constraint_c"]
    satisfied = {"constraint_a": "satisfied", "constraint_b": "pending", "constraint_c": "pending"}
    seen_goals = {}
    seen_remaining = {}

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        focus = _focus_of(sub_contract)
        seen_goals[focus] = sub_contract.goal
        seen_remaining[focus] = sub_contract.remaining_constraint_labels
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                await _run_constraint_labels_from(
                    contract, labels, satisfied, 1,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    asyncio.run(run())

    assert "constraint_c" in seen_goals["constraint_b"], (
        "constraint_b's own goal_text must explicitly name constraint_c as NOT yet in scope"
    )
    assert "constraint_a" not in seen_goals["constraint_b"].split("NOT yet in scope")[-1], (
        "constraint_a is already satisfied, not merely 'not yet in scope' -- it must not appear "
        "in the not-yet-in-scope list (it has its own ALREADY-satisfied sentence instead)"
    )
    assert "NOT yet in scope" not in seen_goals["constraint_c"], (
        "constraint_c is the last constraint -- there's nothing left to name as not-yet-in-scope, "
        "so that sentence must not appear at all for the final round"
    )
    assert seen_remaining["constraint_b"] == ["constraint_c"], (
        "the structured remaining_constraint_labels field must match what the prose says, "
        f"got {seen_remaining['constraint_b']!r}"
    )
    assert seen_remaining["constraint_c"] == [], (
        "the last constraint's structured remaining_constraint_labels must be empty"
    )
    print("PASS: sub-contract goal_text explicitly names not-yet-in-scope constraints, "
          "distinct from the already-satisfied list, and the same fact is set structurally")


def test_run_decomposed_task_seeds_constraint_order_and_original_goal_once():
    """Phase 25A: _run_decomposed_task() must set contract.constraint_order
    (the authoritative order) and contract.original_goal (the real,
    un-narrowed source text) exactly once, up front -- and every
    sub-contract downstream must carry both forward unchanged via
    model_copy(), never re-derive or lose them.
    """
    contract = _make_decomposable_contract()
    labels = ["constraint_a", "constraint_b"]
    seen_orders = []
    seen_original_goals = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_orders.append(sub_contract.constraint_order)
        seen_original_goals.append(sub_contract.original_goal)
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                await _run_decomposed_task(
                    contract, labels, "module_lock", client=None, classifier_model="x",
                    correction_result={}, module_for_repeat_check=None, memory_block="",
                    constitution_text="", sensitive_paths_raw=[],
                )

    asyncio.run(run())

    assert seen_orders == [labels, labels], (
        f"every sub-contract must carry the SAME constraint_order, set once: {seen_orders}"
    )
    assert seen_original_goals == [contract.goal, contract.goal], (
        f"every sub-contract must carry the original, un-narrowed goal, never the narrowed one: "
        f"{seen_original_goals}"
    )
    print("PASS: _run_decomposed_task() seeds constraint_order/original_goal once and every "
          "sub-contract carries both forward unchanged")


def test_reconstruct_resume_order_ignores_scrambled_dict_key_order():
    """Real, severe bug found live (2026-07-12), on the SAME task, right
    after deploying the resume-decomposition fix (§ fix 8): the resume
    path used `list(constraint_status.keys())` as the canonical order
    of remaining constraints. But constraint_status is read back from a
    Postgres `jsonb` checkpoint column, and jsonb does NOT preserve
    object key insertion order. Confirmed live: a checkpoint written
    with keys in true decomposition order came back from Postgres
    completely scrambled.

    Phase 25A: order no longer comes from constraint_status (a dict) at
    all -- it comes from contract.constraint_order (an explicit list).
    This test proves a scrambled constraint_status dict has ZERO effect
    on the reconstructed order, using the exact scrambled dict from the
    live incident, now paired with a real constraint_order field.
    """
    scrambled_constraint_status = {
        "issue_attachments": "pending",
        "issue_title_field": "pending",
        "issue_product_link": "pending",
        "issue_status_field": "pending",
        "issue_priority_field": "pending",
        "issue_description_field": "pending",
        "project_multiple_issues": "satisfied",
        "service_issue_project_link": "satisfied",
    }
    true_order = [
        "service_issue_project_link", "project_multiple_issues", "issue_title_field",
        "issue_status_field", "issue_priority_field", "issue_description_field",
        "issue_attachments", "issue_product_link",
    ]
    contract = _make_decomposable_contract().model_copy(update={
        "constraint_status": scrambled_constraint_status,
        "constraint_order": true_order,
        "current_constraint_label": "project_multiple_issues",
    })

    reconstructed = _reconstruct_resume_order(contract)

    assert reconstructed is not None
    full_order, satisfied, start_index = reconstructed
    assert full_order == true_order, (
        f"order must come from contract.constraint_order, immune to the scrambled dict: {full_order}"
    )
    assert start_index == 2, "must resume at issue_title_field (index 2), NOT re-run an already-satisfied constraint"
    assert satisfied["service_issue_project_link"] == "satisfied"
    assert satisfied["project_multiple_issues"] == "satisfied"
    assert satisfied["issue_title_field"] == "pending"
    print("PASS: _reconstruct_resume_order() derives order from contract.constraint_order alone, "
          "immune to jsonb dict-key reordering that broke this live")


def test_reconstruct_resume_order_refuses_to_finalize_without_a_safe_order():
    """The OLD bug's actual failure mode: with no reliable order signal
    and other constraints still unaccounted for, the old code would
    treat a scrambled dict key as the final constraint and wrongly call
    clear_task_state(), marking an 8-constraint task "completed" after
    only 3 had run.

    Phase 25A equivalent: a checkpoint written BEFORE this field existed
    (constraint_order empty -- a pre-Phase-25A checkpoint) must refuse
    to finalize, exactly like the old missing-marker case did, rather
    than guess from anything else.
    """
    contract = _make_decomposable_contract().model_copy(update={
        "constraint_status": {
            "service_issue_project_link": "satisfied",
            "issue_title_field": "satisfied",
            "issue_status_field": "pending",
        },
        "constraint_order": [],  # pre-Phase-25A checkpoint: never populated
        "current_constraint_label": "issue_title_field",
    })

    reconstructed = _reconstruct_resume_order(contract)

    assert reconstructed is None, (
        "must refuse to finalize when constraint_order is empty (an old, pre-fix checkpoint) -- "
        "guessing here is exactly what caused the live false-completion bug"
    )
    print("PASS: _reconstruct_resume_order() refuses to guess and finalize when constraint_order "
          "was never populated (an old checkpoint predating this field)")


def test_reconstruct_resume_order_refuses_when_current_focus_is_unknown():
    """A second real refusal case: current_constraint_label doesn't
    appear anywhere in constraint_order at all (a corrupted or
    mismatched checkpoint) -- must also refuse rather than guess.
    """
    contract = _make_decomposable_contract().model_copy(update={
        "constraint_order": ["a", "b", "c"],
        "current_constraint_label": "not_a_real_label",
    })

    reconstructed = _reconstruct_resume_order(contract)

    assert reconstructed is None
    print("PASS: _reconstruct_resume_order() refuses when current_constraint_label isn't a "
          "member of constraint_order at all")


def test_reconstruct_resume_order_finalizes_on_genuine_last_constraint():
    """The one case where finalizing IS correct: current_focus is the
    LAST label in constraint_order -- this really is the last
    constraint, and start_index must equal len(full_order) so the
    caller finalizes.
    """
    contract = _make_decomposable_contract().model_copy(update={
        "constraint_order": ["only_constraint"],
        "current_constraint_label": "only_constraint",
    })

    reconstructed = _reconstruct_resume_order(contract)

    assert reconstructed is not None
    full_order, satisfied, start_index = reconstructed
    assert full_order == ["only_constraint"]
    assert start_index == 1 == len(full_order), "start_index must equal len(full_order) so the caller finalizes"
    assert satisfied == {"only_constraint": "satisfied"}
    print("PASS: _reconstruct_resume_order() correctly finalizes when there's genuinely "
          "only one constraint and nothing else to account for")


def test_reconstruct_resume_order_never_marks_a_label_satisfied_by_position_alone():
    """Real, live-confirmed bug (2026-08-08, task 657697fc-f701-4932-a172-b0132da93cfa):
    `constraint_order` is only the raw, arbitrary order the decomposition happened to enumerate
    labels in -- NEVER a topological/dependency order. The OLD `_reconstruct_resume_order()`
    treated "earlier position than current_focus" as "satisfied," which silently marked labels
    satisfied that were never actually built -- confirmed live: `service_ticket_model` (real
    predecessor of `ticket_workflow`) was sorted BEFORE `equipment_registry` in
    `constraint_order`, so once `equipment_registry` (positioned later) passed, every
    earlier-positioned label -- including `service_ticket_model`, never even dispatched --
    was marked "satisfied," letting `ticket_workflow` start with its own real predecessor
    still `pending`.

    This test reproduces that exact scenario (same label names, same scrambled order) with
    `read_node_states()` mocked to return the REAL ground truth (only `equipment_registry`
    genuinely satisfied) -- asserts the fix: a label must never be marked "satisfied" just
    because of its list position, only because real per-label state says so.
    """
    constraint_order = [
        "service_ticket_model", "ticket_views", "ticket_technician_access", "ticket_manager_access",
        "project_smart_buttons", "escalation_cron", "equipment_registry", "maintenance_history",
        "ticket_workflow", "ticket_chatter_logging",
    ]
    contract = _make_decomposable_contract().model_copy(update={
        "constraint_order": constraint_order,
        "current_constraint_label": "equipment_registry",  # just passed, position 6
    })
    # Real ground truth: ONLY equipment_registry actually ran and passed. Every other label,
    # including the five sorted BEFORE it in constraint_order, was never dispatched at all.
    real_live_states = {"equipment_registry": "satisfied"}

    with patch("manager.loop.read_node_states", return_value=real_live_states):
        reconstructed = _reconstruct_resume_order(contract)

    assert reconstructed is not None
    full_order, satisfied, start_index = reconstructed
    assert satisfied["equipment_registry"] == "satisfied", "the label that genuinely passed must be satisfied"
    for never_run_label in (
        "service_ticket_model", "ticket_views", "ticket_technician_access",
        "ticket_manager_access", "project_smart_buttons", "escalation_cron",
    ):
        assert satisfied[never_run_label] == "pending", (
            f"{never_run_label!r} was NEVER actually dispatched and must stay pending, "
            f"regardless of its position ({constraint_order.index(never_run_label)}) relative "
            f"to equipment_registry's own position ({constraint_order.index('equipment_registry')}) "
            f"in constraint_order -- this is the exact live-confirmed corruption being fixed"
        )
    print("PASS: _reconstruct_resume_order() derives 'satisfied' from real per-label state "
          "(read_node_states), never from raw constraint_order list position")


def test_reconstruct_resume_order_start_index_never_finalizes_while_real_pending_labels_remain():
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run, 9-constraint
    field-service task): `start_index` used to be computed purely as
    `full_order.index(current_focus) + 1` -- current_focus's raw POSITION in `constraint_order`
    -- even though `satisfied` (right above it in this same function) had already been fixed
    (2026-08-08) to come from real per-label ground truth via `read_node_states()`. Confirmed
    live: `constraint_order` happened to enumerate `project_ticket_counts` LAST (position 8 of
    9), even though THREE other labels (`daily_escalation_cron`, `ticket_workflow_and_logging`,
    `ticket_bulk_close`) were still genuinely `pending` per real live state. Every resume
    re-verified `project_ticket_counts` (the one and only checkpointed sub-contract), each pass
    computed `start_index = 9 = len(full_order)`, and the caller
    (`resume_task_after_checkpoint`) treated that as "genuinely the last constraint" and called
    `clear_task_state()` -- finalizing a 9-constraint task as complete after only 6 constraints
    had ever been dispatched, with the remaining 3 never even attempted.

    This test reproduces that exact shape: `current_focus` sits LAST in `constraint_order`, but
    real live state shows 3 earlier-enumerated labels still pending. Asserts the fix: the caller
    must NOT be told to finalize (`start_index < len(full_order)`), and the returned
    `start_index` must point at the first genuinely pending label, not at `len(full_order)`.
    """
    constraint_order = [
        "service_ticket_model", "ticket_list_view", "ticket_access_rights",
        "daily_escalation_cron", "equipment_registry", "maintenance_history",
        "ticket_workflow_and_logging", "ticket_bulk_close", "project_ticket_counts",
    ]
    contract = _make_decomposable_contract().model_copy(update={
        "constraint_order": constraint_order,
        "current_constraint_label": "project_ticket_counts",  # last position, but NOT the last real gap
    })
    real_live_states = {
        "service_ticket_model": "satisfied", "ticket_list_view": "satisfied",
        "ticket_access_rights": "satisfied", "equipment_registry": "satisfied",
        "maintenance_history": "satisfied", "project_ticket_counts": "satisfied",
        "daily_escalation_cron": "pending", "ticket_workflow_and_logging": "pending",
        "ticket_bulk_close": "pending",
    }

    with patch("manager.loop.read_node_states", return_value=real_live_states):
        reconstructed = _reconstruct_resume_order(contract)

    assert reconstructed is not None
    full_order, satisfied, start_index = reconstructed
    assert start_index < len(full_order), (
        f"3 real constraints (daily_escalation_cron, ticket_workflow_and_logging, "
        f"ticket_bulk_close) are still pending -- start_index ({start_index}) must NOT equal "
        f"len(full_order) ({len(full_order)}), or the caller wrongly finalizes the whole task"
    )
    assert full_order[start_index] == "daily_escalation_cron", (
        f"start_index must point at the first genuinely pending label in constraint_order "
        f"order, not just past current_focus's own position -- got {full_order[start_index]!r}"
    )
    print("PASS: _reconstruct_resume_order() never finalizes the task while real constraints "
          "remain pending, even when current_focus happens to sit last in constraint_order")


def test_reconstruct_resume_order_falls_back_to_position_when_no_live_state_exists():
    """Conservative-degradation counterpart to the test above: when `read_node_states()`
    genuinely has nothing (a task predating graph_created telemetry, or any other reason live
    state can't be read), the fix must not become MORE dangerous than the pre-fix behavior --
    it degrades to the exact same position-based approximation as before, never to "nothing is
    satisfied" (which would silently re-run already-passed work) or a crash.
    """
    contract = _make_decomposable_contract().model_copy(update={
        "constraint_order": ["a", "b", "c"],
        "current_constraint_label": "b",
    })

    with patch("manager.loop.read_node_states", return_value={}):
        reconstructed = _reconstruct_resume_order(contract)

    assert reconstructed is not None
    full_order, satisfied, start_index = reconstructed
    assert satisfied == {"a": "satisfied", "b": "satisfied", "c": "pending"}, (
        "with no live state available at all, must fall back to the same conservative "
        "position-based approximation the pre-fix code always used -- never worse"
    )
    print("PASS: _reconstruct_resume_order() degrades safely to position-based satisfied when "
          "read_node_states() returns nothing usable")


if __name__ == "__main__":
    test_run_constraint_labels_from_continues_past_a_resumed_index()
    test_run_constraint_labels_from_stops_on_first_failure_without_finalizing()
    test_run_constraint_labels_from_names_not_yet_in_scope_constraints()
    test_run_decomposed_task_seeds_constraint_order_and_original_goal_once()
    test_reconstruct_resume_order_ignores_scrambled_dict_key_order()
    test_reconstruct_resume_order_refuses_to_finalize_without_a_safe_order()
    test_reconstruct_resume_order_refuses_when_current_focus_is_unknown()
    test_reconstruct_resume_order_finalizes_on_genuine_last_constraint()
    test_reconstruct_resume_order_never_marks_a_label_satisfied_by_position_alone()
    test_reconstruct_resume_order_start_index_never_finalizes_while_real_pending_labels_remain()
    test_reconstruct_resume_order_falls_back_to_position_when_no_live_state_exists()
    print("\nALL RESUME-DECOMPOSITION TESTS PASSED")
