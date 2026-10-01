"""Phase 15 (§19.4): the outer task-list layer, persisted through the
same append-only `agent_memory_events` log every other piece of durable
state in this system uses -- no second source of truth. Every plan_item's
FULL `TaskContract` travels forward in each `plan_item_status` row's own
`detail['contract']`, so `next_runnable_item()` can return a real,
reconstructed `TaskContract` from persisted state alone, with no
external dict of in-memory contracts required to call it.

`create_task_plan()` and `mark_plan_item_status()` are the ONLY
functions anywhere allowed to write a `plan_created`/`plan_item_status`
row -- grep-verified (tests/test_task_plan.py), the same discipline as
`append_project_memory()`'s own single-INSERT-path proof.
"""

from __future__ import annotations

import uuid

import psycopg2
import psycopg2.extras

from contracts.schema import TaskContract
from infra.settings import load_postgres_settings
from manager.tools import append_project_memory

_VALID_STATUSES = {"pending", "in_progress", "passed", "failed", "blocked"}


def _get_conn():
    s = load_postgres_settings()
    return psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)


def create_task_plan(operator_request: str, item_contracts: list[TaskContract]) -> str:
    """Writes one plan_created row plus one plan_item_status row
    (status='pending') per item, returns the new plan_id. Every
    item_contract must already have its own `plan_item_id` set by the
    caller (a simple, caller-chosen id like "item-1") and, if it
    depends on an earlier item, that earlier item's `plan_item_id` in
    its own `blocked_by` list -- `create_task_plan` only mints and
    stamps the shared `plan_id` across all of them.
    """
    for contract in item_contracts:
        if not contract.plan_item_id:
            raise ValueError(
                f"every item_contract passed to create_task_plan() must already have its own "
                f"plan_item_id set -- got one with none (goal={contract.goal!r})"
            )

    plan_id = str(uuid.uuid4())
    stamped = [c.model_copy(update={"plan_id": plan_id}) for c in item_contracts]

    append_project_memory(
        event_type="plan_created",
        actor="manager",
        summary=f"Plan created for: {operator_request[:200]}",
        detail={
            "plan_id": plan_id,
            "operator_request": operator_request,
            "items": [
                {"plan_item_id": c.plan_item_id, "blocked_by": c.blocked_by, "goal": c.goal}
                for c in stamped
            ],
        },
    )

    for contract in stamped:
        mark_plan_item_status(
            plan_id, contract.plan_item_id, "pending",
            detail={"contract": contract.model_dump(mode="json")},
        )

    return plan_id


def mark_plan_item_status(
    plan_id: str, plan_item_id: str, status: str, detail: dict | None = None
) -> int:
    """The only function that writes a plan_item_status transition
    after creation. Inherits the previously-persisted contract from
    the current latest row for this item (if any) and carries it
    forward, merged with whatever new detail this call supplies --
    so the view's "latest row wins" projection always has the full
    contract available, without requiring every caller to re-supply it
    on every single status change.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid plan item status: {status!r} -- must be one of {sorted(_VALID_STATUSES)}")

    prior = _read_latest_item_row(plan_id, plan_item_id)
    merged_detail = dict(prior["detail"]) if prior and prior.get("detail") else {}
    merged_detail.update(detail or {})
    merged_detail["plan_id"] = plan_id
    merged_detail["plan_item_id"] = plan_item_id
    merged_detail["status"] = status

    return append_project_memory(
        event_type="plan_item_status",
        actor="manager",
        summary=f"Plan {plan_id} item {plan_item_id}: {status}",
        detail=merged_detail,
    )


def _read_latest_item_row(plan_id: str, plan_item_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT * FROM active_task_plan_items
            WHERE plan_id = %s AND plan_item_id = %s
            """,
            (plan_id, plan_item_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_plan_items(plan_id: str) -> list[dict]:
    """Public read helper -- for the chat UI's GET /api/plan/{plan_id}
    checklist view, and for next_runnable_item()'s own internal use.
    """
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM active_task_plan_items WHERE plan_id = %s", (plan_id,))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def list_all_recent_plan_items(limit: int = 200) -> list[dict]:
    """Phase 16: for GET /api/tasks -- every outer-plan item's latest
    status across EVERY plan, not scoped to one plan_id. Used by the
    dashboard to include plan items alongside standalone tasks.
    """
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM active_task_plan_items ORDER BY created_at DESC LIMIT %s", (limit,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def next_runnable_item(plan_id: str) -> TaskContract | None:
    """Reads active_task_plan_items and returns the first item whose
    blocked_by are all 'passed' and whose own status is still
    'pending' -- reconstructed as a real TaskContract from its own
    persisted detail, not from any externally-held dict. None if
    everything is done, or everything remaining is genuinely blocked.
    """
    rows = list_plan_items(plan_id)
    status_by_item = {row["plan_item_id"]: row["status"] for row in rows}

    for row in rows:
        if row["status"] != "pending":
            continue
        contract = TaskContract.model_validate(row["detail"]["contract"])
        if all(status_by_item.get(dep) == "passed" for dep in contract.blocked_by):
            return contract
    return None
