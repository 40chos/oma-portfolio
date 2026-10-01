"""P12 Tier S item 4 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for resume_orphaned_task_at_startup()'s newly-ported continuation fix -- the same
2026-07-12 decomposition-resume bug resume_task_after_checkpoint() was already fixed for
(see that function's own comments in manager/loop.py), ported here because
resume_orphaned_task_at_startup() had the identical unconditional
`return await _execute_contract(...)` with zero decomposition awareness: a genuine pass on
the one sub-contract an orphan-resume just re-ran had nowhere to hand off to, so every
subsequent resume would re-verify the same already-satisfied constraint forever, and Redis
task state would never get cleared even on genuine full completion.

Mocks _execute_contract (no live model-gateway calls), get_latest_resume_point,
get_original_capability_class, append_project_memory, publish_trace_event, and
read_project_memory -- zero LLM/GPU calls anywhere in this file. Does NOT mock
_reconstruct_resume_order or _run_constraint_labels_from: both are real, pure, already-proven
logic (see tests/test_resume_decomposition.py), so exercising them for real here is what
actually proves the new wiring in resume_orphaned_task_at_startup() is correct, not just that
mocks were called.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import _terminal_node_state_for_result, resume_orphaned_task_at_startup


def test_terminal_node_state_for_result_maps_every_real_result_shape():
    assert _terminal_node_state_for_result({"status": "paused"}) == "paused"
    assert _terminal_node_state_for_result({"status": "completed", "passed": True}) == "satisfied"
    assert _terminal_node_state_for_result({"status": "completed", "passed": False}) == "failing"
    assert _terminal_node_state_for_result({"status": "error"}) is None
    assert _terminal_node_state_for_result({"status": "cancelled"}) is None
    assert _terminal_node_state_for_result({"status": "rejected"}) is None
    print("PASS: _terminal_node_state_for_result() maps every real result shape to the correct node state, or None for a non-terminal one")


def _make_resume_point(constraint_order, current_constraint_label, constraint_status, commit_sha=None):
    task_id = uuid.uuid4()
    contract = TaskContract(
        task_id=task_id, specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A; add field B; add field C.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        constraint_order=constraint_order, current_constraint_label=current_constraint_label,
        constraint_status=constraint_status, original_goal="Add field A; add field B; add field C.",
        # Deliberately stale -- a real bug found live (2026-08-08, task
        # 07141af5-9a4e-41b6-93ea-8b7af04fea9c's flagship run) carried THIS exact stale value
        # forward unchanged across resumes; see the dedicated regression test below.
        frozen_old_files_by_relpath={"models/models.py": "STALE CONTENT FROM ORIGINAL DISPATCH"},
    )
    return str(task_id), {
        "contract": contract.model_dump(mode="json"),
        "commit_sha": commit_sha,
        "module_for_repeat_check": None,
        "human_summary": "Prior attempt summary.",
        "prior_rounds": [],
        "source": "replan_round",
    }


def _patched(resume_point, fake_execute_contract):
    return (
        patch("manager.loop.get_latest_resume_point", return_value=resume_point),
        patch("manager.loop.get_original_capability_class", return_value=None),
        patch("manager.loop.append_project_memory"),
        patch("manager.loop.publish_trace_event"),
        patch("manager.loop.read_project_memory", return_value=[]),
        patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)),
        patch("manager.loop.clear_task_state"),
    )


def test_genuine_pass_with_remaining_constraints_continues_via_run_constraint_labels_from():
    """The actual fix: a genuine pass on the ONE sub-contract this orphan-resume just re-ran,
    with more constraints still pending, must hand off to the remaining ones in order -- never
    just return that single sub-contract's result as if the whole task were done.
    """
    task_id, resume_point = _make_resume_point(
        constraint_order=["constraint_a", "constraint_b", "constraint_c"],
        current_constraint_label="constraint_a",
        constraint_status={"constraint_a": "pending", "constraint_b": "pending", "constraint_c": "pending"},
    )
    seen_focuses = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_focuses.append(sub_contract.current_constraint_label)
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    mocks = _patched(resume_point, fake_execute_contract)

    async def run():
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6] as mock_clear:
            result = await resume_orphaned_task_at_startup(task_id, client=None, classifier_model="x")
            return result, mock_clear

    result, mock_clear = asyncio.run(run())

    assert seen_focuses == ["constraint_a", "constraint_b", "constraint_c"], (
        f"expected the orphan-resumed constraint_a followed by the two remaining constraints "
        f"in order, got {seen_focuses}"
    )
    assert result["passed"] is True
    assert result["decomposition"]["completed_sub_contracts"] == 3
    mock_clear.assert_called_once()
    print("PASS: a genuine pass with remaining constraints continues via _run_constraint_labels_from()")


def test_genuine_pass_on_last_constraint_clears_task_state():
    """A genuine pass that WAS the last remaining constraint must clear Redis task state --
    the other real half of this fix, without which a fully-completed decomposed task resumed
    from an orphan-restart would never get marked done.
    """
    task_id, resume_point = _make_resume_point(
        constraint_order=["constraint_a", "constraint_b"],
        current_constraint_label="constraint_b",
        constraint_status={"constraint_a": "satisfied", "constraint_b": "pending"},
    )

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    mocks = _patched(resume_point, fake_execute_contract)

    async def run():
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6] as mock_clear:
            result = await resume_orphaned_task_at_startup(task_id, client=None, classifier_model="x")
            return result, mock_clear

    result, mock_clear = asyncio.run(run())

    assert result["passed"] is True
    mock_clear.assert_called_once()
    print("PASS: a genuine pass on the last remaining constraint calls clear_task_state()")


def test_resume_refreshes_frozen_snapshot_instead_of_carrying_the_stale_one_forward():
    """Real, confirmed bug found live (2026-08-08, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node): this function used to carry `last_contract.
    frozen_old_files_by_relpath` forward completely unchanged into the resumed sub-contract --
    the per-node snapshot frozen at the constraint's ORIGINAL dispatch time. When a round fails
    with apply_node_result_to_module()'s own "genuinely diverged since this node's own
    dispatch-time snapshot" conflict, its own error message explicitly promises "Retrying will
    take a FRESH snapshot" -- a promise this resume path (and its sibling
    resume_task_after_checkpoint(), fixed the same way) never actually kept, so the identical
    conflict recurred across every subsequent resume, confirmed live across 3 consecutive
    escalations on the same real task. This test proves the sub-contract handed to
    _execute_contract() now carries the FRESHLY re-read snapshot, never the stale one baked
    into the checkpointed contract.
    """
    task_id, resume_point = _make_resume_point(
        constraint_order=["constraint_a"], current_constraint_label="constraint_a",
        constraint_status={"constraint_a": "pending"}, commit_sha="deadbeef",
    )
    fresh_snapshot = {"models/models.py": "# FRESH CONTENT FROM RE-READ AT RESUME TIME\n"}
    seen_snapshots = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_snapshots.append(sub_contract.frozen_old_files_by_relpath)
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    mocks = _patched(resume_point, fake_execute_contract)

    async def run():
        with (
            patch("tools_odoo.module_dev.vcs.read_last_validated_commit", return_value=fresh_snapshot),
            mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6],
        ):
            return await resume_orphaned_task_at_startup(task_id, client=None, classifier_model="x")

    asyncio.run(run())

    assert seen_snapshots == [fresh_snapshot], (
        f"expected the resumed sub-contract to carry the freshly re-read snapshot, not the "
        f"stale one baked into the checkpointed contract -- got {seen_snapshots!r}"
    )
    print("PASS: resume_orphaned_task_at_startup() refreshes frozen_old_files_by_relpath rather "
          "than carrying the stale, original-dispatch-time snapshot forward, closing the real "
          "live gap found on task 07141af5's service_ticket_model node")


def test_reconstruct_resume_order_returning_none_does_not_finalize():
    """When _reconstruct_resume_order() can't safely determine order/completion (e.g. an old,
    pre-Phase-25A checkpoint with empty constraint_order), the fix must NOT call
    clear_task_state() or otherwise finalize -- same conservative posture as the sibling
    function resume_task_after_checkpoint() already has.
    """
    task_id, resume_point = _make_resume_point(
        constraint_order=[], current_constraint_label=None, constraint_status={"constraint_a": "pending"},
    )

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    mocks = _patched(resume_point, fake_execute_contract)

    async def run():
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6] as mock_clear:
            result = await resume_orphaned_task_at_startup(task_id, client=None, classifier_model="x")
            return result, mock_clear

    result, mock_clear = asyncio.run(run())

    assert result["passed"] is True
    mock_clear.assert_not_called()
    print("PASS: _reconstruct_resume_order() returning None does not finalize/clear task state")


def test_non_decomposed_task_returns_result_unchanged():
    """A plain (non-decomposed) orphan-resumed task -- constraint_status empty -- must skip
    this whole new branch entirely and return _execute_contract()'s result exactly as before,
    unchanged.
    """
    task_id = uuid.uuid4()
    contract = TaskContract(
        task_id=task_id, specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Add field A.", inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10, constraint_status={},
    )
    resume_point = {
        "contract": contract.model_dump(mode="json"), "commit_sha": None,
        "module_for_repeat_check": None, "human_summary": "Prior attempt summary.",
        "prior_rounds": [], "source": "replan_round",
    }

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "task_id": str(sub_contract.task_id)}

    mocks = _patched(resume_point, fake_execute_contract)

    async def run():
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6] as mock_clear:
            result = await resume_orphaned_task_at_startup(str(task_id), client=None, classifier_model="x")
            return result, mock_clear

    result, mock_clear = asyncio.run(run())

    assert result["passed"] is True
    mock_clear.assert_not_called()
    print("PASS: a non-decomposed task's result passes through unchanged, new branch is skipped entirely")


def test_orphan_resume_publishes_running_then_terminal_node_state():
    """Phase B UI fix (2026-08-09), the project owner's own direct live report on task
    07141af5-9a4e-41b6-93ea-8b7af04fea9c: after a resume, the graph "showed absolutely nothing" --
    no node ever flipped to 'running', because this direct `_execute_contract()` call (unlike
    every constraint dispatched through `run_graph_scheduler()`) never published a node_state_changed
    event at all. Confirms the real fix: 'running' published before the call, and the correct
    terminal state (here: 'paused', a genuine ask_operator escalation on the re-run constraint)
    published right after -- using the SAME constraint label this resume actually re-ran, never
    a guess or the wrong sibling.
    """
    task_id, resume_point = _make_resume_point(
        constraint_order=["constraint_a", "constraint_b"],
        current_constraint_label="constraint_a",
        constraint_status={"constraint_a": "pending", "constraint_b": "pending"},
    )

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        return {"status": "paused", "reason": "ask_operator", "task_id": str(sub_contract.task_id)}

    mocks = _patched(resume_point, fake_execute_contract)

    async def run():
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6], \
             patch("manager.loop._publish_node_state_changed") as mock_publish_state:
            result = await resume_orphaned_task_at_startup(task_id, client=None, classifier_model="x")
            return result, mock_publish_state

    result, mock_publish_state = asyncio.run(run())

    calls = [c.args for c in mock_publish_state.call_args_list]
    assert calls == [(task_id, "constraint_a", "running"), (task_id, "constraint_a", "paused")], (
        f"expected 'running' published before the direct _execute_contract() call and 'paused' "
        f"(the real result status) published right after, got {calls}"
    )
    assert result["status"] == "paused"
    print("PASS: orphan-resume's direct _execute_contract() call publishes node_state_changed 'running' then the real terminal state")


def test_orphan_resume_publishes_no_terminal_state_for_a_non_terminal_result():
    """A result shape that isn't a real terminal outcome (e.g. 'error'/'cancelled') must not get
    a fabricated terminal node state published -- only the initial 'running' publish fires.
    """
    task_id, resume_point = _make_resume_point(
        constraint_order=["constraint_a"],
        current_constraint_label="constraint_a",
        constraint_status={"constraint_a": "pending"},
    )

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        return {"status": "error", "message": "gateway exploded", "task_id": str(sub_contract.task_id)}

    mocks = _patched(resume_point, fake_execute_contract)

    async def run():
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6], \
             patch("manager.loop._publish_node_state_changed") as mock_publish_state:
            result = await resume_orphaned_task_at_startup(task_id, client=None, classifier_model="x")
            return result, mock_publish_state

    result, mock_publish_state = asyncio.run(run())

    calls = [c.args for c in mock_publish_state.call_args_list]
    assert calls == [(task_id, "constraint_a", "running")], (
        f"expected only the initial 'running' publish for a non-terminal result shape, got {calls}"
    )
    print("PASS: a non-terminal result (e.g. 'error') never gets a fabricated terminal node-state publish")


if __name__ == "__main__":
    test_terminal_node_state_for_result_maps_every_real_result_shape()
    test_genuine_pass_with_remaining_constraints_continues_via_run_constraint_labels_from()
    test_genuine_pass_on_last_constraint_clears_task_state()
    test_resume_refreshes_frozen_snapshot_instead_of_carrying_the_stale_one_forward()
    test_reconstruct_resume_order_returning_none_does_not_finalize()
    test_non_decomposed_task_returns_result_unchanged()
    test_orphan_resume_publishes_running_then_terminal_node_state()
    test_orphan_resume_publishes_no_terminal_state_for_a_non_terminal_result()
    print("\nALL RESUME-ORPHANED-TASK CONTINUATION TESTS PASSED")
