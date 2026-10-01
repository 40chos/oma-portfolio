"""Real, confirmed bug found live (2026-08-10, task e65381cc, a small follow-up correction task
extending the flagship task's own already-installed module, equipment_views_menu node, recurring
identically across 2 straight rounds): `_run_constraint_labels_from()`'s own "NOT yet in scope"
sentence only ever named the not-yet-in-scope constraints by their bare LABEL SLUG (e.g.
'ticket_views_menu') -- never the real Odoo model each one actually concerns
('oma.service.ticket'). Build kept writing a full tree/form/menu for that other model anyway,
since a `depends_on_module:` extension task's views-only round legitimately has an empty
models_py (the real fields already exist for real, owned by the dependency module), so the
existing "no view elements referencing a field not defined by THIS round's models_py" sentence
never actually forbade it. See manager/loop.py's own `_run_constraint_labels_from()` for the fix:
the real model names are already known via `ConstraintNode.requires` (populated at decomposition
time from real artifact-overlap analysis) and are now named explicitly.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, ConstraintNode, SpecialistType, TaskContract
from manager.loop import _run_constraint_labels_from


def _make_decomposable_contract(constraint_nodes) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_2_notify_after,
        goal="Extend the module with equipment views and ticket views.", inputs=[], rules=[],
        deliverables=[], compensating_actions=[], validation_by="testing_qa", pause_if=[],
        turn_budget=10, constraint_status={}, constraint_nodes=constraint_nodes,
    )


def test_not_yet_in_scope_sentence_names_the_real_models_from_requires():
    nodes = {
        "equipment_views_menu": ConstraintNode(
            label="equipment_views_menu", predecessor_labels=[],
            requires=["oma.equipment"], creates=["oma.equipment.tree", "oma.equipment.form"],
        ),
        "ticket_views_menu": ConstraintNode(
            label="ticket_views_menu", predecessor_labels=[],
            requires=["oma.service.ticket"], creates=["oma.service.ticket.tree"],
        ),
    }
    contract = _make_decomposable_contract(nodes)
    labels = ["equipment_views_menu", "ticket_views_menu"]
    satisfied = {label: "pending" for label in labels}
    seen_goals = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_goals.append((sub_contract.current_constraint_label, sub_contract.goal))
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    assert result["passed"] is True
    equipment_goal = next(g for label, g in seen_goals if label == "equipment_views_menu")
    assert "oma.service.ticket" in equipment_goal, (
        f"expected the real not-yet-in-scope model name to be named explicitly, got: {equipment_goal!r}"
    )
    assert "ticket_views_menu" in equipment_goal
    print("PASS: the not-yet-in-scope sentence names the real model (not just the label slug) "
          "so a depends_on_module round with an empty models.py can't mistake a pre-existing "
          "field on a DIFFERENT model as fair game")


def test_no_requires_data_is_a_graceful_no_op_never_guessed():
    nodes = {
        "a": ConstraintNode(label="a", predecessor_labels=[]),
        "b": ConstraintNode(label="b", predecessor_labels=[]),
    }
    contract = _make_decomposable_contract(nodes)
    labels = ["a", "b"]
    satisfied = {label: "pending" for label in labels}
    seen_goals = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_goals.append((sub_contract.current_constraint_label, sub_contract.goal))
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    assert result["passed"] is True
    a_goal = next(g for label, g in seen_goals if label == "a")
    assert "Concretely, this means" not in a_goal, (
        "with no real requires data at all, nothing should be guessed or invented"
    )
    print("PASS: no requires data anywhere is a graceful no-op, never guessed")


def test_a_model_shared_with_this_rounds_own_focus_is_never_forbidden():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node,
    immediately after the sibling fix above): a model can legitimately be `requires`-d by
    MULTIPLE constraints for entirely different reasons -- `ticket_access_restriction` requires
    `oma.service.ticket` because it edits that model's own ir.model.access rows, while THIS
    round's own focus (`ticket_views_menu`) ALSO legitimately needs `oma.service.ticket` to build
    its own real, in-scope views. The original fix's own unconditional union blindly forbade "any
    view/form/tree/menu/action/field for oma.service.ticket" -- directly contradicting this SAME
    round's own correct, in-scope work on that exact model, confirmed live via Code-Review
    correctly refusing the round's own genuinely-required views because the goal text itself said
    not to touch them.
    """
    nodes = {
        "ticket_views_menu": ConstraintNode(
            label="ticket_views_menu", predecessor_labels=[],
            requires=["oma.service.ticket"], creates=["oma.service.ticket.tree"],
        ),
        "ticket_access_restriction": ConstraintNode(
            label="ticket_access_restriction", predecessor_labels=[],
            requires=["ir.model.access.csv", "base.group_user", "oma.service.ticket"],
        ),
    }
    contract = _make_decomposable_contract(nodes)
    labels = ["ticket_views_menu", "ticket_access_restriction"]
    satisfied = {label: "pending" for label in labels}
    seen_goals = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_goals.append((sub_contract.current_constraint_label, sub_contract.goal))
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    assert result["passed"] is True
    views_goal = next(g for label, g in seen_goals if label == "ticket_views_menu")
    if "Concretely, this means" in views_goal:
        forbidden_section = views_goal.split("Concretely, this means", 1)[1]
        assert "oma.service.ticket" not in forbidden_section, (
            f"oma.service.ticket must never be forbidden in ticket_views_menu's own round, "
            f"since that round legitimately needs it too -- got: {forbidden_section!r}"
        )
    print("PASS: a model shared between this round's own focus and a not-yet-in-scope "
          "constraint is never forbidden, closing the real live self-contradiction found on "
          "task e65381cc's ticket_views_menu node")


