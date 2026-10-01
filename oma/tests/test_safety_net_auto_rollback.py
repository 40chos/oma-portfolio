"""Phase 35 §9.2 auto-rollback (2026-08-12): run_post_install_smoke_check() can auto-uninstall a
just-installed module on a smoke-check failure -- but only when OMA_SAFETY_NET_AUTO_ROLLBACK=1 is
explicitly set (off by default, matching the retrieval fast-path's own off-by-default posture in
§3.4, since as of this revision the smoke check has only run against two real production installs
and doesn't yet have a known false-positive rate to trust for an automatic, destructive action).
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.module_dev.safety_net import run_post_install_smoke_check
from tools_odoo.smoke_suite import SmokeCheckResult


def _fake_failing_results():
    return [
        SmokeCheckResult("create_partner", True, "ok"),
        SmokeCheckResult("create_sale_order", False, "broken"),
    ]


def _fake_passing_results():
    return [SmokeCheckResult("create_partner", True, "ok")]


def test_flag_off_by_default_never_rolls_back_even_on_failure():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OMA_SAFETY_NET_AUTO_ROLLBACK", None)
        with patch("tools_odoo.smoke_suite.run_all", return_value=_fake_failing_results()), \
             patch("tools_odoo.module_dev.toolchain.uninstall_module") as mock_uninstall:
            summary = run_post_install_smoke_check("task-1", "some_module", "odoo16_dev")
    assert summary["failed"] == 1
    assert summary["auto_rollback_attempted"] is False
    mock_uninstall.assert_not_called()
    print("PASS: rollback never attempted with the flag unset, even though the check failed")


def test_flag_on_with_failure_calls_uninstall():
    fake_result = MagicMock(success=True, message="uninstalled")
    with patch.dict(os.environ, {"OMA_SAFETY_NET_AUTO_ROLLBACK": "1"}):
        with patch("tools_odoo.smoke_suite.run_all", return_value=_fake_failing_results()), \
             patch("tools_odoo.module_dev.toolchain.uninstall_module", return_value=fake_result) as mock_uninstall:
            summary = run_post_install_smoke_check("task-2", "some_module", "odoo16_dev")
    mock_uninstall.assert_called_once_with("some_module", "odoo16_dev", task_id="task-2")
    assert summary["auto_rollback_attempted"] is True
    assert summary["auto_rollback_succeeded"] is True
    print("PASS: flag=1 plus a real failure calls uninstall_module on the exact just-installed module")


def test_flag_on_but_no_failure_never_calls_uninstall():
    with patch.dict(os.environ, {"OMA_SAFETY_NET_AUTO_ROLLBACK": "1"}):
        with patch("tools_odoo.smoke_suite.run_all", return_value=_fake_passing_results()), \
             patch("tools_odoo.module_dev.toolchain.uninstall_module") as mock_uninstall:
            summary = run_post_install_smoke_check("task-3", "some_module", "odoo16_dev")
    assert summary["failed"] == 0
    mock_uninstall.assert_not_called()
    print("PASS: no failure means no rollback attempt, regardless of the flag")


def test_flag_on_but_missing_module_name_never_calls_uninstall():
    with patch.dict(os.environ, {"OMA_SAFETY_NET_AUTO_ROLLBACK": "1"}):
        with patch("tools_odoo.smoke_suite.run_all", return_value=_fake_failing_results()), \
             patch("tools_odoo.module_dev.toolchain.uninstall_module") as mock_uninstall:
            summary = run_post_install_smoke_check("task-4", module_name=None, db=None)
    assert summary["auto_rollback_attempted"] is False
    mock_uninstall.assert_not_called()
    print("PASS: rollback needs a real module_name/db, never guesses or skips this check")


def test_rollback_failure_is_swallowed_not_raised():
    with patch.dict(os.environ, {"OMA_SAFETY_NET_AUTO_ROLLBACK": "1"}):
        with patch("tools_odoo.smoke_suite.run_all", return_value=_fake_failing_results()), \
             patch("tools_odoo.module_dev.toolchain.uninstall_module", side_effect=RuntimeError("ssh died")):
            summary = run_post_install_smoke_check("task-5", "some_module", "odoo16_dev")
    assert summary["auto_rollback_attempted"] is True
    assert summary["auto_rollback_succeeded"] is False
    print("PASS: a rollback attempt that itself raises is caught and reported, never crashes the task")


if __name__ == "__main__":
    test_flag_off_by_default_never_rolls_back_even_on_failure()
    test_flag_on_with_failure_calls_uninstall()
    test_flag_on_but_no_failure_never_calls_uninstall()
    test_flag_on_but_missing_module_name_never_calls_uninstall()
    test_rollback_failure_is_swallowed_not_raised()
    print("\nALL TESTS PASSED")
