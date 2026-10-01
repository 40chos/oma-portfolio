#!/usr/bin/env python3
"""Phase 36 §4.5 item 1 -- scheduled security-posture drift alert.

Runs once daily, immediately after each scheduled re-extraction/import completes (§4.3's
re-extraction cadence) -- access-control posture changes at the cadence of a code change
landing in a module, not per Build/Code-Review request, so a 15-minute poll (the cadence
§4.3's own `graph_grounding_alert_check.py` uses for a per-request degradation rate) would
only add noise here.

Mechanism: one fixed, parameterized query (`tools_odoo.graph_queries.get_all_ungated_models`,
Cypher equivalent to `MATCH (m:Model) WHERE m.has_ungated_access_rule RETURN
m.technical_name`) against the current import, diffed against the same query's result
persisted from the prior run (a small JSON snapshot, non-overwrite timestamped per this
workspace's own backup convention, alongside a `_latest.json` pointer this script reads back
next time it runs). For every model that newly appears in the ungated set (`false -> true`
since the last snapshot), this script writes one structured `SECURITY`-level JSON log line to
this workspace's Loki ingestion path, matching the exact schema CLAUDE.md's Logging &
Telemetry Standards section specifies.

A model already ungated in both snapshots does NOT re-alert -- only a genuine transition
does, so this converges to a low-noise signal instead of a daily list of the same ~111
models (§4.5's own stated rationale).

Usage:
    python3 scripts/security_posture_alert_check.py [--snapshot-dir DIR] [--log-dir DIR] [--dry-run]

Exit code is always 0 on a successful run (including "graph unavailable, nothing to do" --
this is an alerting script, not a hard gate; a Neo4j outage should not fail a cron job) --
the one exception is a genuine, unexpected exception escaping this script's own try/except,
which DOES propagate (and should page whoever owns the cron job), per this document's own
"a diagnostic/report script should exit non-zero on a real outage, unlike Build/Code-Review's
runtime fail-open path" distinction (graph_queries.py's own module docstring).
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

_DEFAULT_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "state" / "security_posture"
_DEFAULT_LOG_DIR = Path(os.environ.get("OMA_LOG_DIR", str(Path(__file__).resolve().parent.parent / "var" / "logs")))
_LATEST_SNAPSHOT_NAME = "security_posture_snapshot_latest.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _timestamp_for_filename() -> str:
    # Non-overwrite timestamped convention, CLAUDE.md §4.B: YYYY-MM-DD_HH-MM-SS.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")


def load_prior_ungated_set(snapshot_dir: Path) -> set[str]:
    """Reads the prior run's ungated-model set from `_latest.json`. Missing/unreadable/
    malformed snapshot -> empty set (treated as "no prior data", so the FIRST ever run of
    this script never floods an alert for all ~111 models at once -- only genuine
    transitions observed AFTER a first baseline run alert)."""
    latest_path = snapshot_dir / _LATEST_SNAPSHOT_NAME
    try:
        data = json.loads(latest_path.read_text())
        return set(data.get("ungated_technical_names", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def save_snapshot(snapshot_dir: Path, ungated_technical_names: list[str]) -> None:
    """Writes both the timestamped (non-overwrite) audit copy and the `_latest.json`
    pointer this script itself reads back on its next run."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": _now_iso(),
        "ungated_technical_names": sorted(ungated_technical_names),
    }
    timestamped_path = snapshot_dir / f"security_posture_snapshot_{_timestamp_for_filename()}.json"
    timestamped_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    latest_path = snapshot_dir / _LATEST_SNAPSHOT_NAME
    latest_path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def compute_newly_ungated(prior: set[str], current: set[str]) -> list[str]:
    """The `false -> true` transitions this run should alert on -- present now, absent from
    the prior snapshot. Sorted for deterministic test/log output."""
    return sorted(current - prior)


