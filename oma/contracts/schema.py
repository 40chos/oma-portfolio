"""TaskContract / VerificationResult / SpecialistType / AutonomyTier /
CapabilityClass / CompensatingAction -- the shared data language every
specialist and the Manager both speak.

Nothing here is specific to any one specialist's internal logic -- adding a
new specialist means writing a new package and registering it, never
touching this shared contract.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator

# The three states a single, independently-tracked constraint (a short,
# snake_case label the Manager derives once, up front, when a contract is
# first built -- e.g. "scheduling_fields") can be in across a task's round
# history. Named here, once, so TaskContract.constraint_status and every
# renderer/consumer of it share the exact same literal values rather than
# each redeclaring their own strings.
ConstraintState = Literal["pending", "satisfied", "failing"]


class SpecialistType(str, Enum):
    bug_fix = "bug_fix"              # the Build specialist
    code_review = "code_review"
    testing_qa = "testing_qa"


class CapabilityClass(str, Enum):
    # Set by the Manager alongside `tier` -- decides which tools the
    # specialist is actually handed, a separate question from whether the
    # task is allowed to proceed at all.
    data_change = "data_change"           # the existing Odoo data tool layer
    module_development = "module_dev"     # needs the scaffold/lint/install toolchain
    readonly_investigation = "readonly"   # codebase read access only, no deployment capability


class AutonomyTier(int, Enum):
    # Mirrors MANAGER_CONSTITUTION.md directly.
    tier_1_readonly = 1       # proceed autonomously, just log it
    tier_2_notify_after = 2   # proceed, notify Operator after the fact
    tier_3_pause_before = 3   # sensitive_paths hit -- pause for sign-off
    tier_4_presign_off = 4    # schema/migration/permissions -- sign-off required before even delegating


class CompensatingAction(BaseModel):
    forward_step: str
    undo_action: str          # required for any real write step


# A resolved, concrete, correct instruction appended to contract.rules can get
# buried among many accumulated entries from a long multi-round/multi-resume
# history -- a flat, undifferentiated list gives an LLM no signal about which
# entry actually matters most right now. A plain string prefix (rather than a
# schema change) keeps every existing call site that builds a TaskContract
# untouched; only the specific injection points that need to guarantee
# attention use it, and every specialist's own prompt-building code is
# responsible for rendering CRITICAL_RULE_PREFIX-marked rules in their own
# prominent section, separate from and ahead of ordinary accumulated history.
CRITICAL_RULE_PREFIX = "CRITICAL FIX REQUIRED: "


def split_critical_rules(rules: list[str]) -> tuple[list[str], list[str]]:
    """(critical, ordinary) -- critical rules keep the prefix stripped off
    (the specialist's own prompt renders its own heading instead), ordinary
    rules are returned in their original order, unmodified.
    """
    critical = [r[len(CRITICAL_RULE_PREFIX):] for r in rules if r.startswith(CRITICAL_RULE_PREFIX)]
    ordinary = [r for r in rules if not r.startswith(CRITICAL_RULE_PREFIX)]
    return critical, ordinary


class RoundFeedback(BaseModel):
    """A structured, typed record of one round's worth of cross-round/
    cross-role feedback -- an alternative to feedback flattened into
    anonymous prose and re-mined by a word-overlap heuristic.

    Deliberately additive new infrastructure, not a replacement of
    `TaskContract.rules: list[str]` yet -- that field is read and appended to
    across dozens of call sites (prompt rendering, recurrence detection,
    critical-rule accumulation), and migrating all of them to structured
    records is real, separate, higher-risk follow-up work, not something to
    fold into introducing the type itself. This is the same "schema-ready
    ahead of the logic that populates it" pattern already used for
    `VerificationResult.regressed_constraints`/`gates_disagree` below --
    structured comparison (`same_underlying_finding_structured()` below) is
    usable today for any new code that wants it, without requiring `rules`
    itself to change shape first.
    """

    source: Literal["build", "code_review", "testing_qa", "manager"]
    severity: Literal["critical", "info"]
    text: str
    round_number: int
    # Optional structured classification (e.g. a short label like
    # 'missing_field_x' or 'security_csv_empty') -- when both records carry
    # one, comparison is exact and deterministic instead of falling back to
    # the word-overlap heuristic. None is the honest default: not every
    # caller can cheaply produce a real classification yet.
    finding_class: str | None = None


def same_underlying_finding_structured(a: RoundFeedback, b: RoundFeedback) -> bool:
    """Structured sibling of `manager.replanning._same_underlying_finding()`'s
    word-overlap heuristic -- prefers an exact, deterministic comparison over
    fuzzy text matching whenever both records carry a real `finding_class`
    (never a coincidental overlap of common review vocabulary, unlike the
    word-overlap heuristic it complements). Falls back to the existing
    word-overlap heuristic on `.text` only when at least one side lacks a
    `finding_class` -- lives here, not duplicated, to avoid two
    independently-drifting definitions of "same finding." Two records from
    different `source`s are never considered the same underlying finding -- a
    Build note and a Code-Review finding happening to use similar words is a
    coincidence, not a real recurrence signal.
    """
    if a.source != b.source:
        return False
    if a.finding_class is not None and b.finding_class is not None:
        return a.finding_class == b.finding_class
    from manager.replanning import _same_underlying_finding
    return _same_underlying_finding(a.text, b.text)


def dedupe_round_feedback(entries: list[RoundFeedback]) -> list[RoundFeedback]:
    """Caps/dedupes near-identical CRITICAL entries, keeping only the most
    recent reconsider-note, since it already supersedes earlier ones. Only
    ever collapses `severity == 'critical'` entries (an 'info' entry has no
    such supersession semantics) -- for each group of critical entries judged
    the same underlying finding via `same_underlying_finding_structured()`,
    keeps only the one with the highest `round_number` (ties broken by
    original list position, last one wins, matching "most recent"). Every
    'info' entry, and every critical entry with no real duplicate, passes
    through unchanged. Preserves original relative order of whatever survives.
    """
    # Indices into `entries` (not a filtered sub-list), so the final
    # reconstruction never depends on model equality/membership checks --
    # safe even when two genuinely distinct entries happen to have identical
    # field values. Each critical index is assigned to exactly one cluster
    # (built once, no re-clustering as `remaining` shrinks), then only the
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
    tier: AutonomyTier                     # set by the Manager's check_sensitive_paths()
                                            # call before this contract is even created
    goal: str
    inputs: list[str]
    rules: list[str]
    deliverables: list[str]
    compensating_actions: list[CompensatingAction]
    validation_by: str | None               # who verifies this -- never "itself".
                                            # None is a deliberate, narrow exception: a
                                            # readonly_investigation task (Code-Review's
                                            # own audit) has no separate "build" step for
                                            # a second specialist to independently verify --
                                            # Code-Review's own claims_complete IS the
                                            # terminal signal for that shape. Every other
                                            # shape still requires a real, different
                                            # specialist's name here.
    pause_if: list[str]
    turn_budget: int
    retry_sub_budget: int = 3              # separate from turn_budget

    # All additive, all defaulted, so every existing call site that builds a
    # TaskContract keeps working unchanged.
    planning_round_budget: int = 5
    # How many reflect-and-retry rounds this task may go through before the
    # Manager must stop and either move on (if not blocking anything) or
    # escalate to the operator. Deliberately generous -- this system
    # optimizes for a correct result over a fast one, on purpose. A round is
    # one full delegate -> verify -> (maybe revise) cycle, never a
    # specialist's own internal turn_budget.
    round_wall_clock_cap_seconds: int = 2700
    # A real ceiling alongside the round count (45 minutes) -- "take your
    # time" is not "never stop." Hit before planning_round_budget rounds are
    # exhausted: treated exactly like exhausting the round budget.
    plan_id: str | None = None
    plan_item_id: str | None = None
    blocked_by: list[str] = []
    # plan_item_id values this task's plan_id depends on. Empty list means
    # runnable immediately. All three default to None/None/[] -- a
    # standalone TaskContract not part of any multi-item plan at all.

    # Increments each time Continue is used to resume this exact task_id
    # after a round-budget escalation, so a task continued 3 times shows
    # that honestly in the UI/memory, never disguised as "still on round 2."
    resumed_from_checkpoint_count: int = 0

    # Generalizes the CRITICAL_RULE_PREFIX mechanism (above) from "recurring
    # Code-Review findings" to every independent constraint the task's own
    # goal establishes. Keyed by a short, snake_case label the Manager
    # derives once, up front, when this contract is first built, e.g.
    # "scheduling_fields" / "double_booking_rule" / "product_scoping_relation".
    # Deliberately a plain status dict, never prose -- there is nothing
    # narrative here for a summarizer to lossily compress, which is exactly
    # why this field is never passed through any round-history summarization
    # call. It is rendered in full, every round, at a fixed position in
    # Build's own prompt, as a dedicated, lost-in-the-middle mitigation.
    constraint_status: dict[str, ConstraintState] = {}

    # `constraint_status` (above) is a dict -- real, typed, but its key order
    # is not safe to rely on once round-tripped through a Postgres `jsonb`
    # checkpoint column (jsonb does not guarantee object-key order; jsonb
    # does guarantee array/list order, which is exactly why this is a
    # `list[str]`, not another dict). Set once, up front, when a decomposed
    # task's sub-contracts are first built, and carried forward unchanged by
    # every subsequent `model_copy()` across rounds and resumes -- never
    # re-derived from prose, never re-ordered. Without this field, a
    # checkpoint written with constraint_status keys in decomposition order
    # can come back from Postgres in a different order, which can make a
    # resumed task's "which constraint is next" calculation land on the
    # wrong one.
    constraint_order: list[str] = []

    # The task's own original, un-narrowed goal text -- set once (mirrors
    # constraint_order's own discipline) the first time a decomposed task's
    # sub-contracts are built, then carried forward unchanged. Exists so a
    # resume path can recover the base goal from an already-narrowed
    # sub-contract's `goal` field without string-matching against the exact
    # wording of the narrowing sentence. None for any task that was never
    # decomposed (contract.goal IS already the only goal text there is).
    original_goal: str | None = None

    # The current round's own single constraint focus label, and the ordered
    # list of constraint labels this round's goal explicitly defers ("NOT yet
    # in scope"). Both are set as real, structured fields at the exact same
    # point the equivalent sentences are rendered into `goal` for the LLM's
    # own benefit -- the prose rendering and the structured field are
    # produced together, from the same source data, so they can never drift
    # apart. Exists so downstream consumers (Testing/QA's own
    # decomposition-awareness checks) can read a real field instead of
    # regex-matching the rendered prose sentence back out of `goal`.
    current_constraint_label: str | None = None
    remaining_constraint_labels: list[str] = []

    # Cache for the one, schema-guided extraction call
    # `contracts.goal_facts.extract_goal_field_facts()` performs against this
    # contract's own `goal` -- populated once, by the Manager, before round
    # 1, and carried forward unchanged by every later `model_copy()` (a
    # revised round only ever appends to `rules`, never touches `goal`, so
    # the extracted facts stay valid for every round of the same contract).
    # Plain dict, not the typed form directly, matching every other
    # cache-shaped field on this contract. Empty dict means "no extraction
    # ran yet, or it found nothing" -- every consumer treats that identically
    # to "fall back to the existing regex family," never a special case.
    goal_facts: dict = {}

    # The one authoritative "which real Odoo model is this task touching"
    # identity -- computed once, by the Manager, before round 1, following
    # the same "populated once, cached, carried forward unchanged" discipline
    # goal_facts above already established. None means "could not be
    # determined" (e.g. a goal that doesn't state a model and no explicit
    # hint was given either) -- every consumer treats that identically to "no
    # identity available," never guesses.
    module_identity: str | None = None

    # An explicit statement of why this task is likely to go wrong, drawn
    # from prior-failure memory rows matching the same module. A property of
    # the goal/system (computed once by the Manager at contract-build time
    # from already-fetched memory rows), never re-derived per round -- same
    # discipline as goal_facts/module_identity above. None means "no matching
    # prior-failure history found" -- every consumer treats that identically
    # to "no hint available," never invents one. Carried forward unchanged by
    # every later model_copy(), including every resume path.
    known_risk_hint: str | None = None

    # Node-addressable state, additive alongside constraint_status/
    # constraint_order above, never replacing them. Empty dict means "no
    # node state built yet" (e.g. a task never decomposed, or built before
    # this field existed) -- every consumer falls back to the existing
    # constraint_status/constraint_order path unchanged, the same
    # "populated once, cached, carried forward" discipline as above.
    constraint_nodes: dict[str, "ConstraintNode"] = {}

    # Each node dispatch mints its own fresh correlation_id (a UUIDv4
    # string) -- never reused from the parent task, never reused across a
    # retry of the same label. None on the base/parent contract itself (a
    # fresh one is only ever minted per sub-contract dispatch).
    correlation_id: str | None = None
    # The parent task's own correlation_id (or, if that's unset, its
    # task_id) -- lets a trace consumer walk from any one node's own
    # correlation_id back to the task it belongs to, without correlation_id
    # itself ever doubling as a task identifier.
    parent_correlation_id: str | None = None

    # A per-node isolated snapshot of the module's prior-round file state,
    # frozen at dispatch time -- None (the default) preserves ordinary
    # behavior byte-for-byte (the Build specialist's is_retry branch does
    # its normal live read). Set only by the concurrent-write-safety
    # mechanism's own per-node dispatch wiring.
    frozen_old_files_by_relpath: dict[str, str] | None = None

    # The literal, unwrapped text of the immediately-preceding round's own
    # failure evidence (verification notes, which often already contain a
    # real install log excerpt, plus any Code-Review finding, concatenated
    # verbatim) -- a sibling field to `rules`, deliberately not folded into
    # it, because `rules` entries are wrapped in explanatory prose and become
    # subject to a rule-count cap once enough rounds accumulate. This field
    # always holds only the most recent round's raw text (overwritten, never
    # appended to) and is rendered into its own protected prompt block,
    # exempt from any rule-list cap. None means "first round, nothing to show
    # yet" -- every consumer treats that as "no previous-attempt-errors
    # block to render."
    previous_round_raw_failure_text: str | None = None

    # Additive alongside `rules` (free prose, unchanged, still the rendering
    # source for Build's prompt) -- the same evidence, in machine-consumable
    # form. Populated once per round, at the exact same point rules is
    # appended to -- one function, two writes, never two independently-
    # drifting passes. Empty list means "no structured failure evidence
    # recorded yet" -- every consumer falls back to reading `rules` prose
    # unchanged, exactly like every other "populated once, cached" field on
    # this contract.
    failure_records: list["FailureRecord"] = []

    # One entry per goal-derived business-logic decision the classifier had
    # to make an assumption about. Populated once, at the same contract-build
    # call site as constraint_nodes above, gated by a deterministic keyword
    # pre-check so most ordinary tasks never populate this at all (empty
    # list, same "populated once, cached, carried forward" discipline as
    # every other field here). Testing/QA reads this to run one behavioral
    # probe per entry, verifying the interpretation is correct, not just
    # that the underlying field/method exists.
    interpreted_business_rules: list[str] = []

    # Set True the one time this task's round-budget-exhaustion escalation
    # is overridden to grant a single, structurally-different-shaped extra
    # retry round for a root_cause="one_off" failure. Caps the recovery
    # attempt at exactly one per task -- once True, the override never fires
    # again for this contract, and the loop escalates normally on the next
    # round_budget hit.
    one_off_recovery_used: bool = False


class FailureCategory(str, Enum):
    """A machine-consumable classification for a round's own failure,
    additive alongside (never replacing) TaskContract.rules' free-prose
    accumulation -- see FailureRecord.
    """

    scope_violation = "scope_violation"
    hallucinated_finding = "hallucinated_finding"
    install_state_mismatch = "install_state_mismatch"
    missing_required_field = "missing_required_field"
    xmlid_mismatch = "xmlid_mismatch"
    regression = "regression"
    reproduction_gap = "reproduction_gap"
    business_rule_mismatch = "business_rule_mismatch"
    unclassified = "unclassified"


class FailureRecord(BaseModel):
    """Populated once by the Judge role at the exact moment it already has
    the VerificationResult in hand -- a structured ("location", "observed",
    "concrete_alternative") shape, additive alongside TaskContract.rules'
    free prose, never a replacement.
    """

    round_number: int
    constraint_label: str | None = None
    failure_category: FailureCategory = FailureCategory.unclassified
    location: str = ""
    observed: str = ""
    concrete_alternative: str = ""
    source: Literal["judge", "code_review", "testing_qa"] = "judge"


# ConstraintNodeState is deliberately a separate Enum family from
# ConstraintState (above), not a Literal extension of it, even though 3 of
# its 7 values share names with ConstraintState's 3 values. ConstraintState
# models only the 3 terminal/near-terminal states a round's own outcome can
# be in; ConstraintNodeState additionally models graph-scheduling states
# (ready/running/blocked) that have no meaning for a sequentially-executed,
# non-node-aware contract and would be a category error to add to
# ConstraintState itself. The two are read by different consumers at
# different layers and are not meant to be unified.
class ConstraintNodeState(str, Enum):
    pending = "pending"
    ready = "ready"
    running = "running"
    satisfied = "satisfied"
    failing = "failing"
    paused = "paused"
    blocked = "blocked"


def _flatten_round_timing_bucket(round_value, _prefix: str = "") -> dict:
    """round_timings is a pure UI/display timing convenience -- nothing
    outside serialization ever reads it for a correctness decision -- so it's
    safe to be lenient here: recursively descend through any depth of
    dict-of-dicts wrapping (round-trip migrations have accumulated extra
    nesting levels over time), and once a level is reached whose values are
    genuinely numbers (a real {actor: seconds} leaf), keep it as one bucket,
    tagged with the full key-path used to reach it (joined with '.') so
    distinct buckets from different accumulated nesting never collide or
    silently overwrite each other. Tolerates arbitrary future drift depth the
    same way, not just the shapes seen so far.
    """
    if not isinstance(round_value, dict):
        return {}
    if not round_value:
        return {}
    if all(not isinstance(v, dict) for v in round_value.values()):
        # A genuine leaf: either empty, or every value is already a plain
        # number (the real {actor: seconds} shape) -- return as-is, tagged
        # under _prefix if we descended to get here (an accumulated-nesting
        # artifact), or bare "0" if this is a still-flat legacy shape found
        # at the very top (no descent needed at all).
        return {(_prefix or "0"): round_value}
    flattened: dict = {}
    for key, inner in round_value.items():
        sub_prefix = f"{_prefix}.{key}" if _prefix else key
        if isinstance(inner, dict):
            flattened.update(_flatten_round_timing_bucket(inner, sub_prefix))
        else:
            # A bare number sitting alongside dict siblings at the same level
            # (a real, valid {resume_index: {actor: seconds}} entry mixed in
            # with other buckets) -- keep it under its own real resume_index
            # key, untouched.
            flattened.setdefault(_prefix or "0", {})[key] = inner
    return flattened


class ConstraintNode(BaseModel):
    """One node in the additive, artifact-overlap-derived dependency graph
    laid on top of a tier-bucket-only constraint ordering. Every list field
    defaults to empty -- never guessed -- following a "never invent a value
    that isn't concretely present in the text" discipline. `tier` is
    persisted once at decomposition time and is purely a display/fallback-
    ordering value, not the primary source of `predecessor_labels`.
    """

    label: str
    creates: list[str] = []
    requires: list[str] = []
    predecessor_labels: list[str] = []
    tier: int = 0
    state: ConstraintNodeState = ConstraintNodeState.pending
    round_number: int = 0
    # Live-patched directly into the JSONB snapshot by a dedicated update
    # path, and set back onto a real ConstraintNode object when re-hydrating
    # a fresh snapshot -- declared here (rather than special-cased in the
    # sync path) so Pydantic accepts the field the same way every other
    # live-patched field on this class already does.
    active_specialist: str | None = None
    failure_records: list[FailureRecord] = []
    # Display-only lineage marker -- the immediate parent label this node was
    # produced by splitting, when a constraint gets recursively decomposed
    # (None for a node that was never split further, including every
    # original top-level piece). Never read by predecessor_labels derivation
    # or the scheduler -- purely so the UI can draw actual recursive
    # splitting (a piece visibly branching into its own sub-pieces) instead
    # of a flat list of same-size siblings.
    split_from: str | None = None

    # Full historical diffs/findings/checks preserved for every past round,
    # not just whatever was live-streaming at the moment someone happened to
    # be watching. This data used to be SSE-only -- never durably stored --
    # so a past round's own Build/Code-Review/Testing-QA detail could only
    # ever say "not captured." Keyed by round_number (as a string, matching
    # how a dict key round-trips through JSONB) so each past round keeps its
    # own real content permanently, distinct from every other round's.
    # Additive durable storage alongside the existing live SSE path, not a
    # replacement for it.
    round_diffs: dict[str, list[dict]] = {}
    round_findings: dict[str, list[dict]] = {}
    round_checks: dict[str, list[dict]] = {}
    # The granular, real-time, step-by-step narrative a reviewer actually
    # wants to watch -- "Check this. Change that." -- appearing progressively
    # as each specialist actually works, not a single end-of-round summary.
    # Same round_number-keyed-as-string shape as the three fields above, but
    # each value is a growing list (append-only) of real {actor, message,
    # status} steps, in the exact order they happened -- reusing the same
    # trace-event messages already published live, not new prose invented
    # for this.
    round_steps: dict[str, list[dict]] = {}
    # Keyed by round_number (as a string, same convention as the three
    # fields above), each entry a real {actor: seconds} map -- actor is one
    # of "build", "review", "qa" (wall-clock time that specialist's own call
    # took this round, timed at its exact call site), "manager" (the
    # orchestrator's own overhead this round -- decomposition/routing/
    # replanning decisions, computed as round_total minus every specialist's
    # own measured time, never a guess), and "round_total" (wall-clock from
    # this round's own first trace event to its last, so the operator can
    # see the full round's real duration even before Build/Review/QA/manager
    # are individually summed).
    #
    # Nested one level deeper than a bare {actor: seconds} map: this field's
    # real persisted shape is `round_timings.{round_number}.{resume_index}.{actor}`,
    # supporting a task resumed multiple times without one resume's timing
    # overwriting another's. Historical rows written before that nesting
    # existed are a flat legacy shape instead; `_normalize_round_timings`
    # below migrates both shapes to the same validated form at load time, so
    # a genuine mix of old and new shapes across different nodes/rounds
    # always validates correctly.
    round_timings: dict[str, dict[str, dict[str, float]]] = {}

    @field_validator("round_timings", mode="before")
    @classmethod
    def _normalize_round_timings(cls, value):
        if not isinstance(value, dict):
            return value
        return {round_key: _flatten_round_timing_bucket(round_value) for round_key, round_value in value.items()}


TaskContract.model_rebuild()


class SpecialistOutput(BaseModel):
    """What a specialist's run() actually returns -- a report, not a
    VerificationResult. A specialist reports what it believes it did; turning
    that (plus a deterministic spot-check, plus -- for a Build specialist's
    work -- the Testing/QA specialist's own independent SpecialistOutput)
    into an actual VerificationResult is the Manager's job, never the
    specialist's own.
    """
    task_id: UUID
    specialist_type: SpecialistType
    summary: str
    detail: dict
    claims_complete: bool      # the specialist's own claim -- never trusted
                                # alone, exactly the thing VerificationResult
                                # exists to check
    artifacts: list[str] = []  # e.g. module names, file paths touched


class VerificationResult(BaseModel):
    task_id: UUID
    passed: bool
    reproduction_confirmed: bool
    uncovered_paths: list[str]
    coverage_diff: str
    spot_check_mismatch: bool              # result of the deterministic diff-vs-coverage
                                            # check, never just the specialist's own report
    root_cause: str | None = None          # set only when passed=False --
                                            # 'one_off' / 'skill_gap' /
                                            # 'pattern_worth_a_rule' / 'unclear'
    notes: str

    # Which previously-`satisfied` constraint_status entries (by their short
    # label) this round's change flipped back to failing. Empty by default:
    # no round has ever regressed anything until real detection logic says
    # otherwise.
    regressed_constraints: list[str] = []

    # Set specifically when Testing/QA's own independent verification was
    # strongly corroborated (reproduction_confirmed=True,
    # spot_check_mismatch=False) yet a Code-Review blocking finding still
    # overrides `passed` to False -- a genuine two-gate disagreement, not an
    # ordinary, uncontested blocking finding (the much more common case:
    # Testing/QA's own pass was weak/uncorroborated to begin with).
    # Distinguishes "no confidence weighting" ambiguity: without this flag,
    # both cases would look identical downstream. Never set True when
    # `passed` is True (an override that didn't happen has nothing to
    # disagree about). False by default -- the vast majority of overrides
    # are not this case.
    gates_disagree: bool = False


class ReplanRound(BaseModel):
    """One instance per round, every round, logged -- this is what makes the
    Manager's thinking legible across attempts rather than a black box that
    either passes or fails once.
    """
    round_number: int
    previous_contract_task_id: str
    verification_result: VerificationResult
    code_review_finding_summary: str | None = None
    # Populated whenever Code-Review was consulted this round -- None if
    # this round's task shape doesn't call for it.
    revision_reasoning: str
    # A plain-language explanation of *why* the new contract differs from the
    # old one -- not just "retrying," the actual specific thing that changed
    # and why, generated at revision time, never left for a human to
    # reverse-engineer from a diff.
    new_contract: TaskContract

    # The real Gitea commit SHA this round's validated output was committed
    # as. None for a round that never reached that point -- a
    # validator-rejected round, or a round whose specialist_type doesn't
    # produce committable module files at all.
    commit_sha: str | None = None
    # How many candidates Build actually generated this round under
    # verifier-guided best-of-N sampling. Default 1 -- the ordinary,
    # single-candidate case.
    candidate_count: int = 1
    # Real LLM call count and wall-clock duration for this round, covering
    # every specialist and Manager call this round made. None only for a
    # round predating this field (an old checkpoint) -- every round logged
    # from here on always populates a real int/float, even 0 for a round
    # that made no calls.
    llm_call_count: int | None = None
    llm_duration_sec: float | None = None
