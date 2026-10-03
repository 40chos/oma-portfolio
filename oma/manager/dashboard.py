"""GET /api/tasks' aggregation query: every recent task, standalone
TaskContracts and outer-plan items alike, grouped into "Needs your
attention" / "In progress" / "Completed". A dedicated query over
agent_memory_events + Redis pending state, distinct from GET /api/plan/{plan_id}
(scoped to one plan) -- this is the cross-task dashboard view on top of it.
"""

from __future__ import annotations

import datetime

import psycopg2
import psycopg2.extras

from infra.redis_client import get_redis_client
from infra.settings import load_postgres_settings
from manager.escalations import list_pending_escalations
from manager.sign_off import get_pending_contract, list_pending_task_ids
from manager.task_plan import list_all_recent_plan_items
from manager.task_state import PAUSE_ROUND_BUDGET_EXHAUSTED, STATE_RUNNING

NEEDS_ATTENTION = "needs_attention"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"

# Sentinel (not None -- None is a meaningful "no row found" value for the
# prefetched-outcome/cancelled params below) marking "the caller didn't pass
# this, do the old per-task Postgres lookup instead."
_UNSET = object()


def _get_conn():
    s = load_postgres_settings()
    return psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)


def _recent_task_created_rows(limit: int) -> list[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT task_id, summary, detail, created_at FROM agent_memory_events "
            "WHERE event_type = 'task_created' ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _outcome_row_for_task(task_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM agent_memory_events WHERE event_type = 'outcome' AND task_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _cancelled_row_for_task(task_id: str) -> dict | None:
    """Any terminal, operator-initiated decision that ends a task without a
    further retry -- a cancel mid-round, or dismissing an escalation without
    replying with a fix.
    """
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM agent_memory_events WHERE event_type = 'decision' AND task_id = %s "
            "AND (tags && ARRAY['cancelled_by_operator', 'escalation_dismissed', 'terminated_stuck_call']) "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _outcome_rows_for_tasks(task_ids: list[str]) -> dict[str, dict]:
    """Batched replacement for a hot-path loop that used to open a brand-new
    Postgres connection per standalone task_id. One connection, one query for
    every task_id at once (DISTINCT ON picks the latest row per task_id, same
    "most recent wins" semantics a per-task LIMIT 1 has). `_outcome_row_for_task()`
    itself is left untouched for its other, genuinely single-task callers
    (e.g. `task_genuinely_passed()`).
    """
    if not task_ids:
        return {}
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT DISTINCT ON (task_id) * FROM agent_memory_events "
            "WHERE event_type = 'outcome' AND task_id = ANY(%s::uuid[]) "
            "ORDER BY task_id, id DESC",
            (task_ids,),
        )
        return {row["task_id"]: dict(row) for row in cur.fetchall()}
    finally:
        conn.close()


def _cancelled_rows_for_tasks(task_ids: list[str]) -> dict[str, dict]:
    """Batched sibling of `_outcome_rows_for_tasks()` -- same DISTINCT ON /
    "latest row per task_id in one round trip" approach, same cancellation-tag
    filter as the single-task `_cancelled_row_for_task()`.
    """
    if not task_ids:
        return {}
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT DISTINCT ON (task_id) * FROM agent_memory_events "
            "WHERE event_type = 'decision' AND task_id = ANY(%s::uuid[]) "
            "AND (tags && ARRAY['cancelled_by_operator', 'escalation_dismissed', 'terminated_stuck_call']) "
            "ORDER BY task_id, id DESC",
            (task_ids,),
        )
        return {row["task_id"]: dict(row) for row in cur.fetchall()}
    finally:
        conn.close()


