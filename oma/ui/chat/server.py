"""Phase 12: the actual chat interface Operator will use -- a minimal
FastAPI backend wrapping manager.loop.run_turn()/resume_after_sign_off(),
plus a single static page (index.html) implementing the three UX
patterns the build plan names: a plain chat window (step 1), a
pending-approvals view for tier-3/4 sign-offs and proposed-rule
confirmations (step 2), and confidence-card rendering for anything
needing a decision (step 3). Deliberately minimal beyond that, per the
plan's own step 4 -- no diff rendering, no full dashboard, until the
core loop and specialists are proven.

Session state (conversation history) is held in memory, keyed by a
plain session_id -- there is no multi-user auth system yet; this is
the first real UI for a single-user (Operator) demo, not a hardening pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import select
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from contextlib import asynccontextmanager

import psycopg2
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from infra.gateway_client import ModelGatewayClient
from infra.redis_client import get_redis_client
from infra.settings import load_postgres_settings, load_redis_settings
from manager.ambiguity_digest import format_ambiguity_digest_line, list_pending_ambiguity_clusters
from manager.correction import confirm_proposed_rule, list_pending_proposed_rules, reject_proposed_rule
from manager.escalations import clear_pending_escalation, list_pending_escalations
from manager.loop import (
    _RESUME_LOCK_RENEW_INTERVAL_SEC,
    _RESUME_LOCK_TTL_SEC,
    resume_after_sign_off,
    resume_orphaned_task_at_startup,
    resume_task_after_checkpoint,
    run_turn,
)
from manager.replanning import get_latest_resume_point, list_branch_messages, list_rounds_for_task
from manager.sign_off import get_pending_contract, list_pending_task_ids
from manager.dashboard import get_original_task, list_recent_tasks, task_genuinely_passed
from manager.task_plan import list_plan_items
from manager.task_state import (
    PAUSE_ROUND_BUDGET_EXHAUSTED,
    STATE_RUNNING,
    clear_task_state,
    request_cancel,
    set_task_state,
)
from manager.tools import append_project_memory, read_main_chat_history
from manager.trace import channel_name, publish_trace_event
from scripts.bootstrap_specialists import register_default_specialists

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")
MANAGER_MODEL = os.environ.get("OMA_MODEL_MANAGER", "qwen3.6-27b")
_SSE_IDLE_TIMEOUT_SECONDS = 300

# 2026-08-02: no module in this app previously called logging.basicConfig(), so any plain
# logger.info() call anywhere in the codebase (e.g. infra.cloud_escalation's own real-cloud-call
# decision logging) silently went nowhere -- the root logger had no handler, and Python's
# lastResort handler only surfaces WARNING+. Needed for real observability of the cloud-escalation
# go-live verification; keeps INFO-level app logs genuinely visible in journalctl going forward.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

logger = logging.getLogger(__name__)


async def _resume_orphan_in_background(task_id: str, redis_client, lock_key: str) -> None:
    """Phase 30, P2 (§9): runs a real, potentially multi-minute resumed
    round loop in the background so the server can start accepting new
    requests immediately rather than blocking startup on every orphaned
    task's resume. Falls back to the same discard/terminate handling the
    pre-P2 code used only when the resume itself reports genuinely
    nothing resumable (e.g. the checked-out commit no longer compiles) --
    never leaves a task silently stuck at STATE_RUNNING forever.

    Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): this lock is acquired by the caller (see the startup sweep
    above) with a fixed, long TTL and released only in this function's own `finally` block --
    but this whole SERVICE (not just this one background task) can itself be restarted or
    crash while this coroutine is mid-flight, which never lets any Python `finally` run, and
    the very next startup's own stale-lock sweep would otherwise be the only thing to notice.
    Same fix as `manager.loop.resume_task_after_checkpoint()`'s own sibling lock (shares its
    constants rather than duplicating the tuning): a short, actively-renewed lease -- alive for
    as long as this resume genuinely is, self-healing quickly if it isn't.
    """
    async def _renew_lock_while_alive() -> None:
        while True:
            await asyncio.sleep(_RESUME_LOCK_RENEW_INTERVAL_SEC)
            redis_client.expire(lock_key, _RESUME_LOCK_TTL_SEC)

    renew_task = asyncio.create_task(_renew_lock_while_alive())
    try:
        result = await resume_orphaned_task_at_startup(
            task_id, _client, CLASSIFIER_MODEL, manager_model=MANAGER_MODEL,
        )
        if result.get("status") == "not_resumable":
            clear_task_state(task_id, client=redis_client)
            clear_pending_escalation(task_id, client=redis_client)
            append_project_memory(
                event_type="decision", actor="manager", task_id=task_id, module=None,
                summary=f"Stopped -- was stuck on a hung call: {result.get('message', '')}",
                tags=["cancelled_by_operator", "terminated_stuck_call"],
                detail={"reconciled_at_startup": True, "resume_attempted": True},
                verified=True,
            )
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "task",
                "message": result.get("message", "Stopped -- nothing resumable."), "status": "failed",
            })
    except Exception:
        logger.exception("Startup resume of orphaned task %s crashed", task_id)
        clear_task_state(task_id, client=redis_client)
        clear_pending_escalation(task_id, client=redis_client)
        append_project_memory(
            event_type="decision", actor="manager", task_id=task_id, module=None,
            summary="Stopped -- automatic resume after restart itself crashed; giving up on this task.",
            tags=["cancelled_by_operator", "terminated_stuck_call"],
            detail={"reconciled_at_startup": True, "resume_attempted": True, "resume_crashed": True},
            verified=True,
        )
    finally:
        renew_task.cancel()
        redis_client.delete(lock_key)

_sessions: dict[str, list[dict]] = {}
_client: ModelGatewayClient | None = None
# Set once in lifespan(), used as /api/tasks' default cutoff -- see there.
_server_start_ts: datetime | None = None

# 2026-07-20, UPDATE 20 -- see _task_has_recent_progress()'s own
# docstring. Generous relative to a single trace event's own cadence
# (LLM deltas/round transitions land every few seconds during real
# work) but well under the shortest realistic "genuinely dead" gap.
_RECENT_PROGRESS_THRESHOLD_SECONDS = 120


def _task_has_recent_progress(task_id: str, client) -> bool:
    """Real, general fix (2026-07-20, Phase 20 Area 2 UPDATE 20): the
    startup reconciliation sweep below (lifespan()) exists to catch a
    task genuinely orphaned by a killed process -- but was found live
    marking task #50 "orphaned by an external restart" while its own
    round was still actively producing real trace events, 2 minutes
    before it reached a real result. The exact trigger was never
    root-caused (this restart's own sweep, moments earlier, found zero
    stale keys -- confirmed via journalctl), so this is a defensive
    guard against ANY trigger of this shape, matching 2026 distributed-
    systems best practice for false-positive "orphan" reconciliation:
    never trust a one-shot sweep's verdict over direct, recent evidence
    of life -- a heartbeat/last-progress check, not sweep-timing.

    Reads the LAST entry of oma:trace_history:{task_id} (a Redis LIST,
    RPUSH-appended by manager.trace.publish_trace_event -- the same
    list /api/trace_history/{task_id} already serves) and compares its
    own "ts" field against wall-clock now. Conservative like every
    other check in this codebase: any ambiguity (no history yet, an
    unparsable entry, a missing "ts") is treated as "no recent
    progress found" -- i.e. falls through to the existing reconcile
    behavior rather than ever accidentally protecting a genuinely dead
    task from cleanup.
    """
    try:
        raw = client.lindex(f"oma:trace_history:{task_id}", -1)
    except Exception:
        return False
    if not raw:
        return False
    try:
        payload = json.loads(raw)
        ts = payload.get("ts")
    except (json.JSONDecodeError, AttributeError):
        return False
    if not isinstance(ts, (int, float)):
        return False
    return (time.time() - ts) < _RECENT_PROGRESS_THRESHOLD_SECONDS


# ============================= real-time push, Postgres side ==============
# 2026-08-06 (the project owner: "it should be milliseconds, not seconds -- go find
# and implement the best real approach"). GET /api/stream's SUBSCRIBE-only
# workaround (below) closes the gap for tasks it already knows are active,
# but a genuinely BRAND NEW task can't be subscribed to before it exists --
# that gap was bounded by a periodic re-scan (seconds), not truly instant.
# This closes it for real: a Postgres trigger (oma_agent_memory_events_notify,
# applied directly via psql, not app-managed DDL -- see the chat response
# this shipped with) fires pg_notify('oma_events', task_id) on every INSERT
# into agent_memory_events -- the exact table every real durable write this
# whole dashboard's status buckets are computed from already goes through
# (task_created, outcome, decision), including a task's very first moment
# of existing. Measured directly with psycopg2's own LISTEN/NOTIFY,
# insert-to-notify was 0.0000s (sub-millisecond) on this connection -- see
# the chat response for the full research (Postgres LISTEN/NOTIFY is
# well-established at sub-ms-to-low-tens-of-ms for this exact scale:
# a small number of listeners, not thousands).
#
# psycopg2 has no native asyncio support, so this runs the LISTEN loop in
# one dedicated background thread (started once in lifespan(), stopped
# cleanly on shutdown) and hands notifications into the asyncio world via
# loop.call_soon_threadsafe -- the standard, well-established pattern for
# bridging a blocking LISTEN loop into an async app. Every currently-open
# GET /api/stream connection registers its own asyncio.Queue here; the
# listener thread broadcasts to all of them. This runs ALONGSIDE the
# existing Redis trace-subscribe mechanism, not instead of it -- Postgres
# NOTIFY covers durable milestones (new task, terminal outcome, a real
# decision), Redis trace events still carry the more granular live-
# progress signal (round starting, phase changing) that doesn't always
# have its own agent_memory_events row. Both feed the same per-connection
# queue, so the browser sees one unified stream regardless of source.
_sse_broadcast_queues: set[asyncio.Queue] = set()
_pg_listen_stop = threading.Event()


def _pg_listen_thread_main(loop: asyncio.AbstractEventLoop) -> None:
    s = load_postgres_settings()
    while not _pg_listen_stop.is_set():
        conn = None
        try:
            conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute("LISTEN oma_events;")
            while not _pg_listen_stop.is_set():
                if select.select([conn], [], [], 5.0) == ([], [], []):
                    continue  # plain timeout, loop back around to check the stop flag
                conn.poll()
                while conn.notifies:
                    n = conn.notifies.pop(0)
                    task_id = n.payload or None
                    for q in list(_sse_broadcast_queues):
                        loop.call_soon_threadsafe(q.put_nowait, task_id)
        except Exception:
            logging.getLogger(__name__).warning(
                "pg LISTEN thread lost its connection (non-fatal, retrying in 3s)", exc_info=True,
            )
            _pg_listen_stop.wait(3.0)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _server_start_ts
    _server_start_ts = datetime.now(timezone.utc)
    _client = ModelGatewayClient()
    # Phase 35 §17.4.3 / §18.6: the gate/validator/orchestrator source tamper-protection check --
    # local hash-pin detection only (see manager/gate_tamper_protection.py's own docstring for
    # the real, disclosed limitation without the external-authority half). Detect-only at
    # startup, never blocks: a real finding is logged loudly (the same [STARTUP: ...] convention
    # this function already uses) but the service starts regardless -- a hard startup block here
    # would itself be a new, hasty enforcement decision this rollout stage deliberately avoids.
    try:
        from manager.gate_tamper_protection import check_for_tampering

        tamper_findings = check_for_tampering()
        if tamper_findings:
            print(f"[STARTUP WARNING: gate-source tamper check found {len(tamper_findings)} "
                  f"finding(s) -- {[(f.relpath, f.kind) for f in tamper_findings]}. This is "
                  f"detect-only (§17.11 log_only rollout); the service is starting regardless. "
                  f"If this is a legitimate, authorized change, re-pin the manifest via "
                  f"manager.gate_tamper_protection.write_manifest(compute_manifest()).]")
    except Exception as exc:  # noqa: BLE001 -- detect-only, must never prevent the service starting
        print(f"[STARTUP NOTE: gate-source tamper check itself failed to run: {exc!r}]")
    real = register_default_specialists(_client)
    if not real:
        print("[NOTE: OMA_ODOO_DB_DUPLICATE_FOR_BUILD not set -- bug_fix/testing_qa use the DEMO "
              "fake specialist; code_review is registered for real regardless.]")
    # Real bug hit twice live (2026-07-11): manager.loop.resume_task_after_checkpoint()'s
    # oma:resume_lock:{task_id} (Redis SET NX, a short heartbeat-renewed lease as of the
    # 2026-08-09 fix -- see that function's own comment) is only ever released
    # in its own `finally` block -- a service restart while a /continue call
    # is genuinely in flight kills the process before that finally can run,
    # leaving a stale lock that makes every future Continue on that task
    # fail with "already being resumed" for up to an hour, even though
    # nothing is actually resuming it (this process just started). A fresh
    # process can never have a legitimate in-flight resume of its own, so
    # clearing every oma:resume_lock:* key on startup is always correct,
    # never destructive -- unlike other oma:* state (pending escalations,
    # module locks), these locks exist ONLY to serialize concurrent callers
    # within a single process's lifetime and carry no meaning across a
    # restart.
    redis_client = get_redis_client()
    stale_locks = list(redis_client.scan_iter("oma:resume_lock:*"))
    if stale_locks:
        redis_client.delete(*stale_locks)
        print(f"[STARTUP: cleared {len(stale_locks)} stale oma:resume_lock:* key(s) left over from a prior process]")
    # Same reasoning, extended (2026-07-14): manager.loop._execute_contract()'s
    # own `except Exception` fix (added earlier this session) only catches
    # IN-PROCESS exceptions -- it cannot run at all if the whole process is
    # killed externally (confirmed live: this host got restarted three times
    # in one afternoon by a hypervisor-level VM snapshot/restore, orphaning
    # task f886398b mid-round with oma:task:<id>:state stuck at STATE_RUNNING
    # forever, exactly like the in-process case, just via a different cause).
    # A fresh process can never have a legitimate task actually running from
    # its own perspective, so reconciling every stale "running" state at
    # startup is always correct here too -- same logic as the resume-lock
    # cleanup above, just for a second class of leftover state.
    stale_task_keys = list(redis_client.scan_iter("oma:task:*:state"))
    reconciled = 0
    resumed = 0
    for key in stale_task_keys:
        if redis_client.get(key) != STATE_RUNNING:
            continue
        task_id = key.split(":")[2]
        # Real, general fix (2026-07-20, UPDATE 20): task #50 was
        # spuriously marked "orphaned by an external restart" by this
        # exact block while its own round was still genuinely
        # producing real trace events -- the write happened despite
        # this restart's own reconciliation having already found ZERO
        # stale keys moments earlier (confirmed via journalctl), so the
        # exact trigger was never root-caused. Rather than leave this
        # block able to fire against a task that's actively making
        # real progress (whatever the trigger turns out to be), guard
        # it with the SAME pattern 2026 distributed-systems practice
        # converges on for this exact false-positive-orphan shape:
        # never trust a one-shot sweep's verdict over recent, real
        # evidence of life. A task whose own most recent trace event
        # landed within the last _RECENT_PROGRESS_THRESHOLD_SECONDS
        # cannot be a genuine restart-orphan -- skip it this sweep
        # (Redis state stays "running"; a still-genuinely-dead task
        # gets caught on the NEXT sweep once it truly goes quiet).
        if _task_has_recent_progress(task_id, redis_client):
            continue
        # Phase 30, P2 (§9, closes Problem F): before this fix, EVERY
        # orphaned task fell straight into the discard branch below --
        # 271 of 748 real tasks (36%) lost all round progress to a
        # restart, the single largest raw number found in the whole
        # Phase 30 investigation. Try a real resume from the task's last
        # durable state first (round_checkpoint if it escalated, the
        # latest replan_round otherwise -- get_latest_resume_point()
        # covers both real shapes, confirmed against the actual
        # historical data); only fall through to the discard/terminate
        # path below when genuinely nothing durable exists to resume
        # from (still on a never-yet-revised first round when killed).
        if get_latest_resume_point(task_id) is not None:
            lock_key = f"oma:resume_lock:{task_id}"
            if redis_client.set(lock_key, "1", nx=True, ex=_RESUME_LOCK_TTL_SEC):
                asyncio.create_task(_resume_orphan_in_background(task_id, redis_client, lock_key))
                resumed += 1
                continue
        clear_task_state(task_id, client=redis_client)
        # Found live (2026-07-14): a task can be both mid-round AND already
        # holding a pending_escalation from an earlier round's own genuine
        # escalation -- clearing only the running-state left a stale
        # duplicate escalation entry behind (had to be cleaned by hand once).
        # Always clear both so the dashboard never shows a task that's
        # simultaneously "resolved/failed" (Postgres) and "still needs
        # Operator's attention" (Redis).
        clear_pending_escalation(task_id, client=redis_client)
        append_project_memory(
            event_type="decision", actor="manager", task_id=task_id, module=None,
            summary="Stopped -- was stuck on a hung call: orphaned by an external service restart, "
                    "cleaned up automatically at the next startup.",
            tags=["cancelled_by_operator", "terminated_stuck_call"],
            detail={"reconciled_at_startup": True},
            verified=True,
        )
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": "Stopped -- was stuck on a hung call (orphaned by an external restart, "
                       "cleaned up at startup).",
            "status": "failed",
        })
        reconciled += 1
    if resumed:
        print(f"[STARTUP: resuming {resumed} orphaned task(s) from their last durable checkpoint/round instead of discarding]")
    if reconciled:
        print(f"[STARTUP: reconciled {reconciled} stale oma:task:*:state=running key(s) left over from a prior process -- nothing durable to resume from]")

    # Real-time push, Postgres side (2026-08-06) -- see _pg_listen_thread_main's
    # own module-level comment for the full story. One background thread for
    # the whole process's lifetime, started here, stopped cleanly below.
    _pg_listen_stop.clear()
    pg_listen_thread = threading.Thread(
        target=_pg_listen_thread_main, args=(asyncio.get_running_loop(),),
        name="pg-listen-oma-events", daemon=True,
    )
    pg_listen_thread.start()

    # Real, confirmed gap found live (2026-08-17): manager/gateway_orchestration.py's own
    # GatewayOutagePause message promises "I'll resume automatically once the gateway is back"
    # for every gateway/repetition-loop pause, but nothing in this codebase ever actually did
    # that until now -- see manager/gateway_auto_resume.py's own module docstring for the full
    # incident (6 of 9 real `record_rule_row_level_security` attempts hit
    # LLMRepetitionLoopExhaustedError one overnight run, none of which retried on their own).
    # One background task for the whole process's lifetime, same convention as the pg-listen
    # thread above -- started here, cancelled cleanly below.
    from manager.gateway_auto_resume import run_auto_resume_loop

    gateway_auto_resume_task = asyncio.create_task(
        run_auto_resume_loop(_client, CLASSIFIER_MODEL, manager_model=MANAGER_MODEL)
    )

    try:
        yield
    finally:
        gateway_auto_resume_task.cancel()
        _pg_listen_stop.set()
        pg_listen_thread.join(timeout=6.0)
        await _client.aclose()


app = FastAPI(title="Odoo Manager Agent -- Chat UI (Phase 12)", lifespan=lifespan)


class MessageRequest(BaseModel):
    session_id: str = "default"
    message: str
    # Optional -- real scope-extraction from free text doesn't exist yet
    # (a documented gap since Phase 6); exposed here so a sensitive-scope
    # task can still be tested/triggered deliberately through the real
    # chat UI rather than only through a direct loop-level test.
    anticipated_scope: dict | None = None


class SignOffRequest(BaseModel):
    approved: bool


class ProposedRuleRequest(BaseModel):
    approved: bool


class ContinueRequest(BaseModel):
    # Phase 17 (§21.5.4): the one field Operator can add at the moment of
    # clicking Continue itself, in addition to (not instead of) any
    # clarifications he already wrote and stored beforehand while the
    # task sat waiting.
    note: str | None = None


@app.get("/")
async def index():
    # Found live (2026-07-14, the project owner reporting the dashboard "not changing
    # at all" despite real backend state changes underneath): FileResponse
    # sends no explicit Cache-Control, so a browser can serve this page
    # from its own disk cache on a plain reload without ever re-checking
    # the server -- the live JS polling loop inside an already-cached-and-
    # unchanged page still runs fine, but if the user believes a hard
    # reload is needed and the browser silently serves the cached HTML
    # instead of re-fetching, they never actually see whatever changed
    # since that page was first cached. Force no caching so every load is
    # always the real, current file.
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/api/message_history")
async def get_message_history(session_id: str = "default"):
    """Real, confirmed gap found live (the project owner's own report: main chat
    messages "disappear... when reload"): _sessions only ever lived in
    server RAM, with no way for the frontend to ask for it back after a
    reload, and no way to survive a server restart either. Reads the
    same durable main_chat_message rows post_message() now writes.
    """
    return JSONResponse(jsonable_encoder({"messages": read_main_chat_history(session_id)}))


@app.post("/api/message")
async def post_message(req: MessageRequest):
    # Real fix alongside the read-back above: rehydrate from Postgres
    # the first time this session_id is seen in this process (covers a
    # server restart, not just a page reload -- _sessions itself is
    # still the fast, in-memory path for the REST of a live process's
    # own life, this only ever fires once per session per process).
    if req.session_id not in _sessions:
        _sessions[req.session_id] = [
            {"role": m["role"], "content": m["content"]} for m in read_main_chat_history(req.session_id)
        ]
    history = _sessions[req.session_id]
    history.append({"role": "user", "content": req.message})
    append_project_memory(
        event_type="main_chat_message", actor="operator", task_id=None, module=None,
        summary=req.message[:300], tags=["main_chat"],
        detail={"session_id": req.session_id, "role": "user", "content": req.message, "task_id": None},
        verified=True,
    )

    result = await run_turn(
        req.message, _client, CLASSIFIER_MODEL,
        conversation_history=history, anticipated_scope=req.anticipated_scope,
        manager_model=MANAGER_MODEL,
    )

    reply_text = _render_reply(result)
    history.append({"role": "assistant", "content": reply_text})
    append_project_memory(
        event_type="main_chat_message", actor="manager", task_id=None, module=None,
        summary=reply_text[:300], tags=["main_chat"],
        detail={
            "session_id": req.session_id, "role": "assistant", "content": reply_text,
            "task_id": result.get("task_id"),
        },
        verified=True,
    )

    return JSONResponse(jsonable_encoder({"reply": reply_text, "result": result}))


@app.get("/api/pending")
async def get_pending():
    """The pending-items queue -- every tier-3/4 sign-off and every
    proposed-rule confirmation currently awaiting Operator's decision, in
    one place, per the build plan's own "one simple queue" instruction.
    """
    sign_offs = []
    for task_id in list_pending_task_ids():
        payload = get_pending_contract(task_id)
        if payload is None:
            continue
        contract = payload["contract"]
        sign_offs.append(
            {
                "task_id": task_id,
                "kind": "sign_off",
                "action": contract["goal"],
                "reasoning": f"tier {contract['tier']} ({contract['capability_class']}) -- "
                             f"touches something requiring sign-off before proceeding",
                "confidence": "n/a (mechanical tier gate, not a model judgment)",
                "tier": contract["tier"],
            }
        )

    proposed_rules = []
    for row in list_pending_proposed_rules():
        detail = row.get("detail") or {}
        proposed_rules.append(
            {
                "row_id": row["id"],
                "kind": "proposed_rule",
                "action": detail.get("directive", row["summary"]),
                "reasoning": f"applies when: {detail.get('applicability_condition', 'unspecified')}",
                "confidence": detail.get("confidence", "n/a"),
            }
        )

    # Phase 15 (§19.8): a PauseForOperator escalation gets the same
    # confidence-card treatment, with the full round trace attached so
    # Operator can see exactly what was already tried before being asked.
    escalations = []
    for esc in list_pending_escalations():
        escalations.append(
            {
                "task_id": esc["task_id"],
                "kind": "escalation",
                "action": f"Task {esc['task_id']} needs your input",
                "reasoning": esc["message"],
                "confidence": f"{len(esc['prior_rounds'])} round(s) already tried",
                "prior_rounds": esc["prior_rounds"],
            }
        )

    # Phase U (§25.5): every currently-open ambiguity cluster -- many parallel tasks hitting the
    # same underlying category of ambiguity collapse to one queue item here (count + affected
    # task_ids), not N separate identical entries.
    ambiguity_clusters = []
    for cluster in list_pending_ambiguity_clusters():
        ambiguity_clusters.append(
            {
                "signature": cluster["signature"],
                "kind": "ambiguity_cluster",
                "action": format_ambiguity_digest_line(cluster),
                "reasoning": cluster.get("reasoning", ""),
                "blocking_questions": cluster["blocking_questions"],
                "task_ids": cluster["task_ids"],
                "count": cluster["count"],
                "sample_goal": cluster.get("sample_goal", ""),
            }
        )

    return JSONResponse(jsonable_encoder({
        "sign_offs": sign_offs,
        "proposed_rules": proposed_rules,
        "escalations": escalations,
        "ambiguity_clusters": ambiguity_clusters,
    }))


@app.get("/api/tasks")
async def get_tasks(all_history: bool = False):
    """Phase 16 (mockup's Task Plan panel): every recent task, standalone
    and outer-plan items alike, grouped into needs_attention/in_progress/
    completed -- a real, new query over agent_memory_events + Redis
    pending state (manager/dashboard.py), not the single-plan
    GET /api/plan/{plan_id} repurposed.

    Defaults to this server process's own start time, added 2026-07-14
    (revised same day after a calendar-day cutoff proved too coarse --
    the project owner's whole batch-restart cycle happened within one UTC day, so
    "today" still buried a fresh restart under that same day's earlier
    attempts). Every deliberate restart is exactly the "clear it, start
    fresh" moment the project owner keeps asking for, so using process-start as the
    cutoff means a restart always yields a genuinely blank dashboard
    going forward -- purely a VIEW filter, nothing in Postgres is
    touched. Pass ?all_history=true to see the full archive.
    """
    since_ts = None if all_history else _server_start_ts
    return JSONResponse(jsonable_encoder(list_recent_tasks(since_ts=since_ts)))


@app.get("/api/plan/{plan_id}")
async def get_plan(plan_id: str):
    """Phase 15 (§19.8): the outer plan as a literal checklist --
    reads active_task_plan_items, returns each item's status and its
    blocked_by list.
    """
    items = list_plan_items(plan_id)
    return JSONResponse(jsonable_encoder({
        "plan_id": plan_id,
        "items": [
            {
                "plan_item_id": item["plan_item_id"],
                "status": item["status"],
                "blocked_by": (item.get("detail") or {}).get("contract", {}).get("blocked_by", []),
                "goal": (item.get("detail") or {}).get("contract", {}).get("goal", ""),
                # UI redesign 2026-08-06, additive: the real un-narrowed goal the user typed,
                # same field/rationale as manager/dashboard.py's list_recent_tasks() -- see that
                # file's own comment for why this exists alongside (never replacing) `goal`.
                "original_goal": (
                    (item.get("detail") or {}).get("contract", {}).get("original_goal")
                    or (item.get("detail") or {}).get("contract", {}).get("goal", "")
                ),
            }
            for item in items
        ],
    }))


@app.get("/api/rounds/{task_id}")
async def get_rounds(task_id: str):
    """Phase 15 (§19.8): the round trace for a single task -- every
    replan_round event in order, rendered as a collapsed-by-default
    trace under that task's entry in the chat: round 1 failed because
    X, revised to Y, round 2 failed because Z, round 3 passed.
    """
    rounds = list_rounds_for_task(task_id)
    return JSONResponse(jsonable_encoder({"task_id": task_id, "rounds": rounds}))


@app.get("/api/tasks/{task_id}/module.zip")
async def get_task_module_zip(task_id: str):
    """Phase 28B (2026-07-28): `school_student`'s own explicit "Delivery:
    Full module as a zip file" requirement. Deliberately built on-demand
    from Gitea's own real, committed branch state (`build_task_module_
    zip_bytes()`, `tools_odoo/module_dev/vcs.py`), not a separately
    written-to-disk artifact -- there is then nothing new to clean up,
    no risk of serving a stale copy after a later round genuinely
    changed the module, and the zip always reflects the exact same
    durable source of truth Build/Code-Review themselves reason from.

    Gated on `task_genuinely_passed()` (manager/dashboard.py) -- a real,
    durable Postgres `outcome` row with `detail.passed=True` and no
    live Redis state left for this task_id -- so an in-progress, failed,
    or paused task can never hand back a zip that looks like a real,
    complete deliverable but isn't (matching this whole project's
    "only act on a genuinely confirmed-complete result" discipline).
    404, not a silent empty archive, for either "task isn't genuinely
    complete yet" or "nothing was ever actually committed."
    """
    if not task_genuinely_passed(task_id):
        raise HTTPException(status_code=404, detail="task is not genuinely, durably complete yet")
    from tools_odoo.module_dev.vcs import build_task_module_zip_bytes

    zip_bytes = build_task_module_zip_bytes(task_id)
    if zip_bytes is None:
        raise HTTPException(status_code=404, detail="no module was ever committed for this task")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{task_id}.zip"'},
    )


@app.get("/api/tasks/{task_id}/branch_messages")
async def get_branch_messages(task_id: str):
    """Phase 17 (§21.7): a branch's own real conversation log -- Operator's
    clarifications, the Manager's acknowledgments, escalation summaries,
    success digests -- rendered as the mini-chat, separate from (but
    alongside) the existing round trace above.
    """
    messages = list_branch_messages(task_id)
    return JSONResponse(jsonable_encoder({"task_id": task_id, "messages": messages}))


@app.get("/api/trace_history/{task_id}")
async def get_trace_history(task_id: str):
    """Fetch the cached trace history list from Redis for a task."""
    s = load_redis_settings()
    r = aioredis.Redis(
        host=s.host, port=s.port, username=s.username, password=s.password,
        db=s.db, decode_responses=True,
    )
    try:
        events_raw = await r.lrange(f"oma:trace_history:{task_id}", 0, -1)
        events = [json.loads(e) for e in events_raw]
        return JSONResponse(jsonable_encoder({"task_id": task_id, "events": events}))
    except Exception:
        return JSONResponse(jsonable_encoder({"task_id": task_id, "events": []}))


@app.get("/api/stream/{task_id}")
async def stream_task(task_id: str):
    """Phase 16 (§20.4): subscribes to oma:trace:{task_id}, forwards
    each real trace event as an SSE frame. Closes cleanly on a real
    terminal event (phase='task', status in passed/failed) for this
    task's own top-level Manager phase, or after a generous idle
    timeout as a backstop -- never a fake heartbeat, never synthetic
    events; if nothing real is published, the stream simply idles until
    the timeout, exactly reflecting reality.

    Real bug found live (2026-08-10, the project owner's own direct report: resuming a task from the
    backend "worked perfectly" but the UI "just isn't updating... only appearing when I hard
    refresh"). `status == "paused"` used to ALSO close this stream immediately -- wrong: unlike a
    genuine passed/failed conclusion, a paused task is explicitly expected to receive MORE real
    events later (the whole point of a pause is that Operator -- or an automated resume -- may
    continue it). Closing the connection the instant a pause fires forces the browser's
    EventSource to reconnect on its own schedule; if the resume happens before that reconnect
    completes, every one of its real live events (node_state_changed, node_round_advanced,
    node_specialist_changed, the token-by-token deltas) gets published to this task's Redis
    channel while genuinely nobody is subscribed to receive it -- Redis pub/sub has no replay/
    queueing for a not-yet-subscribed listener, so those events are gone for that browser tab
    forever, not just delayed. The tab is then stuck showing the pre-pause state until a hard
    reload opens a brand-new connection and pulls current state via a REST resync. The existing
    300s idle-timeout backstop below still closes a genuinely abandoned paused-task stream
    naturally -- just without the race, since 300s is enormously more headroom than any real
    resume-click-to-event gap.
    """
    async def event_generator():
        s = load_redis_settings()
        r = aioredis.Redis(
            host=s.host, port=s.port, username=s.username, password=s.password,
            db=s.db, decode_responses=True,
        )
        pubsub = r.pubsub()
        last_activity = time.monotonic()
        try:
            await pubsub.subscribe(channel_name(task_id))
            while True:
                if time.monotonic() - last_activity > _SSE_IDLE_TIMEOUT_SECONDS:
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    await asyncio.sleep(0)  # yield control, keep polling
                    continue
                last_activity = time.monotonic()
                data = message["data"]
                yield f"data: {data}\n\n"
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    event = {}
                if event.get("phase") == "task" and event.get("status") in ("passed", "failed"):
                    break
        except Exception as exc:
            # Real, honest failure -- e.g. the Redis ACL user genuinely
            # lacks SUBSCRIBE on oma:* channels (redis-asyncio defers
            # surfacing this until the first get_message() read, not at
            # subscribe() itself). One informative event, then close
            # cleanly, rather than crashing the whole HTTP response with
            # an unhandled exception.
            yield f"data: {json.dumps({'level': 'manager', 'phase': 'stream', 'status': 'failed', 'message': f'live stream unavailable: {exc}'})}\n\n"
        finally:
            try:
                await pubsub.unsubscribe(channel_name(task_id))
            except Exception:
                pass
            await pubsub.aclose()
            await r.aclose()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/stream")
async def stream_all():
    """2026-08-06 (the project owner, live: 'everything should update instantly,
    not every 6 seconds' -- researched against real 2026 practice: SSE,
    not WebSockets, is the right transport here specifically because
    every real update in this system flows server-to-client only [status
    changes, new messages, round completions] -- the client's own actions
    [Stop/Continue/Dismiss, sending a goal] are already ordinary REST
    POSTs and don't need a duplex socket. See the chat response this
    shipped with for full sourcing).

    This is the dashboard-wide sibling of GET /api/stream/{task_id} above
    -- same real transport, same real Redis Pub/Sub channel family
    (oma:trace:{task_id}). Deliberately reuses the exact channel every one
    of manager/loop.py's many real publish_trace_event() call sites
    (round start, phase change, escalation, terminal outcome, etc.)
    already writes to for the per-task live view -- zero changes needed
    to any of those call sites, they already fire at exactly the moments
    that matter.

    Real constraint found live (2026-08-06): the intended design here was
    one PSUBSCRIBE("oma:trace:*") covering every task in a single
    connection. Confirmed directly via redis-cli, isolating the exact
    cause rather than guessing: this Redis user's ACL denies the
    PSUBSCRIBE *command* outright -- NOPERM even on a literal, non-
    wildcard pattern identical to a channel SUBSCRIBE (not PSUBSCRIBE)
    that succeeds fine. That's a command-level ACL restriction only a
    Redis admin can lift, not something app code can route around, and
    not worth blocking real functionality on someone granting new
    permissions. Real workaround instead: track which tasks are actually
    active (needs_attention + in_progress -- a completed task essentially
    never publishes a new trace event) and SUBSCRIBE to exactly those
    named channels, refreshing the set periodically so a task that just
    started gets picked up within one refresh cycle rather than only via
    the slower periodic REST-poll fallback the frontend still keeps.

    Payload is intentionally minimal (just the task_id + a server
    timestamp), never the full task state -- this channel's only job is
    "wake up and refetch the real REST state now," matching this file's
    existing real-data-only discipline: the browser never trusts a
    trace-event payload as truth, only as a cue to go re-ask the real
    source (GET /api/tasks, GET /api/pending, GET /api/rounds/{id}).
    A periodic SSE comment line (a no-op ": keep-alive" per the SSE spec)
    keeps the connection from being silently dropped by an idle-timeout
    proxy when nothing real has happened in a while -- never a synthetic
    data event, so the client can't mistake it for a real change.

    SECOND real-time source, added 2026-08-06 after the SUBSCRIBE-only
    workaround above still left a real gap for brand-new tasks (can't
    subscribe to something before it's known to exist): this endpoint
    also drains a Postgres LISTEN/NOTIFY broadcast (see
    _pg_listen_thread_main's own module-level docstring for the full
    story) into the SAME output queue, so the browser sees one unified
    stream regardless of which real source noticed the change first --
    Postgres NOTIFY typically wins for anything that writes a durable
    agent_memory_events row (including a task's very first moment), Redis
    still carries the more granular live-progress signal in between.
    """
    # Real trade-off (post-critique, 2026-08-06, since narrowed further by
    # the Postgres NOTIFY addition above -- kept as a secondary safety net
    # for the Redis-subscribe side specifically, not the primary "notice a
    # new task" mechanism anymore). list_recent_tasks() costs ~0.2s per
    # call after the earlier N+1-query perf fix, so this stays cheap even
    # at this cadence.
    REFRESH_INTERVAL_SECONDS = 3.0

    async def event_generator():
        # Real merge point for both live sources (Postgres NOTIFY, Redis
        # trace pub/sub): this connection's own queue is registered in the
        # process-wide broadcast set so the pg-listen background thread can
        # push directly into it, AND the Redis pump below pushes into the
        # exact same queue -- the main loop then only ever has to drain one
        # thing, regardless of which source produced it.
        queue: asyncio.Queue = asyncio.Queue()
        _sse_broadcast_queues.add(queue)

        s = load_redis_settings()
        r = aioredis.Redis(
            host=s.host, port=s.port, username=s.username, password=s.password,
            db=s.db, decode_responses=True,
        )
        pubsub = r.pubsub()
        subscribed_task_ids: set[str] = set()
        # Real bug caught by an adversarial critique pass (2026-08-06),
        # confirmed as a genuine race, not a false positive: a task can
        # transition to completed AND publish its own terminal trace event
        # in the same ~few-second window a refresh runs in, with no
        # guaranteed ordering between "Postgres now says completed" and
        # "the terminal event landed on Redis." Unsubscribing the instant a
        # task falls out of the active buckets could tear down its channel
        # a moment before that last event arrives, silently losing the ONE
        # event that matters most. `pending_removal` (tasks that were
        # missing on the PREVIOUS cycle) gives every task one full extra
        # refresh cycle of grace before its subscription is actually torn
        # down -- negligible extra idle-channel cost, real correctness fix.
        pending_removal: set[str] = set()

        async def refresh_subscriptions() -> None:
            nonlocal pending_removal
            try:
                # list_recent_tasks() is a real, synchronous psycopg2 call
                # (same one GET /api/tasks itself uses) -- run off the
                # event loop so it can't stall other connected clients'
                # own streams while this one's periodic refresh runs.
                tasks = await asyncio.to_thread(list_recent_tasks)
            except Exception:
                logging.getLogger(__name__).warning("stream_all: refresh_subscriptions failed (non-fatal)", exc_info=True)
                return
            active_ids = {
                t["task_id"]
                for t in (tasks.get("needs_attention", []) + tasks.get("in_progress", []))
                if t.get("task_id")
            }
            new_ids = active_ids - subscribed_task_ids
            if new_ids:
                await pubsub.subscribe(*[channel_name(tid) for tid in new_ids])
                subscribed_task_ids.update(new_ids)
            still_missing = pending_removal & (subscribed_task_ids - active_ids)
            if still_missing:
                await pubsub.unsubscribe(*[channel_name(tid) for tid in still_missing])
                subscribed_task_ids.difference_update(still_missing)
            pending_removal = subscribed_task_ids - active_ids

        async def redis_pump():
            await refresh_subscriptions()
            last_refresh = time.monotonic()
            while True:
                if time.monotonic() - last_refresh > REFRESH_INTERVAL_SECONDS:
                    await refresh_subscriptions()
                    last_refresh = time.monotonic()
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                channel = message.get("channel") or ""
                prefix = "oma:trace:"
                task_id = channel[len(prefix):] if channel.startswith(prefix) else None
                queue.put_nowait(task_id)

        redis_task = asyncio.create_task(redis_pump())
        try:
            while True:
                try:
                    task_id = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps({'task_id': task_id, 'ts': time.time()})}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'level': 'manager', 'phase': 'stream', 'status': 'failed', 'message': f'live stream unavailable: {exc}'})}\n\n"
        finally:
            _sse_broadcast_queues.discard(queue)
            redis_task.cancel()
            try:
                await redis_task
            except Exception:
                pass
            try:
                if subscribed_task_ids:
                    await pubsub.unsubscribe(*[channel_name(tid) for tid in subscribed_task_ids])
            except Exception:
                pass
            await pubsub.aclose()
            await r.aclose()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/sign_off/{task_id}")
async def post_sign_off(task_id: str, req: SignOffRequest):
    result = await resume_after_sign_off(task_id, req.approved, _client, CLASSIFIER_MODEL, manager_model=MANAGER_MODEL)
    if result["status"] == "error":
        raise HTTPException(status_code=404, detail=result["message"])
    return JSONResponse(jsonable_encoder(result))


@app.post("/api/proposed_rule/{row_id}")
async def post_proposed_rule(row_id: int, req: ProposedRuleRequest):
    if req.approved:
        confirm_proposed_rule(row_id)
    else:
        reject_proposed_rule(row_id)
    return JSONResponse({"row_id": row_id, "approved": req.approved})


@app.post("/api/tasks/{task_id}/cancel")
async def post_cancel_task(task_id: str):
    """Phase 16 (§20.6): a real, user-initiated cancel. Sets the
    cancel_requested flag; the round loop (manager/loop.py) picks it up
    cooperatively at the top of its next iteration -- never a hard kill
    of an in-flight LLM call or Odoo write. Returns immediately; the
    actual stop happens asynchronously, at the next safe boundary.
    """
    request_cancel(task_id)
    return JSONResponse({"task_id": task_id, "cancel_requested": True})


@app.post("/api/tasks/{task_id}/pause")
async def post_pause_task(task_id: str):
    """Phase 17 (§21.5.4): pure bookkeeping -- the task is already
    stopped (the round loop already exited when the round budget was
    hit); this just marks it explicitly "intentionally frozen,
    resumable" as distinct from a task Operator has actively escalated and
    hasn't decided on yet. Nothing is summarized or discarded -- any
    clarifications already written stay exactly as they are, ready to
    be used whenever Continue is eventually clicked.
    """
    set_task_state(task_id, PAUSE_ROUND_BUDGET_EXHAUSTED)
    return JSONResponse({"task_id": task_id, "paused": True})


@app.post("/api/tasks/{task_id}/continue")
async def post_continue_task(task_id: str, req: ContinueRequest):
    """Phase 17 (§21.5.4): the real Continue mechanism -- resumes the
    SAME task_id, never a new one. Blocks until the new round batch
    resolves, matching /api/message's own existing blocking behavior
    for consistency (not changed in this phase).
    """
    result = await resume_task_after_checkpoint(task_id, _client, CLASSIFIER_MODEL, note=req.note, manager_model=MANAGER_MODEL)
    return JSONResponse(jsonable_encoder(result))


@app.post("/api/tasks/{task_id}/confirm")
async def post_confirm_task(task_id: str):
    """Phase 17 (§21.5.6): the single action that closes a branch after
    a real pass -- the digest is posted immediately when the task
    succeeds, but the branch stays open and interactive until Operator
    genuinely clicks Confirm. Writing this real branch_message is what
    the UI checks to decide whether to still show the open input/Confirm
    action or the closed, read-only state.
    """
    append_project_memory(
        event_type="branch_message", actor="operator", task_id=task_id, module=None,
        summary="Operator confirmed this result.", tags=["confirmed"],
        detail={"role": "operator", "kind": "confirmed", "text": "Confirmed."},
        verified=True,
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "operator", "phase": "branch_message", "status": "passed",
        "message": "Confirmed.",
        "branch_message": {"role": "operator", "kind": "confirmed", "text": "Confirmed."},
    })
    return JSONResponse({"task_id": task_id, "confirmed": True})


class ClarifyRequest(BaseModel):
    text: str


@app.post("/api/tasks/{task_id}/clarify")
async def post_clarify_task(task_id: str, req: ClarifyRequest):
    """Phase 17 (§21.5.3): Operator writing into a branch WITHOUT clicking a
    button -- stored as a real branch_message, with a real, lightweight
    Manager acknowledgment posted alongside it. Never triggers any round-
    loop activity on its own -- only /continue does that (§21.5.4's own
    explicit "buttons only ever trigger the real transition" rule).
    """
    append_project_memory(
        event_type="branch_message", actor="operator", task_id=task_id, module=None,
        summary=req.text[:300], tags=["clarification"],
        detail={"role": "operator", "kind": "clarification", "text": req.text},
        verified=True,
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "operator", "phase": "branch_message", "status": "passed",
        "message": req.text,
        "branch_message": {"role": "operator", "kind": "clarification", "text": req.text},
    })
    ack_text = "Got it -- ready whenever you are."
    append_project_memory(
        event_type="branch_message", actor="manager", task_id=task_id, module=None,
        summary=ack_text, tags=["acknowledgment"],
        detail={"role": "manager", "kind": "acknowledgment", "text": ack_text},
        verified=True,
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "branch_message", "status": "passed",
        "message": ack_text,
        "branch_message": {"role": "manager", "kind": "acknowledgment", "text": ack_text},
    })
    return JSONResponse({"task_id": task_id, "recorded": True})


@app.post("/api/escalations/{task_id}/dismiss")
async def post_dismiss_escalation(task_id: str):
    """No real backend concept of "resolve with choice A vs B" exists
    for a PauseForOperator escalation -- the real resolution mechanism is
    Operator replying in chat (a fresh message, a fresh task), same as any
    other request. This just clears an acknowledged escalation from
    the pending queue so it doesn't linger in "needs your attention"
    forever once Operator has actually addressed it.

    A real gap found live (Phase 16 QA pass): a PauseForOperator escalation
    never wrote any terminal Postgres row (only the ephemeral Redis
    pending-escalation entry represented "paused") -- once dismissed,
    the dashboard had nothing left to say the task ever paused, so it
    fell back to the stale "running" Redis flag and showed as
    perpetually in-progress. Fixed here: dismissal now writes a real
    decision event, matching how cancelled_by_operator already works.
    """
    clear_pending_escalation(task_id)
    append_project_memory(
        event_type="decision",
        actor="operator",
        task_id=task_id,
        module=None,
        summary="Operator dismissed this escalation without a further retry.",
        tags=["escalation_dismissed"],
        detail={},
        verified=True,
    )
    return JSONResponse({"task_id": task_id, "dismissed": True})


@app.post("/api/tasks/{task_id}/replay")
async def post_replay_task(task_id: str):
    """Replay -- not spec'd anywhere before this phase's mockup. Design
    decision: resubmits the task's real original goal text as a BRAND
    NEW task (fresh task_id, a fresh run through run_turn()), never a
    resume of the old, possibly-stopped/failed/cancelled one. Picked
    over "resume" because: (1) a stopped/cancelled task's round loop
    holds no meaningful in-flight state worth resurrecting -- the
    cooperative cancel point is deliberately a clean boundary, not a
    snapshot; (2) a task's own history (its outcome/decision/round rows)
    stays immutable either way, matching this project's append-only
    memory discipline -- replaying never mutates or supersedes the
    original task's own real record, it just starts a new one; (3) it's
    the same mental model Operator already uses for every other message --
    "ask again" -- rather than a second, special-cased resume path.
    """
    original = get_original_task(task_id)
    if original is None:
        raise HTTPException(status_code=404, detail=f"no real original goal found for task {task_id!r}")
    result = await run_turn(
        original["goal"], _client, CLASSIFIER_MODEL,
        anticipated_scope={"contract_inputs": original.get("inputs") or []},
        manager_model=MANAGER_MODEL,
    )
    return JSONResponse(jsonable_encoder({"replayed_from": task_id, "result": result}))


def _render_reply(result: dict) -> str:
    # Real gap found live (the project owner's own report): this used to be a raw
    # technical dump ("Task <uuid>: PASSED (tier=1, capability_class=...)")
    # with zero mention that the REAL human-facing summary -- and, on a
    # pass, the Confirm action -- live in this task's own branch, not
    # here. Operator had no way to know where to look. Fixed to point there
    # explicitly, using the real human_summary/success_digest headline
    # already generated, instead of duplicating or re-deriving anything.
    if result["status"] == "paused":
        human_summary = result.get("human_summary")
        if human_summary:
            return f"{human_summary}\n\nOpen this task's own conversation (left sidebar) to see the full picture and choose what to do next."
        return f"[{result['reason']}] {result['message']}"
    if result["status"] == "completed":
        outcome = "passed" if result["passed"] else "failed"
        return (
            f"This task {outcome}. Open its own conversation (left sidebar) for the real summary"
            + (" and to confirm it." if result["passed"] else ".")
        )
    return str(result)
