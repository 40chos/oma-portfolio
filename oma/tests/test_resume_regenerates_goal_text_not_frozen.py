"""Real, confirmed bug found live (2026-08-10, task e65381cc, a small follow-up correction task
extending the flagship task's own already-installed module, equipment_views_menu node, recurring
identically across 3 straight resumes with BYTE-IDENTICAL LLM output despite each resume's own
note being more explicit than the last): `_resume_task_after_checkpoint_locked()`'s own
`resume_update` never included `goal` -- every resume of an already-dispatched, still-paused
decomposed-task node kept resending the EXACT SAME frozen goal_text from that node's original
dispatch through `_run_constraint_labels_from()`'s own `execute_node()`, forever, regardless of
how many rounds passed or how explicit a resume `note` became. See manager/loop.py's
`_compose_focus_goal_text()` (now shared between a first dispatch and every resume) for the fix.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, ConstraintNode, SpecialistType, TaskContract
from manager.loop import _resume_task_after_checkpoint_locked


def _make_paused_contract() -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="STALE frozen goal text from this node's original dispatch, months ago.",
        original_goal="The real, original, never-narrowed multi-item goal text.",
        inputs=[], rules=[], deliverables=[], compensating_actions=[], validation_by="testing_qa",
        pause_if=[], turn_budget=10,
        constraint_status={"equipment_views_menu": "pending", "ticket_views_menu": "pending"},
        constraint_order=["equipment_views_menu", "ticket_views_menu"],
        current_constraint_label="equipment_views_menu",
        constraint_nodes={
            "equipment_views_menu": ConstraintNode(label="equipment_views_menu", requires=["oma.equipment"]),
            "ticket_views_menu": ConstraintNode(label="ticket_views_menu", requires=["oma.service.ticket"]),
        },
    )


def test_resume_regenerates_goal_text_fresh_not_the_stale_frozen_one():
    contract = _make_paused_contract()
    checkpoint = {
        "id": 1,
        "detail": {
            "human_summary": "summary", "last_contract": contract.model_dump(mode="json"),
            "prior_rounds": [],
        },
    }
    with patch("manager.loop.get_latest_checkpoint", return_value=checkpoint), \
         patch("manager.loop.list_pending_escalations", return_value=[]), \
         patch("manager.loop.list_clarifications_since", return_value=[]), \
         patch("manager.loop.estimate_tokens", return_value=0), \
         patch("manager.loop.MODEL_CONTEXT_WINDOWS", {}), \
         patch("manager.loop.get_original_capability_class", return_value=None), \
         patch("tools_odoo.module_dev.vcs.read_last_validated_commit", return_value=None), \
         patch("manager.loop.append_project_memory"), \
         patch("manager.loop.publish_trace_event"), \
         patch("manager.loop._publish_node_state_changed"), \
         patch("manager.loop.read_project_memory", return_value=[]), \
         patch("manager.loop.load_manager_constitution", return_value=""), \
         patch("manager.loop.load_sensitive_paths", return_value=[]), \
         patch("manager.loop._execute_contract", new=AsyncMock(
             return_value={"status": "paused", "reason": "ask_operator", "message": "x"},
         )) as mock_execute:
        asyncio.run(_resume_task_after_checkpoint_locked(
            str(contract.task_id), client=None, classifier_model="x", note="please fix it",
        ))

    passed_contract = mock_execute.call_args[0][0]
    assert passed_contract.goal != contract.goal, (
        "the stale frozen goal must be re-derived on resume, not reused verbatim"
    )
    assert "STALE frozen goal text" not in passed_contract.goal
    assert "The real, original, never-narrowed multi-item goal text." in passed_contract.goal
    assert "This round's own NEW focus is ONLY: 'equipment_views_menu'" in passed_contract.goal
    assert "oma.service.ticket" in passed_contract.goal, (
        "the not-yet-in-scope real-model naming (from ConstraintNode.requires) must also be "
        "present, exactly like a fresh first dispatch already gets"
    )
    print("PASS: resuming an already-dispatched node re-derives goal_text fresh instead of "
          "reusing the stale, frozen original-dispatch text forever")


def test_non_decomposed_resume_leaves_goal_untouched():
    """A plain, non-decomposed task (no current_constraint_label at all) has nothing to
    re-derive -- its `goal` is already the whole, correct, plain-text string. Must not be
    touched or replaced with anything synthetic.
    """
    contract = TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="A plain, single-requirement goal with no decomposition at all.",
        inputs=[], rules=[], deliverables=[], compensating_actions=[], validation_by="testing_qa",
        pause_if=[], turn_budget=10,
    )
    checkpoint = {
        "id": 1,
        "detail": {
            "human_summary": "summary", "last_contract": contract.model_dump(mode="json"),
            "prior_rounds": [],
        },
    }
    with patch("manager.loop.get_latest_checkpoint", return_value=checkpoint), \
         patch("manager.loop.list_pending_escalations", return_value=[]), \
         patch("manager.loop.list_clarifications_since", return_value=[]), \
         patch("manager.loop.estimate_tokens", return_value=0), \
         patch("manager.loop.MODEL_CONTEXT_WINDOWS", {}), \
         patch("manager.loop.get_original_capability_class", return_value=None), \
         patch("tools_odoo.module_dev.vcs.read_last_validated_commit", return_value=None), \
         patch("manager.loop.append_project_memory"), \
         patch("manager.loop.publish_trace_event"), \
         patch("manager.loop._publish_node_state_changed"), \
         patch("manager.loop.read_project_memory", return_value=[]), \
         patch("manager.loop.load_manager_constitution", return_value=""), \
         patch("manager.loop.load_sensitive_paths", return_value=[]), \
         patch("manager.loop._execute_contract", new=AsyncMock(
             return_value={"status": "paused", "reason": "ask_operator", "message": "x"},
         )) as mock_execute:
        asyncio.run(_resume_task_after_checkpoint_locked(
            str(contract.task_id), client=None, classifier_model="x", note=None,
        ))

    passed_contract = mock_execute.call_args[0][0]
    assert passed_contract.goal == contract.goal, (
        "a non-decomposed task's own goal must never be touched on resume"
    )
    print("PASS: a non-decomposed task's goal is left untouched on resume")


if __name__ == "__main__":
    test_resume_regenerates_goal_text_fresh_not_the_stale_frozen_one()
    test_non_decomposed_resume_leaves_goal_untouched()
    print("\nALL RESUME-REGENERATES-GOAL-TEXT-NOT-FROZEN TESTS PASSED")
