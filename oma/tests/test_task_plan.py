"""Phase 15 (§19.9): tests 4 and 5 -- the outer plan layer. Real
Postgres throughout, no mocks.
"""

import glob
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from manager.task_plan import create_task_plan, list_plan_items, mark_plan_item_status, next_runnable_item


def _make(goal, plan_item_id, blocked_by=None):
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix, capability_class=CapabilityClass.module_development,
        tier=AutonomyTier.tier_1_readonly, goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
        plan_item_id=plan_item_id, blocked_by=blocked_by or [],
    )


def test_three_item_chain_with_blocked_by_and_supersession():
    """Test 4: three chained TaskContracts with real blocked_by edges --
    next_runnable_item() must withhold item 3 until item 2 shows
    'passed', and the view must correctly reflect status after
    mark_plan_item_status() calls, including a supersession case (item
    2 fails once, retries, then passes -- the view shows the LATEST
    status, not the first).
    """
    item1 = _make("base model", "chain-1")
    item2 = _make("depends on base", "chain-2", blocked_by=["chain-1"])
    item3 = _make("depends on item 2", "chain-3", blocked_by=["chain-2"])

    plan_id = create_task_plan("three-item chain test", [item1, item2, item3])

    # Only item 1 is runnable at the start.
    r = next_runnable_item(plan_id)
    assert r.plan_item_id == "chain-1"
    mark_plan_item_status(plan_id, "chain-1", "passed")

    # Item 3 must NOT be runnable yet, even though item 1 already passed --
    # it's blocked on item 2, not item 1.
    r2 = next_runnable_item(plan_id)
    assert r2.plan_item_id == "chain-2", "item 2 must run before item 3, even though item 1 already passed"

    # Item 2 fails once, then retries and passes -- a real supersession:
    # multiple plan_item_status rows for the SAME plan_item_id.
    mark_plan_item_status(plan_id, "chain-2", "failed")
    items_after_failure = list_plan_items(plan_id)
    chain2_status = next(i["status"] for i in items_after_failure if i["plan_item_id"] == "chain-2")
    assert chain2_status == "failed"

    still_blocked = next_runnable_item(plan_id)
    assert still_blocked is None, "item 3 must still be blocked -- item 2 failed, not passed"

    mark_plan_item_status(plan_id, "chain-2", "passed")
    items_after_retry = list_plan_items(plan_id)
    chain2_status_now = next(i["status"] for i in items_after_retry if i["plan_item_id"] == "chain-2")
    assert chain2_status_now == "passed", (
        "the view must show the LATEST status (passed), not the first (failed) -- supersession, not mutation"
    )

    r3 = next_runnable_item(plan_id)
    assert r3.plan_item_id == "chain-3", "item 3 must now be runnable, since item 2 shows passed"
    mark_plan_item_status(plan_id, "chain-3", "passed")
    assert next_runnable_item(plan_id) is None

    print("PASS: a 3-item chain with real blocked_by edges withholds each item until its real "
          "dependency passes, and the view correctly reflects supersession (latest status wins, "
          "not the first)")


def test_create_task_plan_and_mark_plan_item_status_are_the_only_write_sites():
    """Test 5: a grep-based structural proof, same pattern as
    test_manager_tools.py's existing single-write-path proof for
    append_project_memory() -- confirm create_task_plan()/
    mark_plan_item_status() are the only call sites anywhere in the
    codebase that construct a plan_created/plan_item_status event.
    """
    project_root = os.path.join(os.path.dirname(__file__), "..")
    py_files = glob.glob(os.path.join(project_root, "**", "*.py"), recursive=True)
    py_files = [
        f for f in py_files
        if "/.venv/" not in f and "/tests/" not in f and "/__pycache__/" not in f
    ]

    offending = []
    for path in py_files:
        if path.endswith("manager/task_plan.py"):
            continue  # the one legitimate place these event_type values are constructed
        text = open(path, encoding="utf-8", errors="ignore").read()
        if re.search(r"""event_type\s*=\s*["']plan_created["']""", text):
            offending.append((path, "plan_created"))
        if re.search(r"""event_type\s*=\s*["']plan_item_status["']""", text):
            offending.append((path, "plan_item_status"))

    assert not offending, (
        f"only manager/task_plan.py may construct plan_created/plan_item_status events, "
        f"found elsewhere: {offending}"
    )
    print("PASS: create_task_plan()/mark_plan_item_status() (manager/task_plan.py) are the ONLY "
          "call sites anywhere constructing plan_created/plan_item_status events -- grep-verified")


if __name__ == "__main__":
    test_three_item_chain_with_blocked_by_and_supersession()
    test_create_task_plan_and_mark_plan_item_status_are_the_only_write_sites()
    print("\nALL TASK PLAN TESTS PASSED")
