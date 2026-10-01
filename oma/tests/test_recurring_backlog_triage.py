"""Phase 30, P4 (§5, Phase B): tests for the recurring backlog-triage
schedule -- the pure, mockable pieces (threshold edge-triggering,
already-drafted-cluster skip) tested directly; the real DB-reading path
(fetch_proposed_rules, fetch_genuinely_open_clusters) was verified live
against the real backlog instead (see docs/reports/PHASE30_P4_BACKLOG_
CLOSURE_AND_RECURRING_SCHEDULE_2026-07-30.md): the atomic self-detected
backlog went from 3 real open clusters to 0, all 15 rows superseded by
byte-exact-verified matches against already-existing validators, not
guessed.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.recurring_backlog_triage as recurring


def test_already_drafted_cluster_sigs_reads_both_pending_and_promoted(tmp_path, monkeypatch):
    pending = tmp_path / "pending"
    promoted = tmp_path / "promoted"
    pending.mkdir()
    promoted.mkdir()
    (pending / "a.py").write_text(
        '"""draft"""\n\n# PHASE29_DRAFT_META: ' + json.dumps({"cluster_sig": "sig-one"}) + "\n\ndef _validate_a(x): pass\n"
    )
    (promoted / "b.py").write_text(
        '"""draft"""\n\n# PHASE29_DRAFT_META: ' + json.dumps({"cluster_sig": "sig-two"}) + "\n\ndef _validate_b(x): pass\n"
    )
    (pending / "test_a.py").write_text("# not a draft, must be skipped, no META header needed")

    monkeypatch.setattr(recurring, "_PENDING_DIR", pending)
    monkeypatch.setattr(recurring, "_PROMOTED_DIR", promoted)

    sigs = recurring._already_drafted_cluster_sigs()
    assert sigs == {"sig-one", "sig-two"}
    print("PASS: already-drafted cluster sigs are read from both pending and promoted directories")


def test_run_once_dry_run_never_drafts_even_with_open_clusters(monkeypatch):
    monkeypatch.setattr(recurring, "fetch_proposed_rules", lambda: [{"id": i} for i in range(5)])
    monkeypatch.setattr(
        recurring, "fetch_genuinely_open_clusters",
        lambda: [{"sig": "some real cluster", "count": 5, "ids": [1, 2, 3, 4, 5], "sample_summary": "x"}],
    )
    monkeypatch.setattr(recurring, "_most_recent_recurring_run_proposed_count", lambda: None)
    monkeypatch.setattr(recurring, "build_validator_catalog", lambda root: [])
    monkeypatch.setattr(recurring, "_already_drafted_cluster_sigs", lambda: set())
    drafted_calls = []
    monkeypatch.setattr(
        recurring, "draft_and_trace_cluster",
        lambda cluster, catalog=None: drafted_calls.append(cluster) or Path("/tmp/fake.py"),
    )
    with patch("scripts.recurring_backlog_triage.append_project_memory"):
        summary = recurring.run_once(dry_run=True)

    assert summary["drafted_this_run"] == []
    assert drafted_calls == [], "dry-run must never actually draft, even with a qualifying open cluster"
    print("PASS: --dry-run clusters + alerts only, never drafts")


def test_run_once_live_drafts_clusters_at_or_above_the_instance_threshold(monkeypatch):
    monkeypatch.setattr(recurring, "fetch_proposed_rules", lambda: [])
    monkeypatch.setattr(
        recurring, "fetch_genuinely_open_clusters",
        lambda: [
            {"sig": "big real cluster", "count": 4, "ids": [1, 2, 3, 4], "sample_summary": "x"},
            {"sig": "too small to auto-draft", "count": 2, "ids": [5, 6], "sample_summary": "y"},
        ],
    )
    monkeypatch.setattr(recurring, "_most_recent_recurring_run_proposed_count", lambda: None)
    monkeypatch.setattr(recurring, "build_validator_catalog", lambda root: [])
    monkeypatch.setattr(recurring, "_already_drafted_cluster_sigs", lambda: set())
    drafted_calls = []
    monkeypatch.setattr(
        recurring, "draft_and_trace_cluster",
        lambda cluster, catalog=None: drafted_calls.append(cluster["sig"]) or Path("/tmp/fake.py"),
    )
    with patch("scripts.recurring_backlog_triage.append_project_memory"):
        summary = recurring.run_once(dry_run=False)

    assert drafted_calls == ["big real cluster"], (
        "only the cluster meeting the 3+ instance threshold must be drafted"
    )
    assert len(summary["drafted_this_run"]) == 1
    print("PASS: live run drafts only clusters at/above the auto-draft instance threshold")


def test_run_once_never_redrafts_an_already_drafted_cluster(monkeypatch):
    monkeypatch.setattr(recurring, "fetch_proposed_rules", lambda: [])
    monkeypatch.setattr(
        recurring, "fetch_genuinely_open_clusters",
        lambda: [{"sig": "already handled", "count": 5, "ids": [1, 2, 3, 4, 5], "sample_summary": "x"}],
    )
    monkeypatch.setattr(recurring, "_most_recent_recurring_run_proposed_count", lambda: None)
    monkeypatch.setattr(recurring, "build_validator_catalog", lambda root: [])
    monkeypatch.setattr(recurring, "_already_drafted_cluster_sigs", lambda: {"already handled"})
    drafted_calls = []
    monkeypatch.setattr(
        recurring, "draft_and_trace_cluster",
        lambda cluster, catalog=None: drafted_calls.append(cluster["sig"]) or Path("/tmp/fake.py"),
    )
    with patch("scripts.recurring_backlog_triage.append_project_memory"):
        recurring.run_once(dry_run=False)

    assert drafted_calls == [], "a cluster already represented by a pending/promoted draft must never be re-drafted"
    print("PASS: an already-drafted cluster is never re-drafted, idempotent across runs")


def test_proposed_threshold_alert_is_edge_triggered_not_level_triggered(monkeypatch):
    monkeypatch.setattr(recurring, "fetch_proposed_rules", lambda: [{"id": i} for i in range(60)])
    monkeypatch.setattr(recurring, "fetch_genuinely_open_clusters", lambda: [])

    # Case 1: previous run was already above threshold -- must NOT re-alert.
    monkeypatch.setattr(recurring, "_most_recent_recurring_run_proposed_count", lambda: 55)
    with patch("scripts.recurring_backlog_triage.append_project_memory"):
        summary_already_above = recurring.run_once(dry_run=True)
    assert summary_already_above["proposed_threshold_alert"] is False

    # Case 2: previous run was below threshold -- this run just crossed it, must alert.
    monkeypatch.setattr(recurring, "_most_recent_recurring_run_proposed_count", lambda: 40)
    with patch("scripts.recurring_backlog_triage.append_project_memory"):
        summary_newly_crossed = recurring.run_once(dry_run=True)
    assert summary_newly_crossed["proposed_threshold_alert"] is True

    print("PASS: the proposed-backlog threshold alert fires only on the run that newly crosses it")


if __name__ == "__main__":
    print("(this file requires pytest's tmp_path/monkeypatch fixtures; run via pytest)")
