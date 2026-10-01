"""Real, confirmed systemic bug found live (2026-08-10, task 07141af5's flagship run,
project_ticket_counts node, resume r185): run_coverage_and_diff()'s own `-i {module_name}`
install command never included `--test-enable`, so Odoo never imported/executed a single
generated `tests/*.py` file, on ANY task, ever -- every test file always showed 0% coverage
regardless of whether the test logic was actually correct and complete. See
tools_odoo/spot_check.py's own updated docstring on run_coverage_and_diff for the full
incident. This test proves the fix: the real shell command built and sent to the container
now includes --test-enable and --without-demo=False, matching the already-proven pattern in
tools_odoo/module_dev/toolchain.py's install_module(test_enable=True) path.
"""
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.spot_check as spot_check_module
from tools_odoo.spot_check import run_coverage_and_diff


@dataclass
class _FakeProc:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def test_coverage_install_command_enables_tests_and_demo_data(monkeypatch):
    captured = {}

    def _fake_run_in_container(cmd, timeout=180, container=None):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        fake_report = 'Name    Stmts   Miss  Cover\n-----------------------------\n'
        fake_json = '{"meta": {}, "files": {}}'
        return _FakeProc(stdout=fake_report + fake_json)

    monkeypatch.setattr(spot_check_module, "_run_in_container", _fake_run_in_container)

    run_coverage_and_diff("some_module", "some_db")

    assert "--test-enable" in captured["cmd"], (
        "run_coverage_and_diff's install command must pass --test-enable -- otherwise Odoo "
        "never imports tests/ at all and every test file always shows 0% coverage, regardless "
        "of whether the test logic is correct"
    )
    assert "--without-demo=False" in captured["cmd"], (
        "test-enabled installs must also disable demo-data suppression, matching "
        "toolchain.py's own proven install_module(test_enable=True) pattern -- some real "
        "generated tests assert against demo data"
    )
    assert captured["timeout"] >= 480, (
        "a test-enabled install runs substantially more code than a plain install and needs "
        "the same generous timeout toolchain.py already uses for test_enable=True"
    )
    print("PASS: coverage install command now actually enables and runs the module's own tests")


def test_coverage_run_returns_graceful_result_on_subprocess_timeout(monkeypatch):
    """Real, confirmed bug found live (2026-08-11, task 07141af5, ticket_bulk_close node):
    `_run_in_container()` calls `subprocess.run(..., timeout=...)` with no try/except of its
    own -- a genuinely slow install+test run on a busy/flaky dev host exceeding the 480s budget
    raised `subprocess.TimeoutExpired` straight past `run_coverage_and_diff()`'s own body, an
    UNCAUGHT exception that crashed the ENTIRE task ("Task crashed mid-round"), not merely this
    one round. This function's own established contract is to never raise, always return a
    real, honest `CoverageResult` even for other real failure modes (empty output, unparseable
    JSON) -- a subprocess timeout must get the identical treatment, not an unhandled crash.
    """
    import subprocess

    def _fake_run_in_container_that_times_out(cmd, timeout=180, container=None):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(spot_check_module, "_run_in_container", _fake_run_in_container_that_times_out)

    result = run_coverage_and_diff("some_module", "some_db")
    assert result.ran_ok is False, f"a subprocess timeout must produce ran_ok=False, not raise: {result!r}"
    assert "timed out" in (result.raw_error or ""), f"raw_error should explain the timeout: {result!r}"
    print("PASS: a subprocess timeout during coverage run returns a graceful CoverageResult "
          "instead of crashing the caller")


if __name__ == "__main__":
    import types

    class _Ctx:
        pass

    # Minimal manual run without pytest's monkeypatch fixture.
    captured = {}

    def _fake_run_in_container(cmd, timeout=180, container=None):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return _FakeProc(stdout='Name Stmts Miss Cover\n{"meta": {}, "files": {}}')

    spot_check_module._run_in_container = _fake_run_in_container
    run_coverage_and_diff("some_module", "some_db")
    assert "--test-enable" in captured["cmd"]
    assert "--without-demo=False" in captured["cmd"]
    assert captured["timeout"] >= 480
    print("PASS: coverage install command now actually enables and runs the module's own tests")
