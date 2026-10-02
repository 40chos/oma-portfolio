"""Phase 15 (§19.4): the reflect-and-retry logic -- the inner loop
wrapped around each individual TaskContract. Deliberately mechanical,
not model-driven: revising a contract's own rules/inputs from the
SPECIFIC evidence already in a VerificationResult (and any Code-Review
finding) is a templated transformation, not a fresh LLM judgment call --
the same "deterministic where it can be" discipline as
check_sensitive_paths() and the coverage spot-check. This also avoids
AgentOrchestra's own published failure mode (re-planning from scratch
every round instead of using the specific evidence in front of it).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import TypeVar

import psycopg2
import psycopg2.extras

from pydantic import BaseModel

from contracts.constraint_graph import derive_predecessor_labels_from_overlap, labels_needing_tier_fallback
from contracts.module_identity import resolve_module_identity
from contracts.schema import (
    CRITICAL_RULE_PREFIX,
    CapabilityClass,
    ConstraintNode,
    ConstraintState,
    FailureCategory,
    FailureRecord,
    ReplanRound,
    SpecialistType,
    TaskContract,
    VerificationResult,
)
from infra.gateway_client import ModelGatewayClient
from infra.structured_output import StructuredOutputError, call_structured, strip_think_block
from infra.settings import load_postgres_settings
from infra.structured_output import generate_checked
from manager.learning import check_repeated_failures, check_repeated_failures_across_modules
from manager.memory import estimate_tokens
from manager.trace import publish_trace_event

T = TypeVar("T", bound=BaseModel)

# Phase 17 (§21.5.4/§21.5.5): percentage-of-context-window, not a fixed
# token count -- confirmed as the more robust approach specifically
# because this project uses multiple models with genuinely different
# real windows in the same pipeline (a fixed absolute number means a
# different real fraction of budget on each one). Real-world default
# values found in research: ~50% as a soft trigger (used only to decide
# what to keep verbatim vs. compress at Continue time), ~85% as an
# independent hard ceiling (used to stop the loop early, proactively,
# per-round -- see estimate_context_pressure()/should_escalate_to_operator()
# below). Never applied to contract.goal or Operator's own clarification
# text, both always carried forward in full regardless of size.
SOFT_TRIGGER_FRACTION = 0.50
HARD_CEILING_FRACTION = 0.85

# Real, known context windows for the models actually in this pipeline.
# Sourced from the project owner directly, not guessed. Unknown models default to a
# conservative floor (32768) so the safety check errs toward stopping
# early rather than silently skipping the check for a model it doesn't
# recognize.
MODEL_CONTEXT_WINDOWS = {
    "qwen3-coder-30b-a3b": 98304,
    "qwen3.6-27b": 65536,
    # Stage 3 port: local Ollama and cloud (via LiteLLM) logical model names.
    "qwen2.5-coder:7b": 32768,
    "qwen2.5:7b-instruct": 32768,
    "qwen2.5:3b-instruct": 32768,
    "openai-coder": 128000,
    "openai-reasoning": 128000,
    "openai-fast-extraction": 128000,
    "claude-coder": 200000,
    "claude-reasoning": 200000,
    "claude-fast-extraction": 200000,
}
UNKNOWN_MODEL_WINDOW_FALLBACK = 32768

# Mirrors each specialist's own real model-resolution env var (Build's
# own OMA_MODEL_BUILD, Code-Review/Manager's own OMA_MODEL_MANAGER,
# Testing/QA's own OMA_MODEL_CLASSIFIER) -- never a second, disconnected
# copy of the model name, so this stays correct if those are ever
# reconfigured.
_SPECIALIST_MODEL_ENV = {
    SpecialistType.bug_fix: ("OMA_MODEL_BUILD", "qwen3-coder-30b-a3b"),
    SpecialistType.code_review: ("OMA_MODEL_MANAGER", "qwen3.6-27b"),
    SpecialistType.testing_qa: ("OMA_MODEL_CLASSIFIER", "qwen3.6-27b"),
}


def model_context_window(specialist_type: SpecialistType) -> int:
    env_name, fallback = _SPECIALIST_MODEL_ENV.get(specialist_type, (None, None))
    model_name = os.environ.get(env_name, fallback) if env_name else fallback
    return MODEL_CONTEXT_WINDOWS.get(model_name, UNKNOWN_MODEL_WINDOW_FALLBACK)


def estimate_context_pressure(contract: TaskContract) -> float:
    """The real fraction of the (likely) next round's specialist's own
    context window that contract.rules alone would consume -- a
    proactive, every-round check (§21.5.5), distinct from and earlier
    than the Continue-time compression check (§21.5.4's own separate use
    of these same fractions). Uses contract.specialist_type (the
    specialist that just ran) as the model to check against -- a
    reasonable proxy for who runs next, since a retry usually stays on
    the same specialist unless select_specialist_for_retry() explicitly
    routes to code_review.
    """
    window = model_context_window(contract.specialist_type)
    rules_tokens = sum(estimate_tokens(rule) for rule in contract.rules)
    return rules_tokens / window if window else 0.0

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")

_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are", "was", "were",
    "this", "that", "with", "required", "missing", "must", "not", "will", "which", "does",
    "correctly", "should", "before", "proceeding", "resolved", "issue", "issues", "cause",
}
_FINDING_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]{3,}")


def _findings_share_keywords(finding_a: str, finding_b: str, min_overlap: int = 2) -> bool | None:
    """Fast path, zero model calls: significant keyword/theme overlap
    between two Code-Review finding summaries. Returns True/False when
    confident, None when genuinely ambiguous (1 shared keyword, or
    either finding has no real content words) -- falls through to a
    cheap LLM classification call only in that ambiguous case, the same
    cascade shape as manager.classify.classify_capability_class().
    """
    words_a = {w for w in _FINDING_WORD_RE.findall(finding_a.lower()) if w not in _STOPWORDS}
    words_b = {w for w in _FINDING_WORD_RE.findall(finding_b.lower()) if w not in _STOPWORDS}
    if not words_a or not words_b:
        return None
    overlap = words_a & words_b
    if len(overlap) >= min_overlap:
        return True
    if not overlap:
        return False
    return None


_SAME_THEME_PROMPT_TEMPLATE = """\
Do these two Code-Review findings describe the SAME underlying problem, \
even if the specific wording or the exact list of symptoms differs \
between them? Answer based on whether fixing finding A's root cause \
would also address finding B, not on surface wording similarity.

Finding A: {finding_a}

Finding B: {finding_b}

Respond with ONLY a JSON object of the exact shape: \
{{"same_theme": true|false}}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


async def classify_findings_same_theme(
    finding_a: str, finding_b: str, client: ModelGatewayClient, model: str,
    task_id: str | None = None,
) -> bool:
    """A real, structural fix (found during a real Phase 15
    verification pass): comparing two real Code-Review finding
    summaries by exact string equality never fires in practice --
    real Code-Review output rephrases and re-enumerates its specific
    complaints each round even when describing a persistent underlying
    issue. This is the same two-stage cascade already used elsewhere in
    this codebase (manager.classify.classify_capability_class()): a
    cheap fast-path check first, escalating to a cheap, temperature-zero
    LLM call only when genuinely ambiguous -- never an LLM call on
    every comparison, and never a hand-rolled heuristic alone.
    """
    fast_result = _findings_share_keywords(finding_a, finding_b)
    if fast_result is not None:
        return fast_result

    prompt = _SAME_THEME_PROMPT_TEMPLATE.format(finding_a=finding_a, finding_b=finding_b)

    # P13 item 12/22 (2026-08-02, the project owner's own explicit go-ahead): real cloud-tier escalation for
    # this named Judge/retry-strategist call site. `fast_result is None` (the deterministic
    # keyword-overlap path found nothing) is this call's own real, non-invented local-model-
    # uncertain signal -- the same shape classify_capability_class's own fast-path already uses.
    # Falls straight through to the existing local LLM path on any cloud failure/malformed
    # response -- never blocks, never raises.
    from infra.cloud_escalation import maybe_escalate_to_cloud

    cloud_raw = await maybe_escalate_to_cloud(
        "select_specialist_for_retry", local_model_uncertain=True, task_id=task_id,
        prompt=prompt, max_tokens=500,
    )
    if cloud_raw is not None:
        match = _JSON_RE.search(cloud_raw)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return bool(parsed.get("same_theme", False))
            except json.JSONDecodeError:
                pass  # fall through to the local model below

    try:
        cleaned = await generate_checked(
            client=client, model=model, messages=[{"role": "user", "content": prompt}],
            no_think=True, temperature=0.0,
            task_id=task_id, actor="manager", call_label="Comparing whether two findings share a theme",
        )
    except Exception:
        return False  # conservative default -- don't switch specialist on an uncertain signal

    match = _JSON_RE.search(cleaned)
    if not match:
        return False
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return False
    return bool(parsed.get("same_theme", False))


class PauseForOperator(Exception):
    """Raised when should_escalate_to_operator() returns true -- worded
    distinctly from GatewayOutagePause/TaskCutOffPause/a tier-3/4
    sign-off pause, per the established pattern: this one specifically
    means "I've tried what I can think of across N rounds, this needs
    a human," not an infrastructure problem or a pre-approval gate.
    Carries the full round trace so Operator can see exactly what was
    already tried before being asked, per §19.8's own UI requirement.
    """

    def __init__(self, task_id: str, message: str, prior_rounds: list, reason: str = "round_budget"):
        super().__init__(message)
        self.task_id = task_id
        self.message = message
        self.prior_rounds = prior_rounds
        # Phase 17 (§21.5.5): the real, honest reason this escalation
        # happened -- "round_budget" / "wall_clock" / "context_pressure"
        # / "repeated_failure" -- threaded through to
        # summarize_escalation_for_operator() so the human-facing summary
        # names the real reason, not a single generic message regardless
        # of why.
        self.reason = reason


_CONSTRAINT_SPLIT_RE = re.compile(r"\s*(?:;|,?\s+and\s+)\s*", re.IGNORECASE)
# Real, general fix (2026-07-24, same investigation as _METADATA_LINE_RE
# below): the period-based split (`\.\s+`, removed from this pattern)
# is a much weaker signal of genuine enumeration than `;`/` and ` --
# ordinary prose routinely uses two sentences to describe ONE
# requirement (a main description sentence plus a clarifying/scoping
# one, e.g. "...where the salesperson can type a note. Only show it
# on the form, not the list." -- both about the SAME single field).
# Confirmed live: even after stripping the metadata block below, task
# 001's own two-sentence prose still produced 2 bogus constraint
# labels purely from the period split, for a task that is genuinely
# one single field-add. `;` and explicit " and "/" , and " conjunctions
# remain strong, low-false-positive enumeration signals and are kept.
_MAX_CONSTRAINT_LABEL_WORDS = 3
_MAX_DERIVED_CONSTRAINTS = 8
# Real, severe bug found live (2026-07-24, 50-task sequential re-run,
# task 001): every real task spec in this project (matching the
# original site_50_tasks.md convention this benchmark itself was
# written in) follows the SAME shape -- a prose request, then a
# structured "Key: Value" technical-spec block (Module:, Model:,
# Field:, View:, Security:, Template:, ...). The naive sentence-split
# above treats EACH of these metadata lines as its own independent
# "constraint" (confirmed live: a single, genuinely simple, one-field
# task -- "add a Special instructions text field to crm.lead" -- got
# split into 3 meaningless labels, 'i_want_see'/'only_show_it'/
# 'module_mis_base', the last one derived straight from the literal
# "Module: mis_base_extend" line). This directly caused a real,
# repeated downstream failure: with the goal wrongly decomposed,
# `_validate_goal_named_field_is_declared()` (a round-1 field-omission
# guard, itself added earlier this same investigation) correctly
# stayed silent for what LOOKED like a decomposed round, so a genuinely
# broken round (the requested field silently never declared at all)
# sailed through every check and only surfaced as a real install
# crash. Since nearly every task spec in this whole 50-task benchmark
# shares this exact prose-plus-metadata shape, this was very likely
# mis-decomposing a large fraction of the entire run, not just this
# one task.
_METADATA_LINE_RE = re.compile(
    r"^\s*(?:Module|Model|Field|Fields|View|Views|Security|Template|Subject|Body|"
    r"Language|Send|Method|Trigger|File|Depends|Rule|Difficulty)\s*:\s*.*$",
    re.IGNORECASE | re.MULTILINE,
)


def derive_constraint_labels(goal: str) -> dict[str, ConstraintState]:
    """Phase 18 (§22.5): the Manager calls this exactly once, when a
    contract is first built (manager/loop.py's Phase 4), to seed
    TaskContract.constraint_status with short, snake_case labels for
    each independent requirement the goal text appears to name -- e.g.
    "add scheduling fields, enforce a double-booking rule, and add a
    product-scoping relation" splits into three segments, each reduced
    to its own short label, all starting "pending".

    Deliberately a plain, dependency-free heuristic -- NOT
    decompose_into_constraints() (§22.9's own LLM-backed classification
    step, a separate, later component, explicitly out of scope for this
    piece of work). This only needs to produce a reasonable, non-empty
    starting set of labels for constraint_status to track against;
    refining which independent requirements a goal actually contains is
    §22.9's own job, reusing these same dict keys once it exists. A goal
    with no clear multi-part structure collapses to a single label,
    matching the dossier's own finding that 1-2-constraint tasks are
    already handled fine by today's single-contract shape.

    Real, general fix (2026-07-24, see _METADATA_LINE_RE's own comment
    above): a structured "Key: Value" technical-spec line (Module:,
    Model:, Field:, View:, ...) is supporting detail for the ONE real
    request above it, never an independent requirement of its own --
    stripped out before segmentation so it can never mint its own
    bogus constraint label. Only the free-prose portion of the goal
    (before any such metadata block starts) is ever split into
    multiple constraints; once a metadata line is seen, everything
    from that point on is treated as one trailing block, never
    segmented further, since a real task's own technical spec is
    describing ONE deliverable in more detail, not additional
    independent deliverables.
    """
    metadata_start = None
    for match in _METADATA_LINE_RE.finditer(goal):
        metadata_start = match.start()
        break
    prose = goal[:metadata_start] if metadata_start is not None else goal

    segments = [s.strip() for s in _CONSTRAINT_SPLIT_RE.split(prose) if s.strip()]
    if not segments:
        segments = [goal.strip()] if goal.strip() else []

    labels: dict[str, ConstraintState] = {}
    for segment in segments[:_MAX_DERIVED_CONSTRAINTS]:
        words = [w for w in _WORD_RE.findall(segment.lower()) if w not in _STOPWORDS]
        words = words[:_MAX_CONSTRAINT_LABEL_WORDS]
        if not words:
            continue
        label = "_".join(words)
        # Never silently collide two distinct segments into one label --
        # append a numeric suffix rather than dropping the second one.
        unique_label, suffix = label, 2
        while unique_label in labels:
            unique_label = f"{label}_{suffix}"
            suffix += 1
        labels[unique_label] = "pending"

    if not labels:
        labels["primary_requirement"] = "pending"
    return labels


class _ConstraintList(BaseModel):
    constraints: list[str]


# Real, severe bug found live (2026-07-13): a cap of 8 silently
# dropped 2 of Operator's real, explicitly-stated requirements (multi-
# employee independent assignment, and the entire separate service-
# visits feature) from a goal that genuinely contained 10+ independent
# requirements -- confirmed live: decompose_into_constraints() returned
# exactly 8 labels covering only the model/field-level requirements,
# and the pipeline never attempted the other two at all, not even a
# failed attempt, because they were never in constraint_status to begin
# with. The prompt below explicitly instructs the LLM to cap itself at
# this number too, not just a defensive slice -- so a goal with more
# real independent requirements than the cap gets requirements
# silently discarded before round 1 ever starts, with no error, log,
# or escalation of any kind. Raised well above any goal seen in
# practice so far; still a real ceiling (not removed entirely) as a
# backstop against pathological over-decomposition of a single goal.
_MAX_DECOMPOSED_CONSTRAINTS = 20


_GOAL_FIELD_METADATA_LINE_RE = re.compile(r"^\s*Field(?:\s*name)?\s*:\s*.*$", re.IGNORECASE | re.MULTILINE)


async def decompose_into_constraints(
    goal: str, client: ModelGatewayClient, model: str, task_id: str | None = None
) -> list[str]:
    """Phase 18 (§22.9): a cheap, cached-discipline classification step
    -- same cascade style as manager.classify's capability-class call,
    temperature zero -- run ONCE, at contract-creation time, before
    round 1 ever starts. Returns a short list of independent constraint
    labels (matching constraint_status's own keys, and reusing the same
    short/snake_case shape derive_constraint_labels() (§22.5) already
    produces heuristically -- this is the real, LLM-backed refinement of
    that heuristic starting point).

    1-2 items means today's single-contract shape is already correct
    (dossier §1.5's own finding: 1-2-constraint tasks already pass
    reliably); manager/loop.py only activates single-constraint
    decomposition (_run_decomposed_task()) at 3+.
    """
    prompt = (
        "Read this task goal and list the INDEPENDENT requirements it actually contains -- "
        "each one a short, distinct thing that could in principle be verified separately from "
        "the others by checking that one specific field, model, or relation exists and works "
        "(e.g. a goal to 'add scheduling fields, enforce a double-booking rule, and add a "
        "product-scoping relation' contains three independent requirements). Use short, "
        "snake_case labels, 2-3 words each (e.g. 'scheduling_fields', 'double_booking_rule'). "
        "A goal describing only ONE real requirement must return a list with exactly one item -- "
        "never invent extra items just to pad the list.\n\n"
        "Two real mistakes to avoid, both confirmed live on a real task:\n"
        "1. Do NOT split a single new record's own set of display/summary fields into multiple "
        "constraints just because the goal lists them as several bullet points -- e.g. 'a visit "
        "should display the customer, project, date, and status' is ONE requirement (one new "
        "model carrying those fields), not four. Group every field that belongs to the SAME new "
        "model or relation into a single constraint, one constraint per genuinely distinct model/"
        "relation/rule, never one constraint per field of an already-scoped model.\n"
        "2. Do NOT invent constraints that restate general build instructions or non-functional "
        "guidance rather than naming one concrete, checkable field/model/relation -- e.g. 'keep "
        "custom code minimal', 'build the module', or 'follow best practices' are guidance, not "
        "independent requirements, and must never appear as their own label. Every label must "
        "name something a later step could look up and confirm exists on a specific model.\n\n"
        f"Return at most {_MAX_DECOMPOSED_CONSTRAINTS}.\n\nTask goal: {goal}"
    )
    result = await call_structured(
        client=client, model=model, prompt=prompt, schema=_ConstraintList,
        no_think=True, task_id=task_id or "decompose_into_constraints", actor="manager",
        call_label="Decomposing task goal into independent constraints", use_grammar=True,
    )
    labels = [label.strip() for label in result.constraints if label.strip()][:_MAX_DECOMPOSED_CONSTRAINTS]
    labels = labels or ["primary_requirement"]

    # Real, general fix (2026-07-25, task 001's 10th fresh submission):
    # this LLM-driven decomposition over-split a genuinely single-field
    # task into 3+ fake constraints (e.g. "add the field" / "update the
    # view" / ...) DESPITE its own prompt above explicitly warning
    # against exactly this pattern -- confirmed live via Gitea commit
    # history (multiple sequential sub-contract commits, each touching
    # only one file, for a task whose real spec is one field on one
    # model). This is the SAME root-cause SHAPE as `_METADATA_LINE_RE`'s
    # fix to `derive_constraint_labels()` above (a structured "Key:
    # Value" technical-spec block over-read as independent
    # requirements) -- but on this SEPARATE, LLM-judgment-driven path,
    # which that earlier fix never touched. The consequence was worse
    # here than a mislabeled ledger entry: manager/loop.py routes any
    # 3+-label result through `_run_decomposed_task()`, whose own
    # per-sub-contract `constraint_status` always carries EVERY label
    # at once (never just one) -- which made specialists/build/
    # specialist.py's `_validate_goal_named_field_is_declared()` guard
    # (`len(constraint_status) >= 2: return`, itself a deliberate,
    # correctly-reasoned guard for genuinely multi-constraint rounds)
    # permanently skip validation for every sub-contract of every
    # over-split task, silently letting the one real field go
    # undeclared for 4+ rounds until it finally surfaced as a real
    # sandbox install crash. Never fixable from inside that validator
    # (it has no way to tell "genuinely multi-constraint" from
    # "wrongly over-split" after the fact) -- fixed here, at the
    # source: a goal whose own metadata names at most one real field
    # (this project's own convention, same signal `_METADATA_LINE_RE`
    # already parses) cannot legitimately decompose into 3+ independent,
    # separately-verifiable requirements, so an LLM result that size is
    # itself the signal of over-splitting, not a real decomposition --
    # collapsed back to the same deterministic single-label result
    # `derive_constraint_labels()` would produce, never trusting the
    # over-split LLM output for this specific, checkable shape.
    if len(labels) >= 3:
        # Exactly 1, not <= 1: a goal with ZERO `Field:` metadata lines
        # (ordinary free-form prose, e.g. Operator's own multi-requirement
        # service-management goal, tested above) carries no reliable
        # signal either way -- its real decomposition must never be
        # touched by this check, only a goal that explicitly, structurally
        # declares exactly one field via this project's own convention.
        field_line_count = len(_GOAL_FIELD_METADATA_LINE_RE.findall(goal))
        if field_line_count == 1:
            return list(derive_constraint_labels(goal).keys())
    return labels


