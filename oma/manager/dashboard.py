"""Phase 16 (§20/mockup's Task Plan panel): GET /api/tasks' real query --
every recent task, standalone TaskContracts and outer-plan items alike,
grouped into "Needs your attention" / "In progress" / "Completed",
matching the approved mockup's own section shape. A real, new query
over agent_memory_events + Redis pending state, not an existing
endpoint repurposed -- Phase 15's GET /api/plan/{plan_id} stays scoped
to one plan, this is the cross-task dashboard on top.
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

# Real sentinel (not None -- None is a real, meaningful "no row found"
# value for the prefetched-outcome/cancelled params below) marking "the
# caller didn't pass this, do the old per-task Postgres lookup instead."
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
    """Any real, terminal Operator-initiated decision that ends a task
    without a further retry -- a real cancel mid-round, or dismissing
    an escalation without replying with a fix (Phase 16 QA pass: the
    latter never wrote a terminal Postgres row before, so a dismissed
    escalation fell back to the stale "running" Redis flag and showed
    as perpetually in-progress).
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
    """Real perf fix (2026-08-06, the project owner live: raising list_recent_tasks's
    default limit 100 -> 5000 to fix the real "only 200 tasks visible" cap
    turned GET /api/tasks into a 13-21 SECOND response -- confirmed by
    direct timing, not a guess. Root cause: _outcome_row_for_task() opens a
    brand-new Postgres CONNECTION (not just a query) for every single
    standalone task_id, called once per row from a loop that used to run
    ~100 times and now runs into the thousands. This is the batched
    replacement for that hot-path loop specifically: one connection, one
    query for every task_id at once (DISTINCT ON picks the latest row per
    task_id, same "most recent wins" semantics the original LIMIT 1 had).
    _outcome_row_for_task() itself is left untouched for its other,
    genuinely single-task callers (e.g. task_genuinely_passed()).
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
    """Batched sibling of _outcome_rows_for_tasks -- see that function's
    docstring for the real perf story. Same DISTINCT ON / "latest row per
    task_id in one round trip" approach, same real cancellation-tag filter
    as the single-task _cancelled_row_for_task().
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
    checked in real priority order: a pending decision beats everything
    else, then a real terminal outcome, then live Redis task state.
    `status` is always one of the mockup's own five qualitative states
    (pending/running/passed/failed/paused); `section` is empty (caller
    excludes the task) if nothing real is found at all -- e.g. a task
    old enough that its 24h Redis state TTL expired without ever
    completing or being cancelled.

    `prefetched_outcome`/`prefetched_cancelled` (2026-08-06 perf fix): the
    real bulk caller (list_recent_tasks()) now batches these two Postgres
    lookups ONCE for every task_id up front (see _outcome_rows_for_tasks/
    _cancelled_rows_for_tasks) instead of this function opening its own
    fresh connection per call -- that per-call pattern was fine at the old
    100-row default, catastrophic (13-21s responses, confirmed live) once
    the cap was raised to fix the real "only 200 tasks visible" bug. The
    sentinel default ("__unset__", not None) keeps any OTHER caller of this
    function working exactly as before, unbatched -- None is a real,
    meaningful value here (genuinely no outcome/cancellation row exists),
    so it can't double as "not provided."
    """
    outcome_prefetched = prefetched_outcome is not _UNSET
    cancelled_prefetched = prefetched_cancelled is not _UNSET
    # Real, confirmed bug found live (Phase 18 dynamic verification pass):
    # a task hard-killed mid-round (server process killed, not a normal
    # Stop/Dismiss click) gets a real, durable `cancelled_by_operator`
    # decision row written to Postgres afterward to mark it cleanly
    # terminated -- but the Redis `pending_escalation_ids` entry from
    # its last real escalation, written earlier in the same run, is
    # never cleared by a hard kill (only the normal Stop/Dismiss code
    # path clears it). The old check order trusted that stale Redis
    # entry FIRST, so a genuinely, durably terminated task kept showing
    # forever as "Waiting on you" in needs_attention -- confirmed live:
    # task 3248784c had a real cancelled_by_operator row in Postgres (the
    # authoritative, durable fact) but still rendered as paused/pending
    # in the UI's own task list. Fixed generally: a durable Postgres
    # terminal fact (a real outcome, or a real cancelled/dismissed/
    # stuck-call decision) always outranks any Redis flag, since Redis
    # state can go stale on any hard restart, not just this one path --
    # checked first now, before any Redis-based pending/paused state.
    outcome = prefetched_outcome if outcome_prefetched else _outcome_row_for_task(task_id)
    if outcome:
        # Real bug found live (2026-07-12, verifying the scaffold-
        # boilerplate fix on Operator's original 8-constraint service-
        # management task): manager.loop._execute_contract() writes a
        # real, durable `outcome` Postgres row every time ANY round
        # passes -- and manager.loop._run_decomposed_task() reuses the
        # SAME task_id across every one of a multi-constraint task's
        # sub-contracts (by design, so slugify_module_name()/the Gitea
        # branch stay continuous). That means a decomposed task with 8
        # constraints writes up to 8 separate `outcome` rows against one
        # task_id -- one per constraint passing -- and this function's
        # own "a durable Postgres outcome always means COMPLETED" rule
        # (correct for the single-contract case the comment above
        # documents) fired on the FIRST constraint's own intermediate
        # pass, showing the whole task as done while constraints 2-8
        # were still actively running underneath. Confirmed live: Redis
        # `oma:task:<id>:state` said "running" and a fresh trace event
        # was streaming at the exact moment /api/tasks showed this task
        # in the completed/passed bucket.
        #
        # Second, more general bug in the SAME family, found live
        # (2026-07-21, Phase 20 Area 2, an uninterrupted 7-task pass run
        # specifically to get an honest snapshot): the fix above only
        # ever checked `!= STATE_RUNNING`, which is true for EVERY
        # `paused:*` state too (gateway outage, ambiguity, sign-off,
        # repeated failure, round-budget-exhausted, task-cut-off -- see
        # manager/task_state.py's own PAUSE_* constants), not just a
        # genuinely finished task. Confirmed live: task #43's constraint
        # 1 wrote a real `outcome=succeeded` row, then constraint 2 hit a
        # genuine LLM gateway failure mid-round
        # (run_with_gateway_outage_handling() correctly called
        # set_task_state(task_id, PAUSE_GATEWAY_UNAVAILABLE)) -- Redis
        # state became "paused:gateway_unavailable", which is !=
        # STATE_RUNNING, so this check STILL fired and reported the whole
        # task COMPLETED/passed using constraint 1's own stale
        # intermediate outcome row, even though constraint 2 (the actual
        # access-rule requirement) never ran to completion at all --
        # independently confirmed against the live DB: the claimed
        # group/access row genuinely did not exist. Every `paused:*`
        # state below (lines ~160-171) already has its own correct,
        # specific handling -- this early return was making all of them
        # unreachable for any task with a prior outcome row. Fixed to the
        # precise, general condition: an outcome row is only the task's
        # real final word when Redis holds NO state at all for this
        # task_id (either genuinely cleared by clear_task_state() at
        # real completion, per _run_decomposed_task()'s own final step,
        # or expired past its 24h TTL) -- any live Redis value at all,
        # running OR any paused:* variant, means there is still real,
        # unfinished work and this function must fall through to the
        # specific checks below instead of guessing "done" from it.
        if client.get(f"oma:task:{task_id}:state") is None:
            return COMPLETED, "passed", outcome["summary"]

    cancelled = prefetched_cancelled if cancelled_prefetched else _cancelled_row_for_task(task_id)
    if cancelled:
        tags = cancelled.get("tags") or []
        # Real, general bug found live (2026-07-21, live during a Operator
        # demo, task 'warranty.claim'): a `terminated_stuck_call` row
        # (written when an external service restart orphans an in-flight
        # resume -- see ui/chat/server.py's own startup cleanup) is real
        # and durable, but only up until the task is legitimately
        # resumed again. Confirmed live: after restarting the service to
        # clear a stale `oma:resume_lock:*` key (the documented, correct
        # remedy for that specific problem) and firing a fresh
        # POST /api/tasks/{id}/continue, the dashboard kept reporting
        # COMPLETED/failed with this exact OLD message even while the
        # fresh resume was genuinely, actively running (confirmed via
        # direct DB inspection: the most recent durable event was a
        # "resumed" branch_message, no new termination row existed at
        # all).
        #
        # Scoped deliberately narrow to ONLY `terminated_stuck_call`,
        # not every cancelled row: an earlier version of this fix
        # checked Redis "running" for ANY cancellation, which broke the
        # Phase 16 escalation-dismiss regression test -- a genuine
        # Stop/Dismiss deliberately does NOT always clear the Redis
        # "running" flag either (that's the ORIGINAL Phase 18 rationale
        # for trusting Postgres over Redis in the first place), so a
        # leftover "running" flag from BEFORE a real dismiss/cancel must
        # NOT reopen it. `terminated_stuck_call` is different in kind:
        # it is the one termination reason Continue is explicitly
        # designed to resume past, so a FRESH mark_task_running() call
        # after it really is newer, more current evidence -- every other
        # cancellation reason (escalation_dismissed, a genuine Stop) is
        # Operator's own deliberate, final word and stays authoritative
        # regardless of any stale Redis state.
        if "terminated_stuck_call" in tags and client.get(f"oma:task:{task_id}:state") == STATE_RUNNING:
            pass  # a real, fresh resume is active -- fall through to the live-state checks below
        elif "escalation_dismissed" in tags:
            return COMPLETED, "failed", "Dismissed by Operator without a retry."
        elif "terminated_stuck_call" in tags:
            return COMPLETED, "failed", "Stopped -- was stuck on a hung call."
        else:
            return COMPLETED, "failed", "Cancelled by Operator."

    # Phase 17 (§21.5.4): a real, deliberate Pause click is a more
    # recent, more specific fact than the original escalation that
    # preceded it -- checked first among the remaining Redis-only
    # signals, so a paused branch genuinely shows as "Paused," never
    # masked by the still-present pending-escalation entry underneath
    # it (Pause deliberately never clears that entry -- only Stop/
    # Dismiss does).
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

    # Real gap found live (2026-07-14, the project owner watching a just-submitted
    # task stay invisible in the dashboard): task_created is written
    # BEFORE mark_task_running() (manager/loop.py's classification/
    # contract-building happens in between, can take a while, especially
    # under a slower or freshly-swapped model) -- so a genuinely brand-
    # new task has a real task_created row but no Redis signal at all
    # yet, and fell through every check above to full exclusion. That
    # made a task the user just submitted look like it never happened.
    # Distinguish "about to start" from "truly old and stale" by recency
    # instead of excluding unconditionally -- 30 minutes is generous
    # (classification is normally seconds, not that), but the alternative
    # (a task correctly excluded here re-appearing as a false "Starting…"
    # ) is worse than occasionally being slow to fall back to exclusion.
    if created_at is not None:
        age = datetime.datetime.now(created_at.tzinfo) - created_at
        if age < datetime.timedelta(minutes=30):
            return IN_PROGRESS, "pending", "Starting…"
    return "", "", ""  # nothing real found -- caller excludes this task


def _plan_item_status(row: dict) -> tuple[str, str, str]:
    """Returns (section, status, one_liner) -- same three-tuple shape
    as _standalone_task_status(), from active_task_plan_items' own
    real, already-mechanical status column (Phase 15).
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
    """The real aggregation. Returns {"needs_attention": [...],
    "in_progress": [...], "completed": [...]}, each item shaped as
    {task_id, title, one_liner, kind: "standalone"|"plan_item",
    plan_id, plan_item_id, blocked_by}.

    `since_ts`, added 2026-07-14 (the project owner, live: the dashboard mixing
    days-old real project history -- the service-management module,
    quality audits, dashboard-regression test fixtures -- in with a
    freshly-started task batch made the UI look "stuck" even though new
    tasks genuinely were appearing underneath the pile). Purely a VIEW
    filter, non-destructive -- nothing in Postgres is touched, so no
    real project history is lost, just hidden from a given call's
    result when the caller wants "what's happening now" rather than
    the full archive.
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
            # UI redesign 2026-08-06: additive fields only, nothing above removed/renamed.
            # `title` for a plan_item is an AI-narrowed per-round restatement (manager/loop.py's
            # "This round's own NEW focus is ONLY: ..." rewrite), never the user's own words --
            # `original_goal` (set once, carried forward unchanged, see contracts/schema.py's own
            # field docstring) is the real, un-narrowed text the user actually typed, so the UI can
            # finally label "what you asked" vs "what the AI is focused on this round" instead of
            # showing one unlabeled AI rewrite as if it were the user's own goal.
            "original_goal": contract_detail.get("original_goal") or contract_detail.get("goal", ""),
            "goal_kind": "ai_narrowed",
            "one_liner": one_liner,
            "status": status,
            "blocked_by": contract_detail.get("blocked_by", []),
        })
        if task_id:
            seen_task_ids.add(task_id)

    # Real perf fix (2026-08-06): two passes over the same rows instead of
    # one, specifically so the two expensive Postgres lookups can be
    # batched ONCE for every standalone task_id up front (see
    # _outcome_rows_for_tasks' own docstring for the real 13-21s-response
    # story this fixes) rather than _standalone_task_status opening a
    # fresh connection per task_id, per row, in a single combined loop.
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
            # UI redesign 2026-08-06: a standalone task was never decomposed, so `title` already
            # IS the user's own verbatim text -- no separate original_goal to recover, but the
            # field is still set (mirroring itself) so the frontend has one consistent contract
            # across both task kinds instead of needing a kind-specific branch to know whether
            # `original_goal` will be present.
            "original_goal": row["summary"],
            "goal_kind": "verbatim",
            "one_liner": one_liner,
            "status": status,
            "blocked_by": [],
        })

    return sections


def get_original_task(task_id: str) -> dict | None:
    """Phase 16 (replay): the real original goal text AND contract.inputs
    for a task_id -- whether it was a standalone task (task_created's
    own summary/detail.inputs) or an outer-plan item (plan_item_status's
    own detail.contract.goal/inputs). None if no real record of this
    task_id exists at all. Both fields matter for a real replay: goal
    alone isn't enough to reproduce a task's real, original runnability
    (e.g. a Code-Review target named via contract_inputs) -- found live,
    via a real replay test failing without inputs carried forward too.
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
    """Phase 28B (2026-07-28): the exact "only act on a genuinely
    confirmed-complete result" gate the zip-delivery endpoint needs --
    reuses the same two real, durable facts `_standalone_task_status()`
    above already established as the only trustworthy signal for
    COMPLETED/"passed" (a real Postgres `outcome` row with
    `detail.passed=True`, AND no live Redis state left for this
    task_id -- any live state, running or any `paused:*` variant, means
    real work is still outstanding, per that function's own second,
    more general bug-fix). Deliberately does NOT reuse
    `_standalone_task_status()` itself: that function also branches on
    pending sign-off/escalation ids and outer-plan-item status, none of
    which this narrower "is it safe to hand back a real zip" question
    needs.
    """
    outcome = _outcome_row_for_task(task_id)
    if not outcome or not (outcome.get("detail") or {}).get("passed"):
        return False
    client = get_redis_client()
    return client.get(f"oma:task:{task_id}:state") is None
