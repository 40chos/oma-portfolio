"""TaskContract / VerificationResult / SpecialistType / AutonomyTier /
CapabilityClass / CompensatingAction -- the shared data language every
specialist and the Manager both speak. Pulled directly from the build
plan's §8 (Phase 5, step 1), verbatim, not re-derived.

Nothing here is specific to any one specialist's internal logic --
that's the whole point of this phase (§0.3's "stay flexible"
requirement: adding specialist four/five means writing a new package
and registering it, never touching this shared contract).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator

# Phase 18 (§22.5): the three states a single, independently-tracked
# constraint (a short, snake_case label the Manager derives once, up
# front, when a contract is first built -- e.g. "scheduling_fields") can
# be in across a task's own round history. Named here, once, so
# TaskContract.constraint_status and every renderer/consumer of it
# (specialists/build/specialist.py's render_constraint_ledger(),
# manager/replanning.py's future regression re-check) share the exact
# same literal values rather than each redeclaring their own strings.
ConstraintState = Literal["pending", "satisfied", "failing"]


class SpecialistType(str, Enum):
    bug_fix = "bug_fix"              # the Build specialist
    code_review = "code_review"
    testing_qa = "testing_qa"
    # migration = "migration"              # not registered yet — §18
    # gui_productionization = "gui_productionization"  # not registered yet — §18


class CapabilityClass(str, Enum):
    # Set by the Manager alongside `tier` — decides which tools the
    # specialist is actually handed, a separate question from whether
    # the task is allowed to proceed at all.
    data_change = "data_change"           # existing Odoo data tool layer
                                           # (Phase 7) is the whole toolset
    module_development = "module_dev"     # needs the scaffold/lint/install
                                           # toolchain (Phase 8)
    readonly_investigation = "readonly"   # codebase read access only —
                                           # no deployment capability at all


class AutonomyTier(int, Enum):
    # Mirrors MANAGER_CONSTITUTION.md (§0.5.3) directly.
    tier_1_readonly = 1       # proceed autonomously, just log it
    tier_2_notify_after = 2   # proceed, notify Operator after the fact
    tier_3_pause_before = 3   # sensitive_paths hit — pause for sign-off
    tier_4_presign_off = 4    # schema/migration/permissions — sign-off
                              # required before even delegating


class CompensatingAction(BaseModel):
    forward_step: str
    undo_action: str          # required for any real write step


# Real, confirmed gap found live: a resolved, concrete, correct
# instruction (e.g. "the model X is defined by module Y, add Y to
# depends") got silently appended to contract.rules and buried among
# 16 other accumulated entries from a long multi-round/multi-resume
# history -- Build never acted on it, not because it was wrong, but
# because a flat, undifferentiated list gives an LLM no signal about
# which entry actually matters most right now. A plain string prefix
# (rather than a schema change) keeps every existing call site that
# builds a TaskContract untouched; only the specific injection points
# that need to guarantee attention use it, and every specialist's own
# prompt-building code is the one place responsible for rendering
# CRITICAL_RULE_PREFIX-marked rules in their own prominent section,
# separate from and ahead of ordinary accumulated history.
CRITICAL_RULE_PREFIX = "CRITICAL FIX REQUIRED: "


def split_critical_rules(rules: list[str]) -> tuple[list[str], list[str]]:
    """(critical, ordinary) -- critical rules keep the prefix stripped
    off (the specialist's own prompt renders its own heading instead),
    ordinary rules are returned in their original order, unmodified.
    """
    critical = [r[len(CRITICAL_RULE_PREFIX):] for r in rules if r.startswith(CRITICAL_RULE_PREFIX)]
    ordinary = [r for r in rules if not r.startswith(CRITICAL_RULE_PREFIX)]
    return critical, ordinary


class RoundFeedback(BaseModel):
    """P12 Tier S/A item 10 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    §4): a structured, typed record of one round's worth of cross-round/cross-role feedback --
    resolves part of B Finding 9 (feedback flattened to anonymous prose immediately, then
    re-mined by `_same_underlying_finding()`'s own word-overlap heuristic, with its own
    documented near-miss-fix incident trail: see that function's docstring in
    `manager/replanning.py`) and lays the foundation for A Finding 5 (uncapped CRITICAL rules
    accumulating full round-history text with no dedup/decay).

    Deliberately ADDITIVE new infrastructure, not a replacement of `TaskContract.rules:
    list[str]` yet -- that field is read and appended to across dozens of call sites
    throughout `manager/replanning.py`/`manager/loop.py` (prompt rendering, recurrence
    detection, critical-rule accumulation), and migrating all of them to structured records is
    real, separate, higher-risk follow-up work, not something to fold into introducing the
    type itself. This is the same "schema-ready ahead of the logic that populates it" pattern
    already used for `VerificationResult.regressed_constraints`/`gates_disagree` above --
    structured comparison (`same_underlying_finding_structured()` below) is real and usable
    today for any NEW code that wants it, without requiring `rules` itself to change shape
    first.
    """

    source: Literal["build", "code_review", "testing_qa", "manager"]
    severity: Literal["critical", "info"]
    text: str
    round_number: int
    # Optional structured classification (e.g. a short label like 'missing_field_x' or
    # 'security_csv_empty') -- when both records carry one, comparison is exact and
    # deterministic instead of falling back to the word-overlap heuristic. None is the
    # honest default: not every caller can cheaply produce a real classification yet.
    finding_class: str | None = None


def same_underlying_finding_structured(a: RoundFeedback, b: RoundFeedback) -> bool:
    """Structured sibling of `manager.replanning._same_underlying_finding()`'s word-overlap
    heuristic -- prefers an exact, deterministic comparison over fuzzy text matching whenever
    both records carry a real `finding_class` (never a coincidental overlap of common review
    vocabulary, unlike the word-overlap heuristic it complements). Falls back to the existing
    word-overlap heuristic on `.text` only when at least one side lacks a `finding_class` --
    lives here, not duplicated, to avoid two independently-drifting definitions of "same
    finding." Two records from different `source`s are never considered the same underlying
    finding -- a Build note and a Code-Review finding happening to use similar words is a
    coincidence, not a real recurrence signal.
    """
    if a.source != b.source:
        return False
    if a.finding_class is not None and b.finding_class is not None:
        return a.finding_class == b.finding_class
    from manager.replanning import _same_underlying_finding
    return _same_underlying_finding(a.text, b.text)


def dedupe_round_feedback(entries: list[RoundFeedback]) -> list[RoundFeedback]:
    """Part of item 10's shape: "cap/dedupe near-identical CRITICAL entries, keep only the
    most recent reconsider-note, since it already supersedes earlier ones." Only ever
    collapses `severity == 'critical'` entries (an 'info' entry has no such supersession
    semantics) -- for each group of critical entries judged the same underlying finding via
    `same_underlying_finding_structured()`, keeps only the one with the highest
    `round_number` (ties broken by original list position, last one wins, matching "most
    recent"). Every 'info' entry, and every critical entry with no real duplicate, passes
    through unchanged. Preserves original relative order of whatever survives.
    """
    # Indices into `entries` (not a filtered sub-list), so the final reconstruction never
    # depends on model equality/membership checks -- safe even when two genuinely distinct
    # entries happen to have identical field values. Each critical index is assigned to
    # exactly one cluster (built once, no re-clustering as `remaining` shrinks), then only the
    # most-recent member of each cluster survives.
    remaining = [i for i, e in enumerate(entries) if e.severity == "critical"]
    drop: set[int] = set()
    while remaining:
        i = remaining.pop(0)
        cluster = [i] + [j for j in remaining if same_underlying_finding_structured(entries[i], entries[j])]
        remaining = [j for j in remaining if j not in cluster]
        if len(cluster) > 1:
            most_recent = max(cluster, key=lambda j: (entries[j].round_number, j))
            drop |= {j for j in cluster if j != most_recent}

    return [e for i, e in enumerate(entries) if i not in drop]


class TaskContract(BaseModel):
    task_id: UUID
    specialist_type: SpecialistType
    capability_class: CapabilityClass
    tier: AutonomyTier                     # set by the Manager's
                                            # check_sensitive_paths() call
                                            # (Phase 4) before this contract
                                            # is even created
    goal: str
    inputs: list[str]
    rules: list[str]
    deliverables: list[str]
    compensating_actions: list[CompensatingAction]
    validation_by: str | None               # who verifies this — never "itself".
                                            # None is a deliberate, narrow exception
                                            # (Phase 12): a readonly_investigation
                                            # task (Code-Review's own audit) has no
                                            # separate "build" step for a second
                                            # specialist to independently verify —
                                            # Code-Review's own claims_complete IS
                                            # the terminal signal for that shape.
                                            # Every other shape still requires a
                                            # real, different specialist's name here.
    pause_if: list[str]
    turn_budget: int
    retry_sub_budget: int = 3              # separate from turn_budget

    # Phase 15 (§19) additions -- all additive, all defaulted, so every
    # existing call site that builds a TaskContract (Phases 6-13) keeps
    # working completely unchanged.
    planning_round_budget: int = 5
    # How many reflect-and-retry rounds this task may go through before
    # the Manager must stop and either move on (if not blocking anything)
    # or call ask_operator(). Deliberately generous, per §19's own stated
    # philosophy: this system optimizes for a correct result over a fast
    # one, on purpose. A round is one full delegate -> verify -> (maybe
    # revise) cycle, never a specialist's own internal turn_budget.
    round_wall_clock_cap_seconds: int = 2700
    # A real ceiling alongside the round count (45 minutes) -- "take your
    # time" is not "never stop." Hit before planning_round_budget rounds
    # are exhausted: treated exactly like exhausting the round budget.
    plan_id: str | None = None
    plan_item_id: str | None = None
    blocked_by: list[str] = []
    # plan_item_id values this task's plan_id depends on. Empty list
    # means runnable immediately. All three default to None/None/[] --
    # a standalone TaskContract not part of any multi-item plan at all,
    # exactly like every contract built in Phases 6-13.

    # Phase 17 (§21.5.4) addition -- additive, defaulted, same discipline
    # as the Phase 15 fields above. Increments each time Continue is used
    # to resume this exact task_id after a round-budget escalation, so a
    # task continued 3 times shows that honestly in the UI/memory, never
    # disguised as "still on round 2."
    resumed_from_checkpoint_count: int = 0

    # Phase 18 (§22.5) addition -- generalizes the CRITICAL_RULE_PREFIX
    # fix (above) from "recurring Code-Review findings" to EVERY
    # independent constraint the task's own goal establishes. Keyed by a
    # short, snake_case label the Manager derives once, up front, when
    # this contract is first built (manager.replanning.derive_constraint_labels()),
    # e.g. "scheduling_fields" / "double_booking_rule" /
    # "product_scoping_relation". Deliberately a plain status dict, never
    # prose -- there is nothing narrative here for a summarizer to
    # lossily compress, which is exactly why this field is NEVER passed
    # through compress_round_history_mid_loop() or any other
    # summarization call (confirmed: that function's own signature takes
    # only goal/prior_rounds/existing_summary, never a TaskContract, so
    # this field structurally cannot reach it) -- it is rendered in full,
    # every round, at a fixed position in Build's own prompt
    # (specialists/build/specialist.py's render_constraint_ledger()),
    # per §22.8's dual-position, lost-in-the-middle mitigation.
    constraint_status: dict[str, ConstraintState] = {}

    # Phase 25A addition (2026-07-25): the missing piece that made
    # manager/loop.py's own `_reconstruct_resume_order()` regex-parse
    # constraint order back out of `goal` prose in the first place.
    # `constraint_status` (above) is a dict -- real, typed, but its KEY
    # ORDER is not safe to rely on once round-tripped through a
    # Postgres `jsonb` checkpoint column (jsonb does not guarantee
    # object-key order; jsonb DOES guarantee array/list order, which is
    # exactly why this is a `list[str]`, not another dict). Set ONCE,
    # up front, when a decomposed task's sub-contracts are first built
    # (manager/loop.py's `_run_decomposed_task()`), and carried forward
    # unchanged by every subsequent `model_copy()` across rounds and
    # resumes -- never re-derived from prose, never re-ordered.
    #
    # Real, confirmed incident this closes (documented in manager/
    # loop.py's own `_reconstruct_resume_order()` history, 2026-07-12):
    # a checkpoint written with constraint_status keys in decomposition
    # order came back from Postgres in a completely different order,
    # which made a resumed task's own "which constraint is next"
    # calculation land on the wrong one -- a task was marked
    # `completed` after only 3 of 8 real constraints had actually run.
    constraint_order: list[str] = []

    # The task's own ORIGINAL, un-narrowed goal text -- set once
    # (mirrors constraint_order's own discipline) the first time a
    # decomposed task's sub-contracts are built, then carried forward
    # unchanged. Exists specifically to replace the previous
    # `new_contract.goal.split("\n\nThis round's own NEW focus is
    # ONLY:")[0]` string-splitting hack manager/loop.py's resume path
    # used to recover the base goal from an already-narrowed
    # sub-contract's `goal` field -- a fragile pattern coupled to the
    # exact wording of the narrowing sentence, now unnecessary since
    # the original text is simply preserved as its own field. None for
    # any task that was never decomposed (contract.goal IS already the
    # only goal text there is).
    original_goal: str | None = None

    # The current round's own single constraint focus label, and the
    # ordered list of constraint labels this round's goal explicitly
    # defers ("NOT yet in scope"). Both are set as real, structured
    # fields by `_run_constraint_labels_from()` at the exact same point
    # it renders the equivalent sentences into `goal` for the LLM's own
    # benefit -- the prose rendering and the structured field are
    # produced together, from the same source data, so they can never
    # drift apart. Exists so downstream consumers (Testing/QA's own
    # decomposition-awareness checks) can read a real field instead of
    # regex-matching the rendered prose sentence back out of `goal`.
    current_constraint_label: str | None = None
    remaining_constraint_labels: list[str] = []

    # Phase 25B addition (2026-07-25): cache for the ONE, schema-guided
    # extraction call `contracts.goal_facts.extract_goal_field_facts()`
    # performs against this contract's own `goal` -- populated once, by
    # the Manager, before round 1 (manager/loop.py's `_execute_contract()`),
    # and carried forward unchanged by every later `model_copy()` (a
    # revised round only ever appends to `rules`, never touches `goal`,
    # so the extracted facts stay valid for every round of the SAME
    # contract). Plain dict, not `GoalFieldFacts` directly, matching
    # every other cache-shaped field on this contract (e.g.
    # `constraint_status`) -- consumers call `GoalFieldFacts.model_
    # validate(contract.goal_facts)` when they need the typed form.
    # Empty dict means "no extraction ran yet, or it found nothing" --
    # every consumer treats that identically to "fall back to the
    # existing regex family," never a special case.
    goal_facts: dict = {}

    # Phase 26B addition (2026-07-27): the ONE authoritative "which real
    # Odoo model is this task touching" identity -- replaces two
    # independent, drifted derivations (manager/loop.py's own optional
    # anticipated_scope hint, confirmed almost never populated by the
    # real chat UI; manager/replanning.py's own _module_identifier(), a
    # 4-word regex slug of goal prose that could never exact-string-
    # match the real outcome-row `module` values check_repeated_failures
    # actually queries against). Computed ONCE, by the Manager, via
    # contracts.module_identity.resolve_module_identity(), before round
    # 1 (manager/loop.py's own contract-construction step) -- exactly
    # the same "populated once, cached, carried forward unchanged by
    # every later model_copy()" discipline goal_facts above already
    # established. None means "could not be determined" (e.g. a goal
    # that doesn't state a Model: line and no explicit hint was given
    # either) -- every consumer (fencing-lock derivation, check_
    # repeated_failures, outcome-row module=) treats that identically
    # to "no identity available," never guesses.
    module_identity: str | None = None

    # P13 item 6 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    # §22.2): the one genuinely new task-spec-enrichment element -- an explicit statement of why
    # THIS task is likely to go wrong, drawn from real prior-failure memory rows matching this same
    # module. A property of the goal/system (computed once by the Manager at contract-build time
    # from already-fetched memory_rows, manager/memory.py's derive_known_risk_hint()), never
    # re-derived per round -- same "fresh-vs-static" discipline as goal_facts/module_identity
    # above. None means "no matching prior-failure history found" -- every consumer treats that
    # identically to "no hint available," never invents one. Carried forward unchanged by every
    # later model_copy() (including every resume path), exactly like goal_facts/module_identity.
    known_risk_hint: str | None = None

    # P13 item 4 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    # §22.2 item 4): node-addressable state, additive alongside constraint_status/constraint_order
    # above, never replacing them. Empty dict means "no node state built yet" (e.g. a task never
    # decomposed, or built before this field existed) -- every consumer falls back to the existing
    # constraint_status/constraint_order/_dependency_tier_for_constraint_label() path unchanged,
    # exactly the same "populated once, cached, carried forward" discipline goal_facts/module_identity
    # above already established.
    constraint_nodes: dict[str, "ConstraintNode"] = {}

    # Phase 31 §10 (telemetry): each node dispatch (manager/loop.py's own
    # _run_constraint_labels_from()) mints its OWN fresh correlation_id (a UUIDv4 string) --
    # never reused from the parent task, never reused across a retry of the same label. None on
    # the base/parent contract itself (a fresh one is only ever minted per SUB-contract dispatch)
    # -- every existing call site that builds a TaskContract keeps working completely unchanged.
    correlation_id: str | None = None
    # The parent task's own correlation_id (or, if that's unset, its task_id) -- lets a trace
    # consumer walk from any one node's own correlation_id back to the task it belongs to,
    # without correlation_id itself ever doubling as a task identifier.
    parent_correlation_id: str | None = None

    # Phase 31 §6 (Phase A.5): a per-node ISOLATED snapshot of the module's prior-round file
    # state, frozen at dispatch time -- None (the default) preserves today's exact behavior
    # byte-for-byte (specialists/build/specialist.py's own is_retry branch does its normal live
    # read). Set only by the concurrent-write-safety mechanism's own per-node dispatch wiring.
    frozen_old_files_by_relpath: dict[str, str] | None = None

    # Phase 30 §26 item 3 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
    # 2026-07-29.md): the LITERAL, unwrapped text of the immediately-preceding round's own
    # failure evidence (verification_result.notes -- which itself often already contains a real
    # install log_tail excerpt -- plus any Code-Review finding, concatenated verbatim) -- a
    # sibling field to `rules`, deliberately NOT folded into it, because `rules` entries are
    # wrapped in explanatory prose and become subject to _MAX_ORDINARY_RULES truncation once
    # enough rounds accumulate. This field always holds only the MOST RECENT round's raw text
    # (overwritten, never appended to) and is rendered into its own protected prompt block,
    # exempt from any rule-list cap. None means "first round, nothing to show yet" -- every
    # consumer treats that as "no previous-attempt-errors block to render."
    previous_round_raw_failure_text: str | None = None

    # P13 item 7: additive alongside `rules` (free prose, unchanged, still the rendering source
    # for Build's prompt) -- the same evidence, in machine-consumable form. Populated once per
    # round by `manager/replanning.py`'s `revise_contract_from_verification()`, at the exact same
    # point it already appends to `rules` -- one function, two writes, never two independently-
    # drifting passes. Empty list means "no structured failure evidence recorded yet" (e.g. a
    # contract on its first round, or built before this field existed) -- every consumer falls
    # back to reading `rules` prose unchanged, exactly like every other "populated once, cached"
    # field on this contract.
    failure_records: list["FailureRecord"] = []

    # P13 item 12a: one entry per goal-derived business-logic decision the classifier had to make
    # an assumption about (contracts/business_rules.py's own extract_interpreted_business_rules()).
    # Populated once, at the same contract-build call site as constraint_nodes above, gated by a
    # deterministic keyword pre-check so most ordinary tasks never populate this at all (empty
    # list, same "populated once, cached, carried forward" discipline as every other field here).
    # Testing/QA reads this to run one behavioral probe per entry, verifying the INTERPRETATION is
    # correct, not just that the underlying field/method exists.
    interpreted_business_rules: list[str] = []

    # P13 item 12b: set True the one time this task's round-budget-exhaustion escalation is
    # overridden to grant a single, structurally-different-shaped extra retry round for a
    # root_cause="one_off" failure (manager/loop.py's own round loop, right after
    # should_escalate_to_operator() returns "round_budget"). Caps the recovery attempt at exactly one
    # per task -- once True, the override never fires again for this contract, and the loop
    # escalates normally on the next round_budget hit.
    one_off_recovery_used: bool = False


class FailureCategory(str, Enum):
    """P13 item 7: a machine-consumable classification for a round's own failure, additive
    alongside (never replacing) TaskContract.rules' free-prose accumulation -- see FailureRecord.
    """

    scope_violation = "scope_violation"
    hallucinated_finding = "hallucinated_finding"
    install_state_mismatch = "install_state_mismatch"
    missing_required_field = "missing_required_field"
    xmlid_mismatch = "xmlid_mismatch"
    regression = "regression"
    reproduction_gap = "reproduction_gap"
    # Added 2026-07-31 integration-coherence pass: closes a real gap -- without this value, a
    # failed interpreted_business_rules probe (item 12a) has no home in this enum and would fall
    # into "unclassified," silently defeating item 12a's own point.
    business_rule_mismatch = "business_rule_mismatch"
    unclassified = "unclassified"


class FailureRecord(BaseModel):
    """P13 item 7: populated once by the Judge role at the exact moment it already has the
    VerificationResult in hand (today's revise_contract_from_verification() call site) -- the
    same three-field shape ("location", "observed", "concrete_alternative") the arXiv:2607.14167
    ablation attributes most of its 42-44pp repair-quality gain to (the "admissible alternatives"
    field specifically). Additive alongside TaskContract.rules' free prose, never a replacement --
    format (prose vs. structured) doesn't move repair quality per that same source, content does.
    """

    round_number: int
    constraint_label: str | None = None
    failure_category: FailureCategory = FailureCategory.unclassified
    location: str = ""
    observed: str = ""
    concrete_alternative: str = ""
    source: Literal["judge", "code_review", "testing_qa"] = "judge"


# P13 item 4: ConstraintNodeState is deliberately a SEPARATE Enum family from ConstraintState
# (above), not a Literal extension of it, even though 3 of its 7 values share names with
# ConstraintState's 3 values. Reason (per the Stage 3 critique's own required correction):
# ConstraintState models only the 3 terminal/near-terminal states a round's OWN outcome can be in;
# ConstraintNodeState additionally models graph-scheduling states (ready/running/blocked) that have
# no meaning for a sequentially-executed, non-node-aware contract and would be a category error to
# add to ConstraintState itself. The two are read by different consumers at different layers
# (ConstraintState: render_constraint_ledger()'s prose rendering, unchanged; ConstraintNodeState:
# the mechanical Orchestrator's own scheduling logic, item 2) and are not meant to be unified.
class ConstraintNodeState(str, Enum):
    pending = "pending"
    ready = "ready"
    running = "running"
    satisfied = "satisfied"
    failing = "failing"
    paused = "paused"
    blocked = "blocked"


def _flatten_round_timing_bucket(round_value, _prefix: str = "") -> dict:
    """Real, confirmed follow-up bug (2026-08-09, same live task, immediately after fixing
    round_timings for the first (single-extra-level) drift shape): some rows accumulated
    MULTIPLE extra nesting levels over time (observed live: `round_timings.1.0.68` -- FOUR real
    levels, `{"0": {"68": {actor: seconds}}}`), not just the one flat-to-nested migration the
    original fix handled. round_timings is a pure UI/display timing convenience -- confirmed via
    a full-codebase read that nothing outside serialization ever reads it for a correctness
    decision -- so it's safe to be lenient here: recursively descend through ANY depth of
    dict-of-dicts wrapping, and once a level is reached whose values are genuinely numbers (a
    real {actor: seconds} leaf), keep it as one bucket, tagged with the full key-path used to
    reach it (joined with '.') so distinct buckets from different accumulated nesting never
    collide or silently overwrite each other. Tolerates arbitrary future drift depth the same
    way, not just the two shapes seen so far.
    """
    if not isinstance(round_value, dict):
        return {}
    if not round_value:
        return {}
    if all(not isinstance(v, dict) for v in round_value.values()):
        # A genuine leaf: either empty, or every value is already a plain number (the real
        # {actor: seconds} shape) -- return as-is, tagged under _prefix if we descended to get
        # here (an accumulated-nesting artifact), or bare "0" if this is the still-flat legacy
        # shape found at the very top (no descent needed at all).
        return {(_prefix or "0"): round_value}
    flattened: dict = {}
    for key, inner in round_value.items():
        sub_prefix = f"{_prefix}.{key}" if _prefix else key
        if isinstance(inner, dict):
            flattened.update(_flatten_round_timing_bucket(inner, sub_prefix))
        else:
            # A bare number sitting alongside dict siblings at the same level (a real, valid
            # {resume_index: {actor: seconds}} entry mixed in with other buckets) -- keep it
            # under its own real resume_index key, untouched.
            flattened.setdefault(_prefix or "0", {})[key] = inner
    return flattened


class ConstraintNode(BaseModel):
    """P13 item 4: one node in the additive, artifact-overlap-derived dependency graph laid on
    top of today's tier-bucket-only constraint ordering. Every list field defaults to empty --
    never guessed -- mirroring contracts/goal_facts.py's already-shipped "never invent a value
    that isn't concretely present in the text" discipline. `tier` is persisted once at
    decomposition time and is now purely a display/fallback-ordering value, not the primary
    source of `predecessor_labels` (see the module-level note on `constraint_nodes` above).
    """

    label: str
    creates: list[str] = []
    requires: list[str] = []
    predecessor_labels: list[str] = []
    tier: int = 0
    state: ConstraintNodeState = ConstraintNodeState.pending
    round_number: int = 0
    # Real bug found live (2026-08-08, task 657697fc's flagship run): manager/tools.py's
    # _update_node_field_live() has always raw-SQL-patched this key straight into the JSONB
    # snapshot (never through this model), and sync_live_node_telemetry_into_new_snapshot()
    # setattr()s it back onto a real ConstraintNode object when re-hydrating a fresh
    # replan_round's own snapshot -- but Pydantic rejects setattr() on any undeclared field, so
    # every such sync silently aborted (caught by that function's own broad try/except) the
    # instant it reached this field for ANY node, leaving every node after it in the same loop
    # un-synced too. Declaring it here (rather than skipping it in the sync loop) matches what
    # every other live-patched field on this class already does.
    active_specialist: str | None = None
    failure_records: list[FailureRecord] = []
    # Phase 31 UI (2026-08-08): real, display-only lineage marker -- the immediate parent label
    # this node was produced by splitting, when manager.replanning.recursively_decompose_
    # constraints() recursed on it (None for a node that was never split further, including
    # every original top-level piece). NEVER read by predecessor_labels derivation or the
    # scheduler -- purely so the UI can draw actual recursive splitting (a piece visibly
    # branching into its own sub-pieces) instead of a flat list of same-size siblings. See
    # manager/replanning.py's _ConstraintArtifacts.split_from for the full real incident this
    # closes (the project owner's own report: "no visual sense of... this piece was itself split further").
    split_from: str | None = None

    # Real UX addition, 2026-08-08 (the project owner's own explicit request: full historical diffs/
    # findings/checks preserved for every past round, not just whatever was live-streaming at
    # the moment someone happened to be watching). Previously this data (specialists/build/
    # specialist.py's real diff, manager/ui_serialize.py's real structured findings/checks) was
    # SSE-only -- never durably stored -- so a past round's own Build/Code-Review/Testing-QA tab
    # could only ever say "not captured." Keyed by round_number (as a string, matching how a
    # dict key round-trips through JSONB) so each past round keeps its OWN real content
    # permanently, distinct from every other round's. Populated in manager/loop.py at the same
    # real call sites that already publish this data live over SSE -- this is additive
    # durable storage alongside that existing live path, not a replacement for it.
    round_diffs: dict[str, list[dict]] = {}
    round_findings: dict[str, list[dict]] = {}
    round_checks: dict[str, list[dict]] = {}
    # Real UX addition, 2026-08-08 (the project owner's own explicit request: the granular, real-time,
    # step-by-step narrative the old pre-Phase-31 UI showed -- "Check this. Change that." --
    # appearing progressively as each specialist actually works, not a single end-of-round
    # summary). Same round_number-keyed-as-string shape as the three fields above, but each
    # value is a growing LIST (append-only) of real {actor, message, status} steps, in the
    # exact order they really happened -- reusing manager/loop.py's own already-real, already-
    # published `publish_trace_event()` messages (e.g. "Reviewing the diff…", the real
    # `verification.notes` text) as the source, not new prose invented for this.
    round_steps: dict[str, list[dict]] = {}
    # Real UX addition, 2026-08-08 (the project owner's own explicit request: "add time tracker and counter
    # for every round for every builder code reviewer tester orchestrator all processes ...
    # everything all time"). Keyed by round_number (as a string, same convention as the three
    # fields above), each entry a real {actor: seconds} map -- actor is one of "build",
    # "review", "qa" (real wall-clock time that specialist's own call took this round, timed at
    # its exact real call site in manager/loop.py), "manager" (the orchestrator's own real
    # overhead this round -- decomposition/routing/replanning decisions, computed as round_total
    # minus every specialist's own measured time, never a guess), and "round_total" (real
    # wall-clock from this round's own first trace event to its last, so Operator can see the full
    # round's real duration even before Build/Review/QA/manager are individually summed).
    # Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run): this type
    # annotation was never updated when manager.tools.persist_node_round_timing() added a THIRD
    # nesting level the same day ("timer for different resumes, it's staying the same... it
    # should be separate sync, not the same timer for everything" -- see that function's own
    # docstring) -- `round_timings.{round_number}.{resume_index}.{actor}`, not
    # `round_timings.{round_number}.{actor}`. The stale 2-level annotation made every real
    # persisted row (an inner dict keyed by resume_index, itself mapping to {actor: seconds})
    # fail Pydantic validation as "not a float," which broke loading/resuming ANY task with
    # round-timing data recorded after that fix shipped -- confirmed live via the exact
    # `ValidationError` this task's own resume hit.
    #
    # Real follow-up bug found immediately after fixing the type above (same live resume,
    # 2026-08-09): manager.tools.persist_node_round_timing()'s own `_migrate()` only upgrades a
    # round's legacy flat `{actor: seconds}` shape to the new `{resume_index: {actor: seconds}}`
    # shape the NEXT time that exact round is written to -- it never touches rows for OTHER
    # rounds/nodes that simply haven't been written to since the resume_index nesting shipped.
    # Real production data is therefore a genuine MIX of both shapes across different
    # nodes/rounds, not a one-time migration cutover -- confirmed live via this task's own
    # `ticket_access_rights` node still holding old flat `{"build": 136.4, "round_total":
    # 442.3, ...}` rows for earlier rounds while `ticket_list_view`/`equipment_registry` already
    # had the new nested shape. `_normalize_round_timings` mirrors that SAME `_migrate()` logic
    # here at load time, so both shapes validate correctly regardless of which rounds happen to
    # have been touched by a write since the nesting fix landed.
    round_timings: dict[str, dict[str, dict[str, float]]] = {}

    @field_validator("round_timings", mode="before")
    @classmethod
    def _normalize_round_timings(cls, value):
        if not isinstance(value, dict):
            return value
        return {round_key: _flatten_round_timing_bucket(round_value) for round_key, round_value in value.items()}


TaskContract.model_rebuild()


class SpecialistOutput(BaseModel):
    """What a specialist's run() actually returns -- a report, NOT a
    VerificationResult. Per the technical document's own insistence:
    "never the specialist's own word, and never only the QA specialist's
    self-report either." A specialist reports what it believes it did;
    turning that (plus a deterministic spot-check, plus -- for a Build
    specialist's work -- the Testing/QA specialist's own independent
    SpecialistOutput) into an actual VerificationResult is the Manager's
    job (Phase 6), never the specialist's own.

    Named in the build plan's §8 step 2 as the alternative return shape
    for run() when a specialist needs to hand back an intermediate
    result before verification -- its fields aren't given verbatim
    there, so this is authored here, kept deliberately minimal.
    """
    task_id: UUID
    specialist_type: SpecialistType
    summary: str
    detail: dict
    claims_complete: bool      # the specialist's OWN claim -- never
                                # trusted alone, exactly the thing
                                # VerificationResult exists to check
    artifacts: list[str] = []  # e.g. module names, file paths touched


class VerificationResult(BaseModel):
    task_id: UUID
    passed: bool
    reproduction_confirmed: bool
    uncovered_paths: list[str]
    coverage_diff: str
    spot_check_mismatch: bool              # result of the deterministic
                                            # diff-vs-coverage check
                                            # (Phase 11), never just the
                                            # specialist's own report
    root_cause: str | None = None          # set only when passed=False —
                                            # 'one_off' / 'skill_gap' /
                                            # 'pattern_worth_a_rule' /
                                            # 'unclear' (§2.7)
    notes: str

    # Phase 18 (§22.10/§22.13) addition: which previously-`satisfied`
    # constraint_status entries (by their short label) this round's
    # change flipped back to failing. Schema-ready now, ahead of the
    # regression-re-check logic that actually populates it (§22.10,
    # deliberately out of scope for this task) -- so nothing downstream
    # breaks once that logic lands. Empty by default: no round has ever
    # regressed anything until real detection logic says otherwise.
    regressed_constraints: list[str] = []

    # P12 Tier S/A item 7 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    # §4): set by `manager.tools.fold_code_review_findings()` specifically when Testing/QA's
    # own independent verification was STRONGLY corroborated (reproduction_confirmed=True,
    # spot_check_mismatch=False) yet a Code-Review blocking finding still overrides `passed`
    # to False -- a genuine two-gate disagreement, not an ordinary, uncontested blocking
    # finding (the much more common case: Testing/QA's own pass was weak/uncorroborated to
    # begin with). Distinguishes B Finding 4's "no confidence weighting" gap: previously both
    # cases looked identical downstream. Never set True when `passed` is True (an override
    # that didn't happen has nothing to disagree about). False by default -- the vast
    # majority of overrides are NOT this case.
    gates_disagree: bool = False


class ReplanRound(BaseModel):
    """Phase 15 (§19.3): one instance per round, every round, logged --
    this is what makes the Manager's thinking legible across attempts
    rather than a black box that either passes or fails once.
    """
    round_number: int
    previous_contract_task_id: str
    verification_result: VerificationResult
    code_review_finding_summary: str | None = None
    # Populated whenever Code-Review was consulted this round (§19.5) --
    # None if this round's task shape doesn't call for it.
    revision_reasoning: str
    # A plain-language explanation of *why* the new contract differs from
    # the old one -- not just "retrying," the actual specific thing that
    # changed and why, generated by revise_contract_from_verification()
    # itself, never left for a human to reverse-engineer from a diff.
    new_contract: TaskContract

    # Phase 18 (§22.4/§22.13) addition: the real Gitea commit SHA this
    # round's validated output was committed as (tools_odoo/module_dev/vcs.py's
    # commit_validated_round()). None for a round that never reached that
    # point -- a validator-rejected round, or a round whose specialist_type
    # doesn't produce committable module files at all -- same conservative
    # posture as build_output.detail already uses for module_name.
    commit_sha: str | None = None
    # Phase 18 (§22.7/§22.13) addition: how many candidates Build actually
    # generated this round under verifier-guided best-of-N sampling
    # (specialists/build/specialist.py). Default 1 -- the ordinary,
    # single-candidate case unchanged from every phase before this one.
    candidate_count: int = 1
    # Phase 30, P1d (Phase L, §15) addition: real LLM call count and
    # wall-clock duration for THIS round, from infra.gateway_client.
    # ModelGatewayClient.pop_call_stats() -- covers every specialist and
    # Manager call this round made (generate() is the whole system's own
    # single choke point). None only for a round predating this field
    # (an old checkpoint) -- every round logged from here on always
    # populates a real int/float, even 0 for a round that made no calls.
    llm_call_count: int | None = None
    llm_duration_sec: float | None = None