class _ConstraintArtifacts(BaseModel):
    label: str
    # P13 item 4/10(a): real Odoo identifiers this constraint concretely BUILDS/NEEDS. Every
    # field defaults to empty -- never guessed -- same discipline as contracts/goal_facts.py's
    # already-shipped extraction functions.
    creates: list[str] = []
    requires: list[str] = []
    # Phase 31 UI (2026-08-08, real gap found live: the project owner's own report -- "the graph shows only
    # the flat, top-level pieces... no visual sense of... this piece was itself split further").
    # recursively_decompose_constraints() below flattens the whole recursion tree into one list
    # for scheduling (a real, deliberate design choice -- cross-level dependency edges "just
    # work" via creates/requires overlap on the flat list, and the parent piece never itself
    # needs to run once it's been split) -- but that flattening genuinely discards the parent-of
    # relationship entirely, with no record anywhere of which split produced which children.
    # split_from is PURELY a display lineage marker for the UI to draw actual recursive
    # splitting (a piece visibly branching into its own sub-pieces) instead of rendering every
    # node at every depth as an identical, same-size sibling -- never read by
    # build_constraint_nodes()'s own predecessor_labels derivation, never touches scheduling.
    split_from: str | None = None


class _ConstraintDecomposition(BaseModel):
    constraints: list[_ConstraintArtifacts] = []


async def decompose_into_constraints_with_artifacts(
    goal: str, client: ModelGatewayClient, model: str, task_id: str | None = None,
) -> list[_ConstraintArtifacts]:
    """P13 item 10(a) (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
    2026-07-29.md §22.2): a real, structured extraction of `creates`/`requires` per constraint,
    for deterministic (zero-LLM) predecessor-label derivation (contracts/constraint_graph.py's
    `derive_predecessor_labels_from_overlap()`).

    Deliberately a SEPARATE function from `decompose_into_constraints()` above, not a refactor of
    it, even though item 10(a)'s own source text calls for "the SAME call" -- two existing,
    well-reasoned regression tests (tests/test_decompose_constraint_cap.py,
    tests/test_replanning_constraint_label_dependency_sort.py) mock `call_structured` against
    `decompose_into_constraints()`'s exact existing `_ConstraintList` schema; refactoring that
    function internally to route through this richer schema would silently break both. This
    function is called INSTEAD OF (not in addition to) `decompose_into_constraints()` at its one
    real production call site (`manager/loop.py`), so the net LLM-call count at that site is
    unchanged -- zero marginal cost in practice, even though the two functions' prompts are
    currently duplicated rather than unified. `decompose_into_constraints()` itself is untouched
    and stays fully correct for any caller that only needs plain labels.

    Real, documented caveat that MUST travel with this function (per item 4/10a's own text): the
    local model's reliability at producing accurate `requires` -- especially IMPLICIT dependencies
    the goal text doesn't explicitly name -- is UNVERIFIED. This extraction strictly improves
    ordering correctness even when `requires` comes back empty (the caller falls back to the
    existing tier-bucket order for exactly those labels, never worse than before) -- it must NOT
    be treated as a proven concurrency-safety certificate until spot-checked against real task
    history (item 10(a)'s own explicit scope boundary: this function ships dependency-detection
    improvements now; concurrent GENERATION/COMMIT built on top of it are separate, not shipped by
    this function alone).
    """
    prompt = _build_decomposition_prompt(goal)
    return await _run_decomposition_with_prompt(
        prompt, goal, client, model, task_id=task_id,
        call_label="Decomposing task goal into independent constraints (with artifacts)",
        allow_cloud_escalation=True,
    )


def _build_decomposition_prompt(goal_text: str) -> str:
    """Phase 31 §1: extracted to module level (was a closure inside
    decompose_into_constraints_with_artifacts()) so decompose_into_constraints_with_artifacts_
    biased() can reuse the exact same base prompt, varied only by an appended bias suffix --
    never a re-derived or paraphrased copy.
    """
    return (
            "Read this task goal and list the INDEPENDENT requirements it actually contains -- "
            "each one a short, distinct thing that could in principle be verified separately from "
            "the others by checking that one specific field, model, or relation exists and works "
            "(e.g. a goal to 'add scheduling fields, enforce a double-booking rule, and add a "
            "product-scoping relation' contains three independent requirements). Use short, "
            "snake_case labels, 2-3 words each (e.g. 'scheduling_fields', 'double_booking_rule'). "
            "A goal describing only ONE real requirement must return a list with exactly one item -- "
            "never invent extra items just to pad the list.\n\n"
            "Two real mistakes to avoid, both confirmed live on a real task:\n"
            "1. Do NOT split a single new record's own set of display/summary fields into multiple "
            "constraints just because the goal lists them as several bullet points -- e.g. 'a visit "
            "should display the customer, project, date, and status' is ONE requirement (one new "
            "model carrying those fields), not four. Group every field that belongs to the SAME new "
            "model or relation into a single constraint, one constraint per genuinely distinct model/"
            "relation/rule, never one constraint per field of an already-scoped model.\n"
            "2. Do NOT invent constraints that restate general build instructions or non-functional "
            "guidance rather than naming one concrete, checkable field/model/relation -- e.g. 'keep "
            "custom code minimal', 'build the module', or 'follow best practices' are guidance, not "
            "independent requirements, and must never appear as their own label. Every label must "
            "name something a later step could look up and confirm exists on a specific model.\n\n"
            "For each constraint, ALSO extract (only what the goal text concretely states -- never "
            "invent or infer a value that isn't clearly present):\n"
            "- creates: real Odoo identifiers (model names, field names, view/action/menu names, "
            "security group names) this constraint concretely BUILDS. Empty list if none clearly "
            "named.\n"
            "- requires: real Odoo identifiers this constraint's own implementation needs to ALREADY "
            "exist (e.g. a computed field naming the model/field it reads). Empty list if the goal "
            "doesn't clearly state a dependency.\n\n"
            f"Return at most {_MAX_DECOMPOSED_CONSTRAINTS}.\n\nTask goal: {goal_text}"
        )


async def _run_decomposition_with_prompt(
    prompt: str,
    goal: str,
    client: ModelGatewayClient,
    model: str,
    *,
    task_id: str | None,
    call_label: str,
    allow_cloud_escalation: bool,
    no_think: bool = True,
    use_grammar: bool = True,
) -> list[_ConstraintArtifacts]:
    """Phase 31 §1: extracted from decompose_into_constraints_with_artifacts()'s own tail so the
    baseline call and Stage A's biased drafts share one real call/parse path -- never two
    independently-maintained copies of the same result-shape handling.

    `allow_cloud_escalation` is False for every Stage A biased draft (see
    decompose_into_constraints_with_artifacts_biased()'s own docstring for why) and True only for
    the one pre-existing baseline call site, so the net cloud-eligible call count at this module
    is unchanged by the architect-stage upgrade.

    `no_think` (2026-08-08, real bug found and root-caused live, second layer of the same
    incident the project owner's own recursion-probe-framing finding uncovered): defaults True, preserving
    every existing caller's exact prior behavior -- the ORIGINAL whole-goal decomposition and
    the architect-stage biased drafts are genuinely closer to "extract the structure the goal
    text already states," the "cheap, direct" case call_structured()'s own docstring says
    no_think=True is FOR. The recursion-check probe (maybe_decompose_piece_further(), below) is
    a genuinely different kind of call -- an open judgment question ("does this piece still hide
    more structure?"), not extraction -- and passes no_think=False explicitly. Confirmed live,
    directly: with no_think=True, the local model echoed a deliberately, obviously-splittable
    piece (11 creates spanning three distinct workflow stages) back completely unchanged, even
    with the corrected, unbiased probe prompt already in place -- it wasn't reasoning about the
    question at all, just pattern-matching input to output. This is a genuinely separate root
    cause from the prompt-framing bug (that fix is real and independently confirmed via direct
    prompt inspection), not a duplicate of it -- fixing the framing alone was necessary but not
    sufficient.
    """
    result: _ConstraintDecomposition | None = None
    if allow_cloud_escalation:
        # P13 item 12/22 (2026-08-02, the project owner's own explicit go-ahead): real cloud-tier escalation
        # for this named Planner/Decomposer call site. No auto-detected local-model confidence
        # signal exists for this specific call (unlike classify_capability_class's own
        # fast-path-found-nothing signal) -- decomposing a real, possibly multi-part goal into
        # independent constraints is inherently the judgment-heavy case this call site exists to
        # handle, so local_model_uncertain=True is a real, honest statement about this call's own
        # nature, not a fabricated confidence score (infra.cloud_escalation's own docstring names
        # this exact, still-open gap). decide_cloud_escalation()'s own enabled-flag/cost-ceiling
        # checks are what actually bound real spend here, not a per-call heuristic. Cloud doesn't
        # get the local grammar-constrained decoding call_structured() uses, so the cloud path
        # asks explicitly for the same JSON shape and validates it the same way -- any
        # parse/validation failure falls through to the existing local call, never raises.
        from infra.cloud_escalation import maybe_escalate_to_cloud, truncate_for_cloud_prompt

        cloud_prompt = (
            _build_decomposition_prompt(truncate_for_cloud_prompt(goal))
            + "\n\nRespond with ONLY a JSON object of the exact shape: "
            '{"constraints": [{"label": "...", "creates": ["..."], "requires": ["..."]}, ...]}'
        )
        cloud_raw = await maybe_escalate_to_cloud(
            "decompose_into_constraints", local_model_uncertain=True, task_id=task_id,
            prompt=cloud_prompt, max_tokens=1500,
        )
        if cloud_raw is not None:
            try:
                result = _ConstraintDecomposition.model_validate_json(cloud_raw.strip())
            except Exception:
                result = None

    if result is None:
        result = await call_structured(
            client=client, model=model, prompt=prompt, schema=_ConstraintDecomposition,
            no_think=no_think, task_id=task_id or "decompose_into_constraints", actor="manager",
            call_label=call_label,
            use_grammar=use_grammar,
        )
    items = [c for c in result.constraints if c.label.strip()][:_MAX_DECOMPOSED_CONSTRAINTS]
    return items or [_ConstraintArtifacts(label="primary_requirement")]


_ARCHITECT_STAGE_BIASES = ("parallelism_precision", "dependency_conservatism")

_ARCHITECT_STAGE_BIAS_PROMPT_SUFFIXES = {
    "parallelism_precision": (
        "\n\nBias for THIS pass specifically: favor SPLITTING any constraint that bundles two "
        "genuinely independent, separately-verifiable pieces of work into separate constraints -- "
        "lean toward more, narrower, independently-schedulable constraints, never toward fewer."
    ),
    "dependency_conservatism": (
        "\n\nBias for THIS pass specifically: favor COMBINING constraints unless a real, concrete "
        "data/artifact dependency between them is genuinely inferable from the goal text -- when "
        "in doubt, prefer fewer, larger constraints. Also double-check every requires/creates pair "
        "for accuracy; a missed real dependency here is worse than an unnecessary split."
    ),
}


async def decompose_into_constraints_with_artifacts_biased(
    goal: str, client: ModelGatewayClient, model: str, bias: str, task_id: str | None = None,
) -> list[_ConstraintArtifacts]:
    """Phase 31 §1 Stage A: one structurally-distinct drafter for the architect-stage upgrade --
    same schema/parse path as decompose_into_constraints_with_artifacts() (via the shared
    `_run_decomposition_with_prompt()` helper), varied ONLY by an appended bias instruction in the
    prompt. This is the Google DeepMind Co-Scientist-style structurally-distinct-roles pattern
    (arXiv:2502.18864) -- deliberately NOT unguided homogeneous multi-agent debate (N identical
    prompts + vote), which arXiv:2511.07784 and arXiv:2605.00914 both found does not reliably
    improve results over a single well-prompted call.

    Local-only, no cloud escalation (see `_run_decomposition_with_prompt()`'s own
    `allow_cloud_escalation` docstring) -- bounds the architect stage's added cost to at most 2
    extra local calls + 1 local critic call, never multiplying the one existing cloud-eligible
    site by 3.
    """
    if bias not in _ARCHITECT_STAGE_BIAS_PROMPT_SUFFIXES:
        raise ValueError(f"unknown architect-stage bias: {bias!r}")
    prompt = _build_decomposition_prompt(goal) + _ARCHITECT_STAGE_BIAS_PROMPT_SUFFIXES[bias]
    return await _run_decomposition_with_prompt(
        prompt, goal, client, model, task_id=task_id,
        call_label=f"Decomposing task goal into independent constraints (architect draft, bias={bias})",
        allow_cloud_escalation=False,
    )


def _render_constraint_draft_for_critic(draft_number: int, items: list[_ConstraintArtifacts]) -> str:
    lines = "\n".join(
        f"  - label={item.label!r} creates={item.creates!r} requires={item.requires!r}"
        for item in items
    )
    return f"Draft {draft_number} ({len(items)} constraints):\n{lines}"


async def critique_and_merge_constraint_drafts(
    goal: str,
    drafts: list[list[_ConstraintArtifacts]],
    client: ModelGatewayClient,
    model: str,
    task_id: str | None = None,
) -> list[_ConstraintArtifacts]:
    """Phase 31 §1 Stage B: one distinct critic/merge call -- never one of Stage A's own drafters
    -- that selects or synthesizes the best decomposition among the baseline + biased drafts.
    Instructed to prefer fewer/better-scoped constraints and to coarsen (merge down) any draft
    whose sibling count exceeds ~8, per §7's own decomposition-granularity research (4-8 units
    before coordination overhead dominates, arXiv:2511.01149 + companion). Local-only, same
    reasoning as the biased drafts above.

    Falls back to the first (baseline) draft, never an empty list, if the critic's own output
    parses to nothing usable -- matches every other decomposition call's own
    never-worse-than-today fallback discipline.
    """
    drafts_text = "\n\n".join(
        _render_constraint_draft_for_critic(i + 1, draft) for i, draft in enumerate(drafts)
    )
    prompt = (
        "You are reviewing multiple independently-drafted decompositions of the SAME task goal -- "
        "each draft splits the goal into independent, separately-verifiable constraints. Pick the "
        "single best draft, or synthesize a new merged list drawing from the strongest parts of "
        "each, using this judgment:\n"
        "- Prefer FEWER, better-scoped constraints over more, narrower ones when in doubt.\n"
        "- If any single draft has more than about 8 constraints, coarsen it: merge closely "
        "related constraints down toward roughly 4-8 total.\n"
        "- Keep only creates/requires pairs that are concretely supported by the goal text or "
        "already present in one of the drafts below; never invent a new one.\n\n"
        f"Task goal: {goal}\n\n{drafts_text}\n\n"
        "Return the single best final list of constraints."
    )
    items = await _run_decomposition_with_prompt(
        prompt, goal, client, model, task_id=task_id,
        call_label="Critiquing and merging architect-stage constraint decomposition drafts",
        allow_cloud_escalation=False,
    )
    if items == [_ConstraintArtifacts(label="primary_requirement")] and drafts and drafts[0]:
        # The shared helper's own empty-result fallback is a generic single-item placeholder --
        # for the critic specifically, falling back to the real baseline draft is strictly more
        # informative and matches decompose_into_constraints_with_artifacts()'s own
        # never-worse-than-today discipline.
        items = drafts[0]

    # §1 Stage B, §10 telemetry: `architect_draft_selected` when the critic's own output is
    # IDENTICAL to one of the input drafts (a real, verbatim pick, never a synthesis credited as
    # one); `architect_drafts_merged` otherwise (a genuine new synthesis drawing from more than
    # one draft). Both name the draft index(es) involved, for auditability.
    selected_index = next((i for i, draft in enumerate(drafts) if draft == items), None)
    if selected_index is not None:
        publish_trace_event(task_id or "", {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"architect_draft_selected: the critic picked draft {selected_index + 1}/{len(drafts)} verbatim, no synthesis",
            "status": "running",
        })
    else:
        publish_trace_event(task_id or "", {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"architect_drafts_merged: the critic synthesized a new merged list from {len(drafts)} drafts, not a verbatim pick of any one",
            "status": "running",
        })
    return items


async def maybe_upgrade_decomposition_with_architect_stage(
    goal: str,
    baseline_items: list[_ConstraintArtifacts],
    client: ModelGatewayClient,
    model: str,
    task_id: str | None = None,
) -> list[_ConstraintArtifacts]:
    """Phase 31 §1: the orchestration entry point for the architect-stage upgrade. Callers MUST
    only invoke this once the baseline's own output already clears manager/loop.py's existing
    `len(constraint_labels) >= 3 or unsupported_touches` gate (loop.py:907) -- this function does
    not re-check that gate itself, so a simple (<3 label, no unsupported-touch) goal's own code
    path never even imports/calls this function, keeping its call count byte-for-byte identical
    to before this change.

    Stage A's 2 biased drafts run CONCURRENTLY via asyncio.gather (structurally-distinct roles,
    not sequential) -- their own failures are not caught here; a real exception from either draft
    propagates to the caller exactly like today's single baseline call would, no silent
    downgrade to fewer drafts.
    """
    drafts = await asyncio.gather(*(
        decompose_into_constraints_with_artifacts_biased(goal, client, model, bias, task_id=task_id)
        for bias in _ARCHITECT_STAGE_BIASES
    ))
    all_drafts = [baseline_items, *drafts]
    return await critique_and_merge_constraint_drafts(goal, all_drafts, client, model, task_id=task_id)


# --- Phase 31 §1.6: universal, cost-bounded recursive decomposition -----------------------------

OMA_DECOMPOSITION_MAX_RECURSION_DEPTH = int(os.environ.get("OMA_DECOMPOSITION_MAX_RECURSION_DEPTH", "3"))
OMA_ARCHITECT_STAGE_MAX_TOTAL_CALLS = int(os.environ.get("OMA_ARCHITECT_STAGE_MAX_TOTAL_CALLS", "8"))
OMA_DECOMPOSITION_MAX_TOTAL_NODES = int(os.environ.get("OMA_DECOMPOSITION_MAX_TOTAL_NODES", "40"))


def _artifact_signal_count(piece: _ConstraintArtifacts) -> int:
    """Tier 0 of §1.6: a free, zero-LLM check. A piece with at most one total creates/requires
    entry is treated as already atomic -- not worth a real probe call."""
    return len(piece.creates) + len(piece.requires)


