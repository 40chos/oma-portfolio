"""Phase 30, P3 (Phase G, §10, closes Problem G): turns a known,
already-active, human-authored rule into a hard, pre-flight,
deterministic gate -- checked BEFORE a contract is even built, instead
of only ever surfacing as a live decision Operator has to catch and reject
by hand, every time, forever.

Real, confirmed premise gap found before writing any of this (checked
against real code, not assumed from the plan's own text): the plan's
proposed insertion point implied reusing `contracts.module_identity.
resolve_module_identity()` (already called in `manager/loop.py` right
where this gate needed to go) as the "module touched" signal. Confirmed
live: `resolve_module_identity("Modify the ir.model.access.csv
permissions for account.move.")` returns `None` -- that exact goal is
the plan's own flagship real example (rejected by Operator 7+ separate
times), and it has no `Model:` metadata line at all, just prose.
`resolve_module_identity()` was built for a different, narrower purpose
(a fencing-lock key / repeated-failure module key) and only recognizes
that one structured convention -- reusing it here would have made this
whole gate silently fail on its own headline case. This module
therefore scans the raw goal text directly instead.

Predicate storage: `detail->>'structured_predicate'` on a `rule` row in
`agent_memory_events` (matching the plan's own "extend active_agent_
rules" framing -- no new table, no schema migration). A JSON object,
never a code string (never eval'd), matching the plan's own "not
something that needs an LLM to interpret" framing: literal regex
patterns, checked deterministically. Only rules that genuinely reduce
to a deterministic yes/no check ever get one -- every other rule stays
exactly as it already was, surfaced as prose via read_project_memory().

Deliberately checks `detail->>'status' == 'active'` explicitly, not
just `active_agent_rules`'s own view filter -- a real, confirmed gap
found live while researching this priority: that view only ever
excludes `status == 'proposed'`, never `'superseded'`/`'rejected'`, so
a stale row whose `detail` was updated to 'superseded' without also
having its own `active` column flipped to false (confirmed live: one
real row, id 9322, in exactly this state) would otherwise still read
as governable. Not fixed at the view level here (out of this
priority's own scope) -- defended against directly in this module's
own query instead.
"""

from __future__ import annotations

import re

import psycopg2
import psycopg2.extras

from infra.settings import load_postgres_settings


def _fetch_structured_governance_rules() -> list[dict]:
    """Every real, active rule row that carries a structured_predicate
    -- the small subset of `active_agent_rules` this gate can actually
    evaluate deterministically. Explicitly re-checks status == 'active'
    (see module docstring) rather than trusting the view alone.
    """
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, summary, detail
            FROM agent_memory_events
            WHERE event_type = 'rule'
              AND active = true
              AND detail->>'status' = 'active'
              AND detail ? 'structured_predicate'
            """
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _predicate_matches(goal: str, predicate: dict) -> bool:
    """Evaluates one structured_predicate against the real goal text.
    Only ever a literal, deterministic regex check -- never an LLM
    call, never eval() of anything from the DB. `type` is currently
    only ever "goal_text_matches_any" (the one shape this priority's
    real motivating rule needs); an unrecognized `type` is treated as
    non-matching (fail open on the STRUCTURED check -- the rule's own
    existing prose path, already running unconditionally via
    read_project_memory(), stays the real safety net regardless).
    """
    if predicate.get("type") != "goal_text_matches_any":
        return False
    patterns = predicate.get("patterns") or []
    if not any(re.search(p, goal, re.IGNORECASE) for p in patterns):
        return False
    carve_out_patterns = predicate.get("carve_out_patterns") or []
    if carve_out_patterns and any(re.search(p, goal, re.IGNORECASE) for p in carve_out_patterns):
        return False
    return True


def check_hard_governance_gates(goal: str) -> dict | None:
    """The real pre-flight check: returns a real, structured pause
    result the instant a goal matches an active, structured-predicate
    rule -- checked BEFORE a contract is generated or proposed, per the
    plan's own step 2. Returns None (never blocks) whenever nothing
    matches, including on any DB-level error (fails open, same
    discipline as classify_unsupported_domain_touches() -- a governance
    gate itself going down must never silently block every real task
    behind it; the rule's own prose path stays the fallback either way).
    """
    try:
        rules = _fetch_structured_governance_rules()
    except Exception:
        return None
    for rule in rules:
        predicate = rule["detail"]["structured_predicate"]
        if _predicate_matches(goal, predicate):
            directive = rule["detail"].get("directive", rule["summary"])
            return {
                "status": "paused",
                "reason": "governance_rule_blocked",
                "message": (
                    f"This matches a known, active governance rule, blocked before being proposed "
                    f"to you: \"{directive}\" (rule id {rule['id']}). Please confirm explicitly "
                    f"before this proceeds."
                ),
                "matched_rule_id": rule["id"],
            }
    return None
