"""Phase 6 tests: the error-learning mechanism (§2.7) -- root-cause
classification, its routing consequences, and the repeated-failure
check, against the real gateway and real Postgres.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

from infra.gateway_client import ModelGatewayClient
from infra.settings import load_postgres_settings
from manager.learning import (
    PATTERN_WORTH_A_RULE,
    SKILL_GAP,
    UNCLEAR,
    VALID_ROOT_CAUSES,
    check_repeated_failures,
    classify_root_cause,
    find_existing_superseded_match,
    handle_failed_verification,
)
from manager.tools import append_project_memory

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


def _get_row(row_id: int) -> dict:
    import psycopg2.extras
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agent_memory_events WHERE id = %s", (row_id,))
        return dict(cur.fetchone())
    finally:
        conn.close()


async def _run_classify_root_cause_tests(client):
    result = await classify_root_cause(
        "A single test failed because a stale test fixture had an outdated timestamp; "
        "unrelated to any real bug in the code.",
        client, CLASSIFIER_MODEL,
    )
    assert result in VALID_ROOT_CAUSES
    print(f"PASS: root-cause classification for a clear one-off case returned {result!r} "
          f"(a valid label; LLM output isn't pinned to one exact string, but must be valid)")

    result2 = await classify_root_cause(
        "Nonsense gibberish input with no real failure description at all: qwerty asdf 12345",
        client, CLASSIFIER_MODEL,
    )
    assert result2 in VALID_ROOT_CAUSES
    print(f"PASS: root-cause classification degrades to a valid label even for nonsense input: {result2!r}")


async def _run_forced_incomplete_falls_back_to_unclear(client):
    """Force max_tokens so small the model can't possibly finish
    thinking -- confirm classify_root_cause() catches the resulting
    IncompleteResponseError internally and returns UNCLEAR rather than
    raising.
    """
    import manager.learning as learning_mod
    original_generate_checked = learning_mod.generate_checked

    async def _forced_incomplete(*args, **kwargs):
        from infra.structured_output import IncompleteResponseError
        raise IncompleteResponseError("forced for test")

    learning_mod.generate_checked = _forced_incomplete
    try:
        result = await classify_root_cause("anything", client, CLASSIFIER_MODEL)
        assert result == UNCLEAR
        print("PASS: a forced IncompleteResponseError correctly falls back to UNCLEAR, not a crash")
    finally:
        learning_mod.generate_checked = original_generate_checked


async def _run_handle_failed_verification_routing_test():
    module = f"test.learning.{uuid.uuid4().hex[:8]}"

    skill_gap_result = await handle_failed_verification(
        task_id=str(uuid.uuid4()), module=module,
        failure_summary="The skill told the specialist to skip a validation step that turned out to be required",
        root_cause=SKILL_GAP,
    )
    assert skill_gap_result["routed_to"] == "skill_revision"
    row = _get_row(skill_gap_result["row_id"])
    assert row["event_type"] == "note"
    assert "skill_gap" in row["tags"]
    print(f"PASS: SKILL_GAP routes to a skill-revision note (#{skill_gap_result['row_id']})")

    pattern_result = await handle_failed_verification(
        task_id=str(uuid.uuid4()), module=module,
        failure_summary="Discount rounding keeps failing the same way across multiple tasks",
        root_cause=PATTERN_WORTH_A_RULE,
    )
    assert pattern_result["routed_to"] == "proposed_rule_pending_confirmation"
    row2 = _get_row(pattern_result["row_id"])
    assert row2["event_type"] == "rule"
    assert row2["detail"]["status"] == "proposed"
    assert row2["detail"]["origin"] == "self_detected_pattern"
    print(f"PASS: PATTERN_WORTH_A_RULE routes into the SAME proposed-rule pipeline as a "
          f"Operator correction (#{pattern_result['row_id']}), tagged self_detected_pattern, "
          f"not auto-enforced")

    one_off_result = await handle_failed_verification(
        task_id=str(uuid.uuid4()), module=module,
        failure_summary="A one-off timing glitch", root_cause="one_off",
    )
    assert one_off_result["routed_to"] == "none"
    print("PASS: one_off correctly results in no further routing action")


async def _run_phase29d_already_covered_routing_test():
    """Phase 29D (2026-07-29): a PATTERN_WORTH_A_RULE failure whose
    normalized claim-shape ALREADY matches a real, previously-superseded
    row must be routed to 'already_covered_by_existing_validator', not
    re-added as a fresh proposed_rule -- closing the exact backlog
    re-accumulation loop root_cause.md described.
    """
    module = f"test.learning29d.{uuid.uuid4().hex[:8]}"
    unique_marker = uuid.uuid4().hex[:8]

    superseded_row_id = append_project_memory(
        event_type="rule", actor="manager", module=module,
        summary=f"Field '{unique_marker}_alpha' referenced in view does not exist on model '{unique_marker}_beta'",
        tags=["proposed_rule", "self_detected"],
        detail={"status": "superseded", "superseded_by": "_validate_fake_thing_for_test", "origin": "self_detected_pattern"},
        verified=False,
    )

    same_shape_summary = (
        f"Field '{unique_marker}_gamma' referenced in view does not exist on model '{unique_marker}_delta'"
    )
    match = find_existing_superseded_match(same_shape_summary)
    assert match == "_validate_fake_thing_for_test", (
        f"expected the differently-worded (same shape) failure to match the superseded row's own "
        f"validator name, got {match!r}"
    )
    print(f"PASS: find_existing_superseded_match found the real prior superseded row "
          f"(#{superseded_row_id}) despite different literal identifiers")

    result = await handle_failed_verification(
        task_id=str(uuid.uuid4()), module=module,
        failure_summary=same_shape_summary, root_cause=PATTERN_WORTH_A_RULE,
    )
    assert result["routed_to"] == "already_covered_by_existing_validator"
    assert result["validator"] == "_validate_fake_thing_for_test"
    row = _get_row(result["row_id"])
    assert row["event_type"] == "note"
    assert "proposed_rule_already_covered" in row["tags"]
    print(f"PASS: handle_failed_verification routes an already-covered pattern to a note "
          f"(#{result['row_id']}), never a duplicate proposed_rule row")

    unrelated_summary = f"Totally unrelated failure about {unique_marker}_never_seen_before, no shape match at all"
    no_match = find_existing_superseded_match(unrelated_summary)
    assert no_match is None, "a genuinely different claim shape must never produce a false match"
    print("PASS: a genuinely unrelated failure summary correctly finds no match")


def test_check_repeated_failures():
    module = f"test.repeat.{uuid.uuid4().hex[:8]}"

    # Zero prior failures -- must not pause.
    result0 = check_repeated_failures(module)
    assert result0["should_pause"] is False
    assert result0["prior_failure_count"] == 0

    # One prior failure -- still must not pause (threshold is 2+).
    append_project_memory(
        event_type="outcome", actor="testing_qa", module=module,
        summary="First failed attempt", tags=["failed", module],
        detail={"passed": False},
    )
    result1 = check_repeated_failures(module)
    assert result1["should_pause"] is False, "1 prior failure should not yet trip the pause"
    assert result1["prior_failure_count"] == 1

    # Second prior failure -- NOW it must pause, with a plain-language message.
    append_project_memory(
        event_type="outcome", actor="testing_qa", module=module,
        summary="Second failed attempt, same underlying issue", tags=["failed", module],
        detail={"passed": False},
    )
    result2 = check_repeated_failures(module)
    assert result2["should_pause"] is True
    assert result2["prior_failure_count"] == 2
    assert "attempt number 3" in result2["message"]
    assert module in result2["message"]
    print(f"PASS: check_repeated_failures correctly stays silent at 0/1 prior failures and "
          f"trips a plain-language pause at 2: {result2['message']!r}")


async def main():
    client = ModelGatewayClient()
    try:
        await _run_classify_root_cause_tests(client)
        await _run_forced_incomplete_falls_back_to_unclear(client)
    finally:
        await client.aclose()
    await _run_handle_failed_verification_routing_test()
    await _run_phase29d_already_covered_routing_test()
    test_check_repeated_failures()
    print("\nALL MANAGER LEARNING TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
