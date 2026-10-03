"""The Testing/QA specialist -- Phase 11. The independent verification
specialist that actually closes the loop on tasks 1, 2, and 3: a task
is never done just because the Build specialist says so. Authored from
scratch, per the technical document's own honest flag that there's no
existing AI-agent pattern for this specific piece.

Model cascade (per §0.5.2, revised 2026-07-13): both the routine and
escalation tiers now use the single shared reasoning model,
`qwen3.6-27b` (GPU Worker 02, permanently resident) -- `qwen3-14b` and
`deepseek-r1-distill-qwen-32b` are retired. Escalation is still called
with a plain client.generate() + strip_think_block(), never
generate_checked(), specifically because that escalation prompt leaves
thinking mode ON (no `no_think`) to get real deep reasoning about the
failure's root cause -- generate_checked()'s </think>-completeness check
exists for the routine, no_think-suffixed calls elsewhere in this file,
not this one.

The deterministic spot-check (tools_odoo/spot_check.py) is deliberately
NOT something this specialist's own judgment performs -- it is a
separate, non-LLM piece of code this specialist's own self-report gets
run through automatically, per the build plan's explicit instruction.
This specialist produces an honest claim about what it believes is
covered; the real coverage.py-backed check is what actually decides
spot_check_mismatch.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from pydantic import BaseModel

from contracts.schema import SpecialistOutput, TaskContract
from contracts.verifier_registry import unverified_shapes_targeted
from infra.gateway_client import ModelGatewayClient
from infra.structured_output import call_structured, strip_think_block
from manager.step_scheduler import Step, run_steps
from manager.trace import publish_trace_event
from paths import skill_path
from specialists.constitution import ConstitutionNotFoundError, load_odoo_development_constitution
from tools_odoo.codebase_read import CodebaseReadError, read_module_files
from tools_odoo.module_dev.toolchain import get_model_fields
from tools_odoo.odoo_schema_client import is_fast_path_eligible
from tools_odoo.spot_check import (
    check_button_group_restricted,
    check_field_exists_on_model,
    check_field_group_restricted,
    check_group_exists,
    check_group_model_access,
    check_menu_exists,
    compute_spot_check_mismatch,
    resolve_group_xmlid_to_display_name,
    run_behavioral_probe,
    run_coverage_and_diff,
)


def _check_field_exists_on_model(db: str, model: str, field_name: str, task_id: str | None) -> bool | None:
    """Phase 21 (2026-07-22): dispatch helper -- the single highest-
    impact remaining conversion, found live investigating why the
    "Reproducing X" step (runs on EVERY task, no opt-out) still showed
    an unexplained ~15.7s gap despite every OTHER Build/Testing-QA
    fast-path conversion already landing. check_field_exists_on_model()
    was simply never in the original 8-function list. Routes to the
    XML-RPC fast path (0.2-0.8s) when task_id is available, falling
    back to the original, universal odoo-bin-shell version whenever
    the fast path can't get a confident answer.

    Real, confirmed false-negative bug found live (2026-07-22) BEFORE
    this ever reached production: the fast path authenticates as a
    fixed "Admin" login, confirmed to exist on odoo16_dev (the real
    dev target) but NOT guaranteed on a fresh/duplicate/sandbox test
    database -- the original shell version needs no login at all
    (superuser `odoo-bin shell` context), so it works on ANY database.
    check_field_exists_on_model_fast() now returns None (never a
    guessed False) when it can't even authenticate -- this dispatcher
    must fall back on None, never treat it as "confirmed does not
    exist." A live test caught this exact bug: a genuinely-installed
    field on a non-odoo16_dev database was wrongly reported missing
    before this fallback was added.
    """
    if task_id and is_fast_path_eligible(db):
        from tools_odoo.odoo_schema_client import check_field_exists_on_model_fast

        result = check_field_exists_on_model_fast(db, model, field_name)
        if result is not None:
            return result
    # P12 Tier A item 12: check_field_exists_on_model() (the slow-path fallback) can now also
    # return None on a genuine infra failure (SSH timeout, missing env var, etc.) instead of
    # raising -- passed through here unchanged; callers must apply the same "None is genuine
    # uncertainty, never a confirmed absence" discipline already established for the fast path.
    return check_field_exists_on_model(db, model, field_name)

_SKILL_PATH = skill_path("odoo-verification-and-reproduction")
_ROUTINE_MODEL_ENV = "OMA_MODEL_CLASSIFIER"  # qwen3.6-27b -- the one shared reasoning tier
_ROUTINE_MODEL_FALLBACK = "qwen3.6-27b"
_ESCALATION_MODEL_ENV = "OMA_MODEL_TESTING_QA_ESCALATION"
_ESCALATION_MODEL_FALLBACK = "qwen3.6-27b"
# Real infra change, 2026-07-22: GPU Worker 03's resident Qwen3-9B
# (enable_thinking disabled server-side, unlike qwen3.6-27b) -- live-
# benchmarked the same night against a realistic extraction-shaped
# prompt: 1.7-2.2s with correct output, versus a real, measured 100.7s
# outlier for the equivalent call on the shared reasoning tier. Used
# ONLY for _extract_reproduction_target()/_extract_security_access_
# claim() below -- pure, short, structured extraction from already-
# known text, exactly the shape this was benchmarked against. NOT used
# for self-reported coverage (a judgment call, not pure extraction) or
# anything Manager-side -- see infra/gateway_client.py's own
# BACKEND_FAST_EXTRACTION comment for the full rationale and why this
# is deliberately narrow, not a blanket replacement of the shared
# reasoning tier.
_FAST_EXTRACTION_MODEL_ENV = "OMA_MODEL_TESTING_QA_FAST_EXTRACTION"
_FAST_EXTRACTION_MODEL_FALLBACK = "qwen3-9b-fast-extraction"

_VERIFY_PREFIX = "verify_module:"
_COLLISION_CONFIRMED_FIELDS_PREFIX = "collision_confirmed_fields:"


def _collision_confirmed_field_names_from_inputs(inputs: list[str]) -> list[str]:
    """Real, confirmed gap found live (2026-08-06, fix-pass task 004): Build's own collision-
    autofix (specialists/build/specialist.py) correctly strips a redundant new-field declaration
    when the field already exists for real on the live target model, and records which field(s)
    via a marker in `generated.notes` -- manager/tools.py's `await_verification()` threads that
    confirmation into `contract.inputs` as a `collision_confirmed_fields:['x', 'y']` entry (see
    its own `_extract_collision_confirmed_field_names()` docstring for the full chain). This is
    the consumer side: parses that entry back into a plain field-name list, or [] if absent.
    """
    for entry in inputs:
        if entry.startswith(_COLLISION_CONFIRMED_FIELDS_PREFIX):
            raw = entry[len(_COLLISION_CONFIRMED_FIELDS_PREFIX):]
            return re.findall(r"'([^']*)'", raw)
    return []

# Mirrors manager/loop.py's own _NOT_YET_IN_SCOPE_RE exactly -- see the
# security-claim-check guard below (run()) for why this specialist
# needs to detect decomposition itself rather than importing the
# manager's own regex (specialists never import from manager).
_NOT_YET_IN_SCOPE_RE = re.compile(r"NOT yet in scope for this round[^:]*:\s*\[(.*?)\]")
_MODEL_FIELD_ASSIGN_RE = re.compile(r"^\s{4,8}(\w+)\s*=\s*fields\.\w+\(", re.MULTILINE)
# Real gap found live (2026-08-06, fix-pass task048): matches a real Odoo dotted model name
# (e.g. "project.satisfaction", "project.project") appearing literally in free-text goal
# prose -- used by `_autocorrect_hallucinated_reproduction_target()`'s own cross-model tier
# below to find OTHER models the goal names, when the extraction call attributes a real field
# to the wrong one of several models a multi-model goal mentions.
_DOTTED_MODEL_NAME_IN_TEXT_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b")


def _load_constitution_text_or_none() -> str | None:
    """P12 Tier A item 24 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    §4): real, confirmed gap -- Code-Review is the ONLY specialist that reads the Odoo
    Development Agent Constitution at all (`load_odoo_development_constitution()`); Testing/QA's
    own self-report judgment (`_self_report_coverage()` below -- "do you believe this change is
    fully covered," a real judgment call, not pure extraction) had no access to the same real
    standard Build/Code-Review both check work against. Reads the exact same function Code-Review
    already uses (never a second, drifting copy of the loading logic) -- always fresh, per that
    function's own docstring ("an edit Operator makes takes effect immediately"). Gracefully degrades
    to None on `ConstitutionNotFoundError` -- this is a real, informational enrichment of the
    self-report prompt, never something that should block or crash Testing/QA's own verification
    step if the document happens to be temporarily missing (same "never let a nice-to-have block
    anything real" posture as every other optional-context fold-in in this codebase).
    """
    try:
        return load_odoo_development_constitution()
    except ConstitutionNotFoundError:
        return None


_SEQUENCE_PATTERN_NAME_FIELD_RE = re.compile(
    r"\bname\s*=\s*fields\.Char\([^)]*default\s*=\s*['\"]New['\"]", re.DOTALL
)
_SEQUENCE_PATTERN_CREATE_RE = re.compile(r"ir\.sequence.{0,10}\]\.next_by_code")
_SEQUENCE_PATTERN_NEW_CHECK_RE = re.compile(r"==\s*['\"]New['\"]")
# Real, confirmed gap found live (2026-08-08, the "flagship field-service" task's own
# `equipment_registry` node, escalated after 2 identical, non-convergent rounds): this whole
# exemption is hardcoded to Odoo's ONE classic textbook idiom -- a `name` field defaulting to
# the literal string `'New'`, checked via `== 'New'`. A field with any OTHER name (here,
# `tracking_number`, per the goal's own explicit field name) auto-assigned via the equally
# correct, equally standard `if 'X' not in vals: vals['X'] = ...next_by_code(...)` guard never
# matched any of the three patterns above, so the exemption never fired -- even though a REAL
# behavioral probe (an actual `create()` call, read back for real) had ALREADY, independently,
# dynamically confirmed the exact same code correctly auto-generated a real, non-empty tracking
# number (`{'tracking_number': 'EQ-00001'}`). The static coverage tool's own structural blind
# spot (never calls `create()` at install time, so this method's own body always shows
# "uncovered" regardless of correctness -- see this function's own docstring above) still won
# over a REAL, dynamic, already-passing proof of correctness, purely because the source-shape
# pattern-matcher was too narrow to recognize a DIFFERENT, equally valid field name/guard shape.
# These two general patterns tie a guard to its assignment via the SAME captured field name --
# `_general_sequence_idiom_field_names()` below only accepts a match when both halves name the
# identical field, so an unrelated guard/assignment pair elsewhere in the same method can never
# accidentally exempt real, uncovered, incorrect content.
_SEQUENCE_PATTERN_GENERAL_GUARD_RE = re.compile(
    r"(?:not\s+vals\.get\(\s*['\"](\w+)['\"]\s*\)"
    r"|['\"](\w+)['\"]\s*not\s+in\s+vals"
    r"|not\s+vals\[\s*['\"](\w+)['\"]\s*\])"
)
_SEQUENCE_PATTERN_GENERAL_ASSIGN_RE = re.compile(
    r"vals\[\s*['\"](\w+)['\"]\s*\]\s*=\s*self\.env\[\s*['\"]ir\.sequence['\"]\s*\]\.next_by_code"
)
_CREATE_METHOD_BLOCK_RE = re.compile(
    r"^([ \t]*)def\s+create\s*\(self,\s*vals(?:_list)?\).*?(?=^\1def\s|\Z)", re.MULTILINE | re.DOTALL
)


def _exempt_verified_sequence_idiom_from_coverage_gap(
    module_files: dict[str, str], uncovered_paths: list[str],
) -> list[str]:
    """Phase 25C (2026-07-25): `tools_odoo.spot_check.run_coverage_and_
    diff()` measures INSTALL-time line execution only -- a plain
    `-i module --stop-after-init` run never calls `create()` at all, so
    ANY `create()` override's own body -- whether hand-written by the
    LLM or injected by `specialists/build/specialist.py`'s own
    `_autofix_goal_named_sequence_field_missing()` (Phase 25C) -- always
    shows as "uncovered", regardless of whether the code is correct.
    Confirmed live (task 006's resubmission): the exact, provably-
    correct, deterministically-injected standard Odoo sequence-
    assignment idiom (matching the SAME textbook pattern `specialists/
    code_review/specialist.py`'s own `_filter_hallucinated_sequence_
    pattern_findings` already recognizes and protects on the Code-Review
    side) was flagged as a coverage gap purely because nothing in this
    project's own verification methodology ever creates a real record
    to exercise it -- a structural blind spot in the coverage tool
    itself, not a real, unverified gap in the code.

    Deliberately narrow, mirroring that same sibling filter's own
    discipline: only exempts lines INSIDE a `create()` method block
    that is CONFIRMED to match a known-safe idiom -- either the classic
    `name` field defaulting to 'New' (a genuine `ir.sequence.
    next_by_code` call and a genuine `== 'New'` check both present), OR
    (2026-08-08, widened after a real, live-confirmed gap -- see the
    sibling `_SEQUENCE_PATTERN_GENERAL_GUARD_RE`/`_SEQUENCE_PATTERN_
    GENERAL_ASSIGN_RE`'s own comment for the full incident) the same
    real idiom generalized to ANY field name: a genuine "not already
    provided" guard and a genuine `next_by_code()` assignment, both
    naming the SAME field. A genuinely different, non-standard
    `create()` override, or one that really does have an untested gap
    elsewhere, is returned completely unchanged either way.
    """
    models_py = next((content for path, content in module_files.items() if path.endswith("models.py")), "")
    if not models_py:
        return uncovered_paths

    # Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    # service_ticket_model node): `_CREATE_METHOD_BLOCK_RE.search()` only ever finds the FIRST
    # create() override in a multi-class models.py -- a module with two models, each with its
    # own idiomatic sequence-assignment create() (confirmed live: ServiceTicket's and
    # Equipment's), only got the first one exempted, leaving the second's identical, equally
    # correct idiom flagged as a coverage gap purely because of iteration order. Changed from
    # `search()` to `finditer()` so EVERY create() block in the file is independently checked
    # against the same idiom signatures and independently exempted -- a genuinely different or
    # non-standard create() elsewhere in the same file is still left alone, block by block.
    # The classic idiom's field DECLARATION (`name = fields.Char(..., default='New', ...)`)
    # lives at class-body scope, outside the create() method itself -- checked once against the
    # whole file, same as the original single-block version did (this part was never per-block).
    has_name_field_declaration = bool(_SEQUENCE_PATTERN_NAME_FIELD_RE.search(models_py))
    exempt_range: set[int] = set()
    for block_match in _CREATE_METHOD_BLOCK_RE.finditer(models_py):
        block = block_match.group(0)
        matches_classic_name_idiom = (
            has_name_field_declaration
            and _SEQUENCE_PATTERN_CREATE_RE.search(block)
            and _SEQUENCE_PATTERN_NEW_CHECK_RE.search(block)
        )
        matches_general_field_idiom = False
        if not matches_classic_name_idiom:
            assign_fields = {m.group(1) for m in _SEQUENCE_PATTERN_GENERAL_ASSIGN_RE.finditer(block)}
            guard_fields = {
                g for m in _SEQUENCE_PATTERN_GENERAL_GUARD_RE.finditer(block)
                for g in m.groups() if g
            }
            matches_general_field_idiom = bool(assign_fields & guard_fields)
        if not (matches_classic_name_idiom or matches_general_field_idiom):
            continue
        start_line = models_py[:block_match.start()].count("\n") + 1
        end_line = start_line + block.count("\n")
        exempt_range.update(range(start_line, end_line + 1))
    if not exempt_range:
        return uncovered_paths

    filtered = []
    for p in uncovered_paths:
        if ":" not in p:
            filtered.append(p)
            continue
        file_part, _, line_part = p.rpartition(":")
        if file_part.endswith("models.py") and line_part.isdigit() and int(line_part) in exempt_range:
            continue
        filtered.append(p)
    return filtered


_GOAL_FIELD_LINE_COMPUTE_RE = re.compile(
    r"^\s*Field(?:\s*name)?:\s*`?(\w+)`?\s*\([^)]*compute\s*=\s*`?(\w+)`?[^)]*depends on\s+(\w+)\.(\w+)",
    re.IGNORECASE | re.MULTILINE,
)


def _exempt_verified_sum_compute_idiom_from_coverage_gap(
    module_files: dict[str, str], goal: str, uncovered_paths: list[str],
) -> list[str]:
    """Phase 25D (2026-07-26): the same structural coverage-tool blind
    spot as `_exempt_verified_sequence_idiom_from_coverage_gap()` above,
    for the sum-aggregate compute shape instead of a `create()` override
    -- `tools_odoo.spot_check.run_coverage_and_diff()` measures install-
    time line execution only, so a compute method's own body never gets
    exercised (nothing creates a real record with real line data during
    a plain install), regardless of whether the code is correct.
    Confirmed live (task 004's own resubmission, right after Code-
    Review's own matching hallucination-filter fix -- see
    `specialists/code_review/specialist.py`'s `_filter_hallucinated_
    incomplete_compute_dependency_findings()` -- already closed the
    false-positive-findings half of this exact investigation): the
    field/method exactly matched the goal's own single stated
    dependency, yet the round still failed purely on this coverage
    blind spot.

    Deliberately narrow, same ground-truth discipline as its sibling:
    only exempts a compute method's own body when the goal states
    EXACTLY the same field name/compute method name/dependency path the
    real code uses -- a genuinely different or incorrect compute body
    is never touched.
    """
    match = _GOAL_FIELD_LINE_COMPUTE_RE.search(goal or "")
    if not match:
        return uncovered_paths
    _field_name, compute_method, relation, subfield = match.groups()
    models_py = next((content for path, content in module_files.items() if path.endswith("models.py")), "")
    if not models_py:
        return uncovered_paths
    if not re.search(
        rf"@api\.depends\(\s*['\"]{re.escape(relation)}\.{re.escape(subfield)}['\"]\s*\)\s*\n\s*def\s+"
        rf"{re.escape(compute_method)}\s*\(",
        models_py,
    ):
        return uncovered_paths
    method_match = re.search(
        rf"^([ \t]*)def\s+{re.escape(compute_method)}\s*\(self\).*?(?=^\1def\s|\Z)",
        models_py, re.MULTILINE | re.DOTALL,
    )
    if not method_match:
        return uncovered_paths
    start_line = models_py[:method_match.start()].count("\n") + 1
    end_line = start_line + method_match.group(0).count("\n")
    exempt_range = set(range(start_line, end_line + 1))

    filtered = []
    for p in uncovered_paths:
        if ":" not in p:
            filtered.append(p)
            continue
        file_part, _, line_part = p.rpartition(":")
        if file_part.endswith("models.py") and line_part.isdigit() and int(line_part) in exempt_range:
            continue
        filtered.append(p)
    return filtered


_STATE_BUTTON_METHOD_RE = re.compile(
    r"^([ \t]*)def\s+(\w+)\s*\(self\):\s*\n"
    r"\1[ \t]+self\.ensure_one\(\)\s*\n"
    r"\1[ \t]+if\s+self\.state\s*==\s*['\"]\w+['\"]\s*:\s*\n"
    r"\1[ \t]+self\.state\s*=\s*['\"]\w+['\"]\s*\n"
    # Real, confirmed bug found live while writing this filter's own regression test: without
    # this lookahead, the pattern above is a PREFIX match -- a method with genuine extra logic
    # after the guarded state assignment (a real side effect this exemption must never hide)
    # still matched, because nothing required the method to actually END there. Requires the
    # very next line to be blank, another `def` at the SAME (class-body) indent, or end of
    # file/string -- i.e. no further method-body-indented line follows.
    r"(?=[ \t]*\n|\1def\s|\Z)",
    re.MULTILINE,
)


def _exempt_verified_state_button_idiom_from_coverage_gap(
    module_files: dict[str, str], uncovered_paths: list[str],
) -> list[str]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    service_ticket_model node): the same install-time-only coverage blind spot as this file's
    sibling `_exempt_verified_sequence_idiom_from_coverage_gap()`/`_exempt_verified_onchange_
    idiom_from_coverage_gap()` above -- a plain `-i module --stop-after-init` run never clicks a
    workflow button, so a genuinely correct `action_assign()`/`action_start()`/etc. state-
    transition method (the standard Odoo idiom: `ensure_one()`, then a guarded
    `if self.state == 'X': self.state = 'Y'`) always shows as uncovered regardless of
    correctness (confirmed live: four such methods on oma.service.ticket, none exercised by any
    part of this project's own install-time verification methodology, all four flagged as a
    coverage gap and driving a real, needless ask_operator escalation).

    Deliberately narrow, same ground-truth discipline as its siblings: only exempts a method
    whose ENTIRE body is exactly `ensure_one()` followed by one guarded single-field state
    reassignment -- a method with any additional logic (side effects, multiple field writes,
    non-state conditions) does not match and is left fully uncovered, still a real gap.
    """
    models_py = next((content for path, content in module_files.items() if path.endswith("models.py")), "")
    if not models_py:
        return uncovered_paths
    filtered = uncovered_paths
    for match in _STATE_BUTTON_METHOD_RE.finditer(models_py):
        method_name = match.group(2)
        filtered = _exempt_method_body_lines(models_py, method_name, filtered)
    return filtered


_MANIFEST_HOOK_KEY_RE_TQA = re.compile(
    r"['\"](post_init_hook|pre_init_hook|uninstall_hook)['\"]\s*:\s*['\"](\w+)['\"]"
)


def _exempt_verified_install_hook_from_coverage_gap(
    module_files: dict[str, str], uncovered_paths: list[str],
) -> list[str]:
    """Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup): the
    same install-time-only coverage blind spot as this file's sibling `_exempt_verified_*_
    idiom_from_coverage_gap()` functions above, for a genuinely different reason -- a
    `post_init_hook`/`pre_init_hook`/`uninstall_hook` function is a plain, module-level
    function (never a method -- no `self`, not inside any class), so it is structurally
    ineligible for `_exempt_method_body_lines()`'s own `def {name}(self)` pattern even though
    the underlying coverage-tool limitation is identical: `tools_odoo.spot_check.run_coverage_
    and_diff()` measures line execution during the plain `-i module --stop-after-init` run
    itself, and a hook function genuinely DOES run exactly once during that same install --
    but coverage.py's own instrumentation, attached only after the harness's own coverage
    context starts, does not reliably attribute lines executed extremely early in module
    loading (before the model registry it depends on is even fully built) back to the source
    file the same way it credits later-executing test/business code. Confirmed live: a real,
    correctly-written, successfully-executed `post_init_hook` (independently verified via a
    direct env.ref()/unlink() check against the live database) was still flagged as an
    uncovered gap, permanently failing the round regardless of correctness.

    Deliberately unconditional on content (unlike the state-button/onchange siblings, which
    require an exact structural match): any function the manifest ITSELF declares as one of
    Odoo's 3 real hook keys is, by definition, expected to run at install/uninstall time only
    -- there is no alternative, more-idiomatic way to write it that would make this coverage
    blind spot go away, so gating on structural narrowness (as the sibling exemptions correctly
    do, since THEIR shapes have safer, more-covered alternatives) would serve no purpose here.
    """
    # Real, confirmed bug found live (2026-08-11, same task, same night): `read_module_files()`
    # keys its dict by the FULL absolute path (e.g. '/mnt/extra-addons/<module>/__manifest__.py'),
    # never a bare relative name -- `.get("__manifest__.py")` always silently returned "",
    # making this entire exemption permanently, invisibly a no-op regardless of any real
    # manifest content. Matches every sibling lookup in this file (e.g. the `models.py` lookup
    # two lines below), which already correctly use `.endswith(...)` for exactly this reason.
    manifest_py = next((content for path, content in module_files.items() if path.endswith("__manifest__.py")), "")
    hook_names = {m.group(2) for m in _MANIFEST_HOOK_KEY_RE_TQA.finditer(manifest_py)}
    if not hook_names:
        return uncovered_paths
    models_py = next((content for path, content in module_files.items() if path.endswith("models.py")), "")
    if not models_py:
        return uncovered_paths
    exempt_ranges: list[range] = []
    for hook_name in hook_names:
        match = re.search(
            rf"^([ \t]*)def\s+{re.escape(hook_name)}\s*\(.*?\).*?(?=^\1def\s|\Z)",
            models_py, re.MULTILINE | re.DOTALL,
        )
        if not match:
            continue
        start_line = models_py[:match.start()].count("\n") + 1
        end_line = start_line + match.group(0).count("\n")
        exempt_ranges.append(range(start_line, end_line + 1))
    if not exempt_ranges:
        return uncovered_paths

    filtered = []
    for p in uncovered_paths:
        if ":" not in p:
            filtered.append(p)
            continue
        file_part, _, line_part = p.rpartition(":")
        if (
            file_part.endswith("models.py") and line_part.isdigit()
            and any(int(line_part) in r for r in exempt_ranges)
        ):
            continue
        filtered.append(p)
    return filtered


_GOAL_ONCHANGE_METHOD_RE = re.compile(r"^\s*Method\s*name:\s*`?(\w+)`?", re.IGNORECASE | re.MULTILINE)
_GOAL_ONCHANGE_TRIGGER_RE = re.compile(r"^\s*Trigger\s*field:\s*`?(\w+)`?", re.IGNORECASE | re.MULTILINE)


def _exempt_method_body_lines(models_py: str, method_name: str, uncovered_paths: list[str]) -> list[str]:
    """Shared line-range-removal mechanics for both this file's
    coverage-exemption functions (sum-compute and onchange) -- given a
    method already confirmed (by the caller) to genuinely match the
    goal's own stated shape, removes every uncovered_paths entry that
    falls inside that method's own body.
    """
    method_match = re.search(
        rf"^([ \t]*)def\s+{re.escape(method_name)}\s*\(self\).*?(?=^\1def\s|\Z)",
        models_py, re.MULTILINE | re.DOTALL,
    )
    if not method_match:
        return uncovered_paths
    start_line = models_py[:method_match.start()].count("\n") + 1
    end_line = start_line + method_match.group(0).count("\n")
    exempt_range = set(range(start_line, end_line + 1))

    filtered = []
    for p in uncovered_paths:
        if ":" not in p:
            filtered.append(p)
            continue
        file_part, _, line_part = p.rpartition(":")
        if file_part.endswith("models.py") and line_part.isdigit() and int(line_part) in exempt_range:
            continue
        filtered.append(p)
    return filtered


def _exempt_verified_onchange_idiom_from_coverage_gap(
    module_files: dict[str, str], goal: str, uncovered_paths: list[str],
) -> list[str]:
    """Phase 25D (2026-07-26): sibling of `_exempt_verified_sum_compute_
    idiom_from_coverage_gap()` above, for `@api.onchange` methods --
    the SAME install-time-only coverage blind spot (nothing in this
    project's own verification methodology ever fills in a form field
    to trigger an onchange during a plain install), now confirmed live
    on task 005's own resubmission: the method body was flagged
    uncovered purely because nothing exercises it, not because the code
    is wrong.

    Deliberately narrow, same ground-truth discipline as its siblings:
    only exempts a method's own body when the goal states BOTH a
    concrete `Method name:` and `Trigger field:`, AND the real code is
    confirmed to have a genuine `@api.onchange('trigger_field')`
    decorator directly above a method of that exact name -- a
    genuinely different or missing onchange method is never touched.
    """
    method_match = _GOAL_ONCHANGE_METHOD_RE.search(goal or "")
    trigger_match = _GOAL_ONCHANGE_TRIGGER_RE.search(goal or "")
    if not method_match or not trigger_match:
        return uncovered_paths
    method_name = method_match.group(1)
    trigger_field = trigger_match.group(1)
    models_py = next((content for path, content in module_files.items() if path.endswith("models.py")), "")
    if not models_py:
        return uncovered_paths
    if not re.search(
        rf"@api\.onchange\(\s*['\"]{re.escape(trigger_field)}['\"]\s*\)\s*\n\s*def\s+{re.escape(method_name)}\s*\(",
        models_py,
    ):
        return uncovered_paths
    return _exempt_method_body_lines(models_py, method_name, uncovered_paths)


_GOAL_FREE_TEXT_COMPUTE_RE = re.compile(
    r"\b(\w+)\s*\([^)]*\bcomputed\b[^)]*\)|"
    r"\bcomputed\s+(\w+)\s+field\b",
    re.IGNORECASE,
)
_GOAL_BASED_ON_RE = re.compile(r"\bbased on\s+(\w+)\b|\bdepends? on\s+(\w+)\b", re.IGNORECASE)


def _exempt_verified_own_field_compute_idiom_from_coverage_gap(
    module_files: dict[str, str], goal: str, uncovered_paths: list[str],
) -> list[str]:
    """Real, confirmed bug found live (2026-07-29, school_student task,
    computed_age_field round -- 7+ consecutive rounds, the SAME "spot-
    check found a real, unclaimed gap" for school.student.age, blocking
    all other progress). The SAME structural coverage-tool blind spot as
    `_exempt_verified_sum_compute_idiom_from_coverage_gap()` and
    `_exempt_verified_onchange_idiom_from_coverage_gap()` above -- but
    for a THIRD real goal-text shape neither of those two rigid regexes
    matches: free natural-language phrasing like "age (Integer,
    computed)" ... "Computed age field (based on date_of_birth)" --
    an own-field dependency (not a `relation.subfield` traversal the
    sum-compute sibling requires), stated in prose rather than the
    structured "Field name: X (compute=Y ... depends on relation.
    subfield)" block the sum-compute regex expects, and not an
    `@api.onchange` method either. Confirmed live: the real committed
    `_compute_age` method is correct and matches the goal's own stated
    field/dependency exactly, yet failed identically across every
    single round because nothing in this project's install-only
    verification methodology ever creates a record with a real
    `date_of_birth` to exercise the compute body.

    Deliberately conservative, same ground-truth discipline as its two
    siblings: only exempts a compute method's body when (1) the goal's
    own free text names a field as "computed" AND states a "based on"/
    "depends on" dependency, (2) the real code defines that exact field
    with `compute=<method>`, AND (3) that method's own real body
    genuinely references the stated dependency field name -- a
    genuinely different, wrong, or missing compute method is never
    touched. Does not require `@api.depends` (unlike its two siblings)
    because Odoo does not require it for a `store=True` field to
    compute a real value -- only for automatic recomputation on trigger
    changes, a separate, different concern from "does this line ever
    execute."
    """
    based_on_match = _GOAL_BASED_ON_RE.search(goal or "")
    if not based_on_match:
        return uncovered_paths
    dependency_field = based_on_match.group(1) or based_on_match.group(2)
    if not _GOAL_FREE_TEXT_COMPUTE_RE.search(goal or ""):
        return uncovered_paths
    models_py = next((content for path, content in module_files.items() if path.endswith("models.py")), "")
    if not models_py:
        return uncovered_paths
    field_compute_pairs = re.findall(
        r"(\w+)\s*=\s*fields\.\w+\(\s*[^)]*?compute\s*=\s*['\"](\w+)['\"]", models_py,
    )
    for _field_name, compute_method in field_compute_pairs:
        method_match = re.search(
            rf"^([ \t]*)def\s+{re.escape(compute_method)}\s*\(self\).*?(?=^\1def\s|\Z)",
            models_py, re.MULTILINE | re.DOTALL,
        )
        if not method_match:
            continue
        if re.search(rf"\b{re.escape(dependency_field)}\b", method_match.group(0)):
            return _exempt_method_body_lines(models_py, compute_method, uncovered_paths)
    return uncovered_paths


def _has_remaining_decomposed_constraints(goal: str) -> bool:
    """True when this round's own goal text still names OTHER
    constraints not yet in scope (a decomposed task, mid-sequence) --
    False for a non-decomposed task, OR the final round of a decomposed
    one (an empty `[]` list is genuinely "nothing left", same
    distinction manager/loop.py's own `_reconstruct_resume_order`
    makes for the identical marker).
    """
    match = _NOT_YET_IN_SCOPE_RE.search(goal)
    if not match:
        return False
    return bool([label for label in match.group(1).split(",") if label.strip()])


# Real, confirmed bug found live (2026-07-28, Phase 28C, school_student
# task, menu_structure round): `_has_remaining_decomposed_constraints`
# alone is too blunt a gate for skipping a claim check -- it only asks
# "are there OTHER constraints still not yet in scope after this
# round", which is true for EVERY round except the task's last one. On
# the menu_structure round itself (this round's own real, current
# focus), that made the brand-new menu-structure check silently skip
# and default `menu_check_passed=True` -- exactly the "trivially,
# simply pass" shortcut ruled out for this work -- because 5 more
# constraints (security_groups, record_rules, ...) still remained
# AFTER menu_structure, even though menu_structure's own claim was
# real and checkable right now. The same latent defect exists for the
# pre-existing `security_check_passed` gate for its own future round
# (security_groups). Fixed by cross-checking the claim against THIS
# round's own real, current focus label (the same "This round's own
# NEW focus is ONLY: 'X'" marker manager/loop.py itself writes and
# specialists/build/specialist.py's + specialists/code_review/
# specialist.py's own `_THIS_ROUNDS_FOCUS_RE` already parse) rather
# than a blanket "is this the very last round" test -- a claim is only
# ever skipped when it demonstrably belongs to a DIFFERENT, not-yet-
# reached round, never merely because more rounds remain after this
# one.
_TESTINGQA_THIS_ROUNDS_FOCUS_RE = re.compile(r"This round's own NEW focus is ONLY:\s*'([^']+)'")


def _this_rounds_own_focus_label(goal: str) -> str | None:
    match = _TESTINGQA_THIS_ROUNDS_FOCUS_RE.search(goal or "")
    return match.group(1) if match else None


def _is_this_rounds_own_focus(focus_label: str | None, keyword_re: "re.Pattern[str]") -> bool:
    """True when there's no decomposition at all (a plain, non-
    decomposed task -- always verify), OR the round's own real,
    current focus label matches the given claim-shape keyword pattern.
    False only when this round's real focus is demonstrably something
    ELSE (a genuinely later, not-yet-reached constraint).
    """
    if focus_label is None:
        return True
    return bool(keyword_re.search(focus_label))


_MENU_FOCUS_KEYWORD_RE = re.compile(r"menu", re.IGNORECASE)
_SECURITY_FOCUS_KEYWORD_RE = re.compile(r"security|access|\bgroup", re.IGNORECASE)


def _is_non_field_constraint_echoed_as_target(target_field_name: str, goal: str) -> bool:
    """Real, general fix (2026-07-29, school_student task, demo_data
    round -- 2 straight identical "Reproduction FAILED" rounds). A
    round whose own constraint is data-loading (`demo_data`) or test-
    writing (`automated_tests`) has no real ORM field to reproduce --
    `_extract_reproduction_target()` falls back to echoing the round's
    own focus LABEL as `field_name` for these shapes (confirmed live:
    `target.field_name == 'demo_data'`, and `school.student` has no
    field literally named `demo_data`), so `_check_field_exists_on_
    model()` always, incorrectly, fails regardless of how correct the
    real content is -- the same class of false negative the existing
    button-restriction and menu-structure skips above already exist
    for, just a third shape neither of those two covers.

    True only when the extracted target's own `field_name` is an EXACT
    match for this round's own real focus label (from the goal's own
    "This round's own NEW focus is ONLY: 'X'" marker) -- a precise,
    narrow signal that the extraction itself echoed the round label
    rather than finding a real field, never a broad guess. A plain,
    non-decomposed task (no focus marker in its goal at all) always
    returns False here, preserving the exact current behavior.
    """
    focus_label = _this_rounds_own_focus_label(goal)
    return focus_label is not None and target_field_name == focus_label


_XMLID_SHAPED_TARGET_RE = re.compile(r"^[a-zA-Z_]\w*\.[a-zA-Z_]\w*$")


def _target_looks_like_an_xmlid_not_a_field(target_field_name: str, goal: str = "") -> bool:
    """Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup
    round): `_extract_reproduction_target()`'s own goal-literal-name heuristic ("if the goal
    literally names a specific technical field... use EXACTLY that name") matched a fully-
    qualified XML external id embedded in the goal text (e.g.
    'oma_build_a_complete_field_ab52b7f8.access_oma_service_ticket', naming the
    ir.model.access.csv ROW a post_init_hook revokes, never any ORM field) purely because it
    superficially looks like a snake_case identifier. A real Odoo field name is always a
    single, undotted Python identifier -- `<module_name>.<record_id>` is structurally
    impossible as a field name, so this is an unambiguous, purely syntactic signal, the same
    class of false-negative the button/menu/non-field-constraint/method shape skips above
    already exist for, just a fifth shape none of those cover.

    Second, real gap found live in THIS fix's own verification, same task, same night: the
    extraction is NOT consistent about whether it keeps the module prefix -- on one round it
    returned the full dotted xmlid, on another it returned only the bare record-id suffix
    ('access_oma_service_ticket', no dot at all), which the strict dotted-shape check above
    can never catch since a bare, undotted identifier is syntactically indistinguishable from a
    genuine field name. Fallback: if the bare check doesn't match, cross-reference `goal` for a
    literal, dotted xmlid token whose OWN suffix (after the last '.') exactly equals
    `target_field_name` -- i.e. the goal itself proves this exact bare name was derived by
    stripping a real xmlid's module prefix, not genuinely naming an ORM field.
    """
    if _XMLID_SHAPED_TARGET_RE.match(target_field_name or ""):
        return True
    if not target_field_name:
        return False
    return bool(re.search(rf"[a-zA-Z_]\w*\.{re.escape(target_field_name)}\b", goal or ""))


_METHOD_DEF_NAME_RE = re.compile(r"^\s*def\s+(\w+)\s*\(", re.MULTILINE)


def _target_is_a_real_method_not_a_field(target_field_name: str, models_py: str) -> bool:
    """Real, general fix (2026-08-06, Phase 30 root-cause pass, task040): a FOURTH real shape
    neither the button/menu/non-field-constraint skips above cover -- a task whose own goal
    explicitly names a plain PYTHON METHOD (e.g. "Add a plain Python method get_summary_data()
    ...") rather than a field. `_extract_reproduction_target()` correctly resolves `field_name`
    to the real method's own name (it IS the real target the goal names), but
    `_check_field_exists_on_model()` can never confirm a method via `fields_get()` -- confirmed
    live: a genuinely correct `get_summary_data()` method (flat dict, `self.ensure_one()`, exactly
    matching this project's own established rules) was reported "Reproduction FAILED" identically
    across 2 straight rounds purely because this check was verifying the wrong thing entirely for
    a task shape it was never designed to cover -- the coverage-based spot-check is the real proof
    for this shape, same discipline as its 3 siblings above.

    True only when `target_field_name` is a real `def <name>(` in THIS round's own committed
    models.py -- never a guess, and never touches a genuine field-shaped target (a field
    declaration and a method definition can never share the same regex match).
    """
    return target_field_name in set(_METHOD_DEF_NAME_RE.findall(models_py))


def _should_run_behavioral_probe(reproduction_confirmed: bool, goal_facts: dict | None) -> bool:
    """Phase 30, P1c (Phase I, §12) gate, extracted here (2026-08-08) so it's directly
    unit-testable without mocking the whole run() method: the plain field-existence check only
    ever proves a field EXISTS, never that it does what the goal claims -- worth the extra real
    create/read round-trip only for the two goal_facts shapes known to have real "exists but
    doesn't actually do the thing" failure modes: `is_computed` (the original signal, reused
    verbatim from `_should_use_best_of_n()`) and `is_sequence_assigned` (widened 2026-08-08,
    real bug found live during the site_50 benchmark's task 006 -- a specialist declared a plain
    Char field and an ir.sequence record but never wrote the create()-override that actually
    calls next_by_code(), and the plain existence check alone reported reproduction_confirmed=True
    for it; confirmed live via coverage_diff showing models.py at only 3 statements, no create()
    override present at all). Never runs at all when existence itself already failed -- nothing
    to probe on a field that was never even declared.
    """
    if not reproduction_confirmed:
        return False
    facts = goal_facts or {}
    return bool(facts.get("is_computed") or facts.get("is_sequence_assigned"))


class ReproductionTarget(BaseModel):
    model: str          # e.g. "res.partner"
    field_name: str      # e.g. "preferred_language"


class ReproductionTargetList(BaseModel):
    """P12 Tier A item 25 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    §4, A Finding 15): the field-existence sibling of the widening the security/menu checks
    already have on a decomposed task's FINAL round (`_is_this_rounds_own_focus`/
    `has_remaining_decomposed_constraints` -- see `run()`'s own security_check_passed/
    menu_check_passed blocks). Real, confirmed gap: `_extract_reproduction_target()` always
    checks exactly ONE field (this round's own current focus); regression protection for every
    EARLIER constraint's own already-claimed field relies entirely on Build's own non-independent
    text diff (`regressed_constraints`), never Testing/QA's independent live-registry ground
    truth. On the final round, `targets` names every distinct model+field pair the FULL original
    goal claims anywhere, not just the current round's -- each is independently re-verified live,
    the same real check every other field-existence claim already gets.
    """

    targets: list[ReproductionTarget]


_GROUP_XMLID_SHAPE_RE = re.compile(r"^([\w]+\.)?group_[\w]+$")


def _looks_like_group_xmlid(group_name: str) -> bool:
    """True for an XML-ID-shaped security group token ('base.group_user', 'group_user',
    'my_module.group_field_technician') as opposed to a real display name ('Internal User',
    'Field Technician') -- a cheap, structural check (never a live query) used to decide whether
    `_group_exists()` should also try `resolve_group_xmlid_to_display_name()` as a fallback.
    Odoo's own convention: every stock/custom security group's technical name starts with
    'group_', so this is a reliable, low-false-positive signal without needing a query.
    """
    return bool(_GROUP_XMLID_SHAPE_RE.match(group_name.strip()))


def _singular_plural_variant(name: str) -> str | None:
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    ticket_access_rights node): `_extract_security_access_claim()`'s own SHAPE-1 prompt is
    deliberately blind to the generated code (see that function's own docstring for why -- it
    must verify what the goal CLAIMS, not what Build happened to write, or a wrong generation
    could trivially "pass"). But when the goal only ever describes a ROLE in prose ("operations
    managers need full visibility...") rather than literally naming/quoting an exact group
    string, BOTH the extraction call and Build's own independent generation have to guess a
    concrete display name from the same ambiguous prose -- and there is no reason those two
    independent guesses land on the same grammatical number. Confirmed live: the goal said
    "operations managers" (plural, prose), Build reasonably named the actual res.groups record
    "Operations Manager" (singular Title Case, standard Odoo convention), and the extraction
    call independently guessed "Operations Managers" (plural) from the same sentence --
    `check_group_exists`'s exact-string SQL match can never reconcile these, regardless of how
    correct Build's own content is. This is a structural gap in the claim shape, not a one-off
    naming mistake either side could have "gotten right" -- confirmed via install_module()
    succeeding and a direct DB check for the ACTUAL name ("Operations Manager") also failing at
    the time, ruling out an install/persistence bug entirely.

    Returns the other grammatical-number form of the FINAL word only (matching how a role name
    like "Operations Manager(s)" or "Field Technician(s)" pluralizes in English -- never
    touching earlier words), or None when there's no sensible variant to try (the name has no
    trailing word to pluralize/singularize, e.g. it's empty). Deliberately simple (trailing
    "s"/"es" only, no irregular-plural handling) -- this closes the one real, observed shape;
    a name this can't handle just falls through to the existing exact-match behavior, never
    worse than before.
    """
    if not name or not name[-1].isalpha():
        return None
    if name.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if name.endswith("es") and len(name) > 2:
        return name[:-2]
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    if name.endswith("y") and len(name) > 1 and name[-2].lower() not in "aeiou":
        return name[:-1] + "ies"
    return name + "s"


_GOAL_MULTI_GROUP_ACCESS_RESTRICTION_RE = re.compile(
    r"so that only the existing ([\w.]+)\s*(?:\([^)]*\))?\s+and\s+([\w.]+)\s*(?:\([^)]*\))?\s+groups? can access ([\w.]+)",
)


class SecurityAccessClaim(BaseModel):
    """Real, general, conceptual fix (2026-07-17, Phase 20 Area 2): the
    field-existence check above only ever proves a field/model exists
    -- it has NO mechanism to verify a security GROUP was actually
    created, that its PERMISSIONS match what the goal asked for, or
    that a field's visibility was actually RESTRICTED to the right
    group. Found live investigating a task that reported "passed" while
    its real committed module granted `base.group_user` instead of a
    NEW group the goal explicitly named, with an explicitly-requested
    delete permission silently missing -- and, worse, three OTHER
    "passed" field-visibility-restriction tasks were confirmed via
    direct DB query to have applied NO real restriction at all. This is
    a structural gap in what "passed" has meant for the entire access-
    rules task category, not a one-off bug -- this schema and the
    extraction/verification built around it are the general fix,
    covering the three concrete claim shapes Area 2's own tasks
    actually make. `applicable=False` means the goal makes none of
    these claims (e.g. a plain field-add task) -- nothing to check.
    """
    applicable: bool
    group_name: str | None = None
    model: str | None = None
    expects_read: bool | None = None
    expects_write: bool | None = None
    expects_create: bool | None = None
    expects_unlink: bool | None = None
    restricted_field_name: str | None = None
    # Real, general fix (2026-07-25, task 008): a FOURTH claim shape --
    # an existing BUTTON's visibility restricted to a group via its
    # view's own `groups` XML attribute -- was completely uncovered by
    # either this schema's existing shapes OR the separate field-
    # existence reproduction check (`_check_field_exists_on_model`,
    # which a button name will NEVER satisfy, since a button is not an
    # ORM field). Confirmed live: a genuinely correct button-restriction
    # fix (confirmed both by Code-Review and a direct live-registry
    # read) was reported as failing identically across 2+ rounds, purely
    # because NOTHING in this specialist actually checked the right
    # thing. `restricted_group_xmlid` is preferred over `group_name`
    # when the goal literally states a standard/base group xmlid (this
    # project's own convention, e.g. "groups=base.group_system") --
    # matching by xmlid is strictly more reliable than by display name
    # for a well-known base group whose real name may not resemble how
    # the goal describes it (base.group_system's real name is
    # 'Settings', not 'System Administrators').
    restricted_button_name: str | None = None
    restricted_group_xmlid: str | None = None


_GOAL_MENTIONS_POST_INIT_HOOK_RE = re.compile(r"post_init_hook", re.IGNORECASE)


def _correct_hook_revocation_goal_misclassified_as_security_claim(
    claim: SecurityAccessClaim, goal: str,
) -> SecurityAccessClaim:
    """Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup): the
    goal explicitly describes REVOKING an existing access grant via a `post_init_hook` --
    none of `_extract_security_access_claim()`'s own 3 defined shapes (a NEW group's access, an
    EXISTING field's visibility restricted to a group, an EXISTING button's visibility
    restricted to a group), and its own prompt already correctly instructs "if the goal is none
    of these... set applicable=false." Confirmed live: the fast extraction model still returned
    `applicable=true` anyway (misreading the goal's own "access"/"group" vocabulary as fitting
    one of the 3 shapes), then inevitably found no real `group_name` to fill in, permanently
    failing `_verify_security_access_claim()` with "security claim incomplete" no matter how
    many times the round was retried -- a goal shape this checker was never built to verify at
    all, not a genuine security regression.

    Deterministic, narrow, self-declaration-only override, same discipline as every sibling
    filter in this codebase: only fires when the goal ITSELF literally names `post_init_hook`
    (an unambiguous signal this round's real mechanism is a install-time hook, not a directly
    declared CSV/group/view claim) AND the extracted claim is genuinely empty (no group_name,
    no restricted_field_name, no restricted_button_name) -- a goal that happens to mention
    post_init_hook while ALSO making a real, fully-specified claim (e.g. a hook alongside a
    genuinely new group) is left untouched.
    """
    if not claim.applicable:
        return claim
    if not _GOAL_MENTIONS_POST_INIT_HOOK_RE.search(goal or ""):
        return claim
    if claim.group_name or claim.restricted_field_name or claim.restricted_button_name:
        return claim
    return claim.model_copy(update={"applicable": False})


class MenuStructureClaim(BaseModel):
    """Real, confirmed gap found live (2026-07-28, Phase 28C,
    `school_student` task) -- closes the `menu_action_placement` entry
    in `contracts/verifier_registry.py` (`status="unverified"` since
    Phase 25E's own audit). The SAME conceptual gap `SecurityAccessClaim`
    above already closed for security groups/field/button restrictions:
    the field-existence reproduction check has NO mechanism to verify a
    menu structure was actually created, because a menu item is not an
    ORM field and can never satisfy that check, correct or not.
    Confirmed live: a genuinely correct, fully-wired menu (root menu,
    child menu, real window action) was reported "failed" identically
    across many rounds, purely because nothing ever checked the real
    thing the goal actually claimed.

    `menu_names` in top-to-bottom hierarchy order (e.g. "Menu structure:
    School -> Students" -> `["School", "Students"]`) -- each name is
    independently verified to exist as a real `ir.ui.menu` record, and
    each name after the first is verified to have the PRECEDING name as
    its own real parent, so a real chain is confirmed, not just isolated
    names anywhere in the menu tree. `action_model`, when the goal
    states or clearly implies which model the LEAF menu's own window
    action should open, is verified against that leaf menu's own real
    action's `res_model` -- a menu that resolves to the wrong model's
    data is a real, different bug this alone would otherwise miss.
    `applicable=False` means the goal makes no menu-structure claim at
    all (e.g. a plain field-add task) -- nothing to check.
    """
    applicable: bool
    menu_names: list[str] = []
    action_model: str | None = None


class SelfReportedCoverage(BaseModel):
    believed_fully_covered: bool
    claimed_uncovered_paths: list[str]
    reasoning: str


class BehavioralProbeCreate(BaseModel):
    model: str
    # JSON-safe values only (str/int/float/bool/None) -- never raw code.
    # A value of the literal form "$0", "$1", ... is resolved to the
    # real id of the record created at that earlier list index (e.g. a
    # child record's own parent-link field pointing back at record 0),
    # the one general mechanism run_behavioral_probe() supports for
    # "create a parent, then children that reference it."
    values: dict[str, str | int | float | bool | None]


class BehavioralProbeSpec(BaseModel):
    """Phase 30, P1c (Phase I, §12): what the model synthesizes for a
    real behavioral probe -- deliberately STRUCTURED data only (never
    LLM-authored code); `tools_odoo.spot_check.run_behavioral_probe()`
    is the one place that ever turns this into a real `env[model].
    create(...)` call, using `repr()` on already-validated values.

    `applicable=False` means this round's own goal doesn't describe a
    checkable compute/action effect (e.g. a goal whose is_computed
    signal fired on something this probe can't meaningfully test) --
    nothing to run, same conservative posture as the sibling Security/
    MenuStructure claim schemas above.
    """
    applicable: bool
    creates: list[BehavioralProbeCreate] = []
    target_index: int = 0
    check_fields: list[str] = []
    # A short, plain-English statement of what SHOULD be true about
    # check_fields' real values afterward (e.g. "amount_total should
    # equal 2 * 150 = 300, the line's quantity times its unit price") --
    # never a pre-computed exact number trusted blindly; this is hand-
    # off text for the SEPARATE judgment step below, which grounds its
    # verdict in the REAL observed value, not a guess made before the
    # probe ever ran.
    expected_effect: str = ""


class BehavioralProbeVerdict(BaseModel):
    behavior_confirmed: bool
    reasoning: str


class TestingQASpecialist:
    def __init__(
        self, client: ModelGatewayClient, routine_model: str | None = None, escalation_model: str | None = None,
        fast_extraction_model: str | None = None,
    ):
        self.client = client
        self.routine_model = routine_model or os.environ.get(_ROUTINE_MODEL_ENV, _ROUTINE_MODEL_FALLBACK)
        self.escalation_model = escalation_model or os.environ.get(_ESCALATION_MODEL_ENV, _ESCALATION_MODEL_FALLBACK)
        self.fast_extraction_model = fast_extraction_model or os.environ.get(
            _FAST_EXTRACTION_MODEL_ENV, _FAST_EXTRACTION_MODEL_FALLBACK
        )

    async def run(self, contract: TaskContract) -> SpecialistOutput:
        verify_input = next((i for i in contract.inputs if i.startswith(_VERIFY_PREFIX)), None)
        if not verify_input:
            return SpecialistOutput(
                task_id=contract.task_id,
                specialist_type=contract.specialist_type,
                summary=(
                    f"contract.inputs contains no {_VERIFY_PREFIX!r} entry -- nothing concrete to "
                    f"verify. inputs={contract.inputs!r}"
                ),
                detail={},
                claims_complete=False,
            )

        rest = verify_input[len(_VERIFY_PREFIX):]
        module_name, db = rest.rsplit(":", 1)

        # Phase 22 follow-up (2026-07-23): converted from a hand-picked
        # asyncio.gather() pair (verified independent once, frozen in
        # code forever) to the generic manager.step_scheduler --
        # confirmed independent the same way as before (reading each
        # step's own body: _extract_security_access_claim never reads
        # module code at all, only contract.goal/deliverables;
        # _self_report_coverage only reads contract.goal/deliverables/
        # module_name; run_coverage_and_diff is a pure deterministic
        # diff with no dependency on any of the others), but now
        # expressed as declared reads/writes so the NEXT person adding
        # a 5th independent step here doesn't need to hand-verify
        # independence against the other four all over again -- the
        # scheduler derives what's safe to run concurrently from the
        # declarations. The reproduction-target autocorrect step is
        # folded into the SAME step as its own raw extraction (reads
        # only what that extraction itself produces, not the other
        # three), preserving the exact same timing shape as before.
        async def _step_target(_context):
            raw = await self._extract_reproduction_target(contract, module_name)
            corrected = await self._autocorrect_hallucinated_reproduction_target(
                raw, module_name, db, task_id=str(contract.task_id),
                collision_confirmed_fields=_collision_confirmed_field_names_from_inputs(contract.inputs),
                goal_text=contract.goal,
            )
            return {"target": corrected}

        async def _step_security_claim(_context):
            raw_claim = await self._extract_security_access_claim(contract, db)
            return {
                "security_claim": _correct_hook_revocation_goal_misclassified_as_security_claim(
                    raw_claim, contract.goal,
                ),
            }

        async def _step_menu_claim(_context):
            return {"menu_claim": await self._extract_menu_structure_claim(contract)}

        async def _step_self_report(_context):
            return {"self_report": await self._self_report_coverage(contract, module_name)}

        async def _step_coverage(_context):
            return {"coverage_result": await asyncio.to_thread(run_coverage_and_diff, module_name, db)}

        async def _step_business_rules(_context):
            confirmed, notes = await self._run_business_rule_probes(contract, module_name, db)
            return {"business_rule_result": (confirmed, notes)}

        step_results = await run_steps(
            [
                Step(name="reproduction_target", reads=frozenset(), writes=frozenset({"target"}), run=_step_target),
                Step(name="security_claim", reads=frozenset(), writes=frozenset({"security_claim"}), run=_step_security_claim),
                Step(name="menu_claim", reads=frozenset(), writes=frozenset({"menu_claim"}), run=_step_menu_claim),
                Step(name="self_report_coverage", reads=frozenset(), writes=frozenset({"self_report"}), run=_step_self_report),
                Step(name="coverage_diff", reads=frozenset(), writes=frozenset({"coverage_result"}), run=_step_coverage),
                Step(name="business_rule_probes", reads=frozenset(), writes=frozenset({"business_rule_result"}), run=_step_business_rules),
            ],
            {},
        )
        target = step_results["target"]
        security_claim = step_results["security_claim"]
        menu_claim = step_results["menu_claim"]
        self_report = step_results["self_report"]
        coverage_result = step_results["coverage_result"]
        business_rule_confirmed, business_rule_notes = step_results["business_rule_result"]

        # Real, general fix (2026-07-25, task 008, follow-up to the
        # reproduction-check skip below): `_extract_security_access_
        # claim()` deliberately never reads the real generated code (its
        # own docstring's whole point -- verifying what the goal CLAIMS,
        # never trivially matching whatever Build wrote), which means
        # its own `model` field is a blind guess whenever the goal
        # doesn't literally state the model's dotted technical name.
        # Confirmed live: task 008's goal states `Module: project_fieldjob`
        # but never a `Model:` line -- the security-claim extraction
        # guessed `model='project_fieldjob'` (confusing the MODULE name
        # with the model's real technical name `project.fieldjob`),
        # while `_extract_reproduction_target()` (which DOES read the
        # real code specifically to resolve technical names, see its own
        # docstring) correctly resolved `target.model='project.fieldjob'`
        # in the very same round. `target.model` is proven more reliable
        # here (it has code-reading capability the security-claim
        # extraction deliberately lacks) -- prefer it whenever the two
        # disagree, for every claim shape, not just the button one.
        if security_claim.applicable and security_claim.model and security_claim.model != target.model:
            security_claim = security_claim.model_copy(update={"model": target.model})

        # Step 2: the real, deterministic reproduction check -- never an
        # assumption based on "the install didn't error."
        #
        # Real, general fix (2026-07-25, task 008): a button-restriction
        # claim (SecurityAccessClaim SHAPE 3) names a BUTTON, never an
        # ORM field -- `_check_field_exists_on_model()` below can NEVER
        # confirm it (a button will never appear in `fields_get()`'s own
        # field list), so running it anyway for this shape always
        # produces a false "Reproduction FAILED", regardless of how
        # correct the real fix is. Confirmed live: a genuinely correct
        # button-restriction fix (confirmed independently by Code-Review
        # AND a direct live-registry read) was reported failing
        # identically across 4 straight rounds, purely because this
        # field-existence check was verifying the wrong thing entirely
        # for a task shape it was never designed to cover. The button-
        # restriction check (`security_check_passed`, computed below via
        # `_verify_security_access_claim`) IS the real, correct
        # reproduction proof for this shape -- skip the field-existence
        # check entirely rather than have it redundantly (and wrongly)
        # gate `passed` on a target that was never a real field.
        # Real, confirmed bug found live (2026-07-28, Phase 28C,
        # school_student task): the SAME class of false-negative the
        # button-restriction skip above already exists for -- a pure
        # menu-structure round's own "field_name" is forced to be
        # something like the constraint's own label ("menu_structure"
        # itself), never a real ORM field, so the field-existence check
        # below can NEVER succeed for this shape either. The real,
        # correct proof for this shape is the menu-structure check
        # below (`menu_check_passed`, via `_verify_menu_structure_claim`
        # -> `check_menu_exists()`, a genuine `ir.ui.menu` registry
        # read) -- skip the field-existence check entirely rather than
        # have it redundantly (and wrongly) gate `passed` on a target
        # that was never a real field.
        if security_claim.applicable and security_claim.restricted_button_name:
            reproduction_confirmed = True
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": (
                    f"Skipping the field-existence reproduction check for {target.model}."
                    f"{target.field_name} -- this task's real target is a BUTTON "
                    f"({security_claim.restricted_button_name!r}), never an ORM field; the "
                    f"button-restriction security-claim check below is the real proof for this shape."
                ),
                "status": "passed",
            })
        elif (
            security_claim.applicable and security_claim.group_name
            and not security_claim.restricted_field_name and not security_claim.restricted_button_name
        ):
            # Real, confirmed gap found live (2026-08-16, record_rule_row_level_security's own
            # certification runs -- 2 straight identical "Reproduction FAILED... Security
            # claim: ... matches all claimed permissions" pauses, wave63/wave64): a pure
            # group+ACL+record-rule task (no field or button restriction at all -- the OTHER
            # common shape for this direction, distinct from the button-skip case above) has no
            # real ORM field to reproduce either, so `_extract_reproduction_target()` falls back
            # to echoing the security GROUP'S OWN display name as `field_name` (confirmed live:
            # target.field_name == 'Sales Team Auditor 64', a group name with spaces -- `crm.
            # team` unsurprisingly has no field literally named that). `_check_field_exists_on_
            # model()` then always, incorrectly, fails regardless of how correct the real
            # group/ACL/record-rule content actually is -- while the SEPARATE, correctly-scoped
            # security-claim check below (which verifies the group by NAME, not by field) can
            # independently pass, producing the confusing "FAILED... matches all claimed
            # permissions" contradiction seen live. Same family as the button-skip immediately
            # above; the security-claim check itself remains the real, unweakened proof for this
            # shape.
            reproduction_confirmed = True
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": (
                    f"Skipping the field-existence reproduction check for {target.model}."
                    f"{target.field_name} -- this task's real target is a security GROUP "
                    f"({security_claim.group_name!r}), never an ORM field; the group/ACL "
                    f"security-claim check below is the real proof for this shape."
                ),
                "status": "passed",
            })
        elif menu_claim.applicable and menu_claim.menu_names:
            reproduction_confirmed = True
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": (
                    f"Skipping the field-existence reproduction check for {target.model}."
                    f"{target.field_name} -- this task's real target is a MENU STRUCTURE "
                    f"({menu_claim.menu_names!r}), never an ORM field; the menu-structure check "
                    f"below is the real proof for this shape."
                ),
                "status": "passed",
            })
        elif _is_non_field_constraint_echoed_as_target(target.field_name, contract.goal):
            # Real, general fix (2026-07-29, school_student task,
            # demo_data round -- 2 straight identical failures): the
            # THIRD real shape neither the button skip nor the menu skip
            # above covers -- a round whose constraint is data-loading
            # (demo_data) or test-writing (automated_tests), never an
            # ORM field either. `_extract_reproduction_target()` has no
            # real field to find for these shapes, so it falls back to
            # echoing the round's own focus LABEL as `field_name`
            # (confirmed live: target.field_name == 'demo_data', and
            # `school.student` unsurprisingly has no field literally
            # named `demo_data`) -- `_check_field_exists_on_model()`
            # then always, incorrectly, fails, regardless of how correct
            # the real demo data or tests actually are. Same pattern as
            # the button/menu skips above: `passed` below still
            # separately requires `not spot_check_mismatch` (the real,
            # coverage-based proof this shape's content was genuinely
            # exercised) computed further down -- this skip only removes
            # the ALWAYS-wrong field-existence check, it does not weaken
            # the real gate.
            reproduction_confirmed = True
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": (
                    f"Skipping the field-existence reproduction check for {target.model}."
                    f"{target.field_name} -- this round's own real target is a non-field constraint "
                    f"({target.field_name!r}, matching this round's own focus label), never an ORM "
                    f"field; the coverage-based spot-check above is the real proof for this shape."
                ),
                "status": "passed",
            })
        elif _target_looks_like_an_xmlid_not_a_field(target.field_name, contract.goal):
            reproduction_confirmed = True
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": (
                    f"Skipping the field-existence reproduction check for {target.model}."
                    f"{target.field_name} -- {target.field_name!r} is shaped like a dotted XML "
                    f"external id (e.g. an ir.model.access.csv row being revoked via a "
                    f"post_init_hook), never a real ORM field (which is always a single, undotted "
                    f"identifier); the module's own successful, real sandbox install (which "
                    f"actually runs any declared post_init_hook) is the real proof for this shape."
                ),
                "status": "passed",
            })
        else:
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": f"Reproducing {target.model}.{target.field_name}…", "status": "running",
            })
            field_check_result = _check_field_exists_on_model(db, target.model, target.field_name, str(contract.task_id))
            # P12 Tier A item 12: None is genuine uncertainty (an infra failure -- SSH timeout,
            # missing env var, etc.), never a confirmed absence -- treated as not-confirmed for
            # the mandatory bool field below (the safe, conservative default: never claim
            # success when uncertain), but reported with a distinct message/status so it's
            # diagnostically distinguishable from a genuinely confirmed-missing field.
            field_check_uncertain = field_check_result is None
            reproduction_confirmed = bool(field_check_result)
            # Real, general fix (2026-08-06, Phase 30 root-cause pass, task040): a FOURTH real
            # shape neither the button/menu/non-field-constraint skips above cover -- a task whose
            # own goal explicitly names a plain PYTHON METHOD (e.g. "Add a plain Python method
            # get_summary_data() ..."). `_extract_reproduction_target()` correctly resolves
            # `field_name` to the real method's own name, but a method can never appear in
            # `fields_get()`'s own field list, so `_check_field_exists_on_model()` always,
            # incorrectly, fails for this shape. Confirmed live: a genuinely correct
            # `get_summary_data()` method (flat dict, `self.ensure_one()`) was reported
            # "Reproduction FAILED" identically across 2 straight rounds. Deliberately checked
            # ONLY here, after the ordinary field check has already failed -- the extra
            # `read_module_files()` SSH round-trip this needs is never paid for the overwhelmingly
            # common case (a genuine field, confirmed on the first, cheaper check above).
            if not field_check_result and not field_check_uncertain:
                try:
                    module_files = read_module_files(module_name)
                except CodebaseReadError:
                    module_files = {}
                models_py = next(
                    (c for p, c in module_files.items() if p.endswith("models.py")), "",
                )
                if _target_is_a_real_method_not_a_field(target.field_name, models_py):
                    reproduction_confirmed = True
                    field_check_uncertain = False
                    publish_trace_event(str(contract.task_id), {
                        "level": "specialist", "actor": "testing_qa",
                        "message": (
                            f"Field-existence check failed for {target.model}.{target.field_name}, "
                            f"but it's actually a real `def {target.field_name}(` in this round's "
                            f"own committed models.py -- a plain PYTHON METHOD, never an ORM field. "
                            f"Treating as reproduced; the coverage-based spot-check is the real "
                            f"proof for this shape."
                        ),
                        "status": "passed",
                    })
            if field_check_uncertain:
                publish_trace_event(str(contract.task_id), {
                    "level": "specialist", "actor": "testing_qa",
                    "message": (
                        f"Could not verify {target.model}.{target.field_name} at all -- an "
                        f"infrastructure error (SSH/odoo-bin shell) made the check itself "
                        f"inconclusive, not a confirmed absence. Treated as not-confirmed."
                    ),
                    "status": "failed",
                })
            else:
                publish_trace_event(str(contract.task_id), {
                    "level": "specialist", "actor": "testing_qa",
                    "message": f"Reproduction {'confirmed' if reproduction_confirmed else 'FAILED'} for {target.model}.{target.field_name}.",
                    "status": "passed" if reproduction_confirmed else "failed",
                })

            # Phase 30, P1c (Phase I, §12): the field-existence check
            # above only ever proves the field EXISTS, never that it
            # does what the goal claims -- the real ceiling on
            # reproduction_gap, this document's own single largest
            # failure category. Only worth the extra real create/read
            # round-trip when the round's own goal_facts.is_computed is
            # true (the exact same signal _should_use_best_of_n()
            # already uses -- no new signal invented) -- an ordinary,
            # non-computed field already gets a fully sufficient proof
            # from existence alone. Runs even when the existence check
            # already passed (existence passing is necessary, not
            # sufficient, for a computed field); skipped entirely when
            # existence already failed (nothing to probe on a field that
            # was never even declared).
            #
            # Widened (2026-08-08, real bug found live during the site_50 benchmark's task 006,
            # "auto-generate a unique reference number on create") -- see
            # _should_run_behavioral_probe()'s own docstring for the full real incident this
            # closes. Extracted into that small, directly-testable helper rather than left as an
            # inline condition.
            if _should_run_behavioral_probe(reproduction_confirmed, contract.goal_facts):
                publish_trace_event(str(contract.task_id), {
                    "level": "specialist", "actor": "testing_qa",
                    "message": f"Running a real behavioral probe for {target.model}.{target.field_name}…",
                    "status": "running",
                })
                probe_result = await self._run_behavioral_probe_check(contract, target, module_name, db)
                if probe_result is not None:
                    reproduction_confirmed, probe_message = probe_result
                    publish_trace_event(str(contract.task_id), {
                        "level": "specialist", "actor": "testing_qa",
                        "message": probe_message,
                        "status": "passed" if reproduction_confirmed else "failed",
                    })

        escalation_notes = ""
        if not reproduction_confirmed:
            escalation_notes = await self._escalate_for_debugging(contract, target)

        # Step 2.5: the general security-access claim check (2026-07-17)
        # -- does the goal claim a NEW group was created with specific
        # permissions, or that a field's visibility was restricted to a
        # group? If so, verify it for real against the live registry,
        # never trusting the field-existence check above (which knows
        # nothing about groups or permissions) to have covered it.
        #
        # Real, confirmed bug found live (2026-07-20): this check ran
        # unconditionally on EVERY round, including intermediate rounds
        # of a DECOMPOSED task (manager/loop.py's _run_decomposed_task)
        # -- which by DESIGN only implements ONE constraint per round
        # and explicitly tells Build "the following constraints are NOT
        # yet in scope for this round" (a real marker in contract.goal,
        # `_NOT_YET_IN_SCOPE_RE` in manager/loop.py). For a task shaped
        # "new model + new group + access rule", round 1 correctly adds
        # only the model -- the group genuinely doesn't exist yet, by
        # design, not because anything is broken. Investigated a real
        # escalation that looked exactly like Code-Review hallucinating
        # a nonexistent scope constraint (it wasn't -- the goal's own,
        # un-truncated text genuinely said the group was "NOT yet in
        # scope for this round"), and traced the REAL failure to this
        # security-claim check firing anyway and reporting "claimed new
        # group X does not exist" as a round failure, when that's the
        # correct, EXPECTED state for every non-final round. Fixed: skip
        # the security-claim check entirely on any round whose own
        # goal text still has remaining not-yet-in-scope constraints
        # (mirrors the exact same marker manager/loop.py's own
        # `_NOT_YET_IN_SCOPE_RE` parses -- duplicated here, not
        # imported, to avoid a specialists->manager import direction
        # this project's own registry pattern deliberately avoids).
        has_remaining_decomposed_constraints = _has_remaining_decomposed_constraints(contract.goal)
        this_rounds_focus_label = _this_rounds_own_focus_label(contract.goal)
        # security_claim already computed concurrently, above.
        # Real fix (2026-07-28): only skip when this round's own real,
        # current focus demonstrably ISN'T security -- "other
        # constraints remain somewhere later" is never enough on its
        # own, or the security_groups round itself would wrongly skip
        # its own verification the instant ANY later constraint
        # (record_rules, computed_age_field, ...) exists. See the
        # `_is_this_rounds_own_focus` docstring above for the full
        # root-cause writeup.
        security_check_passed = True
        security_check_notes = ""
        if security_claim.applicable and has_remaining_decomposed_constraints and not _is_this_rounds_own_focus(
            this_rounds_focus_label, _SECURITY_FOCUS_KEYWORD_RE,
        ):
            security_check_notes = (
                "Skipped: this round's own real current focus "
                f"({this_rounds_focus_label!r}) is a DIFFERENT, not-yet-reached constraint -- "
                "a security-access claim about a LATER constraint's own content is expected "
                "to be unmet at this point, not a failure."
            )
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": security_check_notes, "status": "passed",
            })
        elif security_claim.applicable:
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": f"Verifying real security access claim: {security_claim.model_dump()}", "status": "running",
            })
            security_check_passed, security_check_notes = await self._verify_security_access_claim(
                security_claim, db, task_id=str(contract.task_id), goal=contract.goal,
            )
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": security_check_notes, "status": "passed" if security_check_passed else "failed",
            })

        # Phase 28C (2026-07-28): the real, genuine ground-truth check
        # for a menu-structure claim -- mirrors security_check_passed
        # above exactly, including the SAME corrected own-focus gate
        # (never skip merely because other constraints exist somewhere
        # later; only skip when this round's own real focus is
        # demonstrably a different, not-yet-reached constraint).
        menu_claim_applicable = menu_claim.applicable and bool(menu_claim.menu_names)
        menu_check_passed = True
        menu_check_notes = ""
        if menu_claim_applicable and has_remaining_decomposed_constraints and not _is_this_rounds_own_focus(
            this_rounds_focus_label, _MENU_FOCUS_KEYWORD_RE,
        ):
            menu_check_notes = (
                "Skipped: this round's own real current focus "
                f"({this_rounds_focus_label!r}) is a DIFFERENT, not-yet-reached constraint -- "
                "a menu-structure claim about a LATER constraint's own content is expected "
                "to be unmet at this point, not a failure."
            )
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": menu_check_notes, "status": "passed",
            })
        elif menu_claim_applicable:
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": f"Verifying real menu structure claim: {menu_claim.model_dump()}", "status": "running",
            })
            menu_check_passed, menu_check_notes = await self._verify_menu_structure_claim(
                menu_claim, db, task_id=str(contract.task_id),
            )
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": menu_check_notes, "status": "passed" if menu_check_passed else "failed",
            })

        # P12 Tier A item 25: the field-existence sibling of the security/menu widening above --
        # same trigger (final round only), same reasoning (regression protection for an EARLIER
        # constraint's own claimed field otherwise relies entirely on Build's own non-independent
        # text diff). Only runs when this round's own single-target check actually passed --
        # nothing to widen on top of an already-failing round.
        widening_notes = ""
        if not has_remaining_decomposed_constraints and reproduction_confirmed:
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": "Final round -- re-verifying every earlier constraint's own claimed field, not just this round's…",
                "status": "running",
            })
            earlier_targets_confirmed, widening_notes = await self._reverify_earlier_constraints_field_targets(
                contract, module_name, db, target,
            )
            if not earlier_targets_confirmed:
                reproduction_confirmed = False
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": widening_notes or "Final-round widening: every earlier constraint's own claimed field still confirmed.",
                "status": "passed" if earlier_targets_confirmed else "failed",
            })

        # Step 3/4: self_report and coverage_result already computed
        # concurrently, above -- just combine them.
        publish_trace_event(str(contract.task_id), {
            "level": "specialist", "actor": "testing_qa",
            "message": "Running the deterministic coverage spot-check…", "status": "running",
        })
        # Phase 25C (2026-07-25): exempt a CONFIRMED, verified-correct
        # standard Odoo sequence-assignment idiom (whether LLM-written
        # or autofix-injected) from the install-time-only coverage
        # tool's structural blind spot for create() overrides -- see
        # _exempt_verified_sequence_idiom_from_coverage_gap()'s own
        # docstring. A fresh, direct read (same pattern _extract_
        # reproduction_target()/_extract_security_access_claim() above
        # already use) -- never trusts any cached/self-reported content.
        real_uncovered_paths = coverage_result.uncovered_paths
        try:
            real_module_files = read_module_files(module_name)
            real_uncovered_paths = _exempt_verified_sequence_idiom_from_coverage_gap(
                real_module_files, real_uncovered_paths,
            )
            # Phase 25D (2026-07-26): sibling exemption for the sum-
            # aggregate compute shape -- see that function's own
            # docstring.
            real_uncovered_paths = _exempt_verified_sum_compute_idiom_from_coverage_gap(
                real_module_files, contract.goal, real_uncovered_paths,
            )
            # Phase 25D (2026-07-26): sibling exemption for @api.onchange
            # methods -- see that function's own docstring.
            real_uncovered_paths = _exempt_verified_onchange_idiom_from_coverage_gap(
                real_module_files, contract.goal, real_uncovered_paths,
            )
            # Phase 29 (2026-07-29): sibling exemption for a free-text,
            # own-field compute shape -- see that function's own
            # docstring.
            real_uncovered_paths = _exempt_verified_own_field_compute_idiom_from_coverage_gap(
                real_module_files, contract.goal, real_uncovered_paths,
            )
            # 2026-08-09: sibling exemption for the standard ensure_one()+guarded-state-
            # transition button idiom -- see that function's own docstring.
            real_uncovered_paths = _exempt_verified_state_button_idiom_from_coverage_gap(
                real_module_files, real_uncovered_paths,
            )
            # 2026-08-11: sibling exemption for post_init_hook/pre_init_hook/uninstall_hook --
            # see that function's own docstring.
            real_uncovered_paths = _exempt_verified_install_hook_from_coverage_gap(
                real_module_files, real_uncovered_paths,
            )
        except CodebaseReadError:
            pass  # conservative: if the real code can't be read, never exempt anything
        spot_check_mismatch = compute_spot_check_mismatch(
            self_report.claimed_uncovered_paths, real_uncovered_paths,
            believed_fully_covered=self_report.believed_fully_covered,
        )
        publish_trace_event(str(contract.task_id), {
            "level": "specialist", "actor": "testing_qa",
            "message": "Spot-check found a real, unclaimed gap." if spot_check_mismatch else "Spot-check matches the self-report.",
            "status": "failed" if spot_check_mismatch else "passed",
        })

        passed = (
            reproduction_confirmed and not spot_check_mismatch and security_check_passed
            and menu_check_passed and business_rule_confirmed
        )

        # Phase 25E (2026-07-26): informational-only gap flag -- does
        # NOT affect `passed`/`claims_complete` above. If this goal
        # appears to target a shape with no registered ground-truth
        # verification path (contracts/verifier_registry.py), surface
        # that on the trace and in detail so the gap is visible on this
        # task's own run instead of only being found after several
        # losing rounds (exactly how fix 40's button-groups gap was
        # actually found, live, task 008).
        unverified_shapes = unverified_shapes_targeted(contract)
        if unverified_shapes:
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "testing_qa",
                "message": (
                    f"Note: this goal appears to target shape(s) with no registered "
                    f"ground-truth verification: {', '.join(unverified_shapes)}. "
                    f"Result above still reflects the checks that DO exist."
                ),
                "status": "info",
            })

        return SpecialistOutput(
            task_id=contract.task_id,
            specialist_type=contract.specialist_type,
            summary=(
                f"Reproduction {'confirmed' if reproduction_confirmed else 'FAILED'} for "
                f"{target.model}.{target.field_name}. "
                f"Spot-check {'found a real, unclaimed gap' if spot_check_mismatch else 'matches the self-report'}."
                + (f" Security claim: {security_check_notes}" if security_claim.applicable else "")
                + (f" Menu structure claim: {menu_check_notes}" if menu_claim_applicable else "")
                + (f" {widening_notes}" if widening_notes else "")
                + (f" {business_rule_notes}" if business_rule_notes else "")
            ),
            detail={
                "reproduction_confirmed": reproduction_confirmed,
                "uncovered_paths": real_uncovered_paths,
                "coverage_diff": coverage_result.coverage_diff,
                "spot_check_mismatch": spot_check_mismatch,
                "claimed_uncovered_paths": self_report.claimed_uncovered_paths,
                "self_report_reasoning": self_report.reasoning,
                "escalation_notes": escalation_notes,
                "security_claim": security_claim.model_dump(),
                "security_check_passed": security_check_passed,
                "security_check_notes": security_check_notes,
                "menu_claim": menu_claim.model_dump(),
                "menu_check_passed": menu_check_passed,
                "menu_check_notes": menu_check_notes,
                "unverified_shapes_targeted": unverified_shapes,
                "interpreted_business_rules": contract.interpreted_business_rules,
                "business_rule_check_passed": business_rule_confirmed,
                "business_rule_notes": business_rule_notes,
            },
            claims_complete=passed,
            artifacts=[module_name],
        )

    async def _extract_reproduction_target(self, contract: TaskContract, module_name: str) -> ReproductionTarget:
        """Determines what the task GOAL/DELIVERABLES actually claim,
        then uses the real generated code only to resolve the exact
        technical name when the goal doesn't literally state one --
        never the other way around. This distinction matters a lot:
        reproduction-checking exists specifically to verify a CLAIM,
        never to just describe whatever the code happens to contain
        (a real regression found and fixed during Phase 13's own
        testing -- an earlier version of this prompt let the model
        report the real-but-unclaimed field name in a deliberately
        broken-fix scenario, which trivially "reproduces" anything and
        defeats the entire point of an independent check).

        Reading the real generated code is still required for a
        different, real bug also found this phase: for a brand-new
        model (task 2's shape), the goal text doesn't literally name
        the technical field ("linking to project.project", not
        "project_id"), and a blind guess with no code visibility at
        all can genuinely diverge from what Build actually named it.
        """
        try:
            module_files = read_module_files(module_name)
            models_content = "\n\n".join(
                f"--- {path} ---\n{content}" for path, content in module_files.items() if path.endswith(".py")
            )
        except CodebaseReadError:
            models_content = "(could not read the module's own generated code)"

        # Real, confirmed bug found live (2026-07-23, SITE fix-pass
        # audit): contract.goal, embedded verbatim above, is the FULL
        # original multi-constraint task text on a decomposed round --
        # the same text manager/loop.py's own goal_text already tells
        # Build (and Code-Review) to partly ignore via its own "NOT yet
        # in scope" resolving sentence (see manager/loop.py's
        # _run_constraint_labels_from()). THIS extraction call never
        # got that same treatment: it reads the identical goal text and
        # can pick a field belonging to a LATER, not-yet-built
        # constraint (the goal still literally names it), then reports
        # a false "Reproduction FAILED" when that field genuinely
        # doesn't exist yet -- not because anything is broken, but
        # because it was never supposed to exist this round. Mirrors
        # the same fix already applied to the sibling security-access-
        # claim check below (run(), has_remaining_decomposed_constraints)
        # but scoped narrower on purpose: unlike that check (which can
        # be skipped outright for a mid-sequence round), THIS round's
        # own current-constraint field still genuinely needs checking,
        # so the fix tells the model which mentions to ignore rather
        # than disabling the whole extraction.
        not_yet_match = _NOT_YET_IN_SCOPE_RE.search(contract.goal)
        not_yet_labels = (
            [label.strip() for label in not_yet_match.group(1).split(",") if label.strip()]
            if not_yet_match else []
        )
        scope_note = ""
        if not_yet_labels:
            scope_note = (
                f"\n\nNote: the task goal above is the FULL original multi-part goal, and may "
                f"literally name work belonging to {not_yet_labels} -- those constraints are NOT "
                f"yet in scope for this round (see the goal text's own 'NOT yet in scope' list), so "
                f"do NOT extract a field belonging to them, even if the goal names it literally. "
                f"Extract the field for THIS round's own current focus only."
            )

        prompt = (
            f"Task goal: {contract.goal}{scope_note}\n"
            f"Deliverables: {contract.deliverables}\n\n"
            f"The module's own real, generated code (for resolving the exact technical name only):\n"
            f"{models_content}\n\n"
            "Extract which Odoo model and field name this task's reproduction case should check for. "
            "Priority order, strict: (1) if the goal or deliverables literally name a specific "
            "technical field (e.g. quoted, like 'preferred_language', or 'should_not_exist_field'), "
            "use EXACTLY that name -- this is a claim being verified, not a hint to improve on, even "
            "if that exact name doesn't appear in the code above (that mismatch is precisely what "
            "this check exists to catch). (2) Only if the goal describes a field relationally, "
            "without ever literally naming it (e.g. 'linking to project.project'), use the real code "
            "above to resolve which actual field satisfies that description."
        )
        return await call_structured(
            client=self.client, model=self.fast_extraction_model, prompt=prompt, schema=ReproductionTarget,
            task_id=str(contract.task_id), actor="testing_qa", call_label="Extracting the reproduction target",
            node_id=contract.current_constraint_label,
            use_grammar=True,
        )

    async def _extract_all_reproduction_targets(
        self, contract: TaskContract, module_name: str,
    ) -> ReproductionTargetList:
        """P12 Tier A item 25: the widened, final-round-only sibling of
        `_extract_reproduction_target()` above -- reads the FULL original goal (never a
        narrowed round's own focus) and names every distinct field the goal claims anywhere,
        not just the current round's. Only ever called on the task's final sub-contract round
        (see `run()`'s own call site) -- deliberately not folded into the always-runs extraction
        above, since asking for every field on every intermediate round would re-surface the
        exact "flags a not-yet-built later constraint as broken" bug the `scope_note` fix above
        already closed for the single-target case.
        """
        try:
            module_files = read_module_files(module_name)
            models_content = "\n\n".join(
                f"--- {path} ---\n{content}" for path, content in module_files.items() if path.endswith(".py")
            )
        except CodebaseReadError:
            models_content = "(could not read the module's own generated code)"

        full_goal = contract.original_goal or contract.goal
        prompt = (
            f"Full original task goal (all parts, not just the most recent round's own focus): "
            f"{full_goal}\n"
            f"Deliverables: {contract.deliverables}\n\n"
            f"The module's own real, generated code (for resolving exact technical names only):\n"
            f"{models_content}\n\n"
            "This is the FINAL round of a multi-part task. List EVERY distinct Odoo model+field "
            "pair the FULL goal above claims should exist anywhere -- one entry per real field "
            "described, not just the most recently worked-on one. Same technical-name resolution "
            "rules as always: if a field is literally named (quoted or otherwise exact), use "
            "EXACTLY that name; only resolve from the real code above when the goal describes a "
            "field relationally, without ever literally naming it.\n\n"
            "IMPORTANT, real bug found live (2026-08-08, site_50 benchmark task 007): a quoted "
            "name attached to a BUTTON, FILTER, or MENU ITEM label is NOT a field claim -- e.g. "
            "\"a quick filter button called 'Accepted'\" describes a UI control's own display "
            "label, filtering by an EXISTING status/state field's value, never a requirement that "
            "a field literally named 'accepted' (or any other filter/button/menu label word) "
            "exists on the model. Only extract a model+field pair when the goal describes it as "
            "something a RECORD itself stores or computes (a value on the record), never the "
            "label text of a UI control that merely searches, filters, navigates, or triggers an "
            "action. When genuinely unsure whether a quoted word names a real field or is just a "
            "UI label, do NOT include it -- omitting a real claim here only skips this one "
            "additional regression check (the round's own primary, always-runs check still "
            "verifies its own real target independently); inventing one produces a false "
            "'regression' report on a field that was never real to begin with."
        )
        return await call_structured(
            client=self.client, model=self.fast_extraction_model, prompt=prompt, schema=ReproductionTargetList,
            task_id=str(contract.task_id), actor="testing_qa",
            call_label="Extracting every field claimed by the full original goal (final-round widening)",
            node_id=contract.current_constraint_label,
            use_grammar=True,
        )

    async def _reverify_earlier_constraints_field_targets(
        self, contract: TaskContract, module_name: str, db: str, already_checked: ReproductionTarget,
    ) -> tuple[bool, str]:
        """P12 Tier A item 25: on the final round only, re-verifies every field the FULL
        original goal claims -- not just this round's own single target (`already_checked`,
        skipped here since it was already independently verified moments ago by the normal,
        always-runs check above). Returns (True, "") when every OTHER claimed field is
        confirmed to exist (or none exist to check, or extraction itself failed -- fails open
        the same way `check_hard_governance_gates()` does on a DB error, since this is an
        ADDITIVE regression check, not the task's own primary verification path). Returns
        (False, notes) the instant any earlier constraint's own claimed field is confirmed
        genuinely missing -- a real regression Build's own text-diff heuristic
        (`regressed_constraints`) could easily have missed.
        """
        try:
            target_list = await self._extract_all_reproduction_targets(contract, module_name)
        except Exception:
            return True, ""  # extraction itself failing is not this round's own regression to report

        other_targets = [
            t for t in target_list.targets
            if not (t.model == already_checked.model and t.field_name == already_checked.field_name)
        ]
        # Real, confirmed gap found live (2026-08-06, fix-pass task 004): `_extract_all_
        # reproduction_targets()` above has the EXACT same invented-field failure mode as
        # `_extract_reproduction_target()` -- it is a separate, independent LLM call reading only
        # the goal text (plus code, for relational cases), with no schema-aware correction of its
        # own. Before this fix, a target from this widened list went STRAIGHT to a raw existence
        # check with zero autocorrect, so a hallucinated name here reported a false "regression"
        # even when the round's own single-target check (which DOES run through
        # `_autocorrect_hallucinated_reproduction_target`) just independently confirmed the real
        # field seconds earlier. Confirmed live: task 004's round correctly confirmed
        # `project.fieldjob.amount_total` via the primary check, then this widening step
        # independently re-hallucinated `line_prices_total` and reported it as a lost earlier
        # constraint -- same root cause, same fix, applied here too.
        collision_confirmed_fields = _collision_confirmed_field_names_from_inputs(contract.inputs)
        for target in other_targets:
            target = await self._autocorrect_hallucinated_reproduction_target(
                target, module_name, db, task_id=str(contract.task_id),
                collision_confirmed_fields=collision_confirmed_fields,
                goal_text=contract.goal,
            )
            result = _check_field_exists_on_model(db, target.model, target.field_name, str(contract.task_id))
            if result is False:
                return False, (
                    f"Final-round widening: an EARLIER constraint's own claimed field "
                    f"{target.model}.{target.field_name} no longer exists -- a real regression "
                    f"not caught by this round's own (single-target) check, and independently "
                    f"confirmed live, not just inferred from Build's own text diff."
                )
            # None (genuine uncertainty) and True (confirmed) both leave this earlier target
            # untouched -- never fail the round on infra uncertainty alone, same conservative-
            # but-not-punitive posture as the primary single-target check.
        return True, ""

    async def _synthesize_behavioral_probe(
        self, contract: TaskContract, target: ReproductionTarget, models_content: str,
    ) -> BehavioralProbeSpec:
        """Phase 30, P1c (Phase I, §12): the real widening this priority
        exists for -- "verified" used to mean only "the field exists"
        (`_check_field_exists_on_model()`), never "the field does what
        the goal said." Synthesizes STRUCTURED create values (never raw
        code -- see `BehavioralProbeSpec`'s own docstring) for a real,
        minimal probe: create a record (and, when the goal's own claim
        needs it, a related/child record referencing it via the "$0"
        placeholder `run_behavioral_probe()` resolves), then read back
        the real, live result of the compute/action afterward.

        Deliberately reuses `target`/`models_content` already resolved
        by `_extract_reproduction_target()` above -- same real generated
        code, same already-established model/field -- rather than a
        second, independent extraction pass that could disagree with it.
        """
        prompt = (
            f"Task goal: {contract.goal}\n"
            f"Deliverables: {contract.deliverables}\n\n"
            f"The module's own real, generated code:\n{models_content}\n\n"
            f"This task's own claimed field/effect: {target.model}.{target.field_name}\n\n"
            "Synthesize a MINIMAL real behavioral probe that would prove whether this claim "
            "genuinely holds, not just that the field exists. Specify exactly which real record(s) "
            "to create (in dependency order -- a parent record before any child that must reference "
            "it), what field values to set on each (real, concrete, plausible values -- e.g. a real "
            "quantity and price, not placeholders), and which field(s) to read back afterward on the "
            "TARGET record (the one whose real post-write state actually proves the claim). "
            "A child record's own value that must reference an earlier record's real id should be "
            "the literal string '$0' for the record at list index 0, '$1' for index 1, and so on -- "
            "never a real id you invent yourself, since the real id doesn't exist until creation. "
            "CRITICAL: every child/related record (e.g. an order's own lines) MUST be its own "
            "separate entry in the creates list, referencing its parent via '$N' -- never Odoo's "
            "one2many command-tuple syntax (e.g. never a value like \"[(0, 0, {...})]\") embedded "
            "inside a parent's own values dict. Every value must be a plain string, number, boolean, "
            "or null -- never a list, dict, or any value that looks like Python/Odoo code.\n\n"
            "Also state, in plain English, what you EXPECT the checked field's real value to show if "
            "the claim genuinely holds (e.g. 'amount_total should be 300, the quantity (2) times the "
            "unit price (150)') -- this is a prediction to check against the REAL result afterward, "
            "not something you compute and report as if you'd already verified it.\n\n"
            "applicable=false only if this task's own claim genuinely has no checkable compute/action "
            "effect (e.g. a plain, non-computed field with no derived behavior) -- most computed-field "
            "or action-button claims ARE checkable this way."
        )
        return await call_structured(
            client=self.client, model=self.routine_model, prompt=prompt, schema=BehavioralProbeSpec,
            task_id=str(contract.task_id), actor="testing_qa",
            node_id=contract.current_constraint_label,
            call_label="Synthesizing a real behavioral probe", use_grammar=True,
        )

    async def _judge_behavioral_probe_result(
        self, contract: TaskContract, target: ReproductionTarget, probe: BehavioralProbeSpec, real_result: dict,
    ) -> BehavioralProbeVerdict:
        """The other half of the "ground truth first, then judge"
        discipline this whole specialist already uses elsewhere (Code-
        Review reads real diffs, not descriptions; this reads a REAL,
        just-observed result, never a value the model predicted before
        the probe ran) -- deliberately a SEPARATE call from synthesis
        above, so the verdict is never just the synthesis step re-
        asserting its own prior guess.
        """
        prompt = (
            f"Task goal: {contract.goal}\n\n"
            f"A real record was just created in the live system with these values: "
            f"{[c.model_dump() for c in probe.creates]}\n\n"
            f"The expected effect, stated before the probe ran: {probe.expected_effect!r}\n\n"
            f"The REAL, actually-observed result read back afterward: {real_result}\n\n"
            "Does the REAL observed result genuinely confirm the task goal's own claim about "
            f"{target.model}.{target.field_name}? Judge only by the real observed values above -- "
            "never assume the expected-effect prediction was correct without checking it against "
            "what was actually observed."
        )
        return await call_structured(
            client=self.client, model=self.routine_model, prompt=prompt, schema=BehavioralProbeVerdict,
            task_id=str(contract.task_id), actor="testing_qa",
            node_id=contract.current_constraint_label,
            call_label="Judging the real behavioral probe result", use_grammar=True,
        )

    async def _run_behavioral_probe_check(
        self, contract: TaskContract, target: ReproductionTarget, module_name: str, db: str,
    ) -> tuple[bool, str] | None:
        """Orchestrates synthesis -> real execution -> judgment. Returns
        None (never a guessed pass/fail) whenever the probe itself
        couldn't run for real (synthesis says not applicable, or
        `run_behavioral_probe()` returns None -- a real create/read
        failure, a timeout, anything genuinely uncertain) -- the caller
        falls back to the existing field-existence check in that case,
        never silently treats "couldn't run the probe" as "confirmed."
        """
        try:
            module_files = read_module_files(module_name)
            models_content = "\n\n".join(
                f"--- {path} ---\n{content}" for path, content in module_files.items() if path.endswith(".py")
            )
        except CodebaseReadError:
            models_content = "(could not read the module's own generated code)"

        probe = await self._synthesize_behavioral_probe(contract, target, models_content)
        if not probe.applicable or not probe.creates or not probe.check_fields:
            return None
        real_result = run_behavioral_probe(
            db,
            [(c.model, c.values) for c in probe.creates],
            probe.check_fields,
            target_index=probe.target_index,
        )
        if real_result is None:
            return None
        verdict = await self._judge_behavioral_probe_result(contract, target, probe, real_result)
        message = (
            f"Behavioral probe for {target.model}.{target.field_name}: real observed result "
            f"{real_result} -- {verdict.reasoning}"
        )
        return verdict.behavior_confirmed, message

    async def _synthesize_business_rule_probe(
        self, contract: TaskContract, rule: str, models_content: str,
    ) -> BehavioralProbeSpec:
        """P13 item 12a: the business-rule sibling of `_synthesize_behavioral_probe()` above --
        same STRUCTURED-data-only contract, same `run_behavioral_probe()` execution mechanism, but
        keyed to one entry of `contract.interpreted_business_rules` (a specific INTERPRETATION,
        e.g. "the order total includes tax") rather than a single claimed field/effect. Deliberately
        a separate function, not a generalization of the sibling above, since the prompt framing
        (probing whether a stated INTERPRETATION holds, not whether a claimed field exists) is
        genuinely different content, matching this file's own established convention of parallel,
        non-shared prompt functions for genuinely different verification questions.
        """
        prompt = (
            f"Task goal: {contract.goal}\n"
            f"Deliverables: {contract.deliverables}\n\n"
            f"The module's own real, generated code:\n{models_content}\n\n"
            f"This specific business-logic interpretation to verify: {rule!r}\n\n"
            "Synthesize a MINIMAL real behavioral probe that would prove whether this specific "
            "interpretation genuinely holds in the real, generated code -- not just that some "
            "field exists. Specify exactly which real record(s) to create (in dependency order), "
            "what field values to set on each (real, concrete, plausible values chosen so the "
            "interpretation is actually exercised -- e.g. if checking 'total includes tax', set a "
            "real tax rate and a real line price so the difference is observable), and which "
            "field(s) to read back afterward on the TARGET record. A child record's own value that "
            "must reference an earlier record's real id should be the literal string '$0' for the "
            "record at list index 0, and so on -- never a real id you invent yourself. Every "
            "child/related record MUST be its own separate entry in the creates list, never Odoo's "
            "one2many command-tuple syntax embedded inside a parent's own values dict. Every value "
            "must be a plain string, number, boolean, or null.\n\n"
            "Also state, in plain English, what you EXPECT the checked field's real value to show "
            "if this specific interpretation genuinely holds -- a prediction to check against the "
            "REAL result afterward, not something you compute and report as already verified.\n\n"
            "applicable=false only if this specific interpretation genuinely has no checkable "
            "compute/action effect in the real generated code shown above."
        )
        return await call_structured(
            client=self.client, model=self.routine_model, prompt=prompt, schema=BehavioralProbeSpec,
            task_id=str(contract.task_id), actor="testing_qa",
            node_id=contract.current_constraint_label,
            call_label="Synthesizing a business-rule probe", use_grammar=True,
        )

    async def _judge_business_rule_probe_result(
        self, contract: TaskContract, rule: str, probe: BehavioralProbeSpec, real_result: dict,
    ) -> BehavioralProbeVerdict:
        """Business-rule sibling of `_judge_behavioral_probe_result()` above -- same "ground truth
        first, then judge" discipline, separate call from synthesis.
        """
        prompt = (
            f"Task goal: {contract.goal}\n\n"
            f"This specific business-logic interpretation being verified: {rule!r}\n\n"
            f"A real record was just created in the live system with these values: "
            f"{[c.model_dump() for c in probe.creates]}\n\n"
            f"The expected effect, stated before the probe ran: {probe.expected_effect!r}\n\n"
            f"The REAL, actually-observed result read back afterward: {real_result}\n\n"
            "Does the REAL observed result genuinely confirm this specific interpretation? Judge "
            "only by the real observed values above -- never assume the expected-effect prediction "
            "was correct without checking it against what was actually observed."
        )
        return await call_structured(
            client=self.client, model=self.routine_model, prompt=prompt, schema=BehavioralProbeVerdict,
            task_id=str(contract.task_id), actor="testing_qa",
            node_id=contract.current_constraint_label,
            call_label="Judging the business-rule probe result", use_grammar=True,
        )

    async def _run_business_rule_probes(
        self, contract: TaskContract, module_name: str, db: str,
    ) -> tuple[bool, str]:
        """P13 item 12a: for every entry in `contract.interpreted_business_rules`, synthesize,
        run, and judge a real probe that specifically exercises that interpretation -- not just
        structural existence of a field. Returns (all_confirmed, notes); notes carries a
        "Business rule check FAILED:" marker per failed rule so
        `manager/replanning.py`'s `_build_failure_record()` can classify it as
        `FailureCategory.business_rule_mismatch` (item 7 integration) rather than falling through
        to the generic `reproduction_gap` category.

        Same "never guess" posture as the sibling behavioral-probe check: a rule this task's own
        goal doesn't actually trigger returns True with no work done (empty
        `interpreted_business_rules`); a probe that can't run for real (not applicable, a real
        create/read failure, genuine infra uncertainty) is silently skipped for THAT rule, never
        counted as a failure -- only a real, observed, judged mismatch fails the round.
        """
        if not contract.interpreted_business_rules:
            return True, ""
        try:
            module_files = read_module_files(module_name)
            models_content = "\n\n".join(
                f"--- {path} ---\n{content}" for path, content in module_files.items() if path.endswith(".py")
            )
        except CodebaseReadError:
            return True, ""  # can't read the real code -- never fail on infra uncertainty alone

        failures: list[str] = []
        for rule in contract.interpreted_business_rules:
            probe = await self._synthesize_business_rule_probe(contract, rule, models_content)
            if not probe.applicable or not probe.creates or not probe.check_fields:
                continue
            real_result = run_behavioral_probe(
                db, [(c.model, c.values) for c in probe.creates], probe.check_fields,
                target_index=probe.target_index,
            )
            if real_result is None:
                continue
            verdict = await self._judge_business_rule_probe_result(contract, rule, probe, real_result)
            if not verdict.behavior_confirmed:
                failures.append(
                    f"Business rule check FAILED: {rule!r} -- real observed result {real_result}: "
                    f"{verdict.reasoning}"
                )
        if failures:
            return False, " ".join(failures)
        return True, ""

    async def _autocorrect_hallucinated_reproduction_target(
        self, target: ReproductionTarget, module_name: str, db: str, task_id: str | None = None,
        collision_confirmed_fields: list[str] | None = None, goal_text: str = "",
    ) -> ReproductionTarget:
        """Real, general fix found live (2026-07-17, Phase 20 Area 2):
        this specialist's own `_extract_reproduction_target()` has the
        EXACT same invented-field failure mode
        `specialists/build/specialist.py`'s
        `_validate_ir_rule_domain_fields_exist` was built to catch on
        the Build side -- but here it's a COMPLETELY SEPARATE LLM call,
        with no schema-aware validation of its own, so Build's fix does
        nothing for it. Confirmed live: a real record-rule task's goal
        described the field only relationally ("records they
        themselves created or are responsible for", never literally
        naming a field) -- `_extract_reproduction_target`'s own prompt
        rule (2) says to resolve the real name from the generated code
        in that case, and the generated code WAS correct
        (`user_ids`, confirmed via direct commit inspection) -- but the
        extraction call still returned the hallucinated `user_id`
        anyway, producing a false "Reproduction FAILED" on an otherwise
        working module, 4 of 5 rounds in a row.

        Deliberately conservative, mirroring the same "closest real
        field" logic already proven in Build's own fix -- but here used
        as a real CORRECTION, not just better error text, since this
        check has direct access to ground truth (the real generated
        code) to confirm a correction is warranted: only fires when
        (a) the extracted field genuinely doesn't exist on the real
        model, (b) exactly ONE real field shares a name-stem with it
        (e.g. 'user_id' / 'user_ids' -- ambiguous cases are left alone,
        never guessed), AND (c) that real field is actually referenced
        somewhere in the module's own real generated code (proving
        Build genuinely used it, not just a coincidental name match).
        """
        # P12 Tier A item 12: None (genuine uncertainty -- an infra failure, not a confirmed
        # absence) must never be silently treated as falsy here, same "never guess" discipline
        # already documented two lines below for the real_fields-empty case. Leaves target
        # unchanged, same as any other genuine-uncertainty case in this function.
        field_check_result = _check_field_exists_on_model(db, target.model, target.field_name, task_id)
        if field_check_result is None:
            return target  # genuine uncertainty -- never guess
        if field_check_result:
            return target  # genuinely real -- nothing to correct
        # Real, confirmed gap found live (2026-08-06, fix-pass task 004): checked BEFORE the
        # stem-match/newly-defined-field tiers below, since this is Build's own confirmed ground
        # truth from a live schema check it performed in THIS SAME round (see
        # manager/tools.py's `_extract_collision_confirmed_field_names()` for the full chain from
        # `_ALREADY_SATISFIED_BY_COLLISION_MARKER`), not a name-similarity guess -- strictly more
        # reliable than either tier below. Only fires when exactly one collision-confirmed field
        # was reported (ambiguous cases are left alone, same "never guess" discipline as every
        # other tier here). Confirmed live: task 004's goal was genuinely satisfied by a real,
        # live `amount_total` field (a leftover from an earlier real attempt on this same shared
        # dev database) -- Build correctly recognized this and stripped its own redundant
        # re-declaration, so NEITHER the stem-match tier (zero stem overlap with the hallucinated
        # 'line_prices_total') NOR the newly-defined-field tier (the field was, correctly,
        # removed from models_py, so it was never a "new" declaration at all) could ever find it.
        if collision_confirmed_fields and len(collision_confirmed_fields) == 1:
            return ReproductionTarget(model=target.model, field_name=collision_confirmed_fields[0])
        if task_id and is_fast_path_eligible(db):
            from tools_odoo.odoo_schema_client import get_model_fields_fast

            real_fields = await asyncio.to_thread(get_model_fields_fast, target.model, db)
        else:
            real_fields = await asyncio.to_thread(get_model_fields, target.model, db)
        if not real_fields:
            return target  # genuine uncertainty -- never guess
        try:
            module_files = read_module_files(module_name)
        except CodebaseReadError:
            return target
        code_content = "\n".join(
            content for path, content in module_files.items() if path.endswith((".py", ".xml"))
        )
        stem = re.sub(r"_ids?$", "", target.field_name).lower()
        candidates = [
            f for f in real_fields
            if stem and stem in f.lower() and re.search(rf"\b{re.escape(f)}\b", code_content)
        ]
        if len(candidates) == 1:
            return ReproductionTarget(model=target.model, field_name=candidates[0])
        # Real, confirmed gap found live (2026-08-06, fix-pass task004): the stem-match tier
        # above only catches NEAR-MISS hallucinations (plural/singular, e.g. user_id/user_ids)
        # -- it does nothing when the extraction invents a name with zero stem overlap with the
        # real field (goal: "total of all line prices... in the header", extracted
        # "line_prices_total", real generated field "amount_total" -- confirmed via direct diff
        # inspection: models.py genuinely defines `amount_total = fields.Monetary(...,
        # compute=...)` with a correct compute method; the extraction call simply never read it,
        # despite its own prompt rule (2) requiring exactly that when the goal only describes the
        # field relationally). Second, still-conservative tier: if the module's own generated
        # models.py defines EXACTLY ONE new field via a `fields.<Type>(...)` assignment, and that
        # field is confirmed live on the model, it is overwhelmingly likely to be the field this
        # task's reproduction check is actually about -- a task round only ever adds one field at
        # a time in the common case, and ambiguous multi-field rounds still fall through to the
        # "leave unchanged" case below, same "never guess" discipline as the tier above.
        # Real, confirmed bug found live (2026-08-11, task 18fca388's own regression test,
        # test_deliberately_broken_fix_is_caught_by_reproduction_check): this tier's own
        # "a task round only ever adds one field at a time" assumption is only safe when the
        # goal's own claim was genuinely UNCERTAIN to begin with (rule (2) in
        # `_extract_reproduction_target()`'s prompt -- "describes a field relationally,
        # without ever literally naming it", exactly this tier's own justification above). When
        # the goal instead LITERALLY, explicitly names the exact field it claims (rule (1) --
        # "this is a claim being verified, not a hint to improve on"), silently substituting
        # whatever ONE unrelated field the module happens to define defeats the entire point of
        # an independent reproduction check -- confirmed live: a deliberately broken fix that
        # claims to add 'should_not_exist_field' but actually defines a completely unrelated
        # 'a_completely_different_field' was wrongly "corrected" to the wrong field and reported
        # as a passing reproduction, exactly the failure mode this whole check exists to catch.
        # Gated on whether the goal ever literally names `target.field_name` as a token
        # (word-boundary match, not merely a name-stem) -- if it does, this was an explicit
        # claim, and a genuinely missing field must stay a genuine failure, never "corrected"
        # to an unrelated field just because it's the only one present.
        field_defs = set(_MODEL_FIELD_ASSIGN_RE.findall(code_content))
        live_field_defs = [f for f in field_defs if f in real_fields]
        goal_literally_names_target = bool(
            target.field_name and re.search(rf"\b{re.escape(target.field_name)}\b", goal_text or "")
        )
        if len(live_field_defs) == 1 and not goal_literally_names_target:
            return ReproductionTarget(model=target.model, field_name=live_field_defs[0])
        # Real, confirmed gap found live (2026-08-06, fix-pass task048): every tier above only
        # ever corrects the FIELD NAME while trusting the extraction's own claimed MODEL --
        # confirmed live, task048's own extraction returned model='project.satisfaction',
        # field_name='satisfaction_ids' literally (the field name IS real, per the extraction
        # prompt's own rule (1): the goal literally names 'satisfaction_ids', so it correctly
        # extracted that exact string) -- but the goal's own text is explicit that this field
        # lives on a DIFFERENT model ("Inherit project.project to add satisfaction_ids
        # One2many"), which the extraction call simply attributed to the wrong one of the two
        # models the goal names. Every tier above stays scoped to `target.model` and can never
        # find this class of mistake. Deliberately conservative, same "exactly one candidate"
        # discipline as every sibling tier: only ever fires when (a) a goal text was actually
        # passed in, (b) the goal literally names at least one OTHER dotted model (never a guess
        # at what model COULD exist), and (c) the exact same field_name is confirmed live on
        # EXACTLY ONE of those other named models -- multiple candidates or zero real matches are
        # both left alone, never guessed at.
        if goal_text and task_id and is_fast_path_eligible(db):
            from tools_odoo.odoo_schema_client import get_model_fields_fast as _get_model_fields_fast_cross

            other_named_models = sorted({
                m for m in _DOTTED_MODEL_NAME_IN_TEXT_RE.findall(goal_text) if m != target.model
            })
            cross_model_matches = []
            for candidate_model in other_named_models:
                candidate_fields = await asyncio.to_thread(_get_model_fields_fast_cross, candidate_model, db)
                if candidate_fields and target.field_name in candidate_fields:
                    cross_model_matches.append(candidate_model)
            if len(cross_model_matches) == 1:
                return ReproductionTarget(model=cross_model_matches[0], field_name=target.field_name)
        return target  # no unambiguous real match -- leave as-is, don't guess

    async def _extract_security_access_claim(self, contract: TaskContract, db: str) -> SecurityAccessClaim:
        """Real, general, conceptual fix (2026-07-17, Phase 20 Area 2):
        extracts whatever concrete, checkable security claim the goal
        actually makes -- a NEW group being created (with specific
        model-level permissions), or an EXISTING field's visibility
        being restricted to a group. Deliberately does NOT read the
        generated code (unlike _extract_reproduction_target) -- the
        whole point is verifying what the goal CLAIMS should exist in
        the live registry, independent of what Build happened to write,
        so a wrong or incomplete generation can never trivially "pass"
        by having the extraction just describe the code instead.

        Phase 33 follow-up (2026-08-11): real, confirmed live gap -- for
        SHAPE 2/3 (a plain role phrase, e.g. "only Administrators should see
        X," with no literal xmlid stated), this used to ask an LLM to guess
        `group_name`/`restricted_group_xmlid` directly from the goal text
        alone. Confirmed live: the LLM invented a plausible-looking but
        entirely fabricated xmlid ("oma.group_admin") that was never in the
        goal at all, causing a false "mismatch" failure against Build's own
        now-CORRECT, deterministically-resolved real code. Fixed the same way
        every other freehand-guessing gap in this pipeline gets fixed: try the
        SAME deterministic, live-validated extractor Build itself uses
        (tools_odoo/acl_request_extractor.py) FIRST, and only fall through to
        the LLM extraction below when it can't resolve anything -- never let
        an LLM guess a specific technical identifier this pipeline already
        has a deterministic way to resolve correctly.
        """
        import re as _re
        from tools_odoo.acl_request_extractor import ResolvedAclTarget, extract_acl_request, resolve_acl_request
        from contracts.module_identity import resolve_module_identity

        extracted = extract_acl_request(contract.goal or "")
        if extracted is not None:
            model_name = resolve_module_identity(contract.original_goal or contract.goal)
            # The exact technical field name (from this project's own `Field: <name> (...)`
            # convention), not the prose target description -- the field-existence checks
            # downstream need the real snake_case identifier to look up.
            field_match = _re.search(r"Field:\s*(\w+)", contract.goal or "")
            exact_field_name = field_match.group(1) if field_match else None
            if model_name and exact_field_name:
                resolved = resolve_acl_request(extracted, model_name, db)
                # Only short-circuits the FIELD shape (SHAPE 2) -- the exact technical field name
                # is deterministically available via this project's own `Field:` convention. A
                # button's real technical method name (SHAPE 3) genuinely needs more resolution
                # than this goal-text extractor provides (see the LLM prompt's own "your best
                # resolution of what the button's real technical name would be") -- that part
                # still falls through to the LLM below, unchanged; only the group-identifier
                # guessing (the part actually confirmed live to fabricate) is closed here.
                if isinstance(resolved, ResolvedAclTarget) and resolved.is_field:
                    return SecurityAccessClaim(
                        applicable=True, model=model_name,
                        restricted_field_name=exact_field_name,
                        restricted_group_xmlid=resolved.resolved_group_external_id,
                    )

        prompt = (
            f"Task goal: {contract.goal}\nDeliverables: {contract.deliverables}\n\n"
            "Determine whether this goal makes a checkable claim about ONE of these three shapes "
            "(set applicable=true and fill in exactly the relevant fields; if none apply, "
            "set applicable=false and leave everything else null):\n\n"
            "SHAPE 1 -- a NEW security group with model-level access: if the goal names a new "
            "security group (quoted or clearly named) and states what CRUD access it should have on "
            "a model, set group_name (exact name), model (technical name, e.g. 'res.partner'), and "
            "expects_read/expects_write/expects_create/expects_unlink to true/false based on exactly "
            "what the goal states (e.g. 'full read, write, create, and delete access' -> all four "
            "true; 'read and write access, but not create or delete' -> read=true, write=true, "
            "create=false, unlink=false; 'read-only access' -> read=true, write=false, create=false, "
            "unlink=false). Only set a expects_* field you're confident the goal actually states -- "
            "leave uncertain ones null rather than guessing. Leave restricted_field_name and "
            "restricted_button_name null.\n\n"
            "SHAPE 2 -- restricting an EXISTING field's visibility to a group: if the goal says an "
            "existing field on a model should only be visible to members of a new/named group "
            "(everyone else should not see it), set model, restricted_field_name (the exact technical "
            "field name), and EITHER restricted_group_xmlid (when the goal literally states a group "
            "as 'module.group_name', e.g. 'groups=base.group_system' or 'mis_base_extend.group_x' -- "
            "ALWAYS prefer this over group_name when a literal xmlid is stated) OR group_name (a plain "
            "display name like 'Managers', only when no literal xmlid is given). Leave expects_* and "
            "restricted_button_name all null.\n\n"
            "SHAPE 3 -- restricting an EXISTING button's visibility to a group: if the goal says a "
            "named button (e.g. 'Send to customer') on a form should only be visible to a specific "
            "group, set model, restricted_button_name (the exact technical `name` attribute -- e.g. "
            "'action_send' -- if the goal states it literally; otherwise your best resolution of what "
            "the button's real technical name would be), and EITHER restricted_group_xmlid (when the "
            "goal literally states a group as 'module.group_name', e.g. 'groups=base.group_system' or "
            "'base.group_system' -- ALWAYS prefer this over group_name when a literal xmlid is stated) "
            "OR group_name (a plain display name like 'Managers', only when no literal xmlid is given). "
            "Leave expects_* and restricted_field_name null.\n\n"
            "If the goal is none of these (e.g. a plain field-add task, a record rule with a domain "
            "but no group/permission claim, a menu-visibility restriction), set applicable=false."
        )
        return await call_structured(
            client=self.client, model=self.fast_extraction_model, prompt=prompt, schema=SecurityAccessClaim,
            task_id=str(contract.task_id), actor="testing_qa", call_label="Extracting the security access claim",
            node_id=contract.current_constraint_label,
            use_grammar=True,
        )

    async def _verify_security_access_claim(
        self, claim: SecurityAccessClaim, db: str, task_id: str | None = None, goal: str = "",
    ) -> tuple[bool, str]:
        """The real, deterministic ORM-level verification behind
        `_extract_security_access_claim` -- never trusts the extraction
        alone, always checks the live registry. Conservative on any
        genuine uncertainty (a check returning None): treated as NOT
        passed, since silently ignoring an unverifiable claim would
        recreate the exact false-positive gap this whole fix exists to
        close -- better a false escalation needing a human look than a
        false "passed" on an unverified security claim.

        Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_access_restriction
        node): `SecurityAccessClaim.group_name` is a single field -- built for Area 2's own
        original claim shapes, each naming exactly ONE group -- but a goal restricting access to
        TWO named groups at once (e.g. "so that only the existing group_field_technician and
        group_operations_manager groups can access oma.service.ticket") gives the extraction LLM
        no single correct answer, and it left `group_name` empty rather than guess, correctly
        (and unhelpfully) triggering "security claim incomplete" even though the real, live,
        already-verified module content was fully correct. Rather than risk a wider schema
        change (`SecurityAccessClaim` has many existing call sites) this late, added a narrow,
        additive fallback: when `group_name` is empty but the goal's own literal "so that only
        the existing X (...) and Y (...) groups can access Z" sentence names real groups, verify
        EACH one directly against the live registry (reusing the exact same `_group_exists`/
        `_group_model_access` helpers below) rather than surface "incomplete" for a claim that
        is, in fact, fully checkable from the goal's own real ground truth.
        """
        # Phase 21 (2026-07-22): try-fast-then-fall-back, NOT pick-once
        # -- a real, confirmed false-negative bug found live before
        # this was deployed: the fast functions authenticate as a
        # fixed "Admin" login (confirmed on odoo16_dev, NOT guaranteed
        # on a fresh/duplicate/sandbox database), so on such a
        # database they correctly return None (genuine uncertainty) --
        # but picking the fast function ONCE and treating its None as
        # "could not verify" (a real, reported failure) would silently
        # give up instead of falling back to the original, universal
        # shell version that needs no login at all. Each helper below
        # tries fast first when task_id is given, and only uses the
        # slow, universal path when fast couldn't get a confident answer.
        async def _field_group_restricted(model: str, field_name: str, group_name: str | None, group_xmlid: str | None):
            # Phase 26A follow-up (2026-07-27): group_xmlid support found
            # missing live -- see check_field_group_restricted_fast()'s
            # own docstring for the full story (the button-restriction
            # sibling got this in Phase 25F/fix 40; this one never did).
            if task_id and is_fast_path_eligible(db):
                from tools_odoo.odoo_schema_client import check_field_group_restricted_fast

                result = await asyncio.to_thread(
                    check_field_group_restricted_fast, db, model, field_name, group_name, group_xmlid,
                )
                if result is not None:
                    return result
            return await asyncio.to_thread(check_field_group_restricted, db, model, field_name, group_name, group_xmlid)

        async def _group_exists(group_name: str):
            """Returns (exists, resolved_name) -- resolved_name is group_name unchanged unless a
            singular/plural variant is what actually matched (see _singular_plural_variant()'s own
            docstring for the real incident this closes), in which case callers needing to check
            that group's OWN model-level permissions afterward must use resolved_name, not the
            original claimed spelling, or that follow-up check would fail identically.

            Real, confirmed bug found live overnight (2026-08-14, recurring on 2 separate real
            tasks, both correctly generating a real, valid `base.group_user` CSV row): both fast
            and direct paths above only ever match `res.groups`' own DISPLAY name ("Internal
            User"), never an XML-ID-style token ("base.group_user") -- exactly the same naming-
            convention mismatch `resolve_group_xmlid_to_display_name()` (tools_odoo/spot_check.py)
            was already built to close, and IS already used for this exact purpose one call site
            over (the `_GOAL_MULTI_GROUP_ACCESS_RESTRICTION_RE` branch above), but was never
            applied here, in the main/far more common path most real security-claim checks
            actually go through. If a direct/variant lookup finds nothing AND `group_name` looks
            like an XML-ID (contains a dot, or is a bare `group_*` token), this now also tries
            resolving it to its real display name before concluding the group doesn't exist.
            """
            variant = _singular_plural_variant(group_name)
            # `definitive_negative` tracks whether every check that actually returned a real
            # answer (never a None/uncertain one) came back False -- only then is the eventual
            # fallback allowed to report a confirmed "does not exist". If ANY check along the way
            # returned None (a genuine uncertainty, e.g. an SSH/network hiccup), that must win:
            # returning a confirmed False in that case would be the exact false-negative
            # regression class this whole function exists to prevent (see
            # test_verify_security_access_claim_genuine_uncertainty_fails_conservatively).
            definitive_negative = True

            if task_id and is_fast_path_eligible(db):
                from tools_odoo.odoo_schema_client import check_group_exists_fast

                result = await asyncio.to_thread(check_group_exists_fast, db, group_name)
                if result:
                    return True, group_name
                if result is None:
                    definitive_negative = False
                if variant:
                    variant_result = await asyncio.to_thread(check_group_exists_fast, db, variant)
                    if variant_result:
                        return True, variant
                    if variant_result is None:
                        definitive_negative = False
            else:
                direct_result = await asyncio.to_thread(check_group_exists, db, group_name)
                if direct_result:
                    return direct_result, group_name
                if direct_result is None:
                    definitive_negative = False
                if variant:
                    variant_direct_result = await asyncio.to_thread(check_group_exists, db, variant)
                    if variant_direct_result:
                        return True, variant
                    if variant_direct_result is None:
                        definitive_negative = False

            if _looks_like_group_xmlid(group_name):
                from tools_odoo.spot_check import resolve_group_xmlid_to_display_name

                resolved_display_name = await asyncio.to_thread(
                    resolve_group_xmlid_to_display_name, db, group_name,
                )
                if resolved_display_name:
                    return True, resolved_display_name
                # resolve_group_xmlid_to_display_name() returns None both for "genuinely no such
                # group" and "query itself failed" (same conservative posture as every other
                # check in tools_odoo/spot_check.py) -- it cannot be split further, so it is
                # deliberately NOT allowed to downgrade an already-uncertain result to definitive,
                # but also does not itself force uncertainty if the earlier checks were definitive.

            return (None if not definitive_negative else False), group_name

        async def _group_model_access(group_name: str, model: str, er, ew, ec, eu):
            if task_id and is_fast_path_eligible(db):
                from tools_odoo.odoo_schema_client import check_group_model_access_fast

                result = await asyncio.to_thread(check_group_model_access_fast, db, group_name, model, er, ew, ec, eu)
                if result is not None:
                    return result
            return await asyncio.to_thread(check_group_model_access, db, group_name, model, er, ew, ec, eu)

        async def _button_group_restricted(model: str, button_name: str, group_xmlid: str | None, group_name: str | None):
            # P12 Tier A item 12: the odoo-bin-shell fallback this sibling was missing --
            # same try-fast-then-fall-back discipline as the three siblings above, not a
            # pick-once. Previously returned None unconditionally whenever the fast path
            # wasn't eligible or came back uncertain, so this check could never pass at all on
            # a non-fast-path DB regardless of correctness.
            if task_id and is_fast_path_eligible(db):
                from tools_odoo.odoo_schema_client import check_button_group_restricted_fast

                result = await asyncio.to_thread(
                    check_button_group_restricted_fast, db, model, button_name, group_xmlid, group_name,
                )
                if result is not None:
                    return result
            return await asyncio.to_thread(
                check_button_group_restricted, db, model, button_name, group_xmlid, group_name,
            )

        if claim.restricted_button_name:
            if not (claim.model and (claim.restricted_group_xmlid or claim.group_name)):
                return False, (
                    "security claim incomplete (missing model or a claimed group for a "
                    "button-visibility claim)"
                )
            result = await _button_group_restricted(
                claim.model, claim.restricted_button_name, claim.restricted_group_xmlid, claim.group_name,
            )
            if result is None:
                return False, (
                    f"could not verify button-visibility restriction for {claim.model}."
                    f"{claim.restricted_button_name} (genuine uncertainty -- treated as not verified)"
                )
            return result

        if claim.restricted_field_name:
            if not (claim.model and (claim.restricted_group_xmlid or claim.group_name)):
                return False, (
                    "security claim incomplete (missing model or a claimed group for a "
                    "field-visibility claim)"
                )
            result = await _field_group_restricted(
                claim.model, claim.restricted_field_name, claim.group_name, claim.restricted_group_xmlid,
            )
            if result is None:
                return False, (
                    f"could not verify field-visibility restriction for {claim.model}."
                    f"{claim.restricted_field_name} (genuine uncertainty -- treated as not verified)"
                )
            return result

        if not claim.group_name and goal:
            # Real, confirmed bug found live (2026-08-10, task e65381cc,
            # ticket_access_restriction node): `SecurityAccessClaim.group_name` is a single
            # field -- built for Area 2's own original claim shapes, each naming exactly ONE
            # group -- but a goal restricting access to TWO named groups at once (e.g. "so
            # that only the existing group_field_technician and group_operations_manager
            # groups can access oma.service.ticket") gives the extraction LLM no single
            # correct answer, and it left `group_name` empty rather than guess -- correctly
            # (and unhelpfully) triggering "security claim incomplete" even though the real,
            # live, already-verified module content was fully correct. Rather than risk a
            # wider schema change (`SecurityAccessClaim` has many existing call sites) this
            # late, added this narrow, additive fallback: when `group_name` is empty but the
            # goal's own literal "so that only the existing X (...) and Y (...) groups can
            # access Z" sentence names real groups, verify EACH one directly against the live
            # registry using the SAME `_group_exists`/`_group_model_access` helpers just above,
            # rather than surface "incomplete" for a claim that is, in fact, fully checkable
            # from the goal's own real ground truth.
            multi_group_match = _GOAL_MULTI_GROUP_ACCESS_RESTRICTION_RE.search(goal)
            if multi_group_match:
                group1, group2, multi_model = multi_group_match.groups()
                target_model = claim.model or multi_model.strip().rstrip(".")
                verified_groups: list[str] = []
                for group_name in (group1.strip(), group2.strip()):
                    # A goal typically names a group by its snake_case technical XML-ID
                    # (e.g. "group_field_technician"), never the translated display name
                    # ("Field Technician") `_group_exists`/`_group_model_access` actually
                    # match against -- resolve it first via the real `ir_model_data` registry
                    # (resolve_group_xmlid_to_display_name(), the same real ground truth,
                    # never a guess) so the lookup below checks the group this token really
                    # refers to, not a literal (and never-matching) string comparison.
                    resolved_display_name = await asyncio.to_thread(
                        resolve_group_xmlid_to_display_name, db, group_name,
                    )
                    lookup_name = resolved_display_name or group_name
                    group_exists, resolved_group_name = await _group_exists(lookup_name)
                    if not group_exists:
                        return False, (
                            f"claimed group {group_name!r} (parsed directly from the goal's own "
                            f"restriction sentence, since the extracted claim named none) does "
                            f"not exist in the real registry"
                        )
                    if target_model:
                        access_result = await _group_model_access(
                            resolved_group_name, target_model, True, None, None, None,
                        )
                        if access_result is False:
                            return False, (
                                f"goal-named group {resolved_group_name!r} does not have the "
                                f"expected access to {target_model!r}"
                            )
                    verified_groups.append(resolved_group_name)
                return True, (
                    f"Verified directly against the goal's own real restriction sentence "
                    f"(the extracted claim named no single group): {verified_groups!r} all "
                    f"exist and have real access to {target_model!r}."
                )

        if not claim.group_name:
            return False, "security claim incomplete (no group_name to verify)"

        group_exists, resolved_group_name = await _group_exists(claim.group_name)
        if group_exists is None:
            return False, f"could not verify whether group {claim.group_name!r} exists (genuine uncertainty)"
        if not group_exists:
            return False, f"claimed new group {claim.group_name!r} does not exist in the real registry"

        has_perm_claim = any(
            v is not None for v in
            (claim.expects_read, claim.expects_write, claim.expects_create, claim.expects_unlink)
        )
        if not (has_perm_claim and claim.model):
            return True, f"group {resolved_group_name!r} exists (no specific model-level permission claim to check)"

        result = await _group_model_access(
            resolved_group_name, claim.model,
            claim.expects_read, claim.expects_write, claim.expects_create, claim.expects_unlink,
        )
        if result is None:
            return False, (
                f"could not verify {claim.group_name!r}'s access to {claim.model!r} "
                f"(genuine uncertainty -- treated as not verified)"
            )
        return result

    async def _extract_menu_structure_claim(self, contract: TaskContract) -> MenuStructureClaim:
        """Real, confirmed gap found live (2026-07-28, Phase 28C,
        `school_student` task) -- the menu-verification sibling of
        `_extract_security_access_claim` above, same discipline:
        extracts what the goal itself CLAIMS about a menu structure,
        never reads the generated code (a wrong/missing menu must never
        trivially "pass" by having the extraction just describe
        whatever Build happened to write).
        """
        prompt = (
            f"Task goal: {contract.goal}\nDeliverables: {contract.deliverables}\n\n"
            "Determine whether this goal makes a checkable claim about a SPECIFIC, NAMED menu "
            "structure being created (e.g. a line like 'Menu structure: School -> Students', "
            "'Add a menu item under Sales', 'a new top-level menu called X with a sub-menu Y'). "
            "If so, set applicable=true and menu_names to the real menu labels in top-to-bottom "
            "hierarchy order, exactly as stated in the goal (e.g. 'School -> Students' -> "
            "[\"School\", \"Students\"]; a single menu with no explicit parent -> just that one "
            "name). If the goal also names or clearly implies which model the LEAF menu's own "
            "list/form view should open (e.g. the module's own new model), set action_model to "
            "that model's real technical name (e.g. 'school.student') if you can confidently "
            "resolve it from the goal text alone, otherwise leave it null.\n\n"
            "IMPORTANT: a goal that merely asks for 'a menu item' / 'a menu' to exist so a model "
            "is 'reachable from the main menu' -- WITHOUT ever literally stating what that menu "
            "should be titled/labeled/called -- makes NO checkable NAME claim, even though it "
            "does require a menu to exist. In that case set applicable=false and leave "
            "menu_names empty: inventing a plausible-sounding name that the goal never actually "
            "stated would not be verifying a real claim, just guessing one, and a real menu "
            "under ANY reasonable title still genuinely satisfies a goal phrased this way. Only "
            "set applicable=true when a SPECIFIC title/label is explicitly written in the goal "
            "text itself."
        )
        return await call_structured(
            client=self.client, model=self.fast_extraction_model, prompt=prompt, schema=MenuStructureClaim,
            task_id=str(contract.task_id), actor="testing_qa", call_label="Extracting the menu structure claim",
            node_id=contract.current_constraint_label,
            use_grammar=True,
        )

    async def _verify_menu_structure_claim(
        self, claim: MenuStructureClaim, db: str, task_id: str | None = None,
    ) -> tuple[bool, str]:
        """The real, deterministic ORM-level verification behind
        `_extract_menu_structure_claim` -- never trusts the extraction
        alone, always checks the live registry via `check_menu_exists()`.
        Same conservative posture as `_verify_security_access_claim`:
        any genuine uncertainty is treated as NOT passed.

        Verifies the claimed hierarchy as a real CHAIN, not just
        isolated names anywhere in the menu tree -- each name after the
        first must have the PRECEDING claimed name as its own real,
        immediate parent. `action_model`, when claimed, is only checked
        against the LEAF (last) menu's own real window action -- an
        intermediate parent menu legitimately has no action of its own
        in Odoo's own standard convention.
        """
        if not claim.menu_names:
            return False, "menu-structure claim incomplete (no menu_names to verify)"

        parent_name: str | None = None
        for i, menu_name in enumerate(claim.menu_names):
            is_leaf = i == len(claim.menu_names) - 1
            result = await asyncio.to_thread(
                check_menu_exists, db, menu_name, parent_name, claim.action_model if is_leaf else None,
            )
            if result is None:
                return False, (
                    f"could not verify menu {menu_name!r} (genuine uncertainty -- treated as not verified)"
                )
            passed, notes = result
            if not passed:
                return False, notes
            parent_name = menu_name

        return True, f"menu structure {claim.menu_names!r} genuinely exists in the real registry, matching every claimed detail"

    async def _self_report_coverage(self, contract: TaskContract, module_name: str) -> SelfReportedCoverage:
        skill_text = _SKILL_PATH.read_text()
        # P12 Tier A item 24: gives this real judgment call (not pure extraction) access to the
        # same real standard Code-Review already checks generated work against -- see
        # _load_constitution_text_or_none()'s own docstring for the full reasoning.
        constitution_text = _load_constitution_text_or_none()
        constitution_block = (
            f"The Odoo Development Agent Constitution (the real standard this change is checked "
            f"against):\n{constitution_text}\n\n---\n\n" if constitution_text else ""
        )
        prompt = (
            f"{constitution_block}{skill_text}\n\n---\n\n"
            f"Task goal: {contract.goal}\nDeliverables: {contract.deliverables}\n"
            f"Module: {module_name}\n\n"
            "Give an honest self-report: do you believe this change is fully covered by its own "
            "install/load (i.e. every real code path -- not just field declarations -- actually "
            "gets exercised), or are there specific paths (e.g. a compute method's branches) that "
            "are never called just by installing the module? List any such paths as best you can "
            "describe them, even approximately -- never claim full coverage just to look complete."
        )
        return await call_structured(
            client=self.client, model=self.routine_model, prompt=prompt, schema=SelfReportedCoverage,
            task_id=str(contract.task_id), actor="testing_qa", call_label="Self-reporting coverage",
            node_id=contract.current_constraint_label,
            use_grammar=True,
        )

    async def _escalate_for_debugging(self, contract: TaskContract, target: ReproductionTarget) -> str:
        """Deep-reasoning escalation -- only reached when the routine
        reproduction check already failed. Raw generate() + strip_think_block(),
        never generate_checked(), per this model's own documented
        always-on <think> trace.
        """
        prompt = (
            f"A reproduction check failed: field {target.field_name!r} does not exist on model "
            f"{target.model!r} in Odoo, even though the task claimed to add it.\n"
            f"Task goal: {contract.goal}\nDeliverables: {contract.deliverables}\n\n"
            "Reason concretely about the most likely cause of this failure (a naming mismatch, an "
            "install that silently didn't run, a manifest/data-path error, etc.) -- a short, "
            "specific analysis, not a generic list of possibilities."
        )
        raw = await self.client.generate(
            model=self.escalation_model, messages=[{"role": "user", "content": prompt}], max_tokens=2000,
            task_id=str(contract.task_id), actor="testing_qa", call_label="Escalating for deep-reasoning debugging",
            node_id=contract.current_constraint_label,
        )
        return strip_think_block(raw)