def _standalone_task_status(
    task_id: str, pending_sign_off_ids: set[str], pending_escalation_ids: set[str], client,
    created_at: datetime.datetime | None = None,
    prefetched_outcome: dict | None = _UNSET,  # type: ignore[assignment]
    prefetched_cancelled: dict | None = _UNSET,  # type: ignore[assignment]
) -> tuple[str, str, str]:
    """Returns (section, status, one_liner) for one standalone task_id,
    checked in priority order: a pending decision beats everything else, then
    a durable terminal outcome, then live Redis task state. `status` is always
    one of five qualitative states (pending/running/passed/failed/paused);
    `section` is empty (caller excludes the task) if nothing real is found at
    all -- e.g. a task old enough that its 24h Redis state TTL expired without
    ever completing or being cancelled.

    `prefetched_outcome`/`prefetched_cancelled`: the bulk caller
    (`list_recent_tasks()`) batches these two Postgres lookups once for every
    task_id up front rather than this function opening its own fresh
    connection per call. The sentinel default (not None) keeps any other
    caller of this function working exactly as before, unbatched -- None is a
    meaningful value here (genuinely no outcome/cancellation row exists), so
    it can't double as "not provided."
    """
    outcome_prefetched = prefetched_outcome is not _UNSET
    cancelled_prefetched = prefetched_cancelled is not _UNSET
    # A durable Postgres terminal fact (an outcome row, or a cancelled/dismissed/
    # stuck-call decision) always outranks any Redis flag, since Redis state can
    # go stale on a hard restart -- checked first, before any Redis-based
    # pending/paused state. (A task hard-killed mid-round gets a durable
    # cancellation row written afterward, but a stale pending-escalation Redis
    # entry from its last escalation is only cleared by a normal Stop/Dismiss,
    # never a hard kill -- trusting Redis first would leave a cleanly terminated
    # task stuck showing as "Waiting on you" forever.)
    outcome = prefetched_outcome if outcome_prefetched else _outcome_row_for_task(task_id)
    if outcome:
        # An outcome row only means the task is genuinely done when Redis holds
        # no state at all for this task_id. Two real failure modes otherwise:
        # (1) a decomposed task reuses one task_id across every sub-contract, so
        # a multi-constraint task can write several outcome rows, one per
        # constraint passing -- an early "any outcome row = done" check would
        # report the whole task complete after just the first constraint passed.
        # (2) checking only `!= STATE_RUNNING` is also wrong, since every
        # `paused:*` state (gateway outage, ambiguity, sign-off, repeated
        # failure, round-budget-exhausted) is also != STATE_RUNNING -- a task
        # paused mid-way on a later constraint would still be reported done
        # using an earlier constraint's stale outcome row. The correct,
        # general condition: an outcome row is the task's final word only when
        # Redis holds NO state at all for this task_id (cleared at real
        # completion, or expired past its TTL) -- any live Redis value, running
        # or any paused:* variant, means real work is still outstanding.
        if client.get(f"oma:task:{task_id}:state") is None:
            return COMPLETED, "passed", outcome["summary"]

    cancelled = prefetched_cancelled if cancelled_prefetched else _cancelled_row_for_task(task_id)
    if cancelled:
        tags = cancelled.get("tags") or []
        # A `terminated_stuck_call` row (written when an external service
        # restart orphans an in-flight resume) is real and durable, but only
        # up until the task is legitimately resumed again -- scoped narrowly to
        # just this one cancellation reason, not every cancelled row, since a
        # genuine Stop/Dismiss deliberately does NOT always clear the Redis
        # "running" flag either (trusting Postgres over Redis in general is the
        # whole point), so a leftover "running" flag from before a real
        # dismiss/cancel must not reopen it. `terminated_stuck_call` is
        # different in kind: it's the one termination reason a resume is
        # explicitly designed to continue past, so a fresh running state after
        # it really is newer, more current evidence than the old termination row.
        if "terminated_stuck_call" in tags and client.get(f"oma:task:{task_id}:state") == STATE_RUNNING:
            pass  # a fresh resume is active -- fall through to the live-state checks below
        elif "escalation_dismissed" in tags:
            return COMPLETED, "failed", "Dismissed by Operator without a retry."
        elif "terminated_stuck_call" in tags:
            return COMPLETED, "failed", "Stopped -- was stuck on a hung call."
        else:
            return COMPLETED, "failed", "Cancelled by Operator."

    # A deliberate Pause click is a more recent, more specific fact than the
    # escalation that preceded it -- checked first among the remaining
    # Redis-only signals, so a paused branch genuinely shows as "Paused,"
    # never masked by the still-present pending-escalation entry underneath it
    # (Pause deliberately never clears that entry -- only Stop/Dismiss does).
    if client.get(f"oma:task:{task_id}:state") == PAUSE_ROUND_BUDGET_EXHAUSTED:
        return NEEDS_ATTENTION, "paused", "Paused -- resume when ready."
    if task_id in pending_escalation_ids:
        return NEEDS_ATTENTION, "paused", "Waiting on you -- escalated after retrying."
    if task_id in pending_sign_off_ids:
        return NEEDS_ATTENTION, "paused", "Waiting on your sign-off."

    state = client.get(f"oma:task:{task_id}:state")
    if state == STATE_RUNNING:
        return IN_PROGRESS, "running", "Running…"
    if state and state.startswith("paused:"):
        return NEEDS_ATTENTION, "paused", state.replace("paused:", "").replace("_", " ").capitalize()

    # task_created is written before mark_task_running() (classification and
    # contract-building happen in between, which can take a while) -- so a
    # brand-new task can have a task_created row but no Redis signal yet, and
    # would otherwise fall through every check above to full exclusion, making
    # a just-submitted task look like it never happened. Distinguish "about to
    # start" from "truly old and stale" by recency instead of excluding
    # unconditionally -- 30 minutes is generous (classification is normally
    # seconds), but a task incorrectly excluded reappearing as a false
    # "Starting…" is worse than occasionally being slow to fall back to exclusion.
    if created_at is not None:
        age = datetime.datetime.now(created_at.tzinfo) - created_at
        if age < datetime.timedelta(minutes=30):
            return IN_PROGRESS, "pending", "Starting…"
    return "", "", ""  # nothing real found -- caller excludes this task


