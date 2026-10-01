"""Phase 30, P4 (§5, Phase B): runs the Phase 29 backlog-triage +
auto-draft pipeline on a schedule instead of only ever on-demand when a
human happens to remember to run it -- closing the real gap this
priority exists for: nothing today prevents a fresh backlog from
silently re-accumulating the same way the original 637-row one did,
one round at a time, invisibly.

Designed to be invoked by cron/systemd timer (see
scripts/oma-backlog-triage.{service,timer} -- NOT installed/enabled by
this script itself; that is a separate, deliberate step requiring
human confirmation, matching this whole project's own discipline about
persistent, unattended automation). Idempotent and safe to run
repeatedly: never drafts the same cluster twice (checks both pending
and promoted validators' own PHASE29_DRAFT_META headers first), never
promotes anything (drafting only -- promote_pending_validator.py stays
a separate, human-confirmed step, unconditionally).

Two real, structured threshold alerts, both edge-triggered (fires once
when a threshold is newly crossed, not every run while still above
it -- avoids alert spam):
  1. The upstream proposed-rule backlog crosses _PROPOSED_ALERT_THRESHOLD.
  2. The downstream drafted-but-unpromoted queue in
     contracts/pending_validators/ crosses _PENDING_ALERT_THRESHOLD.

Usage:
    python3 scripts/recurring_backlog_triage.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manager.tools import append_project_memory  # noqa: E402
from scripts.draft_validator_from_cluster import (  # noqa: E402
    draft_and_trace_cluster,
    fetch_genuinely_open_clusters,
)
from scripts.rule_backlog_triage import build_validator_catalog, fetch_proposed_rules  # noqa: E402

_PROPOSED_ALERT_THRESHOLD = 50
_PENDING_ALERT_THRESHOLD = 12
_AUTO_DRAFT_MIN_INSTANCE_COUNT = 3
_PENDING_DIR = Path(__file__).resolve().parent.parent / "contracts" / "pending_validators"
_PROMOTED_DIR = Path(__file__).resolve().parent.parent / "contracts" / "promoted_validators"
_META_RE = re.compile(r"^# PHASE29_DRAFT_META: (\{.*\})\s*$", re.MULTILINE)


def _already_drafted_cluster_sigs() -> set[str]:
    """Every cluster_sig already represented by a real draft file, in
    EITHER contracts/pending_validators/ (awaiting review) or contracts/
    promoted_validators/ (already resolved, archived) -- never re-draft
    a cluster a human is already looking at or already decided on.
    """
    sigs: set[str] = set()
    for directory in (_PENDING_DIR, _PROMOTED_DIR):
        if not directory.exists():
            continue
        for path in directory.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            text = path.read_text()
            match = _META_RE.search(text)
            if match:
                try:
                    sigs.add(json.loads(match.group(1)).get("cluster_sig", ""))
                except json.JSONDecodeError:
                    continue
    return sigs


def _most_recent_recurring_run_proposed_count() -> int | None:
    """The `proposed_count` this same script recorded on its own last
    real run -- read back from agent_memory_events, never a separate
    file -- so the "crosses a threshold" alert can tell newly-crossing
    from already-above (edge-triggered, not level-triggered).
    """
    import psycopg2
    import psycopg2.extras

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT detail->>'proposed_count' AS proposed_count
            FROM agent_memory_events
            WHERE event_type = 'note' AND tags @> ARRAY['recurring_backlog_triage_run']
            ORDER BY id DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None or row["proposed_count"] is None:
            return None
        return int(row["proposed_count"])
    finally:
        conn.close()


def run_once(dry_run: bool) -> dict:
    """One real, complete recurring-triage pass. Returns a summary dict
    (also what gets logged) so this is directly testable without
    needing to parse stdout.
    """
    proposed_rows = fetch_proposed_rules()
    proposed_count = len(proposed_rows)
    open_clusters = fetch_genuinely_open_clusters()

    summary = {
        "proposed_count": proposed_count,
        "distinct_shape_clusters": len(open_clusters),
        "drafted_this_run": [],
        "proposed_threshold_alert": False,
        "pending_threshold_alert": False,
    }

    append_project_memory(
        event_type="note", actor="recurring_backlog_triage", task_id=None, module=None,
        summary=f"Recurring backlog triage: {proposed_count} proposed rule(s) in "
                f"{len(open_clusters)} distinct shape-cluster(s).",
        tags=["recurring_backlog_triage_run"],
        detail=summary,
        verified=False,
    )

    previous_count = _most_recent_recurring_run_proposed_count()
    if (
        proposed_count >= _PROPOSED_ALERT_THRESHOLD
        and (previous_count is None or previous_count < _PROPOSED_ALERT_THRESHOLD)
    ):
        summary["proposed_threshold_alert"] = True
        append_project_memory(
            event_type="note", actor="recurring_backlog_triage", task_id=None, module=None,
            summary=(
                f"Proposed-rule backlog has crossed {_PROPOSED_ALERT_THRESHOLD} "
                f"(now {proposed_count}) -- worth a human triage pass, matching the same real "
                f"631-row growth this whole priority exists to catch earlier next time."
            ),
            tags=["recurring_backlog_triage_alert", "proposed_backlog_threshold"],
            detail={"proposed_count": proposed_count, "threshold": _PROPOSED_ALERT_THRESHOLD},
            verified=False,
        )

    pending_count = len([p for p in _PENDING_DIR.glob("*.py") if not p.name.startswith("test_")]) \
        if _PENDING_DIR.exists() else 0
    if pending_count >= _PENDING_ALERT_THRESHOLD:
        summary["pending_threshold_alert"] = True
        append_project_memory(
            event_type="note", actor="recurring_backlog_triage", task_id=None, module=None,
            summary=(
                f"contracts/pending_validators/ has {pending_count} drafted-but-unpromoted "
                f"validator(s) -- crossed the {_PENDING_ALERT_THRESHOLD} review-queue threshold."
            ),
            tags=["recurring_backlog_triage_alert", "pending_validators_threshold"],
            detail={"pending_count": pending_count, "threshold": _PENDING_ALERT_THRESHOLD},
            verified=False,
        )

    if dry_run:
        return summary

    already_drafted = _already_drafted_cluster_sigs()
    catalog = build_validator_catalog(Path(__file__).resolve().parent.parent)
    for cluster in open_clusters:
        if cluster["count"] < _AUTO_DRAFT_MIN_INSTANCE_COUNT:
            continue
        if cluster["sig"] in already_drafted:
            continue
        out_path = draft_and_trace_cluster(cluster, catalog=catalog)
        if out_path:
            summary["drafted_this_run"].append(str(out_path))

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="cluster + alert only, never draft")
    args = parser.parse_args()

    summary = run_once(dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
