"""Phase 35 §17.6.1.1 / §18.4: the rollback-safety dry-run reversibility probe -- replaces
(eventually; see below) the static migration-class allowlist with an EXECUTED sandbox
install-then-uninstall-then-diff check, per §18.4's own research and recommendation.

Reuses the existing, already-proven sandbox mechanism (create_sandbox_database/
drop_sandbox_database, install_module(sandbox=True), uninstall_module) -- no new DB-cloning
infrastructure invented here. The real signal checked, per §18.4's own Odoo-specific finding: a
module's `ir_model_data` rows (Odoo's own tracked-ownership mechanism -- see toolchain.py's own
uninstall_module docstring for the mechanism this reuses) should be fully gone after a clean
uninstall; any that remain are real, concrete residue, not a classification guess.

Real, honest scope limitation, stated plainly rather than hidden: this checks `ir_model_data`
residue specifically (the mechanism §18.4's research identified as the most common real source of
uninstall residue -- hand-created data via raw SQL or dynamically-created fields is NOT tracked by
`ir_model_data` and is NOT caught by this probe). A full schema+data snapshot diff (comparing
every table, not just the ir_model_data-tracked subset) is explicitly named in §17.10/§18.4's own
"what this does not resolve" as a larger, separate follow-up, not built here.

Ships in log_only mode (manager/graph_governance_flags.py's "rollback_dry_run_probe" gate) -- this
module can be called standalone (e.g. from a scheduled/manual audit run against a real generated
module) without being wired into the live per-task install path yet, since running a full extra
install+uninstall cycle on the sandbox adds real, non-trivial latency to every task if wired in
naively; the wiring decision (call it on every install vs. periodically vs. on-demand) is left
explicit and undecided here rather than silently defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RollbackProbeResult:
    module_name: str
    probe_ran: bool
    install_succeeded: bool
    uninstall_succeeded: bool
    residue_row_count: int
    residue_detail: str
    clean: bool


def _count_ir_model_data_rows(db: str, module_name: str, container: str) -> int:
    """Real psql query against the sandbox DB, reusing the exact same credential-extraction
    pattern tools_odoo/module_dev/toolchain.py's own _sandbox_pg_command() already uses (never
    a hardcoded credential). Returns -1 on any query failure (distinct from a real 0, so a
    caller can tell "confirmed clean" from "couldn't check").
    """
    from tools_odoo.module_dev.toolchain import _ODOO_CONF_PATH, _run_in_container

    query = f"SELECT count(*) FROM ir_model_data WHERE module = '{module_name}';"
    cmd = (
        f"db_host=$(grep -oP '^db_host\\s*=\\s*\\K.*' {_ODOO_CONF_PATH}) && "
        f"db_user=$(grep -oP '^db_user\\s*=\\s*\\K.*' {_ODOO_CONF_PATH}) && "
        f"db_password=$(grep -oP '^db_password\\s*=\\s*\\K.*' {_ODOO_CONF_PATH}) && "
        f"PGPASSWORD=\"$db_password\" psql -h \"$db_host\" -U \"$db_user\" -d {db} "
        f"-t -A -c \"{query}\""
    )
    proc = _run_in_container(cmd, timeout=30, container=container)
    try:
        return int((proc.stdout or "").strip())
    except (ValueError, AttributeError):
        return -1


def run_rollback_dry_run_probe(
    module_name: str, sandbox_db: str, container: str,
) -> RollbackProbeResult:
    """The real probe: install into the sandbox, count real ir_model_data rows this module
    owns, uninstall, count again. `clean=True` only when the post-uninstall count is a
    CONFIRMED 0 (never on a failed/uncertain query, which reports clean=False with the reason
    in residue_detail -- an uncertain result must never be reported as a false pass).
    """
    from tools_odoo.module_dev.toolchain import install_module, uninstall_module_sandbox

    # Matches specialists/build/specialist.py's own local _SANDBOX_REMOTE_PORT = 8072 constant
    # rather than importing infra.odoo_settings.py's underscore-private one directly.
    sandbox_remote_port = 8072

    install_result = install_module(
        module_name, sandbox_db, remote_port=sandbox_remote_port, container=container, sandbox=True,
    )
    if not install_result.success:
        return RollbackProbeResult(
            module_name=module_name, probe_ran=True, install_succeeded=False,
            uninstall_succeeded=False, residue_row_count=-1,
            residue_detail=f"probe could not even install the module: {install_result.message}",
            clean=False,
        )

    pre_uninstall_count = _count_ir_model_data_rows(sandbox_db, module_name, container)

    uninstall_result = uninstall_module_sandbox(module_name, sandbox_db, container)
    if not uninstall_result.success:
        return RollbackProbeResult(
            module_name=module_name, probe_ran=True, install_succeeded=True,
            uninstall_succeeded=False, residue_row_count=-1,
            residue_detail=f"uninstall itself failed: {uninstall_result.message}",
            clean=False,
        )

    post_uninstall_count = _count_ir_model_data_rows(sandbox_db, module_name, container)
    if post_uninstall_count < 0:
        return RollbackProbeResult(
            module_name=module_name, probe_ran=True, install_succeeded=True,
            uninstall_succeeded=True, residue_row_count=-1,
            residue_detail="post-uninstall residue query itself failed -- result is UNCERTAIN, "
                            "not a confirmed pass.",
            clean=False,
        )
    if post_uninstall_count > 0:
        return RollbackProbeResult(
            module_name=module_name, probe_ran=True, install_succeeded=True,
            uninstall_succeeded=True, residue_row_count=post_uninstall_count,
            residue_detail=(
                f"{post_uninstall_count} ir_model_data row(s) for {module_name!r} still exist "
                f"after uninstall (had {pre_uninstall_count if pre_uninstall_count >= 0 else '?'} "
                f"before) -- real, confirmed rollback residue."
            ),
            clean=False,
        )
    return RollbackProbeResult(
        module_name=module_name, probe_ran=True, install_succeeded=True,
        uninstall_succeeded=True, residue_row_count=0,
        residue_detail="confirmed clean: 0 ir_model_data rows remain after uninstall.",
        clean=True,
    )
