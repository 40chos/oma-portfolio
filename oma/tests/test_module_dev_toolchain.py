"""Phase 8: real tests for the scaffold/lint/install toolchain, run
against the actual odoo16-dev container over SSH -- no mocks.

Update (2026-07-10): the Postgres-ownership blocker this file's install
tests originally exercised (installing a module that adds a column to
an existing model failing with InsufficientPrivilege on this
deployment's duplicate databases) is confirmed resolved -- re-verified
directly against the same real module/db pair. The install test below
is repurposed as a genuine positive-path check rather than deleted, and
a new test covers the real, separate gap found live this same day:
install_module() silently reporting success for a module that never
actually reached 'installed' state, closed with a real post-install
state check (§ install_module()'s own docstring).
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.odoo_settings import ProductionOdooGuardError
from tools_odoo.module_dev.toolchain import (
    ModuleDevToolchain,
    ScaffoldError,
    _MODULE_DEV_ADDONS_DIR,
    _run_in_container,
    install_module,
    lint_module,
    scaffold_module,
    strip_scaffold_boilerplate,
)
from tools_odoo.spot_check import check_field_exists_on_model

EXISTING_DUPLICATE_DB = "odoo16_dev_dup_20260707"
KNOWN_THROWAWAY_MODULE = "oma_throwaway_test"  # already scaffolded + hand-edited this phase


def _cleanup_module(module_name: str) -> None:
    _run_in_container(f"rm -rf {_MODULE_DEV_ADDONS_DIR}/{module_name}")


def test_scaffold_produces_expected_skeleton():
    module_name = f"oma_test_scaffold_{uuid.uuid4().hex[:8]}"
    try:
        result = scaffold_module(module_name)
        assert result.module_name == module_name
        assert any(f.endswith("__manifest__.py") for f in result.files)
        assert any(f.endswith("models/__init__.py") for f in result.files)
        print(f"PASS: odoo-bin scaffold produced {len(result.files)} real files for {module_name!r}")
    finally:
        _cleanup_module(module_name)


def test_scaffold_refuses_to_overwrite_existing_module():
    module_name = f"oma_test_noscaffold_{uuid.uuid4().hex[:8]}"
    scaffold_module(module_name)
    try:
        raised = False
        try:
            scaffold_module(module_name)
        except ScaffoldError:
            raised = True
        assert raised, "scaffolding over an existing module directory must be refused"
        print("PASS: scaffold_module() refuses to overwrite an existing module directory")
    finally:
        _cleanup_module(module_name)


def test_strip_scaffold_boilerplate_removes_unclaimable_files():
    """Root-caused live (2026-07-12): odoo-bin scaffold always creates
    controllers/, demo/, and views/templates.xml, none of which
    GeneratedModuleFiles (specialists.build.specialist) has a field for
    -- Code-Review was correctly rejecting these as out-of-scope every
    round of a task that then never passed 48 rounds straight. This
    covers the deterministic fix: after stripping, only the files an
    actual module-dev round can legitimately author remain.
    """
    module_name = f"oma_test_strip_{uuid.uuid4().hex[:8]}"
    scaffold_module(module_name)
    try:
        find_proc = _run_in_container(f"find {_MODULE_DEV_ADDONS_DIR}/{module_name} -type f")
        before = {line for line in find_proc.stdout.splitlines() if line.strip()}
        assert any("controllers" in p for p in before), "fixture assumption: scaffold creates controllers/"
        assert any("demo" in p for p in before), "fixture assumption: scaffold creates demo/"

        removed = strip_scaffold_boilerplate(module_name)
        assert removed, "expected at least controllers/ and demo/ to be stripped"
        assert any("controllers" in p for p in removed)
        assert any("demo" in p for p in removed)

        find_proc = _run_in_container(f"find {_MODULE_DEV_ADDONS_DIR}/{module_name} -type f")
        after = {line for line in find_proc.stdout.splitlines() if line.strip()}
        assert not any("controllers" in p for p in after), "controllers/ must be fully gone"
        assert not any("demo" in p for p in after), "demo/ must be fully gone"
        assert not any("templates.xml" in p for p in after), "views/templates.xml must be gone"
        assert any(p.endswith("__manifest__.py") for p in after), "__manifest__.py must survive"
        assert any(p.endswith("models/models.py") for p in after), "models/models.py must survive"
        assert any(p.endswith("security/ir.model.access.csv") for p in after), "security csv must survive"

        # Real bug found live (2026-07-12), on this fix's own first real
        # verification run: the top-level __init__.py is kept as-is (it's
        # not in the strip list -- every module needs one), but scaffold's
        # stock content is `from . import controllers\nfrom . import
        # models\n` -- an ImportError once controllers/ is gone, unless
        # this function also rewrites it.
        init_proc = _run_in_container(f"cat {_MODULE_DEV_ADDONS_DIR}/{module_name}/__init__.py")
        assert "controllers" not in init_proc.stdout, (
            "top-level __init__.py must not still import the now-deleted controllers package"
        )
        assert "models" in init_proc.stdout, "top-level __init__.py must still import models"

        # Real, general bug found live (2026-07-21, live during a Operator
        # demo, task 'warranty.claim'): views/views.xml is deliberately
        # KEPT (Build may write real content into it later), but
        # odoo-bin's own stock content for it is a large block of fully
        # commented-out placeholder XML -- if a round never needs any
        # views, nothing ever overwrites it, and Code-Review correctly,
        # repeatedly rejects the leftover scaffold noise every round.
        views_proc = _run_in_container(f"cat {_MODULE_DEV_ADDONS_DIR}/{module_name}/views/views.xml")
        assert "<!--" not in views_proc.stdout, (
            f"views/views.xml must be a genuinely empty skeleton after stripping, not odoo-bin's "
            f"own commented-out placeholder content, got: {views_proc.stdout!r}"
        )
        assert "<odoo>" in views_proc.stdout and "</odoo>" in views_proc.stdout, (
            f"views/views.xml must still be valid, minimal XML: {views_proc.stdout!r}"
        )

        second_pass = strip_scaffold_boilerplate(module_name)
        assert second_pass == [], "must be idempotent -- nothing left to strip on a second call"
        print(f"PASS: strip_scaffold_boilerplate() removed {len(removed)} unclaimable file(s), idempotent on retry")
    finally:
        _cleanup_module(module_name)


def test_strip_scaffold_boilerplate_resilient_to_views_xml_already_deleted():
    """Real, confirmed bug found live (2026-08-11, task 18fca388): this function always assumed
    views/views.xml (and its parent views/ directory) still physically existed on disk from
    scaffold_module()'s own original creation -- true for every round until delete_module_file()
    made genuine file deletion a real, exercised capability throughout the pipeline (a round
    that genuinely doesn't need any views now explicitly deletes the file rather than merely not
    referencing it). Once a prior round's own content genuinely deleted the file, the bare `>`
    redirect this function used to reset it back to an empty skeleton crashed the ENTIRE round
    with "No such file or directory" -- confirmed live, the very next real round after
    delete_module_file() first ran. Reproduced here directly: delete the file exactly like a
    round now legitimately can, then confirm strip_scaffold_boilerplate() still succeeds and
    recreates it.
    """
    module_name = f"oma_test_strip_resilient_{uuid.uuid4().hex[:8]}"
    scaffold_module(module_name)
    try:
        strip_scaffold_boilerplate(module_name)  # first pass, same as any real round 1
        views_dir = f"{_MODULE_DEV_ADDONS_DIR}/{module_name}/views"
        rm_proc = _run_in_container(f"rm -f {views_dir}/views.xml && rmdir {views_dir}")
        assert rm_proc.returncode == 0, f"fixture setup failed: {rm_proc.stderr}"
        check_proc = _run_in_container(f"test -d {views_dir}")
        assert check_proc.returncode != 0, "fixture assumption: views/ directory must be genuinely gone"

        strip_scaffold_boilerplate(module_name)  # must not raise ScaffoldError

        views_proc = _run_in_container(f"cat {_MODULE_DEV_ADDONS_DIR}/{module_name}/views/views.xml")
        assert "<odoo>" in views_proc.stdout and "</odoo>" in views_proc.stdout, (
            f"views/views.xml must be recreated as a valid, minimal skeleton even when it (and "
            f"its parent directory) were genuinely absent beforehand: {views_proc.stdout!r}"
        )
        print("PASS: strip_scaffold_boilerplate() is resilient to views/views.xml (and its "
              "parent directory) having already been genuinely deleted by a prior round")
    finally:
        _cleanup_module(module_name)


def test_strip_scaffold_boilerplate_never_removes_data_directory_files():
    """Phase 25E (2026-07-26) regression fix, root-caused live during
    task 006's own regression re-run: `odoo-bin scaffold` never creates
    a top-level `data/` directory at all -- so any `data/*.xml` file
    present can only be real content a PRIOR round of this same task
    legitimately wrote via `GeneratedModuleFiles.extra_data_files` (the
    general mechanism the sequence-assignment autofix uses for its own
    `data/sequence_data.xml`, Phase 25C). Confirmed live: this function
    runs unconditionally at the start of every round, so a round-2
    retry deleted its own round-1 sequence record right back out from
    under itself every time, an unrecoverable loop -- Code-Review then
    correctly (but pointlessly) re-flagged "no ir.sequence record
    defined" every following round, no amount of LLM regeneration could
    ever escape it since the file was gone before each retry even
    started. This simulates exactly that: write a fake `data/x.xml`
    file into an otherwise-stripped scaffold, then confirm a second
    `strip_scaffold_boilerplate()` call leaves it alone.
    """
    module_name = f"oma_test_strip_data_{uuid.uuid4().hex[:8]}"
    scaffold_module(module_name)
    try:
        strip_scaffold_boilerplate(module_name)  # first pass: normal scaffold cruft removed

        module_dir = f"{_MODULE_DEV_ADDONS_DIR}/{module_name}"
        write_proc = _run_in_container(
            f"mkdir -p {module_dir}/data && "
            f"printf '%s\\n' '<odoo><record id=\"x\" model=\"ir.sequence\"/></odoo>' "
            f"> {module_dir}/data/sequence_data.xml"
        )
        assert write_proc.returncode == 0, f"fixture setup failed: {write_proc.stderr}"

        removed = strip_scaffold_boilerplate(module_name)
        assert not any("data/" in p for p in removed), (
            f"a data/*.xml file written by a prior round must never be stripped, but got: {removed}"
        )

        find_proc = _run_in_container(f"find {module_dir}/data -type f")
        after = {line for line in find_proc.stdout.splitlines() if line.strip()}
        assert any(p.endswith("data/sequence_data.xml") for p in after), (
            "data/sequence_data.xml must genuinely survive stripping on disk"
        )
        print("PASS: strip_scaffold_boilerplate() never removes files under data/ -- a prior "
              "round's real generated data record (e.g. the sequence-assignment autofix's own "
              "ir.sequence XML) survives a subsequent round's stripping pass")
    finally:
        _cleanup_module(module_name)


def test_lint_returns_structured_findings_on_fresh_scaffold():
    module_name = f"oma_test_lint_{uuid.uuid4().hex[:8]}"
    scaffold_module(module_name)
    try:
        result = lint_module(module_name)
        assert result.ran_ok, f"pylint-odoo must run cleanly even when it finds issues: {result.raw_error}"
        assert len(result.findings) > 0, "a fresh, unmodified scaffold is expected to legitimately fail some checks"
        assert all(f.message_id and f.symbol and f.path for f in result.findings)
        print(
            f"PASS: lint_module() returned {len(result.findings)} structured findings "
            f"(not a raw log dump) for a fresh scaffold, e.g. {result.findings[0].symbol!r}"
        )
    finally:
        _cleanup_module(module_name)


def test_install_succeeds_on_the_duplicate_db_and_field_genuinely_exists():
    """Real, dated update (2026-07-10): the Postgres-ownership blocker
    this test originally exercised (installing a module that adds a
    column to an existing model used to fail with InsufficientPrivilege
    on this deployment's duplicate databases) is CONFIRMED RESOLVED --
    re-ran the exact same real module/db pair this test always used and
    it now installs cleanly. Whoever administers the actual Postgres
    server behind the dev host must have applied the one-time table-
    ownership fix the code's own comments always said was needed and
    outside this project's access boundary.

    Repurposed as a genuine positive-path test rather than deleted --
    confirms not just install_module()'s own reported success, but the
    real, independent state (this phase's own new post-install
    ir.module.module.state check) AND the real field's actual presence
    on res.partner, not just a claim.
    """
    result = install_module(KNOWN_THROWAWAY_MODULE, EXISTING_DUPLICATE_DB)
    assert result.success is True, (
        f"expected a genuine success now that the ownership blocker is resolved, got "
        f"error_kind={result.error_kind!r}: {result.message}"
    )
    field = check_field_exists_on_model(EXISTING_DUPLICATE_DB, "res.partner", "oma_throwaway_test_field")
    assert field, "install_module() reported success but the real field genuinely isn't on res.partner"
    print(
        "PASS: install_module() succeeded for real on the duplicate db (previously-documented "
        "Postgres-ownership blocker confirmed resolved), and the field genuinely exists -- "
        "not just claimed"
    )


def test_install_module_positively_verifies_state_not_just_absence_of_errors():
    """Real, confirmed gap found live (2026-07-10, an actual end-to-end
    task against real infra): Odoo's own module graph builder can
    silently skip a module -- exits 0, no CRITICAL, no Traceback -- and
    the OLD install_module() would have reported success=True for that.
    Found two independent real repros of this same shape live: a module
    Odoo's graph builder marked 'not installable, skipped', and (this
    test's own case) a module name that doesn't resolve to a real
    directory on disk at all ('invalid module names, ignored').
    install_module() now positively confirms ir.module.module.state ==
    'installed' afterward rather than trusting a clean-looking exit
    alone -- this closes the whole CLASS of silent-skip failures, not
    just the one specific string pattern first found.
    """
    fake_module_name = f"oma_test_nonexistent_{uuid.uuid4().hex[:8]}"
    result = install_module(fake_module_name, EXISTING_DUPLICATE_DB)
    assert result.success is False
    assert result.error_kind == "state_verification_failed"
    assert "state" in result.message.lower()
    print(
        "PASS: install_module() correctly refused to report success for a module that never "
        f"actually reached 'installed' state (error_kind={result.error_kind!r}), even though "
        "the command itself exited cleanly with no known-bad string in its output"
    )


def test_install_refuses_forbidden_production_target():
    raised = False
    try:
        install_module(KNOWN_THROWAWAY_MODULE, "16_202012")
    except ProductionOdooGuardError:
        raised = True
    assert raised, "install_module() must refuse a forbidden target before ever reaching SSH/Odoo"
    print("PASS: install_module() refuses the forbidden Production db before any SSH command runs")


def test_toolchain_class_exposes_all_three_steps():
    toolchain = ModuleDevToolchain()
    assert hasattr(toolchain, "scaffold")
    assert hasattr(toolchain, "lint")
    assert hasattr(toolchain, "install")
    print("PASS: ModuleDevToolchain exposes scaffold/lint/install as one callable interface")


if __name__ == "__main__":
    test_scaffold_produces_expected_skeleton()
    test_scaffold_refuses_to_overwrite_existing_module()
    test_lint_returns_structured_findings_on_fresh_scaffold()
    # Real, pre-existing bug found live (2026-07-28, Phase 28B
    # regression sweep): this block called a function name
    # ('test_install_surfaces_postgres_ownership_blocker_as_structured_
    # result') that does not exist anywhere in this file -- a stale
    # reference from some earlier rename that was never updated here,
    # crashing this file's own __main__ block with a NameError before
    # ever reaching the two real, correct calls below. The two real
    # install tests actually defined in this file were consequently
    # NEVER run when this file was executed directly (only
    # test_scaffold_*/test_lint_* above it ever got the chance to run).
    # Fixed by calling the two real, currently-defined functions.
    test_install_succeeds_on_the_duplicate_db_and_field_genuinely_exists()
    test_install_module_positively_verifies_state_not_just_absence_of_errors()
    test_install_refuses_forbidden_production_target()
    test_toolchain_class_exposes_all_three_steps()
    print("\nALL MODULE DEV TOOLCHAIN TESTS PASSED")