def _build_recursive_split_probe_prompt(base_goal_text: str, piece: _ConstraintArtifacts) -> str:
    """Phase 31 §1.6 recursion-check prompt (2026-08-08, real bug found and root-caused live --
    the project owner's own finding, confirmed against 8 real, varied live production goals that never
    triggered a genuine split, INCLUDING a 10-piece equipment-rental task with obviously complex
    individual pieces): this used to reuse `_narrow_goal_text_for_label()`'s own execution-time
    "Focus ONLY on this one piece of the goal: X" framing, feeding it straight into the SAME
    `_build_decomposition_prompt()` used for the ORIGINAL, whole-goal decomposition -- whose own
    explicit rule says "A goal describing only ONE real requirement must return a list with
    exactly one item -- never invent extra items just to pad the list." Combined, these two
    framings structurally guarantee a "just one piece" answer regardless of how complex the real
    underlying goal is: the "Focus ONLY on X" wording tells the model to treat X as a single,
    already-scoped target (a COMMAND, matching how execution-time goal-narrowing legitimately
    works elsewhere in this codebase, e.g. manager/loop.py's own per-round "This round's own NEW
    focus is ONLY: X"), while the reused decomposition prompt's own anti-padding rule then
    actively discourages returning more than one item for what it's told is already one focused
    thing -- the exact opposite of what a "does this still have more structure worth splitting?"
    probe needs to ask.

    This prompt is DELIBERATELY DIFFERENT, not just a rephrasing: `piece` is presented as CONTEXT
    (a real, already-identified sub-requirement from an earlier pass) for a genuine open
    question, never as a scope-narrowing command -- and the question explicitly allows (and
    explains what would justify) EITHER answer, real single-item atomicity or a real further
    split, rather than implicitly favoring one over the other the way the reused execution
    framing did.

    Worked example added (2026-08-08, calibration follow-up -- the project owner's own instruction, after
    live re-verification confirmed the framing/no_think/use_grammar/call_structured-bypass fixes
    above are all genuinely working and genuinely reasoning, but every real pipeline run since has
    landed on a defensible "no split" verdict): five separate real, live, database-backed pipeline
    attempts all produced legitimate atomic verdicts, each independently checked against the
    prompt's own stated criteria and each correct on its own terms (same-model field bundles,
    already-cleanly-split top-level pieces). Before concluding the mechanism is fine and every real
    goal just happens to be atomic, this adds ONE concrete worked example of a genuine "yes, split
    this" case -- until now the prompt only ever showed the model abstract criteria, never a real
    example of what clearing that bar actually looks like, which plausibly biases a model toward
    the easier, always-available "describe it as-is" answer when the abstract criteria alone don't
    give it a concrete contrast to calibrate against.
    """
    piece_description = f"{piece.label!r}"
    if piece.creates:
        piece_description += f", which so far concretely builds: {piece.creates}"
    if piece.requires:
        piece_description += f", and requires already existing: {piece.requires}"
    return (
        "A task goal was already decomposed into independent, separately-checkable "
        "requirements. One of those pieces is given below as a real, already-identified "
        "sub-requirement -- your job here is to check whether THIS SPECIFIC piece still "
        "bundles two or more genuinely independent, separately-checkable things that were left "
        "combined under one label, not to re-decompose the whole original goal from scratch and "
        "not to redescribe this piece using different words.\n\n"
        f"Full original goal (context only, for understanding what this piece belongs to): "
        f"{base_goal_text}\n\n"
        f"The piece to examine: {piece_description}.\n\n"
        "Two genuinely different, both-valid answers are possible here -- judge honestly, don't "
        "default to either one:\n"
        "- If this piece is ALREADY a single, atomic, one-thing-to-verify unit (even if it "
        "concretely builds several closely-related fields on the SAME model/relation), return a "
        "list with EXACTLY ONE item describing it as-is -- do not invent a split that isn't "
        "really there.\n"
        "- If this piece genuinely still bundles two or more separately-checkable things -- e.g. "
        "it spans multiple DIFFERENT models, combines a data/field change with an unrelated "
        "workflow/permission/notification concern, or names multiple distinct email/access/UI "
        "concerns that could each be verified independently -- split it into that many real, "
        "distinct sub-requirements instead, the same way the original goal itself was split. Use "
        "the same short, snake_case labeling convention (2-3 words each).\n\n"
        "Worked example of a genuine split (for calibration only -- this is not the piece you're "
        "examining): a piece labeled 'approval_and_notifications', concretely building "
        "['project.project.approval_state', 'manager_approve_button', 'manager_notification_email', "
        "'approval_chatter_log'], genuinely bundles THREE separately-checkable things: (1) the "
        "state field and its approve button on project.project -- one workflow-control concern, "
        "(2) the email notification -- a completely different delivery/notification concern that "
        "could be verified (or fail) independently of whether the button itself works, and (3) the "
        "chatter log entry -- a separate audit-trail concern. The correct answer there is 3 items: "
        "'approval_workflow_control' (creates: ['project.project.approval_state', "
        "'manager_approve_button']), 'approval_email_notification' (creates: "
        "['manager_notification_email']), and 'approval_chatter_log' (creates: "
        "['approval_chatter_log']) -- NOT one item just because they all relate to 'approval', and "
        "NOT split purely because there happen to be several creates: the split is justified "
        "because email delivery and audit logging are genuinely different concerns from the "
        "workflow state itself, each checkable on its own without the others. Contrast this with a "
        "piece that only builds several fields on ONE model with no separate notification/"
        "permission/audit concern mixed in -- that piece stays a single item, per the rule above.\n\n"
        "For each returned item, extract creates/requires exactly as before -- real Odoo "
        "identifiers this item concretely builds/needs, never invented or inferred beyond what's "
        "concretely present."
    )


async def _run_recursion_probe_allowing_reasoning(
    prompt: str, client: ModelGatewayClient, model: str, task_id: str | None,
    retry_sub_budget: int = 3,
) -> list[_ConstraintArtifacts]:
    """Phase 31 §1.6 recursion-check call (2026-08-08, THIRD and final layer of the same real
    bug -- see _build_recursive_split_probe_prompt()'s and _run_decomposition_with_prompt()'s
    own docstrings for layers one and two). Confirmed live, directly: even with the corrected,
    unbiased prompt AND no_think=False, `call_structured()`'s own HARDCODED system prompt
    ("Respond with ONLY a single JSON object matching this schema. No prose, no markdown fence,
    no explanation.") still suppressed genuine deliberation -- the model kept echoing an
    obviously-splittable piece (11 creates spanning three distinct workflow stages) back
    unchanged. Calling `client.generate()` directly, with NO system message forbidding prose,
    produced a full, genuine multi-paragraph reasoning trace that correctly concluded the piece
    should split into three real, distinct sub-requirements (approval_state_buttons,
    stage_email_notifications, transition_chatter_log) -- confirmed live.

    This is a genuinely different call shape from call_structured(), not a copy: no grammar
    constraint, no forbidding system prompt, and JSON is requested as an explicit INSTRUCTION
    inside the user prompt itself ("finish with exactly one JSON object... nothing else after
    it") rather than a structural constraint -- deliberately, since the constraint itself was
    the third confirmed suppressor. Parses the LAST balanced top-level `{...}` object in the
    response (after stripping any `<think>...</think>` block) rather than call_structured()'s
    own simpler first-brace-to-last-brace heuristic (infra.structured_output._extract_json()),
    since a genuine multi-paragraph reasoning trace routinely mentions bracketed example values
    (e.g. "creates: [...]") well before the real final answer -- a naive greedy match would
    swallow from the first such mention all the way to the true final closing brace and fail to
    parse. Retries (feeding the validation error back) up to `retry_sub_budget` times, matching
    call_structured()'s own retry discipline, never silently falling back to "no split" on a
    parse failure -- that would just reintroduce the original bug under a different name.
    """
    schema_json = json.dumps(_ConstraintDecomposition.model_json_schema(), indent=2)
    user_content = (
        f"{prompt}\n\nThink it through fully first. When you're done reasoning, finish your "
        f"response with EXACTLY ONE JSON object matching this schema, on its own, as the very "
        f"last thing in your response -- nothing after it:\n\n{schema_json}"
    )
    messages = [{"role": "user", "content": user_content}]
    last_error: Exception | None = None
    for attempt in range(1, retry_sub_budget + 1):
        raw = await client.generate(
            model=model, messages=messages, temperature=0.0, no_think=False,
            task_id=task_id, actor="manager",
            call_label="Checking whether a piece still bundles further structure (reasoning-preserving)",
        )
        parsed = _extract_last_balanced_json_object(raw, _ConstraintDecomposition)
        if parsed is not None:
            items = [c for c in parsed.constraints if c.label.strip()][:_MAX_DECOMPOSED_CONSTRAINTS]
            return items or [_ConstraintArtifacts(label="primary_requirement")]
        last_error = ValueError("no valid JSON object matching the schema found in the response")
        if attempt < retry_sub_budget:
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (
                    "That response didn't end with a valid JSON object matching the schema. "
                    "Finish your NEXT response with exactly one JSON object matching the schema "
                    "above, as the very last thing, nothing after it."
                ),
            })
    raise StructuredOutputError(
        f"Model {model!r} failed to produce a valid _ConstraintDecomposition after "
        f"{retry_sub_budget} reasoning-preserving attempts: {last_error}"
    ) from last_error


def _extract_last_balanced_json_object(text: str, schema: type[T]) -> T | None:
    """Scans for every top-level, brace-balanced `{...}` block in `text` (after stripping any
    `<think>...</think>` block) and returns the FIRST one (scanning from the end) that both
    parses as JSON and validates against `schema` -- correct against nested nested braces and
    unaffected by earlier, non-JSON bracket mentions in a genuine reasoning trace, unlike a
    naive greedy regex. Returns None (never guesses) if nothing in the text validates.
    """
    text = strip_think_block(text)
    candidates: list[str] = []
    depth = 0
    start: int | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start:i + 1])
                    start = None
    for candidate in reversed(candidates):
        try:
            parsed_json = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        try:
            return schema.model_validate(parsed_json)
        except Exception:
            continue
    return None


class _RecursionBudget:
    """Phase 31 §1.6: the three real caps bounding recursive decomposition, shared by reference
    across one top-level recursively_decompose_constraints() call so every branch draws from the
    SAME budget -- a single deeply-recursing piece cannot alone exceed max_nodes/max_calls just
    because the between-siblings check never independently fired for it (round 11's own fix: the
    node-count check must run BEFORE each Tier 1 call's own initiation, not just between
    top-level siblings).
    """

    def __init__(self, max_depth: int, max_calls: int, max_nodes: int, starting_node_count: int):
        self.max_depth = max_depth
        self.max_calls = max_calls
        self.max_nodes = max_nodes
        self.calls_made = 0
        self.node_count = starting_node_count

    def calls_remaining(self) -> bool:
        return self.calls_made < self.max_calls

    def nodes_remaining(self) -> bool:
        return self.node_count < self.max_nodes


async def maybe_decompose_piece_further(
    piece: _ConstraintArtifacts,
    base_goal_text: str,
    client: ModelGatewayClient,
    model: str,
    budget: _RecursionBudget,
    depth: int = 0,
    task_id: str | None = None,
    lineage: dict[str, str] | None = None,
) -> list[_ConstraintArtifacts]:
    """Phase 31 §1.6: recursively probes whether `piece` should be split further. Tier 0 (free)
    short-circuits obviously-atomic pieces at zero cost; Tier 1 spends exactly one real call --
    to the SAME unmodified decompose_into_constraints_with_artifacts() at the piece's own
    narrowed goal text -- only when Tier 0 is inconclusive. A genuine split (>=2 returned pieces)
    recurses on each child sequentially (never asyncio.gather here -- siblings at each level are
    walked one at a time so `budget.calls_made` needs no lock); a non-split (<=1 returned piece)
    stops immediately, no further recursion.
    """
    if _artifact_signal_count(piece) <= 1:
        return [piece]
    if depth >= budget.max_depth:
        publish_trace_event(task_id or "", {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"architect_recursion_depth_capped: {piece.label!r} stopped splitting at depth {depth} (OMA_DECOMPOSITION_MAX_RECURSION_DEPTH={budget.max_depth}), not Tier 0/1 judgment",
            "status": "running",
        })
        return [piece]
    if not budget.calls_remaining():
        publish_trace_event(task_id or "", {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"architect_recursion_call_budget_exhausted: {piece.label!r} stopped splitting -- per-task recursive-call budget ({budget.max_calls}) exhausted, not Tier 0/1 judgment",
            "status": "running",
        })
        return [piece]
    if not budget.nodes_remaining():
        publish_trace_event(task_id or "", {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"architect_recursion_node_cap_reached: {piece.label!r} stopped splitting -- OMA_DECOMPOSITION_MAX_TOTAL_NODES ({budget.max_nodes}) reached, distinct from the depth/call-count caps",
            "status": "running",
        })
        return [piece]

    budget.calls_made += 1
    # Real bug found and root-caused live (2026-08-08) -- see _build_recursive_split_probe_prompt()'s
    # own docstring for the full incident: this used to call decompose_into_constraints_with_
    # artifacts() against an execution-time-narrowed goal, which structurally biased every single
    # probe toward "just one piece." Uses the shared _run_decomposition_with_prompt() parse/
    # validation path (same as every other decomposition call in this module) with the new,
    # genuinely neutral probe prompt instead. allow_cloud_escalation=False, matching Stage A's
    # own biased-draft calls -- a planning-time recursion probe doesn't need cloud-tier judgment.
    probe_prompt = _build_recursive_split_probe_prompt(base_goal_text, piece)
    # no_think=False (2026-08-08, second layer of the same real bug -- see
    # _run_decomposition_with_prompt()'s own docstring for the full incident): this is a genuine
    # open judgment call, not extraction -- confirmed live that no_think=True made the model
    # echo an obviously-splittable piece back unchanged even with the corrected, unbiased prompt.
    #
    # use_grammar=False (2026-08-08, third layer, same incident): STILL confirmed live to echo
    # the piece back unchanged even after the no_think fix. call_structured()'s own hardcoded
    # system prompt ("Respond with ONLY a single JSON object... No prose, no explanation")
    # combined with grammar-constrained decoding forces the model straight into schema-
    # conformant output with no room to actually deliberate on the open question first --
    # plausible for a genuine judgment call in a way it isn't for the original, more mechanical
    # whole-goal extraction (which stays use_grammar=True, unaffected by this).
    # _run_recursion_probe_allowing_reasoning() (2026-08-08, FOURTH and final layer of the same
    # real incident -- see its own docstring): confirmed live that even after the framing,
    # no_think, and use_grammar fixes above, call_structured()'s own hardcoded "no prose, no
    # explanation" system prompt still suppressed genuine deliberation on this open judgment
    # call. Bypasses call_structured() entirely for this one call site.
    sub_items = await _run_recursion_probe_allowing_reasoning(
        probe_prompt, client, model, task_id,
    )

    if len(sub_items) <= 1:
        return [piece]

    parent_creates, parent_requires = set(piece.creates), set(piece.requires)
    child_creates = set().union(*(set(item.creates) for item in sub_items))
    child_requires = set().union(*(set(item.requires) for item in sub_items))
    if not parent_creates <= child_creates:
        publish_trace_event(task_id or "", {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"graph_split_artifact_coverage_gap: split of {piece.label!r} dropped creates coverage, keeping unsplit parent",
            "status": "running",
        })
        return [piece]
    if not parent_requires <= child_requires:
        publish_trace_event(task_id or "", {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"graph_split_requires_coverage_gap: split of {piece.label!r} dropped requires coverage, keeping unsplit parent",
            "status": "running",
        })
        return [piece]

    # Duplicate-artifact dedup: naming-only, strips a colliding `creates` name from the LATER
    # entry. Adds no ordering edge between the two colliding producers -- real write-collision
    # safety is entirely §6's job (per-node branch isolation + line-hunk patch layer), not this
    # planning-time dedup.
    seen_creates: set[str] = set()
    deduped: list[_ConstraintArtifacts] = []
    for item in sub_items:
        kept_creates = []
        for name in item.creates:
            if name in seen_creates:
                publish_trace_event(task_id or "", {
                    "level": "manager", "actor": "manager", "phase": "delegate",
                    "message": f"graph_duplicate_artifact_claim: {name!r} claimed by more than one split of {piece.label!r}, stripped from the later entry",
                    "status": "running",
                })
                continue
            seen_creates.add(name)
            kept_creates.append(name)
        deduped.append(item.model_copy(update={"creates": kept_creates}))

    if len(deduped) >= 3 and budget.calls_remaining():
        # Real, related correctness note (2026-08-08): this must stay SCOPED to `piece`, never
        # the full original `base_goal_text` verbatim -- unlike the recursion PROBE above, this
        # call re-runs a FULL biased-draft decomposition pass (decompose_into_constraints_with_
        # artifacts_biased() -> _build_decomposition_prompt(goal)) on whatever text it's given,
        # so passing the whole multi-part original goal here would make the architect stage try
        # to redecompose the ENTIRE task from scratch instead of refining just this piece's own
        # already-found sub-items. Described as CONTEXT (what piece these sub-items belong to),
        # not as an imperative "Focus ONLY" command -- the same distinction
        # _build_recursive_split_probe_prompt() draws, just reused for a genuinely different
        # downstream call with its own genuinely different prompt shape.
        piece_scoped_goal = (
            f"{base_goal_text}\n\nThe sub-requirements below all belong to one already-"
            f"identified piece of the goal above, {piece.label!r} -- refine THAT piece's own "
            f"breakdown, not the whole original goal."
        )
        deduped = await maybe_upgrade_decomposition_with_architect_stage(
            piece_scoped_goal, deduped, client, model, task_id=task_id,
        )
        budget.calls_made += 3  # 2 concurrent biased drafts + 1 critic call

    budget.node_count += len(deduped) - 1

    # Phase 31 UI (2026-08-08): tag each direct child with the REAL parent it was just split
    # from, before it potentially gets split further itself -- a grandchild produced by a
    # DEEPER recursive call gets its own, closer split_from set inside THAT call's own copy of
    # this same loop (piece.label there is the child's label here, not this level's piece), so
    # following split_from one hop at a time always reconstructs the true, real recursion chain.
    #
    # `lineage` (mutated in place, shared across the WHOLE recursion tree via the one dict
    # object recursively_decompose_constraints() creates once) records EVERY split edge ever
    # made, including intermediate labels that later get consumed by a DEEPER split and so never
    # appear in the final flat output at all (e.g. 'mid_piece' splitting further into leaf_b/
    # leaf_c: mid_piece itself is never a returned item, but lineage['mid_piece']='big_piece'
    # still needs to exist so the UI can chain leaf_b/leaf_c's own split_from='mid_piece' all the
    # way back to the real top-level ancestor, not just one hop). split_from on the final
    # ConstraintNode itself only ever has ONE hop (its own immediate parent); this fuller map is
    # what lets the frontend reconstruct arbitrary-depth recursion instead of collapsing every
    # intermediate level straight to the root.
    if lineage is not None:
        for child in deduped:
            lineage[child.label] = piece.label

    result: list[_ConstraintArtifacts] = []
    for child in deduped:
        child = child.model_copy(update={"split_from": piece.label})
        result.extend(
            await maybe_decompose_piece_further(
                child, base_goal_text, client, model, budget, depth=depth + 1, task_id=task_id,
                lineage=lineage,
            )
        )
    return result


def _dedupe_flattened_labels(items: list[_ConstraintArtifacts]) -> tuple[list[_ConstraintArtifacts], dict[str, str]]:
    """Recursive splitting can independently mint the same short label twice at different tree
    positions (e.g. two unrelated splits both landing on 'validation_rule') -- since the flat
    output feeds build_constraint_nodes() (dict-keyed by label), a collision would silently drop
    one real piece. Renamed with a stable numeric suffix, never dropped.

    Also returns the {original_label: renamed_label} mapping for exactly the items that got
    renamed (Phase 31 UI, 2026-08-08) -- recursively_decompose_constraints()'s own lineage dict
    is populated DURING recursion using each item's original (pre-dedup) label, so a rename here
    must be applied to lineage's own keys afterward too, or the UI's split-lineage chain would
    silently break for the one, real-but-rare renamed node.
    """
    seen: dict[str, int] = {}
    result: list[_ConstraintArtifacts] = []
    renames: dict[str, str] = {}
    for item in items:
        if item.label not in seen:
            seen[item.label] = 1
            result.append(item)
            continue
        seen[item.label] += 1
        new_label = f"{item.label}_{seen[item.label]}"
        renames[item.label] = new_label
        result.append(item.model_copy(update={"label": new_label}))
    return result, renames


async def recursively_decompose_constraints(
    items: list[_ConstraintArtifacts],
    base_goal_text: str,
    client: ModelGatewayClient,
    model: str,
    task_id: str | None = None,
    lineage: dict[str, str] | None = None,
) -> list[_ConstraintArtifacts]:
    """Phase 31 §1.6: the universal entry point -- every task's own decomposition (simple or
    architect-upgraded) is run through this, checking each piece for further splitting at
    effectively zero cost when nothing more is there to find (Tier 0), recursing into genuinely
    complex pieces down to the size the model actually handles best (Tier 1+), bounded by three
    real caps (OMA_DECOMPOSITION_MAX_RECURSION_DEPTH, OMA_ARCHITECT_STAGE_MAX_TOTAL_CALLS,
    OMA_DECOMPOSITION_MAX_TOTAL_NODES). Recursion happens entirely BEFORE build_constraint_nodes()
    is called -- returns one flat, globally-unique-labeled list, so cross-level dependency edges
    "just work" via derive_predecessor_labels_from_overlap()'s own creates/requires-name matching.

    `lineage` (Phase 31 UI, 2026-08-08, real gap found live -- the project owner's own report: "the graph
    shows only the flat, top-level pieces... no visual sense of... this piece was itself split
    further"): an OPTIONAL, opt-in, mutable dict a caller passes in to receive the FULL real
    split lineage -- {child_label: parent_label} for EVERY split edge ever made during this
    call's whole recursion tree, including intermediate labels later consumed by a DEEPER split
    (which never appear in this function's own returned flat list at all). This is strictly
    additive display metadata: the returned list itself, and every scheduling-relevant field on
    every item in it, are byte-for-byte identical whether or not a caller passes `lineage` in --
    confirmed by this file's own pre-existing tests, none of which pass it. None (the default)
    means "no caller wants this" -- no dict is created or populated internally in that case.
    """
    budget = _RecursionBudget(
        max_depth=OMA_DECOMPOSITION_MAX_RECURSION_DEPTH,
        max_calls=OMA_ARCHITECT_STAGE_MAX_TOTAL_CALLS,
        max_nodes=OMA_DECOMPOSITION_MAX_TOTAL_NODES,
        starting_node_count=len(items),
    )
    flattened: list[_ConstraintArtifacts] = []
    for item in items:
        flattened.extend(
            await maybe_decompose_piece_further(
                item, base_goal_text, client, model, budget, task_id=task_id, lineage=lineage,
            )
        )
    deduped, renames = _dedupe_flattened_labels(flattened)
    if lineage is not None and renames:
        # A renamed item's OWN key in lineage (as a child) must follow its new name; any OTHER
        # entry's VALUE (a parent reference) that happens to equal a renamed label must follow
        # too, so a sibling's own chain doesn't silently point at a now-stale label.
        for old_label, new_label in renames.items():
            if old_label in lineage:
                lineage[new_label] = lineage.pop(old_label)
        for child_label in list(lineage.keys()):
            parent_label = lineage[child_label]
            if parent_label in renames:
                lineage[child_label] = renames[parent_label]
    return deduped


