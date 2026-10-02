"""The correction-detection mechanism, built for real here -- per the
build plan's own warning, no later phase revisits this if it's skipped
now. Follows the technical document's §6 exactly:

  1. A cheap classifier call after every message returns
     {is_correction, confidence}.
  2. Below CONFIDENCE_THRESHOLD: log a `possible_correction` note
     rather than dropping the message silently.
  3. At or above threshold: extract the directive and its applicability
     condition, insert as event_type='rule' with
     detail->>'status'='proposed' -- NOT active yet. The Manager's next
     reply must surface this as an explicit confirm/reject/edit prompt
     (Phase 12's confidence-card pattern); only Operator's explicit
     confirmation (confirm_proposed_rule()) flips it to 'active', which
     is what active_agent_rules (per the Phase 6 migration) actually
     starts enforcing.
"""

from __future__ import annotations

import json
import re

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import IncompleteResponseError, generate_checked
from manager.tools import append_project_memory

CONFIDENCE_THRESHOLD = 0.6

_DETECT_PROMPT_TEMPLATE = """\
Operator (the product owner) just sent this message to the Odoo Manager \
Agent. Decide whether it is a CORRECTION -- Operator telling the Manager to \
change its behavior, a standing rule, or a prior decision -- as opposed \
to a normal request, question, or task.

Message: {message}

If it IS a correction, also extract:
- "directive": the concrete instruction going forward (one sentence)
- "applicability_condition": when this directive applies (one phrase, \
e.g. "whenever a task touches the accounting module")

Respond with ONLY a JSON object of this exact shape:
{{"is_correction": true|false, "confidence": 0.0-1.0, \
"directive": "..." or null, "applicability_condition": "..." or null}}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


async def detect_correction(
    message: str,
    client: ModelGatewayClient,
    model: str,
    max_tokens: int = 1500,
    task_id: str | None = None,
) -> dict:
    """Returns {is_correction, confidence, directive, applicability_condition}.
    Never raises to the caller in normal operation -- degrades to
    is_correction=False, confidence=0.0 on any failure (including
    IncompleteResponseError from generate_checked()), matching Nexo's
    real "silently degrade, never block the conversation" shape for
    background classification calls.
    """
    prompt = _DETECT_PROMPT_TEMPLATE.format(message=message)
    try:
        cleaned = await generate_checked(
            client, model, [{"role": "user", "content": prompt}], max_tokens=max_tokens,
            task_id=task_id, actor="manager", call_label="Checking for a correction to past behavior",
        )
        match = _JSON_RE.search(cleaned)
        if not match:
            raise IncompleteResponseError(f"no JSON object found in {cleaned!r}")
        parsed = json.loads(match.group(0))
        return {
            "is_correction": bool(parsed.get("is_correction", False)),
            "confidence": float(parsed.get("confidence", 0.0)),
            "directive": parsed.get("directive"),
            "applicability_condition": parsed.get("applicability_condition"),
        }
    except Exception:
        return {
            "is_correction": False,
            "confidence": 0.0,
            "directive": None,
            "applicability_condition": None,
        }


async def handle_correction_detection(
    message: str,
    client: ModelGatewayClient,
    model: str,
    actor: str = "operator",
    task_id: str | None = None,
) -> dict:
    """Runs detect_correction() and writes the appropriate memory row,
    exclusively through append_project_memory() -- never a direct
    insert of its own. Returns a status dict the loop uses to decide
    whether to surface a confirm/reject/edit prompt on the next reply.
    """
    detection = await detect_correction(message, client, model, task_id=task_id)

    if not detection["is_correction"]:
        return {"status": "not_a_correction"}

    if detection["confidence"] < CONFIDENCE_THRESHOLD:
        row_id = append_project_memory(
            event_type="note",
            actor=actor,
            summary=f"possible_correction (confidence={detection['confidence']:.2f}): {message[:300]}",
            tags=["possible_correction"],
            detail={"possible_correction": True, "confidence": detection["confidence"], "message": message},
            verified=False,
        )
        return {"status": "logged_as_possible_correction", "row_id": row_id, "confidence": detection["confidence"]}

    directive = detection["directive"] or message[:300]
    row_id = append_project_memory(
        event_type="rule",
        actor=actor,
        summary=directive,
        tags=["proposed_rule", "operator_originated"],
        detail={
            "status": "proposed",
            "directive": directive,
            "applicability_condition": detection["applicability_condition"],
            "origin": "operator_correction",
            "confidence": detection["confidence"],
        },
        verified=False,
    )
    return {
        "status": "proposed_rule_pending_confirmation",
        "row_id": row_id,
        "directive": directive,
        "applicability_condition": detection["applicability_condition"],
    }


def list_pending_proposed_rules() -> list[dict]:
    """Phase 12: for the chat UI's pending-items queue -- every proposed
    rule still awaiting Operator's confirm/reject, a real read against the
    same rows confirm_proposed_rule()/reject_proposed_rule() update.
    """
    import psycopg2
    import psycopg2.extras

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, summary, detail, tags, created_at
            FROM agent_memory_events
            WHERE event_type = 'rule' AND detail->>'status' = 'proposed' AND active = true
            ORDER BY id DESC
            """
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def confirm_proposed_rule(rule_row_id: int) -> None:
    """The ONE deliberate, narrow exception to append_project_memory
    being the sole write path: flipping an existing proposed rule's
    status to 'active' is a state transition on that SAME row (Operator
    confirming something already logged), not new information being
    recorded -- so it's a targeted UPDATE, not a new INSERT. Only ever
    called after Operator's EXPLICIT confirmation (never automatically).
    """
    import psycopg2

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE agent_memory_events
            SET detail = detail || '{"status": "active"}'::jsonb,
                verified = true
            WHERE id = %s AND event_type = 'rule'
            """,
            (rule_row_id,),
        )
        if cur.rowcount != 1:
            raise ValueError(f"expected to update exactly 1 row for rule_row_id={rule_row_id}, "
                              f"updated {cur.rowcount}")
        cur.close()
    finally:
        conn.close()


def supersede_proposed_rule(rule_row_id: int, validator_name: str) -> None:
    """Phase 28D (2026-07-29): the third real state a proposed rule can
    land in, alongside confirm/reject -- built for real per the
    root-cause investigation into why 637 of 639 self-detected
    'pattern_worth_a_rule' catches were sitting unconfirmed: cross-
    referencing their own failure shape against this project's already-
    existing `_validate_*`/`_autofix_*` catalog found the large
    majority were NOT waiting to become code at all -- they already
    HAD a deterministic check covering their exact claim shape,
    written independently (during live debugging, the same way every
    fix this session was built), and the proposed-rule row was simply
    never marked as closed. `superseded` is a genuinely different
    outcome from `rejected` (rejected means "this was wrong/not worth
    doing"; superseded means "this was RIGHT, and it's already
    handled") -- conflating the two would misrepresent the historical
    record of what was actually caught and why.

    Same targeted-UPDATE shape as confirm_proposed_rule()/reject_
    proposed_rule() -- a state transition on an existing row, not new
    information, so it stays outside append_project_memory()'s own
    sole-write-path contract for the same reason those two do.
    `active=false` matches reject's own convention (a superseded rule
    should never be surfaced as if still pending either); `verified`
    stays whatever it already was (superseding isn't a confirmation
    that the ORIGINAL catch was accurate, just that it's redundant now).
    """
    import psycopg2

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE agent_memory_events
            SET detail = detail || jsonb_build_object('status', 'superseded', 'superseded_by', %s::text),
                active = false
            WHERE id = %s AND event_type = 'rule'
            """,
            (validator_name, rule_row_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"expected to update exactly 1 row for rule_row_id={rule_row_id}, "
                              f"updated {cur.rowcount}")
        cur.close()
    finally:
        conn.close()


def reject_proposed_rule(rule_row_id: int) -> None:
    """Operator rejecting a proposed rule -- marks it inactive so it never
    gets surfaced or confused with a real active rule again. Also a
    targeted UPDATE for the same reason as confirm_proposed_rule().
    """
    import psycopg2

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE agent_memory_events
            SET detail = detail || '{"status": "rejected"}'::jsonb,
                active = false
            WHERE id = %s AND event_type = 'rule'
            """,
            (rule_row_id,),
        )
        if cur.rowcount != 1:
            raise ValueError(f"expected to update exactly 1 row for rule_row_id={rule_row_id}, "
                              f"updated {cur.rowcount}")
        cur.close()
    finally:
        conn.close()
