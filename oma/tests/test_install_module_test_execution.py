"""Phase 28A (2026-07-28): install_module()'s own new test_enable=True
parsing/gating logic, unit-tested with a mocked subprocess result --
the real, live end-to-end behavior (a genuine module with real,
generated tests, both all-passing and a deliberately-failing case) was
independently confirmed live against the real odoo16-dev2 sandbox
during this same phase's own implementation (see the phase's own
verification report for the full live transcript); this file locks in
the parsing/gating logic itself with fast, repeatable unit coverage.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.module_dev.toolchain import install_module


def _fake_proc(stdout: str, returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = returncode
    return proc


_ALL_PASS_OUTPUT = (
    "2026-07-28 10:43:50,051 1275 INFO odoo16_sandbox odoo.modules.loading: Modules loaded. \n"
    "2026-07-28 10:43:50,369 1275 INFO odoo16_sandbox odoo.tests.result: "
    "0 failed, 0 error(s) of 8 tests when loading database 'odoo16_sandbox' \n"
)

_SOME_FAIL_OUTPUT = (
    "2026-07-28 10:43:50,051 1275 INFO odoo16_sandbox odoo.modules.loading: Modules loaded. \n"
    "2026-07-28 10:43:50,053 1275 ERROR odoo16_sandbox odoo.addons.x.tests.test_x: "
    "FAIL: TestX.test_something\n"
    "Traceback (most recent call last):\n"
    "  File \"/mnt/extra-addons/x/tests/test_x.py\", line 10, in test_something\n"
    "    self.assertEqual(1, 2)\n"
    "AssertionError: 1 != 2\n"
    "2026-07-28 10:43:50,369 1275 ERROR odoo16_sandbox odoo.tests.result: "
    "1 failed, 0 error(s) of 8 tests when loading database 'odoo16_sandbox' \n"
)


_STATE_INSTALLED_OUTPUT = "INSTALL_STATE_CHECK:installed\n"


def test_install_module_reports_all_tests_passed():
    # install_module() (no task_id, not is_fast_path_eligible) always
    # positively re-verifies real DB state via a SECOND _run_in_container
    # call after the install command itself -- side_effect supplies the
    # install output first, then the state-check output the real
    # ir.module.module.state=='installed' read expects.
    with patch(
        "tools_odoo.module_dev.toolchain._run_in_container",
        side_effect=[_fake_proc(_ALL_PASS_OUTPUT), _fake_proc(_STATE_INSTALLED_OUTPUT)],
    ), patch(
        "tools_odoo.module_dev.toolchain._assert_safe_odoo_target",
    ), patch(
        "tools_odoo.module_dev.toolchain._find_missing_manifest_data_files", return_value=None,
    ):
        result = install_module("oma_fake_module", "odoo16_dev", test_enable=True)
    assert result.success is True
    assert result.tests_run == 8
    assert result.tests_failed == 0
    assert result.tests_errored == 0
    assert "8" in result.message
    print(f"PASS: a real 'all tests passed' summary line is correctly parsed and reported -- {result.message!r}")


def test_install_module_fails_when_a_real_test_fails_even_though_install_itself_succeeded():
    """The real regression this phase closes: a genuine test-method
    AssertionError always prints its own 'Traceback (most recent call
    last):' -- must be classified as `tests_failed`, distinct from a
    real install crash (`generic_failure`), never silently treated as
    success just because the module itself loaded.
    """
    with patch(
        "tools_odoo.module_dev.toolchain._run_in_container", return_value=_fake_proc(_SOME_FAIL_OUTPUT),
    ), patch(
        "tools_odoo.module_dev.toolchain._assert_safe_odoo_target",
    ), patch(
        "tools_odoo.module_dev.toolchain._find_missing_manifest_data_files", return_value=None,
    ):
        result = install_module("oma_fake_module", "odoo16_dev", test_enable=True)
    assert result.success is False
    assert result.error_kind == "tests_failed", (
        f"a real test failure must be classified distinctly from a generic install crash, "
        f"got error_kind={result.error_kind!r}"
    )
    assert result.tests_run == 8
    assert result.tests_failed == 1
    assert result.tests_errored == 0
    print(f"PASS: a real test failure is correctly classified as tests_failed, not generic_failure "
          f"or a false success -- {result.message!r}")


def test_install_module_test_enable_false_preserves_prior_behavior():
    """Every existing caller's exact prior behavior (test_enable
    defaults False) must be completely unchanged -- no test-summary
    parsing, no new gating, tests_run/failed/errored all stay None.
    """
    with patch(
        "tools_odoo.module_dev.toolchain._run_in_container",
        side_effect=[_fake_proc("Modules loaded.\n"), _fake_proc(_STATE_INSTALLED_OUTPUT)],
    ), patch(
        "tools_odoo.module_dev.toolchain._assert_safe_odoo_target",
    ), patch(
        "tools_odoo.module_dev.toolchain._find_missing_manifest_data_files", return_value=None,
    ):
        result = install_module("oma_fake_module", "odoo16_dev")
    assert result.success is True
    assert result.tests_run is None
    assert result.tests_failed is None
    assert result.tests_errored is None
    print("PASS: default (test_enable=False) behavior is completely unchanged")


if __name__ == "__main__":
    test_install_module_reports_all_tests_passed()
    test_install_module_fails_when_a_real_test_fails_even_though_install_itself_succeeded()
    test_install_module_test_enable_false_preserves_prior_behavior()
    print("\nALL INSTALL-MODULE TEST-EXECUTION TESTS PASSED")