def _plan_item_status(row: dict) -> tuple[str, str, str]:
    """Returns (section, status, one_liner) -- same three-tuple shape as
    `_standalone_task_status()`, from `active_task_plan_items`'s own
    mechanical status column.
    """
    status = row.get("status")
    if status == "passed":
        return COMPLETED, "passed", "Passed."
    if status == "failed":
        return NEEDS_ATTENTION, "failed", "Failed -- no active retry."
    if status == "blocked":
        return NEEDS_ATTENTION, "paused", (row.get("detail") or {}).get("pause_reason", "Blocked.")
    if status == "in_progress":
        return IN_PROGRESS, "running", "Running…"
    return IN_PROGRESS, "pending", "Queued -- waiting on a dependency."


def list_recent_tasks(limit: int = 5000, since_ts: datetime.datetime | None = None) -> dict:
    """The aggregation. Returns {"needs_attention": [...], "in_progress":
    [...], "completed": [...]}, each item shaped as {task_id, title,
    one_liner, kind: "standalone"|"plan_item", plan_id, plan_item_id,
    blocked_by}.

    `since_ts`: a purely non-destructive view filter -- nothing in Postgres is
    touched, so no history is lost, just hidden from a given call's result
    when the caller wants "what's happening now" rather than the full
    archive. Without it, days-old history mixed in with a freshly-started
    task batch can make the UI look "stuck" even though new tasks genuinely
    are appearing underneath the pile.
    """
    client = get_redis_client()
    pending_escalation_ids = {e["task_id"] for e in list_pending_escalations(client=client)}
    pending_sign_off_ids = set(list_pending_task_ids(client=client))

    sections: dict[str, list[dict]] = {NEEDS_ATTENTION: [], IN_PROGRESS: [], COMPLETED: []}

    seen_task_ids: set[str] = set()
    for row in list_all_recent_plan_items(limit=limit):
        if since_ts is not None and row.get("created_at") and row["created_at"] < since_ts:
            continue
        task_id = (row.get("detail") or {}).get("contract", {}).get("task_id")
        section, status, one_liner = _plan_item_status(row)
        contract_detail = (row.get("detail") or {}).get("contract", {})
        sections[section].append({
            "task_id": task_id,
            "kind": "plan_item",
            "plan_id": row.get("plan_id"),
            "plan_item_id": row.get("plan_item_id"),
            "title": contract_detail.get("goal", ""),
            # `title` for a plan_item is an AI-narrowed per-round restatement
            # ("This round's own NEW focus is ONLY: ..."), never the operator's
            # own words -- `original_goal` (set once, carried forward
            # unchanged) is the real, un-narrowed text the operator actually
            # typed, so the UI can label "what you asked" separately from
            # "what the AI is focused on this round."
            "original_goal": contract_detail.get("original_goal") or contract_detail.get("goal", ""),
            "goal_kind": "ai_narrowed",
            "one_liner": one_liner,
            "status": status,
            "blocked_by": contract_detail.get("blocked_by", []),
        })
        if task_id:
            seen_task_ids.add(task_id)

    # Two passes over the same rows, specifically so the two expensive
    # Postgres lookups can be batched once for every standalone task_id up
    # front (see `_outcome_rows_for_tasks()`) rather than
    # `_standalone_task_status()` opening a fresh connection per task_id, per
    # row, in a single combined loop.
    standalone_rows = []
    standalone_task_ids: list[str] = []
    for row in _recent_task_created_rows(limit=limit):
        if since_ts is not None and row.get("created_at") and row["created_at"] < since_ts:
            continue
        task_id = row["task_id"]
        if task_id in seen_task_ids:
            continue  # already covered as a plan item above
        seen_task_ids.add(task_id)
        standalone_rows.append(row)
        standalone_task_ids.append(task_id)

    prefetched_outcomes = _outcome_rows_for_tasks(standalone_task_ids)
    prefetched_cancellations = _cancelled_rows_for_tasks(standalone_task_ids)

    for row in standalone_rows:
        task_id = row["task_id"]
        section, status, one_liner = _standalone_task_status(
            task_id, pending_sign_off_ids, pending_escalation_ids, client,
            created_at=row.get("created_at"),
            prefetched_outcome=prefetched_outcomes.get(task_id),
            prefetched_cancelled=prefetched_cancellations.get(task_id),
        )
        if not section:
            continue  # nothing real found (state TTL expired) -- exclude
        sections[section].append({
            "task_id": task_id,
            "kind": "standalone",
            "plan_id": None,
            "plan_item_id": None,
            "title": row["summary"],
            # A standalone task was never decomposed, so `title` already IS
            # the operator's own verbatim text -- no separate original_goal to
            # recover, but the field is still set (mirroring itself) so the
            # frontend has one consistent contract across both task kinds.
            "original_goal": row["summary"],
            "goal_kind": "verbatim",
            "one_liner": one_liner,
            "status": status,
            "blocked_by": [],
        })

    return sections


