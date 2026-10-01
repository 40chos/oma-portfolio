"""P14 item 1 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§18c.4b): tests for BuildSpecialist._run_data_change() -- the real, skill-compliant data_change
handler, closing P12 item 1 (route-disable) and P6 (orphaned skill) as one real capability.

Every external call (extraction, JIT API key, XML-RPC authenticate, OdooToolClient, fencing) is
mocked -- zero live LLM/GPU/network/Postgres calls. This mirrors the real skill's own documented
procedure step for step; each test targets one real, named failure mode or the real success path.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contracts.data_change_extraction import DataChangeOperation
from contracts.schema import AutonomyTier, CapabilityClass, SpecialistType, TaskContract
from infra.fencing import LockHandle
from specialists.build.specialist import BuildSpecialist


def _make_contract(goal="Update phone on the contact with email x@example.com") -> TaskContract:
    return TaskContract(
        task_id=uuid.uuid4(), specialist_type=SpecialistType.bug_fix,
        capability_class=CapabilityClass.data_change, tier=AutonomyTier.tier_2_notify_after,
        goal=goal, inputs=[], rules=[], deliverables=[], compensating_actions=[],
        validation_by="testing_qa", pause_if=[], turn_budget=10,
    )


_COMPLETE_OP = DataChangeOperation(
    model="res.partner", record_search_field="email", record_search_value="x@example.com",
    field="phone", new_value="555-1234",
)


def _run(coro):
    return asyncio.run(coro)


def test_incomplete_extraction_reports_honestly_never_guesses():
    specialist = BuildSpecialist(client=None, db="odoo16_sandbox_test")
    contract = _make_contract()

    async def fake_extract(goal, client, model, task_id=None):
        return DataChangeOperation(model="res.partner")  # incomplete -- missing search/field/value

    async def run():
        with patch("contracts.data_change_extraction.extract_data_change_operation", new=fake_extract):
            return await specialist._run_data_change(contract)

    result = _run(run())
    assert result.claims_complete is False
    assert "Could not extract" in result.summary
    print("PASS: an incomplete extraction is reported honestly, never guessed at")


def test_real_success_path_writes_and_cleans_up():
    specialist = BuildSpecialist(client=None, db="odoo16_sandbox_test")
    contract = _make_contract()

    async def fake_extract(goal, client, model, task_id=None):
        return _COMPLETE_OP

    fake_odoo_client = MagicMock()
    fake_odoo_client.search_read.return_value = [{"id": 42, "phone": "old", "__last_update": "2026-08-01 00:00:00"}]
    fake_odoo_client.write.return_value = None
    fake_lock = LockHandle(module="data_change:res.partner:email:x@example.com", task_id=str(contract.task_id), fence_token=1, acquired=True)

    with patch("contracts.data_change_extraction.extract_data_change_operation", new=fake_extract), \
         patch("infra.odoo_jit_apikey.create_task_api_key", return_value=(99, "fake-key")) as mock_create_key, \
         patch("infra.odoo_jit_apikey.revoke_task_api_key") as mock_revoke_key, \
         patch("infra.odoo_settings.load_odoo_settings", return_value=MagicMock(url="http://fake-odoo:8071")), \
         patch("xmlrpc.client.ServerProxy") as mock_proxy_cls, \
         patch("tools_odoo.odoo_tool_client.OdooToolClient", return_value=fake_odoo_client), \
         patch("specialists.build.specialist.acquire_module_lock", return_value=fake_lock) as mock_acquire, \
         patch("specialists.build.specialist.check_fence", return_value=True) as mock_check_fence, \
         patch("specialists.build.specialist.release_module_lock") as mock_release:
        mock_proxy_cls.return_value.authenticate.return_value = 7  # a real, truthy uid
        result = _run(specialist._run_data_change(contract))

    assert result.claims_complete is True, result.summary
    assert result.detail["record_id"] == 42
    fake_odoo_client.write.assert_called_once_with(
        "res.partner", 42, {"phone": "555-1234"}, "2026-08-01 00:00:00",
    )
    mock_acquire.assert_called_once()
    mock_check_fence.assert_called_once()
    mock_release.assert_called_once()
    mock_create_key.assert_called_once()
    mock_revoke_key.assert_called_once_with("odoo16_sandbox_test", 99)
    print("PASS: the real success path writes exactly once with the real record id/__last_update, and cleans up the lock + JIT key")


def test_record_not_found_reports_honestly_and_still_revokes_key():
    specialist = BuildSpecialist(client=None, db="odoo16_sandbox_test")
    contract = _make_contract()

    async def fake_extract(goal, client, model, task_id=None):
        return _COMPLETE_OP

    fake_odoo_client = MagicMock()
    fake_odoo_client.search_read.return_value = []  # no matching record

    with patch("contracts.data_change_extraction.extract_data_change_operation", new=fake_extract), \
         patch("infra.odoo_jit_apikey.create_task_api_key", return_value=(99, "fake-key")), \
         patch("infra.odoo_jit_apikey.revoke_task_api_key") as mock_revoke_key, \
         patch("infra.odoo_settings.load_odoo_settings", return_value=MagicMock(url="http://fake-odoo:8071")), \
         patch("xmlrpc.client.ServerProxy") as mock_proxy_cls, \
         patch("tools_odoo.odoo_tool_client.OdooToolClient", return_value=fake_odoo_client), \
         patch("specialists.build.specialist.acquire_module_lock") as mock_acquire:
        mock_proxy_cls.return_value.authenticate.return_value = 7
        result = _run(specialist._run_data_change(contract))

    assert result.claims_complete is False
    assert "No res.partner record found" in result.summary
    mock_acquire.assert_not_called()  # never even tries to lock a record that doesn't exist
    mock_revoke_key.assert_called_once()  # cleanup still happens
    print("PASS: a record-not-found is reported honestly, never locks anything, still revokes the JIT key")


def test_lock_not_acquired_reports_honestly():
    specialist = BuildSpecialist(client=None, db="odoo16_sandbox_test")
    contract = _make_contract()

    async def fake_extract(goal, client, model, task_id=None):
        return _COMPLETE_OP

    fake_odoo_client = MagicMock()
    fake_odoo_client.search_read.return_value = [{"id": 42, "phone": "old", "__last_update": "x"}]
    not_acquired = LockHandle(module="x", task_id=str(contract.task_id), fence_token=-1, acquired=False)

    with patch("contracts.data_change_extraction.extract_data_change_operation", new=fake_extract), \
         patch("infra.odoo_jit_apikey.create_task_api_key", return_value=(99, "fake-key")), \
         patch("infra.odoo_jit_apikey.revoke_task_api_key") as mock_revoke_key, \
         patch("infra.odoo_settings.load_odoo_settings", return_value=MagicMock(url="http://fake-odoo:8071")), \
         patch("xmlrpc.client.ServerProxy") as mock_proxy_cls, \
         patch("tools_odoo.odoo_tool_client.OdooToolClient", return_value=fake_odoo_client), \
         patch("specialists.build.specialist.acquire_module_lock", return_value=not_acquired), \
         patch("specialists.build.specialist.release_module_lock") as mock_release:
        mock_proxy_cls.return_value.authenticate.return_value = 7
        result = _run(specialist._run_data_change(contract))

    assert result.claims_complete is False
    assert "Could not acquire the lock" in result.summary
    mock_release.assert_not_called()  # never acquired, so never released
    mock_revoke_key.assert_called_once()
    print("PASS: a failed lock acquisition is reported honestly, never releases a lock it never held")


def test_allowlist_violation_is_caught_and_reported_never_crashes():
    from tools_odoo.odoo_tool_client import AllowlistViolationError

    specialist = BuildSpecialist(client=None, db="odoo16_sandbox_test")
    contract = _make_contract()

    async def fake_extract(goal, client, model, task_id=None):
        return DataChangeOperation(
            model="sale.order", record_search_field="name", record_search_value="S00001",
            field="state", new_value="done",
        )

    fake_odoo_client = MagicMock()
    fake_odoo_client.search_read.side_effect = AllowlistViolationError("sale.order/search_read not on the allowlist")

    with patch("contracts.data_change_extraction.extract_data_change_operation", new=fake_extract), \
         patch("infra.odoo_jit_apikey.create_task_api_key", return_value=(99, "fake-key")), \
         patch("infra.odoo_jit_apikey.revoke_task_api_key") as mock_revoke_key, \
         patch("infra.odoo_settings.load_odoo_settings", return_value=MagicMock(url="http://fake-odoo:8071")), \
         patch("xmlrpc.client.ServerProxy") as mock_proxy_cls, \
         patch("tools_odoo.odoo_tool_client.OdooToolClient", return_value=fake_odoo_client):
        mock_proxy_cls.return_value.authenticate.return_value = 7
        result = _run(specialist._run_data_change(contract))

    assert result.claims_complete is False
    assert "Refused" in result.summary
    mock_revoke_key.assert_called_once()
    print("PASS: an allowlist violation is caught, reported honestly, never crashes, still cleans up")


def test_concurrency_conflict_is_caught_and_reported():
    from tools_odoo.odoo_tool_client import ConcurrencyConflictError

    specialist = BuildSpecialist(client=None, db="odoo16_sandbox_test")
    contract = _make_contract()

    async def fake_extract(goal, client, model, task_id=None):
        return _COMPLETE_OP

    fake_odoo_client = MagicMock()
    fake_odoo_client.search_read.return_value = [{"id": 42, "phone": "old", "__last_update": "stale"}]
    fake_odoo_client.write.side_effect = ConcurrencyConflictError("res.partner record 42 was modified since it was last read")
    fake_lock = LockHandle(module="x", task_id=str(contract.task_id), fence_token=1, acquired=True)

    with patch("contracts.data_change_extraction.extract_data_change_operation", new=fake_extract), \
         patch("infra.odoo_jit_apikey.create_task_api_key", return_value=(99, "fake-key")), \
         patch("infra.odoo_jit_apikey.revoke_task_api_key") as mock_revoke_key, \
         patch("infra.odoo_settings.load_odoo_settings", return_value=MagicMock(url="http://fake-odoo:8071")), \
         patch("xmlrpc.client.ServerProxy") as mock_proxy_cls, \
         patch("tools_odoo.odoo_tool_client.OdooToolClient", return_value=fake_odoo_client), \
         patch("specialists.build.specialist.acquire_module_lock", return_value=fake_lock), \
         patch("specialists.build.specialist.check_fence", return_value=True), \
         patch("specialists.build.specialist.release_module_lock") as mock_release:
        mock_proxy_cls.return_value.authenticate.return_value = 7
        result = _run(specialist._run_data_change(contract))

    assert result.claims_complete is False
    assert "Refused" in result.summary
    mock_release.assert_called_once()  # the lock WAS acquired, so it must still be released
    mock_revoke_key.assert_called_once()
    print("PASS: a concurrency conflict from OdooToolClient.write() is caught, reported, and the lock is still released")


def test_data_change_capability_class_route_is_still_gated_off_by_default():
    """P12 item 1's own pre-flight gate (manager/capability_readiness.py) must remain untouched by
    this change -- enabling this handler live is a deliberate, separate decision.
    """
    from manager.capability_readiness import capability_class_is_ready
    assert capability_class_is_ready("data_change") is False
    print("PASS: manager/capability_readiness.py's data_change gate is still False -- this handler is real and tested, but not flipped live")


if __name__ == "__main__":
    test_incomplete_extraction_reports_honestly_never_guesses()
    test_real_success_path_writes_and_cleans_up()
    test_record_not_found_reports_honestly_and_still_revokes_key()
    test_lock_not_acquired_reports_honestly()
    test_allowlist_violation_is_caught_and_reported_never_crashes()
    test_concurrency_conflict_is_caught_and_reported()
    test_data_change_capability_class_route_is_still_gated_off_by_default()
    print("\nALL RUN_DATA_CHANGE HANDLER TESTS PASSED")
