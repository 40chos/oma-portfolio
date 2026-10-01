"""Phase 22 (2026-07-23): the automated partition-creation job the
original schema's own comment promised ("automate creating future
partitions -- a small monthly cron/scheduled job -- rather than doing
this by hand forever," scripts/001_agent_memory_events.sql:53-56) but
was never actually built. Found live while researching whether
`agent_memory_events`' own audit trail had a real gap worth closing
with cryptographic signing (Phase 22 plan §5) -- the actual, more
concrete gap sitting right next to that research was this: only two
partitions (2026-07, 2026-08) exist, with no automation and no
retention/drop policy for anything beyond them. This script is the
"automate creating future partitions" half; it deliberately does NOT
implement a drop/retention policy -- no retention period was ever
specified by anyone, and inventing one here would be a real,
undiscussed data-loss policy decision, not a mechanical fix. If/when a
real retention period is decided, that's a second, separate, explicit
change -- not bundled into this one.

Idempotent and safe to run as often as wanted (e.g. a daily/weekly
cron entry, or manually): only ever CREATEs a partition that doesn't
already exist, for the current month and the next N months (default 3,
so a monthly cron has real slack even if it's missed once or twice),
never touches or drops anything.

Usage:
    python3 scripts/010_ensure_agent_memory_events_partitions.py [--months-ahead N] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

from infra.settings import load_postgres_settings

_TABLE = "agent_memory_events"


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _months_from(start: date, count: int) -> list[tuple[int, int]]:
    months = []
    y, m = start.year, start.month
    for _ in range(count):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def ensure_partitions(months_ahead: int = 3, dry_run: bool = False) -> list[str]:
    """Creates any missing monthly partition for the current month
    through `months_ahead` months into the future. Returns the list of
    partition table names actually created (or, in dry-run mode, that
    WOULD be created) -- an empty list means everything already exists,
    which is the expected, common case on most runs.
    """
    settings = load_postgres_settings()
    conn = psycopg2.connect(
        host=settings.host, port=settings.port, dbname=settings.db,
        user=settings.user, password=settings.password,
    )
    created: list[str] = []
    try:
        conn.autocommit = True
        cur = conn.cursor()
        today = date.today()
        for year, month in _months_from(date(today.year, today.month, 1), months_ahead + 1):
            partition_name = f"{_TABLE}_{year:04d}_{month:02d}"
            cur.execute(
                "SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'r'",
                (partition_name,),
            )
            if cur.fetchone() is not None:
                continue  # already exists -- the common, expected case
            start, end = _month_bounds(year, month)
            created.append(partition_name)
            if dry_run:
                continue
            cur.execute(
                f"CREATE TABLE {partition_name} PARTITION OF {_TABLE} "
                f"FOR VALUES FROM (%s) TO (%s)",
                (start.isoformat(), end.isoformat()),
            )
    finally:
        conn.close()
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--months-ahead", type=int, default=3,
        help="how many months beyond the current one to ensure a partition exists for (default 3)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be created without actually creating anything",
    )
    args = parser.parse_args()

    created = ensure_partitions(months_ahead=args.months_ahead, dry_run=args.dry_run)
    if not created:
        print("All required agent_memory_events partitions already exist -- nothing to do.")
    elif args.dry_run:
        print(f"Would create {len(created)} partition(s): {created}")
    else:
        print(f"Created {len(created)} partition(s): {created}")


if __name__ == "__main__":
    main()
