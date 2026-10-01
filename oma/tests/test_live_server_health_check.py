"""Phase 32 implementation (2026-08-11): unit tests for the new
deployed-and-observed health check (tools_odoo/live_server_health_check.py),
detect-only per Phase 32 section 3.1 item 2. Reproduces the exact real
fault confirmed live tonight (KeyError: 'oma.equipment' from odoo/modules/
registry.py raised through XML-RPC as a Fault) plus a genuinely healthy
call and a genuinely unrelated failure, so this new check never mistakes
one for another.
"""

import os
import sys
import xmlrpc.client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.live_server_health_check as health_check_module
from tools_odoo.live_server_health_check import check_live_server_can_reach_model


class _FakeModelsProxy:
    def __init__(self, fault_string: str | None):
        self._fault_string = fault_string

    def execute_kw(self, db, uid, api_key, model_name, method, args, kwargs):
        if self._fault_string is not None:
            raise xmlrpc.client.Fault(1, self._fault_string)
        return {"name": {"type": "char"}}


def _patch_shared_key_and_proxy(monkeypatch, fault_string):
    import tools_odoo.odoo_schema_client as schema_client_module

    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", lambda db, login: (2, "fake-key"))
    monkeypatch.setattr(schema_client_module, "_models_proxy", lambda db: _FakeModelsProxy(fault_string))


def test_healthy_model_reports_reachable(monkeypatch):
    _patch_shared_key_and_proxy(monkeypatch, fault_string=None)
    result = check_live_server_can_reach_model("oma.equipment", "odoo16_dev")
    assert result.reachable is True
    assert result.stale_registry_detected is False
    print("PASS: a genuinely reachable model reports reachable=True, stale_registry_detected=False")


def test_real_confirmed_key_error_signature_detected_as_stale_registry(monkeypatch):
    """Reproduces the exact live fault (2026-08-11): odoo/modules/registry.py
    raising KeyError('oma.equipment') inside the long-lived web server
    process, surfaced to an XML-RPC caller as a Fault whose faultString
    contains the Python traceback text."""
    fault_string = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3/dist-packages/odoo/modules/registry.py", line 186, in __getitem__\n'
        "    return self.models[name]\n"
        "KeyError: 'oma.equipment'\n"
    )
    _patch_shared_key_and_proxy(monkeypatch, fault_string=fault_string)
    result = check_live_server_can_reach_model("oma.equipment", "odoo16_dev")
    assert result.reachable is False
    assert result.stale_registry_detected is True
    warning = health_check_module.format_stale_registry_warning(result)
    assert "oma.equipment" in warning
    assert "odoo16-dev" in warning
    print("PASS: the real, confirmed live KeyError signature is correctly classified as stale_registry_detected=True")


def test_unrelated_failure_is_not_misclassified_as_stale_registry(monkeypatch):
    fault_string = "AccessError: You are not allowed to access 'Equipment' (oma.equipment) records."
    _patch_shared_key_and_proxy(monkeypatch, fault_string=fault_string)
    result = check_live_server_can_reach_model("oma.equipment", "odoo16_dev")
    assert result.reachable is False
    assert result.stale_registry_detected is False, (
        "a genuine, unrelated failure (e.g. an access-rights error) must never be misreported "
        "as the stale-registry signature -- that would train a human to ignore a real warning"
    )
    print("PASS: a genuinely different failure (AccessError) is not misclassified as stale-registry")


def test_a_different_models_keyerror_does_not_false_positive_on_this_models_name(monkeypatch):
    fault_string = "KeyError: 'oma.service.ticket'\n"
    _patch_shared_key_and_proxy(monkeypatch, fault_string=fault_string)
    result = check_live_server_can_reach_model("oma.equipment", "odoo16_dev")
    assert result.stale_registry_detected is False, (
        "the stale-registry signature must be matched against THIS check's own model name, "
        "never any KeyError text regardless of which model it names"
    )
    print("PASS: a KeyError naming a DIFFERENT model does not false-positive for this check's model")


def test_network_failure_never_raises_and_is_reported_unreachable(monkeypatch):
    import tools_odoo.odoo_schema_client as schema_client_module

    def boom(db, login):
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", boom)
    result = check_live_server_can_reach_model("oma.equipment", "odoo16_dev")
    assert result.reachable is False
    assert result.stale_registry_detected is False
    print("PASS: a network/connection failure never raises out of this detect-only check")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