def build_constraint_nodes(items: list[_ConstraintArtifacts]) -> dict[str, ConstraintNode]:
    """P13 item 4/10(a): assembles real `ConstraintNode`s from
    `decompose_into_constraints_with_artifacts()`'s own output -- `predecessor_labels` derived
    deterministically (zero-LLM) from real `creates`/`requires` overlap
    (`contracts.constraint_graph.derive_predecessor_labels_from_overlap()`), falling back
    PER-CONSTRAINT to the existing `_dependency_tier_for_constraint_label()` bucket order for
    exactly those labels whose `creates` AND `requires` both came back empty -- never worse than
    today's tier-only behavior, and the tier function is demoted to fallback, not deleted.
    """
    nodes = {
        item.label: ConstraintNode(
            label=item.label, creates=item.creates, requires=item.requires, split_from=item.split_from,
        )
        for item in items
    }
    overlap_predecessors = derive_predecessor_labels_from_overlap(nodes)
    fallback_labels = set(labels_needing_tier_fallback(nodes))
    tiers = {label: _dependency_tier_for_constraint_label(label) for label in nodes}
    for label, node in nodes.items():
        if label in fallback_labels:
            # Tier-bucket fallback: every OTHER label in a strictly EARLIER tier is a
            # predecessor -- real tier-number precedence, not same-tier sort-order artifacts.
            # Applied only to labels with no real creates/requires signal.
            predecessors = [
                other for other in nodes
                if other != label and tiers[other] < tiers[label]
            ]
        else:
            predecessors = overlap_predecessors[label]
        nodes[label] = node.model_copy(update={
            "predecessor_labels": predecessors, "tier": tiers[label],
        })
    return nodes


# Phase 30, P0 (Phase K, §14): loaded once at import time from the
# real, standalone data file -- see contracts/unsupported_domains.json's
# own header comment for why this is separate from (and will later be
# absorbed by) P7's larger dimension_table.json rather than waiting on it.
_UNSUPPORTED_DOMAINS_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "unsupported_domains.json"
)


def _load_unsupported_domains() -> list[dict]:
    data = json.loads(_UNSUPPORTED_DOMAINS_PATH.read_text())
    return data["domains"]


class _UnsupportedDomainTouch(BaseModel):
    domain: str
    goal_snippet: str


class _UnsupportedDomainTouches(BaseModel):
    touches: list[_UnsupportedDomainTouch]


async def classify_unsupported_domain_touches(
    goal: str, client: ModelGatewayClient, model: str, task_id: str | None = None,
) -> list[dict]:
    """Phase 30, P0 (Phase K, §14): a cheap, cached-discipline
    classification step, same style as decompose_into_constraints()
    right above -- run once, at contract-build time, before round 1
    ever starts (and, for a goal that decomposes into sub-contracts,
    re-checked per sub-contract's own narrowed focus -- see
    manager/loop.py's _run_constraint_labels_from()).

    Deliberately semantic/LLM-classified, not literal keyword/string
    matching (per §14 step 1's own explicit correction, added after the
    40-task stress test found real ordinary phrasing -- "migrate the
    data," "log who changed what" -- that maps to a gated domain
    without ever naming it) -- the same discipline this codebase
    already applies wherever real judgment is needed (goal_facts-style
    extraction), not a regex.

    Returns [] for a goal that touches none of the domains in
    contracts/unsupported_domains.json -- never invents a touch that
    isn't really there; a generation failure here degrades to "assume
    supported" (fails open, not closed) rather than blocking a genuinely
    fine goal on a classification hiccup, matching this codebase's own
    "never a false rejection from genuine uncertainty" posture elsewhere
    (e.g. _validate_no_new_field_collides_with_real_target_field's own
    SSH-hiccup handling in specialists/build/specialist.py).
    """
    domains = _load_unsupported_domains()
    domain_list_text = "\n".join(f"- {d['domain']}: {d['description']}" for d in domains)
    valid_domain_names = {d["domain"] for d in domains}
    prompt = (
        "Read this task goal and decide whether it asks for anything in the following list of "
        "domains this system has ZERO real generation support for today. Judge by real MEANING, "
        "not literal keyword matching -- ordinary phrasing that doesn't use a domain's own name "
        "still counts if it describes the same real thing (e.g. \"send a confirmation email when "
        "approved\" describes a one-off templated email even though it never says \"email "
        "template\"; \"print a PDF for the customer\" describes a QWeb report even though it never "
        "says \"QWeb\"). Only flag a domain that is GENUINELY, materially part of what the goal "
        "asks for -- never flag a domain just because a loosely related word appears in passing, "
        "and never force a match onto a domain that doesn't really fit just because nothing else "
        "in the list fits better; most real, ordinary Odoo field/model/view/security work touches "
        "NONE of these domains at all, and that is the normal, expected answer.\n\n"
        "The same discipline applies in the OPPOSITE direction too: the word \"wizard\" appearing "
        "in a goal does NOT by itself mean wizard_transient_model applies. \"Open the email compose "
        "wizard pre-loaded with our template, let the employee edit it before sending\" does NOT "
        "touch wizard_transient_model -- it describes returning an action that points at Odoo's own "
        "already-built-in mail.compose.message wizard, which needs no new TransientModel generated "
        "at all, only an ordinary method (already fully supported). Only flag wizard_transient_model "
        "when the goal genuinely needs a NEW, custom transient model built from scratch.\n\n"
        "The same discipline applies to automation_trigger too: an ordinary time-based scheduled "
        "task does NOT touch automation_trigger. \"Every night at midnight, automatically change "
        "all records that have been in X state for more than 30 days\" does NOT touch "
        "automation_trigger -- it describes an ordinary ir.cron scheduled action (Odoo's own "
        "built-in periodic-task mechanism), which is fully supported and needs no base.automation "
        "rule, webhook, or controller code at all. Only flag automation_trigger when the goal "
        "genuinely needs a base.automation rule (an event-triggered-on-write/create automation "
        "record), a webhook endpoint, or custom controller-triggered code -- never merely because "
        "the goal says \"automatically\" or describes something happening on a schedule.\n\n"
        "The same discipline applies to templated_email too, and this one needs extra care because "
        "the surface wording is deceptive: almost EVERY goal that touches templated_email will use "
        "the word \"email\" or \"send\" -- but so will almost every goal that does NOT touch it, "
        "because mail.thread's own message_post(..., partner_ids=[...]) ALSO genuinely sends a real "
        "email (Odoo's own chatter/follower-notification mechanism), whenever the target model "
        "already inherits mail.thread. Do NOT flag templated_email just because a goal says "
        "\"send an email\"/\"send a confirmation email\"/\"notify the customer\" -- that phrasing "
        "alone is EXACTLY AS CONSISTENT with the fully-supported message_post path as it is with "
        "the unsupported mail.template path, so by itself it proves nothing. Concrete worked "
        "example: \"When a record is marked accepted, automatically send a confirmation email to "
        "the customer including the reference, total, and finish date\" -- do NOT flag "
        "templated_email for this; it is satisfied entirely by composing the body in Python and "
        "calling message_post(...). The ONLY real signal for templated_email is an EXPLICIT need "
        "for a reusable/editable template record (something a non-technical user could open and "
        "edit later in Settings, or a marketing/bulk/one-off blast to many recipients from a "
        "template) -- if the goal never explicitly asks for that, do not flag templated_email, no "
        "matter how many times it says \"email\" or \"send\" or \"notify\" or \"automatically\".\n\n"
        "The same discipline applies to translation too: a goal asking for output text \"in the "
        "customer's own language\"/\"in Dutch or English\" does NOT by itself touch translation. "
        "Odoo's real translation domain is specifically field-level translate=True storage or "
        ".po-file/ir.translation-backed terms -- a plain per-language Python dict of hardcoded "
        "strings keyed by partner_id.lang (e.g. {'nl_NL': 'Beste %(partner)s,', 'en_US': 'Dear "
        "%(partner)s,'}), selected with an ordinary dict lookup and never touching ir.translation "
        "or a .po file at all, is a fully supported, ordinary Python pattern. Only flag "
        "translation when the goal genuinely needs Odoo's own field-level translate=True mechanism "
        "or real .po-file-backed translated terms -- never merely because the goal mentions "
        "multiple languages or says \"in their own language\".\n\n"
        "CRITICAL, if this text below states a specific current-round focus (look for a sentence "
        "like \"This round's own NEW focus is ONLY: '...'\") -- judge ONLY that current round's own "
        "focus against the domain list, not the full multi-part goal text also shown for context. "
        "If the text also explicitly lists work as \"NOT yet in scope for this round\", never flag a "
        "domain solely because it's mentioned inside that not-yet-in-scope list or inside the "
        "original, full goal text being described for background -- that content belongs to a "
        "DIFFERENT round of this same task, not this one, and this call is only asking about the "
        "one round described by the text given to you now.\n\n"
        f"Domains with zero real generation support:\n{domain_list_text}\n\n"
        "For each real touch found, return the domain name (exactly as listed above) and a short "
        "verbatim snippet (a few words, copied from the goal text itself) naming the specific part "
        "of the goal that touches it -- this snippet is used to isolate just that piece from any "
        "other, buildable pieces in the same goal, so it must be an exact substring of the goal "
        "text, not a paraphrase. Return an empty list if the goal touches none of these domains.\n\n"
        f"Task goal: {goal}"
    )
    try:
        result = await call_structured(
            client=client, model=model, prompt=prompt, schema=_UnsupportedDomainTouches,
            no_think=True, task_id=task_id or "classify_unsupported_domain_touches", actor="manager",
            call_label="Checking whether the goal touches an unsupported domain", use_grammar=True,
        )
    except Exception:
        # Fails open (never blocks a genuinely fine goal on a
        # classification hiccup) -- see this function's own docstring.
        return []
    return [
        {"domain": t.domain, "goal_snippet": t.goal_snippet}
        for t in result.touches
        if t.domain in valid_domain_names and t.goal_snippet.strip()
    ]


_LABEL_KEYWORD_RE = re.compile(r"[a-zA-Z]{3,}")
# Generic Odoo/module vocabulary that shows up in nearly every generated
# file regardless of which constraint it actually implements (every
# field declaration ends in "= fields.X()", nearly every constraint
# label mentions "field"/"model"/"rule"/"relation" as filler, not
# substance) -- same discipline as _GENERIC_REVIEW_WORDS above, applied
# here because a generic label word matching boilerplate content masks
# the actual regression: a real, confirmed gap found while writing this
# function's own test, where "fields" (from the label "scheduling_fields")
# matched ordinary `fields.Many2one(...)` boilerplate in content that had
# genuinely dropped the real "scheduling" field entirely.
_CONSTRAINT_LABEL_GENERIC_WORDS = {
    "field", "fields", "model", "models", "rule", "rules", "relation",
    "relations", "add", "adds", "enforce", "enforces", "data", "record",
    "records", "value", "values", "and", "the",
}


# Phase 30, P1e (Phase M, §16): a cheap, deterministic post-processing
# tier classification for decompose_into_constraints()'s own label list
# -- same shallow-keyword-on-the-label-text style as
# _CONSTRAINT_LABEL_GENERIC_WORDS above, not a second, different
# extraction pass over the original goal text. Checked in this exact
# order (0 -> 3): a label touching keywords from more than one tier
# (e.g. "restrict_field_by_security_group") gets the EARLIEST matching
# tier, the conservative choice -- a structural/field-defining aspect
# of a label is real signal that at least PART of it must come early,
# and stable-sorting only ever moves a label as far as its own tier
# requires, never further.
_TIER_0_MODEL_FIELD_KEYWORDS = {"field", "fields", "model", "models"}
_TIER_1_VIEW_KEYWORDS = {"view", "views", "form", "forms", "tree", "list", "lists", "kanban", "search"}
_TIER_2_SECURITY_KEYWORDS = {
    "security", "access", "group", "groups", "rule", "rules", "permission", "permissions",
}
_TIER_3_AUTOMATION_KEYWORDS = {
    "automation", "automations", "cron", "button", "buttons", "action", "actions", "trigger", "triggers",
}
_UNCLASSIFIED_TIER = 4  # everything else -- sorted last, a safe default: menu/demo-data/test-style
# labels typically depend on the structural pieces above already existing, not the reverse.


def _dependency_tier_for_constraint_label(label: str) -> int:
    words = set(_LABEL_KEYWORD_RE.findall(label.lower()))
    if words & _TIER_0_MODEL_FIELD_KEYWORDS:
        return 0
    if words & _TIER_1_VIEW_KEYWORDS:
        return 1
    if words & _TIER_2_SECURITY_KEYWORDS:
        return 2
    if words & _TIER_3_AUTOMATION_KEYWORDS:
        return 3
    return _UNCLASSIFIED_TIER


def sort_constraint_labels_by_dependency_tier(labels: list[str]) -> list[str]:
    """Phase 30, P1e (Phase M, §16): a real, structural fix for a known,
    already-observed "hard, guaranteed failure" -- a model/field-
    defining sub-contract sequenced AFTER a sub-contract that references
    it. Python's sorted() is a STABLE sort: within the same tier, every
    label keeps its original relative order (the LLM's own within-tier
    judgment is never second-guessed) -- only the cross-tier dependency
    direction gets enforced. Does not replace the existing prompt-level
    "not yet in scope" warning in manager/loop.py's own goal_text
    construction -- that stays as a real, cheap backstop for anything
    this keyword heuristic misclassifies; this is the structural fix for
    the common, already-observed case.
    """
    return sorted(labels, key=_dependency_tier_for_constraint_label)


def compute_regressed_constraints(
    prior_files: dict[str, str] | None,
    new_files: dict[str, str],
    constraint_status: dict[str, ConstraintState],
    constraint_nodes: dict[str, ConstraintNode] | None = None,
) -> list[str]:
    """Phase 18 (§22.10): a small, deterministic, CODE-computed check --
    never an LLM judgment call, and never dependent on the specialist's
    own self-report. Reused as-is by Component 3's best-of-N candidate
    scoring (§22.7), which is the whole point of keeping this a plain,
    reusable function rather than something baked into Code-Review's
    own prompt.

    For every constraint currently marked "satisfied" in
    constraint_status, this checks whether the constraint label's own
    keywords -- the same short, snake_case words
    derive_constraint_labels() produced -- were present somewhere in
    `prior_files`' content and are no longer present anywhere in
    `new_files`' content. That's a real, honest, but deliberately
    modest signal: this project has no structured, per-constraint
    acceptance predicate to re-run (constraint_status keys are plain
    labels, not callables), so this is the same word-derived-from-label
    heuristic already used by _same_underlying_finding() above, applied
    to a regression question instead of a recurrence one. Conservative
    by construction: a constraint prior_files has NO evidence for at
    all (its keywords never appeared anywhere) is never flagged --
    "never observed" is not the same claim as "regressed."

    Matches on a short stem (a keyword's own first 5 characters, or the
    whole keyword if shorter), not the exact word -- a real, confirmed
    gap found while writing this function's own test: a label word like
    "scheduling" (from decompose_into_constraints()'s own noun-phrase
    style) never exact-matches real generated code, which says
    "scheduled_at" (a verb/field-name form) -- same underlying concept,
    different inflection. A 5-character stem ("sched") matches both
    without needing a real NLP stemmer dependency for what's still a
    deliberately modest, conservative signal.

    `constraint_nodes` (2026-08-04, real live regression found in the same-night full 30-task
    sweep, task013/015): an OPTIONAL, much more precise sibling check to the label-stem heuristic
    above, run only when the caller has real `ConstraintNode.creates` identifiers available (from
    decompose_into_constraints_with_artifacts()'s own "real Odoo identifiers (model names, field
    names, ...)" extraction -- see build_constraint_nodes()'s docstring). The stem heuristic above
    caught only a constraint's own LABEL keywords fully disappearing; it structurally cannot catch
    a field being silently RENAMED to something semantically related (task013: a constraint
    literally about "invoice" stayed keyword-present because the field was renamed from
    `linked_invoice` to a DIFFERENT invoice-related field, `invoice_id` -- "invoic" still matched,
    so the stem check saw nothing wrong). Testing/QA's own final-round widening check
    (`_reverify_earlier_constraints_field_targets`) is what actually caught this live, but only at
    the very last round, after the round budget meant to fix it was already spent. This check
    looks at the EXACT trailing identifier segment of each `creates` entry (e.g. `linked_invoice`
    from `project.meerwerk.linked_invoice`) -- the real field/method name Build's own generated
    code would contain verbatim -- as a whole-word match, not a stem. A constraint whose `creates`
    identifiers are all still present verbatim is never flagged by this half even if the stem
    check above would (deliberately conservative in the OTHER direction: this is additive
    evidence, not a replacement for the stem check, since not every constraint has a
    `constraint_nodes` entry with non-empty `creates`).
    """
    if not prior_files:
        return []
    prior_text = "\n".join(prior_files.values()).lower()
    new_text = "\n".join(new_files.values()).lower()
    regressed: list[str] = []
    for label, state in constraint_status.items():
        if state != "satisfied":
            continue
        keywords = [
            kw[:5] for kw in _LABEL_KEYWORD_RE.findall(label.lower())
            if kw not in _CONSTRAINT_LABEL_GENERIC_WORDS
        ]
        stem_regressed = False
        if keywords:
            was_present = any(kw in prior_text for kw in keywords)
            still_present = any(kw in new_text for kw in keywords)
            stem_regressed = was_present and not still_present

        exact_regressed = False
        node = (constraint_nodes or {}).get(label)
        if node is not None and node.creates:
            for identifier in node.creates:
                token = identifier.strip().split(".")[-1].strip().lower()
                if not token or not re.match(r"^[a-z_][a-z0-9_]*$", token):
                    continue  # not a plain snake_case identifier -- skip, never guess a pattern
                pattern = re.compile(r"\b" + re.escape(token) + r"\b")
                was_present = bool(pattern.search(prior_text))
                still_present = bool(pattern.search(new_text))
                if was_present and not still_present:
                    exact_regressed = True
                    break

        if (stem_regressed or exact_regressed) and label not in regressed:
            regressed.append(label)
    return regressed


_OSCILLATION_LOOKBACK_ROUNDS = 3


def detect_oscillation(prior_rounds: list[ReplanRound]) -> bool:
    """Phase 18 (§22.11): a deterministic, code-only check comparing
    `regressed_constraints` (§22.10) across the last
    _OSCILLATION_LOOKBACK_ROUNDS rounds for a genuine "fixing A drops B"
    back-and-forth signature -- checks every adjacent round pair in that
    window: if an earlier round regressed some constraint(s), the very
    next round fixed all of them (none still regressed), AND that same
    next round regressed at least one DIFFERENT constraint that wasn't
    part of the earlier round's own regression, that's the real
    oscillation shape, not just "still fixing the same thing."

    The only consequence wired to this in manager/loop.py is forcing a
    narrower, single-constraint focus for the task's remaining rounds --
    there is NO cloud-model-escalation path anywhere in this function or
    its caller, by explicit instruction (the project owner's own direction to keep
    this system on its own local model and make it better through
    scaffolding, never by falling back to a different, more expensive
    model the moment something is hard).
    """
    if len(prior_rounds) < 2:
        return False
    window = prior_rounds[-_OSCILLATION_LOOKBACK_ROUNDS:]
    for earlier, later in zip(window, window[1:]):
        earlier_regressed = set(earlier.verification_result.regressed_constraints)
        later_regressed = set(later.verification_result.regressed_constraints)
        if not earlier_regressed or not later_regressed:
            continue
        fixed = earlier_regressed - later_regressed
        newly_broken = later_regressed - earlier_regressed
        if fixed and newly_broken:
            return True
    return False


def oscillation_majority_signal(prior_rounds: list[ReplanRound]) -> bool:
    """Phase 18 (§22.11)'s own standing guardrail, directly mirroring
    the dossier's cascaded-routing caution that an escalation rate
    holding steady near a third of all cases signals a misconfigured
    threshold, not genuinely hard tasks: fires True when
    detect_oscillation() would have fired on a MAJORITY of this task's
    own rounds so far, not just once. This is a durable flag for a
    human to review (manager/loop.py logs it via append_project_memory,
    never an automated prompt/skill edit) -- consistent with this
    project's existing HIL-approval discipline for anything beyond a
    single task's own retry loop.
    """
    if len(prior_rounds) < 4:
        return False
    fired = sum(
        1 for i in range(1, len(prior_rounds))
        if detect_oscillation(prior_rounds[: i + 1])
    )
    checked = len(prior_rounds) - 1
    return checked > 0 and fired > checked / 2


