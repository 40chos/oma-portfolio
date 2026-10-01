"""Phase 28C (2026-07-28): unit test for the real, confirmed live
false-negative fix in _install_module_via_warm_worker() (tools_odoo/
module_dev/toolchain.py).

Root cause, confirmed live on the school_student task's own
security_groups round: the warm worker (a long-lived, always-on Odoo
process kept registry-warm for fast installs) reported a genuine-
looking install failure -- "Foutieve modelnaam 'school.student' in
actie definitie" (Odoo's own Dutch-locale "invalid model name in
action definition" ParseError) -- for installing a model+menu+action
together. Empirically proven a FALSE NEGATIVE specific to this one
long-lived process: the EXACT SAME generated files, installed moments
later via a completely fresh `odoo-bin -i` subprocess against the SAME
database, succeeded cleanly (verified directly: ir_model row created,
module state 'installed'). Before this fix, this error text fell
through to a hard InstallResult(success=False, ...), incorrectly
reported as a genuine content bug -- burning an entire round budget on
something the pipeline's own slow, proven fallback path would have
gotten right immediately.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.module_dev.toolchain as toolchain_module
from tools_odoo.module_dev.toolchain import _install_module_via_warm_worker


def test_stale_registry_false_negative_falls_back_instead_of_hard_failing(monkeypatch):
    def fake_get_or_create_shared_key(db, login):
        return 2, "fake-api-key"

    monkeypatch.setattr(
        toolchain_module, "_get_or_create_shared_key", fake_get_or_create_shared_key, raising=False,
    )
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", fake_get_or_create_shared_key)

    error_text = (
        "odoo.tools.convert.ParseError: while parsing "
        "/mnt/extra-addons/oma_x/views/views.xml:3\n"
        "Foutieve modelnaam 'school.student' in actie definitie."
    )

    def fake_run_in_container(bash_command, timeout=180, container=None):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"WARM_INSTALL_ERROR:{error_text}", stderr="",
        )

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = _install_module_via_warm_worker("oma_x", "odoo16_dev")
    assert result is None, (
        "a known stale-registry false-negative signature ('in actie definitie' / invalid model "
        "name) must fall back to the slow, proven install path (None), never be reported as a "
        "hard content failure -- confirmed live that the exact same content installs cleanly via "
        "a fresh process"
    )
    print("PASS: the real, confirmed stale-registry false negative now falls back to the slow "
          "path instead of incorrectly failing the round")


def test_stale_registry_false_negative_broadened_dutch_group_wording_also_falls_back(monkeypatch):
    """Real, confirmed bug found live (2026-07-29, same task,
    security_groups round): the identical false-negative class
    recurred with a DIFFERENT Dutch phrasing -- "Geen overeenkomende
    records gevonden voor Externe id 'oma_x.group_school_admin' in
    veld 'Group'" -- for a security_csv group_id:id reference this
    time, not an action's res_model. Confirmed empirically the same
    way: the exact same on-disk content installed cleanly moments
    later via a fresh odoo-bin process (Registry loaded in 8.377s,
    zero errors). The narrow 'in actie definitie'-only match from the
    first fix didn't catch this second real-world wording.
    """
    import tools_odoo.odoo_schema_client as schema_client_module

    def fake_get_or_create_shared_key(db, login):
        return 2, "fake-api-key"

    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", fake_get_or_create_shared_key)

    error_text = (
        "odoo.tools.convert.ParseError: while parsing "
        "/mnt/extra-addons/oma_x/security/ir.model.access.csv\n"
        "Geen overeenkomende records gevonden voor Externe id "
        "'oma_x.group_school_admin' in veld 'Group'"
    )

    def fake_run_in_container(bash_command, timeout=180, container=None):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"WARM_INSTALL_ERROR:{error_text}", stderr="",
        )

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = _install_module_via_warm_worker("oma_x", "odoo16_dev")
    assert result is None, (
        "the broadened Dutch 'no matching record' wording must also fall back to the slow, "
        "proven install path -- confirmed live that the exact same content installs cleanly"
    )
    print("PASS: the second, differently-worded real stale-registry false negative also falls "
          "back correctly, closing the gap the narrower first fix left open")


def test_stale_registry_false_negative_field_does_not_exist_wording_also_falls_back(monkeypatch):
    """Real, confirmed bug found live (2026-07-29, same task, resumed
    computed_age_field round): a THIRD phrasing of the same class --
    "Veld 'age' bestaat niet in model 'school.student'" (Dutch: "Field
    'age' does not exist in model 'school.student'") -- fired for a
    field that IS genuinely present in the committed models.py for
    that exact commit (confirmed directly by reading the real
    committed content from Gitea for commit 4f131cae97ada4d0cb0f6c8460
    cdf9099c42a854). Same root cause: the warm worker's long-lived
    registry had not yet picked up a field only just added moments
    earlier in the same database by this same round.
    """
    import tools_odoo.odoo_schema_client as schema_client_module

    def fake_get_or_create_shared_key(db, login):
        return 2, "fake-api-key"

    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", fake_get_or_create_shared_key)

    error_text = (
        "odoo.tools.convert.ParseError: while parsing "
        "/mnt/extra-addons/oma_x/views/views.xml:4\n"
        "Veld \"age\" bestaat niet in model \"school.student\""
    )

    def fake_run_in_container(bash_command, timeout=180, container=None):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"WARM_INSTALL_ERROR:{error_text}", stderr="",
        )

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = _install_module_via_warm_worker("oma_x", "odoo16_dev")
    assert result is None, (
        "the third, differently-worded real stale-registry false negative ('field does not exist "
        "in model') must also fall back to the slow, proven install path -- confirmed live via "
        "Gitea that the field genuinely was present in the committed content"
    )
    print("PASS: the third, differently-worded real stale-registry false negative also falls "
          "back correctly, closing the gap the previous two signatures left open")


def test_a_genuinely_different_failure_still_hard_fails(monkeypatch):
    def fake_get_or_create_shared_key(db, login):
        return 2, "fake-api-key"

    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", fake_get_or_create_shared_key)

    error_text = "psycopg2.errors.InsufficientPrivilege: must be owner of table ir_model_data"

    def fake_run_in_container(bash_command, timeout=180, container=None):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"WARM_INSTALL_ERROR:{error_text}", stderr="",
        )

    monkeypatch.setattr(toolchain_module, "_run_in_container", fake_run_in_container)

    result = _install_module_via_warm_worker("oma_x", "odoo16_dev")
    assert result is not None
    assert result.success is False
    assert result.error_kind == "postgres_ownership_blocked"
    print("PASS: a genuinely different, real failure (postgres ownership) still hard-fails as "
          "before -- this fix is narrowly scoped to the one confirmed false-negative signature")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
