"""Phase 1, step 4: standalone test of the memory schema alone.

Inserts a fake issue -> attempt -> outcome chain, inserts a rule that
supersedes an earlier rule, and confirms active_agent_rules returns only
the current one. Deliberately isolates "is the memory layer correct" from
"is the agent logic correct" -- no agent code exists yet.

Reads connection details only from the environment (populated from .env
by the caller) -- never hardcodes a credential.
"""

import os
import sys
import uuid

import psycopg2
import psycopg2.extras


def get_conn():
    return psycopg2.connect(
        host=os.environ["OMA_PG_HOST"],
        port=os.environ["OMA_PG_PORT"],
        dbname=os.environ["OMA_PG_DB"],
        user=os.environ["OMA_PG_USER"],
        password=os.environ["OMA_PG_PASSWORD"],
    )


def main() -> int:
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Self-cleaning: this script must be safely rerunnable, so wipe any
    # rows a previous run of *this same test* left behind before starting.
    cur.execute(
        "DELETE FROM agent_memory_events WHERE 'schema-self-test' = ANY(tags) "
        "OR module IN ('res.partner', 'account.move', 'stock.valuation.layer')"
    )

    task_id = str(uuid.uuid4())
    failures = []

    # --- issue -> attempt -> outcome chain, tied by task_id ---
    cur.execute(
        """
        INSERT INTO agent_memory_events
            (event_type, task_id, module, tags, actor, summary, detail)
        VALUES
            ('issue', %s, 'res.partner', ARRAY['contact','field-add'],
             'manager', 'Operator asked for a preferred_language field on contacts',
             '{}'::jsonb)
        RETURNING id
        """,
        (task_id,),
    )
    issue_id = cur.fetchone()["id"]

    cur.execute(
        """
        INSERT INTO agent_memory_events
            (event_type, task_id, module, tags, actor, summary, detail)
        VALUES
            ('attempt', %s, 'res.partner', ARRAY['contact','field-add'],
             'bug_fix', 'Build specialist added the field and view change',
             '{"module_written": "oma_contact_language"}'::jsonb)
        RETURNING id
        """,
        (task_id,),
    )
    attempt_id = cur.fetchone()["id"]

    cur.execute(
        """
        INSERT INTO agent_memory_events
            (event_type, task_id, module, tags, actor, summary, detail, verified)
        VALUES
            ('outcome', %s, 'res.partner', ARRAY['contact','field-add'],
             'testing_qa', 'Field confirmed present and editable on contact form',
             '{"passed": true}'::jsonb, true)
        RETURNING id
        """,
        (task_id,),
    )
    outcome_id = cur.fetchone()["id"]

    cur.execute(
        "SELECT task_id FROM agent_memory_events WHERE id IN (%s,%s,%s)",
        (issue_id, attempt_id, outcome_id),
    )
    rows = cur.fetchall()
    if not all(str(r["task_id"]) == task_id for r in rows) or len(rows) != 3:
        failures.append("issue/attempt/outcome chain did not share task_id correctly")

    # --- rule supersede chain ---
    cur.execute(
        """
        INSERT INTO agent_memory_events
            (event_type, module, tags, actor, summary, detail, stale_after)
        VALUES
            ('rule', 'account.move', ARRAY['financial'], 'operator',
             'OLD RULE: always round tax at line level',
             '{"status": "active"}'::jsonb, NULL)
        RETURNING id
        """
    )
    old_rule_id = cur.fetchone()["id"]

    cur.execute(
        """
        INSERT INTO agent_memory_events
            (event_type, module, tags, actor, summary, detail, supersedes_id, stale_after)
        VALUES
            ('rule', 'account.move', ARRAY['financial'], 'operator',
             'NEW RULE: round tax at document level, not line level',
             '{"status": "active"}'::jsonb, %s, NULL)
        RETURNING id
        """,
        (old_rule_id,),
    )
    new_rule_id = cur.fetchone()["id"]

    cur.execute("SELECT active FROM agent_memory_events WHERE id = %s", (old_rule_id,))
    old_active = cur.fetchone()["active"]
    if old_active is not False:
        failures.append(
            f"mark_superseded trigger did not deactivate old rule {old_rule_id}"
        )

    cur.execute("SELECT id, active FROM active_agent_rules WHERE module = 'account.move'")
    view_rows = cur.fetchall()
    view_ids = {r["id"] for r in view_rows}
    if old_rule_id in view_ids:
        failures.append("active_agent_rules still returned the superseded rule")
    if new_rule_id not in view_ids:
        failures.append("active_agent_rules did not return the current rule")
    if len(view_rows) != 1:
        failures.append(
            f"active_agent_rules returned {len(view_rows)} rows for account.move, expected 1"
        )

    # --- stale_after expiry policy (a rule whose window has already passed) ---
    cur.execute(
        """
        INSERT INTO agent_memory_events
            (event_type, module, tags, actor, summary, detail, stale_after, created_at)
        VALUES
            ('rule', 'stock.valuation.layer', ARRAY['financial'], 'operator',
             'Stale test rule from the deep past',
             '{"status": "active"}'::jsonb, INTERVAL '1 hour', now() - INTERVAL '2 hours')
        RETURNING id
        """
    )
    stale_rule_id = cur.fetchone()["id"]
    cur.execute(
        "SELECT id FROM active_agent_rules WHERE id = %s", (stale_rule_id,)
    )
    if cur.fetchone() is not None:
        failures.append("active_agent_rules returned a rule past its stale_after window")

    cur.close()
    conn.close()

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: issue->attempt->outcome chain, supersede trigger, "
          "active_agent_rules filtering, and stale_after expiry all behave correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