# Wider than _OSCILLATION_LOOKBACK_ROUNDS (3) -- a real live oscillation
# (Operator's service-management task, 2026-07-11) alternated between two
# unresolved code shapes across 10 rounds, non-adjacently (A-B-A-A-B-A-B-B-A),
# so a 3-round window would have missed most of the actual alternation.
_SHAPE_OSCILLATION_LOOKBACK_ROUNDS = 8


def detect_shape_oscillation(prior_rounds: list[ReplanRound]) -> bool:
    """A second, complementary oscillation signal alongside
    detect_oscillation() above -- found necessary live the same day
    that function shipped. detect_oscillation() only ever looks at
    regressed_constraints (§22.10), which requires a constraint to have
    been SATISFIED at some point before it can "regress" -- structurally
    blind to a task that has never once passed a round, which is
    exactly the population most likely to actually be oscillating (confirmed
    live: Operator's service-management task alternated between two distinct,
    never-resolved uncovered_paths shapes -- roughly lines 44-53 vs.
    65-100 in models.py -- across 10 straight rounds, going A-B-A-A-B-A-B-B-A,
    while regressed_constraints stayed empty[] every single round since
    nothing had ever been satisfied yet for anything to regress FROM).

    Detects the same underlying "not converging, alternating between
    approaches" shape using uncovered_paths instead: true if some
    round's uncovered_paths set fuzzy-matches (_fuzzy_same_gap) an
    EARLIER round's from at least 2 rounds back (not just the
    immediately prior one -- that's ordinary unresolved-gap recurrence,
    already handled by revise_contract_from_verification()'s own
    CRITICAL escalation) -- i.e. the task returned to a shape it had
    already left, rather than making monotonic forward progress through
    genuinely new gaps.
    """
    window = [r for r in prior_rounds[-_SHAPE_OSCILLATION_LOOKBACK_ROUNDS:] if r.verification_result.uncovered_paths]
    if len(window) < 3:
        return False
    shapes = [sorted(r.verification_result.uncovered_paths) for r in window]
    for i in range(2, len(shapes)):
        if _fuzzy_same_gap(shapes[i], shapes[i - 1]):
            continue  # ordinary consecutive recurrence, not oscillation
        for j in range(i - 1):
            if _fuzzy_same_gap(shapes[i], shapes[j]):
                return True
    return False


# Phase 30, P2c (§13, item 4): a real, confirmed blind spot in BOTH
# oscillation detectors above -- each is built around RECURRENCE (a task
# returning to a previously-seen shape). Neither fires for genuine
# "problem-hopping": a task that fails a DIFFERENT way every round
# (install failure, then a field bug, then a security issue, then an
# XML issue, never repeating any shape) -- arguably the worse case,
# since it also never gives Phase 29's rule-learning pipeline anything
# to generalize from (nothing ever repeats long enough to become a
# pattern). 3+ distinct root_cause values, corrected from an original
# 4+ proposal after a real worked example (a task hitting a double-
# cancel bug, a missed-reminder bug, and a duplicate-invoice bug -- 3
# distinct shapes, none repeating) would have been missed by a 4+
# threshold.
_FAILURE_DIVERSITY_MIN_DISTINCT_CAUSES = 3


def detect_failure_diversity(prior_rounds: list[ReplanRound]) -> bool:
    """True when this task's own round history shows
    `_FAILURE_DIVERSITY_MIN_DISTINCT_CAUSES` or more distinct
    `root_cause` values with NONE of them repeating -- the genuine
    problem-hopping shape, complementary to (never a replacement for)
    `detect_oscillation()`/`detect_shape_oscillation()` above, which
    both require a shape to recur to fire at all. A root_cause of
    `None` (not yet classified, or a round that never failed) is
    excluded, never counted as its own "distinct" value.
    """
    causes = [r.verification_result.root_cause for r in prior_rounds if r.verification_result.root_cause]
    if len(causes) < _FAILURE_DIVERSITY_MIN_DISTINCT_CAUSES:
        return False
    distinct = set(causes)
    return len(distinct) >= _FAILURE_DIVERSITY_MIN_DISTINCT_CAUSES and len(distinct) == len(causes)


# P12 Tier A item 19 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
# A Finding 18): matches the window every OTHER detector in this family uses, for consistency.
_VERIFICATION_NEVER_EXECUTED_WINDOW = 3


def detect_verification_never_executed(prior_rounds: list[ReplanRound]) -> bool:
    """P12 Tier A item 19: a real, confirmed gap in this whole detector family -- none of
    `detect_oscillation()`/`detect_shape_oscillation()`/`detect_failure_diversity()` above ever
    fires for a structurally dead-end task (e.g. Bug #2's data_change stub, or any capability
    with no real handler): the same uninformative signature repeats every round (empty
    `uncovered_paths`, `reproduction_confirmed=False`, a near-identical `notes` string), so
    nothing regresses (no oscillation), nothing diversifies (no shape oscillation, no failure
    diversity) -- every detector correctly declines to fire, but nothing POSITIVELY names "this
    is not oscillating or diversifying, it is structurally stuck" either. The task rides the
    generic `round_budget` escalation, reading identically to a genuinely hard problem given 5
    varied honest attempts, when it's actually the opposite: verification never even ran.

    True when every round in the last `_VERIFICATION_NEVER_EXECUTED_WINDOW` rounds has
    `reproduction_confirmed=False` AND both `uncovered_paths` and `coverage_diff` are
    empty/near-empty -- the real, structural "nothing was ever verified" signature, not just
    "verification ran and found problems."
    """
    if len(prior_rounds) < _VERIFICATION_NEVER_EXECUTED_WINDOW:
        return False
    window = prior_rounds[-_VERIFICATION_NEVER_EXECUTED_WINDOW:]
    return all(
        not r.verification_result.reproduction_confirmed
        and not r.verification_result.uncovered_paths
        and not (r.verification_result.coverage_diff or "").strip()
        for r in window
    )


_SIGNIFICANT_WORD_RE = re.compile(r"[a-zA-Z_]{4,}")
# Generic words that show up in nearly every Code-Review finding
# regardless of the actual underlying issue -- excluded so overlap
# detection reflects the real substance of a complaint, not boilerplate
# review-speak that would make two UNRELATED findings look recurring.
_GENERIC_REVIEW_WORDS = {
    "this", "that", "with", "from", "does", "flag", "flagged", "fails",
    "fail", "code", "review", "attempt", "issue", "issues", "found",
    "blocking", "requires", "implementation", "should", "must", "which",
    "these", "will", "significant", "revision", "required",
}


def _recurs_anywhere_in_rules(finding_text: str, rules: list[str]) -> bool:
    """Real, confirmed bug found live (2026-07-24, task 020's Group C
    re-run): both call sites below used to check ONLY `rules[-1]` --
    the single, immediately-preceding round's own complaint -- for
    recurrence, matching the comment's own reasoning ("a constraint
    that already exhausted one round budget is the one MOST likely to
    keep recurring"). But a real, live oscillation doesn't always
    recur on CONSECUTIVE rounds: round 1 referenced out-of-scope
    fields (`amount_total`, `expected_finish_date`), round 2 correctly
    removed them (a different complaint, about `lang`, was `rules[-1]`
    at that point), and round 3 silently REintroduced the exact same
    out-of-scope fields -- invisible to a same-as-rules[-1] check,
    since `rules[-1]` going into round 3 was round 2's `lang`
    complaint, not round 1's fields complaint. The CRITICAL_RULE_PREFIX
    mechanism exists precisely to make a recurring problem impossible
    to miss -- but it was structurally blind to this "fixed, then
    silently un-fixed two rounds later" shape, the exact failure mode
    that kept task 020 oscillating even after the OTHER 12 real bugs
    found the same session were fixed. Checking the full rules history
    (not just the last entry) is strictly a widening of what counts as
    "recurring," never a narrowing -- every case the old `rules[-1]`-only
    check already caught is still caught here (that's just the len-1
    special case of "anywhere").
    """
    return any(_same_underlying_finding(finding_text, r) for r in rules)


def _same_underlying_finding(a: str, b: str) -> bool:
    """A real, general fix for confirmed dead code (Phase 18): the
    CRITICAL_RULE_PREFIX/split_critical_rules() mechanism was built
    earlier this session to make a genuinely recurring, resolved
    correction stand out from the flat, capped rules history -- but
    nothing ever actually PRODUCED a critical rule; only the specialist
    prompt's own rendering side ever consumed one. Confirmed live: a
    real, semantic Code-Review finding (product-scoping logic being a
    non-functional stub) recurred across MANY rounds of the same task,
    each time just another one of 8 capped ordinary history entries,
    never once surfaced with the weight its own recurrence warranted.

    Deliberately simple and dependency-free (no LLM call -- this
    function stays synchronous, called from deep in the per-round
    replanning path): a plain word-overlap heuristic over each
    finding's own significant (4+ letter, non-generic) words. Good
    enough to catch "the same real complaint, reworded" without needing
    exact string equality (which real, evolving Code-Review prose
    almost never produces twice) or a full LLM judgment call.
    """
    words_a = set(w.lower() for w in _SIGNIFICANT_WORD_RE.findall(a)) - _GENERIC_REVIEW_WORDS
    words_b = set(w.lower() for w in _SIGNIFICANT_WORD_RE.findall(b)) - _GENERIC_REVIEW_WORDS
    if not words_a or not words_b:
        return False
    overlap = words_a & words_b
    smaller = min(len(words_a), len(words_b))
    return len(overlap) / smaller >= 0.4


def is_known_repeat(candidate: FailureRecord, records: list[FailureRecord], lookback: int = 3) -> bool:
    """P13 item 8 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    §22.2): a deterministic, zero-LLM pre-check -- true when `candidate` matches the same
    `failure_category` AND the same underlying finding (via the already-built, already-tuned-
    against-real-live-incidents `_same_underlying_finding()` word-overlap heuristic above, never a
    second, competing similarity function) as one of the last `lookback` entries in `records`.
    Meant to be run immediately after a round's VerificationResult is produced (and folded into a
    FailureRecord, item 7), before the next round's Build call dispatches -- catching MAST's own
    FM-1.3 "step repetition" class (15.7% of all real-world multi-agent failures, the single
    largest individual failure mode in its 14-mode taxonomy) architecturally, since a local model
    correcting a byte-identical prior mistake presented as its own prior output has a 0-17%
    success rate (arXiv:2606.05976) -- this gate does not rely on the model catching it itself.

    Real, confirmed gap found and fixed 2026-08-02 (the Sonnet 5 A/B follow-up,
    docs/reports/PHASE30_P14_ITEM3_SONNET5_FOLLOWUP_2026-08-02.md, task005): originally took a
    `ConstraintNode` directly (`node.failure_records`), which structurally does not exist for a
    plain, non-decomposed, single-constraint task -- `TaskContract.constraint_nodes` is ONLY ever
    populated when a goal decomposes into 3+ constraints (manager/loop.py's own
    `if len(constraint_labels) >= 3 or unsupported_touches:` gate). Confirmed live: task005 (a
    single-requirement "add an onchange" goal) hit the same real, substantive Code-Review finding
    (the field never exposed on the form view) in rounds 4 and 5, verbatim-adjacent, and this
    entire mechanism never even ran -- `current_constraint_label` stays `None` for the task's whole
    life, so the caller's own gate condition was never true. Widened to take the records list
    directly so the caller can pass either a node's own history (decomposed case, unchanged
    behavior) or the contract's flat `failure_records` (non-decomposed case, the real fix) --
    the SAME detection logic either way, never a second, competing check for the non-decomposed
    shape.
    """
    recent = records[-lookback:]
    for prior in recent:
        if prior.failure_category != candidate.failure_category:
            continue
        if _same_underlying_finding(
            f"{prior.location} {prior.observed}", f"{candidate.location} {candidate.observed}",
        ):
            return True
    return False


def _relative_offsets(paths: list[str]) -> dict[str, set[int]]:
    """Each file's own uncovered line numbers, shifted to start at 0 --
    lets two uncovered_paths sets be compared by SHAPE (same relative
    gap) rather than by exact absolute line numbers, which drift as
    unrelated code above the gap gets edited. Module-level (not nested
    in revise_contract_from_verification()) so detect_oscillation() can
    reuse it too.
    """
    by_file: dict[str, list[int]] = {}
    for p in paths:
        if ":" not in p:
            continue
        file_part, _, line_part = p.rpartition(":")
        try:
            by_file.setdefault(file_part, []).append(int(line_part))
        except ValueError:
            continue
    return {f: {n - min(lines) for n in lines} for f, lines in by_file.items()}


def _fuzzy_same_gap(a: list[str], b: list[str]) -> bool:
    """True if two uncovered_paths sets are the SAME underlying gap,
    tolerating the kind of 1-3 line drift real live rounds produce:
    same file(s) AND >=70% JACCARD similarity (intersection over union)
    of relative line offsets within each file.

    Real bug found and fixed live (2026-07-11, same day this was first
    written): the original version scored overlap / min(len(a), len(b))
    -- intersection relative to the SMALLER set only. That silently
    treats a small gap as "the same" as a much larger, totally unrelated
    one whenever the small set happens to be a coincidental subset (e.g.
    a 4-line gap {0,1,2,3} scored as a 100% match against an unrelated
    8-line gap {0,1,2,3,18,19,20,21}, since 4/min(4,8)=1.0), which is
    exactly backwards -- it should be penalized for the 4 extra elements
    the larger set has that the smaller one doesn't. Found while building
    detect_shape_oscillation() (below), which false-positived on this.
    Jaccard (intersection / union) correctly penalizes size mismatches
    in both directions instead of just checking rough containment.
    Threshold recalibrated to 0.55 for Jaccard (down from the old
    metric's 0.7) -- Jaccard is a strictly more conservative measure
    (union >= max(len(a), len(b)) >= the old metric's denominator), so
    the same threshold value would reject real drift cases the old
    metric used to correctly accept. 0.55 verified live against both:
    the real 1-line-per-round drift case this was built for (16-line
    gap, 13/19 relative offsets shared = 0.68, correctly still matches)
    and the false-positive case above (4/8 = 0.5, correctly no longer
    matches) -- clean separation between the two.
    """
    offs_a, offs_b = _relative_offsets(a), _relative_offsets(b)
    if set(offs_a) != set(offs_b) or not offs_a:
        return False
    for f in offs_a:
        sa, sb = offs_a[f], offs_b[f]
        union = len(sa | sb)
        if union == 0 or len(sa & sb) / union < 0.55:
            return False
    return True


# Real, general fix (2026-07-25, task 001's 9th fresh attempt): even
# with `_validate_goal_named_field_is_declared()` (specialists/build/
# specialist.py) correctly firing pre-write on every single round,
# Build never once managed to actually declare the named field across
# a full fresh 5-round budget -- confirmed live via direct DB query,
# the identical rejection recurred verbatim on rounds 1 through 5. The
# CRITICAL_RULE_PREFIX escalation alone (is_recurring_notes, below)
# only makes the SAME diagnostic message more prominent; it never gives
# the model the one thing it was apparently failing to construct on its
# own -- the actual, concrete field-declaration line. This mirrors the
# exact lesson already learned and applied to the uncovered_paths case
# above (consecutive_repeats >= 2 switching from diagnosis to an
# explicit, concrete instruction) -- generalized here to any recurring
# validator rejection whose notes match this marker, deterministically
# parsing the field name (already embedded in the validator's own
# message, quoted) and its declared type (from the goal's own
# `Field type:` line or an inline `Field: name (Type)` shorthand -- both
# real, observed conventions across this project's task specs) to
# produce a literal, ready-to-paste `fields.X(...)` line. General by
# construction: works for any task following this project's own
# metadata convention, not specific to any one field/model/task.
_FIELD_OMISSION_MARKER = "never actually declares it"
_FIELD_OMISSION_PRESCRIPTIVE_MARKER = "failed to declare this field for"
_FIELD_OMISSION_NAME_RE = re.compile(r"goal names a field '([^']+)'")


def _consecutive_field_omission_repeats(field_name: str, rules: list[str]) -> int:
    """Purpose-built alternative to `_consecutive_notes_repeats()` for
    this one escalation: real, confirmed bug found live (2026-07-25,
    tasks 001 and 002's first post-fix-25 runs) -- using the generic
    word-overlap `_same_underlying_finding()` heuristic here meant the
    escalated PRESCRIPTIVE rule text (deliberately worded differently
    from the validator's own diagnostic message -- "Add EXACTLY this
    line..." shares almost no significant vocabulary with "the goal
    names a field... never actually declares it...") never counted as
    "the same finding" as the next round's plain diagnostic notes,
    resetting the consecutive counter to 0 the moment the prescriptive
    form fired once. Confirmed live: round 3 correctly escalated to the
    concrete snippet, but rounds 4 and 5 both silently fell back to the
    plain diagnostic form, because `_consecutive_notes_repeats()` no
    longer recognized round 3's own rule as a repeat of round 4's
    finding. This anchors recurrence on the one thing that's guaranteed
    stable across both wordings -- the field name itself, plus either
    marker phrase -- so the escalation, once triggered, never silently
    reverts for the rest of this field's own recurrence.
    """
    count = 0
    for rule in reversed(rules):
        if field_name in rule and (_FIELD_OMISSION_MARKER in rule or _FIELD_OMISSION_PRESCRIPTIVE_MARKER in rule):
            count += 1
        else:
            break
    return count
# Anchored to line-start (2026-07-25, same fix as specialists/build/
# specialist.py's own `_GOAL_NAMED_FIELD_RE`) -- an unanchored version
# matches "field:" anywhere in ordinary prose, not just this project's
# structured metadata convention. See that file's own comment for the
# live failure this caused (task 003, a spurious match inside "I want
# to see a priority field: Low, Normal, High.").
_GOAL_FIELD_TYPE_LINE_RE = re.compile(r"^\s*Field\s*type:\s*`?(?:fields\.)?(\w+)`?", re.IGNORECASE | re.MULTILINE)
_GOAL_FIELD_INLINE_TYPE_RE = re.compile(
    r"^\s*Field(?:\s*name)?:\s*`?\w+`?\s*\(\s*(?:fields\.)?(\w+)", re.IGNORECASE | re.MULTILINE
)

# Real, general bug found live (2026-07-25, Phase 25A regression gate,
# task 003's resubmission): this dict used to also carry "selection",
# "many2one", "one2many", "many2many" entries, each a confidently-worded
# but FAKE placeholder ("[('TODO', 'TODO')]", "'<comodel.name>'") --
# exactly the "never guess" rule this function's own docstring already
# states for computed fields, violated for these four types instead.
# Confirmed live: task 003 escalated correctly (the round-3+ recurrence
# mechanism fired exactly as designed) but PRESCRIBED
# `fields.Selection([('TODO', 'TODO')], string='Order Priority')` --
# worse than the plain diagnostic fallback, since it's a concrete,
# authoritative-sounding instruction that is simply wrong. Relational
# types stay excluded permanently here (a comodel name is never safely
# guessable from a goal's own Field-type line alone) -- this mirrors
# specialists/build/specialist.py's own `_GOAL_FIELD_TYPE_SKELETONS`,
# which was ALWAYS correctly narrower than this dict for the identical
# reason (see that file's own `_autofix_goal_named_field_declaration_
# missing()` docstring). Selection is NOT excluded -- unlike a comodel,
# this project's own goal convention genuinely does state real option
# values concretely enough to build a correct, non-fake skeleton (see
# `_build_selection_skeleton()` below) -- it's handled as its own,
# separate, options-aware branch, never through this generic dict.
_FIELD_TYPE_SKELETONS = {
    "text": "fields.Text(string={label!r})",
    "char": "fields.Char(string={label!r})",
    "boolean": "fields.Boolean(string={label!r}, default=False)",
    "integer": "fields.Integer(string={label!r})",
    "float": "fields.Float(string={label!r})",
    "monetary": "fields.Monetary(string={label!r}, currency_field='currency_id')",
    "date": "fields.Date(string={label!r})",
    "datetime": "fields.Datetime(string={label!r})",
    "html": "fields.Html(string={label!r})",
    # Phase 30, P5 (§6, Phase C): kept in sync with specialists/build/
    # specialist.py's own identical dict -- see that file's own comment
    # right above its matching entries for the real "why."
    "binary": "fields.Binary(string={label!r})",
    "image": "fields.Image(string={label!r})",
}

