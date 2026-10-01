"""Phase 10: strictly read-only helpers for the Code-Review specialist.
This module never imports or exposes anything write-capable -- its only
job is reading real files from the Odoo container, for the two shapes
Code-Review's run() needs: a scaffolded module under /mnt/extra-addons
(diff review of a Build specialist's own output) and the broader custom
codebase under the same addons mount (task 4's whole-codebase audit, scoped
to whatever Build has actually generated so far in this demo -- there's no
separate pre-existing client codebase to point at in a fresh Odoo CE
install). Both are read via a plain `find`/`cat` through `docker exec` --
no -u root, no write flags, nothing that could mutate anything. Per the
build plan's own emphasis, task 4's read-only property is enforced
structurally here (this module has no write function to call at all), not
left as a promise the specialist keeps.

Stage 5 port: reads used to go over SSH to a second real host; in this
single-host Compose port, `docker exec` into the sibling `odoo` container
replaces that transport directly -- same read-only command shapes below.
"""

from __future__ import annotations

import os
import re
import subprocess

_CONTAINER_ENV = "OMA_ODOO_CONTAINER"

_MODULE_DEV_ADDONS_DIR = "/mnt/extra-addons"
_CUSTOM_CODEBASE_ROOT = "/mnt/extra-addons"

_MAX_FILES = 60
_MAX_FILE_BYTES = 40_000


class CodebaseReadError(RuntimeError):
    pass


def _docker_exec_readonly(bash_command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    container = os.environ.get(_CONTAINER_ENV, "oma-odoo-1")
    return subprocess.run(
        ["docker", "exec", container, "bash", "-c", bash_command],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",  # real custom-addon files aren't guaranteed clean UTF-8
    )


def _read_files_under(root: str, find_expr: str) -> dict[str, str]:
    find_proc = _docker_exec_readonly(f"find {root} {find_expr} 2>/dev/null | head -{_MAX_FILES}")
    if find_proc.returncode != 0:
        raise CodebaseReadError(f"could not list files under {root!r}: {find_proc.stderr}")
    paths = [p for p in find_proc.stdout.splitlines() if p.strip()]
    if not paths:
        raise CodebaseReadError(f"no files found under {root!r}")

    files: dict[str, str] = {}
    for path in paths:
        cat_proc = _docker_exec_readonly(f"cat {path}")
        files[path] = cat_proc.stdout[:_MAX_FILE_BYTES]
    return files


def read_module_files(module_name: str) -> dict[str, str]:
    """Reads every real source file for `module_name` -- checks
    /mnt/extra-addons first (our own scaffolded modules, the original
    and still primary use case: diff review of a Build specialist's
    own output, and depends_on_module:/edit_existing_module: targeting
    a module OUR OWN pipeline previously created), falling back to the
    real custom Odoo codebase root (/opt/site/site16) if not found
    there.

    Phase 22 follow-up (2026-07-23): the fallback is new -- real,
    confirmed gap found live running the SITE 50-task list:
    depends_on_module: and edit_existing_module: both assumed their
    target module always lives under /mnt/extra-addons, silently
    failing (CodebaseReadError, "no files found") for a genuinely
    pre-existing CUSTOMER module like `project_meerwerk` or
    `mis_base_extend` (real files, but at /opt/site/site16, never
    written by this pipeline at all) -- meaning neither mechanism had
    ever actually been exercised against real customer code, only
    against modules this pipeline itself had previously generated.
    Strictly additive/backward-compatible: any caller whose module
    genuinely lives under /mnt/extra-addons gets the exact same result
    as before; this only changes the outcome for a module that was
    NEVER found there at all (previously always a hard error).
    Excludes __pycache__/.pyc -- binary compiled artifacts, not
    source, not useful (or even valid UTF-8) for a code review.
    """
    find_expr = r"-type f -not -path '*__pycache__*' -not -name '*.pyc'"
    try:
        return _read_files_under(f"{_MODULE_DEV_ADDONS_DIR}/{module_name}", find_expr)
    except CodebaseReadError:
        return _read_files_under(f"{_CUSTOM_CODEBASE_ROOT}/{module_name}", find_expr)


_DOTTED_MODEL_NAME_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,4}\b")
# A real, large SITE module's own extension context, capped hard: real,
# confirmed live failure (2026-07-24, 50-task sequential re-run, task
# 001) -- `read_module_files('mis_base_extend')` pulled in ALL 135 files
# / 27,031 lines of that real, large module (read_module_files() has its
# own 60-file/40KB-per-file caps, still enough to reach ~98,305 input
# tokens for a task that only needed ONE model's own file) and the
# generation call was rejected outright by the real backend: "This
# model's maximum context length is 98304 tokens... your prompt
# contains at least 98305 input tokens." A large real module dumped in
# full is fundamentally unscalable as "extension context" -- no
# reasonably-sized prompt budget survives it. 60,000 bytes is generous
# headroom for the handful of files a task's own goal genuinely
# concerns (a model file plus __manifest__.py), while still bounding
# the absolute worst case hard.
_RELEVANT_FILES_MAX_TOTAL_BYTES = 60_000


