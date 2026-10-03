#!/usr/bin/env python3
"""Phase 26D (docs/planning/PHASE26_OMA_VERIFICATION_INTEGRITY_HARDENING_2026-07-27.md,
section 26D): operator-facing CLI to list and clean up abandoned `oma_*` scaffold
modules on a real Odoo target.

Real, evidenced problem this closes: `manager/loop.py`'s `cleanup_module_from_failed_round()`
and `manager/compensations.py`'s compensating-action cleanup both only fire for a task's
OWN active run (a round whose install succeeded but was rejected in the same round loop,
or a task cut off mid-sequence). Neither fires for a task abandoned outside those exact
code paths -- confirmed live (2026-08-04): 38 separate `oma_*fieldjob*` modules
simultaneously `state=installed` in the real, shared `odoo16_dev` database, each from a
DIFFERENT task_id (slugify_module_name() gives every new task_id a brand-new module name
even when the goal text is nearly identical to an earlier abandoned attempt), none ever
uninstalled. New tasks then build against a `project.fieldjob` model whose real, live
registry has been mutated by dozens of unrelated, never-cleaned-up prior attempts.

Never destructive by default: `uninstall --stale` requires an explicit `--confirm` flag;
without it, only a dry-run preview is printed. Per this phase's own explicit design
decision (confirmed with the project owner before the first real run): manual-only, dry-run-by-
default, forever -- no automatic/scheduled invocation of this tool exists or should exist.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys

import psycopg2
import psycopg2.extras

from infra.odoo_settings import load_odoo_settings
from infra.redis_client import get_redis_client
from infra.settings import load_postgres_settings
from manager.dashboard import _outcome_row_for_task
from manager.escalations import list_pending_escalations
from manager.replanning import get_latest_checkpoint
from manager.sign_off import list_pending_task_ids
from tools_odoo.module_dev.toolchain import remove_scaffolded_module, uninstall_module
from tools_odoo.odoo_schema_client import _get_or_create_shared_key, _models_proxy

_OMA_PREFIX = "oma_%"


def _slugify_module_name(goal: str, task_id: str) -> str:
    """Duplicated, deliberately -- not imported from
    specialists.build.specialist, which pulls in the entire Build
    specialist module (heavy, unrelated import graph) just for one pure
    function. Kept byte-for-byte identical to slugify_module_name()
    there; if that function's derivation ever changes, this one must
    change with it (both are exercised by the shared reverse-index test
    to catch drift).
    """
    words = re.findall(r"[a-zA-Z0-9]+", goal.lower())[:4]
    stem = "_".join(words) if words else "task"
    suffix = hashlib.sha256(task_id.encode()).hexdigest()[:8]
    return f"oma_{stem}_{suffix}"


def _pg_conn():
    s = load_postgres_settings()
    return psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)


def list_oma_modules(db: str, login: str = "Admin") -> list[dict]:
    """One bulk XML-RPC search_read against ir.module.module -- every
    oma_*-prefixed module row, install state + write_date. Deliberately
    NOT one odoo-bin-shell call per module (1118+ modules confirmed
    live on 2026-08-04 -- a per-module shell boot at 8-20s each would
    make this tool unusable).
    """
    uid, key = _get_or_create_shared_key(db, login)
    models = _models_proxy(db)
    # Odoo's ORM `like` compiles to raw SQL LIKE, where a bare `_` is a
    # single-character WILDCARD, not a literal underscore -- "oma_" as a
    # `like` domain therefore also matches e.g. "automation" (contains
    # "oma" + any char) and "domain" (same). Escaped here so only real
    # oma_*-prefixed modules match; confirmed live (2026-08-04): the
    # unescaped filter false-matched test_base_automation, web_domain_
    # field, web_widget_domain_editor_dialog.
    rows = models.execute_kw(
        db, uid, key, "ir.module.module", "search_read",
        [[("name", "=like", "oma\\_%")]],
        {"fields": ["name", "state", "write_date"]},
    )
    return sorted(rows, key=lambda r: r["name"])


def build_module_task_index() -> dict[str, str]:
    """module_name -> task_id, built from real, durable Postgres rows --
    never a guess. Two sources (manager/loop.py's task_plan items
    already materialize module_name in `detail`; standalone tasks don't,
    so their module_name is recomputed from the same deterministic
    derivation slugify_module_name() itself uses, keyed off the durable
    `task_created.summary` goal text)."""
    conn = _pg_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        index: dict[str, str] = {}

        cur.execute("SELECT plan_item_id, task_id, detail FROM active_task_plan_items WHERE detail ? 'module_name'")
        for row in cur.fetchall():
            module_name = row["detail"].get("module_name")
            if module_name:
                index[module_name] = row["task_id"]

        cur.execute(
            "SELECT DISTINCT ON (task_id) task_id, summary FROM agent_memory_events "
            "WHERE event_type = 'task_created' ORDER BY task_id, id ASC"
        )
        for row in cur.fetchall():
            if not row["summary"]:
                continue
            module_name = _slugify_module_name(row["summary"], row["task_id"])
            index.setdefault(module_name, row["task_id"])

        return index
    finally:
        conn.close()


def classify_staleness(task_id: str, redis_client, pending_ids: set[str]) -> tuple[bool, str]:
    """Conservative by construction (§26D.5's own regression-gate
    requirement: NEVER classify a module belonging to an active,
    resumable, or still-decision-pending task as stale). Any one live
    signal at all -- a resumable checkpoint, live Redis state, a
    pending escalation/sign-off, or a real passing outcome -- excludes
    the module. Only genuinely abandoned tasks (no live signal
    whatsoever) are ever flagged stale.
    """
    if get_latest_checkpoint(task_id) is not None:
        return False, "has a live, resumable round checkpoint"
    if redis_client.get(f"oma:task:{task_id}:state") is not None:
        return False, "live Redis task state present (running or paused:*)"
    if task_id in pending_ids:
        return False, "pending sign-off/escalation decision outstanding"
    if _outcome_row_for_task(task_id) is not None:
        return False, "task has a real passing outcome row -- genuinely completed work, never swept"
    return True, "no live checkpoint, no live Redis state, no pending decision, no passing outcome -- genuinely abandoned"


def _annotated_rows(db: str) -> list[dict]:
    modules = list_oma_modules(db)
    task_index = build_module_task_index()
    redis_client = get_redis_client()
    pending_ids = set(list_pending_task_ids(redis_client))
    pending_ids.update(e["task_id"] for e in list_pending_escalations(redis_client) if e.get("task_id"))

    out = []
    for m in modules:
        task_id = task_index.get(m["name"])
        row = {"name": m["name"], "state": m["state"], "write_date": m["write_date"], "task_id": task_id}
        if task_id is None:
            row["stale"] = False
            row["reason"] = "task_id could not be resolved -- never swept, conservative default"
        else:
            row["stale"], row["reason"] = classify_staleness(task_id, redis_client, pending_ids)
        out.append(row)
    return out


def cmd_list(args: argparse.Namespace) -> None:
    rows = _annotated_rows(args.db)
    if args.stale:
        rows = [r for r in rows if r["stale"]]
    print(f"{'MODULE':<45} {'STATE':<12} {'TASK_ID':<38} REASON")
    for r in rows:
        print(f"{r['name']:<45} {r['state']:<12} {str(r['task_id']):<38} {r['reason']}")
    print(f"\n{len(rows)} module(s) shown.")


def cmd_audit_ir_model_data(args: argparse.Namespace) -> None:
    uid, key = _get_or_create_shared_key(args.db, "Admin")
    models = _models_proxy(args.db)
    count = models.execute_kw(
        args.db, uid, key, "ir.model.data", "search_count",
        [[("module", "=like", "oma\\_%")]],
    )
    print(f"ir_model_data rows owned by an oma_* module: {count}")


def cmd_uninstall(args: argparse.Namespace) -> None:
    if args.module_name:
        _do_uninstall(args.db, args.module_name)
        return

    if not args.stale:
        print("uninstall requires either a module_name or --stale.", file=sys.stderr)
        sys.exit(2)

    rows = [r for r in _annotated_rows(args.db) if r["stale"]]
    if not rows:
        print("No stale modules found.")
        return

    if not args.confirm:
        print(f"Would uninstall {len(rows)} module(s):")
        for r in rows:
            print(f"  {r['name']}  (task {r['task_id']}, {r['reason']})")
        print("\nRun again with --confirm to actually perform this.")
        return

    for r in rows:
        _do_uninstall(args.db, r["name"])


def _do_uninstall(db: str, module_name: str) -> None:
    print(f"Uninstalling {module_name}...", end=" ")
    result = uninstall_module(module_name, db)
    if not result.success:
        print(f"FAILED -- {result.message}")
        return
    remove_scaffolded_module(module_name)
    print("done.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="odoo16_dev")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Enumerate every oma_*-prefixed module.")
    p_list.add_argument("--stale", action="store_true", help="Filter to genuinely abandoned modules only.")
    p_list.set_defaults(func=cmd_list)

    p_audit = sub.add_parser("audit-ir-model-data", help="Report the size of existing oma_* ir_model_data debris.")
    p_audit.set_defaults(func=cmd_audit_ir_model_data)

    p_uninstall = sub.add_parser("uninstall", help="Uninstall one named module, or --stale for the whole set.")
    p_uninstall.add_argument("module_name", nargs="?", default=None)
    p_uninstall.add_argument("--stale", action="store_true")
    p_uninstall.add_argument("--confirm", action="store_true", help="Actually perform the uninstall (default: dry-run).")
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
