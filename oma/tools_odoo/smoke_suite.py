"""Core-Odoo smoke suite (Phase 35 §9.2 item 1 / §10.1 Day-2 deliverable).

A small, hand-curated, deterministic set of checks against genuinely stock Odoo
functionality Operator relies on daily. Not generated per-task; not tied to
certification progress. Ships and runs independently of everything else in
Phase 35's certification mechanism.

Each check creates a fixed record via XML-RPC, asserts a fixed expected
outcome, and cleans up after itself. A failure here means core Odoo
functionality itself is broken -- not any one generated module.
"""

from __future__ import annotations

import os
import uuid
import xmlrpc.client
from contextlib import contextmanager
from dataclasses import dataclass

from infra.odoo_jit_apikey import create_task_api_key, revoke_task_api_key


@dataclass
class SmokeCheckResult:
    name: str
    passed: bool
    detail: str


@contextmanager
def _connect():
    """Mints a fresh, short-lived JIT API key (the same mechanism every
    real task already uses, per infra/odoo_jit_apikey.py) rather than a
    static credential -- the smoke suite must follow the same
    never-hold-a-permanent-key discipline as everything else in this
    pipeline, not a shortcut."""
    url = os.environ["OMA_ODOO_URL"]
    db = os.environ["OMA_ODOO_DB"]
    user = os.environ["OMA_ODOO_USER"]
    task_id = f"smoke-{uuid.uuid4().hex[:8]}"
    apikeys_row_id, plaintext_key = create_task_api_key(db, user, task_id)
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, user, plaintext_key, {})
        if not uid:
            raise RuntimeError("smoke_suite: Odoo authentication failed")
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        yield db, uid, plaintext_key, models
    finally:
        revoke_task_api_key(db, apikeys_row_id)


def check_create_partner() -> SmokeCheckResult:
    """Can we still create a res.partner (a Contact)?"""
    with _connect() as (db, uid, api_key, models):
        name = "__phase35_smoke_partner__"
        partner_id = models.execute_kw(db, uid, api_key, "res.partner", "create", [{"name": name}])
        try:
            read = models.execute_kw(db, uid, api_key, "res.partner", "read", [[partner_id], ["name"]])
            ok = bool(read) and read[0]["name"] == name
            return SmokeCheckResult("create_partner", ok, f"partner_id={partner_id}, read_back={read}")
        finally:
            models.execute_kw(db, uid, api_key, "res.partner", "unlink", [[partner_id]])


def check_create_sale_order() -> SmokeCheckResult:
    """Can we still create a sale.order (a Quotation)?"""
    with _connect() as (db, uid, api_key, models):
        partner_id = models.execute_kw(
            db, uid, api_key, "res.partner", "create", [{"name": "__phase35_smoke_customer__"}]
        )
        order_id = None
        try:
            order_id = models.execute_kw(db, uid, api_key, "sale.order", "create", [{"partner_id": partner_id}])
            read = models.execute_kw(db, uid, api_key, "sale.order", "read", [[order_id], ["partner_id", "state"]])
            ok = bool(read) and read[0]["partner_id"][0] == partner_id and read[0]["state"] == "draft"
            return SmokeCheckResult("create_sale_order", ok, f"order_id={order_id}, read_back={read}")
        finally:
            if order_id:
                models.execute_kw(db, uid, api_key, "sale.order", "unlink", [[order_id]])
            models.execute_kw(db, uid, api_key, "res.partner", "unlink", [[partner_id]])


def check_create_project_task() -> SmokeCheckResult:
    """Can we still create a project.task?"""
    with _connect() as (db, uid, api_key, models):
        project_id = models.execute_kw(
            db, uid, api_key, "project.project", "create", [{"name": "__phase35_smoke_project__"}]
        )
        task_id = None
        try:
            task_id = models.execute_kw(
                db, uid, api_key, "project.task", "create",
                [{"name": "__phase35_smoke_task__", "project_id": project_id}],
            )
            read = models.execute_kw(db, uid, api_key, "project.task", "read", [[task_id], ["name", "project_id"]])
            ok = bool(read) and read[0]["project_id"][0] == project_id
            return SmokeCheckResult("create_project_task", ok, f"task_id={task_id}, read_back={read}")
        finally:
            if task_id:
                models.execute_kw(db, uid, api_key, "project.task", "unlink", [[task_id]])
            models.execute_kw(db, uid, api_key, "project.project", "unlink", [[project_id]])


ALL_CHECKS = [check_create_partner, check_create_sale_order, check_create_project_task]


def run_all() -> list[SmokeCheckResult]:
    results = []
    for check in ALL_CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001 - a check failing is exactly the signal we want
            results.append(SmokeCheckResult(check.__name__, False, f"raised: {exc}"))
    return results


if __name__ == "__main__":
    outcomes = run_all()
    for r in outcomes:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}: {r.detail}")
    if not all(r.passed for r in outcomes):
        raise SystemExit(1)