# Real, general fix (2026-07-25, same investigation): mirrors
# specialists/build/specialist.py's own `_build_selection_skeleton()` --
# duplicated, not imported, per this codebase's own manager/specialists
# layering (import-linter's own contract forbids `manager` importing
# `specialists`). Extended here (and back-ported to that file's own
# version, same fix) to recognize THREE real conventions this project's
# own task goals actually use for a Selection field's options, not just
# one -- confirmed live, task 003's own resubmission used the SEPARATE-
# LINE form (`Selection: low/normal/high` on its own line), which the
# original version (inline-parenthetical-only) never matched at all,
# silently falling through to the generic (now-removed) TODO skeleton
# above instead.
_GOAL_FIELD_SELECTION_OPTIONS_RE = re.compile(
    r"^\s*Field(?:\s*name)?:\s*`?\w+`?\s*\(\s*Selection:\s*([\w/]+)", re.IGNORECASE | re.MULTILINE
)
_GOAL_FIELD_SELECTION_LINE_RE = re.compile(r"^\s*Selection:\s*([\w/]+)", re.IGNORECASE | re.MULTILINE)
_GOAL_FIELD_SELECTION_VALUES_LINE_RE = re.compile(r"^\s*Values:\s*\[(.*?)\]", re.IGNORECASE | re.MULTILINE)
_GOAL_FIELD_SELECTION_VALUES_TUPLE_RE = re.compile(r"\(\s*'([\w-]+)'\s*,\s*'[^']*'\s*\)")
_GOAL_FIELD_SELECTION_DEFAULT_RE = re.compile(
    r"^\s*(?:Field(?:\s*name)?:\s*`?\w+`?\s*\([^)]*\bdefault\s+(\w+)|Default:\s*`?'?(\w+)`?'?)",
    re.IGNORECASE | re.MULTILINE,
)


def _build_selection_skeleton(goal: str, label: str) -> str | None:
    """Returns a real, valid `fields.Selection([...], string=...)` call
    built from real option values this project's own goal conventions
    actually state, trying each known convention in turn -- or None if
    none of them match, meaning the goal doesn't state options concretely
    enough to build a real skeleton (never fabricates placeholder
    options; see `_FIELD_TYPE_SKELETONS`'s own comment for why a fake
    skeleton is worse than no prescription at all).
    """
    options: list[str] | None = None
    inline_match = _GOAL_FIELD_SELECTION_OPTIONS_RE.search(goal)
    if inline_match:
        options = [o.strip() for o in inline_match.group(1).split("/") if o.strip()]
    if not options:
        line_match = _GOAL_FIELD_SELECTION_LINE_RE.search(goal)
        if line_match:
            options = [o.strip() for o in line_match.group(1).split("/") if o.strip()]
    if not options:
        values_match = _GOAL_FIELD_SELECTION_VALUES_LINE_RE.search(goal)
        if values_match:
            options = _GOAL_FIELD_SELECTION_VALUES_TUPLE_RE.findall(values_match.group(1))
    if not options or len(options) < 2:
        return None
    pairs = ", ".join(f"({o!r}, {o.replace('_', ' ').title()!r})" for o in options)
    default_match = _GOAL_FIELD_SELECTION_DEFAULT_RE.search(goal)
    default_kwarg = ""
    if default_match:
        default_value = default_match.group(1) or default_match.group(2)
        if default_value and default_value in options:
            default_kwarg = f", default={default_value!r}"
    return f"fields.Selection([{pairs}], string={label!r}{default_kwarg})"


def _skeleton_from_goal_facts(field_name: str, label: str, goal_facts: dict | None) -> str | None:
    """Phase 25B (2026-07-25): the PREFERRED source for a field
    declaration snippet -- `goal_facts` (contracts/goal_facts.py's
    schema-guided, JSON-Schema-constrained extraction, cached once on
    the contract) instead of regex-matching `goal` prose. Returns None
    when `goal_facts` doesn't apply (absent, or naming a DIFFERENT
    field -- never silently reused for the wrong field) so the caller
    falls through to the existing regex family unchanged; also returns
    None (a real, final answer, not "try something else") when
    `goal_facts` confirms this field is computed/related/onchange, for
    the identical safety reason `_build_field_declaration_snippet()`'s
    own regex guard exists.
    """
    if not goal_facts or goal_facts.get("field_name") != field_name:
        return None
    if goal_facts.get("is_computed"):
        return None
    options = goal_facts.get("selection_options") or []
    if len(options) >= 2:
        pairs = ", ".join(f"({o!r}, {o.replace('_', ' ').title()!r})" for o in options)
        default = goal_facts.get("selection_default")
        default_kwarg = f", default={default!r}" if default in options else ""
        return f"fields.Selection([{pairs}], string={label!r}{default_kwarg})"
    skeleton = _FIELD_TYPE_SKELETONS.get((goal_facts.get("field_type") or "").lower())
    if skeleton:
        return skeleton.format(label=label)
    return None  # goal_facts matched but had nothing usable (e.g. a relational type) -- try regex


def _build_field_declaration_snippet(field_name: str, goal: str, goal_facts: dict | None = None) -> str | None:
    """Returns a literal, ready-to-paste `name = fields.X(...)` line for
    `field_name`, or None if neither `goal_facts` nor the goal's own
    metadata names a recognized Odoo field type for it -- never guesses
    a type that wasn't actually stated.

    Real, general safety guard (2026-07-25, task 004): a goal asking for
    a COMPUTED field (`compute=_compute_x`, `related=...`, `onchange=...`)
    needs real business logic this function has no way to safely
    prescribe -- suggesting a plain, non-computed skeleton would be a
    concrete, confidently-worded WRONG instruction (worse than the
    diagnostic-only fallback), since a field that "exists" but never
    actually computes anything would silently satisfy the presence check
    while being semantically broken. Same guard already applied to
    specialists/build/specialist.py's own `_autofix_goal_named_field_
    declaration_missing()` for the identical reason.

    Phase 25B (2026-07-25): tries `goal_facts` (structured extraction)
    FIRST; the regex family below is now the fallback, kept completely
    unchanged, for when `goal_facts` is absent/doesn't apply -- e.g. a
    caller that hasn't been threaded a contract with goal_facts
    populated yet (tests, or a task shape extraction was never run for).
    """
    label = field_name.replace("_", " ").strip().title()
    if goal_facts and goal_facts.get("field_name") == field_name:
        from_facts = _skeleton_from_goal_facts(field_name, label, goal_facts)
        if from_facts:
            return f"{field_name} = {from_facts}"
        if goal_facts.get("is_computed"):
            return None  # goal_facts is authoritative here -- never fall through to regex

    field_line_match = re.search(
        rf"^\s*Field(?:\s*name)?:\s*`?{re.escape(field_name)}`?.*$", goal or "", re.IGNORECASE | re.MULTILINE
    )
    if field_line_match and re.search(r"\b(?:compute|related|onchange)\s*=", field_line_match.group(0), re.IGNORECASE):
        return None
    selection_call = _build_selection_skeleton(goal, label)
    if selection_call:
        return f"{field_name} = {selection_call}"
    type_match = _GOAL_FIELD_TYPE_LINE_RE.search(goal) or _GOAL_FIELD_INLINE_TYPE_RE.search(goal)
    if not type_match:
        return None
    type_word = type_match.group(1).lower()
    skeleton = _FIELD_TYPE_SKELETONS.get(type_word)
    if not skeleton:
        return None
    return f"{field_name} = {skeleton.format(label=label)}"


_BUSINESS_RULE_FAILURE_RE = re.compile(
    r"Business rule check FAILED: '(?P<rule>[^']*)' -- real observed result (?P<observed>\{.*?\}):",
)


def _build_failure_record(
    round_number: int,
    contract: TaskContract,
    verification_result: VerificationResult,
    code_review_finding: str | None,
    field_omission_snippet: str | None,
) -> FailureRecord:
    """P13 item 7 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    §22.2): builds one real FailureRecord from the exact same evidence
    revise_contract_from_verification()'s own new_rules construction already uses -- never a
    second, independently-derived judgment that could drift from the prose `rules` entry.

    Deliberately conservative on `failure_category`: only classifies into a specific category when
    an existing, already-checked signal supports it; everything else stays `unclassified` rather
    than guess, matching this whole codebase's "never invent a value that isn't concretely
    present" discipline. `location` has no real structured source in VerificationResult today (a
    real, documented gap the item's own critique names) -- falls back to the first
    `uncovered_paths` entry, then the current constraint label, never invented.
    """
    location = (
        sorted(verification_result.uncovered_paths)[0] if verification_result.uncovered_paths
        else (contract.current_constraint_label or "")
    )
    if field_omission_snippet:
        return FailureRecord(
            round_number=round_number, constraint_label=contract.current_constraint_label,
            failure_category=FailureCategory.missing_required_field,
            location=location, observed=verification_result.notes,
            concrete_alternative=field_omission_snippet, source="testing_qa",
        )
    if code_review_finding:
        lowered = code_review_finding.lower()
        if "not yet in scope" in lowered or "out of scope" in lowered:
            category = FailureCategory.scope_violation
        elif "xmlid" in lowered or "external id" in lowered or "xml id" in lowered:
            category = FailureCategory.xmlid_mismatch
        else:
            category = FailureCategory.unclassified
        return FailureRecord(
            round_number=round_number, constraint_label=contract.current_constraint_label,
            failure_category=category, location=location,
            observed=code_review_finding, concrete_alternative="", source="code_review",
        )
    if verification_result.regressed_constraints:
        return FailureRecord(
            round_number=round_number, constraint_label=contract.current_constraint_label,
            failure_category=FailureCategory.regression, location=location,
            observed=verification_result.notes,
            concrete_alternative=f"Restore: {verification_result.regressed_constraints}",
            source="testing_qa",
        )
    # P13 item 12a integration (per the item's own explicit design, added 2026-08-02 integration
    # pass): a failed business-rule probe (specialists/testing_qa/specialist.py's
    # _run_business_rule_probes()) carries its own distinctive "Business rule check FAILED:"
    # marker in verification_result.notes -- checked BEFORE the generic reproduction_gap fallback
    # below, since a business-rule mismatch can occur even when reproduction_confirmed is True
    # (the claimed field/effect genuinely exists; a SPECIFIC interpretation of it is wrong).
    business_rule_match = _BUSINESS_RULE_FAILURE_RE.search(verification_result.notes)
    if business_rule_match:
        return FailureRecord(
            round_number=round_number, constraint_label=contract.current_constraint_label,
            failure_category=FailureCategory.business_rule_mismatch,
            location=location, observed=business_rule_match.group("observed"),
            concrete_alternative=business_rule_match.group("rule"), source="testing_qa",
        )
    if not verification_result.reproduction_confirmed:
        return FailureRecord(
            round_number=round_number, constraint_label=contract.current_constraint_label,
            failure_category=FailureCategory.reproduction_gap, location=location,
            observed=verification_result.notes, concrete_alternative="", source="testing_qa",
        )
    return FailureRecord(
        round_number=round_number, constraint_label=contract.current_constraint_label,
        failure_category=FailureCategory.unclassified, location=location,
        observed=verification_result.notes, concrete_alternative="", source="testing_qa",
    )


def revise_contract_from_verification(
    contract: TaskContract,
    verification_result: VerificationResult,
    round_number: int,
    code_review_finding: str | None = None,
) -> tuple[TaskContract, str]:
    """Returns a NEW contract (never a blind copy) plus the plain-
    language reasoning for the round log. New rules are appended
    describing exactly what went wrong and what this attempt must
    address -- built from the real evidence already available
    (root_cause, uncovered_paths, notes, any Code-Review finding),
    never a fresh guess at what might be wrong.
    """
    new_rules: list[str] = []
    reasoning_parts: list[str] = [f"Round {round_number} failed."]

    # Real, general bug found live (Operator's service-management task,
    # 2026-07-11, round 11-12): a pre-write structural validator
    # (specialists/build/specialist.py's _validate_view_fields_exist_on_model)
    # raised the SAME error twice in a row (a view referencing a field the
    # model never declares) -- unlike the code_review_finding branch below,
    # this root_cause/notes branch had NO recurrence detection at all, so a
    # validator failure recurring verbatim never got the CRITICAL escalation
    # treatment either, same root gap as the uncovered_paths and
    # code_review_finding cases fixed earlier the same day. Reuses the same
    # word-overlap heuristic already built for Code-Review prose (notes text
    # here can also be reworded slightly round to round for the same
    # underlying complaint).
    # Real, general bug found live (2026-07-12, same task, after a
    # round-budget-exhaustion checkpoint/resume cycle): the marker-
    # string filter above (`notes_marker in r`) depends on THIS
    # function's own literal phrasing ("Prior attempt (round N) failed,
    # classified as ...") having been what's actually IN contract.rules
    # -- but manager.loop's own resume path (resume_task_after_checkpoint,
    # ~loop.py:669-675) REBUILDS contract.rules from scratch on every
    # resume using a completely different format ("Round N: <notes>"),
    # which contains neither "failed, classified as" nor "Code-Review
    # flagged" (the code_review_finding branch's own marker, below).
    # Confirmed live: an identical Code-Review finding ("Defines 7 extra
    # fields...") recurred verbatim across a checkpoint/resume boundary,
    # yet got NO CRITICAL_RULE_PREFIX escalation on the very first round
    # after resuming, because prior_finding_rules/prior_notes_rules came
    # back empty -- exactly the scenario recurrence detection exists for
    # (a constraint that already exhausted one round budget is the one
    # MOST likely to keep recurring). Fixed generally: compare against
    # contract.rules[-1] directly, whatever format it happens to be in
    # -- _same_underlying_finding()'s own word-overlap heuristic doesn't
    # care about the wrapping prose, only the real content, so this is
    # robust to any future rules-formatting change too, not just this
    # one resume-path bug.
    is_recurring_notes = bool(contract.rules) and _recurs_anywhere_in_rules(
        verification_result.notes, contract.rules
    )
    notes_prefix = CRITICAL_RULE_PREFIX if is_recurring_notes else ""
    field_omission_snippet = None
    field_omission_repeats = 0
    if _FIELD_OMISSION_MARKER in verification_result.notes:
        name_match = _FIELD_OMISSION_NAME_RE.search(verification_result.notes)
        if name_match:
            field_name = name_match.group(1)
            field_omission_repeats = (
                _consecutive_field_omission_repeats(field_name, contract.rules) if contract.rules else 0
            )
            if field_omission_repeats >= 2:
                field_omission_snippet = _build_field_declaration_snippet(
                    field_name, contract.goal, goal_facts=contract.goal_facts,
                )

    if field_omission_snippet:
        # Diagnostic-only feedback (the plain notes text below) already
        # recurred identically for 3+ straight rounds by this point --
        # switch to a concrete, literal instruction instead of repeating
        # the same diagnosis again, same discipline as the uncovered_paths
        # consecutive_repeats >= 2 case below. Re-fires EVERY round from
        # here on (not just once) -- `_consecutive_field_omission_repeats()`
        # recognizes this rule's own prescriptive wording as a repeat of
        # itself (see its own docstring), so the concrete instruction
        # stays in force for as long as the omission keeps recurring,
        # never silently reverting to the diagnostic-only form again.
        new_rules.append(
            f"{CRITICAL_RULE_PREFIX}You have failed to declare this field for "
            f"{field_omission_repeats + 1} rounds in a row. Add EXACTLY this line inside the "
            f"model class body (adjust only the comodel/inverse-field placeholders if this is a "
            f"relational field -- the field name, and the fact that it must be a real class "
            f"attribute, are non-negotiable): {field_omission_snippet}"
        )
        reasoning_parts.append(f"field omission recurring, prescribed: {field_omission_snippet}")
    elif verification_result.root_cause:
        # P13 item 13 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
        # 2026-07-29.md §22.2): direct application of the Self-Correction Illusion finding
        # (item 8) -- a model correcting *itself* within one continuous context corrects far
        # less reliably (0-17%) than a model evaluating output attributed to an external role
        # (23-93 points higher). This finding was rendered as "Prior attempt (round N) failed"
        # -- ambiguous, easily read as Build's own prior self-assessment rather than an
        # independent role's verdict. Reworded to explicit external-role attribution
        # (Testing/QA is the real, always-different role that produced verification_result) --
        # a prompt-construction detail only; the underlying content (root_cause, notes) is
        # unchanged, so every existing recurrence-detection/marker-string consumer
        # (_recurs_anywhere_in_rules, _should_use_best_of_n's install-failure markers) still
        # matches, since those all search within `verification_result.notes`'s own text, never
        # the wrapping template prose.
        new_rules.append(
            f"{notes_prefix}Testing/QA's own evaluation of your round {round_number} work found "
            f"this (classified as {verification_result.root_cause!r}): {verification_result.notes}"
        )
        reasoning_parts.append(
            f"root_cause={verification_result.root_cause!r} -- {verification_result.notes}"
        )
    else:
        new_rules.append(
            f"{notes_prefix}Testing/QA's own evaluation of your round {round_number} work found: "
            f"{verification_result.notes}"
        )
        reasoning_parts.append(verification_result.notes)

    if verification_result.uncovered_paths:
        # Real, general bug found live (Operator's "8-piece" service-management
        # task, 2026-07-10): the SAME uncovered_paths (models/models.py:45-48,
        # an out-of-scope create() override never removed or tested) recurred
        # across 3+ rounds unaddressed, while Code-Review kept passing/failing
        # on unrelated, ever-changing complaints each round. Only Code-Review
        # findings got the CRITICAL_RULE_PREFIX recurrence escalation below --
        # a recurring coverage gap was always just one more diluted entry in
        # the capped ordinary rules history, with no more prominence on round
        # 5 than it had on round 1. Fixed generally: an uncovered_paths set
        # that exactly repeats the immediately prior round's own uncovered
        # set is promoted into the same prominent CRITICAL section, since
        # exact-set equality is the correct check here (these are literal,
        # deterministic file:line paths, not evolving prose -- unlike a
        # Code-Review finding, there's no rewording to account for).
        sorted_paths = sorted(verification_result.uncovered_paths)
        gap_marker = "left genuinely untested"
        prior_uncovered_rules = [r for r in contract.rules if gap_marker in r]
        # How many rounds IN A ROW (ending with the immediately prior one)
        # already reported an uncovered set FUZZY-MATCHING this one -- walk
        # backwards from the end of contract.rules (rules accumulate in
        # round order) counting consecutive matches, not just a single-round
        # recurrence check.
        #
        # Real, general bug found live (same task, same day, right after the
        # exact-match version of this shipped): exact list equality was too
        # fragile in practice. Confirmed live: the same underlying untested
        # code block recurred round after round, but its line numbers drift
        # by 1-3 each time as the specialist edits unrelated code above it
        # (e.g. ['models.py:31'..'models.py:72'] one round, then
        # ['models.py:32'..'models.py:74'] the next -- same file, same
        # 16-line size, same relative shape, just shifted) -- exact string
        # equality never matched twice in a row, so the CRITICAL escalation
        # almost never fired despite the gap being genuinely unresolved for
        # 6+ rounds. Fixed with a real similarity check instead of exact
        # equality: same file(s) AND >=70% overlap of RELATIVE line offsets
        # within each file (each file's own lines shifted to start at 0) --
        # this tolerates the kind of incidental drift seen live while still
        # correctly treating a genuinely different gap (different file, or
        # a very different size/shape) as not recurring.
        consecutive_repeats = 0
        for rule in reversed(contract.rules):
            if gap_marker not in rule:
                continue
            # The path list is always rendered as the LAST '[...]' in these
            # rule strings (str(sorted_paths)) -- extract its quoted items
            # generically rather than assuming any particular file prefix.
            bracket_matches = re.findall(r"\[[^\[\]]*\]", rule)
            prior_list = re.findall(r"'([^']+)'", bracket_matches[-1]) if bracket_matches else []
            if not _fuzzy_same_gap(sorted_paths, prior_list):
                break
            consecutive_repeats += 1
        is_recurring_gap = consecutive_repeats >= 1
        gap_prefix = CRITICAL_RULE_PREFIX if is_recurring_gap else ""
        if consecutive_repeats >= 2:
            # Real, general bug found live (same task, same day, immediately
            # after the first fix above shipped): CRITICAL_RULE_PREFIX alone
            # was not enough -- confirmed live, the specialist saw this exact
            # CRITICAL warning FOUR rounds in a row and never once acted on
            # it, because "left genuinely untested -- address or cover them"
            # is diagnostic (says WHERE the gap is) but never prescriptive
            # (never says WHAT action closes it). A Code-Review finding like
            # "use position='after' on mobile" tells Build exactly what to
            # do; this message never did. After 2 confirmed-identical repeats
            # (i.e. the 3rd occurrence), switch to an explicit, concrete
            # instruction instead of repeating the same diagnosis a 4th time.
            new_rules.append(
                f"{CRITICAL_RULE_PREFIX}This exact code ({sorted_paths}) was left genuinely untested and "
                f"has now failed identically across {consecutive_repeats + 1} rounds in a row. Stop leaving "
                f"it in place: either DELETE it outright (it is not part of this contract's declared "
                f"deliverables, so removing it is the simplest correct fix), or -- only if it is a genuine "
                f"hard Odoo requirement -- add a real test/reproduction path that exercises it. Do not "
                f"submit another revision that leaves these exact lines untouched and untested."
            )
        else:
            new_rules.append(
                f"{gap_prefix}The following were left genuinely untested last time -- address or cover "
                f"them this attempt (do not just rewrite unrelated code around them): {sorted_paths}"
            )
        reasoning_parts.append(f"uncovered_paths={sorted_paths}")

    if code_review_finding:
        # Check whether this is the SAME real complaint as the most
        # recent prior Code-Review finding already in this contract's
        # own rules -- if so, mark it CRITICAL this time so it lands in
        # Build's own prominent section instead of getting diluted as
        # just one more entry in the capped ordinary history.
        #
        # Same marker-string fragility fixed above (is_recurring_notes):
        # this used to filter for the literal "Code-Review flagged"
        # marker string, which manager.loop's own resume path never
        # produces (it rebuilds contract.rules as "Round N: <notes>"
        # entries instead) -- so recurrence detection silently went
        # blind on the very first round after every checkpoint/resume,
        # exactly when a finding is most likely to actually be
        # recurring. Compares against contract.rules[-1] directly now,
        # same fix, same reasoning.
        is_recurring = bool(contract.rules) and _recurs_anywhere_in_rules(
            code_review_finding, contract.rules
        )
        prefix = CRITICAL_RULE_PREFIX if is_recurring else ""
        new_rules.append(
            f"{prefix}Code-Review flagged this last attempt -- fix it this time: {code_review_finding}"
        )
        reasoning_parts.append(f"Code-Review finding: {code_review_finding}")

    # P13 item 7: one function, two writes -- the same evidence that just built new_rules
    # (prose, unchanged, still Build's own rendering source) also produces one real,
    # machine-consumable FailureRecord, additive alongside `rules`, never replacing it.
    new_failure_record = _build_failure_record(
        round_number, contract, verification_result, code_review_finding, field_omission_snippet,
    )
    updated_constraint_nodes = contract.constraint_nodes
    if contract.current_constraint_label and contract.current_constraint_label in contract.constraint_nodes:
        # Per item 7's own design: once constraint_nodes (item 4) exists, FailureRecords belong
        # on ConstraintNode.failure_records too, not only the flat contract-level list -- the flat
        # list stays the rendering source for `rules` prose; the node-scoped copy is what item 8's
        # is_known_repeat() pre-check gate and the mechanical Orchestrator actually query.
        node = contract.constraint_nodes[contract.current_constraint_label]
        updated_constraint_nodes = {
            **contract.constraint_nodes,
            contract.current_constraint_label: node.model_copy(
                update={"failure_records": [*node.failure_records, new_failure_record]}
            ),
        }

    # Phase 30 §26 item 3: the LITERAL, unwrapped text of this round's own failure evidence --
    # verification_result.notes (which often already carries a real install log_tail excerpt
    # passed through verbatim) plus any Code-Review finding, concatenated with no added
    # explanatory prose, no truncation. Overwrites (never appends to) the prior round's value --
    # this field is deliberately "only the most recent round," the rules list already carries
    # full round-over-round history for anything needing that.
    raw_failure_parts = [verification_result.notes]
    if code_review_finding:
        raw_failure_parts.append(code_review_finding)
    previous_round_raw_failure_text = "\n\n".join(p for p in raw_failure_parts if p)

    new_contract = contract.model_copy(
        update={
            "rules": [*contract.rules, *new_rules],
            "failure_records": [*contract.failure_records, new_failure_record],
            "constraint_nodes": updated_constraint_nodes,
            "previous_round_raw_failure_text": previous_round_raw_failure_text,
        }
    )
    reasoning = " ".join(reasoning_parts)
    return new_contract, reasoning