def read_module_files_relevant_to_goal(module_name: str, goal: str) -> dict[str, str]:
    """Real, general fix for the bug documented above
    `_RELEVANT_FILES_MAX_TOTAL_BYTES` -- reads ONLY the files inside
    `module_name` that are actually relevant to `goal`, instead of
    `read_module_files()`'s "everything in the module" behavior (still
    the right choice for its OTHER real use case, reviewing a small,
    freshly-scaffolded `oma_*` module's own diff, where "everything"
    genuinely means a handful of files).

    Extracts dotted, Odoo-technical-name-shaped tokens from `goal`
    (e.g. "crm.lead", "project.meerwerk" -- the exact convention every
    real task spec in this project already uses, e.g. "Model:
    crm.lead"), then greps the module's own `models/` directory for
    files whose `_name`/`_inherit` actually defines/extends one of
    those names -- the same grep discipline `find_module_defining_
    model()` above already uses, just scoped to files within ONE known
    module instead of searching for which module owns a name at all.
    Always also includes `__manifest__.py` (small, genuinely useful
    context: real dependencies, real data file list) when present.

    Falls back to `read_module_files()`'s full behavior, still under a
    hard `_RELEVANT_FILES_MAX_TOTAL_BYTES` total-size cap, ONLY if goal
    parsing found no model-name-shaped token at all, or the grep
    matched nothing -- conservative in the direction of "give Build
    SOME context" rather than silently returning empty, since a wrong
    guess here (irrelevant files) is far less damaging than the
    context-overflow bug this function exists to prevent.
    """
    candidates = sorted(set(_DOTTED_MODEL_NAME_RE.findall(goal)))
    matched_paths: list[str] = []
    matched_root: str | None = None
    if candidates:
        pattern = "|".join(re.escape(c) for c in candidates)
        for root in (f"{_MODULE_DEV_ADDONS_DIR}/{module_name}", f"{_CUSTOM_CODEBASE_ROOT}/{module_name}"):
            grep_proc = _docker_exec_readonly(
                f'grep -rlE "_(name|inherit).{{0,20}}({pattern})" {root} '
                f"--include=*.py 2>/dev/null | head -10"
            )
            if grep_proc.returncode == 0 and grep_proc.stdout.strip():
                matched_paths = [p for p in grep_proc.stdout.splitlines() if p.strip()]
                matched_root = root
                break

    if not matched_paths:
        # Conservative fallback -- goal parsing found nothing usable, or
        # the grep found nothing. Still capped hard below; never the
        # unbounded "everything" behavior that caused the real incident.
        # A genuine read failure (module doesn't exist at all) still
        # propagates as CodebaseReadError, same as before this function
        # existed -- callers rely on that to report a real, honest error
        # rather than silently proceeding with no context at all.
        all_files = read_module_files(module_name)
        files: dict[str, str] = {}
        total = 0
        for path, content in all_files.items():
            if total >= _RELEVANT_FILES_MAX_TOTAL_BYTES:
                break
            files[path] = content
            total += len(content)
        return files

    manifest_path = f"{matched_root}/__manifest__.py"
    files = {}
    total = 0
    for path in [manifest_path, *matched_paths]:
        if total >= _RELEVANT_FILES_MAX_TOTAL_BYTES:
            break
        cat_proc = _docker_exec_readonly(f"cat {path} 2>/dev/null")
        if cat_proc.returncode != 0 or not cat_proc.stdout:
            continue
        content = cat_proc.stdout[:_MAX_FILE_BYTES]
        files[path] = content
        total += len(content)
    return files


def read_codebase_tree(relative_path: str = "") -> dict[str, str]:
    """Reads real .py/.xml files under the actual custom Odoo codebase
    (/opt/site/site16) -- task 4's audit target. `relative_path` scopes
    the read to one module/subdirectory at a time -- auditing the whole
    multi-module tree in a single LLM call isn't realistic token-budget-
    wise; the real procedure is module-by-module, per the
    odoo-codebase-audit skill's own instruction.
    """
    root = f"{_CUSTOM_CODEBASE_ROOT}/{relative_path}" if relative_path else _CUSTOM_CODEBASE_ROOT
    return _read_files_under(root, r"-maxdepth 3 -type f \( -name '*.py' -o -name '*.xml' \)")