def _resolve_owner_module(driver, technical_name: str) -> str | None:
    """Best-effort owning-module lookup for the alert's metadata -- reuses
    `get_field_types_for_model`'s own :ImportMetadata-gated read pattern via a tiny inline
    query rather than adding a whole new graph_queries.py function for one metadata field;
    any failure yields `None`, never blocks the alert itself from being written.

    Real finding (2026-08-13, same root cause as graph_queries.py's get_field_impact_analysis
    fix): :Model never carries an `owner_module` property -- ownership is the real
    (:Module)-[:DEFINES]->(:Model) edge. A property-based query here would always return
    None against the real server. Resolves via DEFINES instead; a model with more than one
    owning module (rare, but the schema allows multiple DEFINES edges) returns one
    arbitrarily via LIMIT 1 -- acceptable for this alert's own metadata-only purpose."""
    from neo4j import READ_ACCESS, Query

    from infra.settings import load_neo4j_settings

    settings = load_neo4j_settings()
    cypher = """
    MATCH (owner:Module)-[:DEFINES]->(m:Model {technical_name: $technical_name})
    RETURN owner.name AS owner_module
    LIMIT 1
    """
    try:
        query = Query(cypher, timeout=settings.query_timeout_s)
        with driver.session(database=settings.database, default_access_mode=READ_ACCESS) as session:
            record = session.run(query, {"technical_name": technical_name}).single()
        return record["owner_module"] if record else None
    except Exception:
        return None


def write_security_log_line(log_dir: Path, technical_name: str, owner_module: str | None) -> None:
    """One structured `SECURITY`-level JSON log line, matching CLAUDE.md's Loki ingestion
    schema exactly (§4.C) -- reusing the workspace's already-real alerting substrate rather
    than inventing a new integration point, per §4.5 item 1's own design."""
    log_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "timestamp": _now_iso(),
        "correlation_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "user_id": None,
        "agent_id": "AGT-009-ODOO_INGESTOR",
        "log_level": "SECURITY",
        "message": "model_access_control_newly_ungated",
        "metadata": {
            "technical_name": technical_name,
            "owner_module": owner_module,
        },
    }
    log_path = log_dir / f"security_posture_alert_{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    with log_path.open("a") as f:
        f.write(json.dumps(line) + "\n")


def run(snapshot_dir: Path, log_dir: Path, dry_run: bool = False) -> list[str]:
    """Returns the list of newly-ungated models this run alerted on (empty if none, or if
    the graph was unavailable/mid-import -- both are silent no-ops, never a script failure).
    """
    try:
        from infra.neo4j_client import get_neo4j_read_driver
        from tools_odoo.graph_queries import get_all_ungated_models
    except Exception as exc:  # pragma: no cover -- exercised via a monkeypatched import failure in tests
        print(f"security_posture_alert_check: graph modules unavailable, skipping run: {exc}", file=sys.stderr)
        return []

    try:
        driver = get_neo4j_read_driver()
        import_in_progress, current_ungated = get_all_ungated_models(driver)
    except Exception as exc:
        print(f"security_posture_alert_check: graph unreachable, skipping run: {exc}", file=sys.stderr)
        return []

    if import_in_progress:
        print("security_posture_alert_check: import in progress, skipping this run", file=sys.stderr)
        return []

    current_set = set(current_ungated)
    latest_snapshot_path = snapshot_dir / _LATEST_SNAPSHOT_NAME
    is_baseline_run = not latest_snapshot_path.exists()
    if is_baseline_run:
        # No prior snapshot at all (first run ever, or a fresh snapshot_dir) -- there is
        # nothing to have transitioned FROM, so treating "empty prior" as "everything just
        # transitioned false->true" would flood an alert for every one of the ~111 currently
        # ungated models on day one. Only a snapshot-to-snapshot transition is a real alert.
        newly_ungated: list[str] = []
    else:
        prior_set = load_prior_ungated_set(snapshot_dir)
        newly_ungated = compute_newly_ungated(prior_set, current_set)

    if not dry_run:
        for technical_name in newly_ungated:
            owner_module = _resolve_owner_module(driver, technical_name)
            write_security_log_line(log_dir, technical_name, owner_module)
        save_snapshot(snapshot_dir, sorted(current_set))

    return newly_ungated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, default=_DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--log-dir", type=Path, default=_DEFAULT_LOG_DIR)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute the diff and print it, but write no log lines and save no new snapshot.",
    )
    args = parser.parse_args()

    newly_ungated = run(args.snapshot_dir, args.log_dir, dry_run=args.dry_run)
    if newly_ungated:
        print(f"security_posture_alert_check: {len(newly_ungated)} newly-ungated model(s): "
              f"{', '.join(newly_ungated)}")
    else:
        print("security_posture_alert_check: no newly-ungated models this run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
