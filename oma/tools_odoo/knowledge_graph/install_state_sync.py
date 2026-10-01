"""Phase 36 §13.2/§13.4 -- real, live install-state tracking hook, fired
right after `tools_odoo.module_dev.toolchain.install_module()` reports a
genuine success.

Design (deliberately matching `incremental_sync.py`'s own established
fire-and-forget/fail-open discipline in this same package, not a new
pattern):

  1. Fire-and-forget (`asyncio.create_task`) -- must never slow down or
     fail an install round on a graph-service hiccup.
  2. Reads the module's REAL, live `ir.module.module.state` via
     `tools_odoo.odoo_schema_client.get_module_state_fast()` (the same
     fast XML-RPC path `install_module()` itself already uses for its own
     post-install verification) rather than assuming "installed" just
     because the caller told us the install succeeded -- the whole point
     of this tracker is a real, independently-confirmed state, not a
     restatement of what the caller already believed.
  3. Writes that real state onto the graph's existing `:Module` node via
     `tools_odoo.graph_queries.update_module_install_state()` -- the one
     write-capable function in this codebase's graph_queries.py, see its
     own docstring.
  4. Fails open (never raises) on ANY error -- a bad module_state read, a
     Neo4j connectivity issue, an import error -- exactly like
     `incremental_sync.py`'s own `_run_incremental_sync` try/except.
  5. A killswitch env var (`OMA_SKIP_INSTALL_STATE_SYNC`) is honored
     before scheduling anything, same name shape and same check-before-
     schedule placement as `incremental_sync.py`'s own
     `OMA_SKIP_GRAPH_INCREMENTAL_SYNC`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_module_install_state(module_name: str, db: str, task_id: str | None = None) -> None:
    """Fire-and-forget entry point -- call this right after
    `install_module()` reports a real, confirmed success. Never raises,
    never blocks the caller.
    """
    if os.environ.get("OMA_SKIP_INSTALL_STATE_SYNC"):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop -- e.g. a script/test calling this
        # synchronously. Fail open rather than raise: this is a
        # best-effort freshness sync, never a hard dependency of the
        # caller's own success (matches incremental_sync.py's identical
        # posture for the identical situation).
        return
    asyncio.create_task(_run_install_state_sync(module_name, db, task_id))


async def _run_install_state_sync(module_name: str, db: str, task_id: str | None) -> None:
    try:
        await asyncio.to_thread(_fetch_and_write_state, module_name, db)
    except Exception as exc:  # noqa: BLE001 -- fire-and-forget, must never propagate
        logger.warning(
            "install state sync failed for module %r db %r (task_id=%s): %s",
            module_name, db, task_id, exc,
        )


def _fetch_and_write_state(module_name: str, db: str) -> None:
    from tools_odoo.odoo_schema_client import get_module_state_fast

    state = get_module_state_fast(module_name, db)
    if state is None:
        # get_module_state_fast's own contract: None means the read itself
        # failed (connectivity/auth/etc.), distinct from a real "NOT_FOUND"
        # state -- nothing real to write in that case.
        return

    from infra.neo4j_client import get_neo4j_driver
    from tools_odoo.graph_queries import update_module_install_state

    driver = get_neo4j_driver()
    update_module_install_state(driver, module_name, state, _utc_now_iso())
