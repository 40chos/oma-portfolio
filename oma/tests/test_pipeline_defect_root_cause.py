"""P12 Tier A item 18 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
A Finding 17): tests for the new fifth classify_root_cause() category, PIPELINE_DEFECT --
real, confirmed gap: the original four-way taxonomy had no category for "the orchestration/
pipeline itself cannot succeed here" (e.g. Bug #2's data_change stub), forcing a
structural dead-path bug into skill_gap or pattern_worth_a_rule, both of which actively
misdirect remediation. Real Postgres writes via handle_failed_verification() (same convention
as tests/test_manager_learning.py's own sibling routing tests), zero LLM calls.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2.extras

from manager.learning import PIPELINE_DEFECT, VALID_ROOT_CAUSES, handle_failed_verification
from manager.memory import load_postgres_settings


def _get_row(row_id: int) -> dict:
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agent_memory_events WHERE id = %s", (row_id,))
        return dict(cur.fetchone())
    finally:
        conn.close()


def test_pipeline_defect_is_a_valid_root_cause():
    assert PIPELINE_DEFECT in VALID_ROOT_CAUSES
    assert PIPELINE_DEFECT == "pipeline_defect"
    print("PASS: pipeline_defect is a real, valid root-cause label")


def test_pipeline_defect_routes_to_a_distinct_engineering_queue():
    module = f"test.pipeline_defect.{uuid.uuid4().hex[:8]}"

    async def run():
        return await handle_failed_verification(
            task_id=str(uuid.uuid4()), module=module,
            failure_summary="data_change branch: would use OdooToolClient directly, no real handler exists yet",
            root_cause=PIPELINE_DEFECT,
        )

    result = asyncio.run(run())
    assert result["routed_to"] == "engineering_fix_needed"
    row = _get_row(result["row_id"])
    assert row["event_type"] == "note"
    assert "pipeline_defect" in row["tags"]
    assert "needs_engineering_fix" in row["tags"]
    assert row["detail"]["root_cause"] == "pipeline_defect"
    print(f"PASS: PIPELINE_DEFECT routes to its own distinct 'engineering_fix_needed' queue "
          f"(#{result['row_id']}), never conflated with skill_revision or proposed_rule")


def test_pipeline_defect_queue_is_genuinely_distinct_from_skill_gap_and_pattern():
    from manager.learning import PATTERN_WORTH_A_RULE, SKILL_GAP

    async def run():
        module = f"test.pipeline_defect_distinct.{uuid.uuid4().hex[:8]}"
        a = await handle_failed_verification(
            task_id=str(uuid.uuid4()), module=module, failure_summary="x", root_cause=PIPELINE_DEFECT,
        )
        b = await handle_failed_verification(
            task_id=str(uuid.uuid4()), module=module, failure_summary="x", root_cause=SKILL_GAP,
        )
        c = await handle_failed_verification(
            task_id=str(uuid.uuid4()), module=module, failure_summary="x", root_cause=PATTERN_WORTH_A_RULE,
        )
        return a, b, c

    a, b, c = asyncio.run(run())
    assert len({a["routed_to"], b["routed_to"], c["routed_to"]}) == 3, (
        f"all three routing destinations must be genuinely distinct: {a['routed_to']!r}, "
        f"{b['routed_to']!r}, {c['routed_to']!r}"
    )
    print("PASS: pipeline_defect, skill_gap, and pattern_worth_a_rule all route to three genuinely distinct destinations")


if __name__ == "__main__":
    test_pipeline_defect_is_a_valid_root_cause()
    test_pipeline_defect_routes_to_a_distinct_engineering_queue()
    test_pipeline_defect_queue_is_genuinely_distinct_from_skill_gap_and_pattern()
    print("\nALL PIPELINE-DEFECT ROOT-CAUSE TESTS PASSED")
