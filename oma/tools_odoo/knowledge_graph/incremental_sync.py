"""Phase 36 §0.9c -- real, automated incremental Odoo Knowledge Graph sync,
triggered right after Build durably writes a module's files to disk.

Real, deliberate divergence from §0.9c's literal text (flagged here, same discipline as
every other real-vs-documented gap this document's own revisions call out rather than
silently paper over): the plan text says "a new call from manager/loop.py's existing
post-commit hook." Direct inspection of the real code shows the actual durable-write point
-- both the decomposed multi-node `apply_node_result_to_module()` path and the
non-decomposed direct-write path -- lives in `specialists/build/specialist.py`'s
`_run_module_dev()`, not `manager/loop.py`. This module is wired from there instead,
immediately after the `"Wrote module files."` trace event common to both write paths.

Design:
  1. Fire-and-forget (`asyncio.create_task`) -- must never slow down or fail a Build round
     on a graph-service hiccup, matching §4.3's fail-open posture for every other graph
     touchpoint in this document.
  2. Re-parses ONLY the touched module's real, just-written source
     (`tools_odoo.knowledge_graph.parser.parse_module`, the same Stage A entry point
     `driver.py`'s full-tree run uses per-module) -- not a full 240-module re-parse.
  3. Replaces that module's one record in-place in `final_module_graph.jsonl` (the ETL's
     own authoritative source file), so the freshly-parsed data is what the next ETL run
     -- incremental or full -- actually reads.
  4. Invokes `scripts/odoo_kg_to_neo4j.py --mode incremental --module <name>`.

Known, flagged limitation on item 4: direct inspection of odoo_kg_to_neo4j.py shows its
`--mode incremental`'s own `module_scope` parameter is accepted but never actually used to
narrow what gets written to Neo4j -- incremental mode skips the pre-wipe/backup step (mode
in ("full", "restore") only) but then re-runs the SAME full `build_import_plan()` over the
WHOLE `final_module_graph.jsonl` and MERGEs it all. This is always safe and correct (every
write is an idempotent MERGE on a natural key, so re-syncing untouched modules is a no-op
in practice) but means this hook's actual runtime cost is "re-parse one module, re-merge
all 240" rather than a narrowly-scoped single-module write. Narrowing the ETL's own write
path to genuinely skip untouched modules is real follow-up work, out of this task's scope --
noted rather than silently assumed to already be minimal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import subprocess
import sys
from pathlib import Path

from paths import KNOWLEDGE_GRAPH_DATA_PATH, REPO_ROOT

logger = logging.getLogger(__name__)

_SOURCE_JSONL = KNOWLEDGE_GRAPH_DATA_PATH / "final_module_graph.jsonl"
_ETL_SCRIPT = REPO_ROOT / "scripts" / "odoo_kg_to_neo4j.py"
_ETL_TIMEOUT_S = 120


def trigger_incremental_graph_sync(module_name: str, task_id: str | None = None) -> None:
    """Fire-and-forget entry point -- call this right after a module's files are durably
    written to disk. Never raises, never blocks the caller; a killswitch env var
    (`OMA_SKIP_GRAPH_INCREMENTAL_SYNC`) is provided for tests/ops and honored before
    scheduling anything.
    """
    if os.environ.get("OMA_SKIP_GRAPH_INCREMENTAL_SYNC"):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop -- e.g. a script/test calling this synchronously. Fail
        # open rather than raise: this is a best-effort freshness sync, never a hard
        # dependency of the caller's own success.
        return
    asyncio.create_task(_run_incremental_sync(module_name, task_id))


async def _run_incremental_sync(module_name: str, task_id: str | None) -> None:
    try:
        await asyncio.to_thread(_reparse_and_update_source_jsonl, module_name)
        await asyncio.to_thread(_run_etl_incremental, module_name)
    except Exception as exc:  # noqa: BLE001 -- fire-and-forget, must never propagate
        logger.warning("incremental graph sync failed for module %r (task_id=%s): %s", module_name, task_id, exc)


def _fetch_module_to_local_temp(module_name: str) -> Path | None:
    """Real, confirmed bug found and fixed live (2026-08-13): this function did not
    previously exist -- `_reparse_and_update_source_jsonl()` used to check
    `_MODULE_DEV_ADDONS_DIR / module_name` as a LOCAL filesystem path, but that path
    (`/mnt/extra-addons`, matching `tools_odoo.module_dev.toolchain._MODULE_DEV_ADDONS_DIR`'s
    own string) is a REMOTE path *inside the Odoo dev container*, reached only over SSH --
    it has never existed on this host at all (confirmed directly: `ls /mnt/extra-addons`
    fails here). That meant `.is_dir()` was always False and this whole sync silently
    no-op'ed for every real module, every time, since it was built -- exactly the "fail
    open" behavior masking a real, total failure rather than a genuine absence.

    Fixed by reusing the same real, already-audited, env-based SSH mechanism every other
    real module-content read in this codebase already uses
    (`tools_odoo.module_dev.toolchain._run_in_container`) -- lists the module's real files
    remotely, reads each one back as text (every real Odoo source file this parser reads --
    .py/.xml/.csv/.pot -- is text, so this is binary-safe by construction, unlike a raw tar
    stream over a `text=True` subprocess pipe), and reconstructs them under a local temp
    directory `parse_module()` (which needs a real local `Path`) can read. Returns None if
    the module genuinely doesn't exist on the remote host (a real "nothing to sync yet"
    case, not an error) or if the SSH round-trip itself fails (fails open, logged).
    """
    from tools_odoo.module_dev.toolchain import _MODULE_DEV_ADDONS_DIR as _REMOTE_ADDONS_DIR
    from tools_odoo.module_dev.toolchain import _run_in_container

    remote_module_dir = f"{_REMOTE_ADDONS_DIR}/{module_name}"
    # Real, confirmed issue found live while testing this fix: an unfiltered `find -type f`
    # picks up real __pycache__/*.pyc bytecode files, which are binary and crash
    # `_run_in_container`'s own `text=True` decode with a hard UnicodeDecodeError (not a
    # non-zero returncode this function's own try/except-per-file could otherwise absorb).
    # parse_module() never reads .pyc files anyway (Stage A only parses real .py/.xml/.csv/
    # .pot source) -- excluded at the `find` level so this never wastes an SSH round-trip on
    # something that would just get discarded.
    find_result = _run_in_container(
        f"find {remote_module_dir} -type f -not -path '*/__pycache__/*' -not -name '*.pyc' 2>/dev/null"
    )
    if find_result.returncode != 0 or not find_result.stdout.strip():
        return None  # module not present on the remote host -- nothing real to (re-)sync yet

    remote_paths = [p for p in find_result.stdout.strip().splitlines() if p.strip()]
    local_root = Path(tempfile.mkdtemp(prefix=f"oma_kg_incr_{module_name}_"))
    local_module_dir = local_root / module_name

    try:
        for remote_path in remote_paths:
            rel_path = remote_path[len(remote_module_dir):].lstrip("/")
            if not rel_path:
                continue
            try:
                cat_result = _run_in_container(f"cat {remote_path}")
            except UnicodeDecodeError:
                # A real, non-.pyc binary file (e.g. a stray image/db dump under the module
                # dir) -- not something Stage A's parser reads either way. Skip, don't abort
                # the whole fetch over one file this parser was never going to use.
                continue
            if cat_result.returncode != 0:
                continue  # one unreadable file (permissions, symlink, etc.) -- skip, don't abort the whole fetch
            local_file = local_module_dir / rel_path
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text(cat_result.stdout)
    except Exception:
        shutil.rmtree(local_root, ignore_errors=True)
        raise

    if not local_module_dir.is_dir():
        shutil.rmtree(local_root, ignore_errors=True)
        return None

    return local_module_dir


def _reparse_and_update_source_jsonl(module_name: str) -> None:
    module_dir = _fetch_module_to_local_temp(module_name)
    if module_dir is None:
        return  # module not present on the remote host -- nothing real to (re-)sync yet

    from tools_odoo.knowledge_graph.parser import parse_module

    try:
        fresh_record = parse_module(module_dir).to_dict()
    finally:
        shutil.rmtree(module_dir.parent, ignore_errors=True)

    lines: list[str] = []
    if _SOURCE_JSONL.exists():
        lines = _SOURCE_JSONL.read_text().splitlines(keepends=True)

    new_lines: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            existing = json.loads(stripped)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue
        if existing.get("module") == module_name:
            new_lines.append(json.dumps(fresh_record) + "\n")
            replaced = True
        else:
            new_lines.append(line if line.endswith("\n") else line + "\n")
    if not replaced:
        new_lines.append(json.dumps(fresh_record) + "\n")

    _SOURCE_JSONL.write_text("".join(new_lines))


def _run_etl_incremental(module_name: str) -> None:
    if not _ETL_SCRIPT.exists():
        return
    result = subprocess.run(
        [sys.executable, str(_ETL_SCRIPT), "--mode", "incremental", "--module", module_name],
        capture_output=True,
        text=True,
        timeout=_ETL_TIMEOUT_S,
    )
    if result.returncode != 0:
        logger.warning(
            "incremental ETL run failed for module %r (rc=%d): %s",
            module_name, result.returncode, result.stderr[-2000:],
        )
