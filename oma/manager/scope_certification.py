"""Phase 32 implementation (2026-08-11): the scope-certification / "bake-in"
mechanism from docs/planning/PHASE32_RELIABILITY_ROOT_CAUSE_AND_ROLLOUT_STRATEGY_2026-08-11.md
sections 2 and 3.2 -- the actual core policy the whole document exists to
support: "ready to hand to Operator" means a SCOPE (not the whole system) has
racked up N consecutive clean runs, with the count resetting to zero on any
qualifying failure, and the certification itself expiring after a fixed
window rather than being permanent.

Deliberately mechanical and narrow, matching this session's own repeated
lesson (Bugs 1-91): every over-clever, semantic version of a check in this
pipeline eventually got dodged by a rephrasing or an edge case. This module
does the simplest thing that is still honest:
  - classify_scope(): a small, explicit keyword taxonomy (starting scopes
    from Phase 32 section 3.2) -- NOT an LLM call, NOT fuzzy; an
    unrecognized goal shape classifies as "uncategorized" rather than being
    force-fit into an existing bucket.
  - record_task_outcome(): append one outcome per scope; a qualifying
    failure resets the consecutive-clean-run streak to zero and, per
    section 2's escalation policy, doubles the required N for that scope
    (capped at 3 escalations total) before falling back to a visible
    backlog state instead of ever looping silently.
  - is_scope_certified(): true only if the current streak meets the
    scope's current threshold AND the certification hasn't expired
    (default 30 days per section 3.2, itself explicitly marked a judgment
    call, not a derived constant).

Persisted as one JSON file (state/scope_certification.json) -- no database
dependency, so this can be adopted immediately without any infra change.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import asdict, dataclass, field

_STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "state")
_STATE_PATH = os.path.join(_STATE_DIR, "scope_certification.json")

# Starting taxonomy, verbatim from Phase 32 section 3.2. Order matters: more
# specific scopes are checked before broader ones so a goal matching several
# keyword sets lands in the most specific bucket, not the first alphabetical one.
_SCOPE_KEYWORD_PATTERNS: list[tuple[str, re.Pattern]] = [
    # 2026-08-15: the first of the 18 directions from
    # docs/planning/PHASE35_DIRECTION_CERTIFICATION_ROADMAP_2026-08-12.md's own 25-direction
    # priority table (§2.0) that had a real-world-importance ranking (Tier 1, row 7, N=16
    # historical, 56% pass) but no classify_scope() bucket at all -- goals matching this shape
    # were silently falling into "uncategorized" with no dedicated tracking. Checked before the
    # broader edit_existing_module_access_or_files bucket below, matching this file's own
    # "more specific first" ordering convention (a record-rule goal is a real, narrower sub-case
    # of "editing an existing module's access," not the generic shape that bucket exists for).
    ("record_rule_row_level_security", re.compile(
        r"\brecord rule\b|\brow[- ]level security\b|\bir\.rule\b", re.IGNORECASE)),
    # 2026-08-15: second and third of the 25-direction roadmap's not-yet-implemented buckets
    # (Tier 0d/0e -- validation constraint is literally one step of the roadmap's own "extend
    # Contacts: add a field, show it, validate it, secure it" canonical example; a related field
    # is Odoo Studio's own "add fields to existing models" category applied to a simple, common
    # variant). Both already have real, mature pre-write validators in specialists/build/
    # specialist.py (`_validate_api_constrains_fields_exist` for the former), just no dedicated
    # tracking bucket -- goals matching these shapes were falling into "uncategorized". Checked
    # before single_new_field, which would otherwise swallow both (a validation constraint or a
    # related field is usually phrased as "add a field ..." too).
    # 2026-08-16: fifth new bucket -- roadmap Tier 0a ("Field/ACL visibility restriction on an
    # existing model", row 4, N=90 historical, 33% pass -- the single highest-volume untracked
    # shape on the whole 25-direction table) / Tier 1 item 4 ("New custom security-group
    # creation", Task 037). Deliberately distinct from record_rule_row_level_security right
    # above: THAT bucket's goal always bundles group + ACL row + record rule together in one
    # message, which this session found genuinely triggers a real `ask_operator`/`gates_disagree`
    # escalation (round-planning scopes round 1 to the group alone; Code-Review checks the full
    # verbatim goal) -- not a bug, a real design-scope ambiguity, but one this new bucket's own
    # goal shape structurally avoids: "create a new group, then restrict a specific field/button
    # on an existing view so only that group can see/use it" is a single, atomic requirement with
    # no record-rule component at all, matching the exact shape
    # `_CUSTOM_SECURITY_GROUP_APPLIED_GOAL_RE` (specialists/build/specialist.py) was already built
    # to police, and the shape `_validate_goal_named_custom_security_group_is_actually_applied`
    # was fixed earlier tonight to also accept a valid ir.rule OR a view groups= attribute as real
    # application. Checked before the broader `new_model_with_crud_and_security` bucket below,
    # whose bare `\bsecurity group\b` pattern would otherwise swallow this shape.
    ("custom_security_group_field_or_button_restriction", re.compile(
        r"(?:custom\s+)?security\s+group.{0,80}(?:restrict|appl(?:y|ied|ies))", re.IGNORECASE)),
    ("field_level_validation_constraint", re.compile(
        r"\bvalidation constraint\b|\bapi\.constrains\b|\bconstrains\(.*\)|\bvalidate.{0,30}field\b",
        re.IGNORECASE)),
    ("related_dot_notation_field", re.compile(
        r"\brelated field\b|\bdot[- ]notation\b|\brelated\s*=\s*['\"][\w.]+\.[\w.]+['\"]",
        re.IGNORECASE)),
    # 2026-08-15: fourth of the 25-direction roadmap's not-yet-implemented buckets (row 8, N=13
    # historical, 62% pass -- one of the higher historical pass rates among the untracked
    # directions). Real Odoo mechanism (grounded via web research before adding this bucket):
    # ir.ui.menu's own `groups_id` field, editable via a `<record model="ir.ui.menu">` eval
    # patch or the `<menuitem groups="...">` shorthand attribute -- already has a real
    # generation helper in this codebase (`_new_menu_groups_restriction_present`, discovered
    # earlier tonight investigating a different validator), so only tracking was missing.
    ("menu_visibility_restriction", re.compile(
        r"\bmenu visibility\b|\brestrict.{0,20}menu\b|\bhide.{0,20}menu\b|\bmenu.{0,20}groups_id\b",
        re.IGNORECASE)),
    ("cross_module_modifications", re.compile(
        r"\bcross[- ]module\b|\bmultiple modules\b|\bacross.*modules\b", re.IGNORECASE)),
    ("edit_existing_module_access_or_files", re.compile(
        r"already-installed module|existing module.*directly|edit the existing", re.IGNORECASE)),
    # Phase 33 (2026-08-11), §3 item 5: mined from real production history --
    # this sub-shape (27 real tasks, 37% pass) is really a search/domain-
    # construction task mislabeled as a "button" by the goal's own wording, not
    # a genuine workflow/action-button task (192 real tasks, only 10% pass).
    # Checked BEFORE the broader workflow_with_custom_buttons_or_cron bucket so
    # it is tracked, and can be bake-in-certified, separately.
    ("quick_filter_search_construction", re.compile(
        r"quick filter|filter button|domain filter", re.IGNORECASE)),
    ("workflow_with_custom_buttons_or_cron", re.compile(
        r"\bbutton\b|\bworkflow\b|\bcron\b|\bscheduled action\b|\bescalation\b", re.IGNORECASE)),
    # Mined 2026-08-11 from real production history (agent_memory_events,
    # 1,544 distinct real task outcomes): this exact shape -- a brand-new,
    # self-contained module defining ONE new model (a few plain fields, no
    # visibility restriction on a field of an EXISTING/shared model) or
    # adding a brand-new security group scoped to that new module's own
    # model -- passed 29/29 real runs (100%, avg 1.14 rounds, max 4),
    # across 4 distinct goal templates. Checked BEFORE the broader
    # new_model_with_crud_and_security bucket below, which mixes in the
    # genuinely harder "restrict/edit an EXISTING shared model" shape that
    # this pattern deliberately excludes.
    ("new_self_contained_module_basic_model_or_group", re.compile(
        r"(create|build) a (small )?(new |brand new )?(odoo )?module that (defines|adds) a brand new "
        r"(custom model|security group)", re.IGNORECASE)),
    ("new_model_with_crud_and_security", re.compile(
        r"\bnew model\b|\bcreate a model\b|\bir\.model\.access\b|\bsecurity group\b", re.IGNORECASE)),
    ("single_new_field", re.compile(
        r"\badd (a |an |one )?(new )?field\b|\bcomputed field\b", re.IGNORECASE)),
]

# Mined 2026-08-11: goals restricting an existing/shared model's field or
# record visibility by role/group ("only System Administrators should
# see...") failed reliably (round_budget escalation, ~5 rounds) in the
# same historical sample -- the opposite end of the same taxonomy bucket
# keyword-matched into "new_model_with_crud_and_security"/"single_new_field"
# above. Not currently a separate classify_scope() bucket (still keyword-
# fragile to isolate cleanly); recorded here as an explicit, named NON-safe
# shape so it's never accidentally certified by broadening the safe
# pattern above.
KNOWN_UNSAFE_SHAPE_FIELD_VISIBILITY_RESTRICTION_ON_EXISTING_MODEL = (
    "restricting a field's or record's visibility by role/group on an EXISTING, already-shared "
    "model ('only System Administrators should see...') -- historically fails via round-budget "
    "escalation, not yet safe to hand off"
)

_DEFAULT_N_REQUIRED = 29  # Phase 35 §1.1 (2026-08-12): the success-run-theorem-derived bar
# (95% confidence / 90% reliability), replacing the earlier Phase 32 placeholder of 15 -- this
# was designed in Phase 35 §1.1/§6 but never actually applied here until now.
_DEFAULT_EXPIRY_DAYS = 30
_MAX_ESCALATIONS = 3
_DIVERSITY_FLOOR_MIN_DISTINCT = 10  # Phase 35 §1.1 item 2: ceil(29/3)
_ADJUDICATOR_REVIEW_EVERY_N = 20  # Phase 35 §11.1 fix 3: volume-based, not calendar-based


@dataclass
class ScopeState:
    n_required: int = _DEFAULT_N_REQUIRED
    consecutive_clean_runs: int = 0
    escalations_used: int = 0
    total_runs: int = 0
    total_qualifying_failures: int = 0
    last_certified_at: str | None = None
    backlog: bool = False  # true once the escalation cap is exhausted -- a visible, honest terminal state
    history: list[dict] = field(default_factory=list)


def classify_scope(goal_text: str) -> str:
    """Returns the FIRST matching scope, same behavior as always -- most
    callers only need the single answer, not the full ambiguity picture.
    See classify_scope_with_confidence() for the Phase 35 §11.1 fix 2
    signal (does this goal ALSO match another scope, i.e. is the
    classification itself ambiguous)."""
    for scope_name, pattern in _SCOPE_KEYWORD_PATTERNS:
        if pattern.search(goal_text):
            return scope_name
    return "uncategorized"


def classify_scope_with_confidence(goal_text: str) -> tuple[str, bool, list[str]]:
    """Phase 35 §11.1 fix 2: classify_scope() itself is trusted everywhere in this system
    (certification status, sub-shape membership, and -- once retrieval lands -- retrieval
    eligibility) but its own misclassification rate was never measured, and there was no
    signal at all for "this goal doesn't cleanly belong to one scope."

    Real, computable today, without needing classify_scope() to be rewritten first (the
    original §11.1 draft proposed a per-condition confidence score, which a follow-up audit
    found didn't fit this function's actual heterogeneous regex structure -- some patterns are
    a clean OR of independent keywords, some are one long structured phrase that can't be
    decomposed the same way): checks every pattern instead of stopping at the first match.
    A goal matching exactly one scope's regex is unambiguous; a goal matching two or more is
    a real, mechanical ambiguity signal -- worth routing to the standing human spot-check at
    elevated priority (Phase 35 §11.1 fix 2's own design), not silently treated as a normal
    classification either way.

    Returns (primary_scope, is_ambiguous, all_matched_scopes) -- primary_scope is always
    identical to what classify_scope() alone would return, so this is a strict superset of the
    original function's contract, not a behavior change for any existing caller.
    """
    matched = [name for name, pattern in _SCOPE_KEYWORD_PATTERNS if pattern.search(goal_text)]
    primary = matched[0] if matched else "uncategorized"
    return primary, len(matched) > 1, matched


def _load_state() -> dict:
    if not os.path.exists(_STATE_PATH):
        return {}
    with open(_STATE_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {scope: ScopeState(**data) for scope, data in raw.items()}


def _save_state(state: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    serializable = {scope: asdict(scope_state) for scope, scope_state in state.items()}
    tmp_path = _STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, sort_keys=True)
    os.replace(tmp_path, _STATE_PATH)  # atomic, never leaves a half-written state file


def record_task_outcome(
    scope: str, task_id: str, qualifying_failure: bool, *, now: datetime.datetime | None = None,
    goal_text: str | None = None, used_retrieved_pattern: bool = False, escalate_bar: bool = True,
) -> ScopeState:
    """A "qualifying failure" (per Phase 32 section 3.2) is a failure the
    pipeline's own existing verification should have caught but didn't --
    never a failure Testing/QA itself correctly flagged mid-task (that's
    the system working as intended, not a certification-relevant defect).
    The caller decides qualifying_failure; this function only tracks it.

    `escalate_bar` (overnight direction-certification run, 2026-08-14 -- real gap found
    live: `new_self_contained_module_basic_model_or_group`'s bar was doubled three times
    in one night, 29 -> 232, by three consecutive occurrences of the SAME root cause -- a
    live-server registry-propagation-lag warning, confirmed live to be a real, mostly
    self-inflicted timing artifact from rapid concurrent task submission immediately after
    a warm-worker restart, not evidence that Build's own generated code was unreliable).
    `run_phase32_post_completion_checks()` in manager/loop.py bundles two conceptually
    different signals into one `warnings` list: a stale-registry warning (the live web
    server hasn't yet picked up a real, correct install -- a timing/propagation fact about
    THIS environment, says nothing about whether the generated code itself is trustworthy)
    and a UI-action-gap warning (the goal named a specific button/action and it's actually
    missing from the generated view -- a real, genuine content defect). Both deserved to
    reset the streak (an honest "we can't currently confirm this passed cleanly end to
    end" signal either way) but only the second deserves the bar-doubling escalation,
    which is supposed to mean "we now trust this direction less." Defaults to True --
    zero behavior change for every existing caller; a caller passes False only when it can
    show the qualifying failure came from a signal that says nothing about generation
    quality (currently: a stale-registry-only warning, no UI-action-gap).

    `goal_text`/`used_retrieved_pattern` (Phase 35 §1.1 item 2 / §11.1 fix 1, 2026-08-12):
    recorded per outcome so is_scope_certified() can check the real diversity floor -- at
    least `_DIVERSITY_FLOOR_MIN_DISTINCT` distinct goal texts among the current streak's
    COLD-generated outcomes only (retrieval-assisted passes still count toward the raw
    consecutive-run count, but per the §11.1 fix cannot be the only source of diversity
    evidence). `used_retrieved_pattern` defaults False because the retrieval mechanism
    itself (§3) is not built yet -- every real outcome today genuinely is cold, so this
    defaults to the honest current state, not a guess about a feature that doesn't exist
    yet; ready to be passed real values once retrieval lands, with no further code change.
    Distinctness here is exact-text deduplication, a real, working, and deliberately SIMPLER
    proxy than §3.2's designed embedding-based check -- that check needs Neo4j/Qdrant
    infrastructure this revision does not build; exact-dedup is honest about being a
    narrower stand-in, not a claim of parity with the fuller design.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = _load_state()
    scope_state = state.get(scope) or ScopeState()

    scope_state.total_runs += 1
    scope_state.history.append({
        "task_id": task_id, "at": now.isoformat(), "qualifying_failure": qualifying_failure,
        "goal_text": goal_text, "used_retrieved_pattern": used_retrieved_pattern,
    })

    if qualifying_failure:
        scope_state.total_qualifying_failures += 1
        scope_state.consecutive_clean_runs = 0
        scope_state.last_certified_at = None
        if not escalate_bar:
            pass  # streak reset (honest -- this run didn't cleanly confirm), bar unchanged
        elif scope_state.backlog:
            pass  # already terminal -- stays terminal until a human resets it explicitly
        elif scope_state.escalations_used < _MAX_ESCALATIONS:
            scope_state.escalations_used += 1
            scope_state.n_required *= 2
        else:
            # Cap exhausted: per section 2, this becomes a visible backlog item,
            # never an infinite silent retry loop.
            scope_state.backlog = True
    else:
        scope_state.consecutive_clean_runs += 1
        if (
            scope_state.consecutive_clean_runs >= scope_state.n_required
            and not scope_state.backlog
            and _diversity_floor_met(scope_state)
        ):
            scope_state.last_certified_at = now.isoformat()

    state[scope] = scope_state
    _save_state(state)
    return scope_state


def _current_streak_entries(scope_state: ScopeState) -> list[dict]:
    """The tail of `history` corresponding to the CURRENT consecutive-clean streak only --
    a qualifying failure resets consecutive_clean_runs to zero, so everything before the
    most recent failure is not part of the streak being evaluated for certification."""
    n = scope_state.consecutive_clean_runs
    return scope_state.history[-n:] if n > 0 else []


def _diversity_floor_met(scope_state: ScopeState) -> bool:
    """Phase 35 §1.1 item 2 / §11.1 fix 1: at least _DIVERSITY_FLOOR_MIN_DISTINCT distinct
    goal texts among the current streak's COLD-generated (used_retrieved_pattern=False)
    entries -- retrieval-assisted passes still count toward the raw streak length, but
    cannot be the only source of diversity evidence, closing the retrieval-induced-
    circularity gap §11.1 identifies (a retrieval-steered pass isn't independent evidence
    the same way a cold one is). Entries with no goal_text recorded at all (calls from
    before this field existed, or a caller that genuinely didn't pass one) are excluded
    from the count rather than silently counted as automatically distinct or automatically
    a duplicate -- an honest "we don't know" is safer than either extreme.
    """
    cold_goal_texts = {
        entry["goal_text"]
        for entry in _current_streak_entries(scope_state)
        if entry.get("goal_text") and not entry.get("used_retrieved_pattern", False)
    }
    return len(cold_goal_texts) >= _DIVERSITY_FLOOR_MIN_DISTINCT


def is_scope_certified(
    scope: str, *, expiry_days: int = _DEFAULT_EXPIRY_DAYS, now: datetime.datetime | None = None,
) -> bool:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = _load_state()
    scope_state = state.get(scope)
    if scope_state is None or scope_state.backlog or scope_state.last_certified_at is None:
        return False
    certified_at = datetime.datetime.fromisoformat(scope_state.last_certified_at)
    return (now - certified_at) <= datetime.timedelta(days=expiry_days)


def get_scope_state(scope: str) -> ScopeState:
    return _load_state().get(scope) or ScopeState()


def reset_scope_backlog(scope: str) -> None:
    """The one explicit human-only action for a scope stuck in `backlog`
    after exhausting its escalation cap -- deliberately not automatic."""
    state = _load_state()
    if scope in state:
        state[scope].backlog = False
        state[scope].escalations_used = 0
        state[scope].n_required = _DEFAULT_N_REQUIRED
        state[scope].consecutive_clean_runs = 0
        _save_state(state)


def get_adjudicator_review_sample(sample_size: int = 8, *, _random_module=None) -> list[dict] | None:
    """Phase 35 §11.1 fix 3: a blind self-consistency check for the sole adjudicator
    (the project owner) -- every downstream mechanism in this system (the streak, the diversity floor,
    §3.6's flag/retire decisions) depends entirely on one person's judgment staying
    consistent over months, and until this function existed, nothing checked that at all.

    Volume-based, not calendar-based (a corrected fix, after a follow-up audit found the
    original draft's "monthly" cadence was an unflagged, undefended placeholder,
    inconsistent with this system's own disclosed "we don't know our real throughput yet"):
    returns a random sample of `sample_size` past qualifying-failure adjudications, drawn
    from across every scope, the moment the total number of qualifying failures recorded
    system-wide crosses a new multiple of `_ADJUDICATOR_REVIEW_EVERY_N` (20) -- self-adjusting
    to whatever the real adjudication rate turns out to be, rather than assuming one.

    Returns None when the total isn't currently sitting exactly on a review boundary (so a
    caller can check this cheaply after every new qualifying-failure record without
    re-triggering a review on every single call once the threshold is passed) -- only
    returns a real sample of dicts (each carrying `scope`/`task_id`/`at`) once, right at
    the boundary itself.

    `_random_module` exists purely so a test can inject a seeded/deterministic `random`
    module instead of patching global random state -- never used by real callers.
    """
    import random as _real_random

    rng = _random_module or _real_random

    state = _load_state()
    all_failures: list[dict] = []
    for scope_name, scope_state in state.items():
        for entry in scope_state.history:
            if entry.get("qualifying_failure"):
                all_failures.append({**entry, "scope": scope_name})

    total = len(all_failures)
    if total == 0 or total % _ADJUDICATOR_REVIEW_EVERY_N != 0:
        return None

    all_failures.sort(key=lambda e: e.get("at", ""))
    return rng.sample(all_failures, k=min(sample_size, total))
