"""Phase 29 (2026-07-29): unit test for a real, confirmed live bug --
the school_student task's automated_tests round genuinely, fully
passed ("0 failed, 0 error(s) of 15 tests" in Odoo's own real log)
after the demo-data manifest-reference fix landed, yet install_module()
still reported success=False, error_kind="generic_failure": Odoo's own
log for test_student_unique_constraint (which deliberately inserts a
duplicate student_id to confirm the DB rejects it, correctly caught via
assertRaises) prints "Traceback (most recent call last):" for the
underlying, expected IntegrityError, and the generic CRITICAL/Traceback
check ran regardless of the definitive "0 failed, 0 error(s)" summary
already parsed above it, misclassifying a fully green run as a crash.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module
from tools_odoo.module_dev.toolchain import install_module

# Real, confirmed shape captured live (2026-07-29, round ending
# 22:03:53): a genuinely passing run whose own log still contains a
# "Traceback (most recent call last):" line from the intentionally-
# triggered-and-caught unique-constraint IntegrityError.
_REAL_PASSING_LOG_WITH_INCIDENTAL_TRACEBACK = (
    "2026-07-29 22:03:48,571 9793 ERROR odoo16_sandbox_20260729_220300 odoo.sql_db: "
    "Traceback (most recent call last):\n"
    "  File \"psycopg2 stuff\", line 1, in <module>\n"
    "psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "
    "\"school_student_student_id_unique\"\n"
    "DETAIL:  Key (student_id)=(UNIQUE001) already exists.\n"
    "2026-07-29 22:03:49,139 9793 INFO odoo16_sandbox_20260729_220300 odoo.modules.loading: "
    "Module oma_x loaded in 1.82s (incl. 0.50s test), 178 queries\n"
    "2026-07-29 22:03:53,252 9793 INFO odoo16_sandbox_20260729_220300 odoo.tests.result: "
    "0 failed, 0 error(s) of 15 tests when loading database 'odoo16_sandbox_20260729_220300'\n"
)


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
        # The state-check script's own "INSTALL_STATE_CHECK:" marker is
        # base64-encoded inside bash_command, not a literal substring --
        # order (install call first, state-check call second) is what
        # distinguishes them here instead.
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=combined_stdout, stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"INSTALL_STATE_CHECK:{state}\n", stderr="")

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)


def test_a_fully_passing_test_run_is_not_misclassified_by_an_incidental_traceback(monkeypatch):
    _setup(monkeypatch, _REAL_PASSING_LOG_WITH_INCIDENTAL_TRACEBACK)
    result = install_module("oma_x", "odoo16_sandbox_20260729_220300", remote_port=8072, sandbox=True, test_enable=True)
    assert result.success is True, (
        f"a genuinely fully-passing test run (0 failed, 0 errored) must not be misclassified as a "
        f"generic failure just because the log ALSO contains an incidental Traceback from a "
        f"deliberately-triggered-and-caught constraint violation -- got error_kind={result.error_kind!r}: "
        f"{result.message}"
    )
    assert result.tests_run == 15 and result.tests_failed == 0 and result.tests_errored == 0
    print("PASS: a fully-passing test run wins over an incidental Traceback/CRITICAL string")


def test_a_genuine_test_failure_is_still_reported_as_failure(monkeypatch):
    log = _REAL_PASSING_LOG_WITH_INCIDENTAL_TRACEBACK.replace(
        "0 failed, 0 error(s) of 15 tests", "1 failed, 0 error(s) of 15 tests",
    )
    _setup(monkeypatch, log)
    result = install_module("oma_x", "odoo16_sandbox_20260729_220300", remote_port=8072, sandbox=True, test_enable=True)
    assert result.success is False
    assert result.error_kind == "tests_failed"
    print("PASS: a genuine test failure is still correctly reported, unaffected by this fix")


def test_a_real_crash_with_no_test_summary_at_all_is_still_a_generic_failure(monkeypatch):
    log = "CRITICAL some_module: something exploded before tests could even run\n"
    _setup(monkeypatch, log)
    result = install_module("oma_x", "odoo16_sandbox_20260729_220300", remote_port=8072, sandbox=True, test_enable=True)
    assert result.success is False
    assert result.error_kind == "generic_failure"
    print("PASS: a real crash with no test summary line at all is still caught as a generic failure")


def test_state_verification_still_runs_and_can_still_fail_a_definitive_test_pass(monkeypatch):
    _setup(monkeypatch, _REAL_PASSING_LOG_WITH_INCIDENTAL_TRACEBACK, state="uninstalled")
    result = install_module("oma_x", "odoo16_sandbox_20260729_220300", remote_port=8072, sandbox=True, test_enable=True)
    assert result.success is False
    assert result.error_kind == "state_verification_failed", (
        "a definitive test-pass summary must still fall through to the real, independent "
        "ir.module.module.state check -- never trust a clean-looking test summary alone either"
    )
    print("PASS: the real post-install state check still runs and still wins even on a definitive test pass")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
