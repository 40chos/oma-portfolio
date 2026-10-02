"""The Manager's six tools, per the technical document's §3 and the
build plan's Phase 6 step 1: delegate_to_specialist, ask_operator,
check_sensitive_paths, read_project_memory, append_project_memory,
await_verification.

append_project_memory() is, by design, THE ONLY code path anywhere in
this system allowed to INSERT into agent_memory_events. Every other
module that wants something remembered (correction-detection,
error-learning, the loop itself) calls this function -- never its own
direct INSERT. This is enforced here by convention plus a grep-based
test (see tests/test_manager_tools.py) proving no other .py file in the
project contains a literal INSERT INTO agent_memory_events.
"""

from __future__ import annotations

import asyncio
import re

import psycopg2
import psycopg2.extras

from contracts.exceptions import PartialTaskFailure
from contracts.schema import CapabilityClass, SpecialistOutput, SpecialistType, TaskContract
from contracts.verifier_registry import unverified_shapes_targeted
from infra.settings import load_postgres_settings
from manager.charter import check_sensitive_paths  # re-exported tool
from manager.compensations import handle_partial_task_failure
from manager.memory import read_project_memory  # re-exported tool
from specialists import registry


# Real, confirmed gap found live (2026-08-06, fix-pass task 004): Build's own collision-autofix
# (specialists/build/specialist.py's `_validate_no_new_field_collides_with_real_target_field`)
# correctly strips a redundant new-field declaration when the field already exists for real on
# the live target model, and records WHICH field(s) via `_ALREADY_SATISFIED_BY_COLLISION_MARKER`
# in `generated.notes` -- but that confirmation was never threaded to testing_qa's own
# reproduction-target extraction, which reads only the (now-correctly-stripped) module files and
# the goal text, neither of which mentions the real field name once it's no longer a "new"
# declaration. Confirmed live: task 004's goal ("total of all line prices... in the header") was
# genuinely, correctly satisfied by a real, live `amount_total` field (a leftover from an earlier
# real attempt on this same shared dev database), Build's own generation correctly recognized this
# and stripped its own redundant re-declaration, but testing_qa's extraction still hallucinated an
# unrelated name ('line_prices_total') and had no way to discover the real, Build-confirmed field.
# Mirrors `specialists/build/specialist.py`'s own `_ALREADY_SATISFIED_BY_COLLISION_MARKER`/
# `_COLLISION_MARKER_FIELD_LIST_RE` exactly (duplicated here rather than imported, matching
# `specialists/testing_qa/specialist.py`'s own `_NOT_YET_IN_SCOPE_RE` precedent for cross-module
# regex duplication -- see that file's own comment: "specialists never import from manager", and
# by the same discipline manager avoids importing specialist internals here).
_ALREADY_SATISFIED_BY_COLLISION_MARKER = "ALREADY_SATISFIED_BY_REAL_TARGET_COLLISION"
_COLLISION_MARKER_FIELD_LIST_RE = re.compile(
    re.escape(_ALREADY_SATISFIED_BY_COLLISION_MARKER) + r":\s*\[([^\]]*)\]",
)


def _extract_collision_confirmed_field_names(self_report_uncertain: str | None) -> list[str]:
    """Parses the exact `f"{marker}: {colliding!r}"` format Build's own collision-autofix writes
    into `generated.notes` (threaded here via `build_output.detail['self_report_uncertain']`,
    see that field's own assignment in specialists/build/specialist.py's `run()`) -- returns []
    on anything that doesn't match, never a guess.
    """
    if not self_report_uncertain:
        return []
    match = _COLLISION_MARKER_FIELD_LIST_RE.search(self_report_uncertain)
    if not match:
        return []
    return re.findall(r"'([^']*)'", match.group(1))


def _get_conn():
    s = load_postgres_settings()
    return psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)


