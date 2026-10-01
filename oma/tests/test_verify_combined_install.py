"""P7b (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md §17,
item 3): tests for verify_combined_install() -- the cross-module analog of the existing
whole-assembly single-module install check. Mocked subprocess result, no live infra call.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.module_dev.toolchain import verify_combined_install


def _fake_proc(stdout: str, returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = returncode
    return proc


_CLEAN_OUTPUT = (
    "2026-08-01 10:00:00,000 1 INFO odoo16_sandbox odoo.modules.loading: Modules loaded. \n"
)

_CRITICAL_OUTPUT = (
    "2026-08-01 10:00:00,000 1 CRITICAL odoo16_sandbox odoo.modules.registry: "
    "Failed to load registry\nTraceback (most recent call last):\n  ...\n"
)

_SKIPPED_OUTPUT = (
    "2026-08-01 10:00:00,000 1 WARNING odoo16_sandbox odoo.modules.graph: "
    "module mod_b: not installable, skipped\n"
)


def test_reports_success_when_the_combined_install_comes_up_cleanly():
    with patch(
        "tools_odoo.module_dev.toolchain._run_in_container", return_value=_fake_proc(_CLEAN_OUTPUT),
    ), patch("tools_odoo.module_dev.toolchain._assert_safe_odoo_target"):
        result = verify_combined_install(["mod_a", "mod_b"], db="odoo16_sandbox_test")
    assert result.success is True
    assert result.module_name == "mod_a+mod_b"
    print("PASS: a clean combined install reports success")


def test_the_real_odoo_bin_command_uses_comma_joined_modules():
    captured = {}

    def fake_run_in_container(cmd, timeout=180, container=None):
        captured["cmd"] = cmd
        return _fake_proc(_CLEAN_OUTPUT)

    with patch(
        "tools_odoo.module_dev.toolchain._run_in_container", side_effect=fake_run_in_container,
    ), patch("tools_odoo.module_dev.toolchain._assert_safe_odoo_target"):
        verify_combined_install(["mod_a", "mod_b", "mod_c"], db="odoo16_sandbox_test")
    assert "-i mod_a,mod_b,mod_c" in captured["cmd"]
    print("PASS: the real odoo-bin -i flag gets a comma-separated module list, one combined registry load")


def test_reports_failure_on_a_critical_traceback():
    with patch(
        "tools_odoo.module_dev.toolchain._run_in_container", return_value=_fake_proc(_CRITICAL_OUTPUT),
    ), patch("tools_odoo.module_dev.toolchain._assert_safe_odoo_target"):
        result = verify_combined_install(["mod_a", "mod_b"], db="odoo16_sandbox_test")
    assert result.success is False
    assert result.error_kind == "generic_failure"
    print("PASS: a real CRITICAL/Traceback in the combined install output is reported as a failure")


def test_reports_failure_when_a_module_is_silently_skipped_as_not_installable():
    with patch(
        "tools_odoo.module_dev.toolchain._run_in_container", return_value=_fake_proc(_SKIPPED_OUTPUT),
    ), patch("tools_odoo.module_dev.toolchain._assert_safe_odoo_target"):
        result = verify_combined_install(["mod_a", "mod_b"], db="odoo16_sandbox_test")
    assert result.success is False
    assert result.error_kind == "not_installable"
    assert "mod_b" in result.message
    print("PASS: a module silently skipped as not-installable is caught even with rc=0 and no CRITICAL")


def test_safety_guard_is_always_checked_first():
    with patch(
        "tools_odoo.module_dev.toolchain._run_in_container", return_value=_fake_proc(_CLEAN_OUTPUT),
    ) as mock_run, patch(
        "tools_odoo.module_dev.toolchain._assert_safe_odoo_target",
        side_effect=RuntimeError("unsafe target"),
    ):
        try:
            verify_combined_install(["mod_a", "mod_b"], db="production_db")
            raise AssertionError("expected the safety guard to raise before any real command ran")
        except RuntimeError:
            pass
    mock_run.assert_not_called()
    print("PASS: the production-safety guard is checked before any real odoo-bin command runs")


if __name__ == "__main__":
    test_reports_success_when_the_combined_install_comes_up_cleanly()
    test_the_real_odoo_bin_command_uses_comma_joined_modules()
    test_reports_failure_on_a_critical_traceback()
    test_reports_failure_when_a_module_is_silently_skipped_as_not_installable()
    test_safety_guard_is_always_checked_first()
    print("\nALL VERIFY_COMBINED_INSTALL TESTS PASSED")
