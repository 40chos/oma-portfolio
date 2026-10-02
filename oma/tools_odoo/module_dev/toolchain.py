"""Phase 8: the module-authoring/deployment toolchain -- scaffold ->
lint -> install, wrapped as one callable tool interface for the Build
specialist (phase 9). Per the technical document's own honest finding,
this is the one piece with no mature open-source shortcut, so it's
built directly on Odoo's own official tools:

  odoo-bin scaffold  -- official module-skeleton generator
  pylint-odoo        -- OCA's static analyzer for Odoo modules
  odoo-bin -i / click-odoo-contrib's click-odoo-update -- install/upgrade

All three run inside the odoo16-dev container via the same SSH +
`docker exec` channel infra.odoo_jit_apikey.py already uses for
legitimate ORM/container management -- never a new access path.

Generated modules live in /mnt/extra-addons, a writable Docker volume
on odoo16-dev (confirmed via `docker inspect`: it's an RW volume,
unlike /opt/site, which is a READ-ONLY bind mount shared with the host
image and not a safe place to write generated code into, even if it
were writable). The addons path used for scaffold/lint/install
commands here is passed as an explicit `--addons-path` CLI override --
the original path list plus /mnt/extra-addons.

Real, confirmed gap found live (2026-07-10): for its first several
weeks this module was deliberately built so /mnt/extra-addons was
*only* ever added via that CLI override, on the reasoning that the
container's own persistent odoo.conf should never be edited for a
scratch/dev directory. That reasoning had a real, serious blind spot,
found via a live user report ("Contacts wouldn't open"): the actual,
persistent Odoo WEB SERVER (the process a real browser talks to,
started once from odoo.conf, never restarted per-task) never got that
override -- so a module could genuinely install (real DB row, real
column, a real inherited view referencing the new field) while being
completely invisible to the live UI, which then crashed trying to
render the view against a model that, from ITS OWN process's
perspective, never had the field at all. Root-caused and fixed
directly: /mnt/extra-addons is now ALSO appended to
/etc/odoo16-dev/odoo.conf's own persistent addons_path (backed up
first, `odoo.conf.bak_<timestamp>`, same convention as every other
odoo.conf edit this project has made), and odoo16-dev was restarted
once to pick it up. The explicit `--addons-path` CLI override below is
kept anyway, deliberately redundant -- it's what every scaffold/lint/
install call in this module has always used and keeps working
identically whether or not odoo.conf also has it, and it's what
protects a one-off diagnostic shell command from this exact class of
mistake if odoo.conf ever drifts again.

Every install() call re-validates the safety guard on the target `db`
via infra.odoo_settings._assert_safe_odoo_target -- redundant with any
caller's own check, deliberately, same pattern as odoo_jit_apikey.py.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field

from infra.fencing import acquire_db_install_lock, release_db_install_lock
from infra.odoo_settings import _assert_safe_odoo_target
from tools_odoo.odoo_schema_client import is_fast_path_eligible

_CONTAINER_ENV = "OMA_ODOO_CONTAINER"
_ODOO_BIN_PATH = "/usr/bin/odoo"  # the official odoo:16 image's own binary path
_ODOO_CONF_PATH = "/etc/odoo/odoo.conf"
_LOCAL_BIN = "/var/lib/odoo/.local/bin"  # where pip3 --user lands pylint/click-odoo-*
_FILESTORE_DIR = "/var/lib/odoo/filestore"

# The container's real, persistent addons_path (confirmed via odoo.conf)
# plus our own writable module-dev directory appended -- passed as a
# CLI override on every command in this module, never written to disk.
_MODULE_DEV_ADDONS_DIR = "/mnt/extra-addons"
_BASE_ADDONS_PATH = [
    # The official odoo:16 image's own bundled core addons -- see
    # docker-compose.yml's odoo.conf generation. The original deployment's
    # equivalent list of real OCA/custom addon repo paths doesn't apply
    # here (none of those third-party modules are part of a fresh Odoo CE
    # install); this is the real, actual addons_path confirmed via that
    # same odoo.conf.
    "/usr/lib/python3/dist-packages/odoo/addons",
]
_FULL_ADDONS_PATH = ",".join(_BASE_ADDONS_PATH + [_MODULE_DEV_ADDONS_DIR])

_CUSTOM_SITE_ADDONS_ROOT = "/opt/site/site16"


def list_custom_site_module_names(container: str | None = None) -> list[str]:
    """Phase 22 follow-up (2026-07-23): the full, real list of module
    directory names under `/opt/site/site16` -- SITE's own genuinely
    custom business modules (`project_meerwerk`, `mis_base_extend`,
    etc.), used by `manager.scope_detection` to give the LLM a REAL,
    closed list to pick from instead of asking it to invent a dotted
    Odoo technical model name from an arbitrary domain word with no
    grounding at all (confirmed unreliable live: asked to guess
    "meerwerk" -> `project.meerwerk` cold, the model got it right only
    inconsistently across repeated identical calls -- multiple-choice
    selection from a real list is a fundamentally easier, more
    reliable task for an LLM than open-ended technical-name
    generation). Filters out non-module entries (`README.md`,
    anything ending in `.disabled`). Returns an empty list (never
    raises) on any failure -- the caller must degrade to "no
    candidates, treat as new" on any uncertainty.
    """
    try:
        result = _run_in_container(f"ls {_CUSTOM_SITE_ADDONS_ROOT}", timeout=15, container=container)
        if result.returncode != 0:
            return []
        return [
            line.strip() for line in result.stdout.splitlines()
            if line.strip() and not line.strip().endswith((".md", ".disabled"))
        ]
    except Exception:
        return []


def is_custom_site_module(module_name: str, container: str | None = None) -> bool:
    """Phase 22 follow-up (2026-07-23): True iff `module_name` lives
    under `/opt/site/site16` specifically -- the ONE root in
    `_BASE_ADDONS_PATH` reserved for SITE's own genuinely custom
    business modules (`project_meerwerk`, `mis_base_extend`, etc.),
    distinct from core Odoo (`/opt/site/16/addons`,
    `/opt/site/16/odoo/addons`) and every OCA repo (the other
    `_BASE_ADDONS_PATH` entries). Used by
    `manager.scope_detection.detect_existing_custom_module_target()`
    to decide whether a task's goal targeting an already-real model
    should redirect Build to extend that REAL module
    (`edit_existing_module:`) instead of scaffolding a brand-new one
    from scratch -- deliberately narrow to this one path so a standard
    Odoo/OCA model (already handled correctly via a normal new
    `_inherit` module) never gets misrouted into this path by mistake.
    False on any lookup failure -- never guessed at.
    """
    try:
        result = _run_in_container(
            f"test -d {_CUSTOM_SITE_ADDONS_ROOT}/{module_name}", timeout=15, container=container,
        )
        return result.returncode == 0
    except Exception:
        return False


def list_own_scaffolded_module_names(container: str | None = None) -> list[str]:
    """Real, confirmed gap found live (2026-08-10, task 4724a61f, the flagship task's own
    follow-up correction task): `list_custom_site_module_names()`/`is_custom_site_module()` are
    deliberately scoped ONLY to `/opt/site/site16` -- genuine external SITE customer modules --
    so `detect_existing_custom_module_target()` had ZERO way to recognize a module THIS PIPELINE
    ITSELF had previously scaffolded under `/mnt/extra-addons` (e.g.
    `oma_build_a_complete_field_ab52b7f8`, already real and installed) as an existing-module
    target for a plain-text `run_turn()` goal that named it explicitly. Confirmed live: a goal
    literally containing the real module's own name verbatim still resolved
    `existing_module_dependency` to None, `contract.inputs` stayed `[]`, and Build scaffolded a
    brand-new, disconnected module (`oma_extend_the_existing_field_c53cd979`) with a manifest
    `depends: ['base']` that never declared the real dependency -- the generated views then
    failed sandbox install with "Model not found: oma.equipment" because the module owning that
    model was never even listed as a dependency. This is the `/mnt/extra-addons` sibling of
    `list_custom_site_module_names()`, same conservative contract: returns a real, live directory
    listing (never guessed at) and an empty list (never raises) on any failure.
    """
    try:
        result = _run_in_container(f"ls {_MODULE_DEV_ADDONS_DIR}", timeout=15, container=container)
        if result.returncode != 0:
            return []
        return [
            line.strip() for line in result.stdout.splitlines()
            if line.strip() and not line.strip().endswith((".md", ".disabled"))
        ]
    except Exception:
        return []


def is_own_scaffolded_module(module_name: str, container: str | None = None) -> bool:
    """`/mnt/extra-addons` sibling of `is_custom_site_module()` -- see
    `list_own_scaffolded_module_names()`'s own docstring for the real, live bug this closes.
    True iff `module_name` genuinely lives under `/mnt/extra-addons` (this pipeline's own
    writable module-dev directory) right now. False on any lookup failure -- never guessed at.
    """
    try:
        result = _run_in_container(
            f"test -d {_MODULE_DEV_ADDONS_DIR}/{module_name}", timeout=15, container=container,
        )
        return result.returncode == 0
    except Exception:
        return False


class ModuleDevError(RuntimeError):
    """Base for structured toolchain failures -- callers should branch
    on the specific subclass, never parse a raw log/traceback string.
    """


class ScaffoldError(ModuleDevError):
    pass


@dataclass
class ScaffoldResult:
    module_name: str
    dest_dir: str
    files: list[str]


@dataclass
class LintFinding:
    message_id: str
    symbol: str
    message: str
    path: str
    line: int
    column: int
    severity: str  # pylint's own "type": convention/warning/error/...


@dataclass
class LintResult:
    module_name: str
    ran_ok: bool  # whether pylint itself executed without crashing
    findings: list[LintFinding] = field(default_factory=list)
    raw_error: str | None = None  # only set if ran_ok is False


@dataclass
class InstallFinding:
    """P13 item 11, Unit 1 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    §22.2 item 11): one structured, artifact-tagged finding parsed out of a real install's raw
    output -- closes the gap the item's own critique found by direct code read: `InstallResult` was
    single-boolean-shaped, with no way for a caller to match a specific warning against a specific
    not-yet-built `ConstraintNode.creates` artifact (the join-suppression fix, Unit 2, depends on
    exactly this). `artifact_names` are real Odoo model names actually named by the warning text --
    never guessed, never populated from anything but a positive regex match.
    """

    kind: str
    artifact_names: list[str] = field(default_factory=list)
    raw_text: str = ""


# Real, confirmed Odoo 16 warning text -- verified directly against the real dev container's own
# installed odoo/modules/loading.py (grepped live, not guessed):
#   f"The models {models} have no access rules in module {module_name}, consider adding some, like:"
# `models` is a Python list literal repr of real model-name strings (e.g. "['project.satisfaction']").
_MISSING_ACCESS_RULES_RE = re.compile(
    r"The models? (\[[^\]]*\]) have no access rules in module (\S+), consider adding some",
)


def _parse_install_findings(combined: str) -> list[InstallFinding]:
    """P13 item 11, Unit 1: a small, deterministic, zero-LLM parsing pass over an install's raw
    output. Deliberately conservative -- only the one real, confirmed warning shape (the item's own
    worked example, the `0689823f-...` stuck task) is parsed today; every other warning/log line is
    simply not represented as a finding, never guessed at. Extending this to more patterns is real,
    separately-scoped future work, not something to fake here with a broad catch-all.
    """
    findings: list[InstallFinding] = []
    for match in _MISSING_ACCESS_RULES_RE.finditer(combined):
        models_repr = match.group(1)
        try:
            models = ast.literal_eval(models_repr)
        except (ValueError, SyntaxError):
            models = []
        if isinstance(models, list) and models and all(isinstance(m, str) for m in models):
            findings.append(InstallFinding(
                kind="missing_access_rules", artifact_names=models, raw_text=match.group(0),
            ))
    return findings


@dataclass
class InstallResult:
    module_name: str
    db: str
    success: bool
    error_kind: str | None = None  # e.g. "postgres_ownership_blocked"
    message: str = ""
    log_tail: str = ""
    # Phase 28A (2026-07-28): real, confirmed structural gap -- install
    # success was the only signal this returned; there was no way to
    # know whether a module's own real, generated tests (see
    # specialists/build/specialist.py's own tests_py field) actually
    # PASSED, only that install itself didn't crash. None means tests
    # were never requested (test_enable=False, the default -- preserves
    # every existing caller's exact prior behavior); a real int means
    # Odoo's own test runner genuinely ran that many tests and this many
    # failed/errored, parsed directly from its own real summary line
    # ("N failed, M error(s) of K tests"), never assumed from install
    # success alone.
    tests_run: int | None = None
    tests_failed: int | None = None
    tests_errored: int | None = None
    # P13 item 11, Unit 1: structured, artifact-tagged findings parsed from this install's raw
    # output (see _parse_install_findings() above) -- empty list means either a clean install or a
    # failure whose text didn't match any known structured-finding pattern, never "not checked."
    findings: list[InstallFinding] = field(default_factory=list)


def _run_in_container(bash_command: str, timeout: int = 180, container: str | None = None) -> subprocess.CompletedProcess:
    import os

    # Real, deliberate design point (2026-07-13, Phase 20 sandbox
    # integration): `container` is an explicit PER-CALL override, never
    # a mutation of OMA_ODOO_SSH_CONTAINER itself -- this process runs
    # many tasks concurrently in the same event loop, so temporarily
    # reassigning the shared env var around a sandbox call would be a
    # real race condition (another task's coroutine could read the
    # mutated value mid-call and write into the wrong container).
    # Defaults to the existing env-var behavior, unchanged, for every
    # call site that doesn't pass this -- additive, not a behavior change.
    container = container or os.environ[_CONTAINER_ENV]
    # Real, confirmed gap found live switching to dev-agent (2026-07-09):
    # /etc/odoo16-dev on the HOST (bind-mounted read-only into the
    # container as /etc/odoo) was 750 root:root, blocking the
    # container's own default "odoo" user from even entering the
    # directory -- odoo-bin itself couldn't read its own config.
    # Root-caused and fixed properly at the source (chmod 755 on the
    # host directory, a real, one-time, minimal permission repair --
    # confirmed the file itself was already world-readable, only the
    # directory's own execute/traverse bit was missing) rather than
    # running every command as root here, which would have broken
    # click-odoo-uninstall's own --user-installed Python packages
    # (found live: they live under the "odoo" user's own home, not
    # visible to root). Default (non-root) exec is correct again.
    # Real bug found live (2026-07-14): repr() quotes with single quotes
    # ONLY when the string contains no double quotes -- the moment a
    # bash_command embeds a double quote (e.g. _sandbox_pg_command()'s
    # PGPASSWORD="$db_password" -h "$db_host"), repr() silently switches
    # to double-quote wrapping instead, and the enclosing shell then closes
    # the outer bash -c "..." argument at that FIRST embedded ", splitting
    # the command and producing "syntax error near unexpected token ')'"
    # deterministically, every single call. shlex.quote() always emits a
    # single-quoted, POSIX-safe token regardless of what's inside.
    #
    # Stage 5 port: the original ran this over SSH to a second real host
    # (odoo-dev.int) that itself ran `docker exec`. In this single-host
    # Compose port there's no second host -- `docker exec` runs directly,
    # same quoting/timeout handling below, unchanged.
    docker_cmd = ["docker", "exec", container, "bash", "-c", bash_command]
    try:
        return subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # Real, confirmed bug found live (2026-08-11, task 07141af5, ticket_bulk_close node):
        # this function is a shared, widely-used utility (38+ call sites across this codebase)
        # that never caught its own `subprocess.run(..., timeout=...)` timeout -- a genuinely
        # slow SSH/docker-exec call on a busy/flaky dev host (the same class of transient issue
        # already confirmed live elsewhere this session, e.g. warm-worker ConnectionRefusedError)
        # exceeding its own timeout raised `subprocess.TimeoutExpired` straight past every one
        # of those 38 callers, most of which never wrap this call in their own try/except --
        # confirmed live, TWICE in the same night, crashing the ENTIRE task with an uncaught
        # exception ("Task crashed mid-round") rather than failing just the one round, for two
        # completely different commands (a coverage/test run, and a plain `find` listing).
        # Fixed at the SOURCE rather than patching each call site individually as new crash
        # instances keep surfacing: returns a real, honest `CompletedProcess` (the exact type
        # every caller already expects and already checks `.returncode`/`.stdout`/`.stderr`
        # on) with a sentinel `returncode=-1` and an explanatory `stderr`, instead of raising --
        # every existing caller's own "nonzero returncode means failure" handling now correctly,
        # gracefully absorbs a timeout the same way it already absorbs any other command
        # failure, with zero behavior change for the normal (non-timeout) case.
        return subprocess.CompletedProcess(
            args=docker_cmd, returncode=-1,
            stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=(exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
            + f"\n[oma] _run_in_container command timed out after {timeout}s: {bash_command[:200]!r}",
        )


class SandboxDatabaseError(RuntimeError):
    """Raised when the sandbox's own createdb/dropdb (below) fails."""


