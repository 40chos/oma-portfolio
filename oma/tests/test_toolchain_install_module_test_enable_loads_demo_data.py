"""Phase 29 (2026-07-29): unit test for a real, confirmed live bug --
the school_student task's own automated_tests round had a real,
correct demo_data.xml (5 real student records, correctly referenced in
the manifest), but a real generated test asserting those records exist
post-install failed with `0 not greater than or equal to 5`: demo data
never loaded during the test-enable sandbox install. Fixed by making
`--without-demo=False` explicit for test-enabled installs, rather than
relying on an implicit default this session couldn't fully verify
against the real deployment's own odoo.conf.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module
from tools_odoo.module_dev.toolchain import install_module


def _capture_command(monkeypatch) -> list[str]:
    captured: list[str] = []

    def fake_get_or_create_shared_key(db, login):
        return 2, "fake-api-key"

    monkeypatch.setattr(toolchain_module, "_get_or_create_shared_key", fake_get_or_create_shared_key, raising=False)
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", fake_get_or_create_shared_key)

    def fake_run_in_container(bash_command, timeout=180, container=None):
        captured.append(bash_command)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="Modules loaded.\n", stderr="")

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)
    return captured


def test_test_enabled_install_explicitly_requests_demo_data(monkeypatch):
    captured = _capture_command(monkeypatch)
    install_module("oma_x", "odoo16_sandbox_20260729_120000", remote_port=8072, sandbox=True, test_enable=True)
    real_install_cmd = next(c for c in captured if "-i oma_x" in c)
    assert "--without-demo=False" in real_install_cmd, (
        "a test-enabled install must explicitly request demo data -- the real, confirmed live bug "
        "was demo records never loading, silently failing any test that depends on them"
    )
    assert "--test-enable" in real_install_cmd
    print("PASS: a test-enabled install explicitly requests demo data via --without-demo=False")


def test_plain_install_does_not_pay_the_demo_loading_cost(monkeypatch):
    captured = _capture_command(monkeypatch)
    install_module("oma_x", "odoo16_sandbox_20260729_120000", remote_port=8072, sandbox=True, test_enable=False)
    real_install_cmd = next(c for c in captured if "-i oma_x" in c)
    assert "--without-demo" not in real_install_cmd
    assert "--test-enable" not in real_install_cmd
    print("PASS: a plain (non-test) install is unaffected -- no unnecessary demo-loading cost")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
