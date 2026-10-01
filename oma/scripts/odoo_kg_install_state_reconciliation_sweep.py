#!/usr/bin/env python3
"""Phase 36 §13.4 item 2 -- scheduled install-state reconciliation sweep.

Same structure/discipline as scripts/security_posture_alert_check.py (this
workspace's own established pattern for a periodic, fail-open, graph-
reading diagnostic script): argparse, a timestamped non-overwrite snapshot
dir, --dry-run, real Loki-schema JSON log lines for every correction made,
and a non-zero exit ONLY on a genuine, unexpected failure -- a Neo4j or
Odoo-RPC outage is a silent no-op, not a script failure (this is an
alerting/reconciliation script, not a hard gate).

Mechanism: `tools_odoo.graph_queries.get_all_module_names` lists every real
`:Module` node in the graph; for each one, this script re-checks its REAL,
live `ir.module.module.state` via `tools_odoo.odoo_schema_client.
get_module_state_fast(module_name, db)` against the graph's own stored
`real_install_state` (written by `update_module_install_state`, fired from
`install_module()`'s success path -- see tools_odoo/knowledge_graph/
install_state_sync.py). Any drift (including "never confirmed at all," i.e.
`real_install_state` is still unset) is logged as a named `DRIFT_CORRECTED`
event and the graph is updated to match the real, live state.

`db` defaults to `infra.odoo_settings`'s own canonical dev target
(`odoo16_dev`) -- the same single real Odoo instance every other
`get_module_state_fast` call site in this codebase (`toolchain.py`'s own
fast post-install verification, this sweep's own hook) reads against;
overridable via `--db` for a different real target.

Usage:
    python3 scripts/odoo_kg_install_state_reconciliation_sweep.py [--db DB]
        [--snapshot-dir DIR] [--log-dir DIR] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_DEFAULT_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "state" / "install_state_reconciliation"
_DEFAULT_LOG_DIR = Path(os.environ.get("OMA_LOG_DIR", str(Path(__file__).resolve().parent.parent / "var" / "logs")))
# Matches infra.odoo_settings._CANONICAL_DB -- the one real Odoo dev target
# every other get_module_state_fast() call site in this codebase reads
# against. Not imported directly (that module's guard is deliberately
# import-order-sensitive and hardcoded for a different purpose -- the
# live-target safety assertion, not a settings default); duplicated here as
# a plain literal default, overridable via --db for a genuinely different
# real target.
_DEFAULT_DB = "odoo16_dev"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _timestamp_for_filename() -> str:
    # Non-overwrite timestamped convention, CLAUDE.md §4.B: YYYY-MM-DD_HH-MM-SS.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")


def compute_drift(module_name: str, graph_state: str | None, real_state: str | None) -> dict | None:
    """Returns a drift-record dict if `graph_state` (what the graph currently
    stores) and `real_state` (what a fresh, live RPC read just returned)
    genuinely disagree, else None. `real_state` of None (the RPC read itself
    failed) is never treated as drift -- there is nothing trustworthy to
    correct TO in that case, so this module is simply skipped for this run.
    """
    if real_state is None:
        return None
    if graph_state == real_state:
        return None
    return {"module": module_name, "graph_state": graph_state, "real_state": real_state}


def write_reconciliation_log_line(log_dir: Path, drift: dict) -> None:
    """One structured JSON log line per correction, matching CLAUDE.md's
    Loki ingestion schema exactly -- same convention as
    security_posture_alert_check.py's write_security_log_line."""
    log_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "timestamp": _now_iso(),
        "correlation_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "user_id": None,
        "agent_id": "AGT-009-ODOO_INGESTOR",
        "log_level": "WARN",
        "message": "install_state_drift_corrected",
        "metadata": {
            "module": drift["module"],
            "graph_state": drift["graph_state"],
            "real_state": drift["real_state"],
        },
    }
    log_path = log_dir / f"install_state_reconciliation_{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    with log_path.open("a") as f:
        f.write(json.dumps(line) + "\n")


def save_snapshot(snapshot_dir: Path, drifts: list[dict], modules_checked: int) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": _now_iso(),
        "modules_checked": modules_checked,
        "drifts_corrected": drifts,
    }
    timestamped_path = snapshot_dir / f"install_state_reconciliation_{_timestamp_for_filename()}.json"
    timestamped_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    latest_path = snapshot_dir / "install_state_reconciliation_latest.json"
    latest_path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def run(db: str, snapshot_dir: Path, log_dir: Path, dry_run: bool = False) -> list[dict]:
    """Returns the list of drift records this run found/corrected (empty if
    none, or if the graph/RPC target was unavailable/mid-import -- all
    silent no-ops, never a script failure)."""
    try:
        from infra.neo4j_client import get_neo4j_driver
        from tools_odoo.graph_queries import (
            get_all_module_names,
            get_module_real_install_state,
            update_module_install_state,
        )
        from tools_odoo.odoo_schema_client import get_module_state_fast
    except Exception as exc:  # pragma: no cover -- exercised via a monkeypatched import failure in tests
        print(f"install_state_reconciliation_sweep: modules unavailable, skipping run: {exc}", file=sys.stderr)
        return []

    try:
        driver = get_neo4j_driver()
        import_in_progress, module_names = get_all_module_names(driver)
    except Exception as exc:
        print(f"install_state_reconciliation_sweep: graph unreachable, skipping run: {exc}", file=sys.stderr)
        return []

    if import_in_progress:
        print("install_state_reconciliation_sweep: import in progress, skipping this run", file=sys.stderr)
        return []

    drifts: list[dict] = []
    for module_name in module_names:
        try:
            _, graph_state = get_module_real_install_state(driver, module_name)
        except Exception as exc:
            print(f"install_state_reconciliation_sweep: graph read failed for {module_name!r}: {exc}", file=sys.stderr)
            continue
        try:
            real_state = get_module_state_fast(module_name, db)
        except Exception as exc:
            print(f"install_state_reconciliation_sweep: RPC read failed for {module_name!r}: {exc}", file=sys.stderr)
            continue

        drift = compute_drift(module_name, graph_state, real_state)
        if drift is None:
            continue
        drifts.append(drift)
        if not dry_run:
            try:
                update_module_install_state(driver, module_name, real_state, _now_iso())
                write_reconciliation_log_line(log_dir, drift)
            except Exception as exc:
                print(f"install_state_reconciliation_sweep: failed to write correction for {module_name!r}: {exc}", file=sys.stderr)

    if not dry_run:
        save_snapshot(snapshot_dir, drifts, len(module_names))

    return drifts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=_DEFAULT_DB)
    parser.add_argument("--snapshot-dir", type=Path, default=_DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--log-dir", type=Path, default=_DEFAULT_LOG_DIR)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute the drift and print it, but write no log lines, no graph updates, no new snapshot.",
    )
    args = parser.parse_args()

    drifts = run(args.db, args.snapshot_dir, args.log_dir, dry_run=args.dry_run)
    if drifts:
        print(f"install_state_reconciliation_sweep: {len(drifts)} drift(s) corrected: "
              f"{', '.join(d['module'] for d in drifts)}")
    else:
        print("install_state_reconciliation_sweep: no drift found this run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