_DB_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def _sandbox_pg_command(pg_command: str, container: str) -> str:
    """Builds a bash command that reads db_host/db_user/db_password
    straight out of the TARGET container's own /etc/odoo/odoo.conf
    (never hardcoded in this repo, never in an env var either -- one
    less place a credential could leak from) and runs `pg_command`
    (createdb/dropdb, both accept the same -h/-U flags) against it.
    """
    return (
        f"db_host=$(grep -oP '^db_host\\s*=\\s*\\K.*' {_ODOO_CONF_PATH}) && "
        f"db_user=$(grep -oP '^db_user\\s*=\\s*\\K.*' {_ODOO_CONF_PATH}) && "
        f"db_password=$(grep -oP '^db_password\\s*=\\s*\\K.*' {_ODOO_CONF_PATH}) && "
        f"PGPASSWORD=\"$db_password\" {pg_command} -h \"$db_host\" -U \"$db_user\""
    )


def create_sandbox_database(db_name: str, container: str, template: str | None = None) -> None:
    """Creates a fresh Postgres database for the sandbox's own use --
    direct createdb via the container's own already-configured Postgres
    credentials, deliberately NOT going through Odoo's XML-RPC Database
    Manager (infra.odoo_admin.create_database()): confirmed live
    (2026-07-13) that the real dev target's own `list_db = False`
    setting blocks that entire RPC service, not just list_databases()
    as an earlier comment assumed -- and changing that setting on the
    real, shared dev instance is out of scope for what the sandbox
    needs.

    `template`, added 2026-07-14 (the project owner, real time pressure -- 100-200s
    per round was making the area-by-area rollout impractical): measured
    live against the real sandbox container, a genuinely empty database's
    first `-i <module>` call spends ~9s of its ~30s total on bootstrapping
    Odoo's own 8 base modules (2985 queries) before it ever touches the
    module actually being tested -- identical, repeated work every single
    round. `createdb -T <template>` clones a Postgres database directly
    at the storage layer (near-instant, confirmed ~1.3s), so pointing
    `template` at a database with base already installed (see
    infra.odoo_settings.SANDBOX_TEMPLATE_DB) skips that bootstrap
    entirely -- the clone starts with base's tables already present, 0
    queries needed for it. None (default) preserves the exact prior
    behavior (truly empty database) for any caller not opting in.
    Does NOT touch generation/quality logic at all -- purely how the
    empty starting point is produced.

    Real, confirmed bug found live (2026-07-15) and the reason this
    speed-up was REVERTED for a full week: `createdb -T` clones
    Postgres's own rows (including ir.attachment records with hash-
    based filestore paths) but never the actual filestore BLOB files
    on disk, which live in a separate, db-name-scoped directory
    (/var/lib/odoo/filestore/<db_name>/) Postgres knows nothing about.
    Any module pulling in a dependency whose demo data touches
    attachments (hr's own demo data does -- a default employee photo)
    hit a genuine FileNotFoundError deep in Odoo's own
    ir_attachment._file_read.

    Fixed properly tonight (2026-07-22), researched against the
    standard tool for exactly this (click-odoo-copydb, which does both
    halves together for the same reason): when a template is given,
    ALSO copies the template's own filestore directory to the new
    db's filestore directory right after the createdb clone -- so the
    sandbox starts with BOTH the Postgres rows AND the real blob files
    the golden template's own base install produced. Confirmed live:
    the golden template's filestore is a real, measured 1.1MB -- a
    plain `cp -r` of that is sub-second, nowhere near erasing the
    ~9s/round saved by skipping base's own bootstrap. If the source
    filestore directory doesn't exist at all (e.g. a template with no
    attachment-touching demo data), this is a silent no-op, not an
    error -- exactly matching create_database()'s own "nothing to
    copy" case.
    """
    if not _DB_NAME_RE.match(db_name):
        raise SandboxDatabaseError(f"refusing unsafe database name {db_name!r}")
    createdb_cmd = f"createdb {db_name}" + (f" -T {template}" if template else "")
    proc = _run_in_container(_sandbox_pg_command(createdb_cmd, container), container=container)
    if proc.returncode != 0:
        raise SandboxDatabaseError(f"createdb {db_name!r} failed: {proc.stderr[-1000:]}")
    if template:
        copy_cmd = (
            f"src={_FILESTORE_DIR}/{template}; "
            f"if [ -d \"$src\" ]; then cp -r \"$src\" {_FILESTORE_DIR}/{db_name}; fi"
        )
        copy_proc = _run_in_container(copy_cmd, container=container)
        if copy_proc.returncode != 0:
            raise SandboxDatabaseError(
                f"filestore copy for {db_name!r} (from template {template!r}) failed: "
                f"{copy_proc.stderr[-1000:]}"
            )


def drop_sandbox_database(db_name: str, container: str) -> None:
    """Counterpart to create_sandbox_database() -- always called in the
    sandbox pre-flight's own `finally` block (specialists/build/specialist.py),
    success or failure, so the sandbox never accumulates leftover
    databases across rounds.

    Real, confirmed disk-bloat bug found live (2026-07-22): this only
    ever dropped the Postgres DB itself -- the sandbox's own filestore
    directory (/var/lib/odoo/filestore/<db_name>/, created fresh every
    round once create_sandbox_database() started copying one) was
    NEVER removed, silently accumulating one leftover directory per
    round forever. Confirmed live: 746+ leftover sandbox filestore
    directories on disk from this session's own testing alone, going
    back to 2026-07-14. Fixed: also removes the filestore directory,
    best-effort (a missing/already-gone directory is not an error --
    same conservative posture as everywhere else in this module).
    """
    if not _DB_NAME_RE.match(db_name):
        raise SandboxDatabaseError(f"refusing unsafe database name {db_name!r}")
    proc = _run_in_container(_sandbox_pg_command(f"dropdb {db_name}", container), container=container)
    if proc.returncode != 0:
        raise SandboxDatabaseError(f"dropdb {db_name!r} failed: {proc.stderr[-1000:]}")
    _run_in_container(f"rm -rf {_FILESTORE_DIR}/{db_name}", container=container)


def scaffold_module(module_name: str, container: str | None = None) -> ScaffoldResult:
    """Runs `odoo-bin scaffold <name> .` inside /mnt/extra-addons.
    Refuses if a directory of that name already exists there -- this
    must never silently overwrite an existing module's files.

    container: explicit per-call target override (see
    _run_in_container()'s own docstring) -- None (default) preserves
    every existing call site's exact prior behavior.
    """
    check = _run_in_container(f"test -d {_MODULE_DEV_ADDONS_DIR}/{module_name}", container=container)
    if check.returncode == 0:
        raise ScaffoldError(
            f"{module_name!r} already exists in {_MODULE_DEV_ADDONS_DIR} -- "
            f"refusing to scaffold over it. Remove it first if this is intentional."
        )

    cmd = f"cd {_MODULE_DEV_ADDONS_DIR} && {_ODOO_BIN_PATH} scaffold {module_name} ."
    proc = _run_in_container(cmd, container=container)
    if proc.returncode != 0:
        raise ScaffoldError(f"odoo-bin scaffold failed (rc={proc.returncode}): {proc.stderr[-2000:]}")

    find_proc = _run_in_container(f"find {_MODULE_DEV_ADDONS_DIR}/{module_name} -type f", container=container)
    files = [line for line in find_proc.stdout.splitlines() if line.strip()]
    if not files:
        raise ScaffoldError(f"scaffold reported success but no files found under {module_name!r}")
    return ScaffoldResult(module_name=module_name, dest_dir=_MODULE_DEV_ADDONS_DIR, files=files)


# Root-caused live (2026-07-12), after 48 rounds of a single task never
# passing a single round: `odoo-bin scaffold` (above) always creates its
# own full stock module tree -- controllers/controllers.py,
# controllers/__init__.py, demo/demo.xml, views/templates.xml -- none of
# which specialists.build.specialist.GeneratedModuleFiles even HAS a
# field for. Every prior fix attempt this task went through (recurrence
# escalation, oscillation detection, progressively stronger "add ONLY
# this one field" prompt instructions) was aimed at the Build LLM for a
# problem the LLM never actually caused: Code-Review was correctly
# flagging *scaffold's own untouched stub files* as "out-of-scope"
# every single round, because nothing ever deleted them. Confirmed live
# via a real `find` on an existing scaffolded module on odoo16-dev --
# controllers/, demo/, and views/templates.xml were sitting there
# unmodified next to the real, Build-authored files.
#
# The only files write_module_file() (below) or GeneratedModuleFiles
# ever populate are __manifest__.py, models/models.py,
# security/ir.model.access.csv, and (conditionally) views/views.xml --
# so those, plus the two bare `__init__.py` stubs scaffold creates that
# every module structurally needs (top-level and models/), are the only
# files any module-dev round can ever have a legitimate reason to keep.
# Everything else scaffold creates is deleted here, unconditionally,
# every round (idempotent -- a second call finds nothing left to
# remove), immediately after scaffold_module()/a retry's existing
# directory is confirmed to exist, and BEFORE Build's own LLM call or
# Code-Review ever sees the module -- never relying on a prompt
# instruction to make an LLM avoid content it never had a schema slot
# for in the first place.
_SCAFFOLD_KEEP_RELPATHS = frozenset({
    "__init__.py",
    "__manifest__.py",
    "models/__init__.py",
    "models/models.py",
    "security/ir.model.access.csv",
    "views/views.xml",
})


