"""Real, confirmed bug found live (2026-08-11, task 07141af5, ticket_bulk_close node):
`_run_in_container()` (tools_odoo/module_dev/toolchain.py) is a shared, widely-used utility
(38+ call sites across this codebase) that never caught its own `subprocess.run(...,
timeout=...)` timeout -- a genuinely slow SSH/docker-exec call on a busy/flaky dev host
exceeding its own timeout raised `subprocess.TimeoutExpired` straight past every caller, most
of which never wrap this call in their own try/except. Confirmed live, TWICE in the same
night, crashing the ENTIRE task with an uncaught exception ("Task crashed mid-round") rather
than failing just the one round, for two completely different commands (a coverage/test run,
and a plain `find` listing).

Fixed at the source: `_run_in_container()` now catches `subprocess.TimeoutExpired` and returns
a real `subprocess.CompletedProcess` (the exact type every caller already expects and already
checks `.returncode`/`.stdout`/`.stderr` on) with a sentinel `returncode=-1`, instead of
raising -- every existing caller's own "nonzero returncode means failure" handling now
correctly, gracefully absorbs a timeout the same way it already absorbs any other command
failure.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module


def test_run_in_container_returns_completed_process_on_timeout(monkeypatch):
    monkeypatch.setenv("OMA_ODOO_SSH_HOST", "fake-host")
    monkeypatch.setenv("OMA_ODOO_SSH_USER", "fake-user")
    monkeypatch.setenv("OMA_ODOO_SSH_CONTAINER", "fake-container")

    def _fake_subprocess_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 60))

    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)

    result = toolchain_module._run_in_container("echo hi", timeout=5)

    assert isinstance(result, subprocess.CompletedProcess), (
        f"a timeout must still produce a real CompletedProcess, never raise: {result!r}"
    )
    assert result.returncode != 0, "a timed-out command must report a nonzero (failure) returncode"
    assert "timed out" in result.stderr, f"stderr should explain the timeout: {result.stderr!r}"
    print("PASS: _run_in_container() returns a graceful CompletedProcess on a subprocess "
          "timeout instead of raising and crashing every caller")


def test_run_in_container_normal_case_unaffected(monkeypatch):
    monkeypatch.setenv("OMA_ODOO_SSH_HOST", "fake-host")
    monkeypatch.setenv("OMA_ODOO_SSH_USER", "fake-user")
    monkeypatch.setenv("OMA_ODOO_SSH_CONTAINER", "fake-container")

    def _fake_subprocess_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)

    result = toolchain_module._run_in_container("echo hi", timeout=5)
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    print("PASS: the normal (non-timeout) case is completely unaffected by this fix")


if __name__ == "__main__":
    class _FakeMonkeypatch:
        def __init__(self):
            self._restores = []

        def setenv(self, name, value):
            self._restores.append(("env", name, os.environ.get(name)))
            os.environ[name] = value

        def setattr(self, obj, name, value):
            self._restores.append(("attr", obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for entry in reversed(self._restores):
                if entry[0] == "env":
                    _, name, old = entry
                    if old is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = old
                else:
                    _, obj, name, old = entry
                    setattr(obj, name, old)

    for fn in [test_run_in_container_returns_completed_process_on_timeout, test_run_in_container_normal_case_unaffected]:
        mp = _FakeMonkeypatch()
        try:
            fn(mp)
        finally:
            mp.undo()

    print("\nALL _run_in_container TIMEOUT-HANDLING TESTS PASSED")