def _retry_routing_decision(
    current_specialist: SpecialistType,
    original_capability_class: str | None,
    same_theme: bool,
) -> SpecialistType:
    """P12 Tier S/A item 9 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    §4): the pure, deterministic half of `select_specialist_for_retry()`'s routing logic,
    extracted into its own explicit, independently-testable transition table -- resolves B
    Finding 3 (classify+route+judge+retry fused into one function with no internal boundary)
    and B Finding 10 (two separate live-incident fixes were needed for this exact routing
    function because its states were never made explicit; see `select_specialist_for_retry()`'s
    own docstring for the full two-incident history this table now encodes directly).

    Every `(current_specialist, original_capability_class, same_theme)` combination this
    function can ever be called with maps to exactly one of three outcomes -- this is the
    entire transition table, made explicit rather than left implicit in an if-chain:

    | current_specialist | original_capability_class    | same_theme | -> |
    |---------------------|------------------------------|------------|-----|
    | code_review          | readonly_investigation        | (n/a)      | code_review (standalone audit, stays here forever) |
    | code_review          | anything else                 | (n/a)      | bug_fix (temporary diagnostic detour, always bounces back) |
    | anything else         | (n/a)                          | True       | code_review (persistent finding, detour to diagnose) |
    | anything else         | (n/a)                          | False      | current_specialist (default: no signal to switch) |

    Deliberately does NOT decide `same_theme` itself -- that's a genuine content judgment
    (does round N's Code-Review finding mean the same thing as round N-1's, worded
    differently), inherently needing `classify_findings_same_theme()`'s own LLM call, not a
    table lookup. Only the ROUTING decision (what to do once that judgment is known) is
    deterministic and belongs in this table.
    """
    if current_specialist == SpecialistType.code_review:
        if original_capability_class == CapabilityClass.readonly_investigation.value:
            return SpecialistType.code_review
        return SpecialistType.bug_fix
    if same_theme:
        return SpecialistType.code_review
    return current_specialist


async def select_specialist_for_retry(
    contract: TaskContract,
    verification_result: VerificationResult,
    prior_rounds: list[ReplanRound],
    client: ModelGatewayClient,
    model: str,
) -> SpecialistType:
    """Defaults to the same specialist that just ran. Switches only on
    a real signal: the SAME underlying Code-Review finding recurring
    across 2+ rounds unaddressed -- a sign that blindly retrying Build
    the same way a third time won't help; route this round through
    Code-Review itself instead, so its own output can concretely
    describe what needs to change before the next Build attempt.

    "Same" is judged by classify_findings_same_theme()'s cascade, not
    exact string equality -- a real, evolving Code-Review review
    rephrases its specific complaints each round even for a persistent
    issue, so exact equality never actually fires in practice (found
    during a real Phase 15 verification pass).

    A real, GENERAL bug found live (Phase 16 QA pass, not specific to
    any one task): code_review was always meant to be a one-round
    diagnostic detour -- this function's own docstring already said
    "before the next Build attempt" -- but nothing ever routed back to
    bug_fix afterward. Once the theme-recurrence branch above picked
    code_review once, the fallback (`return contract.specialist_type`)
    kept returning code_review forever, since `contract.specialist_type`
    was already code_review from that point on. A task that hit this
    once would burn its ENTIRE remaining round budget on review-only
    rounds that never touch the actual code again -- confirmed live: a
    real scheduling+product-scoping task got stuck on code_review for
    rounds 4 and 5 straight, never fixing the real AttributeError
    Code-Review itself had already identified in round 3, and escalated
    having made no further progress. Fixed generally, for every task
    shaped this way, not just this one: whatever ran as code_review
    unconditionally routes back to bug_fix the very next round.
    """
    if contract.specialist_type == SpecialistType.code_review:
        # Real, confirmed bug found live, TWICE, in two different
        # shapes: first, this unconditional bounce-back assumed every
        # code_review round belongs to a module_dev task with a real
        # Build role to return to -- fixed by checking capability_class
        # for the readonly_investigation case (a standalone audit,
        # Code-Review solo forever, no Build role at all).
        #
        # But that fix itself then broke the ORIGINAL case it was meant
        # to preserve: for a module_dev task on a TEMPORARY code_review
        # detour round (the normal, intended one-round diagnostic stop
        # this function's own docstring describes), contract.
        # capability_class is readonly_investigation too -- ONLY for
        # that one detour round, not because the task itself is a real
        # audit. Checking contract.capability_class (the CURRENT,
        # possibly-mid-detour value) can't tell these two cases apart.
        # Confirmed live: a real scheduling task got stuck bouncing
        # code_review->code_review->code_review for 2 straight rounds,
        # the code never touched again, going nowhere every round it
        # spent this way -- functionally the same stuck-forever bug as
        # before, just reached from the other direction. The task's own
        # task_created event -- capability_class as classified ONCE, at
        # the very start, never touched by any mid-loop detour -- is the
        # one reliable way to tell "this task IS readonly forever" from
        # "this task is briefly ON a readonly-shaped detour round."
        original_capability_class = get_original_capability_class(str(contract.task_id))
        return _retry_routing_decision(contract.specialist_type, original_capability_class, same_theme=False)

    if len(prior_rounds) >= 2:
        last_two = prior_rounds[-2:]
        findings = [r.code_review_finding_summary for r in last_two]
        if findings[0] and findings[1]:
            # Real infra change, 2026-07-22: `model` is the caller's own
            # classifier_model, threaded in but not used elsewhere in this
            # function (confirmed by direct inspection). Same-theme
            # comparison is a pure boolean judgment on already-known text --
            # live-benchmarked the same night: qwen3.6-27b truncates on
            # invisible thinking tokens even at a generous token budget;
            # GPU Worker 03's resident 9B model (thinking disabled server-
            # side) answers correctly in under a second. See
            # manager/loop.py's own FAST_EXTRACTION_MODEL comment for the
            # full rationale.
            same_theme = await classify_findings_same_theme(
                findings[0], findings[1], client, "qwen3-9b-fast-extraction", task_id=str(contract.task_id),
            )
            return _retry_routing_decision(contract.specialist_type, None, same_theme=same_theme)

    return _retry_routing_decision(contract.specialist_type, None, same_theme=False)


# P14 item 7 (2026-08-01): the exact same two marker strings
# specialists/build/specialist.py's own _INSTALL_FAILURE_RULE_MARKERS already defines as the
# canonical generic-sandbox-failure prefix -- deliberately not re-derived or imported (that
# module's own constant is private/internal to a large specialist file, and duplicating two
# literal strings here is simpler and less coupling than reaching into it), but kept byte-for-byte
# identical on purpose so a change to one is easy to notice should ever need mirroring in the
# other.
_GENERIC_FAILURE_NOTES_PREFIXES = ("Sandbox install failed", "Sandbox pre-flight")


def _is_generic_failure_notes(notes: str) -> bool:
    """True if `notes` is generic sandbox-wrapper boilerplate rather than a real, specific
    failure signature -- see the P14 item 7 comment at this function's call site for why this
    guard exists: a plain substring match on generic text would produce meaningless cross-module
    "matches" between genuinely unrelated failures.
    """
    stripped = notes.strip()
    return any(stripped.startswith(prefix) for prefix in _GENERIC_FAILURE_NOTES_PREFIXES)


def should_escalate_to_operator(
    contract: TaskContract,
    round_number: int,
    elapsed_seconds: float,
    verification_result: VerificationResult,
    prior_rounds: list[ReplanRound] | None = None,
) -> str | None:
    """Returns the real, honest escalation reason -- "round_budget",
    "wall_clock", "context_pressure", or "repeated_failure" -- or None if
    the loop should keep retrying. Phase 17 (§21.5.5) changed this from a
    plain bool to a reason string specifically so
    summarize_escalation_for_operator() can tell Operator the real reason it
    stopped, rather than a single generic message regardless of why.

    context_pressure is a genuinely different KIND of check from the
    other three: it's proactive (evaluated every round, not just once
    the round/wall-clock limit is already hit) and it looks at the real
    token cost of what's about to be sent to the next round's specialist
    against that specialist's own real context window -- never a fixed
    constant, since different rounds can route to different specialists
    with different real windows (select_specialist_for_retry()'s own
    theme-recurrence routing). Only the hard ceiling triggers a stop here
    -- the soft trigger is purely a compression-scope decision used by
    Continue (below), never a signal to stop the loop early, matching the
    same restraint already governing round-budget/wall-clock (neither of
    those fires early on a "getting close" warning either).
    """
    if round_number >= contract.planning_round_budget:
        return "round_budget"
    if elapsed_seconds >= contract.round_wall_clock_cap_seconds:
        return "wall_clock"
    if estimate_context_pressure(contract) >= HARD_CEILING_FRACTION:
        return "context_pressure"

    # Phase 26B (2026-07-27, audit Finding #2): reads the SAME
    # authoritative identity manager/loop.py already resolved and
    # cached on this contract at creation time (contract.module_identity)
    # -- never independently re-derives its own guess. Falls back to a
    # fresh resolve_module_identity() call only for the unusual case of
    # a contract built before this field existed (should never happen
    # on a real, freshly-created task, but a stale/replayed checkpoint
    # is not something to crash on).
    module = contract.module_identity or resolve_module_identity(contract.goal)
    repeat_check = check_repeated_failures(module) if module else {"should_pause": False}
    if repeat_check["should_pause"]:
        return "repeated_failure"

    # P12 Tier S item 3 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
    # check_repeated_failures_across_modules() (manager/learning.py) was fully built and tested
    # (Phase 15, §19.4, reproducing the real Phase 13 cross-module ir.model.access.csv bug) but
    # never actually called from any live path -- dead code. Wired in here, scoped to
    # root_cause == "pattern_worth_a_rule" specifically: that is the one category this project's
    # own root-cause taxonomy already defines as a deterministic, structurally-detected pattern
    # (e.g. "generated models_py assigns ['_inherit'] more than once in the same file"), so its
    # own verification_result.notes text is a real, stable signature likely to recur near-verbatim
    # across different modules hitting the same underlying generation bug -- unlike "one_off"/
    # "skill_gap", whose notes are typically task-specific prose that would rarely match another
    # module's failure text at all.
    #
    # P14 item 7 (docs/planning/PHASE30_P14_ITEM7_ONE_OFF_LATENT_STRUCTURE_2026-08-01.md):
    # a real, offline analysis of 1,741 one_off-classified rounds found 124 specific clusters that
    # each recur 3+ times, several independently cross-validating bug classes P11's own live
    # benchmark tracing found by a completely different method -- real, recurring cross-module
    # patterns that were structurally invisible to this gate purely because they were classified
    # one_off at generation time (each task genuinely looks like a first-time occurrence from its
    # own narrow, single-task view -- classify_root_cause() has no cross-task history to draw on).
    # Extended below to also check one_off, closing that gap. The same report's own §3 found a
    # real risk this widening must not reintroduce: ~22 of 269 clusters (519 of 1,741 rows) are
    # generic wrapper text ("Sandbox install failed: ...", "Sandbox pre-flight failed: ...") that
    # can mean many different, unrelated real bugs -- a plain substring match
    # (check_repeated_failures_across_modules()'s own real matching logic, manager/learning.py) on
    # that generic prefix would produce meaningless cross-module "matches" between genuinely
    # unrelated failures. _is_generic_failure_notes() below excludes exactly that prefix text,
    # reusing the exact marker strings specialists/build/specialist.py's own
    # _INSTALL_FAILURE_RULE_MARKERS already establishes as the canonical generic-sandbox-failure
    # signature (never inventing a new, separate list of the same two strings).
    if (
        verification_result.root_cause in ("pattern_worth_a_rule", "one_off")
        and verification_result.notes
        and not _is_generic_failure_notes(verification_result.notes)
    ):
        cross_module_check = check_repeated_failures_across_modules(verification_result.notes)
        if cross_module_check["should_pause"]:
            return "repeated_failure"

    # P12 Tier S/A item 7: manager.tools.fold_code_review_findings()'s new arbitration marker
    # -- a genuine two-gate disagreement (Testing/QA's own reproduction was strongly
    # corroborated, yet Code-Review still blocked it) escalates immediately rather than
    # burning ordinary retry rounds. Retrying an unmodified pipeline against the SAME
    # disagreement is unlikely to resolve it (Testing/QA will very likely report the same
    # corroborated pass again, Code-Review will very likely flag the same real concern again)
    # -- this is exactly the "reconsideration path" gap B Finding 11 named: without this, a
    # genuine disagreement between the two gates just looks like an ordinary blocking finding
    # and burns retries identically to one, instead of getting a human decision sooner.
    if verification_result.gates_disagree:
        return "gates_disagree"

    # P12 Tier A item 19 (A Finding 18): checked here, alongside the other real signal-based
    # checks above, so a structurally dead-end task (verification never actually running, 3
    # rounds in a row) escalates with an HONEST, specific reason as soon as that pattern is
    # confirmed -- instead of silently riding the generic round_budget escalation several
    # rounds later, reading identically to a genuinely hard problem given varied honest
    # attempts when it's actually the opposite.
    if prior_rounds and detect_verification_never_executed(prior_rounds):
        return "verification_never_executed"
    return None


_ESCALATION_REASON_TEXT = {
    "round_budget": "it ran out of retry attempts",
    "wall_clock": "it ran out of time",
    "context_pressure": "the context was getting too large to safely continue, not because it ran out of attempts",
    "repeated_failure": "it kept hitting the same underlying problem",
    "gates_disagree": "Testing/QA and Code-Review genuinely disagreed -- Testing/QA's own verification was strongly corroborated, but Code-Review still found a real blocking issue",
    "verification_never_executed": "verification never actually ran, several rounds in a row -- this isn't a genuinely hard problem being retried with varied honest attempts, it's a structurally stuck path",
}

_ESCALATION_SUMMARY_PROMPT_TEMPLATE = """\
A real automated task has stopped after {round_count} round(s) and needs a \
human decision. It stopped because: {reason_text}.

Original goal: {goal}

Round-by-round history (each entry: what was tried, what happened, any \
Code-Review finding):
{rounds_text}

Write a 3-5 sentence, plain-language explanation for a person who has NOT \
been reading the technical log above. Cover: what was actually tried, what \
specifically keeps going wrong, and -- if this looks like a genuine \
disagreement or design decision rather than a bug (e.g. Code-Review keeps \
objecting to an architectural choice) -- name the actual decision the human \
needs to make. Never a mechanical round-by-round recap. Write only the \
explanation itself, no preamble, no headers."""


def _format_rounds_for_prompt(prior_rounds: list[ReplanRound]) -> str:
    lines = []
    for r in prior_rounds:
        vr = r.verification_result
        lines.append(
            f"Round {r.round_number}: {vr.notes}"
            + (f" Code-Review: {r.code_review_finding_summary}" if r.code_review_finding_summary else "")
        )
    return "\n".join(lines) if lines else "(no rounds completed)"


def estimate_manager_context_pressure(
    goal: str, prior_rounds: list[ReplanRound], existing_summary: str | None, manager_model: str,
) -> float:
    """The Manager's OWN real memory pressure -- goal + any already-
    compressed rolling summary + the current (uncompressed) round
    history -- against the Manager's OWN real context window. A real,
    previously-missing check (the project owner's own explicit finding, confirmed
    against real 2025-2026 multi-agent orchestration practice): only
    estimate_context_pressure() above existed before this, and that one
    checks the NEXT SPECIALIST's window, never the Manager's own.
    """
    window = MODEL_CONTEXT_WINDOWS.get(manager_model, UNKNOWN_MODEL_WINDOW_FALLBACK)
    tokens = estimate_tokens(goal)
    if existing_summary:
        tokens += estimate_tokens(existing_summary)
    tokens += sum(
        estimate_tokens(r.verification_result.notes) + estimate_tokens(r.code_review_finding_summary or "")
        for r in prior_rounds
    )
    return tokens / window if window else 0.0


_MID_LOOP_COMPRESSION_PROMPT_TEMPLATE = """\
A real automated task is still in progress -- round {round_count} so far, \
not stopped, not escalated. Its own working memory is getting large and \
needs to be condensed before continuing, WITHOUT losing anything that \
matters for avoiding the same mistakes again.

Original goal (do not restate, only for your own context): {goal}

{existing_summary_block}Round-by-round history to fold in (what was tried, \
what happened, any Code-Review finding):
{rounds_text}

Write a real, dense rolling summary covering EVERY round above: what was \
tried, what specifically failed, and why -- detailed enough that a future \
attempt reading only this summary (never the raw rounds again) would not \
repeat the same mistake. Never vague ("various issues were found") -- name \
the actual, specific problems. Write only the summary itself, no preamble, \
no headers."""


async def compress_round_history_mid_loop(
    goal: str,
    prior_rounds: list[ReplanRound],
    existing_summary: str | None,
    client: ModelGatewayClient,
    model: str,
    task_id: str | None = None,
) -> str:
    """Fired mid-loop, proactively, whenever the Manager's OWN memory
    crosses the soft threshold -- NEVER an escalation condition on its
    own, unlike round_budget/wall_clock/context_pressure (the project owner's own
    explicit instruction: "we continue working, we continue working").
    The round loop resumes immediately after this returns -- this never
    raises PauseForOperator. Rolling: folds any EARLIER compression's own
    summary back in, so a second (or third) compression event within
    the same round budget still reflects every round from the very
    start, not just the ones since the last compression. contract.goal
    is passed through only for the model's own context -- the prompt
    explicitly tells it not to restate the goal, since the goal itself
    is never a compression target.
    """
    existing_summary_block = (
        f"Already-compressed summary of earlier rounds (fold this in, do not drop it):\n{existing_summary}\n\n"
        if existing_summary else ""
    )
    try:
        prompt = _MID_LOOP_COMPRESSION_PROMPT_TEMPLATE.format(
            round_count=len(prior_rounds), goal=goal, existing_summary_block=existing_summary_block,
            rounds_text=_format_rounds_for_prompt(prior_rounds),
        )
        summary = await generate_checked(
            client, model, [{"role": "user", "content": prompt}],
            max_tokens=max(2000, 400 * len(prior_rounds)),
            task_id=task_id, actor="manager", call_label="Summarizing round history so far",
        )
        return summary.strip()
    except Exception:
        # Honest, non-crashing fallback -- built from the same real
        # data, never a generic placeholder or silently dropped rounds.
        fallback_lines = [existing_summary] if existing_summary else []
        fallback_lines.append(_format_rounds_for_prompt(prior_rounds))
        return "\n".join(fallback_lines)