def strip_scaffold_boilerplate(module_name: str, container: str | None = None) -> list[str]:
    """Deletes every file under the scaffolded module directory that
    isn't in `_SCAFFOLD_KEEP_RELPATHS` (or under `data/`, see below),
    then removes any directory left empty by that (controllers/, demo/,
    i18n/, and views/ if views/views.xml wasn't kept either). Returns
    the list of relative paths actually removed, purely for trace/log
    visibility -- callers don't need to branch on it.

    Real, general bug found live (Phase 25E, 2026-07-26, task 006's own
    regression re-run): `odoo-bin scaffold` never creates a top-level
    `data/` directory at all (it's not part of its stock template --
    only `controllers/`, `demo/demo.xml`, `views/templates.xml`, per
    this module's own `_SCAFFOLD_KEEP_RELPATHS` docstring above), so
    any `data/*.xml` file found here can ONLY be real content a prior
    round of THIS SAME task legitimately wrote via `GeneratedModuleFiles.
    extra_data_files` (the general mechanism used by the sequence-
    assignment autofix's own `data/sequence_data.xml`, and documented
    as the general home for any other data record shape, e.g. cron
    records). This function runs unconditionally at the START of every
    round, before Build's own LLM call or any live-committed-state read
    -- so a round-2 retry always deleted its own round-1 sequence
    record right back out from under itself, confirmed live: Code-
    Review then correctly (but pointlessly) flagged "next_by_code will
    fail at runtime, no ir.sequence record defined" every following
    round, an unrecoverable loop no amount of LLM re-generation could
    ever escape since the file kept getting deleted before each retry
    even started. Fixed generally, not sequence-specific: `data/` was
    never part of the fixed keep-list's own reasoning (a hardcoded
    finite set of odoo-bin's own known stock files) -- it needs a
    prefix rule instead, since its contents are task-owned, not
    scaffold-owned.
    """
    module_dir = f"{_MODULE_DEV_ADDONS_DIR}/{module_name}"
    find_proc = _run_in_container(f"find {module_dir} -type f", container=container)
    all_files = [line for line in find_proc.stdout.splitlines() if line.strip()]
    prefix = f"{module_dir}/"
    to_remove = [
        path for path in all_files
        if path.startswith(prefix)
        and (relpath := path[len(prefix):]) not in _SCAFFOLD_KEEP_RELPATHS
        and not relpath.startswith("data/")
    ]
    # Real, confirmed bug found live (2026-08-11, task 18fca388): the __init__.py/views.xml
    # reset steps below used to be unreachable whenever `to_remove` was empty (an early `return
    # []` sat right where this comment is now) -- safe in the ORIGINAL design (nothing to fix if
    # __pycache__/controllers/etc were already gone, since __init__.py/views.xml never diverge
    # from correct on their own), but confirmed live to be a real bug once `delete_module_file()`
    # (Bug 86, same night) made genuine file deletion a real, exercised capability: a round with
    # NOTHING else to strip (the common case once __pycache__/controllers/demo are already gone)
    # now skipped ever checking/recreating a genuinely-deleted views/views.xml at all, so the
    # very next round's write step (which needs the file/directory to exist to write into, or
    # simply expects the scaffold's own baseline state) could still hit the identical crash.
    # Disk state reset here is always safe and ephemeral regardless: Build's own generation is
    # grounded in the COMMITTED git history (`prior_files`), never live disk state, so whatever
    # this function resets views.xml/__init__.py to gets correctly re-established by the round's
    # own write step afterward if the committed baseline says otherwise -- there is no real
    # content-loss risk in running these unconditionally, only in skipping them.
    if to_remove:
        # One `rm -f` for every stray file (includes __pycache__/*.pyc,
        # .coverage, controllers/*, demo/*, views/templates.xml -- anything
        # not explicitly kept), then prune now-empty directories. Quoting:
        # these are our own scaffold-derived paths under a slugify_module_name()
        # module name (alnum + underscore only) plus odoo-bin's own fixed
        # template filenames -- no untrusted content ever reaches this command.
        rm_cmd = " ".join(f"rm -f {path!r}" for path in to_remove)
        proc = _run_in_container(
            f"{rm_cmd} && find {module_dir} -mindepth 1 -type d -empty -delete", container=container
        )
        if proc.returncode != 0:
            raise ScaffoldError(
                f"failed stripping scaffold boilerplate from {module_name!r}: {proc.stderr[-1000:]}"
            )
    # Real bug found live in this same fix's own first verification run
    # (2026-07-12): scaffold's top-level __init__.py is kept verbatim
    # (it's in _SCAFFOLD_KEEP_RELPATHS, since every module needs SOME
    # top-level __init__.py), but its stock content is always
    # `from . import controllers\nfrom . import models\n` -- and
    # controllers/ was just deleted above. Left as-is, this is a
    # guaranteed ImportError at install time for every single module,
    # confirmed live (Code-Review: "Imports non-existent 'controllers'
    # module"). models/ is the only subpackage this function ever keeps,
    # so the top-level __init__.py's only correct content, unconditionally,
    # is a single import of it.
    write_proc = _run_in_container(
        f"printf '%s\\n' 'from . import models' > {module_dir}/__init__.py", container=container
    )
    if write_proc.returncode != 0:
        raise ScaffoldError(
            f"failed rewriting top-level __init__.py for {module_name!r} after stripping "
            f"controllers/: {write_proc.stderr[-1000:]}"
        )
    # Real, general bug found live (2026-07-21, live during a Operator demo,
    # task 'warranty.claim'): `views/views.xml` is deliberately KEPT (see
    # _SCAFFOLD_KEEP_RELPATHS above) so Build's own generation can write
    # real view content into it later -- but odoo-bin scaffold's own
    # DEFAULT content for that file is a large block of fully commented-
    # out placeholder XML (a sample list view, window action, server
    # action, and menu items, all wrapped in `<!-- ... -->`). If a
    # round's own focus genuinely doesn't need any views (a very common,
    # correct case -- e.g. "just add the model"), Build's own
    # `generated.views_xml` is empty/None, and _files_from_generated()
    # correctly never includes "views/views.xml" in that round's own
    # write set at all -- so nothing EVER overwrites the scaffold's own
    # stock placeholder text, and it sits there, verbatim, for the
    # entire life of the module. Confirmed live, reproduced 5/5 rounds
    # identically: Code-Review correctly, repeatedly rejected this exact
    # leftover as "commented-out boilerplate... violates the strict
    # constraint to add ONLY strictly required code" -- a real, valid
    # finding (the file is genuinely never referenced in the manifest's
    # own `data` list either, so it's inert to Odoo but still real
    # clutter in the file tree Code-Review reviews) that nothing ever
    # stopped from recurring every single round, since the STRIPPER
    # itself never cleaned this one specific kept file's own content.
    # Fixed the same way __init__.py's own stock content is already
    # corrected above: overwrite views/views.xml with a genuinely
    # minimal, valid, empty skeleton at strip time -- Build's own
    # write_module_file() call for a LATER round that genuinely does
    # add real views still overwrites this unconditionally, exactly as
    # before; a round that doesn't touch it now leaves behind an empty,
    # harmless file instead of a wall of stock scaffold noise.
    views_path = f"{module_dir}/views/views.xml"
    if "views/views.xml" in _SCAFFOLD_KEEP_RELPATHS:
        # Real, confirmed bug found live (2026-08-11, task 18fca388): this always assumed
        # `views/views.xml` (and its parent `views/` directory) still physically existed on
        # disk from `scaffold_module()`'s own original creation -- true for every round until
        # `delete_module_file()` (Bug 86, same night) made genuine file deletion a real,
        # exercised capability throughout the pipeline. Once a PRIOR round's own content
        # genuinely deleted the file (and `find ... -empty -delete` a few lines up then pruned
        # its now-empty parent directory too), this bare `>` redirect crashed the ENTIRE round
        # with "No such file or directory" -- confirmed live, the very next round after
        # `delete_module_file()` first ran for real. `mkdir -p` first, mirroring
        # `write_module_file()`'s own identical pattern, makes this resilient regardless of
        # whether the file/directory currently exists.
        views_write_proc = _run_in_container(
            f"mkdir -p {module_dir}/views && printf '%s\\n' '<odoo></odoo>' > {views_path}",
            container=container,
        )
        if views_write_proc.returncode != 0:
            raise ScaffoldError(
                f"failed rewriting views/views.xml for {module_name!r} to an empty skeleton "
                f"after stripping scaffold boilerplate: {views_write_proc.stderr[-1000:]}"
            )
    return [path[len(prefix):] for path in to_remove]


# The exact, complete set Odoo 16's own ir.module.module.license
# Selection field accepts (confirmed directly against
# odoo/addons/base/models/ir_module.py) -- anything outside this set
# makes `Module.update_list()` crash with a hard ValueError while
# scanning EVERY module on the addons path, not just the one that
# wrote the bad value, which is what makes this failure mode so
# disproportionately damaging: one bad manifest anywhere breaks every
# task's sandbox/install until it's found and fixed.
_VALID_ODOO_LICENSES = frozenset({
    "GPL-2", "GPL-2 or any later version", "GPL-3", "GPL-3 or any later version",
    "AGPL-3", "LGPL-3", "Other OSI approved licence", "OEEL-1", "OPL-1",
    "Other proprietary",
})
_MANIFEST_LICENSE_RE = re.compile(r"""(['"])license\1\s*:\s*(['"])((?:(?!\2).)*)\2""")


def _repair_manifest_license_if_invalid(content: str, module_name: str) -> str:
    """Belt-and-suspenders guard, found live (2026-07-23): a real,
    reproducible crash (`ValueError: Wrong value for
    ir.module.module.license: 'LGPL- -3'`) kept recurring in production
    even after the Build specialist's own generation path was hardened
    to always emit a fixed, correct license value (`ManifestFields`/
    `_render_manifest_py()` in specialists/build/specialist.py) --
    exhaustive live investigation (checked every manifest on both the
    real dev target and the sandbox container via Odoo's own manifest
    loader, checked the sandbox golden template's `ir_module_module`
    table directly via SQL, confirmed the live production service was
    freshly restarted onto the hardened code) never located the exact
    origin, strongly suggesting a transient write/scan race on the
    shared addons directory rather than a bug reachable through this
    specific write call's own arguments. Rather than continue chasing
    an unreproducible-on-demand root cause, this closes the actual
    failure surface directly: write_module_file() is the ONE real
    choke point every manifest write already goes through (main
    target, sandbox, round 1, scoped edits -- all of it), so validating
    the license value here, right at the boundary, guarantees the
    failure class is now structurally impossible regardless of which
    upstream path produced the content. Deliberately narrow: only ever
    touches the `license` key's own value, only when it's not one of
    Odoo's own real Selection options, never touches anything else in
    the file.
    """
    if not content.strip().startswith(("{", "#")):
        return content  # not a manifest dict literal at all -- leave alone, let the real error surface
    match = _MANIFEST_LICENSE_RE.search(content)
    if match is None:
        return content  # no explicit license key -- Odoo's own default ('LGPL-3') applies, nothing to repair
    current = match.group(3)
    if current in _VALID_ODOO_LICENSES:
        return content
    import sys as _sys
    print(
        f"write_module_file({module_name!r}): manifest license {current!r} is not a real Odoo "
        f"license value -- repairing to 'LGPL-3' in place rather than letting it crash the "
        f"ENTIRE addons scan for every module, not just this one.",
        file=_sys.stderr,
    )
    start, end = match.span(3)
    return content[:start] + "LGPL-3" + content[end:]


def write_module_file(module_name: str, relative_path: str, content: str, container: str | None = None) -> None:
    """Writes `content` into `<module>/<relative_path>` inside
    /mnt/extra-addons -- the real authoring step the Build specialist
    uses after scaffold_module(). Content is base64-encoded before
    being sent over SSH specifically to avoid any shell-quoting
    corruption of the generated code (quotes, backticks, `$`, etc. are
    all fair game in real Python/XML source).
    """
    import base64

    if relative_path == "__manifest__.py":
        content = _repair_manifest_license_if_invalid(content, module_name)

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    full_path = f"{_MODULE_DEV_ADDONS_DIR}/{module_name}/{relative_path}"
    dir_path = full_path.rsplit("/", 1)[0]
    cmd = f"mkdir -p {dir_path} && echo {encoded} | base64 -d > {full_path}"
    proc = _run_in_container(cmd, container=container)
    if proc.returncode != 0:
        raise ScaffoldError(f"failed writing {relative_path!r} into {module_name!r}: {proc.stderr[-1000:]}")


def delete_module_file(module_name: str, relative_path: str, container: str | None = None) -> None:
    """Real, confirmed bug found live (2026-08-11, task 18fca388): every write path in this
    codebase (the Build specialist's own per-file writes, `_sandbox_preflight()`, and the
    concurrent multi-node merge path) only ever WRITES a file when the corresponding
    `GeneratedModuleFiles` field (e.g. `views_xml`) is truthy -- when a round's own content (via
    a full regeneration, or an autofix like `_autofix_strip_unrequested_views_xml_for_pure_
    behavior_task` setting the field back to `None`) means the file is no longer wanted, the
    write is simply SKIPPED, never explicitly deleted. A file genuinely written by an EARLIER
    round then survives on disk completely untouched, forever, no matter what any LATER round's
    own `generated` object says -- confirmed live: `views/views.xml` (with by-then-stale
    content) kept failing sandbox installs and Code-Review for 6+ consecutive rounds, each of
    which correctly, deterministically decided the file was no longer needed, because nothing
    ever actually removed it. `rm -f` (not `test -f && rm`) is intentionally idempotent --
    deleting an already-absent file is a normal, expected no-op here, never an error.
    """
    full_path = f"{_MODULE_DEV_ADDONS_DIR}/{module_name}/{relative_path}"
    proc = _run_in_container(f"rm -f {full_path}", container=container)
    if proc.returncode != 0:
        raise ScaffoldError(f"failed deleting {relative_path!r} from {module_name!r}: {proc.stderr[-1000:]}")


def lint_module(module_name: str, container: str | None = None) -> LintResult:
    """Runs pylint-odoo with --output-format=json so findings come back
    structured -- never a raw log dump the Build specialist has to
    parse itself. A non-empty findings list is an EXPECTED, normal
    result (e.g. a fresh scaffold legitimately fails several checks) --
    this function's job is only to run the tool and structure its
    output, not to judge pass/fail itself.
    """
    cmd = (
        f"export PATH=$PATH:{_LOCAL_BIN} && cd {_MODULE_DEV_ADDONS_DIR} && "
        f"pylint --rcfile=/dev/null --load-plugins=pylint_odoo -d all -e odoolint "
        f"--output-format=json {module_name}"
    )
    proc = _run_in_container(cmd, container=container)
    # pylint's own exit code is a bitmask of finding severities, not a
    # simple 0/1 -- a non-zero code here is EXPECTED whenever there are
    # findings, so it must never be treated as "the tool crashed."
    stdout = proc.stdout.strip()
    if not stdout:
        if proc.returncode == 0:
            return LintResult(module_name=module_name, ran_ok=True, findings=[])
        return LintResult(
            module_name=module_name,
            ran_ok=False,
            raw_error=f"pylint produced no output (rc={proc.returncode}): {proc.stderr[-2000:]}",
        )
    try:
        raw_findings = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return LintResult(
            module_name=module_name,
            ran_ok=False,
            raw_error=f"could not parse pylint JSON output ({exc}): {stdout[-2000:]!r}",
        )
    findings = [
        LintFinding(
            message_id=f.get("message-id", ""),
            symbol=f.get("symbol", ""),
            message=f.get("message", ""),
            path=f.get("path", ""),
            line=f.get("line", 0),
            column=f.get("column", 0),
            severity=f.get("type", ""),
        )
        for f in raw_findings
    ]
    return LintResult(module_name=module_name, ran_ok=True, findings=findings)


def security_lint_module(module_name: str, container: str | None = None) -> LintResult:
    """Phase 35 §10.1: a security-specific static analysis pass, distinct from lint_module()'s
    functional/style pass -- catches a different failure class (SQL injection, dangerous
    builtins, insecure deserialization) that functional Code-Review was never checking for.

    Runs Bandit (github.com/PyCQA/bandit, the standard, widely-used Python security scanner --
    real 2026 practice for this exact job, per Semgrep/Bandit/CodeQL being the named standard
    tools for AI-generated-code security scanning; installed 2026-08-12 the same way pylint-odoo
    already is, via `pip3 install --user` inside this same container) with --output-format=json,
    same structured-findings contract as lint_module() above, reusing the same LintFinding/
    LintResult shapes rather than inventing parallel types.

    Also does one small, Odoo-specific check Bandit has no way to know about: any `.sudo()` call
    in generated Python bypasses this system's own record-rule certification work (§1 of Phase 35)
    -- not necessarily wrong, but always worth a human's attention, so every occurrence is
    surfaced as its own finding rather than silently allowed through.
    """
    cmd = (
        f"export PATH=$PATH:{_LOCAL_BIN} && cd {_MODULE_DEV_ADDONS_DIR} && "
        f"bandit -r {module_name} -f json"
    )
    proc = _run_in_container(cmd, container=container)
    stdout = proc.stdout.strip()
    if not stdout:
        return LintResult(
            module_name=module_name, ran_ok=False,
            raw_error=f"bandit produced no output (rc={proc.returncode}): {proc.stderr[-2000:]}",
        )
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return LintResult(
            module_name=module_name, ran_ok=False,
            raw_error=f"could not parse bandit JSON output ({exc}): {stdout[-2000:]!r}",
        )
    if raw.get("errors"):
        return LintResult(
            module_name=module_name, ran_ok=False,
            raw_error=f"bandit reported errors: {raw['errors']}",
        )
    findings = [
        LintFinding(
            message_id=f.get("test_id", ""),
            symbol=f.get("test_name", ""),
            message=f.get("issue_text", ""),
            path=f.get("filename", ""),
            line=f.get("line_number", 0),
            column=0,
            severity=f.get("issue_severity", "").lower(),
        )
        for f in raw.get("results", [])
    ]

    # Odoo-specific: flag every .sudo() call in generated models.py, Bandit has no concept
    # of Odoo's own access-control model so this is never something it could catch itself.
    sudo_check_cmd = (
        f"grep -rn '\\.sudo(' {_MODULE_DEV_ADDONS_DIR}/{module_name}/models/*.py 2>/dev/null || true"
    )
    sudo_proc = _run_in_container(sudo_check_cmd, container=container)
    for line in sudo_proc.stdout.strip().splitlines():
        if ":" not in line:
            continue
        path, _, rest = line.partition(":")
        line_no, _, text = rest.partition(":")
        findings.append(LintFinding(
            message_id="odoo-unscoped-sudo",
            symbol="odoo-unscoped-sudo",
            message=f".sudo() call bypasses this model's own record rules -- confirm this is intentional: {text.strip()}",
            path=path,
            line=int(line_no) if line_no.strip().isdigit() else 0,
            column=0,
            severity="medium",
        ))

    return LintResult(module_name=module_name, ran_ok=True, findings=findings)


