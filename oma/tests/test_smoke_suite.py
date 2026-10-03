"""Live integration test for tools_odoo/smoke_suite.py (Phase 35 §9.2 item 1).

Runs the actual smoke suite against the real live Odoo dev instance --
skipped automatically when the OMA_ODOO_* env vars aren't loaded (e.g. in a
CI environment with no network path to the dev host), since this is
deliberately a real-system check, not a mocked unit test.

Confirmed working live 2026-08-12: all three checks pass, and no
__phase35_smoke% records are left behind afterward (verified directly via
search() against res.partner/sale.order/project.task/project.project).
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("OMA_ODOO_URL"),
    reason="requires live OMA_ODOO_* env vars and network access to the dev host",
)


def test_smoke_suite_all_checks_pass():
    from tools_odoo.smoke_suite import run_all

    results = run_all()
    failures = [r for r in results if not r.passed]
    assert not failures, f"smoke suite failures: {failures}"


def test_smoke_suite_cleans_up_after_itself():
    import xmlrpc.client

    from infra.odoo_jit_apikey import create_task_api_key, revoke_task_api_key
    from tools_odoo.smoke_suite import run_all

    run_all()

    url = os.environ["OMA_ODOO_URL"]
    db = os.environ["OMA_ODOO_DB"]
    user = os.environ["OMA_ODOO_USER"]
    row_id, key = create_task_api_key(db, user, "verify-smoke-cleanup")
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, user, key, {})
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        for model in ("res.partner", "sale.order", "project.task", "project.project"):
            leftover = models.execute_kw(
                db, uid, key, model, "search", [[("name", "like", "__phase35_smoke%")]]
            )
            assert leftover == [], f"{model} left behind smoke records: {leftover}"
    finally:
        revoke_task_api_key(db, row_id)
