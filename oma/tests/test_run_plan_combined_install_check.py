"""P7b item 3 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§17): tests for run_plan()'s new combined-install check on its own completion path -- the real,
final piece of P7b (verify_combined_install() itself was already built/tested,
tests/test_verify_combined_install.py; this file tests its wiring into run_plan()).

Real Postgres writes via create_task_plan()/mark_plan_item_status() (same convention as
tests/test_run_plan_aggregate_ceiling.py), _execute_contract() mocked, client=None -- zero
LLM/GPU calls. verify_combined_install() itself (real SSH/Odoo install) is also mocked -- this
file tests run_plan()'s own wiring logic, not the toolchain function's own real install behavior
(already covered elsewhere).
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import run_plan


def _item(plan_item_id: str, blocked_by=None) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal=f"Combined-install test item {plan_item_id}.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        plan_item_id=plan_item_id, blocked_by=blocked_by or [],
    )


def _fake_output(module_name: str):
    output = MagicMock()
    output.detail = {"module_name": module_name}
    return output


def test_combined_install_check_runs_when_two_plus_modules_and_db_configured(monkeypatch):
    items = [_item("a"), _item("b", blocked_by=["a"])]
    module_names = {"a": "oma_module_a", "b": "oma_module_b"}

    async def fake_execute_contract(contract, *args, **kwargs):
        pid = contract.plan_item_id
        return {"status": "completed", "passed": True, "rounds_taken": 1, "build_output": _fake_output(module_names[pid])}

    monkeypatch.setenv("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev_dup_test")
    fake_install_result = MagicMock(success=True, message="Combined install completed.")
    fake_verify = MagicMock(return_value=fake_install_result)
    monkeypatch.setattr("tools_odoo.module_dev.toolchain.verify_combined_install", fake_verify)

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            return await run_plan(
                "combined install test", items, client=None, classifier_model="x",
                plan_round_budget=99, plan_wall_clock_cap_seconds=99999,
            )

    result = asyncio.run(run())
    assert result["status"] == "completed"
    assert result["combined_install_check"]["success"] is True
    assert result["combined_install_check"]["module_names"] == ["oma_module_a", "oma_module_b"]
    fake_verify.assert_called_once_with(["oma_module_a", "oma_module_b"], "odoo16_dev_dup_test")
    print("PASS: run_plan() runs the real combined-install check across every passed item's module when 2+ modules and a target db are available")


def test_combined_install_check_skipped_when_only_one_module():
    items = [_item("a")]

    async def fake_execute_contract(contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "rounds_taken": 1, "build_output": _fake_output("oma_module_a")}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)), \
             patch.dict(os.environ, {"OMA_ODOO_DB_DUPLICATE_FOR_BUILD": "odoo16_dev_dup_test"}):
            return await run_plan(
                "combined install test single", items, client=None, classifier_model="x",
                plan_round_budget=99, plan_wall_clock_cap_seconds=99999,
            )

    result = asyncio.run(run())
    assert result["status"] == "completed"
    assert result["combined_install_check"] is None
    print("PASS: a single-module plan never runs the combined-install check -- nothing to combine")


def test_combined_install_check_skipped_when_db_not_configured():
    items = [_item("a"), _item("b", blocked_by=["a"])]
    module_names = {"a": "oma_module_a", "b": "oma_module_b"}

    async def fake_execute_contract(contract, *args, **kwargs):
        pid = contract.plan_item_id
        return {"status": "completed", "passed": True, "rounds_taken": 1, "build_output": _fake_output(module_names[pid])}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", None)
            return await run_plan(
                "combined install test no db", items, client=None, classifier_model="x",
                plan_round_budget=99, plan_wall_clock_cap_seconds=99999,
            )

    result = asyncio.run(run())
    assert result["status"] == "completed"
    assert result["combined_install_check"] is None
    print("PASS: with no target db configured, the check is gracefully skipped, never raises, never blocks the plan's own completion")


def test_combined_install_check_never_blocks_a_paused_plan(monkeypatch):
    # A plan that pauses partway through must return its normal pause result unaffected --
    # the combined-install check only runs on the plan's own final "completed" return path.
    items = [_item("a"), _item("b", blocked_by=["a"])]

    async def fake_execute_contract(contract, *args, **kwargs):
        if contract.plan_item_id == "a":
            return {"status": "completed", "passed": True, "rounds_taken": 1, "build_output": _fake_output("oma_module_a")}
        return {"status": "paused", "reason": "ask_operator"}

    monkeypatch.setenv("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev_dup_test")
    fake_verify = MagicMock()
    monkeypatch.setattr("tools_odoo.module_dev.toolchain.verify_combined_install", fake_verify)

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            return await run_plan(
                "combined install test paused", items, client=None, classifier_model="x",
                plan_round_budget=99, plan_wall_clock_cap_seconds=99999,
            )

    result = asyncio.run(run())
    assert result["status"] == "paused"
    assert "combined_install_check" not in result
    fake_verify.assert_not_called()
    print("PASS: a paused plan's return is completely unaffected -- the combined-install check never runs, never called")


def test_combined_install_check_failure_is_surfaced_not_swallowed(monkeypatch):
    items = [_item("a"), _item("b", blocked_by=["a"])]
    module_names = {"a": "oma_module_a", "b": "oma_module_b"}

    async def fake_execute_contract(contract, *args, **kwargs):
        pid = contract.plan_item_id
        return {"status": "completed", "passed": True, "rounds_taken": 1, "build_output": _fake_output(module_names[pid])}

    monkeypatch.setenv("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "odoo16_dev_dup_test")
    fake_install_result = MagicMock(success=False, message="Combined install failed (rc=1).")
    monkeypatch.setattr("tools_odoo.module_dev.toolchain.verify_combined_install", MagicMock(return_value=fake_install_result))

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            return await run_plan(
                "combined install test failure", items, client=None, classifier_model="x",
                plan_round_budget=99, plan_wall_clock_cap_seconds=99999,
            )

    result = asyncio.run(run())
    assert result["status"] == "completed"  # each item genuinely did pass its own individual verification
    assert result["combined_install_check"]["success"] is False
    assert "Combined install failed" in result["combined_install_check"]["message"]
    print("PASS: a real combined-install failure is surfaced honestly on the return value, never silently swallowed, and never retroactively flips an item's own already-real 'passed' status")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
