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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.odoo_settings import load_odoo_settings  # noqa: E402
from infra.settings import load_postgres_settings  # noqa: E402
from paths import REPO_ROOT  # noqa: E402

_DEFAULT_BACKUP_DIR = REPO_ROOT / "var" / "backups" / "odoo"


def run_backup(tag: str) -> int:
    backup_dir = Path(os.environ.get("OMA_BACKUP_DIR", str(_DEFAULT_BACKUP_DIR))) / tag
    backup_dir.mkdir(parents=True, exist_ok=True)

    odoo_settings = load_odoo_settings()
    pg_settings = load_postgres_settings()
    db_name = odoo_settings.db

    dump_path = backup_dir / "db.dump"
    # Real fix: pg_dump must match (or exceed) the SERVER's major version, and this
    # script runs on the host, which has no reason to track whatever Postgres version
    # the postgres container happens to ship (postgres:16-alpine here) -- a host
    # pg_dump that's behind (e.g. Homebrew's default of 14.x) fails hard with
    # "aborting because of server version mismatch". Running pg_dump *inside* the
    # postgres container itself (docker exec) sidesteps the host/container version
    # coupling entirely -- the container always has a pg_dump that matches its own
    # server -- then copies the resulting dump back out, same pattern already used
    # for Odoo-container tool calls (tools_odoo/module_dev/toolchain.py's
    # _run_in_container / OMA_ODOO_CONTAINER).
    pg_container = os.environ.get("OMA_PG_CONTAINER", "oma-postgres-1")
    container_dump_path = f"/tmp/backup_{tag}.dump"
    env = os.environ.copy()
    env["PGPASSWORD"] = pg_settings.password
    dump_result = subprocess.run(
        [
            "docker", "exec",
            "-e", f"PGPASSWORD={pg_settings.password}",
            pg_container,
            "pg_dump",
            "-h", "127.0.0.1",
            "-p", "5432",
            "-U", pg_settings.user,
            "-Fc",
            "-f", container_dump_path,
            db_name,
        ],
        capture_output=True, text=True, timeout=55, env=env,
    )
    if dump_result.returncode != 0:
        print(f"pg_dump failed: {dump_result.stderr[-2000:]}", file=sys.stderr)
        return dump_result.returncode

    cp_result = subprocess.run(
        ["docker", "cp", f"{pg_container}:{container_dump_path}", str(dump_path)],
        capture_output=True, text=True, timeout=30,
    )
    subprocess.run(["docker", "exec", pg_container, "rm", "-f", container_dump_path], capture_output=True, timeout=10)
    if cp_result.returncode != 0:
        print(f"docker cp of pg_dump output failed: {cp_result.stderr[-2000:]}", file=sys.stderr)
        return cp_result.returncode

    # Same host/container split as the db dump above: the filestore lives inside the
    # odoo container's own odoo_filestore volume, never on the host filesystem, so
    # _filestore_path() (a plain host Path) can never actually find it in this
    # Compose topology -- tar it up inside the odoo container instead, then copy the
    # archive out, same docker cp pattern as the dump above.
    odoo_container = os.environ.get("OMA_ODOO_CONTAINER", "oma-odoo-1")
    container_filestore = f"/var/lib/odoo/.local/share/Odoo/filestore/{db_name}"
    container_tar_path = f"/tmp/filestore_{tag}.tar.gz"
    check = subprocess.run(
        ["docker", "exec", odoo_container, "test", "-d", container_filestore],
        capture_output=True, timeout=10,
    )
    if check.returncode == 0:
        tar_path = backup_dir / "filestore.tar.gz"
        tar_result = subprocess.run(
            ["docker", "exec", odoo_container, "tar", "-czf", container_tar_path,
             "-C", str(Path(container_filestore).parent), Path(container_filestore).name],
            capture_output=True, text=True, timeout=55,
        )
        if tar_result.returncode != 0:
            print(f"filestore tar (in-container) failed: {tar_result.stderr[-2000:]}", file=sys.stderr)
        else:
            fs_cp_result = subprocess.run(
                ["docker", "cp", f"{odoo_container}:{container_tar_path}", str(tar_path)],
                capture_output=True, text=True, timeout=30,
            )
            subprocess.run(["docker", "exec", odoo_container, "rm", "-f", container_tar_path],
                            capture_output=True, timeout=10)
            if fs_cp_result.returncode != 0:
                print(f"docker cp of filestore tar failed: {fs_cp_result.stderr[-2000:]}", file=sys.stderr)
    else:
        print(f"no filestore directory at {container_filestore} in {odoo_container}, skipping (db dump only)", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--odoo-filestore", action="store_true", help="back up db + filestore")
    parser.add_argument("--tag", required=True, help="task_id this backup is for")
    args = parser.parse_args(argv)
    return run_backup(args.tag)


if __name__ == "__main__":
    raise SystemExit(main())
