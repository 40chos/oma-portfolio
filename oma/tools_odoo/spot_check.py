"""Phase 11: the deterministic, non-LLM spot-check -- per the build
plan's own explicit instruction, this is "a separate, non-LLM piece of
code this specialist's output gets run through automatically, not
something the specialist does itself." Two real, ground-truth checks:

  - check_field_exists_on_model(): a real ORM-level read (via odoo-bin
    shell, full addons-path), never trusted from an XML-RPC read alone
    (which can't distinguish "field doesn't exist" from "field exists
    but is empty").
  - run_coverage_and_diff(): installs a module under `coverage run`
    (Python's real, standard coverage tool -- confirmed working during
    this phase's own testing: a method body never actually called
    during install shows up as genuinely uncovered lines, exactly the
    real signal needed) and reports real per-line coverage, never a
    specialist's own guess about what's tested.

compute_spot_check_mismatch() is the actual diff-vs-claim comparison:
true only when the real, measured uncovered lines include something the
self-report claimed was fine -- a specialist reporting MORE caution
than reality (over-claiming untested areas) is not a mismatch; under-
claiming (missing a real gap) is exactly the discrepancy this exists to
catch.
"""

from __future__ import annotations

import ast
import base64
import json
import re
import subprocess
from dataclasses import dataclass, field

from tools_odoo.module_dev.toolchain import (
    _FULL_ADDONS_PATH,
    _MODULE_DEV_ADDONS_DIR,
    _ODOO_BIN_PATH,
    _ODOO_CONF_PATH,
    _run_in_container,
)


class SpotCheckError(RuntimeError):
    pass


