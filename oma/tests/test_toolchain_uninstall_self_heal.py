"""Phase 20 Area 2 (2026-07-20): unit tests for
tools_odoo/module_dev/toolchain.py's uninstall_module() self-healing
retry -- pure logic against a monkeypatched _run_in_container, no live
SSH/DB needed, same style as test_manager_loop_round_cleanup.py.

Real bug this fixes: click-odoo-uninstall returned rc=0 ("Uninstall
completed") on three separate live tasks (#43 service.ticket, #44
asset.registry, #45 vendor.review, all in the same 2026-07-20
deep-reverify pass) while the module's ir.module.module.state stayed
'installed' -- the model was still genuinely real several rounds
later, causing a false "already exists" rejection on a fresh attempt.
A manual re-run of uninstall_module against the same still-installed
module immediately afterward worked correctly every time. Rather than
trust click-odoo-uninstall's exit code alone, uninstall_module now
independently re-checks the module's real state via a fresh odoo-bin
shell process and retries once before giving up.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module
from tools_odoo.module_dev.toolchain import uninstall_module


def _fake_proc(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_uninstall_succeeds_immediately_when_state_check_confirms_uninstalled(monkeypatch):
    calls = []

    def fake_run_in_container(cmd, timeout=180, container=None):
        calls.append(cmd)
        if "click-odoo-uninstall" in cmd:
            return _fake_proc(0, stdout="")
        return _fake_proc(0, stdout="uninstalled")

    monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)
    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = uninstall_module("oma_test_module", "odoo16_dev")
    assert result.success is True
    assert len(calls) == 2  # one uninstall call, one state-verify call, no retry needed
    print("PASS: uninstall succeeds immediately when the state check confirms uninstalled")


def test_uninstall_retries_and_heals_when_first_claimed_success_is_false(monkeypatch):
    """The real bug found live: rc=0 the first time, but the module is
    still 'installed' -- must retry once, and succeed if the retry
    actually takes effect.
    """
    call_log = []

    def fake_run_in_container(cmd, timeout=180, container=None):
        if "click-odoo-uninstall" in cmd:
            call_log.append("uninstall")
            return _fake_proc(0, stdout="")
        call_log.append("state_check")
        # First state check (after attempt 1): still installed (the
        # false-success case). Second state check (after attempt 2):
        # genuinely uninstalled.
        if call_log.count("state_check") == 1:
            return _fake_proc(0, stdout="installed")
        return _fake_proc(0, stdout="uninstalled")

    monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)
    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = uninstall_module("oma_test_module", "odoo16_dev")
    assert result.success is True
    assert call_log == ["uninstall", "state_check", "uninstall", "state_check"]
    print("PASS: a false-success first attempt is caught and healed by a second attempt")


def test_uninstall_reports_real_failure_when_module_never_actually_leaves_installed_state(monkeypatch):
    def fake_run_in_container(cmd, timeout=180, container=None):
        if "click-odoo-uninstall" in cmd:
            return _fake_proc(0, stdout="")
        return _fake_proc(0, stdout="installed")  # never actually uninstalls, both attempts

    monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)
    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = uninstall_module("oma_test_module", "odoo16_dev")
    assert result.success is False
    assert "still 'installed'" in result.message
    print("PASS: a module that never actually leaves 'installed' after two attempts is honestly reported as failed")


def test_uninstall_real_click_odoo_uninstall_failure_still_returns_cleanly(monkeypatch):
    """Pre-existing bug (2026-07-19, already fixed): a genuine
    click-odoo-uninstall failure (rc != 0) must never crash with
    NameError -- confirms that fix still holds after this rewrite.
    """
    def fake_run_in_container(cmd, timeout=180, container=None):
        return _fake_proc(1, stdout="", stderr="some real click-odoo-uninstall error")

    monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)
    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = uninstall_module("oma_test_module", "odoo16_dev")
    assert result.success is False
    assert result.error_kind == "uninstall_failed"
    assert result.log_tail  # not empty -- the log_tail crash bug stays fixed
    print("PASS: a genuine click-odoo-uninstall rc!=0 failure still returns cleanly, no retry attempted")


def test_uninstall_module_not_found_after_uninstall_treated_as_success(monkeypatch):
    """If the module row can't be found at all post-uninstall (e.g. it
    was fully purged), that's still a real success -- not a reason to
    retry.
    """
    def fake_run_in_container(cmd, timeout=180, container=None):
        if "click-odoo-uninstall" in cmd:
            return _fake_proc(0, stdout="")
        return _fake_proc(0, stdout="MODULE_NOT_FOUND")

    monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)
    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = uninstall_module("oma_test_module", "odoo16_dev")
    assert result.success is True
    print("PASS: module row not found post-uninstall (state=None) is treated as a real success")


if __name__ == "__main__":
    import types

    class _MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    mp = _MonkeyPatch()
    test_uninstall_succeeds_immediately_when_state_check_confirms_uninstalled(mp)
    test_uninstall_retries_and_heals_when_first_claimed_success_is_false(mp)
    test_uninstall_reports_real_failure_when_module_never_actually_leaves_installed_state(mp)
    test_uninstall_real_click_odoo_uninstall_failure_still_returns_cleanly(mp)
    test_uninstall_module_not_found_after_uninstall_treated_as_success(mp)
    print("\nALL TOOLCHAIN UNINSTALL SELF-HEAL TESTS PASSED")
