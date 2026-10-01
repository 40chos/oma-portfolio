"""Phase 32 implementation (2026-08-11): the "deployed-and-observed health
check" recommended by docs/planning/PHASE32_RELIABILITY_ROOT_CAUSE_AND_ROLLOUT_STRATEGY_2026-08-11.md
section 3.1 item 2 -- DETECT-ONLY, never auto-remediates.

Real, confirmed root cause this closes: Odoo's long-lived web server
process (odoo16-dev, port 8071 -- the exact process OMA_ODOO_URL's tunnel
points at, and the one every real browser session hits) caches its own
model registry in memory. An automated module install runs in a SEPARATE,
short-lived process (odoo-bin -i / the warm worker on port 8073) and never
signals the long-lived web server to reload. Confirmed live tonight
(2026-08-11): every one of this pipeline's own post-install verification
calls looked completely clean (fresh shell processes, sandbox container),
while a real human browser session against odoo16-dev raised
`KeyError: 'oma.equipment'` in odoo/modules/registry.py. Fixed operationally
by restarting the odoo16-dev container by hand.

This module gives every completed task ONE extra, cheap check: does a
plain XML-RPC call against the SAME process a human would actually use
(port 8071, via the existing odoo_schema_client machinery) succeed for the
model this task just touched? If it raises the specific stale-registry
signature, the task is still reported as completed (the pipeline's own
work was genuinely correct) but the completion notes carry a loud,
unmissable human-facing warning. This deliberately does NOT restart
anything itself -- per Phase 32 section 3.1's own explicit safety
scoping, automating a production-mutating action (even against a dev
box) needs its own safety wrapper (in-flight-deploy check, exact
allow-listed target, post-action health verification, hard-fail-not-
silent-retry) that does not exist yet. Detect and report only.
"""

from __future__ import annotations

import re
import xmlrpc.client
from dataclasses import dataclass

# The exact fault signatures confirmed live tonight (KeyError raised by
# odoo/modules/registry.py when a model isn't in the calling worker's
# in-memory registry) plus the generic "no such object" phrasing XML-RPC
# wraps a server-side KeyError in. Deliberately several patterns, not one --
# this session's own history (Bugs 1-91) is full of narrow, single-wording
# filters that a rephrasing immediately dodged; a real fix here means
# matching the underlying SHAPE (this model name, wrapped in a KeyError-like
# server fault), not one exact string.
_STALE_REGISTRY_FAULT_PATTERNS = (
    re.compile(r"KeyError:\s*['\"]{model}['\"]"),
    re.compile(r"object has no attribute .*{model}", re.IGNORECASE),
    re.compile(r"Model not found[:\s]+.*{model}", re.IGNORECASE),
)


@dataclass
class LiveServerHealthCheckResult:
    model_name: str
    db: str
    reachable: bool
    stale_registry_detected: bool
    detail: str


def _model_name_pattern(model_name: str) -> list[re.Pattern]:
    escaped = re.escape(model_name)
    return [re.compile(p.pattern.format(model=escaped), p.flags) for p in _STALE_REGISTRY_FAULT_PATTERNS]


def _looks_like_stale_registry_fault(error_text: str, model_name: str) -> bool:
    return any(p.search(error_text) for p in _model_name_pattern(model_name))


def check_live_server_can_reach_model(
    model_name: str, db: str, *, login: str = "Admin",
) -> LiveServerHealthCheckResult:
    """Calls fields_get() on `model_name` through the real, long-lived web
    server process (never the sandbox, never the warm worker) using the
    exact same shared-key XML-RPC path odoo_schema_client.py already uses
    for schema verification. Never raises -- any XML-RPC/network failure is
    reported as reachable=False with the raw detail, so a caller can log a
    warning and move on; this must never be able to fail a task on its own.
    """
    try:
        from tools_odoo.odoo_schema_client import _get_or_create_shared_key, _models_proxy

        uid, api_key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        models.execute_kw(db, uid, api_key, model_name, "fields_get", [], {"attributes": []})
        return LiveServerHealthCheckResult(
            model_name=model_name, db=db, reachable=True,
            stale_registry_detected=False, detail="fields_get succeeded against the live web server",
        )
    except xmlrpc.client.Fault as fault:
        error_text = str(fault.faultString or fault)
        stale = _looks_like_stale_registry_fault(error_text, model_name)
        return LiveServerHealthCheckResult(
            model_name=model_name, db=db, reachable=False,
            stale_registry_detected=stale, detail=error_text,
        )
    except Exception as exc:  # noqa: BLE001 -- detect-only, must never raise into a caller's success path
        return LiveServerHealthCheckResult(
            model_name=model_name, db=db, reachable=False,
            stale_registry_detected=False, detail=f"{type(exc).__name__}: {exc}",
        )


def format_stale_registry_warning(result: LiveServerHealthCheckResult) -> str:
    return (
        f"⚠ LIVE SERVER HEALTH CHECK: '{result.model_name}' is not reachable through the real "
        f"web server process on {result.db} (stale in-memory model registry -- the exact "
        f"2026-08-11 registry-staleness bug). The pipeline's own work is otherwise correct, but "
        f"a human browser session will see errors until the odoo16-dev container is restarted. "
        f"Detail: {result.detail}"
    )
