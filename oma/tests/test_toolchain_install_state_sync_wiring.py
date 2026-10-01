"""Phase 36 §13.2/§13.4 -- confirms install_module() actually fires the new
install-state-sync hook on a real success, on BOTH of its real success
paths (the warm-worker fast path and the plain subprocess path), and does
NOT fire it on any failure return. Mirrors this test directory's own
established monkeypatch style for toolchain.install_module (see
test_toolchain_install_module_test_summary_wins_over_traceback_text.py).
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module
from tools_odoo.module_dev.toolchain import InstallResult, install_module


def _setup(monkeypatch, combined_stdout: str, state: str = "installed"):
    def fake_get_or_create_shared_key(db, login):
        return 2, "fake-api-key"

    monkeypatch.setattr(toolchain_module, "_get_or_create_shared_key", fake_get_or_create_shared_key, raising=False)
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", fake_get_or_create_shared_key)
    monkeypatch.setattr(toolchain_module, "_find_missing_manifest_data_files", lambda *a, **k: None)
    monkeypatch.setattr(toolchain_module, "is_fast_path_eligible", lambda db: False)

    calls = {"n": 0}

    def fake_run_in_container(bash_command, timeout=180, container=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=combined_stdout, stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"INSTALL_STATE_CHECK:{state}\n", stderr="")

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)


_CLEAN_INSTALL_LOG = (
    "2026-08-13 00:00:00,000 1 INFO db odoo.modules.loading: Module oma_x loaded in 1.0s, 10 queries\n"
)


class TestSubprocessPathSuccessFiresHook:
    def test_success_calls_trigger_install_state_sync_with_module_db_and_task_id(self, monkeypatch):
        _setup(monkeypatch, _CLEAN_INSTALL_LOG, state="installed")
        mock_trigger = MagicMock()
        monkeypatch.setattr(toolchain_module, "_trigger_install_state_sync", mock_trigger)

        result = install_module(
            "oma_x", "odoo16_sandbox_20260813_000000", remote_port=8072, sandbox=True,
        )

        assert result.success is True
        mock_trigger.assert_called_once_with("oma_x", "odoo16_sandbox_20260813_000000", None)

    def test_state_verification_failure_does_not_fire_the_hook(self, monkeypatch):
        _setup(monkeypatch, _CLEAN_INSTALL_LOG, state="uninstalled")
        mock_trigger = MagicMock()
        monkeypatch.setattr(toolchain_module, "_trigger_install_state_sync", mock_trigger)

        result = install_module(
            "oma_x", "odoo16_sandbox_20260813_000000", remote_port=8072, sandbox=True,
        )

        assert result.success is False
        mock_trigger.assert_not_called()

    def test_generic_failure_does_not_fire_the_hook(self, monkeypatch):
        _setup(monkeypatch, "CRITICAL something exploded\n")
        mock_trigger = MagicMock()
        monkeypatch.setattr(toolchain_module, "_trigger_install_state_sync", mock_trigger)

        result = install_module("oma_x", "odoo16_sandbox_20260813_000000", remote_port=8072, sandbox=True)

        assert result.success is False
        mock_trigger.assert_not_called()

    def test_manifest_missing_file_early_return_does_not_fire_the_hook(self, monkeypatch):
        _setup(monkeypatch, _CLEAN_INSTALL_LOG)
        monkeypatch.setattr(toolchain_module, "_find_missing_manifest_data_files", lambda *a, **k: ["data/x.xml"])
        mock_trigger = MagicMock()
        monkeypatch.setattr(toolchain_module, "_trigger_install_state_sync", mock_trigger)

        result = install_module("oma_x", "odoo16_sandbox_20260813_000000", remote_port=8072, sandbox=True)

        assert result.success is False
        assert result.error_kind == "manifest_references_missing_file"
        mock_trigger.assert_not_called()


class TestWarmWorkerPathSuccessFiresHook:
    def test_warm_worker_success_fires_the_hook(self, monkeypatch):
        monkeypatch.setattr(toolchain_module, "_find_missing_manifest_data_files", lambda *a, **k: None)
        monkeypatch.setattr(toolchain_module, "is_fast_path_eligible", lambda db: True)
        warm_success = InstallResult(
            module_name="oma_x", db="odoo16_dev", success=True, message="Install completed (warm worker).",
        )
        monkeypatch.setattr(toolchain_module, "_install_module_via_warm_worker", lambda module_name, db: warm_success)
        mock_trigger = MagicMock()
        monkeypatch.setattr(toolchain_module, "_trigger_install_state_sync", mock_trigger)
        monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)

        result = install_module("oma_x", "odoo16_dev", remote_port=8071, task_id="task-456")

        assert result is warm_success
        mock_trigger.assert_called_once_with("oma_x", "odoo16_dev", "task-456")

    def test_warm_worker_failure_does_not_fire_the_hook(self, monkeypatch):
        monkeypatch.setattr(toolchain_module, "_find_missing_manifest_data_files", lambda *a, **k: None)
        monkeypatch.setattr(toolchain_module, "is_fast_path_eligible", lambda db: True)
        warm_failure = InstallResult(
            module_name="oma_x", db="odoo16_dev", success=False, error_kind="state_verification_failed",
            message="did not reach installed",
        )
        monkeypatch.setattr(toolchain_module, "_install_module_via_warm_worker", lambda module_name, db: warm_failure)
        mock_trigger = MagicMock()
        monkeypatch.setattr(toolchain_module, "_trigger_install_state_sync", mock_trigger)
        monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)

        result = install_module("oma_x", "odoo16_dev", remote_port=8071, task_id="task-456")

        assert result is warm_failure
        mock_trigger.assert_not_called()


class TestTriggerInstallStateSyncItselfFailsOpen:
    def test_import_error_inside_trigger_never_raises(self, monkeypatch):
        # Simulate the real hook module being genuinely unimportable --
        # _trigger_install_state_sync must swallow this, never propagate
        # into install_module()'s own success return.
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "tools_odoo.knowledge_graph.install_state_sync":
                raise ImportError("boom")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        toolchain_module._trigger_install_state_sync("oma_x", "odoo16_dev", "task-1")  # must not raise