def append_project_memory(
    event_type: str,
    actor: str,
    summary: str,
    task_id: str | None = None,
    module: str | None = None,
    tags: list[str] | None = None,
    detail: dict | None = None,
    supersedes_id: int | None = None,
    verified: bool = False,
    stale_after: str | None = None,
) -> int:
    """The single, careful insert function. Every column that Phase 1's
    schema added specifically to avoid silent bare-defaults
    (`verified`, `stale_after`) is an explicit, required-to-consider
    parameter here -- never left implicit. Returns the new row's id.
    """
    if event_type not in (
        "issue", "attempt", "outcome", "decision", "rule", "note",
        # Phase 15 (§19) additions -- plan_created/plan_item_status must
        # only ever be written via manager.task_plan's own dedicated
        # functions (grep-verified, see tests/test_task_plan.py), never
        # called with these event_type values from anywhere else, even
        # though append_project_memory() itself is still the one INSERT
        # path underneath them. replan_round has no such restriction --
        # manager/loop.py's own round loop writes it directly.
        "plan_created", "plan_item_status", "replan_round",
        # Phase 16 (§20/dashboard) addition: a real, durable record of a
        # STANDALONE task's own goal text -- a real gap found while
        # building GET /api/tasks: outer-plan items already carry their
        # goal durably (plan_item_status's own detail.contract.goal),
        # but a standalone run_turn() task previously had nowhere
        # durable storing its original goal separately from the
        # in-memory TaskContract object, meaning an in-progress/pending
        # standalone task had no real title to show on a dashboard.
        # Written once, at contract-build time, by manager/loop.py only.
        "task_created",
        # Phase 17 (§21.5.7): round_checkpoint is the cold-tier checkpoint
        # written at escalation time (human_summary, prior_rounds,
        # round_count, last_contract). branch_message is the durable log
        # for a branch's own mini-chat (Operator's clarifications, the
        # Manager's acknowledgments, escalation summaries, success
        # digests) -- deliberately separate from the technical
        # round-by-round trace.
        "round_checkpoint", "branch_message",
        # Real, confirmed gap found live (the project owner's own report: main
        # chat messages "disappear... when reload"): the main chat's
        # own conversation_history lived ONLY in a plain in-memory dict
        # (ui/chat/server.py's _sessions), never written anywhere
        # durable -- a page reload, or any server restart, lost it
        # completely, with no way to recover it even though every other
        # conversation surface in this system (branch_message for a
        # task's own mini-chat) already had exactly this durability.
        # task_id is None here on purpose -- a main-chat turn doesn't
        # necessarily have one yet (it's what CREATES a task_id, when
        # it does).
        "main_chat_message",
        # Phase 20 (§24.4 Component 7): template_harvested is written
        # once per template templates/harvest.py extracts from a
        # verified real task. template_match/template_fallback have no
        # writer yet (Components 2/3 aren't built) but are allowed here
        # now for the same reason the migration added them now --
        # see scripts/008_template_metrics_event_types.sql.
        "template_harvested", "template_match", "template_fallback",
        # Phase 31 UI (2026-08-08): real, confirmed gap -- GET /api/rounds/{task_id} (and thus
        # the UI's own buildGraphForTask()) only ever derives constraint_nodes from a
        # replan_round row's own new_contract snapshot, which is written ONLY when a round FAILS
        # and gets revised. A task cleanly executing its first round (the common, good case --
        # confirmed live, the project owner's own report: "I don't see any graph in UI" for a genuinely
        # in-progress, still-on-round-1 task) has ZERO replan_round rows yet, so the graph never
        # appears at all until/unless something fails -- backwards, since watching the graph
        # structure during a task's real, live first execution is exactly when it matters most.
        # graph_created is written once, immediately after manager/loop.py's own decomposition
        # gate builds the real constraint_nodes graph -- before round 1 of any node has even
        # started -- so the UI has real structure to render from the very first moment, with
        # live per-node status then patched over it via the already-existing node_state_changed
        # SSE/Redis telemetry (today's own graph_scheduler.py fix).
        "graph_created",
    ):
        raise ValueError(f"invalid event_type: {event_type!r}")

    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_memory_events
                (event_type, task_id, module, tags, actor, summary, detail,
                 supersedes_id, verified, stale_after)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::interval)
            RETURNING id
            """,
            (
                event_type, task_id, module, tags or [], actor, summary,
                psycopg2.extras.Json(detail) if detail is not None else None,
                supersedes_id, verified, stale_after,
            ),
        )
        new_id = cur.fetchone()[0]
        cur.close()
        return new_id
    finally:
        conn.close()


def _update_node_field_live(task_id: str, label: str, field_name: str, value) -> None:
    """Real bug found live (2026-08-08, task 05c20568-d4ba-4836-b120-86410bb2abc1, the project owner's own
    report, first fix) -- and a SECOND, deeper real bug in that same fix found live later the
    same day (the project owner's own follow-up, after independently re-verifying the specialist/round
    telemetry work): the original fix only ever updated the `graph_created` event's own snapshot,
    on the theory that "a real replan_round, once one exists, fully supersedes the synthetic
    graph_created entry" (list_rounds_for_task()'s own stated precedence) -- meaning graph_created
    updates stop mattering the instant that's true. That reasoning missed that the REPLAN_ROUND
    row taking over is itself just ANOTHER frozen, point-in-time snapshot, written once when that
    round was revised and never touched again -- so the exact same "stuck forever" bug the first
    fix closed for the pre-first-failure window reopens, permanently, for the rest of the task's
    life, the instant a single retry happens. Confirmed live: a node that had genuinely already
    moved on to `failing`/`blocked` after a real round failure still read back `pending`/round
    0/no specialist from `GET /api/rounds/{task_id}` afterward, because that endpoint had already
    switched over to reading the stale `replan_round` row.

    Real fix: every live node-field update now patches BOTH real candidate rows unconditionally --
    the `graph_created` snapshot (unchanged, still the only source before any round has ever
    failed) AND the single latest `replan_round` row for this task_id, if one exists (targeting
    `detail.new_contract.constraint_nodes.{label}.{field_name}` there, matching the exact shape
    `list_rounds_for_task()`'s own real replan_round rows already carry). Whichever one
    `list_rounds_for_task()` actually surfaces for a given request always has live-current data --
    the frozen snapshot stops being the ONLY source of truth once selected, instead of becoming
    the PERMANENT one. A later, second real failure still writes a fresh `replan_round` snapshot
    exactly as before (unaffected by this fix) -- and immediately starts receiving its own live
    patches again from that point forward, for the same reason.

    Both UPDATEs guard against fabricating a LABEL that doesn't exist in a given row (e.g. a
    `replan_round` snapshot taken before a later recursive split introduced a brand-new label) via
    a `detail -> 'constraint_nodes' ? %s` (jsonb "has key") check in the WHERE clause -- a row
    missing that label is left completely untouched, no update attempted at all. Real bug caught
    while writing THIS fix, not shipped: `jsonb_set()`'s own `create_missing` flag applies to the
    WHOLE path, not per-segment, so passing `create_missing=false` to guard the label also silently
    blocked ever setting a genuinely NEW field key (like `active_specialist`, which never existed
    in any row's original snapshot) -- confirmed live, that first attempt permanently no-op'd on
    every specialist/round update. The WHERE-clause guard above solves the actual problem (never
    invent a label) without that side effect: `jsonb_set()` itself is called with its default
    `create_missing=true`, freely adding/overwriting the leaf field once the label is confirmed to
    exist. Both remain single, atomic statements -- never a read-modify-write race against a
    concurrent node's own update to a DIFFERENT label in the same row. Swallows every exception and
    only logs, matching this module's own "never break the caller's real work" discipline for
    anything telemetry-adjacent -- never raises.
    """
    import logging

    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        # jsonb_set's own create_missing flag applies to the WHOLE path, not per-segment -- there
        # is no way to say "create the final field key if it's new, but never fabricate the LABEL
        # segment if that's new" with create_missing alone (confirmed the hard way: create_missing
        # =false also blocks a brand-new field key like active_specialist, which never existed in
        # ANY row's original snapshot, permanently breaking it). The real guard belongs in the
        # WHERE clause instead: `detail -> 'constraint_nodes' ? %s` (the jsonb "has key" operator)
        # confirms the LABEL already exists before touching the row at all -- jsonb_set itself
        # then defaults to create_missing=true, freely adding/overwriting the leaf field, which is
        # exactly what a genuinely new telemetry field on an already-known node needs.
        cur.execute(
            """
            UPDATE agent_memory_events
            SET detail = jsonb_set(detail, %s::text[], %s::jsonb)
            WHERE task_id = %s AND event_type = 'graph_created'
              AND detail -> 'constraint_nodes' ? %s
            """,
            (["constraint_nodes", label, field_name], psycopg2.extras.Json(value), task_id, label),
        )
        cur.execute(
            """
            UPDATE agent_memory_events
            SET detail = jsonb_set(detail, %s::text[], %s::jsonb)
            WHERE task_id = %s AND event_type = 'replan_round'
              AND id = (
                  SELECT MAX(id) FROM agent_memory_events
                  WHERE task_id = %s AND event_type = 'replan_round'
              )
              AND detail -> 'new_contract' -> 'constraint_nodes' ? %s
            """,
            (
                ["new_contract", "constraint_nodes", label, field_name],
                psycopg2.extras.Json(value), task_id, task_id, label,
            ),
        )
        cur.close()
    except Exception:
        logging.getLogger(__name__).warning(
            "_update_node_field_live failed for task_id=%s label=%r field_name=%r value=%r "
            "(non-fatal -- the UI's live graph may show stale data for this node until the "
            "next successful update)",
            task_id, label, field_name, value, exc_info=True,
        )
    finally:
        conn.close()


def update_graph_created_node_state(task_id: str, label: str, new_state: str) -> None:
    """See `_update_node_field_live()`'s own docstring for the full incident (original fix +
    the later "freezes forever after the first retry" bug, both closed there). Kept as its own
    named function -- called from manager/graph_scheduler.py's own `_publish_node_state_changed()`
    -- for the same reason its sibling functions below are: call-site clarity, not because the
    underlying mechanism differs per field.
    """
    _update_node_field_live(task_id, label, "state", new_state)


def update_graph_created_node_specialist(task_id: str, label: str, specialist: str | None) -> None:
    """See `_update_node_field_live()`'s own docstring. Closes the third, and last, real gap of
    the same UI-telemetry incident: the live Redis publish alone only reaches a browser with an
    OPEN SSE connection at the exact instant it fires -- a fresh page load instead calls
    `GET /api/rounds/{task_id}`, which needs a live-current snapshot to read from regardless of
    whether a round has ever failed for this task (Phase 31 UI doc §7 item 16's own named gap).
    """
    _update_node_field_live(task_id, label, "active_specialist", specialist)


def update_graph_created_node_round(task_id: str, label: str, round_number: int) -> None:
    """See `_update_node_field_live()`'s own docstring. Same real gap as
    `update_graph_created_node_specialist()` above, for the round-number badge instead: a
    decomposed sub-contract's own internal round loop (manager/loop.py's `_execute_contract()`)
    can run several rounds while that single ConstraintNode stays `running` the whole time --
    without this, a fresh page load mid-round would show a stale round number.
    """
    _update_node_field_live(task_id, label, "round_number", round_number)


def _migrate_legacy_round_attempts(existing, value_key: str) -> list[dict]:
    """Real bug found live (2026-08-09, same day as the resume-tagging fix this repairs after --
    the project owner's own direct report: "I don't see any code difference... it's very weird looking"):
    `persist_node_round_diff/findings/checks()` switched from SET to blind-APPEND (see those
    functions' own docstrings), but a round key that ALREADY held data from before that fix
    shipped was still in the OLD flat shape (the bare value itself -- e.g. `round_diffs["1"]` was
    literally `[{file, lines}, ...]`, not `[{resume_index, diff}, ...]`). Blindly appending a new
    `{resume_index, [value_key]}` wrapper onto that old flat array produced a genuinely corrupted
    MIXED array (confirmed live: `round_diffs["1"]` held one bare `{file, lines}` entry alongside
    four real `{resume_index, diff}` entries) -- the frontend's own legacy-detection
    (`_normalizeRoundAttempts()`, keyed off `raw[0]`) then treated the WHOLE mixed array as one
    single legacy blob, handing the diff renderer a list containing both real per-file hunks AND
    unrelated wrapper objects, which is exactly what looked "weird" and showed no real diff.

    Called before every append from now on: splits `existing` into "bare" entries (missing
    `resume_index` or `value_key` -- the pre-fix shape) and already-tagged entries, collapses
    every bare entry into ONE `{resume_index: 0, value_key: [...]}` group (that's what they
    collectively always were -- the single flat value written before per-attempt tagging
    existed), and returns tagged-entries-only, bare-group first. A never-touched round (existing
    is None/missing) returns `[]`, unaffected. An already-fully-migrated round (every entry
    already tagged) returns unchanged, so this is safe to run on every single append, forever --
    not a one-time migration script that can be forgotten.
    """
    if not existing:
        return []
    if not isinstance(existing, list):
        return []
    bare: list = []
    tagged: list[dict] = []
    for item in existing:
        if isinstance(item, dict) and "resume_index" in item and value_key in item:
            tagged.append(item)
        else:
            bare.append(item)
    result = []
    if bare:
        result.append({"resume_index": 0, value_key: bare})
    result.extend(tagged)
    return result


def _append_migrated_node_round_field_live(task_id: str, label: str, field_name: str, round_number: int, entry: dict, value_key: str) -> None:
    """The diff/findings/checks-specific counterpart to `_append_node_round_field_live()` --
    reads the existing per-round array first (from BOTH the graph_created snapshot and the
    latest replan_round row, independently, since either can hold its own differently-shaped
    legacy data), repairs it via `_migrate_legacy_round_attempts()` if needed, appends the new
    entry, and writes the whole corrected array back as one explicit value -- never the blind
    `COALESCE(...) || jsonb_build_array(entry)` append `_append_node_round_field_live()` uses,
    which is exactly what produced the corrupted mixed-shape array this function exists to never
    repeat. Same same-label-exists guard and non-fatal-swallow discipline as every other function
    in this module.
    """
    import logging

    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        container_path = ["constraint_nodes", label, field_name]
        round_key = str(round_number)
        full_path = container_path + [round_key]

        cur.execute(
            """
            SELECT detail #> %s::text[]
            FROM agent_memory_events
            WHERE task_id = %s AND event_type = 'graph_created'
              AND detail -> 'constraint_nodes' ? %s
            """,
            (full_path, task_id, label),
        )
        row = cur.fetchone()
        if row is not None:
            existing = row[0]
            new_array = _migrate_legacy_round_attempts(existing, value_key)
            new_array.append(entry)
            # jsonb_set()'s own create_missing only ever fills the LAST path segment -- if
            # `field_name` (round_diffs/round_findings/round_checks) doesn't exist on this node
            # AT ALL yet (this round's very first real entry), a naive jsonb_set straight at
            # `full_path` silently no-ops (same lesson learned earlier this session, reintroduced
            # here and caught live: task 07141af5's own ticket_access_rights round 1 diff
            # corrupted). Same COALESCE-then-merge-then-set-one-level-up fix as every other
            # function in this module: guarantee `container_path` itself exists first.
            cur.execute(
                """
                UPDATE agent_memory_events
                SET detail = jsonb_set(
                    detail, %s::text[],
                    COALESCE(detail #> %s::text[], '{}'::jsonb) || jsonb_build_object(%s, %s::jsonb)
                )
                WHERE task_id = %s AND event_type = 'graph_created'
                  AND detail -> 'constraint_nodes' ? %s
                """,
                (container_path, container_path, round_key, psycopg2.extras.Json(new_array), task_id, label),
            )

        replan_container_path = ["new_contract", *container_path]
        replan_full_path = replan_container_path + [round_key]
        cur.execute(
            """
            SELECT detail #> %s::text[]
            FROM agent_memory_events
            WHERE task_id = %s AND event_type = 'replan_round'
              AND id = (
                  SELECT MAX(id) FROM agent_memory_events
                  WHERE task_id = %s AND event_type = 'replan_round'
              )
              AND detail -> 'new_contract' -> 'constraint_nodes' ? %s
            """,
            (replan_full_path, task_id, task_id, label),
        )
        replan_row = cur.fetchone()
        if replan_row is not None:
            existing = replan_row[0]
            new_array = _migrate_legacy_round_attempts(existing, value_key)
            new_array.append(entry)
            cur.execute(
                """
                UPDATE agent_memory_events
                SET detail = jsonb_set(
                    detail, %s::text[],
                    COALESCE(detail #> %s::text[], '{}'::jsonb) || jsonb_build_object(%s, %s::jsonb)
                )
                WHERE task_id = %s AND event_type = 'replan_round'
                  AND id = (
                      SELECT MAX(id) FROM agent_memory_events
                      WHERE task_id = %s AND event_type = 'replan_round'
                  )
                  AND detail -> 'new_contract' -> 'constraint_nodes' ? %s
                """,
                (
                    replan_container_path, replan_container_path, round_key,
                    psycopg2.extras.Json(new_array), task_id, task_id, label,
                ),
            )
        cur.close()
    except Exception:
        logging.getLogger(__name__).warning(
            "_append_migrated_node_round_field_live failed for task_id=%s label=%r "
            "field_name=%r round_number=%r (non-fatal -- this entry may be missing from this "
            "round's historical log on a fresh load until the next successful write)",
            task_id, label, field_name, round_number, exc_info=True,
        )
    finally:
        conn.close()


def persist_node_round_diff(task_id: str, label: str, round_number: int, diff: list[dict], resume_index: int = 0) -> None:
    """Durable counterpart to the existing live-only SSE diff publish (manager/loop.py).

    Real fix, 2026-08-09 (the project owner's own direct live report on task
    07141af5-9a4e-41b6-93ea-8b7af04fea9c: "it should replace our previous round one... but I want
    it to collect into one single picture... do not replace previous because it's replaced
    history"). Used to plain-SET `round_diffs.{round_number}` (via `_update_node_round_field_live()`),
    so a resumed attempt reusing round_number=1 (a genuine, intentional restart of THIS attempt's
    own round counter, not a bug -- see `_execute_contract()`'s own round loop) silently
    OVERWROTE the pre-pause attempt's real diff with no trace it ever existed. Now APPENDS a
    `{resume_index, diff}` entry to a real array at `round_diffs.{round_number}` instead --
    `_append_migrated_node_round_field_live()` (not the plain array-append
    `_append_node_round_field_live()` `round_steps` uses -- see that function's own docstring for
    why: a round already holding OLD, pre-fix flat data needs a real one-time-per-write repair,
    not a blind append) -- so every attempt at a given round number is preserved, tagged with
    which resume produced it, and any already-corrupted mixed-shape data self-heals on the very
    next write.
    """
    entry = {"resume_index": resume_index, "diff": diff}
    _append_migrated_node_round_field_live(task_id, label, "round_diffs", round_number, entry, "diff")


def persist_node_round_findings(task_id: str, label: str, round_number: int, findings: list[dict], resume_index: int = 0) -> None:
    """Durable counterpart to the existing live-only SSE findings publish (manager/loop.py) --
    see `persist_node_round_diff()`'s own docstring for the real resume-data-loss bug this,
    identically, closes for Code-Review's own findings."""
    entry = {"resume_index": resume_index, "findings": findings}
    _append_migrated_node_round_field_live(task_id, label, "round_findings", round_number, entry, "findings")


def persist_node_round_checks(task_id: str, label: str, round_number: int, checks: list[dict], resume_index: int = 0) -> None:
    """Durable counterpart to the existing live-only SSE checks publish (manager/loop.py) --
    see `persist_node_round_diff()`'s own docstring for the real resume-data-loss bug this,
    identically, closes for Testing/QA's own checks."""
    entry = {"resume_index": resume_index, "checks": checks}
    _append_migrated_node_round_field_live(task_id, label, "round_checks", round_number, entry, "checks")


def _append_node_round_field_live(task_id: str, label: str, field_name: str, round_number: int, entry: dict) -> None:
    """Shared append-to-array-per-round machinery, factored out of what was originally
    `persist_node_round_step()`'s own private SQL (2026-08-08) so `round_diffs`/`round_findings`/
    `round_checks` (below) can reuse the exact same, already-proven "append, never overwrite"
    pattern -- see those functions' own docstrings for why they were switched from a SET-once
    value to this. `COALESCE(..., '[]'::jsonb) || jsonb_build_array(entry)` handles "the array
    doesn't exist yet for this round" the same `jsonb_set()`-only-fills-the-last-segment
    workaround used throughout this module -- append-safe from a round's very first entry.
    """
    import logging

    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        container_path = ["constraint_nodes", label, field_name]
        round_key = str(round_number)
        cur.execute(
            """
            UPDATE agent_memory_events
            SET detail = jsonb_set(
                detail, %s::text[],
                COALESCE(detail #> %s::text[], '{}'::jsonb)
                || jsonb_build_object(%s, COALESCE(detail #> (%s::text[] || %s), '[]'::jsonb) || jsonb_build_array(%s::jsonb))
            )
            WHERE task_id = %s AND event_type = 'graph_created'
              AND detail -> 'constraint_nodes' ? %s
            """,
            (
                container_path, container_path, round_key,
                container_path, [round_key], psycopg2.extras.Json(entry),
                task_id, label,
            ),
        )
        replan_container_path = ["new_contract", *container_path]
        cur.execute(
            """
            UPDATE agent_memory_events
            SET detail = jsonb_set(
                detail, %s::text[],
                COALESCE(detail #> %s::text[], '{}'::jsonb)
                || jsonb_build_object(%s, COALESCE(detail #> (%s::text[] || %s), '[]'::jsonb) || jsonb_build_array(%s::jsonb))
            )
            WHERE task_id = %s AND event_type = 'replan_round'
              AND id = (
                  SELECT MAX(id) FROM agent_memory_events
                  WHERE task_id = %s AND event_type = 'replan_round'
              )
              AND detail -> 'new_contract' -> 'constraint_nodes' ? %s
            """,
            (
                replan_container_path, replan_container_path, round_key,
                replan_container_path, [round_key], psycopg2.extras.Json(entry),
                task_id, task_id, label,
            ),
        )
        cur.close()
    except Exception:
        logging.getLogger(__name__).warning(
            "_append_node_round_field_live failed for task_id=%s label=%r field_name=%r "
            "round_number=%r (non-fatal -- this entry may be missing from this round's "
            "historical log on a fresh load until the next successful write)",
            task_id, label, field_name, round_number, exc_info=True,
        )
    finally:
        conn.close()


def persist_node_round_step(task_id: str, label: str, round_number: int, actor: str, message: str, status: str, resume_index: int = 0) -> None:
    """Real UX addition, 2026-08-08 (the project owner's own explicit request: the granular, real-time
    step-by-step narrative -- "Check this. Change that." -- the pre-Phase-31 UI showed, now
    durably preserved too). A round's own step list genuinely grows over the course of that
    round -- multiple real steps happen per specialist per round -- so this APPENDS to a real
    JSON array at `round_steps.{round_number}` rather than overwriting it.

    `resume_index` (2026-08-09, the project owner's own direct live report: "it should replace our previous
    round one... I want it to collect into one single picture... resume one, resume two, resume
    three... so we could sort out all this resume, but not replace") -- `contract.
    resumed_from_checkpoint_count` (0 for a never-resumed original attempt), tagged onto every
    step so the UI can group a round's steps by WHICH resume attempt produced them, never
    silently interleaving two different attempts' own narratives with no way to tell them apart.
    """
    entry = {"actor": actor, "message": message, "status": status, "resume_index": resume_index}
    _append_node_round_field_live(task_id, label, "round_steps", round_number, entry)


def persist_node_round_timing(task_id: str, label: str, round_number: int, actor: str, seconds: float, resume_index: int = 0) -> float:
    """Real UX addition, 2026-08-08 (the project owner's own explicit request: "add time tracker and
    counter for every round for every builder code reviewer tester orchestrator ... everything
    all time"). Same durable-storage discipline as `persist_node_round_step()` above, but a
    round's own timing is a real {actor: seconds} MAP, not a growing list -- each actor
    (build/review/qa/manager/round_total) is set at most once per round+attempt, at that actor's
    own real completion, so this SETS (merges) rather than appends.

    `resume_index` (2026-08-09, the project owner's own direct live report: "timer for different resumes,
    it's staying the same... it should be separate sync, not the same timer for everything").
    Three real jsonb levels deep now -- `round_timings.{round_number}.{resume_index}.{actor}` --
    nested one level deeper to isolate each ATTEMPT's own real timing from every other attempt's,
    exactly matching `round_diffs`/`round_findings`/`round_checks`'s own resume-tagging (see
    `persist_node_round_diff()`'s docstring for that fix). A given (round, resume_index, actor)
    triple is written AT MOST ONCE ever -- round numbering only repeats ACROSS resumes (a new
    resume_index every time), never within a single continuous attempt -- so this reverted from
    the earlier 2026-08-08 cross-attempt ADD-accumulation fix back to a plain SET at the (round,
    resume_index, actor) key: that accumulation existed only to avoid losing an attempt's real
    seconds when a DIFFERENT, LATER attempt blindly overwrote the same un-tagged key -- tagging
    by resume_index removes the collision at its root, so summing is no longer needed.

    Real bug caught before this ever ran against production data (same category as
    `persist_node_round_diff()`'s own live-caught corruption, closed minutes earlier the same
    day): a round already holding OLD, pre-resume-tagging data has `round_timings.{round}` shaped
    as the flat `{actor: seconds}` map directly (e.g. `{"build": 12.5, "round_total": 20.0}`) --
    blindly merging a NEW `{resume_index: {...}}` entry onto that with a plain `||` would produce
    a genuinely corrupted MIXED object (`{"build": 12.5, "round_total": 20.0, "65": {...}}`,
    actor keys and a resume_index key sitting at the very same level). Reads the existing
    `round_timings.{round}` value first and migrates it (wraps the WHOLE old flat map as
    `{"0": old_map}`) before merging in the new attempt's entry, exactly mirroring
    `_migrate_legacy_round_attempts()`'s own repair for diffs/findings/checks -- safe to call on
    every single write, forever, not a one-time migration script that can be forgotten.
    """
    import logging

    def _migrate(existing):
        if not existing or not isinstance(existing, dict):
            return {}
        # New shape: every value is itself a dict (one per resume_index). Old shape: values are
        # plain numbers (seconds), keyed directly by actor name.
        if all(isinstance(v, dict) for v in existing.values()):
            return dict(existing)
        return {"0": existing}

    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        container_path = ["constraint_nodes", label, "round_timings"]
        round_key = str(round_number)
        resume_key = str(resume_index)
        round_path = container_path + [round_key]

        cur.execute(
            "SELECT detail #> %s::text[] FROM agent_memory_events "
            "WHERE task_id = %s AND event_type = 'graph_created' AND detail -> 'constraint_nodes' ? %s",
            (round_path, task_id, label),
        )
        row = cur.fetchone()
        if row is not None:
            migrated = _migrate(row[0])
            migrated.setdefault(resume_key, {})
            migrated[resume_key][actor] = seconds
            cur.execute(
                """
                UPDATE agent_memory_events
                SET detail = jsonb_set(
                    detail, %s::text[],
                    COALESCE(detail #> %s::text[], '{}'::jsonb) || jsonb_build_object(%s, %s::jsonb)
                )
                WHERE task_id = %s AND event_type = 'graph_created'
                  AND detail -> 'constraint_nodes' ? %s
                """,
                (container_path, container_path, round_key, psycopg2.extras.Json(migrated), task_id, label),
            )

        replan_container_path = ["new_contract", *container_path]
        replan_round_path = replan_container_path + [round_key]
        cur.execute(
            "SELECT detail #> %s::text[] FROM agent_memory_events "
            "WHERE task_id = %s AND event_type = 'replan_round' "
            "AND id = (SELECT MAX(id) FROM agent_memory_events WHERE task_id = %s AND event_type = 'replan_round') "
            "AND detail -> 'new_contract' -> 'constraint_nodes' ? %s",
            (replan_round_path, task_id, task_id, label),
        )
        replan_row = cur.fetchone()
        if replan_row is not None:
            migrated = _migrate(replan_row[0])
            migrated.setdefault(resume_key, {})
            migrated[resume_key][actor] = seconds
            cur.execute(
                """
                UPDATE agent_memory_events
                SET detail = jsonb_set(
                    detail, %s::text[],
                    COALESCE(detail #> %s::text[], '{}'::jsonb) || jsonb_build_object(%s, %s::jsonb)
                )
                WHERE task_id = %s AND event_type = 'replan_round'
                  AND id = (
                      SELECT MAX(id) FROM agent_memory_events
                      WHERE task_id = %s AND event_type = 'replan_round'
                  )
                  AND detail -> 'new_contract' -> 'constraint_nodes' ? %s
                """,
                (
                    replan_container_path, replan_container_path, round_key,
                    psycopg2.extras.Json(migrated), task_id, task_id, label,
                ),
            )
        cur.close()
        return seconds
    except Exception:
        logging.getLogger(__name__).warning(
            "persist_node_round_timing failed for task_id=%s label=%r round_number=%r actor=%r "
            "resume_index=%r (non-fatal -- this round's timing may be missing from this actor's "
            "historical log)",
            task_id, label, round_number, actor, resume_index, exc_info=True,
        )
        # Real, honest fallback: the durable write may or may not have landed, but the caller
        # (manager/loop.py's live SSE publish) still needs SOME real number rather than crashing
        # -- this round's own freshly-measured delta is the best available truth when the
        # write genuinely couldn't be confirmed.
        return seconds
    finally:
        conn.close()


def sync_live_node_telemetry_into_new_snapshot(task_id: str, constraint_nodes: dict) -> dict:
    """Closes a real, live-confirmed bug (2026-08-08, the project owner's own direct investigation, task
    07936710-e7a0-41d2-bc96-9282feebe3a8) in the sibling functions above: `_update_node_field_live()`
    correctly targets the newest `replan_round` row that EXISTS AT THE MOMENT a live patch fires --
    but a node's mid-round telemetry (round_number, active_specialist) is patched via this raw-SQL
    path, which is completely disconnected from the in-memory `TaskContract`/`ConstraintNode`
    object `revise_contract_from_verification()` builds the NEXT round's `new_contract` from. So
    the instant a new `replan_round` row is created for the next round, its own fresh
    `new_contract.constraint_nodes` snapshot has no way to know about patches that landed on the
    row it's superseding -- it silently resets to whatever the in-memory contract object last held
    (confirmed live: round_number=2 on the row a live patch reached, round_number=0 on the very
    next row created afterward for the same label).

    Real fix, applied once, right before a new `replan_round` row is written (manager/loop.py,
    immediately before the `ReplanRound(...)`/`append_project_memory(event_type="replan_round", ...)`
    call): read the CURRENT live telemetry (state/active_specialist/round_number) and overlay it
    onto the freshly-built `new_contract.constraint_nodes` for every label both sides share, before
    it's persisted. This does not touch `revise_contract_from_verification()` or any real
    execution-path contract logic -- it is purely a telemetry-continuity patch applied to the dict
    that is about to become the new row's own frozen snapshot, the same "display data only, never
    behavior" scope every other function in this module keeps to. Returns the same
    `constraint_nodes` dict, mutated in place, for a convenient call-site expression.

    Corrected 2026-08-08, same day as the original fix (the project owner's own direct, live investigation
    against a real running task, c3a8181d-9716-46f0-bc2a-312896dabdbc): the first version of this
    function preferred "the latest existing `replan_round` row" as its sync source, falling back
    to `graph_created` only when no `replan_round` row existed yet. That's backwards, and the real
    bug is a chaining one: `_update_node_field_live()`'s dual-write means `graph_created` is
    ALWAYS patched on every single live telemetry event, unconditionally, for the life of the
    task -- it is the one row that can never itself go stale. A `replan_round` row, by contrast,
    is a point-in-time snapshot that stops receiving guaranteed patches the moment a NEWER
    `replan_round` row supersedes it as `_update_node_field_live()`'s own `MAX(id)` target -- so
    syncing a brand-new row FROM the prior `replan_round` row (rather than from `graph_created`)
    copies forward whatever staleness that prior row had already accumulated in the (often short,
    but real and observed live) window between its own creation and its own last successful
    patch, and that staleness compounds across every subsequent round's own row. Confirmed live:
    round_number correctly synced across two successive rows (both read `graph_created`-derived
    data at the time), but state independently drifted between them because the two fields were
    patched via separate, non-atomic `_update_node_field_live()` calls landing at different real
    moments relative to each row's own creation -- exactly the compounding-chain failure mode this
    correction closes by always reading the one row that's authoritative for the task's entire
    life, never a prior round's own possibly-still-catching-up snapshot.
    """
    import logging

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT detail -> 'constraint_nodes'
            FROM agent_memory_events
            WHERE task_id = %s AND event_type = 'graph_created'
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        )
        row = cur.fetchone()
        live_nodes = row[0] if row and row[0] else None
        if live_nodes is None:
            # Defensive only -- graph_created should always exist by the time any replan_round
            # is written; this branch exists purely so a truly unexpected gap degrades to the
            # old prior-round-chaining behavior rather than syncing nothing at all.
            cur.execute(
                """
                SELECT detail -> 'new_contract' -> 'constraint_nodes'
                FROM agent_memory_events
                WHERE task_id = %s AND event_type = 'replan_round'
                ORDER BY id DESC LIMIT 1
                """,
                (task_id,),
            )
            row = cur.fetchone()
            live_nodes = row[0] if row and row[0] else None
        cur.close()
        if live_nodes:
            for label, node in constraint_nodes.items():
                live = live_nodes.get(label)
                if not live:
                    continue
                # Real UX addition, 2026-08-08: round_diffs/round_findings/round_checks carried
                # forward here too, same as the three original fields -- without this, a new
                # replan_round row's own fresh snapshot would silently drop every past round's
                # already-persisted diff/findings/checks the moment a new round starts, exactly
                # the same class of loss this whole function exists to prevent for state/
                # active_specialist/round_number.
                for field_name in ("state", "active_specialist", "round_number", "round_diffs", "round_findings", "round_checks", "round_steps", "round_timings"):
                    if field_name in live and live[field_name] is not None:
                        # constraint_nodes may be dict[str, ConstraintNode] (real Pydantic
                        # objects, not yet serialized -- ConstraintNode confirmed non-frozen,
                        # in-place attribute mutation is safe per Phase 31 §1.5) or a plain
                        # dict (already-serialized JSON), depending on the caller's own stage --
                        # support both rather than assuming one.
                        if hasattr(node, "__setattr__") and not isinstance(node, dict):
                            setattr(node, field_name, live[field_name])
                        else:
                            node[field_name] = live[field_name]
    except Exception:
        logging.getLogger(__name__).warning(
            "sync_live_node_telemetry_into_new_snapshot failed for task_id=%s (non-fatal -- the "
            "new replan_round row may start from stale telemetry until its own next live patch)",
            task_id, exc_info=True,
        )
    finally:
        conn.close()
    return constraint_nodes


def read_node_states(task_id: str) -> dict[str, str]:
    """Real fix, 2026-08-08 (the project owner's own direct report + live-confirmed bug, task
    657697fc-f701-4932-a172-b0132da93cfa): the ONE genuinely authoritative source for "which
    constraint labels are REALLY satisfied" for a decomposed task -- `graph_created`, the same
    row every other function in this module treats as never-stale (see
    `sync_live_node_telemetry_into_new_snapshot()`'s own docstring for why). Returns
    `{label: state}` for every real constraint node this task has, `state` being the exact
    `ConstraintNodeState` value (`contracts/schema.py`) last live-patched by
    `manager/graph_scheduler.py`'s `_publish_node_state_changed()`.

    Exists specifically to close a real, confirmed corruption in `manager/loop.py`'s
    `_reconstruct_resume_order()`: that function used to infer "already satisfied" purely from a
    label's POSITION in `contract.constraint_order` (an arbitrary decomposition-time enumeration
    order, NOT a topological/dependency order -- confirmed live: a label was sorted ahead of the
    very predecessor it real-depends on) relative to whichever label happened to be
    `current_constraint_label` at checkpoint time. That silently marked labels "satisfied" that
    were never actually built or verified, which then let their real dependents look "ready" to
    `manager/graph_scheduler.py`'s own `ready_labels()` even though their real predecessor never
    ran -- confirmed live: `ticket_workflow` (needs `service_ticket_model`) started running while
    `service_ticket_model` itself was still `pending`, having never been dispatched at all. Every
    resume now reads real per-label ground truth from here instead of guessing from list
    position.
    """
    import logging

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT detail -> 'constraint_nodes'
            FROM agent_memory_events
            WHERE task_id = %s AND event_type = 'graph_created'
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        )
        row = cur.fetchone()
        cur.close()
        nodes = row[0] if row and row[0] else {}
        return {label: (node or {}).get("state") for label, node in nodes.items()}
    except Exception:
        logging.getLogger(__name__).warning(
            "read_node_states failed for task_id=%s (non-fatal to the caller, but a resume that "
            "can't read real state should NOT guess -- caller must treat an empty/partial result "
            "conservatively, never as \"nothing is satisfied yet\" confirmation)",
            task_id, exc_info=True,
        )
        return {}
    finally:
        conn.close()


def read_main_chat_history(session_id: str, limit: int = 200) -> list[dict]:
    """Real, durable read-back for the main chat's own conversation --
    the counterpart to append_project_memory(event_type=
    'main_chat_message', ...). Ordered oldest-first, ready to hand
    straight to the frontend to rebuild the visible transcript after a
    reload (or restore conversation_history for run_turn() after a
    server restart, which previously lost it completely along with
    everything else _sessions held only in memory).
    """
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT detail, created_at
            FROM agent_memory_events
            WHERE event_type = 'main_chat_message' AND detail->>'session_id' = %s AND active = true
            ORDER BY id ASC
            LIMIT %s
            """,
            (session_id, limit),
        )
        rows = cur.fetchall()
        return [
            {"role": r["detail"]["role"], "content": r["detail"]["content"], "task_id": r["detail"].get("task_id")}
            for r in rows
        ]
    finally:
        conn.close()


def ask_operator(question: str) -> dict:
    """Ends the current turn with a question instead of a delegation.
    Just a plain marker -- the loop's own phase-two step is what
    actually decides to call this rather than proceed.
    """
    return {"action": "ask_operator", "question": question}


# 5,000-char / first-30%-last-30% rule (§2.6): the specialist's own
# summary and conclusion typically matter more than whatever's in the
# middle, so truncate from the middle rather than from one end.
_SPECIALIST_RESULT_CHAR_LIMIT = 5000


def fold_specialist_result(text: str) -> str:
    if len(text) <= _SPECIALIST_RESULT_CHAR_LIMIT:
        return text
    keep_each_side = int(_SPECIALIST_RESULT_CHAR_LIMIT * 0.3)
    head = text[:keep_each_side]
    tail = text[-keep_each_side:]
    dropped = len(text) - (2 * keep_each_side)
    return f"{head}\n\n[... {dropped} characters truncated ...]\n\n{tail}"


# P12 Tier B/C item 28 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
# A Finding 27): real, confirmed gap -- fold_specialist_result()'s middle-truncation is what
# classify_root_cause() reads as its own failure_summary (VerificationResult.notes, via
# await_verification()); the "first 30%/last 30%" heuristic is reasonable for a specialist's own
# free-form narrative, but notes is frequently a concatenation of a summary PLUS a specific error
# detail (an install traceback, a concrete file:line reference) -- for any notes text long enough
# to truncate, that diagnostic detail can land in the dropped middle 40%, leaving root-cause
# classification (and the next round's own revise_contract_from_verification(), which also reads
# notes) working from a summary with a hole exactly where the actionable content would be.
_DIAGNOSTIC_LINE_RE = re.compile(
    r"^\s*(File \"[^\"]+\", line \d+|[\w/\.\-]+\.py:\d+|\w*Error\b.*|\w*Exception\b.*|Traceback \(most recent call last\):)",
    re.MULTILINE,
)


# Phase 30 §26 follow-up (2026-08-04): real, confirmed gap -- fold_verification_notes() shared
# _SPECIALIST_RESULT_CHAR_LIMIT (5,000 chars) with fold_specialist_result(), even though its own
# whole purpose (per its own docstring) is preserving diagnostic content for
# TaskContract.previous_round_raw_failure_text (§26 item 3's own "verbatim, byte-for-byte" field
# Build's next-round prompt now directly relies on) -- a "smart" truncation that still drops most
# of a genuinely long install traceback is not actually the verbatim guarantee item 3 promises.
# Decoupled from the general-purpose specialist-summary cap (which stays at 5,000 -- a reasonable
# bound for ordinary free-form prose, not implicated in this gap) and raised to a size generous
# enough that truncation becomes a rare, genuinely-pathological-input case rather than a routine
# occurrence for any realistic install error / traceback / Code-Review finding text.
_VERIFICATION_NOTES_CHAR_LIMIT = 50_000


def fold_verification_notes(text: str) -> str:
    """The structure-aware sibling of fold_specialist_result(), for VerificationResult.notes
    specifically (never used for a specialist's own general free-form summary -- that stays on
    the original, simpler function unchanged). Same first-30%/last-30% shape when nothing
    diagnostic falls in the dropped middle -- but any traceback/file:line/exception-shaped line
    that WOULD have been silently dropped is preserved verbatim in its own labeled section,
    regardless of its original position, so classify_root_cause() and
    revise_contract_from_verification() never lose the one piece of text that's actually
    actionable. Uses its own, much larger _VERIFICATION_NOTES_CHAR_LIMIT (see that constant's own
    docstring) -- never the general-purpose _SPECIALIST_RESULT_CHAR_LIMIT.
    """
    if len(text) <= _VERIFICATION_NOTES_CHAR_LIMIT:
        return text
    keep_each_side = int(_VERIFICATION_NOTES_CHAR_LIMIT * 0.3)
    head = text[:keep_each_side]
    tail = text[-keep_each_side:]
    middle_start, middle_end = keep_each_side, len(text) - keep_each_side
    middle = text[middle_start:middle_end]
    dropped = len(middle)

    # Scanned line-by-line (not via .findall() on the whole middle) so each preserved entry is
    # the FULL matched line's own real text, not just the capturing group's fragment -- a bare
    # "line 42" with no surrounding "File "..."," context would be far less useful.
    preserved_lines = []
    for line in middle.splitlines():
        if _DIAGNOSTIC_LINE_RE.match(line) and line.strip() not in preserved_lines:
            preserved_lines.append(line.strip())

    if not preserved_lines:
        return f"{head}\n\n[... {dropped} characters truncated ...]\n\n{tail}"
    preserved_block = "\n".join(preserved_lines)
    return (
        f"{head}\n\n[... {dropped} characters truncated, but the following diagnostic line(s) "
        f"from the truncated middle were preserved: ...]\n{preserved_block}\n\n{tail}"
    )


async def delegate_to_specialist(contract: TaskContract) -> SpecialistOutput:
    """Calls registry.get(specialist_type).run(contract) -- exactly what
    the registry mechanism (Phase 5) was built to make possible. This is
    the ONLY place in manager/ that touches the specialists package,
    and it does so exclusively through registry.get(), never a direct
    specialist import.
    """
    specialist = registry.get(contract.specialist_type)
    try:
        return await specialist.run(contract)
    except PartialTaskFailure as exc:
        pause = await asyncio.to_thread(
            handle_partial_task_failure,
            str(contract.task_id), contract, exc.completed_steps, original=exc.original,
        )
        raise pause from exc


async def run_code_review_diff(
    contract: TaskContract, module_name: str, self_report_uncertain: str | None = None,
) -> SpecialistOutput:
    """Phase 15 (§19.5): wires Code-Review into the real per-task
    closing flow for module_dev tasks -- fully built and tested since
    Phase 10, but never actually consulted mid-loop until now. Reuses
    the exact same "diff_module:<name>" input convention Phase 10
    already established; no new contract shape needed. Routes through
    the registry, same discipline as delegate_to_specialist() -- this
    stays the only other place in manager/ that touches the specialists
    package.

    Real, general bug found live (2026-08-07, task041, real task_id
    11e44739-a0b4-4e61-8e8f-8eb07490af13, HUMAN_DECISION deep-push): the
    `_ALREADY_SATISFIED_BY_COLLISION_MARKER` this same file already threads to testing_qa's own
    reproduction-target extraction (see `_extract_collision_confirmed_field_names` above, fixed
    for task 004) was NEVER threaded to Code-Review -- confirmed live, 4 consecutive relaunches
    (v6-v9): Build correctly wrote `project_count` on res.partner, this file's own real, deep,
    functioning collision-autofix correctly detected it already exists as a REAL, LIVE, FUNCTIONING
    field (owned by `mis_base_extend`, confirmed via direct SSH source read: a real
    `compute='_compute_project_count'` method that already returns exactly this partner's project
    count, not a dead/inert field), and correctly stripped the round's own redundant redeclaration
    -- but Code-Review, seeing only the resulting (correctly-empty-of-this-field) models.py diff
    with no idea WHY the field is absent, flagged it as a "completely missing" blocking finding,
    all 4 times, wrongly escalating a genuinely-satisfied round to ask_operator as if it were a real
    model-competency failure. `Specialist.run()` (specialists/base.py) is a strict one-arg
    Protocol shared by every specialist -- rather than break that interface with a code-review-only
    kwarg, the collision-confirmed field list is appended directly to `review_contract.goal`
    (the exact same mechanism manager/loop.py already uses for the "NOT yet in scope" /
    already-correct-omission clarifying sentences), so Code-Review reads it as ordinary goal text
    with no interface change needed.
    """
    review_contract = contract.model_copy(
        update={
            "specialist_type": SpecialistType.code_review,
            "capability_class": CapabilityClass.readonly_investigation,
            "inputs": [f"diff_module:{module_name}"],
            "validation_by": None,
        }
    )
    collision_fields = _extract_collision_confirmed_field_names(self_report_uncertain)
    if collision_fields:
        review_contract = review_contract.model_copy(update={
            "goal": (
                review_contract.goal
                + f"\n\nIMPORTANT: {collision_fields} already exist as REAL, LIVE, FUNCTIONING "
                f"field(s) on the actual target model (confirmed independently, not a model claim) "
                f"-- this round's own code correctly did NOT redeclare them, to avoid colliding with "
                f"the real existing field(s). Their absence from this diff's models.py is CORRECT "
                f"and COMPLETE, not a missing-implementation defect. Do NOT flag {collision_fields} "
                f"as missing, unimplemented, or incomplete."
            )
        })
    specialist = registry.get(SpecialistType.code_review)
    return await specialist.run(review_contract)


def fold_code_review_findings(
    result: "VerificationResult", code_review_output: SpecialistOutput | None,
) -> "VerificationResult":
    """Phase 22 (2026-07-23): factored out of await_verification()'s own
    body (where it's still called, unchanged, for every existing
    caller) so manager/loop.py's own round loop can also call it
    directly, AFTER running Code-Review's mid-round diff review and
    Testing-QA's own verification CONCURRENTLY instead of sequentially
    -- confirmed safe to do because this folding step never read
    anything except code_review_output.detail.get("findings"), always
    AFTER validator_output/result already existed, never influencing
    the validator_contract Testing-QA itself was given. A no-op when
    code_review_output is None (nothing to fold), exactly as before.
    """
    if code_review_output is None:
        return result
    # P12 Tier S item 2 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
    # a real, confirmed fail-open gap -- a structural failure (Code-Review's own generation
    # exhausted retries, or it never had a real target/could not even read the codebase; see
    # specialists/code_review/specialist.py's run()) used to return detail={}, and
    # detail.get("findings", []) silently reads that as "reviewed, zero findings" -- the exact
    # same outcome as a genuinely clean review. Checked FIRST, before the findings-list logic
    # below, so a structural failure can never be silently folded into an approval.
    if code_review_output.detail.get("structural_failure"):
        kind = code_review_output.detail.get("structural_failure_kind", "unknown")
        return result.model_copy(
            update={
                "passed": False,
                "notes": (
                    f"{result.notes} Code-Review could not actually produce a real review "
                    f"({kind}) -- treated as a hard fail, not silently folded in as \"reviewed "
                    f"cleanly, found nothing.\""
                ),
            }
        )
    blocking = [f for f in code_review_output.detail.get("findings", []) if f.get("severity") == "blocking"]
    if not blocking:
        return result
    # P12 Tier S/A item 7: real arbitration/triangulation instead of a blind, unconditional
    # single-vote override (B Finding 4: "single-vote veto with no confidence weighting"; B
    # Finding 11: "specialists run fully blind to each other, fold happens after both are
    # already finalized with no reconsideration path"). Doesn't change the safety-conservative
    # OUTCOME here -- a real, named Code-Review objection still fails the round; there is no
    # third, sandbox-level gate yet to deterministically break the tie in Code-Review's own
    # favor, and silently trusting Testing/QA's self-report over a specific, concrete finding
    # would be unsafe -- but it DISTINGUISHES which kind of override this was, via the new
    # `gates_disagree` field, so should_escalate_to_operator() (manager/replanning.py) can route a
    # genuine two-gate disagreement differently from the much more common case (an ordinary,
    # uncontested blocking finding on top of an already-weak/uncorroborated Testing/QA pass).
    strongly_corroborated = result.passed and result.reproduction_confirmed and not result.spot_check_mismatch
    explanations = "; ".join(f.get("explanation", "") for f in blocking)
    update = {
        "passed": False,
        "notes": f"{result.notes} Code-Review found {len(blocking)} blocking issue(s): {explanations}",
    }
    if strongly_corroborated:
        update["gates_disagree"] = True
        update["notes"] += (
            " [Arbitration: Testing/QA's own independent reproduction was confirmed and its "
            "spot-check matched its self-report -- this is a genuine two-gate disagreement, "
            "not a reinforced finding.]"
        )
    return result.model_copy(update=update)


def apply_unverified_shape_confidence_gate(
    result: "VerificationResult", contract: TaskContract, code_review_output: SpecialistOutput | None,
) -> "VerificationResult":
    """P12 Tier A item 11 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
    unverified_shapes_targeted() (contracts/verifier_registry.py) has existed since Phase 25E as
    a purely informational flag -- attached to detail, surfaced on the trace, but never once
    affecting `passed`. A goal targeting one of the six shapes with no registered ground-truth
    check (computed_field, onchange_behavior, selection_options, search_view_filter,
    ir_cron_schedule, smart_button_counter) could claim complete on nothing but its own
    self-report and nobody would ever know the claim was structurally unverifiable. Interim,
    cheaper step (per this item's own stated shape, ahead of building all six real verifiers):
    only downgrades when there is ALSO no Code-Review pass this round to independently
    corroborate the claim -- a round that already has Code-Review's own eyes on it isn't relying
    on the self-report alone, so it's left untouched. Narrow by design: only overrides an
    otherwise-PASSING result (nothing to gate on an already-failing one), and only forces
    `spot_check_mismatch=True` (never silently invents a different failure reason) so the
    override is visible and consistent with `passed=False`.

    Real, confirmed bug found live (2026-08-03) and fixed by extracting this into its own
    function, called explicitly by each real caller AFTER its own true `code_review_output` is
    known (never from inside `await_verification()`/`fold_code_review_findings()` themselves,
    where the concurrent-execution path's own deliberate `None` placeholder made this check
    structurally blind to a real, concurrently-running Code-Review pass) -- see
    `await_verification()`'s own docstring for the full account of what this closes.
    """
    unverified_shapes = unverified_shapes_targeted(contract)
    if unverified_shapes and code_review_output is None and result.passed:
        result = result.model_copy(update={
            "passed": False,
            "spot_check_mismatch": True,
            "notes": (
                f"{result.notes} [Confidence gate: this goal targets shape(s) with no "
                f"registered ground-truth verification ({', '.join(unverified_shapes)}), and no "
                f"Code-Review pass ran this round to independently corroborate the self-report -- "
                f"forcing spot_check_mismatch=True rather than trusting the claim alone.]"
            ),
        })
    return result


async def await_verification(
    contract: TaskContract,
    build_output: SpecialistOutput,
    client,
    model: str,
    code_review_output: SpecialistOutput | None = None,
    fallback_module_name: str | None = None,
    fallback_db: str | None = None,
) -> "VerificationResult":  # noqa: F821 -- imported lazily below to avoid a cycle
    """Blocks on / obtains a specialist's INDEPENDENTLY-verified result.
    Never trusts build_output.claims_complete directly -- always
    delegates to whichever specialist contract.validation_by names
    (almost always "testing_qa") and builds the actual VerificationResult
    from THAT specialist's own independent report.

    Requires contract.validation_by to be a real specialist name, never
    None -- manager.loop.run_turn() (Phase 12) never calls this for a
    readonly_investigation task (validation_by=None for that shape;
    see manager.loop.select_specialist_and_validator()), building a
    pass-through VerificationResult from build_output directly instead.

    Resolved as of Phase 11: when contract.validation_by == "testing_qa",
    the real TestingQASpecialist supplies a genuine, independently
    computed spot_check_mismatch (tools_odoo/spot_check.py's real,
    non-LLM coverage-diff check) -- read directly from its own
    detail dict, never hardcoded. For any other/older validator that
    doesn't populate these keys, this falls back to deriving them from
    claims_complete alone (the pre-Phase-11 behavior), so this function
    never breaks against a validator that hasn't been updated.

    A real gap found and fixed during Phase 13's own end-to-end testing:
    TestingQASpecialist needs a "verify_module:<name>:<db>" entry in
    contract.inputs to know what to verify (Phase 11's own convention),
    but contract itself never carries that -- the Build specialist only
    learns the real module name/db it used at RUN time (module names are
    derived from the goal text, not chosen by the Manager up front).
    Fixed here: when the validator is testing_qa and build_output.detail
    actually has a module_name (and db), a copy of the contract with that
    entry appended is what actually gets passed to the validator -- the
    original contract object itself is never mutated.

    A second real gap found live (Phase 16 QA pass): when a retry round
    gets routed through select_specialist_for_retry() to code_review
    (2 consecutive rounds sharing the same finding theme), THAT round's
    own build_output is CodeReviewSpecialist's output -- its detail is
    {mode, audited_path, files_read, findings}, never module_name/db,
    since Code-Review reviews an existing diff rather than scaffolding
    one. Every such round then reached testing_qa with no verify_module:
    entry at all and failed instantly and identically, wasting a round
    (confirmed live: a real service.record task's round 4 failed this
    way in under a second, right after two rounds of genuine reproduction
    work). fallback_module_name/fallback_db (the caller's own running
    memory of the last round that WAS Build) are used only when this
    round's own build_output doesn't carry them -- the module Code-Review
    is looking at is still the same one on disk from that last Build.

    Phase 15 (§19.5): an optional code_review_output, when supplied,
    folds in ANY blocking finding as a reason to fail this round --
    even if Testing/QA's own reproduction independently passed. A
    module that works but has a hardcoded credential or an unguarded
    sudo() bulk-delete is not actually done (exactly Phase 13's real
    task-2 finding: Testing/QA's field-existence check had no way to
    catch the access.csv bug Code-Review caught).
    """
    from contracts.schema import VerificationResult

    # Real, confirmed regression found live (Phase 18 dynamic verification
    # pass, task 3): when Build's own pre-write validator rejects a round
    # (claims_complete=False, no module_name in detail -- nothing was
    # ever written this round, see specialists/build/specialist.py's own
    # except-ValueError block), this function used to still call
    # testing_qa anyway. testing_qa has genuinely nothing to check (no
    # verify_module: entry, since module_name is absent) and returns a
    # generic, useless "nothing concrete to verify" summary -- which
    # then became THIS round's verification.notes below, silently
    # DISCARDING Build's own real, specific rejection reason (e.g. "the
    # manifest is missing 'hr'/'product' in depends") on its way into
    # the next round's rules. Confirmed live: a task extending an
    # existing module repeated the identical missing-dependency mistake
    # for 5 straight rounds, because every round's own real cause was
    # being thrown away and replaced with a useless generic message
    # before ever reaching the next round's contract. Fixed generally,
    # same pattern already used for the readonly_investigation case
    # (manager/loop.py): when Build itself already failed with nothing
    # to verify, build_output.summary (the one real, current reason) IS
    # the verification result -- never route it through a validator
    # that has nothing real to check.
    # Real, confirmed bug found live (2026-08-08, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    # flagship run, service_ticket_model node, rounds 1 and 3 both): the guard above only ever
    # checked for "module_name" missing entirely -- but specialists/build/specialist.py's own
    # sandbox_failed early-return (`_sandbox_preflight()`'s own caller) DOES include a real
    # "module_name" in its detail (the sandbox write genuinely happened) while NEVER including
    # "db" at all, by design (a sandbox failure means the REAL target db was never touched, so
    # there is no db to report). On the very first round of a brand-new constraint node,
    # fallback_db is still None (no prior successful Build round this session to fall back to
    # -- see this function's own docstring on fallback_module_name/fallback_db), so
    # `db = build_output.detail.get("db") or fallback_db` resolves to None below, `if
    # module_name and db:` is false, no `verify_module:` entry is ever added, and the round
    # silently reaches testing_qa with a genuinely EMPTY inputs list -- the exact same failure
    # class this whole guard exists to prevent, just triggered by a missing "db" instead of a
    # missing "module_name". Confirmed live: Build's own real, specific sandbox error was
    # discarded both times, replaced with the useless generic "nothing concrete to verify"
    # message, escalating to a human decision for a problem whose real cause (visible in
    # build_output.summary the whole time) was never even shown.
    has_usable_module_name = bool(build_output.detail.get("module_name") or fallback_module_name)
    has_usable_db = bool(build_output.detail.get("db") or fallback_db)
    if (
        contract.specialist_type != SpecialistType.code_review
        and not build_output.claims_complete
        and not (has_usable_module_name and has_usable_db)
    ):
        return VerificationResult(
            task_id=contract.task_id,
            passed=False,
            reproduction_confirmed=False,
            uncovered_paths=[],
            coverage_diff="",
            spot_check_mismatch=False,
            # P12 Tier B/C item 28: VerificationResult.notes specifically uses the
            # structure-aware fold, never the plain one -- see fold_verification_notes()'s own
            # docstring for why.
            notes=fold_verification_notes(build_output.summary),
        )

    validator_type = SpecialistType(contract.validation_by)
    validator = registry.get(validator_type)

    validator_contract = contract
    if validator_type == SpecialistType.testing_qa:
        module_name = build_output.detail.get("module_name") or fallback_module_name
        db = build_output.detail.get("db") or fallback_db
        extra_inputs = []
        if module_name and db:
            extra_inputs.append(f"verify_module:{module_name}:{db}")
        collision_fields = _extract_collision_confirmed_field_names(
            build_output.detail.get("self_report_uncertain")
        )
        if collision_fields:
            extra_inputs.append(f"collision_confirmed_fields:{collision_fields}")
        if extra_inputs:
            validator_contract = contract.model_copy(update={"inputs": [*contract.inputs, *extra_inputs]})

    validator_output = await validator.run(validator_contract)

    passed = bool(validator_output.claims_complete)
    result = VerificationResult(
        task_id=contract.task_id,
        passed=passed,
        reproduction_confirmed=bool(validator_output.detail.get("reproduction_confirmed", passed)),
        uncovered_paths=list(validator_output.detail.get("uncovered_paths", [])),
        coverage_diff=str(validator_output.detail.get("coverage_diff", "")),
        spot_check_mismatch=bool(validator_output.detail.get("spot_check_mismatch", False)),
        # P12 Tier B/C item 28: same structure-aware fold as the early-return case above.
        notes=fold_verification_notes(validator_output.summary),
        # Phase 18 (§22.10): BuildSpecialist computes this deterministically
        # from its own old/new file diff (manager.replanning.
        # compute_regressed_constraints()) -- testing_qa/code_review never
        # compute it themselves, since only Build's own round actually has
        # the prior-vs-new file content to compare. Read from build_output,
        # not validator_output, for exactly that reason.
        regressed_constraints=list(build_output.detail.get("regressed_constraints", [])),
    )

    # Real, confirmed bug found live (2026-08-03, task019's own real re-test, task012 hit the
    # identical shape earlier the same day): the P12 Tier A item 11 confidence gate used to live
    # right here, checking `code_review_output is None` to decide whether a Code-Review pass had
    # corroborated this round. That's correct for a caller that already has Code-Review's real
    # result in hand -- but Phase 22's own concurrent-execution optimization (manager/loop.py)
    # deliberately calls `await_verification()` with `code_review_output=None` as a placeholder,
    # BEFORE Code-Review's own concurrently-running result is available, folding the real result
    # in SEPARATELY afterward via `fold_code_review_findings()` once both specialists return. A
    # gate check living inside `await_verification()` itself is structurally blind to that later
    # fold -- it always saw `None` for every concurrently-run round, regardless of whether
    # Code-Review genuinely ran and produced real findings. Confirmed live: task019's real trace
    # showed Code-Review actually running and returning real, specific blocking findings on both
    # escalating rounds, yet the gate still fired claiming "no Code-Review pass ran this round."
    # Moved to `apply_unverified_shape_confidence_gate()` below, a standalone function each real
    # caller now invokes explicitly AFTER its own real `code_review_output` is definitively known
    # (immediately after `fold_code_review_findings()`, in both the sequential and concurrent
    # paths in manager/loop.py) -- never guessed at from inside this function again.
    result = fold_code_review_findings(result, code_review_output)

    # Real, confirmed gap found on a deliberate Phase 18 re-audit against
    # its own plan (§22.10): regressed_constraints was computed and
    # recorded on `result` above, but nothing ever factored it into
    # `passed` -- a round that silently broke an earlier-satisfied
    # constraint while fixing the current one would still be accepted as
    # a genuine pass, exactly the failure shape §22.10 exists to catch
    # "the same round it happens," not several rounds later. This is
    # also load-bearing for §22.9's own explicit requirement (a
    # sub-contract's result is only accepted once the constraint(s) it
    # was supposed to preserve are confirmed still holding) --
    # _run_decomposed_task() (manager/loop.py) only ever checks this same
    # top-level `passed` flag before advancing to the next sub-contract.
    # Mirrors the code_review_output blocking-finding pattern immediately
    # above, deliberately, rather than a new, separate mechanism.
    if result.regressed_constraints:
        result = result.model_copy(
            update={
                "passed": False,
                "notes": (
                    f"{result.notes} Regression re-check found {len(result.regressed_constraints)} "
                    f"previously-satisfied constraint(s) broken this round: {result.regressed_constraints}."
                ),
            }
        )

    if not result.passed:
        from manager.learning import classify_root_cause
        # Real infra change, 2026-07-22: `model` here is the CALLER's own
        # (classifier_model, threaded in for whatever else this function
        # might need it for -- in practice, nothing else in this function
        # body uses `model` at all, confirmed by direct inspection). Root-
        # cause classification is a pure 4-way label task, live-benchmarked
        # the same night: qwen3.6-27b truncates on invisible thinking
        # tokens even at the real 1500-token budget; GPU Worker 03's
        # resident 9B model (thinking disabled server-side) answers
        # correctly in under a second. See manager/loop.py's own
        # FAST_EXTRACTION_MODEL comment for the full rationale -- same
        # literal value duplicated here (not imported) to avoid a circular
        # import between manager.tools and manager.loop.
        root_cause = await classify_root_cause(
            failure_summary=result.notes, client=client, model="qwen3-9b-fast-extraction",
            task_id=str(contract.task_id),
        )
        result = result.model_copy(update={"root_cause": root_cause})

    return result