def check_field_exists_on_model(db: str, model: str, field_name: str) -> bool | None:
    """Real ORM-level proof a field exists -- via odoo-bin shell with
    the full addons-path override (a shell session without it never
    loads a custom module's own code at all and gives a false negative
    -- a real mistake made and caught during Phase 9.5's own manual
    verification). Never trusted from an XML-RPC read alone.

    P12 Tier A item 12 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
    real, confirmed asymmetry found live -- this function had NO try/except at all, unlike its
    sibling `_run_odoo_shell_script()` (which every OTHER live-registry check in this file
    already goes through), so a genuine infra failure here (SSH timeout, missing env var,
    connection refused, a non-zero-but-non-crashing odoo-bin exit) propagated as a raw,
    uncaught exception all the way up through Testing/QA's own reproduction step -- run on
    EVERY task, no opt-out -- instead of being reported as the same kind of "genuine
    uncertainty" every sibling check already handles safely. Now wraps the same failure modes
    in the same try/except discipline and returns `None` (never a guessed `False`) on any of
    them, matching `check_field_exists_on_model_fast()`'s own already-established contract one
    layer up (see `_check_field_exists_on_model()`'s own docstring for the real, confirmed
    false-negative bug that exact same "None, never guess False" discipline was built to
    prevent for the fast path).
    """
    script = (
        f"field = env[{model!r}]._fields.get({field_name!r})\n"
        f"print(f'FIELD_CHECK exists={{field is not None}}')\n"
    )
    cmd = (
        f"{_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} --addons-path={_FULL_ADDONS_PATH} "
        f"-d {db} --no-http"
    )
    import os
    import subprocess

    try:
        container = os.environ.get("OMA_ODOO_CONTAINER", "oma-odoo-1")
        proc = subprocess.run(
            ["docker", "exec", "-i", container] + cmd.split(),
            input=script,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return "FIELD_CHECK exists=True" in proc.stdout


@dataclass
class CoverageResult:
    uncovered_paths: list[str] = field(default_factory=list)  # "relative/path.py:line"
    coverage_diff: str = ""
    ran_ok: bool = True
    raw_error: str = ""


def run_coverage_and_diff(module_name: str, db: str) -> CoverageResult:
    """Installs module_name into db under `coverage run`, sourced to
    just that module's own directory, and returns the REAL per-line
    coverage result -- never a specialist's own claim about what ran.
    Requires `coverage` installed in the container (one-time `pip3
    install --user coverage`, same pattern as pylint-odoo/click-odoo-contrib
    from Phase 8/10 -- lands in the persistent /var/lib/odoo/.local).

    Real, confirmed systemic bug found live (2026-08-10, task 07141af5's flagship run,
    project_ticket_counts node, resume r185): this function's own `-i {module_name}` install
    command never included `--test-enable` (or `--test-tags`), which is the ONLY thing that
    makes Odoo actually import and execute anything under a module's `tests/` directory --
    confirmed by cross-referencing tools_odoo/module_dev/toolchain.py's OWN separately-proven
    `install_module(test_enable=True)` path, which explicitly adds `--test-enable
    --without-demo=False` for exactly this reason (see that function's own Phase 28A/2026-07-29
    comments). Without it, a plain `-i` install NEVER imports `tests/__init__.py` at all, so
    EVERY SINGLE generated test file always showed 0% coverage (every line, including its own
    top-of-file imports) on every single task, forever -- not because the test logic was
    incomplete, but because the coverage tool was never given a chance to run it in the first
    place. Confirmed live: task 07141af5's own real committed
    `tests/test_project_ticket_counts.py` (20 statements) and `tests/__init__.py` (1 statement)
    both showed 100% MISSING coverage across many separate resumes, while `models/models.py`
    showed normal PARTIAL coverage (install-time code executes, e.g. class/field registration) --
    the exact fingerprint of "tests directory never imported", not "test logic has a real gap".
    Downstream, `compute_spot_check_mismatch()` only fires when a self-report claims
    `believed_fully_covered=True` -- which is the CORRECT, honest claim for genuinely complete,
    correct code -- so this bug made every honestly-complete submission whose self-report claimed
    full coverage look like a "real, unclaimed gap" and fail the round, an unfixable-by-the-model
    dead end that could only ever be resolved by this pipeline-level fix. `--without-demo=False`
    is included for the same reason toolchain.py includes it: some real generated tests assert
    against demo data that only loads when demo data isn't suppressed.
    """
    module_dir = f"{_MODULE_DEV_ADDONS_DIR}/{module_name}"
    json_path = f"/tmp/oma_coverage_{module_name}.json"
    cmd = (
        f"export PATH=$PATH:/var/lib/odoo/.local/bin && "
        f"cd {module_dir} && "
        f"coverage run --source={module_dir} {_ODOO_BIN_PATH} -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} -i {module_name} --stop-after-init --no-http "
        f"--test-enable --without-demo=False "
        f"> /dev/null 2>&1; "
        f"coverage report -m; "
        f"coverage json -o {json_path} > /dev/null 2>&1; "
        f"cat {json_path}"
    )
    try:
        proc = _run_in_container(cmd, timeout=480)
    except subprocess.TimeoutExpired as exc:
        # Real, confirmed bug found live (2026-08-11, task 07141af5, ticket_bulk_close node):
        # `_run_in_container()` calls `subprocess.run(..., timeout=...)` directly with no
        # try/except of its own -- a genuinely slow install+test run on a busy/flaky dev host
        # (the same class of transient network issue already confirmed live elsewhere this
        # session, e.g. warm-worker ConnectionRefusedError) exceeding the 480s budget raised
        # `subprocess.TimeoutExpired` straight past this function's own body, an UNCAUGHT
        # exception that crashed the entire task ("Task crashed mid-round"), not merely this
        # one round -- exactly the same robustness gap already fixed once this session for a
        # different validator's own unhandled `SyntaxError`. This function's own established
        # contract is to NEVER raise, always return a real, honest `CoverageResult` even for
        # other real failure modes (empty output, unparseable JSON) -- a subprocess timeout
        # deserves the identical treatment, not an unhandled crash.
        return CoverageResult(ran_ok=False, raw_error=f"coverage run timed out after {exc.timeout}s")
    if not proc.stdout.strip():
        return CoverageResult(ran_ok=False, raw_error=f"coverage produced no output: {proc.stderr[-1000:]}")

    # The human-readable `coverage report -m` text comes first, then the
    # JSON report -- split on the JSON's own opening brace, the one
    # reliable marker between the two (module names could contain
    # anything, so this can't be split on a fixed line count).
    json_start = proc.stdout.find('{"meta"')
    if json_start == -1:
        return CoverageResult(ran_ok=False, raw_error=f"could not find JSON coverage report in output: {proc.stdout[-1000:]!r}")

    coverage_diff = proc.stdout[:json_start].strip()
    try:
        report = json.loads(proc.stdout[json_start:])
    except json.JSONDecodeError as exc:
        return CoverageResult(ran_ok=False, raw_error=f"coverage JSON did not parse: {exc}")

    uncovered_paths = []
    for path, file_data in report.get("files", {}).items():
        if path == "__manifest__.py":
            continue  # a plain dict literal, never "executed" in the code sense -- not a real gap
        for line in file_data.get("missing_lines", []):
            uncovered_paths.append(f"{path}:{line}")

    return CoverageResult(uncovered_paths=uncovered_paths, coverage_diff=coverage_diff)


def compute_spot_check_mismatch(
    claimed_uncovered_paths: list[str], real_uncovered_paths: list[str],
    believed_fully_covered: bool = False,
) -> bool:
    """The actual diff-vs-claim comparison, per the technical document's
    §9 spot_check_mismatch field. True only when reality found a real
    gap (`real_uncovered_paths`) the claim didn't already own up to --
    a specialist being MORE conservative than reality (claiming extra
    untested areas) is not a mismatch worth flagging.

    Real, confirmed structural bug found live (2026-08-06, fix-pass task 005): the original
    implementation did a literal `set(real) - set(claimed)` diff between `real_uncovered_paths`
    (exact `"file.py:line"` strings from Python's own `coverage` tool) and `claimed_uncovered_
    paths` (free-text prose -- confirmed by reading `_self_report_coverage()`'s own prompt in
    specialists/testing_qa/specialist.py, which explicitly asks the model to "List any such paths
    as best you can describe them, EVEN APPROXIMATELY", inviting sentences like "the onchange
    logic ... is not executed during installation", never a `"file:line"` string). These two
    formats can never set-intersect -- meaning this function returned True (a "mismatch")
    whenever `real_uncovered_paths` was non-empty at all, REGARDLESS of what the self-report
    said, even when it correctly and honestly disclosed the exact same gap in prose. Confirmed
    live: task 005 (an `@api.onchange` fix) generated genuinely correct code, Code-Review found
    no blocking issues, self_report.believed_fully_covered was correctly `False` with an accurate
    prose explanation naming the exact onchange method -- and this function still reported a
    mismatch and failed the round, purely on string-format grounds.

    This exact class of bug was ALREADY found 3 times before (see
    `_exempt_verified_sum_compute_idiom_from_coverage_gap`/`_exempt_verified_onchange_idiom_
    from_coverage_gap`/`_exempt_verified_own_field_compute_idiom_from_coverage_gap` in
    specialists/testing_qa/specialist.py) but only ever patched reactively, per specific rigid
    goal-text shape (a "Method name: X" / "Trigger field: Y" structured phrasing) -- confirmed
    live that `_exempt_verified_onchange_idiom_from_coverage_gap`'s OWN docstring cites "task
    005's own resubmission" as its motivating case, yet still doesn't cover task 005's REAL,
    natural-language goal text, because that goal was never phrased in the rigid structured shape
    the regex requires. Fixed here at the actual root instead: a self-report that HONESTLY
    discloses incomplete coverage (`believed_fully_covered=False`) has already done its job --
    per this file's own header comment, "under-claiming (missing a real gap) is exactly the
    discrepancy this exists to catch," and a report that already says "not fully covered" is
    definitionally not under-claiming. Only a report that claims FULL coverage
    (`believed_fully_covered=True`) while reality disagrees is a genuine, meaningful mismatch --
    the existing per-shape exemption functions remain as a real, still-useful backstop for
    exactly that overconfident-claim case, layered underneath this general fix, not replaced by
    it.
    """
    if not real_uncovered_paths:
        return False
    return believed_fully_covered


# Real, general, conceptual fix found live (2026-07-17, Phase 20 Area 2,
# investigating task #43 which "passed" -- reproduction_confirmed=True,
# spot_check_mismatch=False -- yet the real committed module granted
# `base.group_user` full access instead of creating the NEW group the
# goal explicitly asked for, with delete access silently missing too.
# Root cause: this whole file's own verification methodology only ever
# checks (a) does ONE named field exist on ONE named model, and (b) did
# every Python line execute during install -- NEITHER check has any
# mechanism to verify a security GROUP was actually created, that its
# PERMISSIONS match what the goal asked for, or that a field's
# visibility was actually RESTRICTED to the right group. This is not a
# one-off Testing/QA bug, it's a structural gap in what "passed" has
# ever meant for the entire access-rules category of tasks -- these
# three functions are the general, conceptual fix: real ORM-level
# ground truth for the three claim shapes Area 2's tasks actually make
# (a group exists; a group has specific CRUD access to a model; a
# field's visibility is restricted to a specific group), reusable by
# ANY future task shape that makes one of these same claims, not
# hand-coded per task.
def _run_odoo_shell_script(db: str, script: str, timeout: int = 90) -> str | None:
    """Shared plumbing for the three checks below -- base64-encodes the
    script (avoids shell-quoting hazards for names containing quotes/
    special characters) and runs it via odoo-bin shell with the full
    addons-path override, same discipline as every other live-registry
    check in this project. Returns raw stdout, or None on any failure
    (timeout, nonzero exit) -- callers must never guess on None.
    """
    encoded = base64.b64encode(script.encode()).decode()
    cmd = (
        f"echo {encoded} | base64 -d | {_ODOO_BIN_PATH} shell -c {_ODOO_CONF_PATH} "
        f"--addons-path={_FULL_ADDONS_PATH} -d {db} --no-http"
    )
    try:
        proc = _run_in_container(cmd, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def resolve_group_xmlid_to_display_name(db: str, group_token: str) -> str | None:
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_access_restriction
    node): a goal frequently names a security group by its snake_case technical identifier
    (e.g. "group_field_technician", matching Odoo's own `res.groups` XML-ID convention) rather
    than its human-readable display name ("Field Technician") -- but `check_group_exists()`/
    `check_group_model_access()` above only ever match against the translated display name
    field, so a goal-token lookup against a real, genuinely-existing group silently reports
    "does not exist" purely because of this naming-convention mismatch. Resolves a bare or
    module-qualified XML-ID token (e.g. "group_field_technician" or
    "my_module.group_field_technician") to the real group's own display name via `ir_model_data`,
    so a caller holding an XML-ID-style token can look it up the same way a caller holding an
    actual display name already does. Returns None (never guessed) on any uncertainty or if no
    matching record exists -- same conservative posture as every other check in this file.
    """
    module_part, _, name_part = group_token.rpartition(".")
    name_part = name_part or group_token
    script = (
        "env.cr.execute(\n"
        "    \"SELECT g.name->>'en_US' FROM ir_model_data d \"\n"
        "    \"JOIN res_groups g ON g.id = d.res_id \"\n"
        "    \"WHERE d.model = 'res.groups' AND d.name = %s\"\n"
        f"    + (\" AND d.module = %s\" if {module_part!r} else \"\"),\n"
        f"    ({name_part!r},) + (({module_part!r},) if {module_part!r} else ())\n"
        ")\n"
        "row = env.cr.fetchone()\n"
        "print('GROUP_XMLID_RESOLVE found=' + (row[0] if row else 'NONE'))\n"
    )
    out = _run_odoo_shell_script(db, script)
    if out is None:
        return None
    for line in out.splitlines():
        if line.startswith("GROUP_XMLID_RESOLVE found="):
            value = line[len("GROUP_XMLID_RESOLVE found="):]
            return None if value == "NONE" else value
    return None


def check_group_exists(db: str, group_name: str) -> bool | None:
    """Real ORM/DB-level proof a res.groups record with this exact
    display name exists -- returns None (never a guessed False) on any
    uncertainty, same conservative posture as every other live-registry
    check in this project.
    """
    script = (
        f"env.cr.execute(\"SELECT id FROM res_groups WHERE name->>'en_US' = %s\", ({group_name!r},))\n"
        "print('GROUP_CHECK found=' + str(bool(env.cr.fetchone())))\n"
    )
    out = _run_odoo_shell_script(db, script)
    if out is None:
        return None
    if "GROUP_CHECK found=True" in out:
        return True
    if "GROUP_CHECK found=False" in out:
        return False
    return None


def check_group_model_access(
    db: str, group_name: str, model: str,
    expects_read: bool | None, expects_write: bool | None,
    expects_create: bool | None, expects_unlink: bool | None,
) -> tuple[bool, str] | None:
    """Real ORM/DB-level proof of what a group can ACTUALLY do on a
    model, compared against what the task's own goal claimed it should
    be able to do -- catches the exact real bug found live: a module
    reporting "passed" while granting the wrong group, or the right
    group but missing an explicitly-requested permission (e.g. delete).
    Only compares the expects_* values the caller actually provides
    (None means "the goal didn't make a claim about this permission,
    don't check it"). Aggregates multiple real ir.model.access rows for
    the same group+model with OR, matching Odoo's own real semantics
    (any one grant is enough). Returns None on any genuine uncertainty.
    """
    script = (
        "env.cr.execute(\n"
        "    \"SELECT a.perm_read, a.perm_write, a.perm_create, a.perm_unlink \"\n"
        "    \"FROM ir_model_access a JOIN res_groups g ON g.id = a.group_id \"\n"
        "    \"JOIN ir_model m ON m.id = a.model_id \"\n"
        "    \"WHERE g.name->>'en_US' = %s AND m.model = %s\",\n"
        f"    ({group_name!r}, {model!r}),\n"
        ")\n"
        "print('ACCESS_CHECK rows=' + str(env.cr.fetchall()))\n"
    )
    out = _run_odoo_shell_script(db, script)
    if out is None:
        return None
    marker = "ACCESS_CHECK rows="
    line = next((l for l in out.splitlines() if l.startswith(marker)), None)
    if line is None:
        return None
    try:
        rows = ast.literal_eval(line[len(marker):])
    except (ValueError, SyntaxError):
        return None
    if not rows:
        return False, f"group {group_name!r} has NO access rows at all for model {model!r}"

    actual_read = any(r[0] for r in rows)
    actual_write = any(r[1] for r in rows)
    actual_create = any(r[2] for r in rows)
    actual_unlink = any(r[3] for r in rows)

    mismatches = []
    for label, expected, actual in (
        ("read", expects_read, actual_read),
        ("write", expects_write, actual_write),
        ("create", expects_create, actual_create),
        ("delete", expects_unlink, actual_unlink),
    ):
        if expected is not None and expected != actual:
            mismatches.append(f"{label}: expected {expected}, actual {actual}")
    if mismatches:
        return False, f"group {group_name!r} on model {model!r} permission mismatch: {'; '.join(mismatches)}"
    return True, f"group {group_name!r} on model {model!r} matches all claimed permissions"


def check_menu_exists(
    db: str, menu_name: str, parent_menu_name: str | None = None, action_model: str | None = None,
) -> tuple[bool, str] | None:
    """Real, confirmed gap found live (2026-07-28, Phase 28C,
    `school_student` task, closing the `menu_action_placement` entry
    in `contracts/verifier_registry.py`, `status="unverified"` since
    Phase 25E): the ONLY reproduction check this whole specialist had
    -- `check_field_exists_on_model()` -- can only ever prove or
    disprove an ORM FIELD's existence. A goal whose own constraint is
    "Menu structure: School -> Students" makes no field claim at all;
    forcing the extraction into `ReproductionTarget`'s own
    `{model, field_name}` shape produces a `field_name` like
    "menu_structure" itself, which is not a real field and can NEVER
    exist on any model -- an UNWINNABLE check for ANY module content,
    correct or not. Confirmed live as a real, repeated, non-convergent
    failure: a genuinely correct, fully-wired menu (root menu, child
    menu, real ir.actions.act_window record) was reported as "failed"
    round after round, purely because nothing ever checked the real
    thing the goal actually claimed.

    Real, direct ORM-level proof a `ir.ui.menu` record with this exact
    display name exists -- and, when given, that its real parent menu
    matches by name too (a menu named "Students" nested under some
    OTHER unrelated top-level menu is not what the goal claimed), and
    that its own window action's `res_model` matches the claimed
    target model (a menu that resolves to the wrong model's data is a
    real, different bug this alone would otherwise miss). Every
    parameter beyond `menu_name` is optional and only checked when the
    caller actually has a claim about it -- mirrors `check_group_
    model_access()`'s own "only compare what was actually claimed"
    discipline directly above.

    Returns None on any genuine uncertainty (SSH hiccup, timeout,
    unparseable output) -- same conservative posture as every other
    live-registry check in this project; a caller must never guess on
    None.

    Real, confirmed bug found live (2026-08-10, task e65381cc, equipment_views_menu node,
    deterministically reproduced EVERY single round since round 1 -- confirmed via a direct
    SSH+odoo-shell repro, not guessed), TWO distinct SQL defects stacked:

    1. `ir_ui_menu.action` is a real Odoo `fields.Reference` column, stored as PLAIN TEXT in
       Postgres (e.g. `'ir.actions.act_window,45'`), never a bare integer FK -- so
       `a.id = m.action` always raised `operator does not exist: integer = character varying`.
    2. `ir_actions` is the polymorphic BASE table Odoo's own `_inherits`-style table inheritance
       uses for every action type -- it has no `res_model` column at all; that column only
       exists on the CHILD table `ir_act_window` (window actions specifically, the only kind a
       menu's own `action_model` claim ever means in practice).

    Both raised a hard SQL error (not a real "not found" result), which
    `_run_odoo_shell_script()`'s own conservative "nonzero exit -> None" contract correctly
    surfaced as genuine uncertainty every single time, for EVERY menu with any action at all,
    regardless of whether the real menu was correct. Fixed by parsing the reference column's own
    `'<model>,<id>'` text shape with `split_part(...)::integer` (exactly the way Odoo's own ORM
    resolves a Reference field internally) and joining against `ir_act_window` directly.
    """
    script = (
        "env.cr.execute(\n"
        "    \"SELECT m.id, pm.name->>'en_US' AS parent_name, aw.res_model \"\n"
        "    \"FROM ir_ui_menu m \"\n"
        "    \"LEFT JOIN ir_ui_menu pm ON pm.id = m.parent_id \"\n"
        "    \"LEFT JOIN ir_act_window aw ON m.action IS NOT NULL \"\n"
        "    \"AND aw.id = split_part(m.action, ',', 2)::integer \"\n"
        "    \"WHERE m.name->>'en_US' = %s\",\n"
        f"    ({menu_name!r},),\n"
        ")\n"
        "print('MENU_CHECK rows=' + str(env.cr.fetchall()))\n"
    )
    out = _run_odoo_shell_script(db, script)
    if out is None:
        return None
    marker = "MENU_CHECK rows="
    line = next((l for l in out.splitlines() if l.startswith(marker)), None)
    if line is None:
        return None
    try:
        rows = ast.literal_eval(line[len(marker):])
    except (ValueError, SyntaxError):
        return None
    if not rows:
        return False, f"no ir.ui.menu record named {menu_name!r} exists in the real registry"

    if parent_menu_name is not None:
        matching = [r for r in rows if r[1] == parent_menu_name]
        if not matching:
            actual_parents = sorted({r[1] for r in rows})
            return False, (
                f"menu {menu_name!r} exists but its real parent is {actual_parents!r}, "
                f"not the claimed {parent_menu_name!r}"
            )
        rows = matching

    if action_model is not None:
        matching = [r for r in rows if r[2] == action_model]
        if not matching:
            actual_models = sorted({r[2] for r in rows if r[2] is not None})
            return False, (
                f"menu {menu_name!r} exists but its real window action targets "
                f"{actual_models!r}, not the claimed {action_model!r}"
            )

    return True, f"menu {menu_name!r} genuinely exists in the real registry, matching every claimed detail"


def check_field_group_restricted(
    db: str, model: str, field_name: str, group_name: str | None = None, group_xmlid: str | None = None,
) -> tuple[bool, str] | None:
    """Real ORM-level proof a field's visibility is actually restricted
    to (at least) the named group.

    Phase 26A follow-up (2026-07-27): `group_xmlid` param added for
    consistency with the fast-path sibling
    (tools_odoo.odoo_schema_client.check_field_group_restricted_fast) --
    see that function's own docstring for the real, live-confirmed gap
    this closes (xmlid-preference matching existed for the BUTTON
    restriction check since Phase 25F/fix 40, but was never propagated
    to this, its field-restriction sibling, until found live testing
    Phase 26A). Matches on the raw xmlid FIRST (more reliable for a
    well-known base group whose display name may not resemble how the
    goal describes it), falling back to resolved-display-name matching
    exactly as before.

    Real, confirmed bug found live (2026-07-19, Phase 20 Area 2) fixing
    the FIRST version of this function: it queried
    `ir_model_fields_group_rel` -- but Odoo's OWN source code
    (`odoo/addons/base/models/ir_model.py`) labels that exact field
    `# CLEANME unimplemented field (empty table)`. Confirmed directly
    against the real running Odoo instance: a field correctly defined
    with `fields.Date(groups='module.group_xmlid')` installs cleanly,
    and the field's own in-memory `.groups` attribute is correctly set
    -- but `ir_model_fields_group_rel` has ZERO rows for it, always,
    confirming Odoo genuinely never writes to that table. The real
    enforcement mechanism, confirmed by reading `odoo/models.py`
    directly (`check_field_access_rights()`,
    `self.user_has_groups(field.groups)`): Odoo checks the field
    object's own `.groups` attribute (a comma-separated string of
    xmlids) IN MEMORY, at read/write time -- there is no DB table to
    query at all. Fixed: reads the real field object's `.groups`
    attribute via the ORM directly (same technique as
    check_field_exists_on_model), resolving each xmlid to its group's
    real display name for a readable comparison. Returns None on any
    genuine uncertainty.
    """
    script = (
        f"field = env[{model!r}]._fields.get({field_name!r})\n"
        "groups_str = (getattr(field, 'groups', None) or '') if field is not None else None\n"
        "if field is None:\n"
        "    print('FIELD_GROUPS=NONE')\n"
        "else:\n"
        "    xmlids = [g.strip() for g in groups_str.split(',') if g.strip()]\n"
        "    names = []\n"
        "    for xmlid in xmlids:\n"
        "        try:\n"
        "            names.append(env.ref(xmlid).name)\n"
        "        except Exception:\n"
        "            names.append(xmlid)\n"
        "    print('FIELD_GROUP_XMLIDS=' + str(xmlids))\n"
        "    print('FIELD_GROUPS=' + str(names))\n"
    )
    out = _run_odoo_shell_script(db, script)
    if out is None:
        return None
    xmlid_marker = "FIELD_GROUP_XMLIDS="
    xmlid_line = next((l for l in out.splitlines() if l.startswith(xmlid_marker)), None)
    marker = "FIELD_GROUPS="
    line = next((l for l in out.splitlines() if l.startswith(marker)), None)
    if line is None:
        return None
    payload = line[len(marker):]
    if payload == "NONE":
        return None  # field itself doesn't exist -- a different check's job, not a real restriction answer
    try:
        restricting_groups = ast.literal_eval(payload)
        restricting_xmlids = ast.literal_eval(xmlid_line[len(xmlid_marker):]) if xmlid_line else []
    except (ValueError, SyntaxError):
        return None
    if not restricting_groups:
        return False, f"{model}.{field_name} has NO group restriction at all -- visible to everyone"
    if group_xmlid and group_xmlid in restricting_xmlids:
        return True, f"{model}.{field_name} is correctly restricted to {group_xmlid!r}"
    if group_name and group_name in restricting_groups:
        return True, f"{model}.{field_name} is correctly restricted to {group_name!r}"
    claimed = group_xmlid or group_name
    return False, (
        f"{model}.{field_name} is restricted to {restricting_groups!r}, "
        f"not the claimed group {claimed!r}"
    )


def check_button_group_restricted(
    db: str, model: str, button_name: str, group_xmlid: str | None = None, group_name: str | None = None,
) -> tuple[bool, str] | None:
    """P12 Tier A item 12 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    §4): the missing odoo-bin-shell fallback for `check_button_group_restricted_fast()`
    (`tools_odoo/odoo_schema_client.py`) -- real, confirmed gap: every OTHER security-claim
    check in `specialists/testing_qa/specialist.py`'s `_verify_security_access_claim()`
    (`_field_group_restricted`, `_group_exists`, `_group_model_access`) already has this exact
    fast-then-shell-fallback pattern; the button-restriction check alone just returned `None`
    unconditionally whenever the fast path wasn't eligible or came back uncertain, so the
    check could never pass at all on a non-fast-path DB, regardless of correctness -- the same
    narrow-but-real failure shape as Bug #2, one claim-type wide.

    Uses `env[model].get_views([[False, 'form']], {})` + `ir.ui.view.read_combined()` in the
    shell script, deliberately NOT the simpler `get_views()`-resolved arch alone -- mirrors the
    fast path's own real, live-confirmed fix (see `check_button_group_restricted_fast()`'s own
    docstring): `get_views()`'s resolved arch is post-processed for the CALLING context's own
    permissions, and odoo-bin shell's superuser context has every permission there is, so a
    genuine `groups="..."` restriction would be silently stripped before this function ever
    saw it. `read_combined()` returns the fully-inherited arch as actually DECLARED, without
    that per-viewer ACL postprocessing.
    """
    from tools_odoo.odoo_schema_client import _BUTTON_TAG_RE_TEMPLATE, _GROUPS_ATTR_RE

    script = (
        f"views = env[{model!r}].get_views([[False, 'form']], {{}})\n"
        "view_id = (views.get('views') or {}).get('form', {}).get('id')\n"
        "if not view_id:\n"
        "    print('BUTTON_CHECK_ARCH=NONE')\n"
        "else:\n"
        "    combined = env['ir.ui.view'].browse(view_id).read_combined()\n"
        "    print('BUTTON_CHECK_ARCH=' + repr(combined.get('arch') or ''))\n"
    )
    out = _run_odoo_shell_script(db, script)
    if out is None:
        return None
    marker = "BUTTON_CHECK_ARCH="
    line = next((l for l in out.splitlines() if l.startswith(marker)), None)
    if line is None:
        return None
    payload = line[len(marker):]
    if payload == "NONE":
        return None  # no form view resolvable -- a different check's job, not a real restriction answer
    try:
        arch = ast.literal_eval(payload)
    except (ValueError, SyntaxError):
        return None
    tag_match = re.search(_BUTTON_TAG_RE_TEMPLATE.format(name=re.escape(button_name)), arch)
    if not tag_match:
        return None  # button itself not found in the resolved arch -- a different check's job
    groups_match = _GROUPS_ATTR_RE.search(tag_match.group(0))
    groups_str = groups_match.group(1) if groups_match else ""
    xmlids = [g.strip() for g in groups_str.split(",") if g.strip()]
    if not xmlids:
        return False, f"button {button_name!r} on {model!r} has NO group restriction at all -- visible to everyone"
    if group_xmlid and group_xmlid in xmlids:
        return True, f"button {button_name!r} on {model!r} is correctly restricted to {group_xmlid!r}"
    names_script = (
        "names = []\n"
        f"for xmlid in {xmlids!r}:\n"
        "    try:\n"
        "        names.append(env.ref(xmlid).name)\n"
        "    except Exception:\n"
        "        names.append(xmlid)\n"
        "print('BUTTON_CHECK_NAMES=' + str(names))\n"
    )
    names_out = _run_odoo_shell_script(db, names_script)
    names_marker = "BUTTON_CHECK_NAMES="
    names_line = next((l for l in (names_out or "").splitlines() if l.startswith(names_marker)), None)
    try:
        names = ast.literal_eval(names_line[len(names_marker):]) if names_line else xmlids
    except (ValueError, SyntaxError):
        names = xmlids
    if group_name and group_name in names:
        return True, f"button {button_name!r} on {model!r} is correctly restricted to {group_name!r}"
    return False, (
        f"button {button_name!r} on {model!r} is restricted to {xmlids!r} ({names!r}), not the "
        f"claimed group {(group_xmlid or group_name)!r}"
    )


def run_behavioral_probe(
    db: str,
    creates: list[tuple[str, dict]],
    check_fields: list[str],
    target_index: int = 0,
) -> dict | None:
    """Phase 30, P1c (Phase I, §12): the real, deterministic execution
    half of the new behavioral probe -- closes the gap where "verified"
    meant only "the field exists," not "the field does what the goal
    said." Creates one or more real records (in the given order, so a
    parent can be created before children that reference it) with the
    given field VALUES, then reads back `check_fields` from the record
    at `creates[target_index]` -- the one whose real, post-write state
    is the actual thing being verified (e.g. a parent order's own
    computed `amount_total` after its lines were created).

    `creates` is a list of `(model, values)` pairs. Any value equal to
    the literal string `"$0"`, `"$1"`, ... is replaced with the REAL id
    of the record already created at that earlier index -- the one
    general mechanism needed for "create a parent, then children that
    reference it," without inventing a second, different placeholder
    scheme per caller.

    Deliberately narrow, matching this file's own established
    discipline (`check_group_exists`/`check_menu_exists`/etc.): every
    value that reaches the real Odoo shell is a `repr()`'d Python
    literal built from already-validated, JSON-safe caller data --
    never raw LLM-authored code executed directly. Returns None (never
    a guessed/fabricated result) on any uncertainty: a create that
    fails, a shell timeout, or output that doesn't parse as the
    expected marker.
    """
    if not creates or not (0 <= target_index < len(creates)):
        return None
    lines: list[str] = ["import json"]
    for i, (model, values) in enumerate(creates):
        resolved_values = {}
        for key, value in values.items():
            if isinstance(value, str) and len(value) >= 2 and value[0] == "$" and value[1:].isdigit():
                ref_index = int(value[1:])
                if ref_index >= i:
                    return None  # a forward/self reference can never be real -- refuse rather than guess
                resolved_values[key] = f"__record_{ref_index}.id"
            else:
                resolved_values[key] = repr(value)
        values_src = "{" + ", ".join(f"{k!r}: {v}" for k, v in resolved_values.items()) + "}"
        lines.append(f"__record_{i} = env[{model!r}].create({values_src})")
    target_model = creates[target_index][0]
    lines.append(f"__target = env[{target_model!r}].browse(__record_{target_index}.id)")
    lines.append("__target.invalidate_recordset()")  # force a fresh read, never a stale in-memory compute cache
    fields_src = repr(check_fields)
    lines.append(f"__result = {{f: __target[f] for f in {fields_src}}}")
    lines.append(
        "print('BEHAVIORAL_PROBE_RESULT:' + json.dumps("
        "{k: (str(v) if not isinstance(v, (int, float, bool, str, type(None))) else v) "
        "for k, v in __result.items()}))"
    )
    script = "\n".join(lines) + "\n"
    out = _run_odoo_shell_script(db, script, timeout=90)
    if out is None:
        return None
    marker = "BEHAVIORAL_PROBE_RESULT:"
    line = next((l for l in out.splitlines() if l.startswith(marker)), None)
    if line is None:
        return None
    try:
        return json.loads(line[len(marker):])
    except json.JSONDecodeError:
        return None
