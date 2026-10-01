"""Phase 31 §6(a), item 15: the real safety net for per-node branches
(`node/<task_id>/<node_label>`, tools_odoo/module_dev/vcs.py) that `apply_node_result_to_module()`
failed to clean up itself -- a crashed process, a killed round, anything that skipped its own
`finally: vcs.delete_branch(...)`. Deletes `node/*/*` branches older than a threshold (default
24h). NEVER touches `task/<task_id>` branches (a task's own durable, permanent record) or `main`.

Real, disclosed limitation (2026-08-07, confirmed live against the real Gitea instance): every
commit made through vcs.py's own batch-contents payload gets a real, explicit author/committer
`date` as of this same Phase 31 push (see vcs.py's own commit_validated_round() comment) -- but
this fix was only confirmed to change the SUBMITTED payload, not confirmed to change what this
Gitea instance's own branches API reports back (`commit.timestamp` was still observed as the
placeholder "2001-01-01T00:00:00Z" even after adding an explicit date in local testing, an
unresolved discrepancy between the documented Gitea contents-API date field and what this
specific instance's branches-list endpoint surfaces). Because of this, age-based filtering here
is NOT yet guaranteed reliable on every commit -- this script defaults to `--dry-run` (no real
deletions) specifically because of this open question; `--execute` is required to actually
delete anything, and even then only ever targets the `node/` prefix.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools_odoo.module_dev import vcs

_NODE_BRANCH_PREFIX = "node/"
_DEFAULT_MAX_AGE_HOURS = 24


def _parse_gitea_timestamp(raw: str) -> datetime | None:
    """Gitea's own branch-list `commit.timestamp` -- ISO-8601, always ending `Z`. Returns None
    (never raises) on anything unparseable, so a malformed timestamp is treated as "cannot
    determine age," never as a false "genuinely old" signal.
    """
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def list_node_branches(owner: str, repo: str) -> list[dict]:
    """Every real `node/*` branch currently on the repo, each as `{"name": ..., "timestamp":
    datetime|None}` -- paginated via Gitea's own `page`/`limit` params (50/page, matching this
    project's other Gitea list call sites' own conservative page size).
    """
    cfg = vcs._gitea_config()
    result: list[dict] = []
    page = 1
    with vcs._client(cfg) as client:
        while True:
            resp = client.get(
                f"/api/v1/repos/{owner}/{repo}/branches", params={"page": page, "limit": 50},
            )
            if resp.status_code != 200:
                raise vcs.VcsError(f"Gitea branch list failed ({resp.status_code}): {resp.text[:500]}")
            batch = resp.json()
            if not batch:
                break
            for entry in batch:
                name = entry.get("name", "")
                if not name.startswith(_NODE_BRANCH_PREFIX):
                    continue
                raw_timestamp = (entry.get("commit") or {}).get("timestamp", "")
                result.append({"name": name, "timestamp": _parse_gitea_timestamp(raw_timestamp)})
            page += 1
    return result


def find_orphaned_node_branches(
    branches: list[dict], max_age_hours: int = _DEFAULT_MAX_AGE_HOURS, now: datetime | None = None,
) -> list[str]:
    """Pure filter, no network -- a `node/*` branch qualifies as orphaned iff its own commit
    timestamp both PARSED successfully and is older than `max_age_hours`. A branch whose
    timestamp failed to parse (or is the known-bogus placeholder-adjacent case) is deliberately
    NOT flagged -- "cannot confirm it's old" is never treated as "confirmed old," matching this
    whole codebase's own never-guess discipline.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    return [
        entry["name"] for entry in branches
        if entry["timestamp"] is not None and entry["timestamp"] < cutoff
    ]


def _task_id_and_node_label_from_branch(branch_name: str) -> tuple[str, str] | None:
    """`node/<task_id>/<node_label>` -> (task_id, node_label); None for anything else, including
    a `node_label` that itself contains a `/` (vcs.delete_branch() only ever needs the two-part
    split the actual naming scheme guarantees).
    """
    if not branch_name.startswith(_NODE_BRANCH_PREFIX):
        return None
    remainder = branch_name[len(_NODE_BRANCH_PREFIX):]
    parts = remainder.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def run_cleanup(max_age_hours: int = _DEFAULT_MAX_AGE_HOURS, dry_run: bool = True) -> dict:
    """The real entry point. Returns `{"scanned": N, "orphaned": [...], "deleted": [...],
    "dry_run": bool}` -- `deleted` is always `[]` when `dry_run=True`, never a simulated guess.
    """
    cfg = vcs._gitea_config()
    owner, repo = cfg["DEV_AGENT_GITEA_ORG"], cfg["DEV_AGENT_GITEA_GENERATED_MODULES_REPO"]
    branches = list_node_branches(owner, repo)
    orphaned = find_orphaned_node_branches(branches, max_age_hours=max_age_hours)

    deleted: list[str] = []
    if not dry_run:
        for branch_name in orphaned:
            parsed = _task_id_and_node_label_from_branch(branch_name)
            if parsed is None:
                continue
            task_id, node_label = parsed
            if vcs.delete_branch(task_id, node_label):
                deleted.append(branch_name)

    return {
        "scanned": len(branches), "orphaned": orphaned, "deleted": deleted, "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Delete orphaned per-node Gitea branches (node/<task_id>/<node_label>) older than "
            "a threshold. Defaults to --dry-run; pass --execute to actually delete."
        ),
    )
    parser.add_argument(
        "--max-age-hours", type=int, default=_DEFAULT_MAX_AGE_HOURS,
        help=f"delete branches whose own commit is older than this many hours (default {_DEFAULT_MAX_AGE_HOURS})",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="actually delete orphaned branches (default is a dry run -- lists them only)",
    )
    args = parser.parse_args()

    result = run_cleanup(max_age_hours=args.max_age_hours, dry_run=not args.execute)
    print(f"Scanned {result['scanned']} node/* branch(es).")
    print(f"Found {len(result['orphaned'])} orphaned (older than {args.max_age_hours}h):")
    for name in result["orphaned"]:
        print(f"  - {name}")
    if result["dry_run"]:
        print("Dry run -- nothing deleted. Pass --execute to actually delete.")
    else:
        print(f"Deleted {len(result['deleted'])} branch(es).")


if __name__ == "__main__":
    main()