def test_a_non_model_artifact_sharing_a_prefix_with_an_owned_model_is_never_forbidden():
    """Real, confirmed follow-up bug found live (2026-08-10, same task, same night, immediately
    after the exact-match sibling fix above): `ticket_status_decoration`'s own real requires
    list includes 'oma.service.ticket.status' -- an internal decomposition-time artifact
    reference, not a real Odoo model -- which shares a dotted PREFIX with 'oma.service.ticket',
    the real model `ticket_views_menu` already legitimately owns. Naming it verbatim let
    Code-Review reasonably (if technically incorrectly) read it as implicating the same real,
    in-scope model, reproducing the exact self-contradiction the exact-match fix already closed.
    """
    nodes = {
        "ticket_views_menu": ConstraintNode(
            label="ticket_views_menu", predecessor_labels=[],
            requires=["oma.service.ticket"], creates=["oma.service.ticket.tree"],
        ),
        "ticket_status_decoration": ConstraintNode(
            label="ticket_status_decoration", predecessor_labels=[],
            requires=["oma.service.ticket.tree", "oma.service.ticket.status"],
        ),
    }
    contract = _make_decomposable_contract(nodes)
    labels = ["ticket_views_menu", "ticket_status_decoration"]
    satisfied = {label: "pending" for label in labels}
    seen_goals = []

    async def fake_execute_contract(sub_contract, *args, **kwargs):
        seen_goals.append((sub_contract.current_constraint_label, sub_contract.goal))
        return {"status": "completed", "passed": True, "rounds_taken": 1, "task_id": str(sub_contract.task_id)}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            with patch("manager.loop.clear_task_state"):
                return await _run_constraint_labels_from(
                    contract, labels, satisfied, 0,
                    "module_lock", client=None, classifier_model="x", correction_result={},
                    module_for_repeat_check=None, memory_block="", constitution_text="",
                    sensitive_paths_raw=[],
                )

    result = asyncio.run(run())
    assert result["passed"] is True
    views_goal = next(g for label, g in seen_goals if label == "ticket_views_menu")
    if "Concretely, this means" in views_goal:
        forbidden_section = views_goal.split("Concretely, this means", 1)[1]
        assert "oma.service.ticket" not in forbidden_section, (
            f"neither the bare model nor its dotted-prefix artifact may be forbidden in "
            f"ticket_views_menu's own round -- got: {forbidden_section!r}"
        )
    print("PASS: a non-model artifact sharing a dotted prefix with a model this round already "
          "owns is never forbidden either, closing the real live follow-up bug")


if __name__ == "__main__":
    test_not_yet_in_scope_sentence_names_the_real_models_from_requires()
    test_no_requires_data_is_a_graceful_no_op_never_guessed()
    test_a_model_shared_with_this_rounds_own_focus_is_never_forbidden()
    test_a_non_model_artifact_sharing_a_prefix_with_an_owned_model_is_never_forbidden()
    print("\nALL NOT-YET-IN-SCOPE-NAMES-REAL-MODELS TESTS PASSED")
