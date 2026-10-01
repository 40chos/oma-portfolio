"""Phase 20 Area 2 (2026-07-20, UPDATE 20): unit tests for
tools_odoo/module_dev/toolchain.py's _find_missing_manifest_data_files()
and its wiring into install_module() -- pure logic against a
monkeypatched _run_in_container, no live SSH/DB needed.

Real bug this fixes: task #54's __manifest__.py referenced
'security/security.xml' in its 'data' list, but the file was never
actually written to the container (a stale-baseline scoped-edit
patch) -- odoo-bin's own install crashed deep inside module loading
with a raw FileNotFoundError instead of a clean, actionable result.
This is a mandatory pre-install consistency gate, the same role
Terraform's own `validate` step plays before `apply`.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module
from tools_odoo.module_dev.toolchain import _find_missing_manifest_data_files, install_module


def _fake_proc(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


_MANIFEST_WITH_MISSING_FILE = repr({
    "name": "oma_test", "version": "0.1", "depends": ["base"],
    "data": ["security/ir.model.access.csv", "security/security.xml"],
})

_MANIFEST_ALL_PRESENT = repr({
    "name": "oma_test", "version": "0.1", "depends": ["base"],
    "data": ["security/ir.model.access.csv"],
})


def test_detects_a_missing_declared_file(monkeypatch):
    def fake_run_in_container(cmd, timeout=180, container=None):
        if "cat" in cmd and "__manifest__.py" in cmd:
            return _fake_proc(0, stdout=_MANIFEST_WITH_MISSING_FILE)
        # the existence-check loop: report security.xml missing, csv present
        return _fake_proc(0, stdout="MISSING:/mnt/extra-addons/oma_test/security/security.xml\n")

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)
    missing = _find_missing_manifest_data_files("oma_test")
    assert missing == ["security/security.xml"]
    print("PASS: a declared-but-missing file is correctly detected")


def test_returns_empty_list_when_everything_present(monkeypatch):
    def fake_run_in_container(cmd, timeout=180, container=None):
        if "cat" in cmd and "__manifest__.py" in cmd:
            return _fake_proc(0, stdout=_MANIFEST_ALL_PRESENT)
        return _fake_proc(0, stdout="")  # no MISSING: lines

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)
    missing = _find_missing_manifest_data_files("oma_test")
    assert missing == []
    print("PASS: a manifest whose every declared file is present returns an empty list, not None")


def test_returns_none_when_manifest_unreadable(monkeypatch):
    def fake_run_in_container(cmd, timeout=180, container=None):
        return _fake_proc(1, stdout="", stderr="No such file or directory")

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)
    missing = _find_missing_manifest_data_files("oma_test")
    assert missing is None
    print("PASS: an unreadable manifest returns None (inconclusive), never a false empty list")


def test_returns_none_when_manifest_unparsable(monkeypatch):
    def fake_run_in_container(cmd, timeout=180, container=None):
        return _fake_proc(0, stdout="not a python literal {{{")

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)
    missing = _find_missing_manifest_data_files("oma_test")
    assert missing is None
    print("PASS: an unparsable manifest returns None (inconclusive)")


def test_install_module_refuses_cleanly_instead_of_crashing_when_file_missing(monkeypatch):
    calls = []

    def fake_run_in_container(cmd, timeout=180, container=None):
        calls.append(cmd)
        if "cat" in cmd and "__manifest__.py" in cmd:
            return _fake_proc(0, stdout=_MANIFEST_WITH_MISSING_FILE)
        if "-i " in cmd:
            raise AssertionError("odoo-bin install must never run when a declared file is missing")
        return _fake_proc(0, stdout="MISSING:/mnt/extra-addons/oma_test/security/security.xml\n")

    monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)
    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = install_module("oma_test", "odoo16_dev")
    assert result.success is False
    assert result.error_kind == "manifest_references_missing_file"
    assert "security/security.xml" in result.message
    print("PASS: install_module refuses cleanly with a structured result instead of ever running odoo-bin")


def test_install_module_proceeds_normally_when_gate_is_inconclusive(monkeypatch):
    """A manifest read/parse failure must never BLOCK install -- the
    gate is a safety net, not a hard precondition; the real odoo-bin
    install command must still run normally in that case (its own,
    separate result -- success or failure -- is out of scope for this
    test, which only proves the new gate doesn't short-circuit it).
    """
    install_command_ran = []

    def fake_run_in_container(cmd, timeout=180, container=None):
        if "cat" in cmd and "__manifest__.py" in cmd:
            return _fake_proc(1, stdout="", stderr="permission denied")
        if "-i " in cmd:
            install_command_ran.append(cmd)
        return _fake_proc(0, stdout="INFO odoo: Modules loaded.\n")

    monkeypatch.setattr(toolchain_module, "_assert_safe_odoo_target", lambda db, port: None)
    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    install_module("oma_test", "odoo16_dev")
    assert install_command_ran, "the real odoo-bin install command must still run when the gate is inconclusive"
    print("PASS: an inconclusive gate result never blocks the real install command from running")


if __name__ == "__main__":
    import types

    class _MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    mp = _MonkeyPatch()
    test_detects_a_missing_declared_file(mp)
    test_returns_empty_list_when_everything_present(mp)
    test_returns_none_when_manifest_unreadable(mp)
    test_returns_none_when_manifest_unparsable(mp)
    test_install_module_refuses_cleanly_instead_of_crashing_when_file_missing(mp)
    test_install_module_proceeds_normally_when_gate_is_inconclusive(mp)

    print("\nALL MANIFEST-MISSING-FILE GATE TESTS PASSED")
