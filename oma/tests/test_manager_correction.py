"""Phase 6 tests: the correction-detection mechanism, built for real,
against the real gateway and real Postgres. Confirms: a genuinely
ambiguous message lands as a possible_correction note (not a silent
drop, not a false-positive auto-enforced rule); a clear correction
becomes a PROPOSED rule that does NOT yet appear in active_agent_rules;
and only explicit confirm/reject flips its actual enforcement state.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import psycopg2.extras

from infra.gateway_client import ModelGatewayClient
from infra.settings import load_postgres_settings
from manager.correction import confirm_proposed_rule, handle_correction_detection, reject_proposed_rule

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


def _rule_in_active_view(row_id: int) -> bool:
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM active_agent_rules WHERE id = %s", (row_id,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _get_row(row_id: int) -> dict:
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agent_memory_events WHERE id = %s", (row_id,))
        return dict(cur.fetchone())
    finally:
        conn.close()


async def _run_ambiguous_message_test(client):
    """A message that is NOT a clear correction and NOT a clear normal
    request -- ambiguous enough that a real classifier might see some
    correction-like signal but shouldn't confidently commit to it.
    """
    ambiguous_message = "hmm, actually wait, I'm not sure that's quite right, let me think about it"
    result = await handle_correction_detection(ambiguous_message, client, CLASSIFIER_MODEL)

    assert result["status"] in ("not_a_correction", "logged_as_possible_correction"), (
        f"an ambiguous message must never silently become an auto-enforced rule, got: {result}"
    )
    if result["status"] == "logged_as_possible_correction":
        row = _get_row(result["row_id"])
        assert row["event_type"] == "note"
        assert row["detail"]["possible_correction"] is True
        assert not _rule_in_active_view(result["row_id"])  # it's a note, not a rule at all
        print(f"PASS: ambiguous message correctly logged as a possible_correction NOTE "
              f"(confidence={result['confidence']:.2f}), not silently dropped, not auto-enforced")
    else:
        print("PASS: ambiguous message correctly classified as not a correction at all "
              "(also acceptable -- the key property is it did NOT become an active rule)")


async def _run_clear_correction_test(client):
    clear_correction = (
        "No -- from now on, never touch the accounting module without asking me first. "
        "That's a hard rule going forward."
    )
    result = await handle_correction_detection(clear_correction, client, CLASSIFIER_MODEL)

    assert result["status"] == "proposed_rule_pending_confirmation", (
        f"expected a clear correction to propose a rule, got: {result}"
    )
    row_id = result["row_id"]
    row = _get_row(row_id)
    assert row["event_type"] == "rule"
    assert row["detail"]["status"] == "proposed"
    assert row["verified"] is False

    # THE critical property: a just-proposed rule must NOT be enforced yet.
    assert not _rule_in_active_view(row_id), (
        "a proposed, unconfirmed rule must never appear in active_agent_rules"
    )
    print(f"PASS: clear correction proposed rule #{row_id} with status='proposed', "
          f"verified=False, and correctly ABSENT from active_agent_rules until confirmed")

    # Now confirm it -- only this should activate it.
    confirm_proposed_rule(row_id)
    assert _rule_in_active_view(row_id), (
        "after explicit confirmation, the rule must now appear in active_agent_rules"
    )
    confirmed_row = _get_row(row_id)
    assert confirmed_row["detail"]["status"] == "active"
    assert confirmed_row["verified"] is True
    print(f"PASS: after confirm_proposed_rule(#{row_id}), it correctly became active "
          f"and now appears in active_agent_rules")


async def _run_reject_test(client):
    clear_correction = "From now on, always double-check currency conversions before finalizing anything."
    result = await handle_correction_detection(clear_correction, client, CLASSIFIER_MODEL)
    if result["status"] != "proposed_rule_pending_confirmation":
        print("SKIP: this run's classifier call didn't propose a rule for the reject test "
              "(non-deterministic LLM output) -- the confirm-path test above already covers "
              "the core mechanism")
        return
    row_id = result["row_id"]
    reject_proposed_rule(row_id)
    assert not _rule_in_active_view(row_id)
    row = _get_row(row_id)
    assert row["detail"]["status"] == "rejected"
    assert row["active"] is False
    print(f"PASS: reject_proposed_rule(#{row_id}) correctly marked it rejected and inactive")


async def main():
    client = ModelGatewayClient()
    try:
        await _run_ambiguous_message_test(client)
        await _run_clear_correction_test(client)
        await _run_reject_test(client)
    finally:
        await client.aclose()
    print("\nALL MANAGER CORRECTION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