def get_original_task(task_id: str) -> dict | None:
    """The original goal text and contract.inputs for a task_id -- whether it
    was a standalone task or an outer-plan item. None if no record of this
    task_id exists at all. Both fields matter for a real replay: goal alone
    isn't enough to reproduce a task's original runnability (e.g. a
    Code-Review target named via contract_inputs).
    """
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT summary, detail FROM agent_memory_events WHERE event_type = 'task_created' "
            "AND task_id = %s ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        if row:
            return {"goal": row["summary"], "inputs": (row["detail"] or {}).get("inputs", [])}

        cur.execute(
            "SELECT detail FROM agent_memory_events WHERE event_type = 'plan_item_status' "
            "AND (detail->'contract'->>'task_id') = %s ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        if row:
            contract = (row["detail"] or {}).get("contract", {})
            return {"goal": contract.get("goal"), "inputs": contract.get("inputs", [])}
        return None
    finally:
        conn.close()


def task_genuinely_passed(task_id: str) -> bool:
    """The "only act on a genuinely confirmed-complete result" gate the
    zip-delivery endpoint needs -- reuses the same two durable facts
    `_standalone_task_status()` establishes as the only trustworthy signal for
    COMPLETED/"passed" (a Postgres `outcome` row with `detail.passed=True`,
    and no live Redis state left for this task_id). Deliberately does not
    reuse `_standalone_task_status()` itself: that function also branches on
    pending sign-off/escalation ids and outer-plan-item status, none of which
    this narrower "is it safe to hand back a zip" question needs.
    """
    outcome = _outcome_row_for_task(task_id)
    if not outcome or not (outcome.get("detail") or {}).get("passed"):
        return False
    client = get_redis_client()
    return client.get(f"oma:task:{task_id}:state") is None
