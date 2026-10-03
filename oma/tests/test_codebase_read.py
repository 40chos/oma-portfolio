"""Tests for tools_odoo.codebase_read -- real SSH/docker-exec reads
against the actual dev container, no LLM calls, so these run fast.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.codebase_read import (
    _CUSTOM_CODEBASE_ROOT,
    _MODULE_DEV_ADDONS_DIR,
    find_module_defining_model,
    read_module_files_relevant_to_goal,
)


def test_relevant_files_avoids_dumping_a_whole_large_module():
    """Real, confirmed bug found live (2026-07-24, 50-task sequential
    re-run, task 001): `read_module_files('mis_base_extend')` (used as
    `depends_on_module` extension context) pulled in ALL 135 files /
    27,031 lines of that real, large SITE module, and the very first
    generation call for a task that only needed to touch `crm.lead`
    was rejected outright by the real backend: "This model's maximum
    context length is 98304 tokens... your prompt contains at least
    98305 input tokens." A large real module dumped in full is
    fundamentally unscalable as extension context.

    This test confirms the fix picks out ONLY the file(s) actually
    relevant to the task's own goal (here: `models/crm.py`, the real
    file defining `crm.lead` extensions) instead of the whole module,
    and stays well under any realistic context budget.
    """
    goal = (
        "I want to see a text field on the lead form called 'Special "
        "instructions'.\n\nModule: mis_base_extend\nModel: crm.lead\n"
        "Field: special_instructions (Text)\n"
    )
    files = read_module_files_relevant_to_goal("mis_base_extend", goal)
    assert files, "expected at least one relevant file to be found"
    total_bytes = sum(len(c) for c in files.values())
    assert total_bytes < 60_000, (
        f"expected the relevant-files read to stay well under the real incident's "
        f"~400KB (98K+ tokens) -- got {total_bytes} bytes across {list(files.keys())}"
    )
    assert any(path.endswith("models/crm.py") for path in files), (
        f"expected the real file defining crm.lead extensions to be picked out -- "
        f"got {list(files.keys())}"
    )
    print(f"PASS: relevant-files read for a crm.lead task picked out {list(files.keys())} "
          f"({total_bytes} bytes total), not the whole 27,031-line module")


def test_find_module_defining_model_prefers_the_real_customer_codebase():
    """Real, confirmed bug found live (2026-07-26, Phase 25D, task 005's
    own resubmission): this function used to search ONLY /mnt/extra-
    addons (this pipeline's own transient scaffolded modules) -- for a
    real, permanent customer model (project.fieldjob, defined by the
    real project_fieldjob module under /opt/site/site16), that search
    could only ever find a WRONG answer: some unrelated earlier task's
    own scaffold that happened to also (incorrectly) redefine the same
    model name. Confirmed live: 'project.fieldjob' resolved to
    'oma_create_an_import_wizard_ede68004' -- a completely unrelated
    module. The real customer codebase must be checked FIRST.
    """
    def fake_docker_exec(bash_command, timeout=60):
        from subprocess import CompletedProcess
        if bash_command.startswith(f"grep") and _CUSTOM_CODEBASE_ROOT in bash_command:
            return CompletedProcess(args=[], returncode=0, stdout=f"{_CUSTOM_CODEBASE_ROOT}/project_fieldjob/models/models.py\n", stderr="")
        if bash_command.startswith("grep") and _MODULE_DEV_ADDONS_DIR in bash_command:
            return CompletedProcess(args=[], returncode=0, stdout=f"{_MODULE_DEV_ADDONS_DIR}/oma_create_an_import_wizard_ede68004/models/models.py\n", stderr="")
        return CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    with patch("tools_odoo.codebase_read._docker_exec_readonly", side_effect=fake_docker_exec):
        result = find_module_defining_model("project.fieldjob")
    assert result == "project_fieldjob", (
        f"expected the real customer module to be preferred over an unrelated scaffold, got: {result!r}"
    )
    print("PASS: find_module_defining_model() prefers the real customer codebase over this "
          "pipeline's own transient scaffolded modules")


def test_find_module_defining_model_falls_back_to_scaffolded_modules():
    """When the model genuinely isn't a real customer model (i.e. it's
    one THIS pipeline itself created in an earlier task), the transient
    scaffold search must still work as the fallback.
    """
    def fake_docker_exec(bash_command, timeout=60):
        from subprocess import CompletedProcess
        if bash_command.startswith("grep") and _CUSTOM_CODEBASE_ROOT in bash_command:
            return CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        if bash_command.startswith("grep") and _MODULE_DEV_ADDONS_DIR in bash_command:
            return CompletedProcess(args=[], returncode=0, stdout=f"{_MODULE_DEV_ADDONS_DIR}/oma_build_a_service_record_56298314/models/models.py\n", stderr="")
        return CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    with patch("tools_odoo.codebase_read._docker_exec_readonly", side_effect=fake_docker_exec):
        result = find_module_defining_model("oma.service.record")
    assert result == "oma_build_a_service_record_56298314", (
        f"expected the scaffold-search fallback to still work, got: {result!r}"
    )
    print("PASS: falls back to the transient scaffold search when the model isn't a real "
          "customer model")


if __name__ == "__main__":
    test_relevant_files_avoids_dumping_a_whole_large_module()
    test_find_module_defining_model_prefers_the_real_customer_codebase()
    test_find_module_defining_model_falls_back_to_scaffolded_modules()
    print("\nALL CODEBASE_READ TESTS PASSED")