def _rounds_text_with_compression(prior_rounds: list[ReplanRound], pre_compressed_summary: str | None) -> str:
    """Folds in any earlier mid-loop compression (the project owner's own explicit
    design) so the FINAL escalation summary / success digest reflects
    every round from the very start, even the ones no longer held raw.
    """
    raw_text = _format_rounds_for_prompt(prior_rounds)
    if not pre_compressed_summary:
        return raw_text
    return f"Earlier rounds, already summarized:\n{pre_compressed_summary}\n\nMore recent rounds, in full:\n{raw_text}"


class _EscalationSummary(BaseModel):
    """P11 finding #15 (docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md §1.15,
    confirmed twice, same session): a minimal, single-field schema so
    `summarize_escalation_for_operator()` can use grammar-constrained decoding
    (`call_structured(..., use_grammar=True)`) instead of a raw free-text
    `generate_checked()` call. Real, grounded root cause: the leaked `human_summary` text had NO
    `<think>` tags at all (confirmed directly against both real leaked responses) -- the model
    narrated its entire reasoning process as literal prose ("Here's a thinking process:...")
    despite the prompt's own explicit "Write only the explanation itself, no preamble, no
    headers" instruction and `no_think=True` already being passed. `strip_think_block()` cannot
    help here structurally -- there is no tag to find and strip. This is the exact same failure
    shape `generate_checked()`'s own docstring already documents being fixed by grammar-
    constrained decoding for a different call ("a real, measured 41.84s call... dropped to 5.37s
    with response_format set, producing a correct, complete answer with zero prose preamble") --
    reapplying an already-proven fix, not a new guess.
    """

    summary: str


async def summarize_escalation_for_operator(
    goal: str,
    prior_rounds: list[ReplanRound],
    reason: str,
    client: ModelGatewayClient,
    model: str,
    task_id: str | None = None,
    pre_compressed_summary: str | None = None,
) -> str:
    """Phase 17 (§21.5.3): a genuinely human-facing summary, separate
    from the existing technical `message` field (which stays exactly as
    it is). On any generation failure, falls back to a real, honest
    (if less polished) summary built from the same real data rather than
    a generic placeholder or a crash -- matching this project's own
    "never fabricate, degrade honestly" discipline elsewhere.
    """
    reason_text = _ESCALATION_REASON_TEXT.get(reason, "it could not proceed further on its own")
    try:
        prompt = _ESCALATION_SUMMARY_PROMPT_TEMPLATE.format(
            round_count=len(prior_rounds), reason_text=reason_text, goal=goal,
            rounds_text=_rounds_text_with_compression(prior_rounds, pre_compressed_summary),
        )
        # Real, confirmed gap found live: this deployment's models always
        # narrate a full reasoning trace before their actual answer
        # (see strip_think_block()'s own docstring) -- a flat 1500-token
        # budget was routinely too tight for a 5-round escalation to
        # ever reach its closing </think>, so generate_checked() (which
        # correctly refuses to return an unfinished thought as if it
        # were final) kept raising IncompleteResponseError, silently
        # falling back to the honest-but-plain template every time
        # instead of the real, polished summary. Scaling with round
        # count gives the model enough room to think through what it's
        # actually being asked to summarize.
        #
        # P11 finding #15 (2026-08-01, confirmed twice): switched from generate_checked()
        # (raw free text) to call_structured(use_grammar=True) -- the leaked human_summary text
        # confirmed live had NO <think> tags at all, so strip_think_block() structurally cannot
        # help; grammar-constrained decoding is the already-proven fix for exactly this "model
        # narrates its reasoning as plain prose despite no_think" shape (see generate_checked()'s
        # own docstring for the prior, independent confirmation of this same mitigation).
        result = await call_structured(
            client=client, model=model, prompt=prompt, schema=_EscalationSummary,
            no_think=True, task_id=task_id, actor="manager",
            call_label="Writing the escalation summary for Operator", use_grammar=True,
        )
        return result.summary.strip()
    except Exception:
        last_notes = prior_rounds[-1].verification_result.notes if prior_rounds else "no rounds completed"
        return (
            f"After {len(prior_rounds)} round(s), this task stopped because {reason_text}. "
            f"The most recent attempt's own result: {last_notes}"
        )


_SUCCESS_DIGEST_PROMPT_TEMPLATE = """\
A real automated task just succeeded after {round_count} round(s).

Original goal: {goal}

What the final, passing attempt actually produced: {final_summary}

Round-by-round history (if more than one round was needed):
{rounds_text}

Respond with ONLY a JSON object of the exact shape: {{"headline": "one \
concise sentence naming what was ultimately built/found and, if relevant, \
where it lives (e.g. the real module name)", "notable_moment": "one \
sentence naming a real, genuinely noteworthy problem that was hit and fixed \
along the way, OR null if this task passed cleanly with nothing noteworthy \
to call out -- never invent one"}}"""

_SUCCESS_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


async def summarize_success_for_operator(
    goal: str,
    prior_rounds: list[ReplanRound],
    final_summary: str,
    client: ModelGatewayClient,
    model: str,
    task_id: str | None = None,
    pre_compressed_summary: str | None = None,
) -> dict:
    """Phase 17 (§21.5.6): closes the gap where a passing task told Operator
    nothing beyond a bare status flip. Returns {"headline": str,
    "notable_moment": str | None} -- never a flat string, so the UI can
    render the callout distinctly when (and only when) one is real.
    """
    try:
        rounds_text = (
            _rounds_text_with_compression(prior_rounds, pre_compressed_summary)
            if (prior_rounds or pre_compressed_summary) else "(passed on the first round)"
        )
        prompt = _SUCCESS_DIGEST_PROMPT_TEMPLATE.format(
            round_count=len(prior_rounds) + 1, goal=goal, final_summary=final_summary,
            rounds_text=rounds_text,
        )
        cleaned = await generate_checked(
            client, model, [{"role": "user", "content": prompt}],
            max_tokens=max(800, 400 * (len(prior_rounds) + 1)), temperature=0.1,
            task_id=task_id, actor="manager", call_label="Writing the success digest for Operator",
        )
        match = _SUCCESS_JSON_RE.search(cleaned)
        if not match:
            raise ValueError("no JSON object in response")
        parsed = json.loads(match.group(0))
        headline = str(parsed.get("headline") or "").strip() or final_summary
        notable_moment = parsed.get("notable_moment")
        notable_moment = str(notable_moment).strip() if notable_moment else None
        return {"headline": headline, "notable_moment": notable_moment}
    except Exception:
        return {"headline": final_summary, "notable_moment": None}


_DIAGNOSIS_PROMPT_TEMPLATE = """\
A task has been submitted, but before building/changing anything, restate \
your own understanding of what's being asked and what you plan to do -- \
this will be shown to a human for confirmation BEFORE any code is written \
or anything is installed.

Goal: {goal}
Inputs (if any concrete targets were named): {inputs}
Deliverables (if specified): {deliverables}

Respond with ONLY a JSON object of the exact shape: {{"understanding": \
"one or two sentences restating what you understand is being asked, in \
your own words", "plan": "one or two sentences naming concretely what \
you intend to build/change/investigate to satisfy this", "open_questions": \
"any real ambiguity worth flagging before proceeding, OR null if the \
request is clear enough to proceed as understood -- never invent one \
just to seem thorough"}}"""

_DIAGNOSIS_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


async def summarize_diagnosis_for_operator(
    goal: str,
    inputs: list[str],
    deliverables: list[str],
    client: ModelGatewayClient,
    model: str,
    task_id: str | None = None,
) -> dict:
    """Phase 22 (2026-07-23): the "diagnose" half of the opt-in
    diagnose-then-confirm pause (manager/loop.py, pause_if=
    ["diagnose_first"]) -- runs BEFORE round 1, no specialist delegated
    yet, no round history to summarize (unlike the two sibling
    functions above, which run AFTER real work happened). Same "never
    fabricate, degrade honestly" discipline: any generation failure
    falls back to a plain, honest restatement built from the real
    contract fields rather than a crash or a placeholder.
    """
    try:
        prompt = _DIAGNOSIS_PROMPT_TEMPLATE.format(
            goal=goal, inputs=inputs or "(none named)", deliverables=deliverables or "(none specified)",
        )
        cleaned = await generate_checked(
            client, model, [{"role": "user", "content": prompt}],
            max_tokens=600, temperature=0.1,
            task_id=task_id, actor="manager", call_label="Diagnosing the request before proposing a plan",
        )
        match = _DIAGNOSIS_JSON_RE.search(cleaned)
        if not match:
            raise ValueError("no JSON object in response")
        parsed = json.loads(match.group(0))
        understanding = str(parsed.get("understanding") or "").strip() or goal
        plan = str(parsed.get("plan") or "").strip() or "Build/change what the goal describes."
        open_questions = parsed.get("open_questions")
        open_questions = str(open_questions).strip() if open_questions else None
        return {"understanding": understanding, "plan": plan, "open_questions": open_questions}
    except Exception:
        return {
            "understanding": goal,
            "plan": "Build/change what the goal describes.",
            "open_questions": None,
        }


def list_rounds_for_task(task_id: str) -> list[dict]:
    """Phase 15 (§19.8): for the chat UI's round-trace view -- every
    replan_round event for a given task, in order, so Operator can watch
    the Manager's thinking evolve across attempts rather than seeing
    only a final pass/fail.

    Phase 31 UI (2026-08-08): real, confirmed gap -- this used to return ONLY replan_round rows,
    which are written exclusively when a round FAILS and gets revised. A task cleanly executing
    its first round (the common, good case) had zero rows here, so the UI's own
    buildGraphForTask() (which reads rounds[i].detail.new_contract.constraint_nodes) never had
    anything to render the real graph from until/unless something failed -- confirmed live,
    the project owner's own report: a genuinely in-progress, still-on-round-1 task showed no graph at all.
    Now also reads the one-time `graph_created` event (manager/loop.py, written immediately
    after the real decomposition graph is built, before round 1 of any node starts) and
    synthesizes a round-shaped entry from it -- SAME `detail.new_contract.constraint_nodes` /
    `detail.new_contract.planning_round_budget` shape a real replan_round row has, so
    buildGraphForTask() needs no changes at all to consume it. Only included when no real
    replan_round exists yet (a real revision, once one happens, is the more current, authoritative
    structure and completely supersedes this synthetic one) -- ordered first (oldest), exactly
    where the real graph-creation moment actually happened.
    """
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, created_at, summary, detail
            FROM agent_memory_events
            WHERE event_type = 'replan_round' AND task_id = %s
            ORDER BY id ASC
            """,
            (task_id,),
        )
        rounds = [dict(row) for row in cur.fetchall()]
        if not rounds:
            cur.execute(
                """
                SELECT id, created_at, summary, detail
                FROM agent_memory_events
                WHERE event_type = 'graph_created' AND task_id = %s
                ORDER BY id ASC
                LIMIT 1
                """,
                (task_id,),
            )
            graph_row = cur.fetchone()
            if graph_row:
                rounds = [{
                    "id": graph_row["id"],
                    "created_at": graph_row["created_at"],
                    "summary": graph_row["summary"],
                    "detail": {
                        "new_contract": {
                            "constraint_nodes": graph_row["detail"].get("constraint_nodes", {}),
                            "planning_round_budget": graph_row["detail"].get("planning_round_budget"),
                        },
                        # Phase 31 UI (2026-08-08): the full real split lineage (see
                        # manager/loop.py's own graph_created write) -- a sibling of new_contract,
                        # not per-node, since it's the WHOLE task's own recursion structure.
                        "split_lineage": graph_row["detail"].get("split_lineage", {}),
                    },
                }]
        # Real fix, 2026-08-08 (the project owner's direct report: "I cannot open round one, round two...
        # it should be available always for all these rounds, for past rounds"). `graph_created`
        # is the one row `manager/tools.py`'s `_update_node_field_live()`/`_update_node_round_
        # field_live()`/`persist_node_round_step()` ALWAYS dual-write, unconditionally, for the
        # task's entire life -- authoritative for round_steps/round_diffs/round_findings/
        # round_checks/round_number/state/active_specialist at every instant. A `replan_round`
        # row, by contrast, is a frozen point-in-time snapshot: `sync_live_node_telemetry_into_
        # new_snapshot()` (manager/tools.py) only copies graph_created's CURRENT telemetry onto
        # it once, at the exact moment it's created -- any later dual-write still lands on
        # graph_created (and on whichever `replan_round` row is `MAX(id)` at THAT later moment),
        # so a row that gets superseded by a newer one before some in-flight step/diff/finding
        # write lands never receives it. Confirmed live on a real task (451f102c-bc38-48ef-a8ce-
        # fc9b58be7466): the newest `replan_round` row's own `milestone_model.round_steps` was
        # `{}` while `graph_created`'s was `{"1": [...], "2": [...]}` for the same label, at the
        # same instant. Rather than trying to close every write-time race (unbounded -- any
        # future field added to this telemetry set would need the same fix again), overlay
        # graph_created's current values onto the LAST (most recent) round entry here, on READ,
        # every single call -- this is exactly the code path buildGraphForTask() walks newest-
        # first, so the round it actually renders from is always current regardless of write
        # timing. Never touches anything but this returned dict; no execution-path behavior
        # changes, same "display data only" scope as every other function this incident touched.
        if rounds:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT detail -> 'constraint_nodes' AS constraint_nodes,
                       detail -> 'split_lineage' AS split_lineage
                FROM agent_memory_events
                WHERE task_id = %s AND event_type = 'graph_created'
                ORDER BY id DESC LIMIT 1
                """,
                (task_id,),
            )
            graph_row = cur.fetchone()
            live_nodes = graph_row["constraint_nodes"] if graph_row and graph_row["constraint_nodes"] else None
            # Real fix, 2026-08-08 (same live investigation, task 4af6d7bc-5200-4209-9f26-
            # 9183450826e7 -- the project owner's own direct report of a graph showing only a flat "STEP 1"
            # for a task that genuinely DID recurse further): split_lineage is computed ONCE, at
            # decomposition time, and written only onto `graph_created` -- it is a whole-task
            # recursion map, a SIBLING of constraint_nodes, never itself a per-round or per-node
            # field. But it was never carried onto any real `replan_round` row at all (only the
            # synthetic graph_created-shaped entry above ever set it, and that branch is skipped
            # the moment a real replan_round row exists) -- so any task with more than one round
            # silently lost its own real split-lineage from this point on, and the frontend
            # (index.html's `_addRootAndSplitLineage()`) fell back to treating every node as an
            # original top-level piece. Confirmed live: `/api/rounds/4af6d7bc...` was already
            # returning `split_lineage: null` despite Postgres genuinely holding two real split
            # edges on that same task's own `graph_created` row. Overlaid here, onto the same
            # last-round entry buildGraphForTask() actually renders from, same as every other
            # field this function already keeps current.
            if graph_row and graph_row["split_lineage"]:
                rounds[-1].setdefault("detail", {})["split_lineage"] = graph_row["split_lineage"]
            if live_nodes:
                last_cn = rounds[-1].get("detail", {}).get("new_contract", {}).get("constraint_nodes") or {}
                for label, node in last_cn.items():
                    live = live_nodes.get(label)
                    if not live:
                        continue
                    for field_name in (
                        "state", "active_specialist", "round_number",
                        "round_diffs", "round_findings", "round_checks", "round_steps", "round_timings",
                    ):
                        if field_name in live and live[field_name] is not None:
                            node[field_name] = live[field_name]
        return rounds
    finally:
        conn.close()


def list_branch_messages(task_id: str) -> list[dict]:
    """Phase 17 (§21.7): every real branch_message for this task_id, in
    order -- the durable log for a branch's own mini-chat, deliberately
    separate from the technical round-by-round trace (list_rounds_for_task
    above), so the UI can render "what was actually said" and "what
    actually technically happened" as two coherent, individually
    legible views of the same branch.
    """
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, created_at, detail
            FROM agent_memory_events
            WHERE event_type = 'branch_message' AND task_id = %s
            ORDER BY id ASC
            """,
            (task_id,),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_latest_checkpoint(task_id: str) -> dict | None:
    """Phase 17 (§21.5.4): the most recent round_checkpoint row for this
    task_id -- the durable source of truth Continue reads from. None if
    this task never escalated (nothing to resume).
    """
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, created_at, detail
            FROM agent_memory_events
            WHERE event_type = 'round_checkpoint' AND task_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_resume_point(task_id: str) -> dict | None:
    """Phase 30, P2 (§9): a wider version of get_latest_checkpoint() for
    startup-orphan resume, not just the tier-3/4 Continue button.

    Real, confirmed gap in this priority's own plan text, found by
    checking `get_latest_checkpoint()`'s own docstring against the
    actual orphan scenario before building anything: round_checkpoint is
    only ever written from the `except PauseForOperator` handler in
    `_execute_contract()` -- a task killed mid-round by an external
    restart (the entire Problem F scenario) was, by definition, still
    actively working and had NOT yet reached that escalation point.
    Confirmed against the real historical data: of 258 real orphaned
    tasks, only 132 (51%) have a round_checkpoint at all; 170 (66%) have
    at least one replan_round (which also carries a real commit_sha and
    contract, just under different key names: `new_contract` instead of
    `last_contract`, no `prior_rounds`/`human_summary`); 88 (34%) have
    neither -- genuinely nothing durable to resume from, since they were
    still on their very first, never-yet-revised round when killed.

    Returns a normalized shape regardless of source: `{"contract":
    <dict>, "commit_sha": <str|None>, "module_for_repeat_check":
    <str|None>, "human_summary": <str>, "prior_rounds": <list>, "source":
    "round_checkpoint"|"replan_round"}` -- or None if truly nothing
    exists for this task_id (caller must fall back to a clean restart).
    """
    checkpoint = get_latest_checkpoint(task_id)
    if checkpoint is not None:
        detail = checkpoint["detail"]
        return {
            "contract": detail["last_contract"],
            "commit_sha": (detail.get("prior_rounds") or [{}])[-1].get("commit_sha"),
            "module_for_repeat_check": detail.get("module_for_repeat_check"),
            "human_summary": detail.get("human_summary", ""),
            "prior_rounds": detail.get("prior_rounds", []),
            "source": "round_checkpoint",
        }
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, module, detail
            FROM agent_memory_events
            WHERE event_type = 'replan_round' AND task_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        detail = row["detail"]
        vr = detail.get("verification_result") or {}
        return {
            "contract": detail["new_contract"],
            "commit_sha": detail.get("commit_sha"),
            "module_for_repeat_check": row["module"],
            "human_summary": (
                f"Orphaned mid-round {detail.get('round_number')}: {vr.get('notes', '')}"
            ),
            "prior_rounds": [],
            "source": "replan_round",
        }
    finally:
        conn.close()


def get_original_capability_class(task_id: str) -> str | None:
    """Real, confirmed bug found live: Continue rebuilds its resumed
    contract from the checkpoint's own last_contract -- whatever
    contract the escalating round itself happened to be running with.
    If that round was mid-bounce through a code_review-primary detour
    when it escalated, last_contract.capability_class is readonly_
    investigation, not the task's real, original shape -- and Continue
    would then inherit and PERPETUATE that corruption for the entire
    new round budget, since nothing else in the resumed run has any
    other reference point to correct it against. The task_created
    event's own capability_class is written once, at Phase 3
    classification, and is NEVER touched by any mid-loop specialist
    routing -- the one reliable source of "what this task actually is."
    """
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT detail FROM agent_memory_events WHERE event_type = 'task_created' AND task_id = %s "
            "ORDER BY id ASC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row["detail"].get("capability_class")
    finally:
        conn.close()


def list_clarifications_since(task_id: str, since_event_id: int) -> list[str]:
    """Phase 17 (§21.5.3/§21.5.4): every real branch_message of
    kind='clarification' Operator wrote after the given checkpoint row's own
    id, in order -- the full, verbatim set of everything he said while
    the task sat waiting. Never summarized, per §21.5.4's explicit
    exemption.
    """
    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT detail
            FROM agent_memory_events
            WHERE event_type = 'branch_message' AND task_id = %s AND id > %s
              AND detail->>'kind' = 'clarification'
            ORDER BY id ASC
            """,
            (task_id, since_event_id),
        )
        return [row["detail"]["text"] for row in cur.fetchall()]
    finally:
        conn.close()
