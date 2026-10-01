"""Phase 35 §17.6.1.1 / §18.4: tests for manager/rollback_dry_run_probe.py -- the executed
sandbox install-then-uninstall-then-diff reversibility probe. Mocks install_module()/
uninstall_module()/the psql residue query directly (real end-to-end sandbox execution is covered
separately by this session's live integration testing, not repeated here as a unit test).
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.module_dev.toolchain import InstallResult
from manager.rollback_dry_run_probe import run_rollback_dry_run_probe


def _install_result(success=True, message=""):
    return InstallResult(module_name="oma_test", db="sandbox_db", success=success, message=message)


def test_clean_rollback_reports_clean_true():
    with patch("tools_odoo.module_dev.toolchain.install_module", return_value=_install_result(True)), \
         patch("tools_odoo.module_dev.toolchain.uninstall_module_sandbox", return_value=_install_result(True)), \
         patch("manager.rollback_dry_run_probe._count_ir_model_data_rows", side_effect=[5, 0]):
        result = run_rollback_dry_run_probe("oma_test", "sandbox_db", "container_x")
    assert result.clean is True
    assert result.residue_row_count == 0
    assert result.install_succeeded is True
    assert result.uninstall_succeeded is True


def test_real_residue_reports_clean_false_with_the_real_count():
    with patch("tools_odoo.module_dev.toolchain.install_module", return_value=_install_result(True)), \
         patch("tools_odoo.module_dev.toolchain.uninstall_module_sandbox", return_value=_install_result(True)), \
         patch("manager.rollback_dry_run_probe._count_ir_model_data_rows", side_effect=[5, 3]):
        result = run_rollback_dry_run_probe("oma_test", "sandbox_db", "container_x")
    assert result.clean is False
    assert result.residue_row_count == 3
    assert "3" in result.residue_detail


def test_install_failure_short_circuits_before_uninstall_is_attempted():
    with patch("tools_odoo.module_dev.toolchain.install_module", return_value=_install_result(False, "install broke")) as mock_install, \
         patch("tools_odoo.module_dev.toolchain.uninstall_module_sandbox") as mock_uninstall:
        result = run_rollback_dry_run_probe("oma_test", "sandbox_db", "container_x")
    assert result.install_succeeded is False
    assert result.clean is False
    mock_uninstall.assert_not_called()


def test_uninstall_failure_is_reported_as_not_clean():
    with patch("tools_odoo.module_dev.toolchain.install_module", return_value=_install_result(True)), \
         patch("tools_odoo.module_dev.toolchain.uninstall_module_sandbox", return_value=_install_result(False, "uninstall crashed")), \
         patch("manager.rollback_dry_run_probe._count_ir_model_data_rows", return_value=5):
        result = run_rollback_dry_run_probe("oma_test", "sandbox_db", "container_x")
    assert result.uninstall_succeeded is False
    assert result.clean is False


def test_query_failure_is_reported_as_uncertain_not_a_false_pass():
    with patch("tools_odoo.module_dev.toolchain.install_module", return_value=_install_result(True)), \
         patch("tools_odoo.module_dev.toolchain.uninstall_module_sandbox", return_value=_install_result(True)), \
         patch("manager.rollback_dry_run_probe._count_ir_model_data_rows", side_effect=[5, -1]):
        result = run_rollback_dry_run_probe("oma_test", "sandbox_db", "container_x")
    # A failed query must NEVER be silently treated as "0 residue = clean".
    assert result.clean is False
    assert "UNCERTAIN" in result.residue_detail