def get_model_fields(model_name: str, db: str) -> list[str] | None:
    """Real, confirmed recurring bug found live (Phase 18 dynamic
    verification pass, task 3, 3 consecutive rounds): Build repeatedly
    referenced a field (e.g. `project.project.product_ids`) that
    doesn't exist on that model at all -- a genuinely different mistake
    class than the earlier "referenced a field this SAME module never
    defines" bug (_validate_view_fields_exist_on_model), because here
    the model isn't newly defined by this module at all; it's an
    EXISTING core/other-module model, and there's no way to know its
    real field list from models_py alone. A plain grep across addon
    source files can't reliably answer this either -- a model's real
    fields can come from many different files via `_inherit`, exactly
    the kind of thing Odoo's own registry already resolves for real.
    So this queries the LIVE registry directly (same SSH/docker-exec
    channel as every other tool in this module) rather than guessing
    from static source text -- the one genuine source of truth for
    "does this model really have this field."

    Returns None (never an empty list) if the model doesn't exist, or
    the query itself failed for any reason (SSH hiccup, model typo,
    the model belonging to a not-yet-installed module) -- deliberately
    conservative, same posture as every other _validate_* check in this
    codebase: a check that can't get a real, confident answer skips
    silently rather than risking a false positive that blocks a
    genuinely correct attempt.
    """
    # Real, confirmed shell-quoting bug found live (same failure class
    # as find_module_defining_model()'s own first attempt): nesting a
    # Python-string-literal-bearing command through THREE independent
    # quoting layers (the python code's own quotes, the shell pipe's
    # quotes, and _run_in_container()'s own repr()-based escaping)
    # cannot be made to survive all three at once -- whichever quote
    # character is chosen for the middle layer collides with one of the
    # other two. Sidestepped entirely by base64-encoding the script:
    # base64 output contains only [A-Za-z0-9+/=], nothing any shell
    # layer ever needs to escape, so it survives all three layers
    # unmodified. Decoded back into a real Python string on the remote
    # side immediately before use.
    import base64

    python_code = (
        f'print(",".join(sorted(env["{model_name}"]._fields.keys()))) '
        f'if "{model_name}" in env.registry.models else print("MODEL_NOT_FOUND")'
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    # The one real output line is a bare comma-joined field list with no
    # spaces at all -- every other line in odoo-bin shell's own startup
    # chatter (INFO/WARNING log lines, timestamps) has spaces in it, so
    # this is a reliable, simple way to pick out the real answer from
    # the noise without needing a delimiter marker.
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line == "MODEL_NOT_FOUND":
            return None
        if line and " " not in line:
            return line.split(",")
    return None


def get_primary_form_view_xmlid(model_name: str, db: str) -> str | None:
    """Real, general fix (2026-07-15, the project owner): the single most-repeated
    failure across the Phase 20 Area-1 batch was Build's own free-form
    views_xml generation reaching for Odoo scaffold's own stock demo
    field names ('value'/'value2') instead of the real field(s) this
    round actually added -- confirmed a documented, named LLM failure
    mode ("non-progress loop": self-repair concentrates its real gains
    in the first 1-2 rounds, further identical retries rarely converge).
    Inserting an already-known field into an existing model's form is a
    purely mechanical transformation, not something that needs freeform
    generation at all -- so specialists/build/specialist.py's
    deterministic view builder uses THIS function to find the model's
    real, live base form view (the root view with no inherit_id of its
    own) and its real external id, then constructs the insertion XML in
    Python, never asking an LLM to invent it.

    Same discipline as get_model_fields()/get_relation_fields() above:
    queries the LIVE registry (the only real source of truth for which
    view is a model's actual base form), returns None -- never guesses
    or raises -- on anything uncertain (SSH hiccup, timeout, a model
    with genuinely no form view at all) so a caller can safely fall
    back to the existing LLM-generation path rather than risk building
    XML against a wrong or nonexistent view.

    Superseded its own first version same day (2026-07-15): originally
    hand-rolled a heuristic (search inherit_id=False, prefer an exact
    "<model>.form" name match) -- worked for 11 of 12 real batch models,
    but wrongly returned "ambiguous" for product.product, whose real
    primary form (product.product_normal_form_view, confirmed via
    Odoo's own UI action) is itself an INHERITING view, not a root one,
    so the inherit_id=False filter excluded the correct answer before
    the name-match step ever got a chance to find it. Rather than patch
    that heuristic further, this now calls
    `env[model].get_views([(False, 'form')])` -- Odoo's own real,
    official view-resolution method (the same one the actual web client
    uses to decide which form to show when none is explicitly
    requested) -- so it always matches Odoo's own answer instead of a
    second, independently-guessed one that can disagree with it.
    """
    return _get_primary_view_xmlid(model_name, db, "form")


def get_primary_tree_view_xmlid(model_name: str, db: str) -> str | None:
    """Sibling of get_primary_form_view_xmlid() above, for the tree/list
    view (2026-07-23, SITE fix-pass audit): the deterministic view
    builder this feeds (specialists/build/specialist.py's
    build_deterministic_view_xml()) only ever covered the form-view
    insertion shape ("show this new field on the form") -- a task
    asking for a field on the LIST view instead (a real, common shape:
    tonight's own Task 003 explicitly asked for a badge-widget column
    on the sale.order tree) still went through pure free-form LLM
    generation with no deterministic path at all. Same discipline,
    same safety contract as the form version: queries the live
    registry's own real answer via get_views(), returns None on any
    uncertainty so the caller always has the proven LLM-generation
    fallback to rely on.
    """
    return _get_primary_view_xmlid(model_name, db, "tree")


def _get_primary_view_xmlid(model_name: str, db: str, view_type: str) -> str | None:
    """Shared implementation behind get_primary_form_view_xmlid() and
    get_primary_tree_view_xmlid() -- Odoo's own get_views() API takes
    the view type as a plain string ('form', 'tree', 'kanban',
    'search', ...), so the exact same real, official resolution
    mechanism generalizes to any of them without any further change;
    only the two callers above are kept as named, specific functions
    since those are the two shapes build_deterministic_view_xml()
    currently knows how to insert into.
    """
    import base64

    python_code = (
        f"res = env['{model_name}'].get_views([(False, {view_type!r})], {{}})\n"
        f"view_id = res.get('views', {{}}).get({view_type!r}, {{}}).get('id')\n"
        f"if not view_id:\n"
        f"    print('AMBIGUOUS_OR_NONE')\n"
        f"else:\n"
        f"    v = env['ir.ui.view'].browse(view_id)\n"
        f"    xmlid = v.get_external_id().get(v.id)\n"
        f"    print(xmlid if xmlid else 'NO_XMLID')\n"
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line in ("AMBIGUOUS_OR_NONE", "NO_XMLID"):
            return None
        if line and " " not in line and "." in line:
            return line
    return None


def get_relation_fields(model_name: str, db: str) -> dict[str, str] | None:
    """The other real gap get_model_fields() alone leaves open (Phase
    18, task 3, round found live): Build's own generated code accessed
    `self.project_id.product_ids` where `project_id` was never
    redefined in THIS module at all -- it's correctly INHERITED from a
    prior task's own module (service.record already has it). The
    Build-side validator that catches invented dotted-field access
    (_validate_no_invented_related_fields) only ever knew about
    Many2one/One2many/Many2many fields THIS module's own models_py
    explicitly defines -- an inherited relation field it never
    redefines was invisible to it, letting the exact same
    `product_ids`-on-`project.project` mistake slip through untouched
    when accessed via an INHERITED field instead of a newly-defined
    one.

    Returns {field_name: target_model} for every real
    Many2one/One2many/Many2many field on `model_name`, straight from
    the live registry -- including ones inherited from a parent model,
    not just ones that model's own code happens to define directly.
    Used to extend the same validator's own relation-tracking to also
    cover a module's `_inherit` target, not just its own new fields.
    Returns None (never a false/empty answer) on any uncertainty, same
    conservative posture as every other tool here.
    """
    import base64

    python_code = (
        "out = []\n"
        f'if "{model_name}" in env.registry.models:\n'
        f'    for fname, f in env["{model_name}"]._fields.items():\n'
        "        comodel = getattr(f, 'comodel_name', None)\n"
        "        if comodel:\n"
        "            out.append(fname + '|' + comodel)\n"
        'print(",".join(out)) if out else print("NO_RELATIONS")'
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line == "NO_RELATIONS":
            return None
        if line and " " not in line and "|" in line:
            result = {}
            for entry in line.split(","):
                if "|" in entry:
                    fname, comodel = entry.split("|", 1)
                    result[fname] = comodel
            return result or None
    return None


def resolve_xmlids_exist(xmlids: list[str], db: str) -> dict[str, bool] | None:
    """Real, general fix (2026-07-15, Phase 20 Area 2, §24.14.8): the
    first genuinely new failure class this project's XML-generating
    tasks (security_xml/views_xml records referencing an EXISTING
    Odoo object by external id -- a menu, an action, a group) hit that
    none of the existing invented-name checks cover. Confirmed live:
    Build's generated views_xml referenced 'stock.action_stock_
    inventory_form', a plausible-sounding but entirely nonexistent
    external id (env.ref() raised ValueError: External ID not found
    in the system) -- Odoo's own data loader then crashes at install
    time (rc=255) rather than surfacing a clear error anywhere earlier.
    get_model_fields()/list_custom_models() only ever check MODEL
    names and FIELD names; nothing previously checked whether a `ref=`
    attribute inside generated XML actually resolves to a real record
    at all.

    Same discipline as every other tool here: batches all xmlids into
    ONE shell call rather than one per id, queries the LIVE registry
    (the only real source of truth), and returns None -- never a
    false/empty answer -- on any uncertainty (SSH hiccup, timeout) so
    a caller can safely skip the check rather than risk a false
    rejection. Returns {xmlid: True/False} for every xmlid asked
    about, using env.ref(..., raise_if_not_found=False) exactly like
    Odoo's own XML data loader resolves a `ref=` attribute internally,
    so a False here means the real install would genuinely fail too.
    """
    import base64

    if not xmlids:
        return {}
    xmlid_list_literal = repr(xmlids)
    python_code = (
        f"xmlids = {xmlid_list_literal}\n"
        "out = []\n"
        "for xmlid in xmlids:\n"
        "    try:\n"
        "        rec = env.ref(xmlid, raise_if_not_found=False)\n"
        "        out.append(xmlid + '|' + ('1' if rec else '0'))\n"
        "    except Exception:\n"
        "        out.append(xmlid + '|0')\n"
        "print(','.join(out))"
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line and "|" in line:
            result = {}
            for entry in line.split(","):
                if "|" in entry:
                    xmlid, found = entry.rsplit("|", 1)
                    result[xmlid] = found == "1"
            return result if len(result) == len(xmlids) else None
    return None


def resolve_owning_modules(
    bare_xmlids: list[str], db: str, own_module_name: str | None = None,
    depends_on_module: str | None = None,
) -> dict[str, str | None] | None:
    """Real, general fix (2026-07-15, Phase 20 Area 2, §24.14.8): a
    security_csv row's `model_id:id` column (e.g. `model_project_task`)
    is a BARE xmlid with no `module.` prefix -- unlike an XML
    `<record id="module.name">`, the CSV format's own external-id
    convention never states which module owns it. Confirmed live via a
    direct install reproduction: Odoo's own CSV loader raises "No
    matching record found for external id 'model_project_task'" when
    the owning module ('project') isn't declared in this module's own
    `depends` -- the exact same real requirement
    resolve_xmlids_exist()/the XML-record case already covers, just
    for a bare id with no dot to read the module from directly.

    Same discipline as every other tool here: one batched shell call,
    returns None (never a false/empty answer) on any uncertainty.
    Returns {bare_xmlid: owning_module_name_or_None} -- None for a
    given id means it genuinely has no `ir_model_data` row (so it's
    not a valid model reference at all, a separate problem
    resolve_xmlids_exist()-style checks would catch).

    Real, general fix found live (2026-07-16, Phase 20 Area 2), via a
    fresh reproduction AFTER the stripper/adder ordering fix landed:
    `ir_model_data` for 'model_project_task' had 26 rows, 16 of them
    owned by our OWN past 'oma_*' generated modules -- confirmed this
    is real, ongoing environment contamination, not a one-off: Odoo's
    CSV loader silently CREATES a new `ir_model_data` row attributing a
    bare `model_id:id` reference to whichever module is CURRENTLY
    installing when no existing row is found for that bare name yet --
    so every one of our own past broken tasks that referenced
    'model_project_task' without 'project' in its own `depends`
    permanently poisoned this table with a bogus self-owned row. The
    original query had no WHERE exclusion and no deterministic
    ordering, so `dict(fetchall())`'s last-write-wins semantics could
    (and did, confirmed live) resolve to one of these poisoned
    'oma_*' rows instead of the real owning module -- silently adding
    NO real dependency (since the CSV-ref autofix skips a match against
    this module's OWN new models, but a match against a DIFFERENT past
    oma_* module isn't skipped and isn't useful either) and reproducing
    the exact generic 'Sandbox install failed rc=255' failure. Fixed by
    excluding any 'oma_*'-prefixed module from candidacy (our own
    generated modules can never be a legitimate xmlid owner to depend
    on) and preferring the EARLIEST-registered real row (lowest id) --
    the genuine original definer was installed long before any
    generated task ever ran.

    Extended (2026-07-20, live on #44, Phase 20 Area 2 pass 11): the
    blanket 'oma_*' exclusion above was too broad for a DECOMPOSED
    task's own later constraint referencing a model an EARLIER
    constraint of the SAME task already created (e.g. constraint 2's
    `ref="model_asset_registry"` after constraint 1 created
    `asset.registry` in the SAME, reused module) -- that IS a
    genuinely real, legitimate self-reference, not the contamination
    this exclusion was built to catch, but the blanket exclusion
    treated it identically and returned None (never resolving it),
    confirmed live as the direct cause of #44 failing 5/5 rounds
    identically even with fix 8 (the bare-local-ref autofix, which only
    covers refs to records defined VIA `<record>` in the same
    generation -- an auto-generated model xmlid like
    `model_asset_registry` isn't written that way, so only THIS
    DB-lookup path could ever resolve it). `own_module_name`, when
    given, is exempted from the exclusion so the caller's own current
    module is always a valid candidate -- every OTHER oma_* module
    remains excluded exactly as before.
    """
    import base64

    if not bare_xmlids:
        return {}
    xmlid_list_literal = repr(bare_xmlids)
    own_module_literal = repr(own_module_name) if own_module_name else "None"
    depends_on_module_literal = repr(depends_on_module) if depends_on_module else "None"
    python_code = (
        f"names = {xmlid_list_literal}\n"
        f"own_module = {own_module_literal}\n"
        f"depends_on_module = {depends_on_module_literal}\n"
        "env.cr.execute(\n"
        "    \"SELECT name, module FROM ir_model_data WHERE model = 'ir.model' AND name = ANY(%s) \"\n"
        "    \"AND (module NOT LIKE 'oma\\_%%' ESCAPE '\\\\' OR module = %s OR module = %s) ORDER BY id ASC\",\n"
        "    (names, own_module, depends_on_module),\n"
        ")\n"
        "found = {}\n"
        "for _n, _m in env.cr.fetchall():\n"
        "    found.setdefault(_n, _m)\n"
        "out = [f'{n}|{found.get(n, \"\")}' for n in names]\n"
        "print(','.join(out))"
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line and "|" in line:
            result: dict[str, str | None] = {}
            for entry in line.split(","):
                if "|" in entry:
                    name, module = entry.split("|", 1)
                    result[name] = module if module else None
            return result if len(result) == len(bare_xmlids) else None
    return None


def resolve_model_names_from_xmlids(
    bare_names: list[str], db: str, own_module_name: str | None = None, depends_on_module: str | None = None,
) -> dict[str, str | None] | None:
    """Real, general fix (2026-07-17, Phase 20 Area 2): resolves a bare
    `model_XXX` xmlid (e.g. a security_xml `<record model="ir.rule">`'s
    own `model_id` ref) straight to its REAL dotted model name (e.g.
    'res.partner') via a live `ir_model_data`/`ir_model` join, so a
    validator checking whether a domain_force field reference is real
    doesn't have to guess the dotted name back from the underscored
    xmlid form (ambiguous in general -- e.g. 'model_stock_move_line'
    could theoretically split several ways without a live lookup).

    Same contamination-safety discipline as resolve_owning_modules()
    (2026-07-16 fix): excludes 'oma_*'-prefixed rows and orders by
    `id ASC` so a past broken generated module's own self-poisoned
    ir_model_data row (see resolve_owning_modules()'s own docstring for
    the full story) can never be picked over the real, original one.

    Real, confirmed bug found live (2026-08-10, task e65381cc): same fix as
    `resolve_owning_modules()`'s own `depends_on_module` exemption -- an `ir.rule` referencing one
    of THIS task's own real `depends_on_module:` models (routinely `oma_*`-prefixed since Bug 40)
    would otherwise be permanently unresolvable. `own_module_name`/`depends_on_module`, when
    given, are exempted from the exclusion the same way `resolve_owning_modules()` already does.
    """
    import base64

    if not bare_names:
        return {}
    names_literal = repr(bare_names)
    own_module_literal = repr(own_module_name) if own_module_name else "None"
    depends_on_module_literal = repr(depends_on_module) if depends_on_module else "None"
    python_code = (
        f"names = {names_literal}\n"
        f"own_module = {own_module_literal}\n"
        f"depends_on_module = {depends_on_module_literal}\n"
        "env.cr.execute(\n"
        "    \"SELECT d.name, m.model FROM ir_model_data d JOIN ir_model m ON m.id = d.res_id \"\n"
        "    \"WHERE d.model = 'ir.model' AND d.name = ANY(%s) \"\n"
        "    \"AND (d.module NOT LIKE 'oma\\_%%' ESCAPE '\\\\' OR d.module = %s OR d.module = %s) ORDER BY d.id ASC\",\n"
        "    (names, own_module, depends_on_module),\n"
        ")\n"
        "found = {}\n"
        "for _n, _m in env.cr.fetchall():\n"
        "    found.setdefault(_n, _m)\n"
        "out = [f'{n}|{found.get(n, \"\")}' for n in names]\n"
        "print(','.join(out))"
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line and "|" in line:
            result: dict[str, str | None] = {}
            for entry in line.split(","):
                if "|" in entry:
                    name, model = entry.split("|", 1)
                    result[name] = model if model else None
            return result if len(result) == len(bare_names) else None
    return None


def list_custom_models(db: str) -> list[tuple[str, str]] | None:
    """The other half of the fix for the SAME real bug get_model_fields()
    targets: Build doesn't just invent nonexistent FIELDS on real
    models, it sometimes invents the MODEL NAME itself -- confirmed live
    (Phase 18, task 3): a task explicitly about extending "the service
    module" (a prior task's own real model, service.record) kept
    writing `_inherit = 'oma.service'`, a plausible-sounding name that
    was never real, instead of the actual model. Static analysis of
    models_py can't catch this (there's nothing wrong with the syntax,
    the referenced model just doesn't exist), and the existing
    depends_on_module mechanism only helps when the Manager already
    knows which prior module to point at -- it does nothing when Build
    itself is the one guessing wrong.

    This lists every real model any of OUR OWN scaffolded modules
    (oma_* by construction, see specialists.build.specialist.
    slugify_module_name) actually defines, straight from the live
    registry's own ir.model.data -- the durable record of which module
    owns which model, not a guess from folder names. Used to turn "that
    model doesn't exist" into "that model doesn't exist -- did you mean
    one of these real ones instead," giving a concrete, correct answer
    in the SAME round rather than requiring several more rounds of
    blind guessing.

    Returns None (never an empty list) on any uncertainty -- same
    conservative posture as get_model_fields().
    """
    import base64

    python_code = (
        # search() already returns a real recordset -- browse()-ing it
        # AGAIN (a real bug caught here directly, via a live test that
        # failed with "can't adapt type 'ir.model.data'") wraps each
        # record inside another spurious recordset layer instead of
        # iterating actual rows.
        #
        # A second real bug caught here directly (unit test false
        # positive): a module that only EXTENDS an existing model
        # (`_inherit = 'res.partner'`, e.g. task 1's own preferred_
        # language module) still gets its own ir.model.data row for
        # that model's ir.model record -- Odoo tracks per-module model
        # metadata even for pure extensions, not just genuinely new
        # models. Naively including every oma_* module's own ir.model
        # rows wrongly reported 'res.partner' as "owned by" a task-1
        # module, which would have told Build to depend on the WRONG
        # thing for a model it should just treat as always-available
        # core. Filtered here: only keep a model if NO non-oma_ module
        # also has an ir.model.data row for it -- that's the real,
        # reliable signal that WE are the one and only real definer of
        # this model, not just one of many modules that happen to touch
        # it.
        'recs = env["ir.model.data"].search([("module", "like", "oma_"), ("model", "=", "ir.model")])\n'
        "out = []\n"
        "for r in recs:\n"
        "    m = env['ir.model'].browse(r.res_id)\n"
        "    if not m.exists():\n"
        "        continue\n"
        "    other_owner = env['ir.model.data'].search_count([('model', '=', 'ir.model'), "
        "('res_id', '=', r.res_id), ('module', 'not like', 'oma_')])\n"
        "    if other_owner == 0:\n"
        "        out.append(m.model + '|' + r.module)\n"
        'print(",".join(out)) if out else print("NO_CUSTOM_MODELS")'
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line == "NO_CUSTOM_MODELS":
            return None
        if line and " " not in line and "|" in line:
            pairs = []
            for entry in line.split(","):
                if "|" in entry:
                    model_name, module_name = entry.split("|", 1)
                    pairs.append((model_name, module_name))
            return pairs or None
    return None


_ERROR_MARKER_RE = re.compile(r"Traceback \(most recent call last\)|CRITICAL")
_BENIGN_HTTPD_PORT_BIND_EXCEPTION_RE = re.compile(
    r"http_thread.{0,1500}Address already in use", re.DOTALL,
)


def _extract_error_excerpt(combined: str, window: int = 4000) -> str:
    """Real, confirmed bug found live (2026-07-15, chasing what looked
    like a mysterious, unexplained "Install failed (rc=0)" every round
    of a real, repeatedly-failing task): every InstallResult.log_tail
    below used to be a blind `combined[-2000:]` -- fine for a short
    install, but a real install pulling in a long dependency chain
    (e.g. anything depending on 'hr') routinely produces tens of
    thousands of characters of module-loading log, with the actual
    Traceback/CRITICAL marker buried in the MIDDLE, not the end. The
    tail-only slice silently discarded the one thing that would have
    explained the failure, every single time -- Build (and Operator) only
    ever saw a content-free "see log_tail for detail" with no real
    detail in it. Prefer a window around the FIRST real error marker,
    since that's almost always the actual root cause; later output is
    usually just the process unwinding/shutting down after it already
    failed. Falls back to the tail (the old behavior) if no marker is
    found at all, so a truly marker-less failure still gets something.
    """
    # Real, confirmed bug found live (2026-07-29, school_student task,
    # automated_tests round -- 4+ straight identical, content-free
    # reports): the sandbox container's own `Exception in thread odoo.
    # service.httpd: ... OSError: [Errno 98] Address already in use`
    # is a KNOWN-BENIGN, already-recovered-from exception (confirmed
    # live: module loading continues normally immediately afterward,
    # every single time) -- but it's ALSO a real `Traceback (most
    # recent call last)` block, so it kept winning the "first marker"
    # search above, anchoring the window on pure noise and pushing the
    # ACTUAL failure (further down in the log) out of the fixed-size
    # window every time. Skips any marker whose surrounding text
    # matches this specific known-benign shape and keeps searching for
    # the NEXT real marker instead -- general on purpose (matches the
    # exception TYPE/shape, not this one task's own content), and safe:
    # a marker that ISN'T this exact benign shape is never skipped.
    search_from = 0
    while True:
        match = _ERROR_MARKER_RE.search(combined, search_from)
        if not match:
            return combined[-window:]
        surrounding = combined[match.start():match.start() + 1600]
        if _BENIGN_HTTPD_PORT_BIND_EXCEPTION_RE.search(surrounding):
            search_from = match.end()
            continue
        start = max(0, match.start() - 200)
        return combined[start:start + window]


def _find_missing_manifest_data_files(module_name: str, container: str | None = None) -> list[str] | None:
    """Real, general fix (2026-07-20, Phase 20 Area 2 UPDATE 20): a
    mandatory pre-install consistency gate, the same role Terraform's
    own `validate` step plays before `apply` -- confirms every file
    the module's own __manifest__.py `data` list declares ACTUALLY
    exists on disk, independent of which generation path (full
    regeneration or a scoped/incremental patch) produced the current
    file set. Root-caused live (task #54, 2026-07-20): a scoped-edit
    round patched from a stale in-memory/DB "prior files" baseline,
    leaving `__manifest__.py` still referencing 'security/security.xml'
    while that file itself was never actually re-written to the
    container this round -- Odoo's own install crashed with a raw
    FileNotFoundError deep in odoo.tools.convert.convert_file(),
    instead of surfacing clearly before ever touching the database.

    Runs directly against the container's real filesystem (same
    SSH/docker-exec channel as every other check in this file) --
    never trusts any in-memory/DB model of "what should be there."
    Returns None (never an empty list) if the manifest itself can't be
    read or parsed at all (conservative, same posture as every other
    query helper here -- a check that can't get a confident answer
    skips rather than risking a false positive). Returns an empty list
    when every declared file is genuinely present.
    """
    import ast

    module_dir = f"{_MODULE_DEV_ADDONS_DIR}/{module_name}"
    manifest_proc = _run_in_container(f"cat {module_dir}/__manifest__.py", timeout=30, container=container)
    if manifest_proc.returncode != 0 or not manifest_proc.stdout.strip():
        return None
    try:
        parsed = ast.literal_eval(manifest_proc.stdout)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(parsed, dict):
        return None
    data_list = parsed.get("data")
    if not isinstance(data_list, list) or not data_list:
        return []
    quoted_paths = " ".join(shlex.quote(f"{module_dir}/{path}") for path in data_list if isinstance(path, str))
    if not quoted_paths:
        return []
    check_cmd = (
        f"for f in {quoted_paths}; do test -f \"$f\" || echo \"MISSING:$f\"; done"
    )
    check_proc = _run_in_container(check_cmd, timeout=30, container=container)
    if check_proc.returncode != 0:
        return None
    missing = [
        line[len("MISSING:") + len(module_dir) + 1:]
        for line in check_proc.stdout.splitlines()
        if line.startswith(f"MISSING:{module_dir}/")
    ]
    return missing


def _self_heal_stale_templates_xml_manifest_refs(container: str | None = None) -> list[str]:
    """Real, root-caused fix (2026-08-14): `_render_manifest_py()` was
    already fixed (Phase 35 overnight session) to never write a fresh
    manifest referencing 'views/templates.xml' (odoo-bin scaffold's own
    stock file, always deleted by strip_scaffold_boilerplate()). But a
    direct scan found 608 PRE-EXISTING modules across the whole
    multi-day session still carrying this stale reference from before
    that fix landed. Odoo's registry-wide reload during ANY install can
    sweep one of these already-broken modules in via dependency chains,
    regardless of the current task's own clean content -- so a one-time
    manual bulk edit of 608 files doesn't actually close the hole (any
    module generated tomorrow with the same bug would just add to the
    backlog again).

    This runs the identical, already dry-run-verified strip logic
    (`ast.literal_eval` the manifest, drop any 'views/*.xml' data entry
    that isn't the real 'views/views.xml', re-`repr()` and write back)
    as a small, self-healing PRE-INSTALL step instead: a normal,
    reviewable code change that fixes the existing backlog as a side
    effect on first use and prevents any future recurrence of this
    exact class of bug, rather than a one-off bulk data mutation.
    Deliberately narrow -- touches ONLY the one known-bad literal
    pattern, nothing else in any manifest. Best-effort: any scan/parse
    failure is swallowed and skipped (same conservative,
    never-block-on-an-inconclusive-result posture as every other
    pre-install check in this file). Returns the list of module names
    actually fixed this call (empty list on a clean scan or on any
    failure to run the scan at all).
    """
    import base64

    script = (
        "import ast, glob\n"
        "fixed = []\n"
        f"for path in glob.glob({_MODULE_DEV_ADDONS_DIR!r} + '/*/__manifest__.py'):\n"
        "    try:\n"
        "        with open(path) as f: content = f.read()\n"
        "        manifest = ast.literal_eval(content)\n"
        "    except Exception:\n"
        "        continue\n"
        "    if not isinstance(manifest, dict):\n"
        "        continue\n"
        "    data = manifest.get('data')\n"
        "    if not isinstance(data, list):\n"
        "        continue\n"
        "    bad = [d for d in data if isinstance(d, str) and d.startswith('views/')"
        " and d.endswith('.xml') and d != 'views/views.xml']\n"
        "    if not bad:\n"
        "        continue\n"
        "    manifest['data'] = [d for d in data if d not in bad]\n"
        "    try:\n"
        "        with open(path, 'w') as f: f.write(repr(manifest))\n"
        "    except Exception:\n"
        "        continue\n"
        "    fixed.append(path)\n"
        "print('\\n'.join(fixed))\n"
    )
    encoded = base64.b64encode(script.encode()).decode()
    try:
        proc = _run_in_container(
            f"echo {encoded} | base64 -d | python3", timeout=90, container=container,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


_WARM_WORKER_PORT = 8073


def _install_module_via_warm_worker(module_name: str, db: str) -> InstallResult | None:
    """Phase 21 (2026-07-22): the real fix for the ~30-46s/install cost
    confirmed live to be dominated by Postgres/registry-loading work a
    fresh `odoo-bin -i` process pays on EVERY invocation (isolated via
    direct measurement: `odoo-bin --version` alone, no DB at all, took
    2.92s -- the remaining ~27-30s is registry loading, not addon-path
    scanning or Python import time). A SEPARATE, dedicated, always-on
    Odoo process (port 8073, workers=0, `oma_install_worker_wrapper.sh`,
    self-restarting via a shell loop) stays registry-warm against the
    SAME odoo16_dev database the live web server also uses -- installing
    a real, trivial single-field module through it (button_immediate_install
    via XML-RPC) measured 5.51s, confirmed correct, live.

    Safety, confirmed live before this was ever pointed at the real
    target: two independent Odoo processes against the same database
    stay in sync WITHOUT a restart (Odoo's own registry-signaling
    mechanism, the same one that lets a normal multi-worker deployment's
    HTTP workers and cron worker share one database) -- validated on a
    disposable sandbox database (create a field via one process, an
    entirely separate, never-restarted second process saw it
    immediately). This worker process is architecturally the same
    pattern Odoo itself uses to separate cron workers from HTTP workers
    -- it never touches or restarts the live, Operator-facing web server.

    Reachable only via the SSH/docker-exec channel (no host port is
    exposed for 8073, deliberately -- this worker is internal-only,
    never meant to be reachable from outside the container at all).
    Returns None (never raises) on ANY failure -- worker not running,
    JIT key auth failure, XML-RPC error -- so install_module() can
    always fall back to the proven, if slower, subprocess path rather
    than ever risk a false failure report for something this
    optimization doesn't control.
    """
    import base64
    import json

    from tools_odoo.odoo_schema_client import _get_or_create_shared_key

    try:
        # Reuses the SAME (uid, key) cache the metadata-read fast path
        # (odoo_schema_client.py) already maintains -- a real bug caught
        # before this ever ran: an earlier draft hand-rolled its own
        # key-creation here with a placeholder (None, key) cache entry,
        # which would have silently corrupted _shared_key_cache for
        # every OTHER caller of _get_or_create_shared_key (they'd read
        # back uid=None and fail every subsequent metadata query). The
        # uid/key pair is a property of the Admin user in this
        # database, not tied to which port/process authenticates it --
        # safe to reuse verbatim against this different (warm-worker)
        # port, no separate cache needed.
        uid, api_key = _get_or_create_shared_key(db, "Admin")

        python_code = f"""\
import time, xmlrpc.client, json
db = {db!r}
uid = {uid!r}
models = xmlrpc.client.ServerProxy('http://127.0.0.1:{_WARM_WORKER_PORT}/xmlrpc/2/object')
models.execute_kw(db, uid, {api_key!r}, 'ir.module.module', 'update_list', [])
mod_ids = models.execute_kw(db, uid, {api_key!r}, 'ir.module.module', 'search', [[('name', '=', {module_name!r})]])
if not mod_ids:
    print('WARM_INSTALL_MODULE_NOT_FOUND')
else:
    # Real, confirmed transient condition found live (2026-07-22):
    # "Odoo is currently processing a scheduled action. Module
    # operations are not possible at this time" -- a genuine,
    # database-wide lock Odoo itself takes while ANY process's cron
    # runs (the live web server's own cron, not this worker's --
    # --max-cron-threads=0 on this worker's own launch didn't
    # eliminate it, confirmed live, because the lock isn't scoped to
    # this worker). Odoo's own error message says "try again later" --
    # a short, bounded retry is the correct response, not treating it
    # as a hard failure requiring the slow subprocess fallback.
    last_exc = None
    for attempt in range(3):
        try:
            models.execute_kw(db, uid, {api_key!r}, 'ir.module.module', 'button_immediate_install', [mod_ids])
            state = models.execute_kw(db, uid, {api_key!r}, 'ir.module.module', 'read', [mod_ids, ['state']])
            # Real, confirmed bug found live (2026-08-14, overnight
            # direction-certification run): the SAME real, database-wide
            # cron lock documented above can also surface here without
            # raising an exception at all -- button_immediate_install()
            # returns cleanly, but the actual install work is deferred
            # behind the lock, so an immediate state read catches the
            # module still 'to install' (or 'to upgrade'), a genuine,
            # real transitional state, not a hard failure. Confirmed
            # live: a module that legitimately reached 'installed' a
            # few seconds later got reported as failed here purely from
            # reading too early. A short, bounded poll for exactly these
            # known-transitional states (never for a genuinely terminal
            # state like 'uninstalled') before giving up, same retry
            # budget/backoff as the exception-based retry above.
            for poll_attempt in range(3):
                cur_state = state[0].get('state') if state else None
                if cur_state not in ('to install', 'to upgrade'):
                    break
                time.sleep(5)
                state = models.execute_kw(db, uid, {api_key!r}, 'ir.module.module', 'read', [mod_ids, ['state']])
            print('WARM_INSTALL_RESULT:' + json.dumps(state))
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if 'processing a scheduled action' in str(exc) and attempt < 2:
                time.sleep(5)
                continue
            break
    if last_exc is not None:
        # Real, confirmed bug found live (2026-07-22): a genuinely
        # FAILING install (not an infra/auth problem) raised here
        # uncaught, producing no parseable marker at all -- the outer
        # code then fell through to the full slow subprocess install
        # a SECOND time just to learn the SAME failure, roughly
        # doubling the cost of every failing round (confirmed live:
        # 53.9s for a failed install, vs 13.4s for a successful one
        # the same session). Caught and reported directly instead --
        # the fast path can report a real failure just as well as a
        # real success, no slow-path re-attempt needed either way.
        print('WARM_INSTALL_ERROR:' + str(last_exc)[:4000])
"""
        encoded = base64.b64encode(python_code.encode()).decode()
        proc = _run_in_container(f"echo {encoded} | base64 -d | python3", timeout=90)
        combined = (proc.stdout or "") + (proc.stderr or "")
        if "WARM_INSTALL_RESULT:" in combined:
            state_json = combined.split("WARM_INSTALL_RESULT:", 1)[1].strip().splitlines()[0]
            state = json.loads(state_json)
            if state and state[0].get("state") == "installed":
                return InstallResult(module_name=module_name, db=db, success=True, message="Install completed (warm worker).")
            return InstallResult(
                module_name=module_name, db=db, success=False, error_kind="state_verification_failed",
                message=f"Warm-worker install did not reach 'installed' state: {state}",
            )
        if "WARM_INSTALL_ERROR:" in combined:
            error_text = combined.split("WARM_INSTALL_ERROR:", 1)[1].strip()
            # Real, confirmed bug found live (2026-07-28, Phase 28C,
            # school_student task, security_groups round): a genuine,
            # empirically-proven false negative specific to this long-
            # lived worker process -- "Foutieve modelnaam 'X' in actie
            # definitie" (Odoo's own "invalid model name in action
            # definition" ParseError, Dutch locale) fired here for a
            # model+menu+action install, while the EXACT SAME generated
            # files, installed moments later via a completely fresh
            # `odoo-bin -i` process against the SAME database, succeeded
            # cleanly (confirmed directly: model_id row created, module
            # state 'installed'). This worker's own long-lived registry
            # can apparently get into a stale/incomplete state relative
            # to a model that was only just created moments earlier in
            # the SAME database by an EARLIER round -- a fresh process
            # never has this problem because it loads the registry from
            # scratch every time. Unlike a genuine content bug (which a
            # fresh process would reproduce identically), this is a
            # worker-process-specific artifact -- exactly the class the
            # module's own docstring already promises to fall back for
            # ("worker not running, JIT key auth failure, XML-RPC error"),
            # just a signature that wasn't recognized as such before.
            # Returning None here (the SAME fallback-to-slow-path
            # signal every other genuine infra hiccup above already
            # uses) is the correct, honest response, not a shortcut --
            # it defers to the SAME real, proven install path that just
            # empirically demonstrated the real content is fine.
            # Broadened (2026-07-29, same task, security_groups round):
            # the identical class of false negative recurred with a
            # DIFFERENT Dutch phrasing -- "Geen overeenkomende records
            # gevonden voor Externe id ... in veld 'Group'" ("No
            # matching records found for External id ... in field
            # 'Group'"), this time for a security_csv `group_id:id`
            # reference instead of an action's `res_model`. Confirmed
            # empirically the SAME way: the exact same on-disk content,
            # installed moments later via a completely fresh `odoo-bin
            # -u` process against the SAME database, succeeded cleanly
            # (Registry loaded in 8.377s, zero errors). Matching only
            # on the specific wording is too fragile against locale/
            # rewording (already proven twice) -- the real, robust
            # signal is Odoo's own generic "no matching record for an
            # external id" shape, checked in both English and the
            # Dutch phrasing this deployment's own locale has now
            # produced twice, rather than one more one-off string.
            # Broadened again (2026-07-29, school_student task,
            # computed_age_field round, resumed after the security_xml
            # and model-xmlid fixes above): a THIRD phrasing of the
            # same class -- "Veld 'age' bestaat niet in model
            # 'school.student'" (Dutch: "Field 'age' does not exist in
            # model 'school.student'") -- fired for a field (`age`)
            # that IS genuinely present in the committed models.py for
            # this exact commit (confirmed directly by reading the
            # real committed content from Gitea), during view-parsing
            # of a data file that references it. Same root cause as
            # the two signatures above: the warm worker's long-lived
            # registry had not yet picked up a model field only just
            # added moments earlier in the same database by this same
            # round. Matched generically (any field name, any model
            # name) rather than hardcoding 'age'/'school.student' --
            # this is a structural Odoo ParseError shape, not specific
            # to this one task.
            _stale_registry_signatures = (
                "in actie definitie",              # Dutch: invalid model name in action definition
                "invalid model name",               # English equivalent
                "geen overeenkomende records",      # Dutch: no matching records (found for external id)
                "no matching record",               # English equivalent
                "bestaat niet in model",            # Dutch: field/element does not exist in model
                "does not exist in model",          # English equivalent
            )
            if any(sig in error_text.lower() for sig in _stale_registry_signatures):
                import logging

                logging.getLogger(__name__).warning(
                    "warm-worker install hit a known stale-registry false negative for %r on %r "
                    "(external-id-not-yet-registered ParseError) -- falling back to the slow, "
                    "fresh-process install path rather than reporting a false content failure: %r",
                    module_name, db, error_text[:500],
                )
                return None
            error_kind = None
            if "must be owner of table" in error_text or "InsufficientPrivilege" in error_text:
                error_kind = "postgres_ownership_blocked"
            return InstallResult(
                module_name=module_name, db=db, success=False, error_kind=error_kind,
                message=f"Install failed (warm worker): {error_text[:500]}",
                log_tail=error_text[-2000:],
            )
        # Temporary diagnostic (2026-07-22): this path silently fell back
        # to the slow subprocess install on the first real production
        # run, with no visibility into why. Logged (not raised -- the
        # fallback itself must never be disturbed) so the next run
        # surfaces the real cause instead of staying a silent mystery.
        import logging

        logging.getLogger(__name__).warning(
            "warm-worker install fell back for %r on %r -- rc=%s stdout_tail=%r stderr_tail=%r",
            module_name, db, proc.returncode, (proc.stdout or "")[-500:], (proc.stderr or "")[-500:],
        )
        return None
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "warm-worker install raised for %r on %r: %r", module_name, db, exc,
        )
        return None


def install_module(
    module_name: str, db: str, remote_port: int = 8071,
    container: str | None = None, sandbox: bool = False, task_id: str | None = None,
    test_enable: bool = False,
) -> InstallResult:
    """First-time install of `module_name` into `db` via `odoo-bin -i`
    (click-odoo-update's own hash-based update only ever touches
    modules already in an 'installed'/'to upgrade' state -- confirmed
    by reading click_odoo_contrib/update.py directly during this
    phase's testing -- so it cannot perform an initial install; that
    part is odoo-bin's own job, plain and official). Always re-runs the
    Production-instance safety guard on `db` before touching anything.

    sandbox (Phase 20, §24.13, 2026-07-13): False (default) preserves
    every existing call site's exact prior behavior -- the real dev
    guard (_assert_safe_odoo_target(), port 8071 only). Callers doing a
    genuine Build sandbox pre-flight pass explicitly opt in with
    sandbox=True, which runs the SEPARATE, narrower
    infra.odoo_settings.assert_safe_sandbox_target() guard instead
    (port 8072 / odoo16-dev2 only, a fresh sandbox-pattern db name only)
    -- never a loosening of the main guard, a genuinely different check
    for a genuinely different, explicitly-opted-into target.

    Known, real limitation as of this phase (2026-07-07): installing
    ANY module that adds a column to an existing model (e.g. res.partner)
    fails on this deployment's duplicate databases with
    psycopg2.errors.InsufficientPrivilege ("must be owner of table ...")
    -- confirmed systemic across two independently-created duplicates,
    not a one-off. This is a Postgres-level table-ownership issue on
    the duplicate databases themselves, entirely outside this project's
    access boundary (no Postgres access, by design) -- surfaced here as
    a structured error_kind so a caller can report it cleanly rather
    than getting a raw traceback, but NOT something this function
    attempts to work around. Confirmed (Phase 9.5) this does NOT occur
    against a genuinely fresh database (create_database()) -- which is
    exactly what the sandbox pre-flight path uses, never
    duplicate_database().
    """
    if sandbox:
        from infra.odoo_settings import assert_safe_sandbox_target
        assert_safe_sandbox_target(db, remote_port)
    else:
        _assert_safe_odoo_target(db, remote_port)

    # Self-healing pre-install pass (2026-08-14): see
    # _self_heal_stale_templates_xml_manifest_refs()'s own docstring --
    # strips the one known-bad 'views/templates.xml'-style stale
    # reference from ANY module on the addons path (not just this
    # one), since Odoo's registry-wide reload during install can pull
    # in an already-broken dependency regardless of this task's own
    # clean content. Best-effort, never blocks install on failure.
    _self_heal_stale_templates_xml_manifest_refs(container=container)

    # Pre-install consistency gate (2026-07-20, UPDATE 20) -- see
    # _find_missing_manifest_data_files()'s own docstring for the real
    # bug this closes (task #54's FileNotFoundError, a stale-baseline
    # scoped-edit patch leaving the manifest referencing a file that
    # was never re-written). None means the check itself couldn't get
    # a confident answer (manifest unreadable/unparsable) -- never
    # blocks install on an inconclusive result, same conservative
    # posture as every other query helper in this file; a genuinely
    # broken manifest still gets caught by odoo-bin's own install
    # error below, just without this earlier, clearer message.
    missing_files = _find_missing_manifest_data_files(module_name, container=container)
    if missing_files:
        return InstallResult(
            module_name=module_name,
            db=db,
            success=False,
            error_kind="manifest_references_missing_file",
            message=(
                f"__manifest__.py's own 'data' list references file(s) {missing_files!r} that do "
                f"not actually exist on disk -- refusing to install rather than let odoo-bin crash "
                f"deep inside module loading with a raw FileNotFoundError."
            ),
        )

    # Phase 21 (2026-07-22): try the warm-worker path first (see
    # _install_module_via_warm_worker()'s own docstring) -- only for
    # the real dev target (never sandbox, a genuinely different
    # container/database the worker isn't connected to), only when a
    # real task is driving this (task_id given). Falls back to the
    # proven subprocess path below on ANY failure, including the
    # worker simply not being up -- this optimization must never be
    # able to turn a real success into a false failure.
    # Phase 28A (2026-07-28): the warm-worker path is a persistent,
    # long-running process reused across many installs -- --test-enable
    # runs Odoo's own "post tests" phase as part of a single fresh
    # registry build, and this project has never confirmed the warm
    # worker's own incremental-install mechanism runs that phase the
    # same way a plain, fresh `-i` invocation does. Rather than assume,
    # test_enable=True always uses the proven, directly-observed
    # subprocess path below (already confirmed live to correctly run
    # and report real test results) -- never the warm-worker fast path.
    if task_id and not sandbox and is_fast_path_eligible(db) and not test_enable:
        warm_result = _install_module_via_warm_worker(module_name, db)
        if warm_result is not None:
            # Phase 36 §13.2/§13.4: fire-and-forget live install-state sync,
            # ONLY on a genuine success -- this is one of two real success
            # returns in this function (see the identical call just before
            # the function's final `return` below for the other, the
            # non-warm-worker subprocess path); every failure return above
            # and below is deliberately left untouched.
            if warm_result.success:
                _trigger_install_state_sync(module_name, db, task_id)
            return warm_result

    # Real, confirmed bug found live (2026-07-29, school_student task,
    # automated_tests round): a real, generated test asserted demo data
    # records exist post-install (`self.assertGreaterEqual(len(students),
    # 5)`) and got `0 not greater than or equal to 5` -- the module's
    # own demo_data.xml was confirmed correct and correctly referenced
    # in the manifest, so the records simply never loaded during this
    # install. Explicit `--without-demo=False` for test-enabled installs
    # only (never the plain, non-test path, which has no reason to pay
    # the extra demo-loading cost) removes any ambiguity about which
    # default actually applies in this specific deployment, rather than
    # relying on an implicit default this session couldn't fully verify.
    test_flag = " --test-enable --without-demo=False" if test_enable else ""
    cmd = (
        f"{_ODOO_BIN_PATH} -c {_ODOO_CONF_PATH} --addons-path={_FULL_ADDONS_PATH} "
        f"-d {db} -i {module_name} --stop-after-init --no-http{test_flag}"
    )
    # Phase 31 §9/Phase B (2026-08-08): real, root-caused bug found live during the round-3
    # controlled concurrency experiment -- confirmed via the actual traceback recorded in
    # Postgres (agent_memory_events, task 0cc73a5d-...): `psycopg2.errors.SerializationFailure:
    # could not serialize access due to concurrent update`, raised from
    # `Registry.new() -> load_modules() -> env.flush_all()` deep inside a FRESH `odoo-bin -i`
    # subprocess's own registry bootstrap. This install target (`db`) is not in
    # odoo_schema_client._FAST_PATH_ELIGIBLE_DBS (only the real live dev DB is), so
    # install_module() always takes THIS slow subprocess path for it -- meaning two genuinely
    # independent constraint nodes (correctly non-colliding on infra.fencing's own per-target-
    # model lock, which is scoped to `contract.module_identity`, e.g. 'product.template' vs
    # 'hr.employee') can still launch two separate `odoo-bin -i` processes CONCURRENTLY against
    # the SAME physical database. Odoo's own full-registry-load transaction touches shared core
    # metadata tables (ir_model_fields/ir_model_data/etc.) regardless of which specific model
    # each install targets, so two concurrent registry loads against the same DB can genuinely
    # write-write-conflict there -- this is a REAL, different shared resource than the one
    # infra.fencing's per-module lock protects (that lock is about the generated module's own
    # git-tracked files, not the physical install target), so widening that lock's scope would
    # incorrectly re-serialize genuinely independent nodes' whole round (LLM generation included,
    # the actually expensive part) just to protect a ~10-50s install step.
    #
    # Per Postgres's own documented guidance (SQLSTATE 40001 -- "could not serialize access due
    # to concurrent update" -- is BY DESIGN meant to be retried by the client, not treated as a
    # genuine content/logic failure), and matching this same file's existing retry pattern for
    # the sibling transient condition ("processing a scheduled action" in
    # _install_module_via_warm_worker() above): retry the whole install a few times with a short
    # backoff before treating it as a real failure. Only ever fires for this one, specific,
    # well-known-transient Postgres error class -- any other failure (a real content bug, a
    # missing dependency, a genuine test failure) still surfaces on the very first attempt,
    # unchanged.
    _SERIALIZATION_FAILURE_SIGNATURES = (
        "SerializationFailure",
        "could not serialize access due to concurrent update",
    )
    _INSTALL_SERIALIZATION_RETRY_ATTEMPTS = 3
    _INSTALL_SERIALIZATION_RETRY_BACKOFF_SECONDS = 5
    combined = ""
    # Real fix, 2026-08-09 (the project owner's own explicit request for "smart" concurrency that accounts
    # for "installation of sandbox platform" specifically): acquire_db_install_lock() (see
    # infra/fencing.py's own docstring for the full incident) makes two nodes targeting the SAME
    # physical `db` genuinely wait their turn for JUST this real registry-bootstrap step, instead
    # of racing and relying solely on the retry-after-failure safety net below. Best-effort by
    # design (task_id may be None for a non-task-driven caller -- tests, scripts -- which skips
    # the lock entirely, unchanged prior behavior; a genuine max_wait_sec timeout also proceeds
    # unlocked rather than blocking forever) -- the SerializationFailure retry loop below is
    # UNCHANGED and stays as the real safety net for whatever this lock doesn't catch (a
    # different, unlocked caller, a timed-out wait, or a race this lock's own TTL didn't cover).
    _lock_handle = acquire_db_install_lock(db, task_id) if task_id else None
    try:
        for _install_attempt in range(_INSTALL_SERIALIZATION_RETRY_ATTEMPTS):
            proc = _run_in_container(cmd, timeout=300 if not test_enable else 480, container=container)
            combined = (proc.stdout or "") + (proc.stderr or "")
            if not any(sig in combined for sig in _SERIALIZATION_FAILURE_SIGNATURES):
                break
            if _install_attempt < _INSTALL_SERIALIZATION_RETRY_ATTEMPTS - 1:
                import logging
                import time as _time

                logging.getLogger(__name__).warning(
                    "install of %r into %r hit a transient Postgres SerializationFailure "
                    "(concurrent registry load on the same database) -- retrying (attempt %d/%d) "
                    "after a short backoff, per Postgres's own documented guidance for this error "
                    "class, rather than treating it as a real content failure",
                    module_name, db, _install_attempt + 2, _INSTALL_SERIALIZATION_RETRY_ATTEMPTS,
                )
                _time.sleep(_INSTALL_SERIALIZATION_RETRY_BACKOFF_SECONDS * (_install_attempt + 1))
    finally:
        if task_id and _lock_handle is not None and _lock_handle.acquired:
            release_db_install_lock(db, task_id)
    log_tail = _extract_error_excerpt(combined)
    # P13 item 11, Unit 1: parsed once from the FULL raw output (not the windowed log_tail --
    # a WARNING like the access-rules one is not necessarily near whatever marker log_tail's own
    # window anchors on), reused by every return path below, same discipline as tests_run/etc.
    findings = _parse_install_findings(combined)

    # Parsed once, reused by every return path below -- Odoo's own real
    # summary line, confirmed live (2026-07-28) via direct observation:
    # "odoo.tests.result: 0 failed, 0 error(s) of 8 tests when loading
    # database ...". None when test_enable=False (no such line exists).
    tests_run = tests_failed = tests_errored = None
    if test_enable:
        test_summary_match = re.search(
            r"(\d+)\s+failed,\s+(\d+)\s+error\(s\)\s+of\s+(\d+)\s+tests", combined,
        )
        if test_summary_match:
            tests_failed = int(test_summary_match.group(1))
            tests_errored = int(test_summary_match.group(2))
            tests_run = int(test_summary_match.group(3))

    if "must be owner of table" in combined or "InsufficientPrivilege" in combined:
        return InstallResult(
            module_name=module_name,
            db=db,
            success=False,
            error_kind="postgres_ownership_blocked",
            message=(
                f"Install failed: the Postgres role Odoo connects as does not own "
                f"one or more tables in {db!r} (confirmed systemic across multiple "
                f"duplicate databases created via Odoo's own Database Manager). "
                f"This requires a one-time table-ownership fix by whoever administers "
                f"the actual Postgres server behind odoo-dev.int -- outside this "
                f"project's access boundary by design. Do not attempt a workaround here."
            ),
            log_tail=log_tail,
            findings=findings,
        )

    # Phase 28A (2026-07-28): checked BEFORE the generic CRITICAL/
    # Traceback check below, deliberately -- a real test FAILURE
    # (an AssertionError inside a test method) always prints its own
    # "Traceback (most recent call last):" in Odoo's own test-runner
    # output, which would otherwise be misclassified as a generic
    # install crash. A real, parsed "N failed, M error(s) of K tests"
    # summary line is definitive, unambiguous proof the module itself
    # loaded successfully (Odoo only reaches its own "Starting post
    # tests" phase after "Modules loaded.") and tests genuinely ran --
    # this is checked first and wins over the weaker Traceback-text
    # signal specifically because it's the more precise, direct answer
    # to "did this succeed," confirmed live (2026-07-28) against a real
    # module with a real, deliberately-generated failing assertion.
    if test_enable and tests_run is not None and (tests_failed or tests_errored):
        return InstallResult(
            module_name=module_name,
            db=db,
            success=False,
            error_kind="tests_failed",
            message=(
                f"Install succeeded, but {tests_failed} test(s) failed and "
                f"{tests_errored} errored out of {tests_run} real, generated tests -- "
                f"see log_tail for the real assertion/error detail."
            ),
            log_tail=log_tail,
            tests_run=tests_run, tests_failed=tests_failed, tests_errored=tests_errored,
            findings=findings,
        )

    # Real, confirmed bug found live (2026-07-29, school_student task,
    # automated_tests round): "0 failed, 0 error(s) of 15 tests" is
    # definitive, positive proof every generated test genuinely passed
    # -- but the generic CRITICAL/Traceback check below used to fire
    # anyway regardless of this signal: a test deliberately exercising
    # a real constraint (test_student_unique_constraint inserting a
    # duplicate student_id to confirm the DB rejects it) makes
    # Postgres/Odoo log its own "Traceback (most recent call last):"
    # for the caught IntegrityError, even though the test's own
    # assertRaises() handled it correctly and the test PASSED. A fully
    # green test run was misclassified as a generic install failure,
    # deterministically, on every round, for as long as this task
    # included a genuine uniqueness-constraint test. Skipping the
    # Traceback/CRITICAL check here (rather than returning success
    # immediately) deliberately still lets execution fall through to
    # the real, independent ir.module.module.state=='installed'
    # verification below -- "never trust a clean-looking exit alone"
    # applies just as much to a clean-looking test summary.
    definitive_test_success = test_enable and tests_run is not None and not tests_failed and not tests_errored

    # CRITICAL exceptions during load_modules() still exit 0 on some
    # Odoo builds when --stop-after-init is set without a hard crash,
    # so success is judged by absence of a CRITICAL/Traceback marker,
    # not solely by returncode.
    if not definitive_test_success and (
        proc.returncode != 0 or "CRITICAL" in combined or "Traceback (most recent call last)" in combined
    ):
        return InstallResult(
            module_name=module_name,
            db=db,
            success=False,
            error_kind="generic_failure",
            message=f"Install failed (rc={proc.returncode}) -- see log_tail for detail.",
            log_tail=log_tail,
            findings=findings,
        )

    # Real, confirmed gap found live (2026-07-10, real end-to-end task
    # against real infra): Odoo's own module graph builder can silently
    # skip a module -- `_logger.warning('module %s: not installable,
    # skipped', module)`, odoo/modules/graph.py -- and still exit 0 with
    # no CRITICAL/Traceback anywhere in the output, whenever
    # `get_manifest()` can't resolve the module via `get_module_path()`.
    # Confirmed directly against Odoo's own source this session: this
    # fires whenever the addons-path this exact process was started
    # with doesn't include the module's real directory -- a real,
    # reproducible failure shape distinct from every case already
    # handled above, and NOT hypothetical: found while investigating a
    # real task, live. A first, cheap check on the exact known
    # signature string:
    if f"module {module_name}: not installable, skipped" in combined:
        return InstallResult(
            module_name=module_name,
            db=db,
            success=False,
            error_kind="not_installable",
            message=(
                f"Odoo's own module graph builder silently skipped {module_name!r} as "
                f"'not installable' -- it never actually loaded, despite exiting 0 with "
                f"no CRITICAL/Traceback. Usually means the addons-path this install "
                f"command ran with didn't actually resolve the module's real directory."
            ),
            log_tail=log_tail,
            findings=findings,
        )

    # The real, general fix, not just one more string pattern to chase:
    # never trust "no known-bad string appeared in the output" alone --
    # POSITIVELY confirm the module's own real state in the database
    # afterward, the same way this project's own tests
    # (test_build_specialist.py's _field_genuinely_exists_on_model())
    # already independently verify real outcomes rather than trusting a
    # process's own stdout. Closes the whole CLASS of "silently did
    # nothing, exited 0, said nothing alarming" failures at once --
    # including shapes not yet seen, not just the one found live above.
    # Base64-encoded, same discipline write_module_file() already uses
    # for arbitrary content over this SSH/bash -c/docker exec chain --
    # sidesteps quote-nesting entirely rather than trying to get a
    # Python script's own single quotes to survive two layers of shell
    # repr() unescaped.
    # Phase 21 (2026-07-22): a SEPARATE odoo-bin-shell boot found live
    # after the first pass on this fix -- runs on EVERY install (sandbox
    # AND real), not just the 8 registry-lookup functions originally
    # identified. XML-RPC fast path against the already-running server
    # replaces it the same way; only usable against the real dev target
    # (port 8071), never the sandbox (port 8072, odoo16-dev2) -- the
    # schema client's JIT key mechanism authenticates against whichever
    # single Odoo instance OMA_ODOO_URL's tunnel actually points at,
    # confirmed to be the real dev target, not the sandbox container.
    if task_id and not sandbox and is_fast_path_eligible(db):
        from tools_odoo.odoo_schema_client import get_module_state_fast

        real_state = get_module_state_fast(module_name, db) or "UNKNOWN"
    else:
        import base64

        state_check_script = (
            f"m = env['ir.module.module'].search([('name','=','{module_name}')])\n"
            f"print('INSTALL_STATE_CHECK:' + (m.state if m else 'NOT_FOUND'))\n"
        )
        encoded_script = base64.b64encode(state_check_script.encode("utf-8")).decode("ascii")
        state_cmd = (
            f"echo {encoded_script} | base64 -d | "
            f"{_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} --addons-path={_FULL_ADDONS_PATH} "
            f"-d {db} --no-http"
        )
        state_proc = _run_in_container(state_cmd, timeout=120)
        state_combined = (state_proc.stdout or "") + (state_proc.stderr or "")
        match = re.search(r"INSTALL_STATE_CHECK:(\S+)", state_combined)
        real_state = match.group(1) if match else "UNKNOWN"
    if real_state != "installed":
        return InstallResult(
            module_name=module_name,
            db=db,
            success=False,
            error_kind="state_verification_failed",
            message=(
                f"install_module()'s own command exited cleanly with no known-bad "
                f"marker, but a real, independent post-install check found "
                f"ir.module.module.state={real_state!r} for {module_name!r}, not "
                f"'installed' -- never trusting a clean-looking exit alone."
            ),
            log_tail=log_tail,
            findings=findings,
        )

    # Phase 36 §13.2/§13.4: fire-and-forget live install-state sync -- the
    # other of this function's two real success returns (see the identical
    # call above, on the warm-worker fast path). This is the correct single
    # insertion point on THIS path: every earlier `return InstallResult(...,
    # success=False, ...)` above (manifest check, postgres ownership,
    # tests_failed, generic_failure, not_installable, state_verification_failed)
    # is a real early-return failure and is deliberately left untouched.
    _trigger_install_state_sync(module_name, db, task_id)
    return InstallResult(
        module_name=module_name, db=db, success=True,
        message=(
            "Install completed." if not test_enable
            else f"Install completed; all {tests_run or 0} generated test(s) passed."
        ),
        tests_run=tests_run, tests_failed=tests_failed, tests_errored=tests_errored,
        findings=findings,
    )


def _trigger_install_state_sync(module_name: str, db: str, task_id: str | None) -> None:
    """Best-effort wrapper around install_state_sync.sync_module_install_state --
    must never raise or block install_module()'s own success path on a graph-
    service import/scheduling problem (fail-open, matching every other graph
    touchpoint in this codebase)."""
    try:
        from tools_odoo.knowledge_graph.install_state_sync import sync_module_install_state

        sync_module_install_state(module_name, db, task_id=task_id)
    except Exception:  # noqa: BLE001 -- fail-open, see docstring
        pass


def verify_combined_install(
    module_names: list[str], db: str, remote_port: int = 8071, container: str | None = None,
) -> InstallResult:
    """P7b (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    §17, item 3): the cross-module analog of the whole-assembly single-module install check --
    verifies the COMBINED install of every module in `module_names` together, in one registry
    load (Odoo's own `-i mod1,mod2,mod3` comma-separated form), not each module's install checked
    in isolation. This is a real, new check: nothing today verifies that two related modules that
    each install cleanly on their own also work correctly installed TOGETHER (e.g. one module's
    view referencing a field a sibling module adds).

    Deliberately narrower than `install_module()` above, not a full mirror of it -- reuses only
    the two most load-bearing checks from that function (a hard rc/CRITICAL/Traceback failure, and
    the "silently skipped as not installable" signature, checked per real module name since
    Odoo's own graph builder reports that warning per-module even inside a combined `-i` list),
    not every edge case that function's own extensive history has accumulated (Postgres
    table-ownership, test-result parsing) -- those remain single-module-install concerns this
    function's own narrower "does the combined assembly come up at all" question doesn't need.
    `module_name` on the returned `InstallResult` is the '+'-joined list, for identification only
    -- never a real, individually-addressable Odoo module name.
    """
    _assert_safe_odoo_target(db, remote_port)
    combined_name = "+".join(module_names)
    addons_csv = ",".join(module_names)
    cmd = (
        f"{_ODOO_BIN_PATH} -c {_ODOO_CONF_PATH} --addons-path={_FULL_ADDONS_PATH} "
        f"-d {db} -i {addons_csv} --stop-after-init --no-http"
    )
    proc = _run_in_container(cmd, timeout=300, container=container)
    output = (proc.stdout or "") + (proc.stderr or "")
    log_tail = _extract_error_excerpt(output)

    if proc.returncode != 0 or "CRITICAL" in output or "Traceback (most recent call last)" in output:
        return InstallResult(
            module_name=combined_name, db=db, success=False, error_kind="generic_failure",
            message=(
                f"Combined install of {module_names!r} failed (rc={proc.returncode}) -- see "
                f"log_tail for detail. This does not by itself say which individual module is at "
                f"fault, only that the modules do not install correctly TOGETHER."
            ),
            log_tail=log_tail,
        )

    skipped = [m for m in module_names if f"module {m}: not installable, skipped" in output]
    if skipped:
        return InstallResult(
            module_name=combined_name, db=db, success=False, error_kind="not_installable",
            message=(
                f"Odoo's own module graph builder silently skipped {skipped!r} as 'not "
                f"installable' during the combined install of {module_names!r} -- despite "
                f"exiting 0 with no CRITICAL/Traceback."
            ),
            log_tail=log_tail,
        )

    return InstallResult(
        module_name=combined_name, db=db, success=True,
        message=f"Combined install of {module_names!r} completed.",
    )


def remove_scaffolded_module(module_name: str) -> None:
    """The undo half of scaffold_module() -- removes the module
    directory from /mnt/extra-addons entirely. Used by
    manager.compensations.run_compensations() when a module-development
    task gets cut off after scaffolding but before (or instead of) a
    successful install -- per the build plan's Phase 9 step 4, "before
    creating a draft record/module, record what undo means."
    """
    _run_in_container(f"rm -rf {_MODULE_DEV_ADDONS_DIR}/{module_name}")


def _get_module_state(module_name: str, db: str) -> str | None:
    """Fresh-process (odoo-bin shell) read of ir.module.module.state for
    module_name -- same discipline as get_model_fields() above: a new
    process each call, reading directly from the DB, never a cached
    live-server registry. Returns None if the module row doesn't exist
    or the query itself failed (SSH hiccup, timeout) -- conservative,
    same posture as every other query helper in this file.
    """
    import base64

    python_code = (
        f'r = env["ir.module.module"].search([("name", "=", "{module_name}")]); '
        f'print(r[0].state if r else "MODULE_NOT_FOUND")'
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line == "MODULE_NOT_FOUND":
            return None
        if line and " " not in line:
            return line
    return None


def uninstall_module(module_name: str, db: str, remote_port: int = 8071, task_id: str | None = None) -> InstallResult:
    """The undo half of install_module() -- uninstalls via
    click-odoo-contrib's click-odoo-uninstall, the same official
    mechanism family used to install, never a manual database edit
    (per the build plan's own explicit instruction, echoed in
    §0.5.7's example TaskContract). Re-validates the safety guard on
    `db` first, same as install_module().

    Real bug found and fixed live (2026-07-11): unlike odoo-bin,
    click-odoo-uninstall has NO --addons-path option at all (confirmed
    via its own --help) -- every call here always failed immediately
    with "no such option: --addons-path", rc=2, before ever touching
    the database. This call path had apparently never been successfully
    exercised. click-odoo-uninstall resolves addons_path purely from
    the config file passed via -c (or ODOO_RC/~/.odoorc) -- which is
    exactly why the earlier addons-path persistent-config fix (making
    /etc/odoo/odoo.conf itself include /mnt/extra-addons) matters here
    too, not just for the live web server: this call now correctly
    depends on that same fix to see generated modules at all.

    Real bug found live (2026-07-20, Phase 20 Area 2 deep-reverify pass
    5): click-odoo-uninstall returned rc=0 ("Uninstall completed")
    while the module's ir.module.module.state stayed 'installed' and
    its model's ir.model row was still real -- confirmed directly via
    the DB on two separate tasks in the same run (#43's service.ticket,
    #44's asset.registry), both of which had this exact module name's
    cleanup reported as "succeeded" by an EARLIER round, yet the model
    was still real several rounds later, blocking a fresh install with
    a false "already exists" rejection. A manual re-run of this same
    function against the same still-installed module immediately
    afterward uninstalled it correctly -- so rc=0 alone is not a
    trustworthy signal; the DB state must be independently re-checked,
    same "never trust the self-report" discipline used everywhere else
    in this codebase (e.g. the security-access-claim verification).
    Never proven why the first attempt silently didn't take (a lock
    held by the live dev server sharing this DB is the leading
    suspect), but rather than chase a one-off race, this makes the
    function self-healing: verify via a fresh process after every
    claimed-successful uninstall, and retry once before giving up.
    """
    _assert_safe_odoo_target(db, remote_port)

    cmd = (
        f"export PATH=$PATH:{_LOCAL_BIN} && "
        f"click-odoo-uninstall -c {_ODOO_CONF_PATH} "
        f"-d {db} -m {module_name}"
    )

    for attempt in range(2):
        proc = _run_in_container(cmd, timeout=300)
        combined = (proc.stdout or "") + (proc.stderr or "")

        if proc.returncode != 0:
            # Real, latent bug found live (2026-07-19, Phase 20 Area 2):
            # `log_tail` was referenced here but never computed anywhere in
            # this function (every other failure-path return in this file
            # computes it via _extract_error_excerpt(combined) first) --
            # every failed uninstall crashed with NameError instead of
            # returning a clean InstallResult, silently swallowing the real
            # click-odoo-uninstall error and reporting a confusing
            # "NameError" instead. Rarely triggered before since uninstall
            # was rarely called on a genuinely-failing target; now called
            # far more often by cleanup_module_from_failed_round()
            # (manager/loop.py), making this a real, live-hit bug.
            log_tail = _extract_error_excerpt(combined)
            return InstallResult(
                module_name=module_name,
                db=db,
                success=False,
                error_kind="uninstall_failed",
                message=f"click-odoo-uninstall failed (rc={proc.returncode}) -- see log_tail.",
                log_tail=log_tail,
            )

        if task_id and is_fast_path_eligible(db):
            from tools_odoo.odoo_schema_client import get_module_state_fast

            state = get_module_state_fast(module_name, db)
        else:
            state = _get_module_state(module_name, db)
        if state is None or state != "installed":
            return InstallResult(module_name=module_name, db=db, success=True, message="Uninstall completed.")
        # rc=0 but the module is still genuinely 'installed' -- the
        # false-success case found live above. Retry once before
        # giving up.

    return InstallResult(
        module_name=module_name,
        db=db,
        success=False,
        error_kind="uninstall_failed",
        message=(
            "click-odoo-uninstall reported success (rc=0) twice, but "
            f"ir.module.module.state for {module_name!r} is still 'installed' -- "
            "the uninstall did not actually take effect."
        ),
    )


def uninstall_module_sandbox(module_name: str, db: str, container: str) -> InstallResult:
    """Phase 35 §18.4: the sandbox-target counterpart to uninstall_module() above, built for
    manager/rollback_dry_run_probe.py's own real, executed reversibility probe -- confirmed live
    that uninstall_module() itself is NOT sandbox-aware at all (unlike install_module()'s own
    explicit `sandbox=True` opt-in): it hardcodes the real-dev-target safety guard
    (_assert_safe_odoo_target(), port 8071 canonical-dev-db patterns only) and never threads a
    `container` argument through to its own _run_in_container()/_get_module_state() calls, so it
    can only ever run against the real dev container.

    Real, confirmed gap found live (2026-08-14): `click-odoo-uninstall` (the mechanism
    uninstall_module() above uses) is genuinely not installed on the sandbox container
    (`odoo16-dev2`) -- confirmed directly via SSH (`command not found`), never installed there
    since sandbox pre-flight previously only ever dropped the whole disposable database, never
    selectively uninstalled a module from it. Rather than add a new pip-install infra dependency
    to the sandbox container, this uses Odoo's own direct API instead
    (`ir.module.module.button_immediate_uninstall()` via a fresh `odoo-bin shell` process, the
    same "fresh process, direct DB read" discipline `_get_module_state_in_container()` below
    already uses) -- self-contained, no new external tool dependency on the target container.
    """
    from infra.odoo_settings import assert_safe_sandbox_target

    assert_safe_sandbox_target(db, 8072)

    import base64

    python_code = (
        f'r = env["ir.module.module"].search([("name", "=", "{module_name}")]); '
        f'r.button_immediate_uninstall() if r else None; '
        f'env.cr.commit(); '
        f'print("UNINSTALL_ATTEMPTED")'
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )

    for _attempt in range(2):
        proc = _run_in_container(cmd, timeout=300, container=container)
        combined = (proc.stdout or "") + (proc.stderr or "")

        if proc.returncode != 0 or "UNINSTALL_ATTEMPTED" not in combined:
            log_tail = _extract_error_excerpt(combined)
            return InstallResult(
                module_name=module_name, db=db, success=False, error_kind="uninstall_failed",
                message=f"button_immediate_uninstall() shell call failed (rc={proc.returncode}) -- see log_tail.",
                log_tail=log_tail,
            )

        state = _get_module_state_in_container(module_name, db, container)
        if state is None or state != "installed":
            return InstallResult(module_name=module_name, db=db, success=True, message="Uninstall completed.")

    return InstallResult(
        module_name=module_name, db=db, success=False, error_kind="uninstall_failed",
        message=(
            "click-odoo-uninstall reported success (rc=0) twice, but ir.module.module.state "
            f"for {module_name!r} is still 'installed' -- the uninstall did not actually take effect."
        ),
    )


def _get_module_state_in_container(module_name: str, db: str, container: str) -> str | None:
    """The container-parameterized sibling of _get_module_state() above -- same exact mechanism
    (a fresh odoo-bin shell process reading ir.module.module.state directly), just routed to the
    given container instead of always the real dev container. Needed for
    uninstall_module_sandbox() above; _get_module_state() itself is left untouched since every
    one of its existing callers needs the real dev container, unchanged.
    """
    import base64

    python_code = (
        f'r = env["ir.module.module"].search([("name", "=", "{module_name}")]); '
        f'print(r[0].state if r else "MODULE_NOT_FOUND")'
    )
    encoded = base64.b64encode(python_code.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=90, container=container)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line == "MODULE_NOT_FOUND":
            return None
        if line and " " not in line:
            return line
    return None


class ModuleDevToolchain:
    """The single callable tool interface the Build specialist (phase 9)
    uses for all module-development work -- scaffold, lint, install, as
    one object rather than three loose functions scattered across the
    specialist's own logic.
    """

    def scaffold(self, module_name: str) -> ScaffoldResult:
        return scaffold_module(module_name)

    def lint(self, module_name: str) -> LintResult:
        return lint_module(module_name)

    def install(self, module_name: str, db: str) -> InstallResult:
        return install_module(module_name, db)
