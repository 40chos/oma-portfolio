#!/usr/bin/env python3
"""Real pre-install backup: pg_dump of the Odoo database plus a tar of its
filestore, tagged by task_id. Invoked by tools_odoo/module_dev/safety_net.py's
run_pre_install_backup() before every real (non-sandbox) module install --
best-effort, non-blocking on the caller's side, so failures here just need to
exit non-zero with a useful stderr message.

Backups land under OMA_BACKUP_DIR (default: <repo>/var/backups/odoo), one
subdirectory per task_id, containing db.dump (pg_dump custom format) and
filestore.tar.gz.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.odoo_settings import load_odoo_settings  # noqa: E402
from infra.settings import load_postgres_settings  # noqa: E402
from paths import REPO_ROOT  # noqa: E402

_DEFAULT_BACKUP_DIR = REPO_ROOT / "var" / "backups" / "odoo"


def _filestore_path(db_name: str) -> Path:
    override = os.environ.get("OMA_ODOO_FILESTORE_PATH")
    if override:
        return Path(override)
    return Path("/var/lib/odoo/.local/share/Odoo/filestore") / db_name


def run_backup(tag: str) -> int:
    backup_dir = Path(os.environ.get("OMA_BACKUP_DIR", str(_DEFAULT_BACKUP_DIR))) / tag
    backup_dir.mkdir(parents=True, exist_ok=True)

    odoo_settings = load_odoo_settings()
    pg_settings = load_postgres_settings()
    db_name = odoo_settings.db

    dump_path = backup_dir / "db.dump"
    env = os.environ.copy()
    env["PGPASSWORD"] = pg_settings.password
    result = subprocess.run(
        [
            "pg_dump",
            "-h", pg_settings.host,
            "-p", str(pg_settings.port),
            "-U", pg_settings.user,
            "-Fc",
            "-f", str(dump_path),
            db_name,
        ],
        capture_output=True, text=True, timeout=55, env=env,
    )
    if result.returncode != 0:
        print(f"pg_dump failed: {result.stderr[-2000:]}", file=sys.stderr)
        return result.returncode

    filestore = _filestore_path(db_name)
    if filestore.is_dir():
        tar_path = backup_dir / "filestore.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(filestore, arcname="filestore")
    else:
        print(f"no filestore directory at {filestore}, skipping (db dump only)", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--odoo-filestore", action="store_true", help="back up db + filestore")
    parser.add_argument("--tag", required=True, help="task_id this backup is for")
    args = parser.parse_args(argv)
    return run_backup(args.tag)


if __name__ == "__main__":
    raise SystemExit(main())
