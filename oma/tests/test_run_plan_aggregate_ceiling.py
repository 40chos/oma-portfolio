"""P12 Tier A item 20 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 21): tests for run_plan()'s new aggregate round/wall-clock ceiling -- real, confirmed
gap: each plan item only ever had its OWN per-item budget, nothing capped the plan as a whole.
Real Postgres writes via create_task_plan()/mark_plan_item_status() (same convention as
tests/test_manager_sign_off.py's own run_plan() tests), but _execute_contract() is mocked so
zero LLM/GPU calls happen anywhere -- client=None throughout, since the ceiling check fires
before any specialist would ever be touched.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.loop import run_plan


class _FakeOutput:
    """Minimal stand-in for SpecialistOutput -- run_plan() only ever reads
    .detail.get('module_name') off a passing item's build_output."""
    detail = {"module_name": "fake_module"}


def _item(plan_item_id: str, blocked_by=None) -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.module_development, tier=AutonomyTier.tier_1_readonly,
        goal=f"Plan-ceiling test item {plan_item_id}.", inputs=[], rules=[], deliverables=[],
        compensating_actions=[], validation_by="testing_qa", pause_if=[], turn_budget=10,
        plan_item_id=plan_item_id, blocked_by=blocked_by or [],
    )


def test_plan_stops_before_starting_an_item_once_round_budget_is_exhausted():
    items = [_item("a"), _item("b", blocked_by=["a"])]

    async def fake_execute_contract(contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "rounds_taken": 999, "build_output": _FakeOutput()}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            return await run_plan(
                "aggregate ceiling test", items, client=None, classifier_model="x",
                plan_round_budget=5, plan_wall_clock_cap_seconds=99999,
            )

    result = asyncio.run(run())
    assert result["status"] == "paused"
    assert result["paused_on"] == "b"
    assert result["pause_result"]["reason"] == "plan_budget_exhausted"
    assert "a" in result["item_results"]
    assert "b" not in result["item_results"], "item b must never have been started at all -- the aggregate ceiling was already exhausted by item a"
    print("PASS: run_plan() stops BEFORE starting the next item once the plan's own aggregate round budget is exhausted by earlier items")


def test_plan_completes_normally_when_within_the_aggregate_ceiling():
    items = [_item("a"), _item("b", blocked_by=["a"])]

    async def fake_execute_contract(contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "rounds_taken": 1, "build_output": _FakeOutput()}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            return await run_plan(
                "aggregate ceiling test 2", items, client=None, classifier_model="x",
                plan_round_budget=5, plan_wall_clock_cap_seconds=99999,
            )

    result = asyncio.run(run())
    assert result["status"] == "completed"
    assert set(result["item_results"].keys()) == {"a", "b"}
    print("PASS: a plan comfortably within its own aggregate ceiling completes both items normally, unaffected by the new check")


def test_ceiling_accumulates_across_multiple_earlier_items_not_just_the_last_one():
    items = [_item("a"), _item("b", blocked_by=["a"]), _item("c", blocked_by=["b"])]

    async def fake_execute_contract(contract, *args, **kwargs):
        return {"status": "completed", "passed": True, "rounds_taken": 3, "build_output": _FakeOutput()}

    async def run():
        with patch("manager.loop._execute_contract", new=AsyncMock(side_effect=fake_execute_contract)):
            return await run_plan(
                "aggregate ceiling test 3", items, client=None, classifier_model="x",
                plan_round_budget=5, plan_wall_clock_cap_seconds=99999,
            )

    result = asyncio.run(run())
    assert result["status"] == "paused"
    assert result["paused_on"] == "c"
    assert set(result["item_results"].keys()) == {"a", "b"}
    print("PASS: the ceiling correctly accumulates rounds ACROSS multiple earlier items (3+3=6 >= 5), not just the most recent one")


if __name__ == "__main__":
    test_plan_stops_before_starting_an_item_once_round_budget_is_exhausted()
    test_plan_completes_normally_when_within_the_aggregate_ceiling()
    test_ceiling_accumulates_across_multiple_earlier_items_not_just_the_last_one()
    print("\nALL RUN-PLAN AGGREGATE-CEILING TESTS PASSED")
