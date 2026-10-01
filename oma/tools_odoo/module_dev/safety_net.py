"""Phase 35 §9/§10 wiring: the pre-install backup, post-install smoke
check, and (optional, off by default) auto-rollback, called automatically
around the real production install step (`specialists/build/specialist.py`'s
call to `install_module()` against `self.db`, never the sandbox install).

Backup and the smoke check itself are deliberately **best-effort, never
task-blocking** on this rollout -- a new safety mechanism that could itself
take down every real task on Day 1 (e.g. a transient SSH hiccup during the
backup call, or a smoke-suite false positive) would be a worse outcome than
the gap it's meant to close. This mirrors standard canary practice: a new
check starts in monitor-only/logged mode, and only becomes a hard gate once
its real false-positive rate is known.

Auto-rollback (reverting the just-installed module on a smoke-check
failure) is the natural completion of the canary loop -- 2026 practice for
this shape of system is "ship automatically, detect fast, roll back fast,"
not a heavyweight staging environment a real request distribution can't be
replicated in anyway. It is implemented here, fully, and tested -- but kept
OFF by default (`OMA_SAFETY_NET_AUTO_ROLLBACK` unset or not "1"), for the
same reason the retrieval fast-path (§3.4) defaults to off: as of this
revision the smoke check has only ever run against two real production
installs, both clean -- not enough evidence yet to trust an automatic,
destructive action on its say-so alone. Once its real false-positive rate
is known (the same standing signal §3.4 already tracks for the retrieval
path), this flag is the one line that turns rollback on; no further code
change is needed. This is a deliberate, disclosed choice, not an
unfinished feature -- see Phase 35 §9/§10's Revision Log for the reasoning.

- Backup failures are logged and swallowed; they never prevent an install
  that would otherwise succeed.
- Smoke-check failures are logged with elevated visibility (a distinct
  trace event, not merged into ordinary install logging) and returned to
  the caller so a human reviewing the task later can see the result --
  but do not flip `claims_complete` on this rollout, and do not trigger
  rollback unless the flag above is explicitly set.
"""

from __future__ import annotations

import os
import subprocess

from manager.trace import publish_trace_event
from paths import REPO_ROOT

_BACKUP_SCRIPT = os.environ.get(
    "OMA_BACKUP_SCRIPT", str(REPO_ROOT / "scripts" / "odoo_filestore_backup.py")
)
_AUTO_ROLLBACK_ENV = "OMA_SAFETY_NET_AUTO_ROLLBACK"


def run_pre_install_backup(task_id: str) -> None:
    """Best-effort: back up the real Odoo filestore, tagged to this task,
    immediately before installing a change into it. Never raises -- a
    backup failure must not turn an otherwise-successful task into a
    failed one; it is logged loudly instead so it's visible without being
    a new production outage vector."""
    publish_trace_event(task_id, {
        "level": "specialist", "actor": "safety_net",
        "message": "Backing up Odoo filestore before install…", "status": "running",
    })
    try:
        result = subprocess.run(
            ["python3", _BACKUP_SCRIPT, "--odoo-filestore", "--tag", task_id],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            publish_trace_event(task_id, {
                "level": "specialist", "actor": "safety_net",
                "message": "Pre-install backup completed.", "status": "done",
            })
        else:
            publish_trace_event(task_id, {
                "level": "specialist", "actor": "safety_net",
                "message": f"Pre-install backup FAILED (non-blocking): {result.stderr[-500:]}",
                "status": "failed",
            })
    except Exception as exc:  # noqa: BLE001 - best-effort by design, see module docstring
        publish_trace_event(task_id, {
            "level": "specialist", "actor": "safety_net",
            "message": f"Pre-install backup raised (non-blocking): {exc}",
            "status": "failed",
        })


def run_post_install_smoke_check(task_id: str, module_name: str | None = None, db: str | None = None) -> dict:
    """Best-effort: run the core-Odoo smoke suite (tools_odoo/smoke_suite.py)
    after a real install completes, and log the result with elevated
    visibility. Does not change the calling task's own success/failure
    status. Returns a plain-dict summary so the caller can attach it to the
    task's own detail payload for later human review.

    `module_name`/`db` are optional (auto-rollback simply can't run without
    them, matching every other best-effort helper in this file's own
    posture -- never a hard requirement) -- when both are given AND
    `OMA_SAFETY_NET_AUTO_ROLLBACK=1` is set in the environment, a smoke-check
    failure automatically calls `uninstall_module()` on the just-installed
    module, completing the canary loop. Off by default; see module
    docstring for why.
    """
    publish_trace_event(task_id, {
        "level": "specialist", "actor": "safety_net",
        "message": "Running post-install regression smoke check…", "status": "running",
    })
    try:
        from tools_odoo.smoke_suite import run_all

        results = run_all()
        failures = [r for r in results if not r.passed]
        summary = {
            "checked": len(results),
            "passed": len(results) - len(failures),
            "failed": len(failures),
            "failures": [{"name": r.name, "detail": r.detail} for r in failures],
            "auto_rollback_attempted": False,
            "auto_rollback_succeeded": None,
        }
        if failures:
            publish_trace_event(task_id, {
                "level": "specialist", "actor": "safety_net",
                "message": (
                    f"REGRESSION SIGNAL: {len(failures)}/{len(results)} core smoke checks failed "
                    f"after this install: {[f['name'] for f in summary['failures']]}"
                ),
                "status": "failed",
            })
            if os.environ.get(_AUTO_ROLLBACK_ENV) == "1" and module_name and db:
                summary["auto_rollback_attempted"] = True
                try:
                    from tools_odoo.module_dev.toolchain import uninstall_module

                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": "safety_net",
                        "message": f"Auto-rollback enabled -- uninstalling {module_name!r} from {db!r}…",
                        "status": "running",
                    })
                    rollback_result = uninstall_module(module_name, db, task_id=task_id)
                    summary["auto_rollback_succeeded"] = rollback_result.success
                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": "safety_net",
                        "message": (
                            f"Auto-rollback of {module_name!r} "
                            + ("succeeded." if rollback_result.success else f"FAILED: {rollback_result.message}")
                        ),
                        "status": "done" if rollback_result.success else "failed",
                    })
                except Exception as rollback_exc:  # noqa: BLE001 - never let rollback itself crash the task
                    summary["auto_rollback_succeeded"] = False
                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": "safety_net",
                        "message": f"Auto-rollback of {module_name!r} raised: {rollback_exc}",
                        "status": "failed",
                    })
        else:
            publish_trace_event(task_id, {
                "level": "specialist", "actor": "safety_net",
                "message": f"All {len(results)} core smoke checks passed after install.",
                "status": "done",
            })
        return summary
    except Exception as exc:  # noqa: BLE001 - best-effort by design, see module docstring
        publish_trace_event(task_id, {
            "level": "specialist", "actor": "safety_net",
            "message": f"Post-install smoke check raised (non-blocking): {exc}",
            "status": "failed",
        })
        return {
            "checked": 0, "passed": 0, "failed": 0, "failures": [], "error": str(exc),
            "auto_rollback_attempted": False, "auto_rollback_succeeded": None,
        }