def module_exists_on_disk(module_name: str) -> bool:
    """True if `module_name` is a real, already-scaffolded module
    directory under /mnt/extra-addons -- a plain, deterministic
    existence check, no grep/regex guessing involved.

    Exists so callers with a string that MIGHT already be a real module
    name (not a model name needing a lookup) can check that directly
    first, rather than running it through model-name interpretation
    (e.g. underscore-to-dot conversion) that only makes sense for a
    genuine Odoo model name and silently mangles an already-correct
    module name into something nothing will ever match.
    """
    proc = _docker_exec_readonly(f"test -d {_MODULE_DEV_ADDONS_DIR}/{module_name} && echo yes")
    return proc.returncode == 0 and proc.stdout.strip() == "yes"


def find_module_defining_model(model_name: str) -> str | None:
    """Real, confirmed architectural gap found live: a task extending
    or referencing a model built by an EARLIER task (e.g. "the service
    module") has no reliable way to know which module actually defines
    it, since every scaffolded module gets a fresh, randomly-suffixed
    name (oma_build_a_service_record_56298314, never a stable, guessable
    one) -- Build kept failing the exact same "missing dependency"
    Code-Review finding round after round, not from carelessness, but
    because the correct module name genuinely wasn't knowable from the
    goal text alone. /mnt/extra-addons IS the one real, durable source
    of truth for this -- every module ever scaffolded lives there,
    still on disk, regardless of which task created it. A plain grep
    for `_name = '<model>'` across every models.py under it, read-only,
    same SSH/docker-exec discipline as everything else in this module,
    answers the question directly instead of guessing or asking the
    model to know something it structurally cannot.

    Real, general fix (2026-07-26, Phase 25D, task 005's own
    resubmission): this used to search ONLY `/mnt/extra-addons` (this
    pipeline's own transient, per-task scaffolded modules) -- for a
    model this pipeline never actually created (e.g. `project.meerwerk`,
    a real, permanent model the genuine customer module `project_
    meerwerk` under `/opt/site/site16` defines), that search can only
    ever find a WRONG answer: some unrelated earlier task's own OMA
    scaffold that happens to also (incorrectly) redefine the same model
    with its own `_name` line, rather than correctly `_inherit`ing it.
    Confirmed live: `project.meerwerk` resolved to
    `oma_create_an_import_wizard_ede68004` -- a completely unrelated
    module from an unrelated earlier task -- purely because it was the
    first (wrong) hit under `/mnt/extra-addons`, while the REAL,
    permanent, correct answer (`project_meerwerk`) was never even
    searched. The real customer codebase is checked FIRST now (the
    stable, authoritative, permanent answer whenever it exists at all),
    falling back to the transient scaffold search only when the model
    genuinely isn't a real customer model -- i.e. it's one this
    pipeline itself must have created in an earlier task.
    """
    for root in (_CUSTOM_CODEBASE_ROOT, _MODULE_DEV_ADDONS_DIR):
        found = _find_module_defining_model_under(model_name, root)
        if found:
            return found
    return None


def _find_module_defining_model_under(model_name: str, root: str) -> str | None:
    # No literal quote characters, and no backslash-escaped regex
    # metacharacters, in the grep pattern itself -- the SSH/docker-exec
    # plumbing here (_docker_exec_readonly -> bash_command!r) uses
    # Python's repr() for shell-escaping, which doubles any literal
    # backslash for its OWN display purposes; combined with bash's
    # single-quote semantics (no escape processing inside '...'), a
    # re.escape()'d '\.' arrived on the remote end as a literal '\\.'
    # -- matching a backslash character, never the real dot -- so this
    # silently matched nothing on every real test until traced down.
    # An unescaped '.' in the model name is harmless here: it just
    # matches "any character," which still matches the real literal dot
    # fine, and false positives from this are not a real risk for a
    # dotted Odoo model name in this narrow a search.
    # Double-quoted (not single-quoted, and not bare) -- confirmed live
    # this matters: bash brace expansion silently rewrites an UNQUOTED
    # {0,10} into two separate literal arguments before grep ever sees
    # it, and repr()-based escaping (see above) breaks on embedded
    # single quotes. Double quotes with no single quotes inside survive
    # this plumbing intact and still block brace expansion.
    grep_proc = _docker_exec_readonly(
        f'grep -rlE "_name.{{0,10}}{model_name}" {root} 2>/dev/null | head -1'
    )
    if grep_proc.returncode != 0 or not grep_proc.stdout.strip():
        return None
    hit_path = grep_proc.stdout.strip().splitlines()[0]
    # hit_path looks like <root>/<module_name>/models/models.py
    relative = hit_path[len(root) + 1:]
    module_name = relative.split("/", 1)[0]
    return module_name or None
