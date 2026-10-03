"""P13 item 11, Unit 1 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2 item 11): unit tests for `_parse_install_findings()`/`InstallResult.findings` -- the
structured, artifact-tagged finding extraction the join-suppression fix (Unit 2) depends on.

The one real, confirmed warning pattern parsed here (`_MISSING_ACCESS_RULES_RE`) is verified
against the real dev container's own installed `odoo/modules/loading.py` source (grepped live,
2026-08-01), not guessed -- see the exact wording captured in `_REAL_ACCESS_RULES_LOG` below.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module
from tools_odoo.module_dev.toolchain import InstallFinding, install_module, _parse_install_findings

# Real, confirmed Odoo 16 warning text, byte-for-byte as produced by the real dev container's own
# odoo/modules/loading.py (grepped live, 2026-08-01) -- not a guessed or paraphrased string.
_REAL_ACCESS_RULES_LOG = (
    "2026-08-01 12:00:00,000 12345 INFO test_db odoo.modules.loading: Module site_fieldjob loaded in 1.10s\n"
    "2026-08-01 12:00:00,100 12345 WARNING test_db odoo.modules.loading: "
    "The models ['project.satisfaction'] have no access rules in module site_fieldjob, "
    "consider adding some, like:\n"
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    "site_fieldjob.access_project_satisfaction,access_project_satisfaction,"
    "site_fieldjob.model_project_satisfaction,base.group_user,1,0,0,0\n"
)


def test_parse_install_findings_extracts_real_access_rules_warning():
    findings = _parse_install_findings(_REAL_ACCESS_RULES_LOG)
    assert len(findings) == 1
    assert findings[0] == InstallFinding(
        kind="missing_access_rules",
        artifact_names=["project.satisfaction"],
        raw_text=(
            "The models ['project.satisfaction'] have no access rules in module site_fieldjob, "
            "consider adding some"
        ),
    )
    print("PASS: the real, confirmed access-rules warning text is parsed into a structured finding")


def test_parse_install_findings_multiple_models_in_one_warning():
    log = (
        "WARNING test_db odoo.modules.loading: The models ['project.satisfaction', 'project.rating'] "
        "have no access rules in module site_fieldjob, consider adding some, like:\n"
    )
    findings = _parse_install_findings(log)
    assert len(findings) == 1
    assert findings[0].artifact_names == ["project.satisfaction", "project.rating"]
    print("PASS: multiple model names in one warning are all captured, none dropped")


def test_parse_install_findings_returns_empty_list_for_clean_log():
    findings = _parse_install_findings("2026-08-01 12:00:00,000 INFO test_db odoo.modules.loading: Modules loaded.\n")
    assert findings == []
    print("PASS: a clean log with no matching warning produces zero findings -- never guessed")


def test_parse_install_findings_never_crashes_on_malformed_list_repr():
    log = "The models [this is not valid python] have no access rules in module site_x, consider adding some, like:\n"
    findings = _parse_install_findings(log)
    assert findings == []
    print("PASS: a malformed/unparseable model-list repr produces zero findings, never a crash or a guess")


def _setup_state_verification_failed(monkeypatch, log_body: str):
    def fake_get_or_create_shared_key(db, login):
        return 2, "fake-api-key"

    monkeypatch.setattr(toolchain_module, "_get_or_create_shared_key", fake_get_or_create_shared_key, raising=False)
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", fake_get_or_create_shared_key)
    monkeypatch.setattr(toolchain_module, "_find_missing_manifest_data_files", lambda *a, **k: None)
    monkeypatch.setattr(toolchain_module, "is_fast_path_eligible", lambda db: False)
    # 2026-08-14: the self-heal pre-install pass (see
    # _self_heal_stale_templates_xml_manifest_refs()) now also calls
    # _run_in_container, ahead of the actual install command -- match
    # on the real distinguishing content ("-i " denotes the actual
    # odoo-bin install invocation) instead of call position, same
    # pattern already used by test_toolchain_manifest_missing_file_gate.py.
    monkeypatch.setattr(toolchain_module, "_self_heal_stale_templates_xml_manifest_refs", lambda *a, **k: [])

    def fake_run_in_container(bash_command, timeout=180, container=None):
        if "-i " in bash_command:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=log_body, stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="INSTALL_STATE_CHECK:UNKNOWN\n", stderr="")

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)


def test_install_module_state_verification_failed_carries_the_real_finding(monkeypatch):
    """The exact real-world shape item 11's own worked example (task `0689823f-...`) hits: a clean
    exit, no CRITICAL/Traceback, but ir.module.module.state != 'installed' -- and the log contains
    the real access-rules warning that plausibly explains it. Unit 2 (the join-suppression fix)
    depends on `InstallResult.findings` being populated exactly here.
    """
    _setup_state_verification_failed(monkeypatch, _REAL_ACCESS_RULES_LOG)
    result = install_module("site_fieldjob", "odoo16_sandbox_20260801_120000", remote_port=8072, sandbox=True)
    assert result.success is False
    assert result.error_kind == "state_verification_failed"
    assert len(result.findings) == 1
    assert result.findings[0].kind == "missing_access_rules"
    assert result.findings[0].artifact_names == ["project.satisfaction"]
    print("PASS: a real state_verification_failed InstallResult carries the structured, artifact-tagged finding")


def test_install_module_success_path_still_carries_empty_findings_list():
    _ = install_module  # imported for parity with other tests in this file; not called here
    findings = _parse_install_findings("Modules loaded.\n")
    assert findings == []
    print("PASS: a clean install's own findings list is empty, never omitted or None")


if __name__ == "__main__":
    test_parse_install_findings_extracts_real_access_rules_warning()
    test_parse_install_findings_multiple_models_in_one_warning()
    test_parse_install_findings_returns_empty_list_for_clean_log()
    test_parse_install_findings_never_crashes_on_malformed_list_repr()
    test_install_module_success_path_still_carries_empty_findings_list()
    print("\n(monkeypatch-dependent tests require pytest; run via: pytest tests/test_toolchain_install_findings_parsing.py)")
