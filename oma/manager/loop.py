"""The Manager's own phased async loop -- Phase 6 step 5. A plain async
function with six sequential phases, following §2.3's confirmed real
Nexo pattern (a single async function, local variables, no graph
library) rather than an invented shape.

Phase 1: correction-detection check on the incoming message.
Phase 2: read relevant project memory; decide whether to ask a
         clarifying question or proceed (repeated-failure check
         wired in when an anticipated_scope model is known -- see the
         honest limitation noted in the docstring below).
Phase 3: sensitivity/capability classification -- sets tier and
         capability_class.
Phase 4: build the TaskContract and delegate.
Phase 5: await verification (gateway-outage handling + root-cause
         classification both wired in here).
Phase 6: write the verified outcome to memory, compose the reply.

Historical note (accurate through Phase 25, fixed in Phase 26B): there
used to be no real scope-extraction that turned free text into a
concrete Odoo model name, so `module_for_repeat_check` derived solely
from an optional `anticipated_scope["models"]` hint the real chat UI
never actually populated in production -- meaning the fencing-lock key
and `check_repeated_failures()` were both silently inert whenever no
caller happened to supply that hint (i.e. essentially always). Phase
26B (audit Finding #2) closed this: `contracts.module_identity.
resolve_module_identity()` is now the ONE authoritative source, parsing
the goal's own established `Model: <name>` convention (still preferring
an explicit `anticipated_scope["models"]` hint when one IS given, same
"explicit caller intent beats an automatic guess" precedent this file
already uses elsewhere). Cached once on `TaskContract.module_identity`
and reused by every consumer (this file's own fencing-lock derivation,
`manager/replanning.py`'s `should_escalate_to_operator()`) -- never
independently re-derived.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid

logger = logging.getLogger(__name__)

# Real infra change, 2026-07-22: GPU Worker 03's resident Qwen3-9B
# (enable_thinking disabled server-side, unlike qwen3.6-27b's shared
# reasoning tier) -- live-benchmarked the same night against realistic
# prompts for every one of these four call sites, at the REAL
# production max_tokens budget (1500): qwen3.6-27b reliably burned
# through the whole budget on invisible <think> tokens without ever
# reaching a real answer (3.6-24.6s, several genuinely truncated mid-
# thought), while this model answered correctly, cleanly, every time
# in 0.6-2.7s. Scoped deliberately narrow to these four pure
# classification/extraction calls (a 3-way label, a boolean+short
# extraction, a 4-way label, a boolean comparison) -- NOT used for
# decomposition (a live test showed a real quality concern: it split a
# single new model's own fields into separate constraints, violating a
# documented anti-pattern _decompose_into_constraints() explicitly
# guards against) or anything human-facing (escalation/success
# summaries, which need coherent prose, not just a fast label). See
# infra/gateway_client.py's own BACKEND_FAST_EXTRACTION comment for the
# full infra-side rationale.
FAST_EXTRACTION_MODEL = os.environ.get("OMA_MODEL_FAST_EXTRACTION", "qwen3-9b-fast-extraction")

from contracts.goal_facts import extract_goal_field_facts
from contracts.module_identity import resolve_module_identity
from contracts.schema import (
    CRITICAL_RULE_PREFIX,
    AutonomyTier,
    CapabilityClass,
    CompensatingAction,
    ConstraintNode,
    ConstraintNodeState,
    ConstraintState,
    ReplanRound,
    SpecialistType,
    TaskContract,
    VerificationResult,
)
from contracts.constraint_graph import collapse_cycle_to_tier_chain, detect_cycle
from manager.graph_scheduler import (
    _publish_node_round_advanced,
    _publish_node_specialist_changed,
    _publish_node_state_changed,
    run_graph_scheduler,
)
from infra.gateway_client import GatewayUnavailableError, LLMRepetitionLoopExhaustedError, ModelGatewayClient
from infra.redis_client import get_redis_client
from manager.charter import check_sensitive_paths, load_manager_constitution, load_sensitive_paths
from manager.classify import classify_capability_class
from manager.correction import handle_correction_detection
from manager.compensations import TaskCutOffPause
from manager.gateway_orchestration import (
    GatewayOutagePause,
    mark_task_running,
    run_with_gateway_outage_handling,
)
from manager.scope_detection import detect_existing_custom_module_target
from manager.graph_governance_flags import GateMode, get_gate_mode
from manager.intake_grounding import IntakeGroundingVerdict, check_intake_grounding
from tools_odoo.module_dev.toolchain import is_own_scaffolded_module
from tools_odoo.odoo_schema_client import list_module_models_fast
from manager.step_scheduler import Step, run_steps
from manager.task_state import clear_task_state
from manager.learning import (
    check_repeated_failures,
    classify_root_cause,
    flag_llm_call_count_outlier,
    handle_failed_verification,
)
from manager.memory import (
    ContextFormatter,
    ContextSelector,
    derive_known_risk_hint,
    estimate_tokens,
    read_project_memory,
)
from manager.replanning import (
    HARD_CEILING_FRACTION,
    MODEL_CONTEXT_WINDOWS,
    SOFT_TRIGGER_FRACTION,
    PauseForOperator,
    UNKNOWN_MODEL_WINDOW_FALLBACK,
    build_constraint_nodes,
    classify_unsupported_domain_touches,
    compress_round_history_mid_loop,
    decompose_into_constraints_with_artifacts,
    derive_constraint_labels,
    detect_failure_diversity,
    detect_oscillation,
    detect_shape_oscillation,
    estimate_context_pressure,
    estimate_manager_context_pressure,
    get_latest_checkpoint,
    get_latest_resume_point,
    get_original_capability_class,
    is_known_repeat,
    list_clarifications_since,
    maybe_upgrade_decomposition_with_architect_stage,
    model_context_window,
    recursively_decompose_constraints,
    oscillation_majority_signal,
    revise_contract_from_verification,
    select_specialist_for_retry,
    should_escalate_to_operator,
    sort_constraint_labels_by_dependency_tier,
    summarize_diagnosis_for_operator,
    summarize_escalation_for_operator,
    summarize_success_for_operator,
)
from infra.fencing import release_module_lock
from contracts.ambiguity_check import extract_ambiguity_report
from manager.ambiguity_digest import format_ambiguity_digest_line, record_ambiguity_pause
from contracts.business_rules import extract_interpreted_business_rules, goal_has_business_rule_ambiguity_signal
from infra.cloud_escalation import clear_cloud_spend
from contracts.coverage_signal import field_type_has_low_coverage_confidence
from manager.capability_readiness import build_capability_not_ready_block, capability_class_is_ready
from manager.governance import check_hard_governance_gates
from manager.escalations import list_pending_escalations, store_pending_escalation
from manager.sign_off import clear_pending_contract, get_pending_contract, store_pending_contract
from manager.task_plan import create_task_plan, list_plan_items, mark_plan_item_status, next_runnable_item
from manager.task_state import (
    PAUSE_DIAGNOSIS_REVIEW,
    PAUSE_ROUND_BUDGET_EXHAUSTED,
    PAUSE_SIGN_OFF_REQUIRED,
    clear_cancel_request,
    is_cancel_requested,
    set_task_state,
)
from manager.tools import (
    append_project_memory,
    apply_unverified_shape_confidence_gate,
    ask_operator,
    await_verification,
    delegate_to_specialist,
    fold_code_review_findings,
    fold_verification_notes,
    persist_node_round_checks,
    persist_node_round_diff,
    persist_node_round_findings,
    persist_node_round_step,
    persist_node_round_timing,
    read_node_states,
    run_code_review_diff,
    sync_live_node_telemetry_into_new_snapshot,
)
from manager.trace import publish_trace_event
from manager.ui_serialize import serialize_code_review_findings, serialize_testing_qa_checks

_SIGN_OFF_TIER_THRESHOLD = 3  # tier_3_pause_before and tier_4_presign_off, per MANAGER_CONSTITUTION.md
# Phase 26B follow-up (2026-07-27): a real, live-measured ordinary
# successful round (scaffold -> generate -> sandbox -> real install)
# takes on the order of 2-3 minutes -- this is short relative to that,
# deliberately: enough for a genuinely close-by lock release to land
# within the round budget (5 rounds x this backoff is a worst case of a
# couple minutes total, still well inside round_wall_clock_cap_seconds'
# own default of 2700s), without turning a real, resolvable collision
# into a slow, silent stall either.
_LOCK_REJECTED_RETRY_BACKOFF_SECONDS = 15

# Real, confirmed structural gap found live (2026-08-12, day-to-day directions sweep, "edit an
# existing module directly" re-verification): mirrors manager/classify.py's own established
# convention regex for this exact phrasing shape -- see the contract_inputs-building call site
# below for the real bug this closes.
#
# Real, confirmed FOLLOW-UP structural gap found live (2026-08-13 overnight Phase 35
# single_new_field bake-in, 5+ real occurrences the same night): this regex mirrors
# `manager/scope_certification.py`'s own `edit_existing_module_access_or_files` classify_scope()
# pattern EXACTLY, on purpose, per the comment above -- but that shared design means a goal
# deliberately phrased to AVOID that classify_scope() trigger (so it can be evidence for a
# DIFFERENT, not-yet-certified scope, e.g. single_new_field) also, as an unintended side effect,
# never triggers the strong `edit_existing_module:` signal here -- it falls through to the
# weaker `depends_on_module:` marker below, which was repeatedly, directly confirmed live NOT
# reliable enough to stop Build from scaffolding a brand-new, disconnected module even when the
# goal explicitly said "in that module's own models/models.py file, NOT a new extension module."
# Broadened to also recognize that exact real phrasing pattern -- a goal saying "not a new
# module" this explicitly is just as unambiguous a signal as "already-installed module", without
# reusing classify_scope()'s own specific trigger words.
_EDIT_EXISTING_MODULE_GOAL_RE = re.compile(
    r"\balready-installed module\b|\bedit the existing\b.{0,20}\bmodule\b"
    r"|\bnot a new\b.{0,20}\bmodule\b",
    re.IGNORECASE,
)


def _schema_detected_model_hint(detected_existing_module_dependency: str | None, target_db: str) -> str | None:
    """Phase 30 §26 follow-up (2026-08-04): converts an already-detected existing-module target
    (`detect_existing_custom_module_target()`'s own real, live-schema-grounded detection with
    independent re-verification -- see that function's own docstring) into the MODEL name
    `resolve_module_identity()`'s `hint` parameter expects, so a plain-English goal that never
    names its target model's real dotted identifier (confirmed live, 2026-08-04: 100% of a real
    24-task benchmark's goals) can still resolve `module_identity` correctly -- unblocking Build's
    own live-schema-grounding prompt injection (§26 item 1), which silently never fired without
    this. Deliberately conservative, matching detect_existing_custom_module_target()'s own "a
    wrong guess is worse than no guess" philosophy: only trusted when the module owns EXACTLY ONE
    model, OR (real gap found live, 2026-08-04, second retest pass: `project_meerwerk` owns FOUR
    models -- `project.meerwerk`, `.batch.invoice`, `.line`, plus its own extension of core
    `project.project` -- so the "exactly one" rule alone left this real, common shape (a primary
    model plus its own line-items/sub-models) unresolved even with a correct module pick in hand)
    one of the owned models' own dotted name, with dots replaced by underscores, EXACTLY equals
    the module's own name -- confirmed live: `project_meerwerk` module -> `project.meerwerk`
    model is exactly this real, common Odoo naming convention (the module is named after its own
    primary model), a safe, structural signal, never a guess at which of several equally-plausible
    candidates is "the" one. Still returns None (never guesses) if neither condition is met.
    Never raises -- any real failure (a stale module name, a live-lookup error) degrades to None,
    letting the caller fall through to the existing dotted-prose fallback unchanged.
    """
    if not detected_existing_module_dependency or not target_db:
        return None
    try:
        owned_models = list_module_models_fast(detected_existing_module_dependency, target_db)
    except Exception:
        return None
    if not owned_models:
        return None
    if len(owned_models) == 1:
        return owned_models[0]
    for model_name in owned_models:
        if model_name.replace(".", "_") == detected_existing_module_dependency:
            return model_name
    return None


def select_specialist_and_validator(capability_class_label: str) -> tuple[SpecialistType, str | None]:
    """Phase 6 originally hardcoded specialist_type=bug_fix and
    validation_by="testing_qa" for every task, regardless of shape --
    a real bug, not just a cosmetic gap: a capability_class=readonly
    task (task 4's audit shape) was being delegated to BuildSpecialist,
    which explicitly refuses that capability_class (Phase 9), and would
    then have gone to TestingQASpecialist for "verification" -- a
    reproduction-and-coverage check that has nothing meaningful to do
    with a read-only report at all.

    Fixed here (Phase 12), now that both Code-Review (Phase 10) and
    Testing/QA (Phase 11) actually exist: `readonly` routes directly to
    CodeReviewSpecialist, with validation_by=None -- there is no
    separate "build" step for a second specialist to independently
    verify; Code-Review's own claims_complete IS the terminal signal
    for a pure audit. Every other capability_class still requires a
    real, different specialist's name in validation_by, per the
    contract schema's own "never itself" rule -- module_dev/data_change
    tasks always go to BuildSpecialist with Testing/QA as the
    independent verifier, never trusting the builder's own word.
    """
    if capability_class_label == CapabilityClass.readonly_investigation.value:
        return SpecialistType.code_review, None
    return SpecialistType.bug_fix, "testing_qa"


def _extract_decomposition_touched_models(goal: str, constraint_artifacts) -> list[str]:
    """Shared extraction for both §17.3.1's blast-radius check and §17.3.2's claim admission
    check below -- reuses the exact same deterministic dotted-technical-name scan already built
    and tested for manager/intake_grounding.py's own gate, applied to the plan's own
    `creates`/`requires` identifiers plus the raw goal text -- `constraint_artifacts` entries are
    a flat mix of model/field/view/action/menu identifiers (see manager/replanning.py's own
    _ConstraintArtifacts docstring), so this extracts whatever LOOKS like a real dotted model
    technical name from them rather than assuming a clean, pre-separated model list exists.
    """
    from manager.intake_grounding import deterministic_technical_name_scan

    all_text = goal + "\n" + "\n".join(
        item for piece in constraint_artifacts for item in (piece.creates + piece.requires)
    )
    return deterministic_technical_name_scan(all_text)


async def _log_decomposition_blast_radius(goal: str, constraint_artifacts, task_id: str) -> None:
    """Phase 35 §17.3.1: the decomposition-level plan blast-radius check -- a NEW consultation
    point, run once per decomposed plan, BEFORE any sub-contract is dispatched. Ships in
    log_only mode per §17.11: always computes the real radius against the live graph and logs
    it, never blocks or reshapes the plan. `excluding_module=None` per §17.0.0.0a's own table --
    no module identity exists yet at this point in the pipeline, so this is a real, honest,
    slightly-more-conservative pre-estimate (§4.1's later pre-write check is the authoritative,
    module-aware re-measurement).
    """
    gate_mode = get_gate_mode("decomposition_blast_radius")
    if gate_mode == GateMode.DISABLED:
        return
    touched_models = _extract_decomposition_touched_models(goal, constraint_artifacts)
    if not touched_models:
        return
    try:
        from infra.neo4j_client import get_neo4j_read_driver
        from tools_odoo.knowledge_graph.build_safety_grounding import compute_change_radius

        driver = get_neo4j_read_driver()
        radius = await asyncio.to_thread(compute_change_radius, driver, touched_models, None)
    except Exception as exc:  # noqa: BLE001 -- fail-open, never blocks a turn on a graph hiccup
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "decomposition_blast_radius",
            "message": f"decomposition_blast_radius failed open ({type(exc).__name__}: {exc}).",
            "status": "running",
        })
        return
    if radius.in_progress:
        return
    # Phase 35 §18.3: log the real metric value for future shadow-mode threshold calibration --
    # this gate's own "<10 affected modules" number is an explicit, undstudied Bucket A
    # placeholder (§17.0.0.1a); this is the raw data point that calibration eventually replaces
    # it with. threshold=None here (not yet an enforced numeric gate at this rollout stage) --
    # once a real threshold is wired in, pass it through instead of None.
    from manager.threshold_calibration_log import log_gate_decision

    log_gate_decision(
        gate_name="decomposition_blast_radius", metric_name="affected_module_count",
        metric_value=radius.affected_module_count, threshold=None, verdict=gate_mode.value,
        task_id=task_id, extra={"touched_models": sorted(touched_models), "affected_modules": radius.affected_modules},
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "decomposition_blast_radius",
        "message": (
            f"[{gate_mode.value}] decomposition_blast_radius: plan touches "
            f"{sorted(touched_models)}, real graph shows {radius.affected_module_count} "
            f"existing module(s) already reference these ({radius.affected_modules}) -- "
            f"informational only at this rollout stage, plan proceeds unchanged."
        ),
        "status": "running",
    })


async def _log_concurrent_claim_admission(touched_models: list[str], task_id: str) -> None:
    """Phase 35 §17.3.2 / §18.1: concurrent-task admission control -- a NEW consultation point.
    Ships in log_only mode per §17.11: attempts a REAL claim against the real dev Redis (so the
    collision signal is genuine, not simulated), logs the outcome, then immediately releases it
    regardless of the result -- at this rollout stage nothing depends on holding the claim for
    the task's own real duration (that wiring -- hold through completion, release on
    success/failure/pause -- is the natural extension point once this gate is promoted to
    `enforced`, not built here ahead of that decision). A collision NEVER blocks or serializes a
    task at log_only; it is purely informational.
    """
    gate_mode = get_gate_mode("concurrent_claim_admission")
    if gate_mode == GateMode.DISABLED or not touched_models:
        return
    try:
        from manager.concurrent_claim import acquire_task_claim, release_task_claim

        result = await asyncio.to_thread(acquire_task_claim, touched_models, task_id)
        if result.acquired:
            await asyncio.to_thread(release_task_claim, touched_models, task_id)
    except Exception as exc:  # noqa: BLE001 -- fail-open, a Redis hiccup must never block a turn
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "concurrent_claim_admission",
            "message": f"concurrent_claim_admission failed open ({type(exc).__name__}: {exc}).",
            "status": "running",
        })
        return
    if not result.acquired:
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "concurrent_claim_admission",
            "message": (
                f"[{gate_mode.value}] concurrent_claim_admission: target "
                f"{result.colliding_target!r} is currently claimed by task "
                f"{result.colliding_task_id!r} -- a genuine, real concurrent-work collision. "
                f"Informational only at this rollout stage, this task proceeds unchanged."
            ),
            "status": "running",
        })


async def run_turn(
    message: str,
    client: ModelGatewayClient,
    classifier_model: str,
    conversation_history: list[dict] | None = None,
    anticipated_scope: dict | None = None,
    manager_model: str | None = None,
    override_repeated_failure_check: bool = False,
) -> dict:
    """Runs one full turn of the Manager's loop against a single Operator
    message. Returns a dict describing what happened -- never raises
    for expected pause conditions (ambiguity, gateway outage, repeated
    failure, sign-off-required); those come back as a structured
    {"status": "paused", "reason": ..., "message": ...} result instead,
    exactly so a CLI or future UI can render each one distinctly.

    `override_repeated_failure_check` (2026-08-08, real gap found live during the site_50
    benchmark, tasks 008-010): `check_repeated_failures()`'s own pause message literally asks
    "keep trying the same approach, or should we look at this differently?" -- but before this,
    there was NO parameter anywhere on this function to actually act on "keep trying." The check
    is a pre-flight, same-turn rejection with no stored escalation row (unlike a real mid-round
    PauseForOperator), so a plain resubmission of the identical goal always re-triggers the exact
    same pause forever -- the failure count this reads never decreases on its own. Confirmed
    live: 3 completely different, unrelated real business tasks (008/009/010, none sharing any
    actual content or root cause with each other) all got paused here purely because 2 EARLIER,
    genuinely unrelated tasks against the same Odoo model (`project.meerwerk`) had failed --
    correct, conservative behavior for the check itself, but with literally no way for a caller
    who's made the deliberate, informed choice to proceed anyway (a real "yes, keep trying"
    answer, not a blind retry) to express that choice. Defaults to False -- zero behavior change
    for every existing caller (the chat UI's own real submission path is untouched; wiring an
    actual UI affordance for Operator to answer this prompt is a separate, later piece of work, not
    silently done here) -- only ever skips the pause when a caller explicitly opts in.
    """
    conversation_history = conversation_history or []
    anticipated_scope = anticipated_scope or {}
    task_id = str(uuid.uuid4())

    # Phase 30, P3 (Phase G, §10, closes Problem G): a hard, deterministic
    # pre-flight gate for the small subset of active rules that reduce to
    # a real yes/no check (e.g. "never touch the accounting module
    # without asking first") -- checked before ANY LLM call this turn
    # would otherwise make, let alone before a contract is built. See
    # manager/governance.py's own docstring for the real, confirmed
    # premise gap found before writing this (resolve_module_identity()
    # -- the insertion point the plan's own text implied reusing --
    # returns None for the plan's own flagship real example, so this
    # scans the raw goal text directly instead of piggybacking on that).
    governance_block = check_hard_governance_gates(message)
    if governance_block is not None:
        governance_block["task_id"] = task_id
        governance_block["correction_result"] = {"status": "not_a_correction"}
        append_project_memory(
            event_type="decision", actor="manager", task_id=task_id, module=None,
            summary=governance_block["message"], tags=["governance_rule_blocked"],
            detail={"matched_rule_id": governance_block["matched_rule_id"], "goal": message},
            verified=True,
        )
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": governance_block["message"], "status": "paused",
        })
        return governance_block

    # Load the Manager's own charter IN FULL, every turn -- never
    # summarized, never referenced by name only (Phase 6 step 6, per
    # the technical document's §3).
    constitution_text = load_manager_constitution()
    sensitive_paths_raw = load_sensitive_paths()

    # --- Phase 1: correction-detection, and Phase 3's own classification
    # call, run CONCURRENTLY (Phase 22, 2026-07-23) -- both take only
    # the raw message text + the SAME fast-extraction model
    # (FAST_EXTRACTION_MODEL), confirmed independent by reading both
    # functions' own bodies: correction_result only feeds Phase 2's
    # memory logic and later pause-branch messaging; capability_class_
    # label only feeds tier_value/contract construction below -- neither
    # reads the other's output. handle_correction_detection's own side
    # effect (append_project_memory) is likewise independent of Phase
    # 3's pure classification. Runs on EVERY turn, so the savings
    # (roughly halving a few seconds of sequential fast-extraction
    # latency) are modest per-call but cumulative across every real
    # task submitted. Phase 3's own risk-tier computation
    # (check_sensitive_paths, purely local/no I/O) stays sequential
    # right after, unchanged -- it's not part of what was slow.
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "correction",
        "message": "Checking whether this is a correction to past behavior", "status": "running",
    })
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "classify",
        "message": "Classifying risk tier and work type", "status": "running",
    })
    # Phase 22 follow-up (2026-07-23): converted from a hand-picked
    # gather() pair to the generic manager.step_scheduler -- same
    # independence already established above, now expressed as
    # declared reads/writes so it composes automatically with any
    # future independent turn-start step, rather than needing a human
    # to re-verify and hand-edit this specific gather() call again.
    async def _step_correction_detection(_context):
        return {"correction_result": await handle_correction_detection(message, client, FAST_EXTRACTION_MODEL, task_id=task_id)}

    async def _step_capability_classification(_context):
        return {"capability_class_label": await classify_capability_class(message, client, FAST_EXTRACTION_MODEL, task_id=task_id)}

    # Phase 22 follow-up (2026-07-23): a real, structural gap found
    # running the SITE 50-task list -- a plain-text goal submitted
    # through this ordinary chat entry point had NO way to tell Build
    # "this targets an ALREADY-INSTALLED custom module" (e.g. "the
    # meerwerk record" meaning the real project_meerwerk module, 48
    # real fields already there), so Build scaffolded a brand-new
    # module with its own fresh field set instead, hitting the
    # single-constraint-per-round decomposition cap on a task that
    # only needed ONE new field.
    #
    # Deliberately maps to `depends_on_module:`, NOT `edit_existing_module:`
    # -- a real, live-confirmed distinction, not a naming detail.
    # `edit_existing_module:` means "this task's own module_name IS the
    # real target," which only ever made sense for a module OUR OWN
    # pipeline previously scaffolded under /mnt/extra-addons; tried
    # live against a genuine pre-existing CUSTOMER module
    # (`project_meerwerk`, real files at /opt/site/site16, never
    # written by this pipeline), it silently scaffolded a second,
    # colliding module of the same name instead of touching the real
    # one -- and even if it hadn't collided, directly rewriting real,
    # hand-maintained customer code in place (no Gitea history, no
    # established write-safety net) would be the wrong, unsafe
    # direction anyway. `depends_on_module:` instead generates a NEW,
    # normal `oma_*` module that properly `_inherit`s the real model
    # and declares a manifest dependency on it -- the exact same safe,
    # already-proven mechanism `run_plan()` uses for one outer-plan
    # item extending another's module, now also reachable from a
    # single free-text message, not just a caller-constructed plan.
    #
    # Only meaningful for module_development tasks, so this step reads
    # capability_class_label (computed in the SAME wave above) and
    # runs in the NEXT wave, skipping the LLM call entirely for any
    # other task type. See manager/scope_detection.py's own docstring
    # for the full "why" and the live false-positive it was redesigned
    # around.
    async def _step_existing_module_target(context):
        if context["capability_class_label"] != CapabilityClass.module_development.value:
            return {"existing_module_dependency": None}
        target_db = os.environ.get("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "")
        if not target_db:
            return {"existing_module_dependency": None}
        # Real, live-reproduced bug found 2026-07-27: unlike every
        # gateway-dependent call in the round loop proper (wrapped in
        # run_with_gateway_outage_handling(), which turns a transient
        # GatewayUnavailableError into a clean, resumable pause), this
        # turn-START step (run via run_steps()/asyncio.gather(), before
        # a contract or module lock even exist yet) had no handling at
        # all -- a transient 429 from the fast-extraction backend
        # crashed the ENTIRE request with a raw 500, before the task
        # was even created, with no pause state, no lock to release,
        # nothing for a caller to resume. This is purely an optional
        # detection hint (every OTHER early-return path above already
        # treats "can't determine this" as a graceful None, e.g. no
        # target_db configured) -- gracefully degrading to None on a
        # transient gateway hiccup is strictly safer than crashing the
        # whole turn over a soft, already-optional pre-check.
        try:
            result = await detect_existing_custom_module_target(
                message, target_db, client, FAST_EXTRACTION_MODEL, task_id=task_id,
            )
        except GatewayUnavailableError:
            return {"existing_module_dependency": None}
        return {"existing_module_dependency": result}

    # P12 Tier B/C item 26 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    # §4, source C's §3 Role 1, independently corroborated in the synthesis's §3.3): real,
    # confirmed gap -- OMA had no pre-build "is this goal genuinely well-specified enough to
    # build against" check; under-specification was only ever discovered the expensive way, via
    # failed rounds and eventual reactive escalation. Runs in the SAME concurrent turn-start
    # wave as correction_detection/capability_classification (same independence: reads only the
    # raw message text, writes nothing any other step reads), so this doesn't add sequential
    # latency on top of what already runs on every turn. Deliberately a HIGH bar (see
    # contracts/ambiguity_check.py's own docstring) -- the normal, well-specified task must never
    # be blocked by this.
    async def _step_ambiguity_check(_context):
        try:
            report = await extract_ambiguity_report(message, client, FAST_EXTRACTION_MODEL, task_id=task_id)
        except GatewayUnavailableError:
            return {"ambiguity_report": None}
        return {"ambiguity_report": report}

    # Phase 35 §17.2.1: the task-intake existence/dedup check -- ships in log_only mode per
    # §17.11's staged rollout (manager/graph_governance_flags.py). Runs in the same turn-start
    # wave as the other independent checks above (reads nothing any other step writes). At
    # log_only, this ALWAYS computes and logs the real verdict against the live graph but NEVER
    # changes what the task does -- the mode check below is the only place that would ever act
    # on the verdict, and it currently never does, by design, until a human promotes this gate
    # to `enforced` via set_gate_mode().
    async def _step_intake_grounding_gate(_context):
        gate_mode = get_gate_mode("intake_grounding_gate")
        if gate_mode == GateMode.DISABLED:
            return {"intake_grounding_result": None}
        try:
            from infra.neo4j_client import get_neo4j_read_driver
            driver = get_neo4j_read_driver()
            result = await check_intake_grounding(
                message, driver, client, FAST_EXTRACTION_MODEL, task_id=task_id,
            )
        except Exception as exc:  # noqa: BLE001 -- fail-open, never blocks a turn on a graph hiccup
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "intake_grounding",
                "message": f"intake_grounding_gate failed open ({type(exc).__name__}: {exc}) -- "
                           f"continuing without an intake verdict.",
                "status": "running",
            })
            return {"intake_grounding_result": None}
        if result.verdict == IntakeGroundingVerdict.EXISTS_VERBATIM:
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "intake_grounding",
                "message": (
                    f"[{gate_mode.value}] intake_grounding_gate: {result.detail} "
                    f"(mode={gate_mode.value} -- informational only, this task proceeds unchanged)"
                ),
                "status": "running",
            })
        return {"intake_grounding_result": result}

    # Real, live-reproduced bug found 2026-07-27: unlike every gateway-
    # dependent call in the round loop proper (wrapped in run_with_
    # gateway_outage_handling(), which turns a transient
    # GatewayUnavailableError into a clean, resumable pause),
    # correction_detection/capability_classification here have no safe
    # neutral fallback to silently degrade to the way existing_module_
    # target's own optional hint does (guessing the wrong capability_
    # class could route a real task to the wrong specialist entirely,
    # worse than pausing) -- so a transient 429/outage crashed the
    # ENTIRE request with a raw 500, before the task even had a
    # contract or module lock to clean up, confirmed live twice in a
    # row. No module lock exists yet at this point to release (unlike
    # run_with_gateway_outage_handling's own case), so this is a
    # narrower, local catch rather than reusing that function directly.
    try:
        turn_start_results = await run_steps(
            [
                Step(name="correction_detection", reads=frozenset(), writes=frozenset({"correction_result"}), run=_step_correction_detection),
                Step(name="capability_classification", reads=frozenset(), writes=frozenset({"capability_class_label"}), run=_step_capability_classification),
                Step(
                    name="existing_module_target", reads=frozenset({"capability_class_label"}),
                    writes=frozenset({"existing_module_dependency"}), run=_step_existing_module_target,
                ),
                Step(name="ambiguity_check", reads=frozenset(), writes=frozenset({"ambiguity_report"}), run=_step_ambiguity_check),
                Step(
                    name="intake_grounding_gate", reads=frozenset(),
                    writes=frozenset({"intake_grounding_result"}), run=_step_intake_grounding_gate,
                ),
            ],
            {},
        )
    except GatewayUnavailableError as exc:
        # Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run): mirrors the
        # identical fix in manager/gateway_orchestration.py's own run_with_gateway_outage_
        # handling() -- a repetition loop that exhausts every retry is NOT a genuine
        # infrastructure outage (confirmed live via an immediate real health check both times),
        # so the message must not claim "it's an infrastructure outage... resubmit once the
        # gateway is back" when the real cause is the model itself struggling with this specific
        # generation, not the backend being down.
        is_repetition_exhaustion = isinstance(exc, LLMRepetitionLoopExhaustedError)
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "correction",
            "message": (
                "This task's own startup content generation kept hitting a repetition loop -- "
                "pausing before this task even started."
                if is_repetition_exhaustion else
                "The model gateway is currently unreachable -- pausing before this task even started."
            ),
            "status": "paused",
        })
        message = (
            (
                "This task's own startup content generation kept hitting a repetition loop even "
                "after several attempts with escalating temperature -- this is NOT an "
                "infrastructure outage (the model gateway itself is healthy and reachable); the "
                "model is genuinely struggling to generate this specific content without "
                "repeating itself. A plain resubmit may still succeed (sampling is stochastic)."
            )
            if is_repetition_exhaustion else
            (
                "The model gateway is currently unreachable, so I've paused before this task even "
                "started rather than fail outright. This isn't a question for you and it isn't "
                "waiting on your sign-off -- it's an infrastructure outage. Please resubmit once "
                "the gateway is back."
            )
        ) + f" Real error: {type(exc).__name__}: {exc}"
        return {
            "status": "paused",
            "reason": "gateway_unavailable",
            "message": message,
            "task_id": task_id,
            "correction_result": {"status": "not_a_correction"},
        }
    correction_result = turn_start_results["correction_result"]
    capability_class_label = turn_start_results["capability_class_label"]
    detected_existing_module_dependency = turn_start_results["existing_module_dependency"]
    ambiguity_report = turn_start_results["ambiguity_report"]
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "correction",
        "message": correction_result.get("message", correction_result.get("status", "")), "status": "passed",
    })

    # P12 Tier S item 1 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
    # "capability-readiness registry"): a hard, deterministic pre-flight gate for capability
    # classes with no real, working specialist handler -- checked right after classification,
    # before a contract is ever built, matching the same "fail loud before wasting a round"
    # discipline as check_hard_governance_gates() above. Real, live-confirmed premise: tasks 014
    # and 016 of the 2026-07-30 SITE 50-task benchmark both burned a full real round hitting
    # BuildSpecialist._run_data_change()'s own documented permanent stub before failing -- this
    # closes that gap for every future data_change-classified task, not just those two.
    if not capability_class_is_ready(capability_class_label):
        capability_block = build_capability_not_ready_block(capability_class_label)
        capability_block["task_id"] = task_id
        capability_block["correction_result"] = correction_result
        append_project_memory(
            event_type="decision", actor="manager", task_id=task_id, module=None,
            summary=capability_block["message"], tags=["capability_not_ready"],
            detail={"capability_class_label": capability_class_label, "goal": message},
            verified=True,
        )
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": capability_block["message"], "status": "paused",
        })
        return capability_block

    # P12 Tier B/C item 26: the actual halt -- only when the extraction is confidently sure the
    # goal has genuine, blocking ambiguity (a deliberately high bar; see contracts/
    # ambiguity_check.py's own docstring). `ambiguity_report is None` (a gateway outage during
    # this turn-start step) is never treated as ambiguous -- fails open, same posture as every
    # other optional turn-start step in this same wave.
    if ambiguity_report is not None and ambiguity_report.has_blocking_ambiguity:
        # Phase U (§25.5): cluster this occurrence with any already-open pause raising the same
        # underlying question, rather than treating every parallel task hitting the same category
        # as its own separate escalation -- the real, observed failure mode this avoids (§25.5's
        # own cited evidence): an un-rate-aware escalation gate degrades to rubber-stamped noise
        # under volume. A genuinely NEW category is still always surfaced immediately (goal-level
        # ambiguity must be resolved before work starts, never deferred); only a DUPLICATE
        # occurrence of an already-open cluster gets the lower-urgency framing.
        cluster = record_ambiguity_pause(
            task_id, message, ambiguity_report.blocking_questions, ambiguity_report.reasoning,
        )
        base_message = (
            "Before I start building, I need you to resolve something the goal doesn't "
            "make clear: " + " ".join(ambiguity_report.blocking_questions)
            + (f" ({ambiguity_report.reasoning})" if ambiguity_report.reasoning else "")
        )
        if not cluster["is_new_cluster"]:
            base_message += (
                f" ({format_ambiguity_digest_line(cluster)} -- already pending, no new action "
                "needed from you for this specific occurrence.)"
            )
        ambiguity_block = {
            "status": "paused",
            "reason": "blocking_ambiguity",
            "message": base_message,
            "task_id": task_id,
            "correction_result": correction_result,
        }
        append_project_memory(
            event_type="decision", actor="manager", task_id=task_id, module=None,
            summary=ambiguity_block["message"], tags=["blocking_ambiguity"],
            detail={"blocking_questions": ambiguity_report.blocking_questions, "goal": message},
            verified=True,
        )
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": ambiguity_block["message"], "status": "paused",
        })
        return ambiguity_block

    # --- Phase 2: relevant project memory + repeated-failure check ---
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "memory",
        "message": "Reading memory for relevant history", "status": "running",
    })
    memory_rows = read_project_memory(tags=None, limit=50)
    selector = ContextSelector()
    assembled = selector.select(candidate_rows=memory_rows, history=conversation_history, memory_query_ran=True)
    formatter = ContextFormatter()
    memory_block = formatter.format_system_block(assembled)
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "memory",
        "message": f"Found {len(memory_rows)} relevant memory row(s).", "status": "passed",
    })

    # Phase 26B (2026-07-27, audit Finding #2): resolve_module_identity()
    # is now the ONE authoritative source -- see its own docstring and
    # this file's top-level docstring for the real, confirmed bug this
    # replaces (the old `scope_models[0]`-only derivation silently
    # collapsed to None whenever no caller supplied anticipated_scope,
    # which the real chat UI never does). An explicit anticipated_scope
    # hint, when given, still wins -- same precedent as
    # detected_existing_module_dependency below.
    #
    # Phase 30 §26 follow-up (2026-08-04): real, confirmed gap -- resolve_module_identity()'s own
    # prose fallback only matches a literal `Model: X` line or a dotted identifier (e.g.
    # "project.meerwerk") appearing verbatim in the goal text. Confirmed live: every one of a
    # real 24-task benchmark's goals describes the target in plain English ("the meerwerk form",
    # never "project.meerwerk"), so module_identity resolved to None on 100% of them -- silently
    # disabling Build's own live-schema-grounding prompt injection (§26 item 1) for exactly the
    # goal-writing style real users (and, per this comment's own point below, real production
    # traffic) actually use. detect_existing_custom_module_target() (already computed above, at
    # turn-start, as detected_existing_module_dependency) already solves this same problem for a
    # DIFFERENT purpose (edit_existing_module: routing) via real, live-schema-grounded detection
    # with independent re-verification against the live registry -- never a bare LLM guess -- so
    # this reuses that ALREADY-COMPUTED result rather than a second detection call. Converts the
    # detected MODULE name to its owning MODEL name via list_module_models_fast(); deliberately
    # conservative, matching detect_existing_custom_module_target()'s own "a wrong guess is worse
    # than no guess" philosophy: only trusted when the module owns EXACTLY ONE model (an
    # ambiguous multi-model module falls through to None, same as today). Never raises -- any
    # failure here degrades to the prior behavior (dotted-prose fallback), never blocks the turn.
    scope_models = anticipated_scope.get("models") or []
    module_for_repeat_check = resolve_module_identity(
        message,
        hint=(scope_models[0] if scope_models else _schema_detected_model_hint(
            detected_existing_module_dependency,
            os.environ.get("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", ""),
        )),
    )
    # P13 item 6: computed once, here, from the memory_rows already fetched above -- a property
    # of the goal/system, never re-derived per round (see TaskContract.known_risk_hint's own
    # docstring for the fresh-vs-static rationale).
    known_risk_hint = derive_known_risk_hint(memory_rows, module_for_repeat_check)
    if module_for_repeat_check and not override_repeated_failure_check:
        repeat_check = check_repeated_failures(module_for_repeat_check)
        if repeat_check["should_pause"]:
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "memory",
                "message": repeat_check["message"], "status": "paused",
            })
            return {
                "status": "paused",
                "reason": "repeated_failure",
                "message": repeat_check["message"],
                "task_id": task_id,
                "correction_result": correction_result,
            }

    # --- Phase 3: sensitivity classification (capability_class_label
    # was already computed concurrently with Phase 1's correction
    # detection, above) ---
    tier_value = check_sensitive_paths(
        models=anticipated_scope.get("models"),
        fields=anticipated_scope.get("fields"),
        concerns=anticipated_scope.get("concerns"),
        files=anticipated_scope.get("files"),
        is_write=anticipated_scope.get("is_write", False),
        touches_schema_or_permissions=anticipated_scope.get("touches_schema_or_permissions", False),
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "classify",
        "message": f"Risk tier {tier_value} -- work type: {capability_class_label}.", "status": "passed",
    })

    # --- Phase 4: build the TaskContract ---
    specialist_type, validation_by = select_specialist_and_validator(capability_class_label)
    contract_inputs = list(anticipated_scope.get("contract_inputs") or [])
    # Phase 22 follow-up (2026-07-23): fold in the turn-start
    # existing-module detection (above) -- only if the caller hasn't
    # ALREADY supplied its own depends_on_module: marker (an explicit
    # caller hint always wins over an automatic guess, even a verified
    # one). Deliberately depends_on_module:, not edit_existing_module:
    # -- see the turn-start step's own comment for the real, live bug
    # that distinction fixes.
    #
    # Real, confirmed structural gap found live (2026-08-12, day-to-day directions sweep, "edit an
    # existing module directly" re-verification): `edit_existing_module:` was a real mechanism
    # Build already honored, but NOTHING ever populated it automatically for a plain-text goal --
    # this whole branch always mapped to depends_on_module: instead, even when the goal explicitly
    # asked to edit an already-installed module DIRECTLY and the detected target was confirmed to
    # be one of THIS PIPELINE'S OWN previously scaffolded modules (never a genuine external
    # customer module, which stays on the safer depends_on_module: path per this comment's own
    # original reasoning above). Confirmed live: Build scaffolded a brand-new, disconnected
    # extension module instead of touching the real one, on a goal naming it verbatim.
    if (
        detected_existing_module_dependency
        and _EDIT_EXISTING_MODULE_GOAL_RE.search(message)
        and is_own_scaffolded_module(detected_existing_module_dependency)
        and not any(i.startswith(("depends_on_module:", "edit_existing_module:")) for i in contract_inputs)
    ):
        contract_inputs.append(f"edit_existing_module:{detected_existing_module_dependency}")
    elif detected_existing_module_dependency and not any(
        i.startswith("depends_on_module:") for i in contract_inputs
    ):
        contract_inputs.append(f"depends_on_module:{detected_existing_module_dependency}")
    contract = TaskContract(
        task_id=task_id,
        specialist_type=specialist_type,
        capability_class=CapabilityClass(capability_class_label),
        tier=AutonomyTier(tier_value),
        goal=message,
        # `contract_inputs`, like every other anticipated_scope key, is an
        # explicit caller-supplied hint -- real scope-extraction from free
        # text still doesn't exist (same documented Phase 6 gap). This is
        # what lets a caller name a concrete Code-Review target
        # ("diff_module:<name>" / "full_codebase_audit:<path>") rather
        # than leaving contract.inputs empty and unusable for that
        # specialist. Testing/QA's own "verify_module:..." entry is
        # populated separately and automatically (manager/tools.py's
        # await_verification()), since it depends on the Build
        # specialist's own real, run-time-derived module name/db, not
        # something knowable before delegation. `depends_on_module:`
        # may ALSO now be populated automatically here, from
        # detect_existing_custom_module_target() above -- the one
        # exception to "real scope-extraction doesn't exist yet",
        # deliberately narrow and independently live-verified rather
        # than a general free-text scope parser.
        inputs=contract_inputs,
        rules=[],
        deliverables=[],
        compensating_actions=[
            CompensatingAction(forward_step="(set by the specialist)", undo_action="(set by the specialist)")
        ] if specialist_type == SpecialistType.bug_fix else [],
        validation_by=validation_by,
        # Phase 22 (2026-07-23): pause_if was declared on TaskContract
        # since Phase 6 but no real call site ever set it to anything
        # but []. First real use: a caller opts into "diagnose first,
        # confirm the plan before I build anything" via
        # anticipated_scope["diagnose_first"] -- orthogonal to tier
        # (works on ANY task, not just sensitive-tier ones), checked
        # right after the existing tier gate below.
        pause_if=["diagnose_first"] if anticipated_scope.get("diagnose_first") else [],
        turn_budget=15,
        # Phase 18 (§22.5): derived ONCE, here, up front, when the
        # contract is first built -- never re-derived mid-loop (a later
        # round revising this contract via
        # revise_contract_from_verification() preserves whatever
        # constraint_status already holds via model_copy(), it never
        # calls this again).
        constraint_status=derive_constraint_labels(message),
        # Phase 26B (2026-07-27): cached once here, same discipline as
        # constraint_status above -- every later round's revised
        # contract (model_copy()) carries this forward unchanged, never
        # re-derives it. Already computed above (module_for_repeat_check)
        # via the same resolve_module_identity() call this field exists
        # to make the single authoritative answer.
        module_identity=module_for_repeat_check,
        # P13 item 6: computed once, above, from memory_rows already fetched for this same turn.
        known_risk_hint=known_risk_hint,
        # Same caller-supplied-hint pattern as contract_inputs above --
        # lets a test/CLI/UI override Phase 15's own round budget/cap
        # without needing real scope-extraction to exist. Falls back to
        # TaskContract's own real defaults (5 rounds, 2700s) when absent.
        **{
            k: v for k, v in {
                "planning_round_budget": anticipated_scope.get("planning_round_budget"),
                "round_wall_clock_cap_seconds": anticipated_scope.get("round_wall_clock_cap_seconds"),
            }.items() if v is not None
        },
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "contract",
        "message": contract.goal, "status": "passed",
    })

    # Real, confirmed bug found live: a readonly_investigation task with
    # no contract_inputs (e.g. Operator just typing "audit the codebase" with
    # no anticipated_scope, the ONLY way this actually happens in the
    # real chat UI today, which never supplies anticipated_scope at all)
    # used to sail straight into the round loop, where
    # CodeReviewSpecialist immediately rejected it ("neither a
    # diff_module: nor a full_codebase_audit: entry"), the loop then
    # burned all remaining rounds crashing on BuildSpecialist too (it
    # can never handle readonly_investigation), and Operator got a confusing
    # escalation after 5 rounds of pure noise instead of a real audit or
    # a clear, honest question. This is the documented, accepted
    # scope-extraction gap (see this file's own module docstring) --
    # the fix isn't inventing NLP, it's asking Operator directly the moment
    # the gap is knowable, instead of pretending it isn't there.
    if contract.capability_class == CapabilityClass.readonly_investigation and not contract.inputs:
        clarification = (
            "I can run that audit, but I need to know what to point it at -- "
            "a specific module name, or a path under the codebase. Which one?"
        )
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "contract",
            "message": clarification, "status": "paused",
        })
        return {
            "status": "paused",
            "reason": "needs_clarification",
            "message": clarification,
            "task_id": task_id,
            "correction_result": correction_result,
        }

    # A real, durable record of this standalone task's own goal text --
    # see scripts/004_task_created_event.sql's own comment for why this
    # exists (GET /api/tasks needs a real title for an in-progress task,
    # not just completed ones, and previously had nowhere durable to
    # read one from for a task outside the outer-plan layer).
    append_project_memory(
        event_type="task_created",
        actor="operator",
        task_id=task_id,
        module=module_for_repeat_check,
        summary=contract.goal,
        tags=[capability_class_label],
        detail={
            "tier": tier_value,
            "capability_class": capability_class_label,
            "specialist_type": specialist_type.value,
            # Real, honest necessity for replay (found live, via a real
            # replay test failing): contract.inputs (e.g. a Code-Review
            # target named via contract_inputs) is a real, caller-
            # supplied hint with no other durable home -- without it
            # here too, a replayed task loses whatever made the
            # original one runnable in the first place.
            "inputs": contract.inputs,
        },
    )

    module_lock_name = module_for_repeat_check or f"task:{task_id}"

    # Tier 3/4: pause for Operator's sign-off BEFORE a specialist ever
    # touches Odoo -- per MANAGER_CONSTITUTION.md's own tier
    # definitions ("tier_3_pause_before", "tier_4_presign_off"). A real
    # gap found while building Phase 12's plan/act UI pattern: this
    # gate never existed before (PAUSE_SIGN_OFF_REQUIRED was defined in
    # Phase 6 but never actually wired in) -- a tier-3/4 task would
    # have proceeded straight through delegation. The pending contract
    # is stored in Redis, not just held on the stack, specifically so
    # it survives across a real chat UI's separate approve/reject
    # request, which may arrive long after this one returns.
    if tier_value >= _SIGN_OFF_TIER_THRESHOLD:
        set_task_state(task_id, PAUSE_SIGN_OFF_REQUIRED)
        store_pending_contract(task_id, contract, module_lock_name, correction_result, module_for_repeat_check)
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"Tier {tier_value} -- waiting on your sign-off before delegating.", "status": "paused",
        })
        return {
            "status": "paused",
            "reason": "sign_off_required",
            "message": (
                f"This task is tier {tier_value} and needs your sign-off before I proceed -- "
                f"nothing has touched Odoo yet. Goal: {contract.goal!r}. "
                f"Reply with a sign-off decision for task {task_id} to continue."
            ),
            "task_id": task_id,
            "contract": contract,
            "correction_result": correction_result,
        }

    # Phase 22 (2026-07-23): diagnose-then-confirm -- an opt-in mode,
    # orthogonal to the tier gate above (works on ANY task, not just
    # sensitive-tier ones; only reached at all when the tier gate above
    # did NOT already fire, so a task never pauses twice). Restates the
    # Manager's own understanding and proposed plan via a lightweight
    # LLM call (summarize_diagnosis_for_operator -- no specialist delegated,
    # nothing has touched Odoo yet, same "nothing built before
    # approval" guarantee the tier gate already gives sensitive-tier
    # tasks), then reuses the EXACT SAME Redis-backed pending-contract
    # store and resume_after_sign_off() back half the tier gate already
    # uses -- a distinct task-state/reason so the UI renders "here's
    # what I found, proceed?" copy, never the risk-based sign-off
    # wording, but no new storage/resume mechanism.
    if "diagnose_first" in contract.pause_if:
        # manager_model is only resolved from its real default inside
        # _execute_contract() (line ~1324) -- this gate runs BEFORE
        # that, so it needs the exact same fallback here, not a bare
        # possibly-None value passed to a real model-gateway call.
        resolved_manager_model = manager_model or os.environ.get("OMA_MODEL_MANAGER", "qwen3.6-27b")
        diagnosis = await summarize_diagnosis_for_operator(
            contract.goal, contract.inputs, contract.deliverables, client, resolved_manager_model, task_id=task_id,
        )
        set_task_state(task_id, PAUSE_DIAGNOSIS_REVIEW)
        store_pending_contract(
            task_id, contract, module_lock_name, correction_result, module_for_repeat_check,
            diagnosis=diagnosis,
        )
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"Diagnosis: {diagnosis['plan']} -- waiting on your confirmation before delegating.",
            "status": "paused",
        })
        return {
            "status": "paused",
            "reason": "diagnosis_confirmation",
            "message": (
                f"Before I build/change anything: {diagnosis['understanding']} My plan: "
                f"{diagnosis['plan']}"
                + (f" Open question: {diagnosis['open_questions']}" if diagnosis["open_questions"] else "")
                + f" Reply with a confirmation decision for task {task_id} to continue."
            ),
            "task_id": task_id,
            "contract": contract,
            "diagnosis": diagnosis,
            "correction_result": correction_result,
        }

    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "delegate",
        "message": "Delegating to a specialist", "status": "running",
    })

    # Phase 18 (§22.9): single-constraint-at-a-time decomposition for
    # genuinely multi-constraint module_dev goals. A cheap, cached
    # classification call, run once per task, before round 1 -- 1-2
    # items changes nothing (today's single-contract shape is already
    # correct for that case, per the dossier's own §1.5 finding); 3+
    # items issues an ordered sequence of single-new-constraint
    # sub-contracts instead of one contract asking for everything at
    # once (the dossier's own Constraint Decay finding: agents reliably
    # hold 1-2 constraints, degrade sharply at 3+ held simultaneously).
    # Scoped to module_development only -- the exact shape §22.1's own
    # confirmed hard task has, and the only capability_class Build's
    # scaffold/write/install toolchain (and this component's own
    # module-name continuity requirement) applies to at all.
    if contract.capability_class == CapabilityClass.module_development:
        # Phase 30, P0 (Phase K, §14): a cheap, whole-goal check for
        # whether ANY part of this goal touches a domain with zero real
        # generation support (contracts/unsupported_domains.json) --
        # before deciding whether/how to decompose. A goal that touches
        # none of these domains (the overwhelming majority) proceeds
        # exactly as before, zero behavior change. A goal that touches
        # even one -- possibly alongside genuinely buildable pieces in
        # the same sentence, per §14 step 3's own worked example ("add a
        # field AND wire it to a QWeb report") -- is ALWAYS routed
        # through single-constraint decomposition regardless of the
        # normal 3+-label threshold below, so each piece gets its own
        # sub-contract and _run_constraint_labels_from() can gate the
        # unsupported one specifically, on its own narrowed focus,
        # rather than the whole task silently best-effort attempting it.
        unsupported_touches = await classify_unsupported_domain_touches(
            contract.goal, client, classifier_model, task_id=task_id
        )
        # P13 item 10(a): the same decomposition call, extended to also emit creates/requires
        # per constraint (as a separate function -- see decompose_into_constraints_with_
        # artifacts()'s own docstring for why it's not a refactor of decompose_into_constraints()
        # itself) -- one real LLM call at this site, same as before this change.
        constraint_artifacts = await decompose_into_constraints_with_artifacts(
            contract.goal, client, classifier_model, task_id=task_id
        )
        await _log_decomposition_blast_radius(contract.goal, constraint_artifacts, task_id)
        _decomposition_touched_models = _extract_decomposition_touched_models(contract.goal, constraint_artifacts)
        await _log_concurrent_claim_admission(_decomposition_touched_models, task_id)
        constraint_labels = [item.label for item in constraint_artifacts]
        # Phase 30, P1e (Phase M, §16): a cheap, deterministic post-
        # processing sort -- model/field-defining sub-contracts always
        # precede sub-contracts that reference them, a known "hard,
        # guaranteed failure" when violated. Stable: never reorders the
        # LLM's own within-tier judgment, only enforces the cross-tier
        # dependency direction. Harmless to always apply here (a no-op
        # when constraint_labels ends up unused below, e.g. <3 labels
        # and no unsupported touches).
        constraint_labels = sort_constraint_labels_by_dependency_tier(constraint_labels)
        # P13 item 12a: same contract-build call site, gated by a deterministic keyword pre-check
        # (goal_has_business_rule_ambiguity_signal()) so most ordinary tasks never make this call
        # at all -- near-zero marginal cost across the real task mix, run here rather than merged
        # into decompose_into_constraints_with_artifacts()'s own schema (see
        # contracts/business_rules.py's own docstring for why they're kept separate).
        if goal_has_business_rule_ambiguity_signal(contract.goal):
            interpreted_business_rules = await extract_interpreted_business_rules(
                contract.goal, client, classifier_model, task_id=task_id,
            )
            if interpreted_business_rules:
                contract = contract.model_copy(update={
                    "interpreted_business_rules": interpreted_business_rules,
                })
        if len(constraint_labels) >= 3 or unsupported_touches:
            # Phase 31 §1: the architect-stage upgrade -- ONLY reached once the baseline's own
            # output already clears this exact gate (the free, always-made baseline call above is
            # unchanged; a simple <3-label, no-unsupported-touch goal never enters this branch, so
            # its own call count is byte-for-byte identical to before this change). Runs Stage A's
            # 2 concurrent, structurally-distinct biased drafts + Stage B's one critic/merge call,
            # then treats the critic's own choice as this task's real decomposition -- replacing
            # constraint_artifacts (and re-deriving constraint_labels from it) before anything
            # below reads either one.
            constraint_artifacts = await maybe_upgrade_decomposition_with_architect_stage(
                contract.goal, constraint_artifacts, client, classifier_model, task_id=task_id,
            )
        # Phase 31 §1.6 (2026-08-08, real bug found and root-caused live -- the project owner's own report,
        # confirmed against real agent_memory_events rows): this call used to live INSIDE the
        # `len(constraint_labels) >= 3 or unsupported_touches` branch above, so a goal that the
        # top-level call itself bundled into fewer than 3 giant constraints -- e.g. an entire
        # three-stage approval workflow returned as ONE constraint with 9 creates -- never once
        # reached Tier-1 recursion at all, because under-splitting at the top level is exactly
        # what produces fewer than 3 labels in the first place. The one case Tier-1 recursion
        # exists to catch was structurally excluded from ever calling it. Now runs unconditionally
        # for every module_development goal -- cheap for the common, genuinely-atomic case
        # (maybe_decompose_piece_further()'s own Tier-0 check makes zero LLM calls for a piece
        # with signal count <=1), and if it's already run above (the >=3-or-unsupported branch),
        # this simply keeps refining the architect stage's own output exactly as before.
        # Phase 31 UI (2026-08-08): split_lineage is the FULL real recursion map (including
        # intermediate labels consumed by a deeper split, never just the final flat items'
        # own immediate parent) -- see recursively_decompose_constraints()'s own docstring.
        pre_recursion_label_count = len(constraint_labels)
        split_lineage: dict[str, str] = {}
        constraint_artifacts = await recursively_decompose_constraints(
            constraint_artifacts, contract.goal, client, classifier_model, task_id=task_id,
            lineage=split_lineage,
        )
        constraint_labels = sort_constraint_labels_by_dependency_tier(
            [item.label for item in constraint_artifacts]
        )
        # A goal that started under the 3-label/unsupported-touch threshold but that Tier-1
        # recursion just found genuine internal structure in must now ALSO take the decomposed,
        # graph-scheduled path below -- otherwise the split it just found would be silently
        # discarded by falling through to the plain, single-constraint _execute_contract() path.
        genuine_split_found_below_threshold = len(constraint_labels) > pre_recursion_label_count
        if len(constraint_labels) >= 3 or unsupported_touches or genuine_split_found_below_threshold:
            # P13 item 4/10(a): real, node-addressable state -- predecessor_labels derived
            # deterministically from creates/requires overlap, falling back per-constraint to the
            # tier-bucket order. Additive: constraint_labels (the list consumed everywhere else
            # below) is completely unchanged; this only populates the new constraint_nodes field.
            new_constraint_nodes = build_constraint_nodes(constraint_artifacts)
            # Phase 31 §1.5: real, artifact-overlap-derived predecessor edges CAN form a cycle
            # (e.g. two constraints each naming an artifact the other requires) -- detected and
            # collapsed into a synthetic linear tier-ordered chain here, once, right after the
            # graph is built, before the scheduler (§2) ever reads predecessor_labels.
            detect_and_collapse_cycles_with_telemetry(new_constraint_nodes, task_id)
            contract = contract.model_copy(update={
                "constraint_nodes": new_constraint_nodes,
            })
            # Real, confirmed UI gap found live (2026-08-08, the project owner's own report: a genuinely
            # in-progress, still-on-round-1 task showed no graph at all) -- see
            # manager/tools.py's own "graph_created" event-type comment for the full incident.
            # Written once, here, immediately after the real graph is built and before round 1 of
            # any node starts -- gives the UI real structure to render from the very first
            # moment, never waiting on a round to fail first.
            append_project_memory(
                event_type="graph_created", actor="manager", task_id=task_id,
                module=module_for_repeat_check,
                summary=f"{len(constraint_labels)} independent constraint(s) decomposed for this task.",
                tags=["graph_created"],
                detail={
                    "constraint_nodes": {k: v.model_dump(mode="json") for k, v in new_constraint_nodes.items()},
                    "planning_round_budget": contract.planning_round_budget,
                    # Phase 31 UI (2026-08-08): the FULL split lineage, including intermediate
                    # labels consumed by a deeper split -- see recursively_decompose_
                    # constraints()'s own docstring. Lets the UI reconstruct real, arbitrary-
                    # depth recursion (a piece visibly branching into its own sub-pieces),
                    # never just a flat list of same-size siblings.
                    "split_lineage": split_lineage,
                },
                verified=True,
            )
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "delegate",
                "message": (
                    f"{len(constraint_labels)} independent constraints detected -- "
                    f"sequencing single-constraint sub-contracts: {constraint_labels}"
                    + (
                        f" ({len(unsupported_touches)} touch(es) an unsupported domain, will be "
                        f"gated individually)" if unsupported_touches else ""
                    )
                ),
                "status": "running",
            })
            return await _run_decomposed_task(
                contract, constraint_labels, module_lock_name, client, classifier_model,
                correction_result, module_for_repeat_check, memory_block, constitution_text,
                sensitive_paths_raw, manager_model=manager_model,
            )

    # Real, general bug found live (2026-08-08, site_50 benchmark task 011) -- see
    # _goal_shape_clarifications()'s own docstring for the full incident: this plain,
    # non-decomposed path (reached by any module_development goal that decomposes into fewer
    # than 3 constraints and touches no unsupported domain -- the common case for a simple,
    # single-ask goal) never got this same guidance before, since it lived only inside
    # _run_constraint_labels_from()'s own per-round loop. Scoped to module_development only,
    # matching that same check's own scope above -- this is purely a code-generation-time Odoo
    # API gotcha, meaningless for any other capability class.
    if contract.capability_class == CapabilityClass.module_development:
        clarifications = _goal_shape_clarifications(contract.goal)
        if clarifications:
            contract = contract.model_copy(update={"goal": contract.goal + clarifications})

    result = await _execute_contract(
        contract, module_lock_name, client, classifier_model, correction_result,
        module_for_repeat_check, memory_block, constitution_text, sensitive_paths_raw,
        manager_model=manager_model,
    )
    # Same fix as _run_decomposed_task()'s own final return below: clear
    # the "running" Redis flag on genuine completion of a plain, non-
    # decomposed task too, since nothing else in the codebase ever did.
    if result.get("status") == "completed":
        clear_task_state(task_id)
        clear_cloud_spend(task_id)
    return result




def _reconstruct_resume_order(
    contract: TaskContract,
) -> tuple[list[str], dict[str, ConstraintState], int] | None:
    """Phase 25A rewrite (2026-07-25): the ORIGINAL version of this
    function (see git history) regex-parsed constraint order back out
    of `goal` prose, specifically because `constraint_status`'s own
    dict key order is not safe to trust after a Postgres jsonb
    round-trip. That workaround is no longer needed: `constraint_order`
    (contracts/schema.py, Phase 25A) is a real, explicit `list[str]`
    field, set once when a decomposed task's sub-contracts are first
    built and carried forward unchanged by every `model_copy()` since --
    JSON/jsonb arrays, unlike JSON objects, DO preserve element order,
    so this field is safe to read directly, with no reconstruction
    logic and no dependency on any particular sentence's wording
    surviving intact in `goal`.

    Returns (full_order, satisfied, start_index) for the remaining
    sequencing loop, or None when this checkpoint predates Phase 25A
    (constraint_order empty -- an old checkpoint written before this
    field existed) or is otherwise unreadable -- caller must then NOT
    finalize the task, same conservative default as before: safer to
    look stuck than to lie about completion.

    Real fix, 2026-08-08 (the project owner's own direct report + live-confirmed bug, task
    657697fc-f701-4932-a172-b0132da93cfa -- "resume tasks to not start from scratch and save
    progress, it should perfectly represent in UI"): `satisfied` used to be derived purely from
    each label's POSITION in `full_order` relative to `current_focus` -- "everything earlier in
    the list is done." But `constraint_order` is only the raw, arbitrary order the decomposition
    happened to enumerate labels in, never a topological/dependency order (confirmed live:
    `service_ticket_model` -- which itself real-depends on `equipment_registry` -- was sorted
    BEFORE it). That silently marked labels "satisfied" purely by list position, whether or not
    they were ever actually built or verified, corrupting every resumed decomposed task's own
    real dependency gating from that point forward -- confirmed live: `ticket_workflow` started
    running while its own real predecessor, `service_ticket_model`, had never been dispatched at
    all and was still `pending`. `read_node_states()` (manager/tools.py) is the one continuously
    live-patched, authoritative record of which labels are genuinely satisfied (the same source
    the UI itself trusts) -- read fresh here instead of inferring anything from list position.
    `start_index` is kept only as the conservative fallback dispatch point when `read_node_states`
    itself returns nothing usable (a genuinely unreadable/pre-migration task) -- real satisfied
    state, when available, is always authoritative over it.
    """
    full_order = contract.constraint_order
    current_focus = contract.current_constraint_label
    if not full_order or current_focus not in full_order:
        return None
    live_states = read_node_states(str(contract.task_id))
    if live_states:
        satisfied: dict[str, ConstraintState] = {
            label: ("satisfied" if live_states.get(label) == "satisfied" else "pending")
            for label in full_order
        }
        # Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run): `start_index`
        # used to be computed purely as `full_order.index(current_focus) + 1` -- a raw POSITION in
        # `constraint_order`, which (per this function's own 2026-08-08 fix above) is only an
        # arbitrary decomposition-time enumeration order, never a dependency/dispatch order. The
        # caller (`resume_task_after_checkpoint`) then treats `start_index >= len(full_order)` as
        # "this was the last constraint, the whole task is done" and calls `clear_task_state()` --
        # a decision made from POSITION even though `satisfied` (right above, real per-label
        # ground truth) was already correctly computed. Confirmed live: `project_ticket_counts`
        # happened to be the LAST label in this task's own `constraint_order` even though THREE
        # other labels (`daily_escalation_cron`, `ticket_workflow_and_logging`,
        # `ticket_bulk_close`) were still genuinely `pending` -- every resume kept re-verifying
        # `project_ticket_counts` alone (it was the one and only sub-contract checkpointed), each
        # pass computed `start_index = len(full_order)`, and the task was finalized as complete
        # (`clear_task_state`) after only 6 of 9 constraints had ever been dispatched, with the
        # remaining 3 never even attempted -- the exact same class of corruption already fixed
        # once for `satisfied` itself (see the 2026-08-08 docstring above), just missed here
        # because `start_index` was a second, separate position-derived value nobody had updated
        # to match. Fixed: `start_index` is now ALSO derived from the same real `satisfied`
        # ground truth -- the index of the first label in `full_order` that is NOT yet satisfied,
        # or `len(full_order)` only when every single label genuinely is. `current_focus`'s own
        # position no longer matters at all once real per-label state is available.
        pending_labels = [label for label in full_order if satisfied[label] != "satisfied"]
        start_index = full_order.index(pending_labels[0]) if pending_labels else len(full_order)
    else:
        # Conservative fallback ONLY -- no real per-label state available at all (e.g. a task
        # predating graph_created telemetry). Never silently claim MORE than position-based
        # certainty in that case; matches the exact prior behavior so an unreadable-state task
        # degrades to "as safe as before," not worse.
        start_index = full_order.index(current_focus) + 1
        satisfied = {
            label: ("satisfied" if i < start_index else "pending")
            for i, label in enumerate(full_order)
        }
    return full_order, satisfied, start_index


async def _run_decomposed_task(
    contract: TaskContract,
    constraint_labels: list[str],
    module_lock_name: str,
    client: ModelGatewayClient,
    classifier_model: str,
    correction_result: dict,
    module_for_repeat_check: str | None,
    memory_block: str,
    constitution_text: str,
    sensitive_paths_raw: list,
    manager_model: str | None = None,
) -> dict:
    """Phase 18 (§22.9): sequences `constraint_labels` as an ORDERED
    series of single-new-constraint sub-contracts, reusing
    _execute_contract() (the same delegate -> verify -> reflect-and-
    retry round loop every other task already goes through) once per
    constraint -- never a second, drifting copy of that logic.

    Deliberately keeps `contract.task_id` UNCHANGED across every
    sub-contract (only `goal`/`constraint_status` differ, via
    model_copy()) -- this is what makes
    specialists.build.slugify_module_name() derive the SAME module name
    for every sub-contract of one decomposed task (it hashes task_id
    into the name), and what makes tools_odoo.module_dev.vcs's
    `task/<task_id>` Gitea branch (§22.4) the same continuous branch
    across sub-contracts too. Safe to reuse: sub-contracts run strictly
    sequentially (each one fully awaited before the next starts), so
    there is no concurrent-write hazard against the Redis task-state key
    that mark_task_running()/set_task_state() key off of.

    Sub-contract N's own constraint_status marks every earlier
    constraint 'satisfied' -- this round's own regression re-check
    (§22.10, reused here rather than duplicated) is what actually
    re-verifies each one still holds -- and the current one 'pending'
    (later ones stay 'pending' too, visible in the ledger (§22.8) but
    not yet the round's own focus).

    Stops and returns immediately on the first sub-contract that pauses
    (sign-off, gateway outage, repeated failure, escalation) or fails to
    pass -- never silently proceeds to the next constraint on top of an
    unresolved one.
    """
    satisfied: dict[str, ConstraintState] = {label: "pending" for label in constraint_labels}
    # Phase 25A (2026-07-25): the authoritative order and the original,
    # un-narrowed goal text are set HERE, exactly once, and carried
    # forward unchanged by every later model_copy() (in
    # _run_constraint_labels_from() below and every resume) -- see
    # contracts/schema.py's own field docstrings for why this replaces
    # the previous regex-reconstruction-from-prose approach.
    contract = contract.model_copy(update={
        "constraint_order": constraint_labels,
        "original_goal": contract.original_goal or contract.goal,
    })
    return await _run_constraint_labels_from(
        contract, constraint_labels, satisfied, 0, module_lock_name, client, classifier_model,
        correction_result, module_for_repeat_check, memory_block, constitution_text,
        sensitive_paths_raw, manager_model=manager_model,
    )


# Real, general bug found live (2026-08-07, task039, HUMAN_DECISION deep-push, real task_id
# 3c317ee7-9934-4b71-a3de-46f534bade49): a goal naming "a <adjective>? button (method_name)" as a
# single deliverable reads as one combined concept to Build, not two separate required pieces (a
# method AND a real view <button> element referencing it) -- see the goal_text append below this
# regex feeds. Matches e.g. "a Mark Refreshed button (action_mark_refreshed)",
# "a Customer Overview smart button ... (action_view_customer_projects)" is deliberately NOT
# matched by this exact shape (no parenthesized name directly after the word "button") -- kept
# narrow and conservative, matching only the confirmed real phrasing shape rather than guessing
# at every possible way a goal might describe a UI action.
_GOAL_BUTTON_METHOD_RE = re.compile(
    r"\b(?:\w+\s+){0,3}button\s*\((\w+)\)", re.IGNORECASE,
)

# Real, general bug found live (2026-08-08, site_50 benchmark task 011, "I need an intermediate
# state called 'Paid' between Invoiced and Done"): Build generated `state = fields.Selection(
# selection_add=[('paid', 'Paid')])` extending an EXISTING (Odoo-core-required) Selection field
# with no `ondelete=` kwarg -- Odoo's own registry-build-time validation (odoo/fields.py) hard-
# rejects this with "required selection fields must define an ondelete policy," a fully
# deterministic, well-documented Odoo API requirement whenever selection_add targets a required
# field, not a subjective judgment call. Confirmed live: the model repeated the IDENTICAL mistake
# across 2 straight rounds (self-detected as `pattern_worth_a_rule` by round 2's own
# classification), never once including `ondelete=`. Matched narrowly on phrasing describing a
# NEW state/stage/status VALUE being added to an EXISTING workflow (never a brand-new field of
# some other type) -- kept conservative, matching only the confirmed real shape.
_GOAL_NEW_WORKFLOW_STATE_RE = re.compile(
    r"\b(?:new|intermediate|extra)\s+(?:state|stage|status)\b|"
    r"\b(?:state|stage|status)\s+called\b",
    re.IGNORECASE,
)


def _goal_shape_clarifications(base_goal_text: str) -> str:
    """Real, general bug found live (2026-08-08, site_50 benchmark task 011): both the
    button/method clarification and the selection_add/ondelete clarification below were
    originally coded ONLY inside `_run_constraint_labels_from()`'s own per-round loop -- reached
    exclusively by tasks that decompose into 3+ constraints (or touch an unsupported domain).
    Confirmed live: task 011 ("I need an intermediate state called 'Paid'...") decomposed into
    exactly 1 constraint (`constraint_nodes: {{}}`, confirmed via direct Postgres read), routing
    straight to the plain `_execute_contract()` call below -- which reads `contract.goal`
    completely unmodified, so the new ondelete= guidance never reached Build's prompt at all,
    and the model repeated the identical mistake in a fresh resubmission after the fix had
    already landed. This is almost certainly ALSO true of the pre-existing button/method fix for
    any 1-2-constraint goal describing a button -- extracted here into one shared function,
    called from BOTH the decomposed-task loop and the plain single-contract path, so every goal
    shape this covers gets the same guidance regardless of how many constraints it decomposes
    into. Returns the text to APPEND to goal_text (empty string if nothing matches) -- never
    mutates its input.
    """
    appended = ""
    button_method_matches = _GOAL_BUTTON_METHOD_RE.findall(base_goal_text)
    if button_method_matches:
        button_method_names = sorted({m for m in button_method_matches})
        appended += (
            f" IMPORTANT, general clarification about every button-shaped deliverable named "
            f"above ({button_method_names!r}): a 'button' requirement is TWO separate, both-"
            f"required pieces, not one -- (1) the underlying Python method, AND (2) a real "
            f"<button type=\"object\" name=\"...\"/> element in a view (added via inherit_id "
            f"+ xpath against a real, existing view for this model -- see this round's own "
            f"real, live view list below if provided). Writing the method alone, with no way "
            f"to trigger it from the UI, does NOT satisfy this requirement -- both pieces "
            f"must exist together in the SAME round that constraint belongs to."
        )
    if _GOAL_NEW_WORKFLOW_STATE_RE.search(base_goal_text):
        appended += (
            f" IMPORTANT, general Odoo requirement: if this task adds a NEW option to an "
            f"EXISTING Selection field via `selection_add=[('new_value', 'Label')]` (e.g. a "
            f"new workflow state/stage/status), you MUST also pass an `ondelete=` dict "
            f"mapping every new option to a cleanup policy, e.g. "
            f"`ondelete={{'new_value': 'set default'}}` -- Odoo's own registry validation "
            f"HARD-REJECTS the install with 'required selection fields must define an "
            f"ondelete policy' whenever the base field turns out to be required and this is "
            f"missing, and providing it is completely harmless even when the field is not "
            f"required. Always include it for any selection_add= you write, never omit it."
        )
    return appended


def detect_and_collapse_cycles_with_telemetry(nodes: dict[str, ConstraintNode], task_id: str) -> None:
    """Phase 31 §1.5/§10: real, artifact-overlap-derived predecessor edges CAN form a cycle
    (e.g. two constraints each naming an artifact the other requires) -- detects every such SCC
    and collapses each into a synthetic linear tier-ordered chain in place, logging
    `graph_cycle_detected` (named exactly per the design document's own telemetry table) once per
    SCC, carrying the offending labels AND the real synthetic edges just inserted. Extracted to a
    standalone function (rather than left inline in run_turn()'s own large body) specifically so
    this real behavior is independently unit-testable -- run_turn() itself is impractical to
    reproduce live end-to-end (see tests/test_constraint_nodes_run_turn_wiring.py's own docstring
    for the same reasoning applied to its neighboring wiring).
    """
    for cyclic_labels in detect_cycle(nodes):
        collapse_cycle_to_tier_chain(nodes, cyclic_labels)
        synthetic_edges = {label: nodes[label].predecessor_labels for label in cyclic_labels}
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"graph_cycle_detected: real predecessor edges formed a cycle among {cyclic_labels!r} -- collapsed to a tier-ordered chain, synthetic edges: {synthetic_edges!r}",
            "status": "running",
        })


def _build_constraint_node_graph_for_scheduling(
    contract: TaskContract,
    constraint_labels: list[str],
    satisfied: dict[str, ConstraintState],
    start_index: int,
) -> dict[str, ConstraintNode]:
    """Phase 31 §2: prefers `contract.constraint_nodes` (the real, artifact-overlap-derived
    graph this module's own decomposition gate builds via `build_constraint_nodes()`) whenever
    its own keys exactly match `constraint_labels` -- falls back to a synthetic LINEAR chain, in
    original `constraint_labels` order, whenever `constraint_nodes` is missing/stale/mismatched
    (a legacy contract from before this field existed, or a direct low-level call the way this
    module's own regression tests make). The fallback reproduces today's exact dispatch order
    byte-for-byte, since a linear chain admits exactly one ready label at a time, in list order.
    """
    real_nodes = contract.constraint_nodes or {}
    if set(real_nodes.keys()) == set(constraint_labels):
        nodes = {label: node.model_copy(deep=True) for label, node in real_nodes.items()}
    else:
        nodes = {
            label: ConstraintNode(
                label=label,
                predecessor_labels=[constraint_labels[i - 1]] if i > 0 else [],
            )
            for i, label in enumerate(constraint_labels)
        }
    # Real fix, 2026-08-08 (the project owner's own direct report + live-confirmed bug, task
    # 657697fc-f701-4932-a172-b0132da93cfa): `i < start_index` used to ALSO mark a label
    # "already done" purely by its position in `constraint_labels`, regardless of what
    # `satisfied` (now the real, authoritative per-label ground truth -- see
    # `_reconstruct_resume_order()`'s own docstring for the full incident) actually says --
    # so a label the caller correctly knows was never built could still get silently marked
    # satisfied here, re-corrupting exactly what the caller just fixed. `satisfied` alone is
    # now the single source of truth for every caller: a fresh run's own `satisfied` (see
    # `_run_decomposed_task()`) is `{label: "pending" for label in constraint_labels}` with
    # `start_index=0` -- `i < 0` was already always False for every real caller, so dropping it
    # here changes nothing for a fresh run and only removes the position-based override that
    # was wrong for a resume.
    for label in constraint_labels:
        already_done = satisfied.get(label) == "satisfied"
        nodes[label].state = ConstraintNodeState.satisfied if already_done else ConstraintNodeState.pending
    return nodes


def _halt_all_remaining_pending_nodes(nodes: dict[str, ConstraintNode]) -> None:
    """Preserves `_run_constraint_labels_from()`'s own historical, deliberate "stop the ENTIRE
    decomposed task on the FIRST non-passing sub-contract" behavior -- every existing regression
    test for this function assumes it. Real graph-based partial-independent-progress (letting a
    genuinely unrelated sibling keep going after a different sibling fails) is left for a later,
    explicitly separate decision -- not silently introduced here as a side effect of adopting the
    scheduler for ORDERING.
    """
    for node in nodes.values():
        if node.state in (ConstraintNodeState.pending, ConstraintNodeState.ready):
            node.state = ConstraintNodeState.blocked


def _compose_focus_goal_text(
    contract: TaskContract, constraint_labels: list[str], focus_label: str,
    satisfied_now: dict[str, ConstraintState], nodes_now: dict[str, ConstraintNode],
) -> tuple[str, dict[str, ConstraintState], list[str]]:
    """Pure goal-text composition for a single constraint's own round, factored out
    (2026-08-10, real fix found live on task e65381cc, equipment_views_menu node, recurring
    identically across MANY straight resumes with zero convergence despite maximally
    explicit resume notes) so it can be called from BOTH a node's first-ever dispatch
    (`execute_node()`, below) AND `_resume_task_after_checkpoint_locked()` -- previously,
    `resume_task_after_checkpoint()` reused `last_contract.goal` UNCHANGED (`resume_update`
    never included `goal`), meaning EVERY resume of an already-dispatched, still-paused node
    kept re-sending the exact same frozen prompt from that node's original dispatch, with
    only a resume `note` appended as one more competing rule among many. Confirmed live: the
    LLM's own output for 'equipment_views_menu' was BYTE-IDENTICAL across 3 consecutive
    resumes despite each one's note being more explicit than the last -- strong evidence the
    frozen original goal text (still describing the FULL, un-narrowed, 3-item original
    request) was the dominant signal, not genuinely un-followable instructions. Re-deriving
    this fresh on every resume, exactly the way a first dispatch already does, means a
    resume's own `note` competes against a goal_text that's ALSO fresh (reflecting anything
    newly satisfied since the original dispatch) instead of a permanently stale one.
    Returns (goal_text, constraint_status, not_yet_labels) -- the exact 3 values the caller
    needs to build its own sub_contract update.
    """
    constraint_status_now: dict[str, ConstraintState] = dict(satisfied_now)
    already_satisfied_now = [label for label, state in satisfied_now.items() if state == "satisfied"]
    base_goal_text = contract.original_goal or contract.goal
    goal_text = (
        f"{base_goal_text}\n\nThis round's own NEW focus is ONLY: {focus_label!r}. "
        f"(This is only this system's own internal tracking name for what to build this "
        f"round -- it is NOT a literal field, variable, or method name you must create, and "
        f"it does not describe the SHAPE of the work either (a label mentioning 'decoration' "
        f"or 'access' or 'relation' does not mean the work is necessarily a new field/method "
        f"of that name -- it could just as easily be a view attribute, a removed CSV row, or "
        f"anything else). Real, confirmed bug found live on a DIFFERENT round of this same "
        f"task: a prior round whose own focus label was quoted here invented an entire unused "
        f"field literally named after that label, purely because the label appeared in this "
        f"sentence, even though the actual requirement needed no such field at all (its own "
        f"generated comment admitted \"this field is not used directly\"). Read the ORIGINAL "
        f"goal text above for what this round literally requires and how to name anything you "
        f"add; never invent a field/method purely to give this round's own label something to "
        f"point at.) "
        f"Add ONLY the code this one constraint strictly requires -- no other fields, "
        f"methods, onchange logic, or security records, even if they seem helpful or "
        f"related. Every extra line you add that nothing exercises will be reported as a "
        f"real, uncovered gap and will fail this round on its own, regardless of code "
        f"quality. This applies to views_xml too: do NOT add view elements referencing "
        f"any field that isn't defined by THIS round's own models_py change -- referencing "
        f"a future constraint's field before it exists is a hard, guaranteed failure, not "
        f"a style choice."
    )
    if already_satisfied_now:
        goal_text += (
            f" The following constraints are ALREADY satisfied by earlier work on this same "
            f"task and MUST continue to hold -- do not remove or weaken them: "
            f"{already_satisfied_now}."
        )
    goal_text += _goal_shape_clarifications(base_goal_text)
    if contract.known_risk_hint:
        goal_text += f"\n\n{contract.known_risk_hint}"
    not_yet_labels_now = [
        label for label in constraint_labels
        if label != focus_label and label not in already_satisfied_now
    ]
    if not_yet_labels_now:
        goal_text += (
            f" The following constraints are NOT yet in scope for this round and must NOT be "
            f"implemented even partially -- no fields, methods, or view elements for any of "
            f"them yet, no matter how related they seem: {not_yet_labels_now}. Each one will "
            f"get its own dedicated round later; adding any of their fields now is a "
            f"guaranteed failure of THIS round, not a head start."
        )
        # Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node,
        # immediately after this same not_yet_in_scope_models mechanism's own first fix): a
        # model can legitimately be `requires`-d by MULTIPLE constraints for entirely different
        # reasons -- e.g. `ticket_access_restriction` requires `oma.service.ticket` because it
        # edits that model's own ir.model.access rows, while THIS round's own focus
        # (`ticket_views_menu`) ALSO legitimately needs `oma.service.ticket` to build its real,
        # in-scope views. The original unconditional union blindly forbade "any view/form/tree/
        # menu/action/field for oma.service.ticket" -- directly contradicting this SAME round's
        # own correct, in-scope work on that exact model, confirmed live via Code-Review
        # correctly refusing the round's own genuinely-required views because the goal text
        # itself said not to touch them. A model this round's own focus constraint also
        # `requires`/`creates` is never actually forbidden -- only an EXCLUSIVELY not-yet-in-
        # scope model (one no in-scope constraint has any claim to) is a real, safe exclusion.
        this_rounds_own_models = set(
            (nodes_now.get(focus_label).requires if nodes_now.get(focus_label) else [])
            + (nodes_now.get(focus_label).creates if nodes_now.get(focus_label) else [])
        )

        def _shares_a_real_model_this_round_already_owns(candidate: str) -> bool:
            # Real, confirmed follow-up bug found live (2026-08-10, same task, same node, same
            # night): `ConstraintNode.requires` doesn't always hold a genuine, bare, real Odoo
            # model name -- `ticket_status_decoration`'s own real requires list included
            # 'oma.service.ticket.status', an internal decomposition-time artifact reference
            # (not a real model at all), which shares a dotted PREFIX with the real
            # 'oma.service.ticket' model this round already legitimately owns. Naming it
            # verbatim in the "do not touch these real models" sentence let Code-Review
            # reasonably (if technically incorrectly) read it as implicating that same real,
            # in-scope model, reproducing the exact self-contradiction the sibling fix above
            # already closed for the bare-name case. Any not-yet-in-scope candidate that shares
            # a dotted prefix with (or is a prefix of) a model this round already owns is
            # excluded the same way an exact match already is.
            return any(
                candidate == owned or candidate.startswith(owned + ".") or owned.startswith(candidate + ".")
                for owned in this_rounds_own_models
            )

        not_yet_in_scope_models = sorted({
            model
            for label in not_yet_labels_now
            for model in (nodes_now.get(label).requires if nodes_now.get(label) else [])
            if not _shares_a_real_model_this_round_already_owns(model)
        })
        if not_yet_in_scope_models:
            goal_text += (
                f" Concretely, this means: do not add or reference ANY view, form, tree, "
                f"menu, action, or field for these real models in this round, even though "
                f"they already exist and their fields are real: {not_yet_in_scope_models}. "
                f"Content for those models belongs entirely to the not-yet-in-scope "
                f"constraints listed above, not this round."
            )
        goal_text += (
            f" If the ORIGINAL goal text above happens to mention any work belonging to "
            f"{not_yet_labels_now}, that mention is describing the OVERALL multi-round plan, "
            f"not something to build in THIS round -- the NOT-yet-in-scope list above "
            f"governs, not the original goal text's wording. Do not build it now just "
            f"because the original goal text names it."
        )
        goal_text += (
            f" This also means: code that OMITS {not_yet_labels_now} in this round is "
            f"CORRECT and COMPLETE for this round's own scope, not a shortfall against the "
            f"original goal. Any reviewer or verifier judging this round's diff must NOT "
            f"flag the absence of {not_yet_labels_now} as a missing requirement, incomplete "
            f"implementation, or goal contradiction -- their own dedicated later round is "
            f"where they belong, and this round is not required (and must not attempt) to "
            f"satisfy them."
        )
    return goal_text, constraint_status_now, not_yet_labels_now



async def _run_constraint_labels_from(
    contract: TaskContract,
    constraint_labels: list[str],
    satisfied: dict[str, ConstraintState],
    start_index: int,
    module_lock_name: str,
    client: ModelGatewayClient,
    classifier_model: str,
    correction_result: dict,
    module_for_repeat_check: str | None,
    memory_block: str,
    constitution_text: str,
    sensitive_paths_raw: list,
    manager_model: str | None = None,
) -> dict:
    """The actual sequencing loop, factored out of `_run_decomposed_task()`
    so BOTH a fresh run (start_index=0, satisfied all "pending") and a
    real resume past a checkpoint (start_index=wherever it left off,
    satisfied reflecting everything already confirmed) drive the exact
    same code -- never two copies that can drift apart.

    Real, severe bug root-caused live (2026-07-12): `resume_task_after_
    checkpoint()` used to call `_execute_contract()` directly for JUST
    the one sub-contract a checkpoint happened to capture, with zero
    decomposition awareness. A checkpoint is only ever written on a
    genuine PauseForOperator -- never on a pass -- so once ANY constraint's
    own round paused mid-decomposition, get_latest_checkpoint() kept
    returning that SAME stale checkpoint on every subsequent /continue
    call forever. Since that constraint was typically one that had
    already been satisfied earlier (or was easy to re-satisfy), it
    would "pass" again and again -- producing a fresh, real, genuine-
    looking `outcome: passed=true` event every single time, with NO
    mechanism to ever advance to the next constraint. Confirmed live: 7
    consecutive "constraint passes" on Operator's real 8-constraint task
    were ALL silently re-verifying the exact same first constraint
    (service_issue_project_link) -- the module on disk after all 7
    still had only the 2 fields from constraint 1, never progressing to
    constraints 2-8 at all, while the dashboard/DB history made it look
    like the task was genuinely closing in on completion. This is a
    plausible, additional explanation (beyond the scaffold-boilerplate
    deadlock) for why the *original* 48-round effort never converged:
    any resume past a real pause could only ever re-loop the same
    already-checkpointed constraint. Fixed by making the resume path
    call this same shared continuation logic instead of a bare
    `_execute_contract()` call.

    P12 Tier A item 20 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
    A Finding 21): real, confirmed gap -- no aggregate round/wall-clock ceiling exists across a
    decomposed task's sub-contracts. Each sub-contract only ever bounds ITSELF (its own
    `_execute_contract()` internally retries up to that sub-contract's own per-item budget
    before genuinely failing/pausing), but nothing caps the SUM across many sub-contracts each
    individually succeeding -- an 8-constraint task where every constraint happens to take its
    own full per-item budget before passing could run unboundedly long in aggregate with no
    ceiling anywhere. Mirrors the identical fix just added to `run_plan()` (the sibling
    multi-item executor with the exact same gap) -- same generous defaults, same posture (meant
    to catch a genuinely runaway task, never to interrupt a normal multi-constraint task working
    as intended).
    """
    sub_results: list[dict] = []
    task_id = str(contract.task_id)
    decomposed_task_start_time = time.monotonic()
    total_sub_contract_rounds_taken = 0
    final_result: dict | None = None
    # Phase 31 §2: replaces this function's own former flat `for index in range(...)` walk.
    # Prefers the real, artifact-overlap-derived graph (contract.constraint_nodes, built by
    # build_constraint_nodes() at manager/loop.py's one decomposition gate) when it matches
    # constraint_labels; falls back to a synthetic linear chain -- in original list order --
    # whenever it doesn't (a legacy contract, or a direct low-level call the way this
    # module's own regression tests make). The fallback reproduces today's exact dispatch
    # order byte-for-byte: a linear chain admits exactly one ready label at a time, in order.
    nodes = _build_constraint_node_graph_for_scheduling(contract, constraint_labels, satisfied, start_index)

    async def execute_node(focus_label: str) -> str:
        nonlocal total_sub_contract_rounds_taken, final_result
        index = start_index + len(sub_results)
        # P12 Tier A item 20: aggregate ceiling check, ahead of starting the next sub-contract
        # -- reuses run_plan()'s own constants/reasoning (defined later in this same module,
        # resolved at call time, not duplicated). A task whose earlier constraints already
        # burned the aggregate budget must not even start the next one.
        decomposed_task_elapsed_seconds = time.monotonic() - decomposed_task_start_time
        if (
            total_sub_contract_rounds_taken >= _DEFAULT_PLAN_ROUND_BUDGET
            or decomposed_task_elapsed_seconds >= _DEFAULT_PLAN_WALL_CLOCK_CAP_SECONDS
        ):
            reason = (
                "decomposed_task_round_budget" if total_sub_contract_rounds_taken >= _DEFAULT_PLAN_ROUND_BUDGET
                else "decomposed_task_wall_clock"
            )
            ceiling_result = {
                "status": "paused",
                "reason": "decomposed_task_budget_exhausted",
                "message": (
                    f"This decomposed task has already used {total_sub_contract_rounds_taken} "
                    f"round(s) across {index} completed sub-contract(s), "
                    f"{decomposed_task_elapsed_seconds:.0f}s elapsed -- the task's own aggregate "
                    f"budget ({_DEFAULT_PLAN_ROUND_BUDGET} rounds / "
                    f"{_DEFAULT_PLAN_WALL_CLOCK_CAP_SECONDS:.0f}s) is exhausted, so the next "
                    f"constraint ({focus_label!r}) was never even started. This is a whole-task "
                    f"ceiling, distinct from any single sub-contract's own per-item budget."
                ),
                "task_id": task_id,
                "contract": contract,
                "correction_result": correction_result,
                "decomposition": {
                    "constraint_labels": constraint_labels,
                    "completed_sub_contracts": index,
                    "sub_result_count": len(sub_results),
                    "ceiling_reason": reason,
                },
            }
            sub_results.append(ceiling_result)
            final_result = ceiling_result
            _halt_all_remaining_pending_nodes(nodes)
            return "failing"

        goal_text, constraint_status, not_yet_labels = _compose_focus_goal_text(
            contract, constraint_labels, focus_label, satisfied, nodes,
        )
        # Phase 31 §6(a): a per-node ISOLATED snapshot, frozen at dispatch time -- read ONCE,
        # here, before this node's own generation round ever starts, never re-read live mid-
        # round. `read_last_validated_commit()` is vcs.py's own synchronous (httpx.Client)
        # function -- run via asyncio.to_thread so this coroutine never blocks the event loop,
        # matching resume_task_after_checkpoint()'s own established pattern for the same call.
        # None (a genuine Gitea outage, or a real round-1 task with nothing committed yet) is a
        # valid, honest result -- specialists/build/specialist.py's own is_retry branch already
        # treats a None/empty frozen snapshot as "fall back to the live read," never a hard
        # failure of this round.
        from tools_odoo.module_dev.vcs import read_last_validated_commit

        frozen_old_files_by_relpath = await asyncio.to_thread(read_last_validated_commit, task_id)
        sub_contract = contract.model_copy(update={
            "goal": goal_text,
            "frozen_old_files_by_relpath": frozen_old_files_by_relpath,
            "constraint_status": constraint_status,
            # Phase 25A: the same focus/remaining-labels facts just
            # rendered into goal_text above, ALSO set as real structured
            # fields on the contract itself -- produced together, from
            # the same source data, so they can never drift apart from
            # what the LLM was actually told.
            "current_constraint_label": focus_label,
            "remaining_constraint_labels": not_yet_labels,
            # P12 Tier A item 21 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_
            # 2026-07-30.md §4, A Finding 24): real, confirmed gap -- this update dict never
            # cleared goal_facts, so whatever was extracted for an EARLIER constraint's own
            # goal text silently carried forward onto this constraint's differently-focused
            # sub_contract. The extraction guard elsewhere (`and not contract.goal_facts:`)
            # then never re-fires for the rest of this task, since a resumed/later
            # sub-contract always already has SOME non-empty goal_facts from an earlier round
            # -- meaning every constraint after the first was silently checked against the
            # FIRST constraint's own extracted facts, not its own. Cleared here so extraction
            # genuinely re-runs against each sub-contract's own real, current goal_text.
            "goal_facts": {},
            # Real, confirmed gap (2026-08-07, HUMAN_DECISION escalation push, task019, real
            # task_id 0c2d4660-5dee-4cef-9468-c09b291b233e): the SAME "stale carry-forward across
            # a constraint transition" bug class as goal_facts above, for a field that never got
            # the same fix -- previous_round_raw_failure_text is EARLIER constraint X's own raw
            # rejection text (e.g. "['action_create_batch_invoice'] ... belongs to a LATER round's
            # own constraint, not this one"), rendered verbatim into the NEXT sub-contract's own
            # <previous_attempt_errors> block even though the round has now genuinely moved on to
            # a DIFFERENT constraint Y that may require the exact thing X's own message told the
            # model to remove. Confirmed live: round 2's own real focus was 'invoice_action_return'
            # (a later constraint requiring action_create_batch_invoice to exist), but round 1's
            # stale "remove the extra method(s)" rejection was still shown as this round's own
            # feedback, directly contributing to the model omitting the now-required method
            # entirely. Cleared here for the same reason goal_facts is: a new constraint's own
            # first attempt has no real prior-attempt feedback of its own yet.
            "previous_round_raw_failure_text": "",
            # Phase 31 §10: a fresh correlation_id for THIS node dispatch -- never reused from
            # the parent task, never reused across a retry of the same label (each call to
            # execute_node(focus_label) mints its own, even on a re-dispatch after
            # unblock_transitive_dependents()). parent_correlation_id records the parent task's
            # own identity so a trace consumer can walk back from any one node to its task.
            "correlation_id": str(uuid.uuid4()),
            "parent_correlation_id": contract.correlation_id or task_id,
        })
        # P12 Tier A item 23 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
        # §4): real, confirmed gap -- check_hard_governance_gates() only ever ran once, at
        # run_turn()'s own intake, against the whole original goal text. A later sub-contract's
        # own narrowed goal_text is genuinely different text (this round's own focus label,
        # not-yet-in-scope constraints, etc.) -- if its own drift happens to match a hard
        # governance rule's structured predicate in a way the original, differently-worded goal
        # never did, nothing would ever catch it once past intake. Deterministic, no LLM call
        # (mirrors the intake-time gate's own "before ANY LLM call this round would otherwise
        # make" discipline), checked before the LLM-backed unsupported-domain check right below.
        #
        # Real, live-confirmed bug (2026-08-08, Phase 31 §9 Phase B concurrency experiment):
        # `goal_text` at this point already has `contract.known_risk_hint` folded in (above) --
        # a VERBATIM prior-round failure summary, which can itself contain arbitrary, unrelated
        # diagnostic noise (a real install log tail can quote Odoo's own internal warnings about
        # completely different modules, e.g. "account.bank.statement.line: inconsistent
        # compute_sudo..."). Confirmed live: this exact substring, inherited from an EARLIER,
        # unrelated attempt's own install-failure log, tripped the "never touch the accounting
        # module" governance rule for a task that never once mentioned accounting itself. The
        # governance check's own stated purpose is to catch THIS ROUND'S real scope drifting into
        # a gated domain -- not to re-litigate whatever incidental text a past failure's raw log
        # happened to quote. Checked against goal_text with the risk-hint contribution stripped
        # back out (exact substring removal, not a re-derivation) -- every other real scope-
        # defining sentence (focus label, already-satisfied/not-yet-in-scope lists, button
        # clarification) still participates in this check unchanged.
        goal_text_for_governance_check = (
            goal_text.replace(f"\n\n{contract.known_risk_hint}", "", 1)
            if contract.known_risk_hint else goal_text
        )
        governance_block = check_hard_governance_gates(goal_text_for_governance_check)
        if governance_block is not None:
            governance_block["task_id"] = task_id
            governance_block["contract"] = sub_contract
            governance_block["correction_result"] = correction_result
            governance_block["decomposition"] = {
                "constraint_labels": constraint_labels,
                "completed_sub_contracts": index,
                "sub_result_count": len(sub_results),
            }
            append_project_memory(
                event_type="decision", actor="manager", task_id=task_id, module=module_for_repeat_check,
                summary=governance_block["message"], tags=["governance_rule_blocked", "per_sub_contract"],
                detail={"matched_rule_id": governance_block["matched_rule_id"], "goal": goal_text},
                verified=True,
            )
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "delegate",
                "message": governance_block["message"], "status": "paused",
            })
            sub_results.append(governance_block)
            final_result = governance_block
            _halt_all_remaining_pending_nodes(nodes)
            return "failing"
        # Phase 30, P0 (Phase K, §14): re-checked HERE, on this specific
        # sub-contract's own narrowed goal_text (not the whole original
        # goal, and not a snippet correlated back from the earlier
        # whole-goal pre-check -- goal_text already states "this round's
        # own NEW focus is ONLY: <focus_label>", so a fresh classification
        # against it is a direct, reliable read of just this one piece,
        # never a guess at which label an earlier snippet belongs to).
        # Only reached when the whole-goal pre-check in run_turn() found
        # at least one touch somewhere in the task, so most tasks never
        # pay this extra call at all.
        sub_touches = await classify_unsupported_domain_touches(
            goal_text, client, classifier_model, task_id=task_id,
        )
        if sub_touches:
            domain_names = sorted({t["domain"] for t in sub_touches})
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "delegate",
                "message": (
                    f"Sub-contract {index + 1}/{len(constraint_labels)} ({focus_label}) needs "
                    f"{', '.join(domain_names)}, which this system doesn't support generating yet -- "
                    f"pausing this piece for a human decision."
                ),
                "status": "paused",
            })
            unsupported_result = {
                "status": "paused",
                "reason": "unsupported_domain",
                "message": (
                    f"Sub-contract {index + 1}/{len(constraint_labels)} of this task ({focus_label!r}) "
                    f"needs {', '.join(domain_names)}, which this system has no real generation "
                    f"support for yet -- not attempted, so none of it is silently best-effort code "
                    f"with no validators or verification behind it. Real snippet(s) from the goal: "
                    f"{[t['goal_snippet'] for t in sub_touches]}. "
                    + (
                        f"{index} earlier sub-contract(s) in this same task already completed "
                        f"normally." if index > 0 else
                        "This was the first sub-contract in this task's own sequence."
                    )
                ),
                "task_id": task_id,
                "contract": sub_contract,
                "correction_result": correction_result,
                "decomposition": {
                    "constraint_labels": constraint_labels,
                    "completed_sub_contracts": index,
                    "sub_result_count": len(sub_results),
                    "unsupported_domain_touches": sub_touches,
                },
            }
            sub_results.append(unsupported_result)
            final_result = unsupported_result
            _halt_all_remaining_pending_nodes(nodes)
            return "failing"
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "delegate",
            "message": f"Sub-contract {index + 1}/{len(constraint_labels)}: {focus_label}",
            "status": "running",
        })
        result = await _execute_contract(
            sub_contract, module_lock_name, client, classifier_model, correction_result,
            module_for_repeat_check, memory_block, constitution_text, sensitive_paths_raw,
            manager_model=manager_model,
        )
        sub_results.append(result)
        # P12 Tier A item 20: accumulated toward the aggregate ceiling checked above,
        # regardless of this sub-contract's own outcome.
        total_sub_contract_rounds_taken += result.get("rounds_taken", 0)
        if result.get("status") != "completed" or not result.get("passed"):
            result["decomposition"] = {
                "constraint_labels": constraint_labels,
                "completed_sub_contracts": index,
                "sub_result_count": len(sub_results),
            }
            final_result = result
            _halt_all_remaining_pending_nodes(nodes)
            return "failing"
        satisfied[focus_label] = "satisfied"
        return "satisfied"

    # Phase B (2026-08-09), the project owner's own explicit request: real, bounded concurrency -- "maybe
    # two things at a time. I don't want more." `max_concurrent=2` here is real, but
    # `run_graph_scheduler()` itself hard-clamps to `OMA_GRAPH_HARD_MAX_CONCURRENT_NODES` (also 2)
    # regardless of this value, so this can never silently regress into unbounded concurrency even
    # if this call site is edited carelessly later. `force_allow_concurrency=True` is safe now:
    # both prerequisites this flag exists to gate are landed and tested -- (1) per-node branch
    # isolation + serialized-apply write layer (`_CONCURRENT_WRITE_ISOLATION_LANDED`, verified live
    # per docs/reports/PHASE31_FULL_IMPLEMENTATION_EVIDENCE_2026-08-07.md §4), and (2) this
    # session's own two closing gaps: target-model-prefix conflict detection in
    # `_admit_from_wave()` (30/30 tests, tests/test_graph_scheduler.py) and the DB-install
    # serialization lock (`infra/fencing.py`'s `acquire_db_install_lock()`, 12/12 tests,
    # tests/test_fencing.py) -- so two sibling nodes can never race on the same target Odoo model,
    # the same generated files, or the same physical database's install/registry-bootstrap step.
    # LLM-call concurrency itself is bounded separately and already: Build's coder backend
    # (`infra/gateway_client.py`'s `_CODER_BACKEND_MAX_CONCURRENT = 1`) and the broader
    # `OMA_LOCAL_INFERENCE_MAX_CONCURRENT` semaphore already queue a second node's generation calls
    # behind the first whenever the backend is genuinely busy, and release it the moment a slot
    # frees -- exactly the "queue when GPU is busy, resume when free" behavior asked for, with no
    # new GPU-load-sensing code needed on top of it.
    await run_graph_scheduler(
        nodes, execute_node, task_id=task_id,
        max_concurrent=2, force_allow_concurrency=True,
    )

    # Phase 31 §2: this function's own historical, deliberate "stop the ENTIRE decomposed
    # task on the FIRST non-passing sub-contract" behavior is preserved via
    # _halt_all_remaining_pending_nodes() above -- final_result, once set, is always the
    # right thing to return, regardless of what the scheduler itself did with any other
    # node's own state afterward.
    if final_result is not None:
        return final_result

    final = sub_results[-1]
    final["decomposition"] = {
        "constraint_labels": constraint_labels,
        "completed_sub_contracts": len(constraint_labels),
        "sub_result_count": len(sub_results),
    }
    # Real bug found live (2026-07-12): manager.gateway_orchestration's
    # mark_task_running() sets oma:task:<id>:state to "running" with a
    # 24h TTL at the start of EVERY sub-contract's own round -- but
    # nothing anywhere ever transitioned or cleared it at genuine
    # completion (clear_task_state() existed in manager/task_state.py
    # but had zero callers in the whole codebase). Confirmed live: a
    # task that had genuinely, fully finished all 8 constraints would
    # still show "running" in Redis for up to 24h afterward, which is
    # exactly why manager.dashboard._standalone_task_status() had to be
    # taught to distrust a stale "running" flag against a durable
    # outcome row (see that fix's own comment) -- but that fix alone
    # would have made a truly-finished decomposed task hang in
    # "running" forever if this call weren't added too. Cleared here,
    # the one place that's unambiguously "every constraint really is
    # satisfied," not any earlier sub-contract's own intermediate pass.
    clear_task_state(task_id)
    clear_cloud_spend(task_id)
    return final


# See resume_task_after_checkpoint()'s own lock-acquisition comment below for the full
# rationale: short enough that a killed/crashed resume self-heals within roughly two minutes
# (not up to the old fixed 3600s), long enough that the renewal interval below never races a
# real, momentary Redis hiccup into a false expiry while the resume is still genuinely alive.
_RESUME_LOCK_TTL_SEC = 120
_RESUME_LOCK_RENEW_INTERVAL_SEC = 30


async def resume_task_after_checkpoint(
    task_id: str,
    client: ModelGatewayClient,
    classifier_model: str,
    note: str | None = None,
    manager_model: str | None = None,
) -> dict:
    """Phase 17 (§21.5.4): the real Continue mechanism -- resumes the
    SAME task_id after a round-budget escalation, never a new one (the
    concrete difference from Replay, which always starts fresh). Skips
    Phase 1-4 contract creation entirely (correction-detection and
    capability classification don't need to re-run for a genuine
    continuation of the same task) and goes straight into the round
    loop from a reconstructed contract.

    The one and only compression trigger point (§21.5.4): estimates the
    token cost of the round history against the Manager's own model
    window and, if over the soft threshold, carries forward only the
    cold-tier human_summary instead of the raw rounds (logged per
    §21.6). contract.goal and every one of Operator's own clarifications are
    NEVER compressed, regardless of size -- only old, already-resolved
    round-by-round technical detail is ever eligible.
    """
    # Real, atomic idempotency guard -- found necessary live (Phase 17 QA
    # pass): two concurrent /continue calls on the same task_id (a real
    # double-click, a network retry, or two people both trying the new
    # feature on the same task) previously ran two full, independent
    # round loops in parallel against the same task, each writing its
    # own "resumed"/outcome/success_digest -- confirmed live, a real bug,
    # not a hypothetical. SET NX is a genuine atomic Redis primitive
    # (not a plain read-then-write, which would still race); only the
    # first concurrent caller acquires it, every other one is refused
    # cleanly with a real, honest message rather than silently doubling
    # the work.
    # Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    # project_ticket_counts node): the old fixed `ex=3600` TTL assumed the `finally: r.delete(...)`
    # below would always eventually run to release the lock -- but an external wrapper killing
    # this process (e.g. a shell `timeout`, which sends SIGTERM) terminates the interpreter
    # immediately under Python's own default SIGTERM disposition, WITHOUT running any `finally`
    # block at all. Confirmed live, repeatedly: every such kill left the lock orphaned for the
    # FULL remaining hour, with no way to distinguish "still genuinely working" from "the process
    # is long dead" from the lock's own state -- forcing a manual choice between waiting out the
    # full TTL or bypassing the lock entirely (neither acceptable). A short, actively-renewed
    # lease (heartbeat pattern) resolves this cleanly: a genuinely alive resume keeps its own
    # lock fresh indefinitely (however many rounds it takes), while a killed/crashed one stops
    # renewing and the lock self-heals within `_RESUME_LOCK_TTL_SEC`, not up to an hour.
    r = get_redis_client()
    lock_key = f"oma:resume_lock:{task_id}"
    if not r.set(lock_key, "1", nx=True, ex=_RESUME_LOCK_TTL_SEC):
        return {
            "status": "error", "task_id": task_id,
            "message": f"Task {task_id} is already being resumed by another request -- refusing to start a second, concurrent round loop.",
        }

    async def _renew_lock_while_alive() -> None:
        while True:
            await asyncio.sleep(_RESUME_LOCK_RENEW_INTERVAL_SEC)
            r.expire(lock_key, _RESUME_LOCK_TTL_SEC)

    renew_task = asyncio.create_task(_renew_lock_while_alive())
    try:
        return await _resume_task_after_checkpoint_locked(task_id, client, classifier_model, note, manager_model)
    finally:
        renew_task.cancel()
        r.delete(lock_key)


async def _resume_task_after_checkpoint_locked(
    task_id: str, client: ModelGatewayClient, classifier_model: str, note: str | None,
    manager_model: str | None = None,
) -> dict:
    checkpoint = get_latest_checkpoint(task_id)
    if checkpoint is None:
        # Real, general race found live (2026-07-11): store_pending_escalation()
        # (called from the PauseForOperator handler below) makes /api/tasks show
        # "paused" IMMEDIATELY, but the round_checkpoint row this function
        # actually needs is only written AFTER a real LLM call
        # (summarize_escalation_for_operator()) that runs in between -- so any
        # caller (human or automated) hitting Continue in that window got a
        # false "nothing to resume" error even though the task genuinely was
        # mid-escalation, not "never escalated." Confirmed live: 3 consecutive
        # automated retries all hit this before the checkpoint existed, then
        # the 4th (after the LLM summary finished) found it fine. Distinguish
        # the two real cases instead of failing both identically: if a
        # pending escalation exists for this task_id (the Redis marker
        # store_pending_escalation() sets), the checkpoint is very likely
        # still being written -- wait briefly and re-check rather than
        # failing a genuinely-in-flight escalation.
        is_mid_escalation = any(
            e.get("task_id") == task_id for e in list_pending_escalations()
        )
        if is_mid_escalation:
            for _ in range(20):  # ~40s max -- summarize_escalation_for_operator() is one LLM call
                await asyncio.sleep(2)
                checkpoint = get_latest_checkpoint(task_id)
                if checkpoint is not None:
                    break
    if checkpoint is None:
        return {
            "status": "error", "task_id": task_id,
            "message": f"No round_checkpoint found for task {task_id} -- nothing to resume.",
        }

    detail = checkpoint["detail"]
    human_summary = detail["human_summary"]
    last_contract = TaskContract.model_validate(detail["last_contract"])
    clarifications = list_clarifications_since(task_id, checkpoint["id"])

    round_history_tokens = estimate_tokens(human_summary) + sum(
        estimate_tokens(r.get("verification_result", {}).get("notes", "")) for r in detail.get("prior_rounds", [])
    )
    manager_window = MODEL_CONTEXT_WINDOWS.get(
        os.environ.get("OMA_MODEL_MANAGER", "qwen3.6-27b"), UNKNOWN_MODEL_WINDOW_FALLBACK,
    )
    pressure = round_history_tokens / manager_window if manager_window else 0.0

    new_rules = [f"Original attempt summary: {human_summary}"]
    if pressure < SOFT_TRIGGER_FRACTION:
        # Under budget -- the warm tier: carry the raw, real round detail
        # forward verbatim, not just the summary.
        for r in detail.get("prior_rounds", []):
            vr = r.get("verification_result", {})
            new_rules.append(f"Round {r.get('round_number')}: {vr.get('notes', '')}")
    else:
        append_project_memory(
            event_type="note", actor="manager", task_id=task_id, module=None,
            summary=f"Round history compressed on resume ({len(detail.get('prior_rounds', []))} round(s) dropped)",
            tags=["round_history_compressed"],
            detail={"dropped_round_count": len(detail.get("prior_rounds", [])), "summary_used": human_summary},
            verified=True,
        )
    new_rules.extend(f"Operator: {c}" for c in clarifications)
    if note:
        # Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
        # project_ticket_counts node): a human's own explicit resume-time correction landed as a
        # plain ordinary rule, competing with up to 8 capped, mostly-superseded auto-generated
        # round summaries in split_critical_rules()'s own "General history" bucket (see that
        # function's docstring one screen up) -- never the dedicated, prioritized "CRITICAL FIXES
        # REQUIRED THIS ROUND" section Build's own prompt actually highlights. Confirmed live:
        # six consecutive resumes with increasingly explicit, code-literal notes produced
        # byte-identical failures, because the note was structurally diluted, not because Build
        # ignored it outright. A human explicitly resuming a paused task with guidance IS exactly
        # the "confirmed, resolved correction, act on this over general history" shape
        # CRITICAL_RULE_PREFIX exists for -- arguably more authoritative than the internal
        # bounded-recovery notes that already get this treatment (see the reconsider/focus-note
        # call sites elsewhere in this file).
        new_rules.append(f"{CRITICAL_RULE_PREFIX}Operator: {note}")

    resume_update = {
        "rules": new_rules,
        "resumed_from_checkpoint_count": last_contract.resumed_from_checkpoint_count + 1,
    }
    # Real, confirmed bug found live: last_contract can be whatever
    # contract the escalating round itself happened to be running --
    # e.g. mid-bounce through a code_review-primary detour round when
    # it escalated, with capability_class=readonly_investigation and a
    # diff_module: input that mean nothing for the task's real shape.
    # Resuming inherited and PERPETUATED that corruption for the whole
    # new round budget, since nothing else in the resumed run had any
    # other reference point to correct it against. task_created's own
    # capability_class, written once at original Phase 3 classification
    # and never touched by mid-loop routing, is the one reliable ground
    # truth -- always resume from the task's real primary specialist,
    # never from whatever detour it happened to be on when it gave up.
    original_capability_class = get_original_capability_class(task_id)
    if original_capability_class and last_contract.capability_class.value != original_capability_class:
        new_specialist_type, new_validation_by = select_specialist_and_validator(original_capability_class)
        resume_update["capability_class"] = CapabilityClass(original_capability_class)
        resume_update["specialist_type"] = new_specialist_type
        resume_update["validation_by"] = new_validation_by
        resume_update["inputs"] = []
    # Real, confirmed bug found live (2026-08-08, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    # flagship run, service_ticket_model node): this used to carry `last_contract.
    # frozen_old_files_by_relpath` forward unchanged -- the per-node snapshot taken ONCE at this
    # node's ORIGINAL dispatch time (see _run_constraint_labels_from()'s own comment: "read ONCE,
    # here, before this node's own generation round ever starts, never re-read live mid-round").
    # When a round fails with apply_node_result_to_module()'s own "genuinely diverged since this
    # node's own dispatch-time snapshot" conflict, its own error message explicitly promises
    # "Retrying will take a FRESH snapshot" -- a promise this function never actually kept: every
    # resume kept reusing the SAME stale snapshot from the original dispatch, so once the task
    # branch's real last-validated-commit had genuinely moved on, EVERY subsequent resume hit the
    # identical conflict again, forever -- confirmed live, the exact same conflict recurred
    # across two consecutive resumes. Re-reading it fresh here, the same way
    # _run_constraint_labels_from() does at original dispatch time, actually delivers on that
    # promise.
    from tools_odoo.module_dev.vcs import read_last_validated_commit

    resume_update["frozen_old_files_by_relpath"] = await asyncio.to_thread(
        read_last_validated_commit, task_id,
    )
    # Real, confirmed bug found live (2026-08-10, task e65381cc, equipment_views_menu node): this
    # function used to leave `goal` out of `resume_update` entirely, meaning every resume of an
    # already-dispatched, still-paused decomposed-task node kept resending the EXACT SAME frozen
    # goal_text from that node's original dispatch through `_run_constraint_labels_from()`'s own
    # `execute_node()` -- forever, no matter how many rounds passed or how much a resume `note`
    # (added to `rules`, above) tried to correct it. Confirmed live: the LLM's own generated
    # output was BYTE-IDENTICAL across 3 consecutive resumes despite each resume's note being
    # more explicit than the last, strong evidence the frozen original goal text (still
    # describing the full, un-narrowed, multi-item original request) was the dominant signal, a
    # genuine pipeline gap rather than an unfixable model limitation. Re-derives the SAME way a
    # fresh dispatch already does (`_compose_focus_goal_text()`, shared with `execute_node()`)
    # whenever this is genuinely a decomposed-task resume (`current_constraint_label` set) --
    # a non-decomposed task's `goal` is a plain, already-correct string with nothing to
    # re-derive, so this is skipped for that shape entirely, never guessed at.
    if last_contract.current_constraint_label:
        resume_nodes = _build_constraint_node_graph_for_scheduling(
            last_contract, last_contract.constraint_order, last_contract.constraint_status, 0,
        )
        resumed_goal_text, _, _ = _compose_focus_goal_text(
            last_contract, last_contract.constraint_order, last_contract.current_constraint_label,
            last_contract.constraint_status, resume_nodes,
        )
        resume_update["goal"] = resumed_goal_text
    new_contract = last_contract.model_copy(update=resume_update)

    append_project_memory(
        event_type="branch_message", actor="manager", task_id=task_id, module=None,
        summary="Resumed after Operator's decision to continue.", tags=["resumed"],
        detail={"role": "manager", "kind": "resumed", "text": "Resumed after Operator's decision to continue."},
        verified=True,
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "branch_message", "status": "passed",
        "message": "Resumed after Operator's decision to continue.",
        "branch_message": {"role": "manager", "kind": "resumed", "text": "Resumed after Operator's decision to continue."},
    })

    # Same real module-identity source as the original run (Phase 1's
    # own module_for_repeat_check), read back from the checkpointed
    # contract rather than re-derived, so a resumed task keeps locking
    # and repeated-failure tracking against the exact same module.
    module_for_repeat_check = detail.get("module_for_repeat_check")
    memory_rows = read_project_memory(tags=None, limit=50)
    selector = ContextSelector()
    assembled = selector.select(candidate_rows=memory_rows, history=[], memory_query_ran=True)
    formatter = ContextFormatter()
    memory_block = formatter.format_system_block(assembled)
    constitution_text = load_manager_constitution()
    sensitive_paths_raw = load_sensitive_paths()
    module_lock_name = module_for_repeat_check or f"task:{task_id}"

    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "delegate",
        "message": f"Resuming (continuation #{new_contract.resumed_from_checkpoint_count})", "status": "running",
    })
    # See _terminal_node_state_for_result()'s own docstring for the real live-reported bug this
    # closes: this direct single-constraint call bypasses run_graph_scheduler() entirely, so
    # without this, the resumed node's graph status never moves off whatever it was before resume.
    _publish_node_state_changed(task_id, new_contract.current_constraint_label, "running")
    result = await _execute_contract(
        new_contract, module_lock_name, client, classifier_model, {"status": "not_a_correction"},
        module_for_repeat_check, memory_block, constitution_text, sensitive_paths_raw,
        manager_model=manager_model,
    )
    _terminal_state = _terminal_node_state_for_result(result)
    if _terminal_state:
        _publish_node_state_changed(task_id, new_contract.current_constraint_label, _terminal_state)

    # Real, severe bug root-caused live (2026-07-12): this function used
    # to return `result` right here, unconditionally -- for a decomposed
    # task (new_contract.constraint_status non-empty), a checkpoint is
    # only ever captured for the ONE sub-contract that happened to be
    # paused when escalation fired. A genuine pass never writes a new
    # checkpoint, so every subsequent /continue call kept re-reading
    # that SAME stale checkpoint and re-running the SAME already-
    # satisfied constraint forever, with no way to ever reach the next
    # one. Confirmed live: 7 consecutive "constraint passes" on a real
    # 8-constraint task were all silently re-verifying constraint 1 --
    # the module on disk never grew past its own first field. Fixed:
    # on a genuine pass for a decomposed sub-contract, figure out which
    # constraint this was (read directly from
    # `new_contract.current_constraint_label`, Phase 25A -- see
    # contracts/schema.py) and, if further constraints remain pending,
    # continue the SAME sequencing loop
    # `_run_decomposed_task()` itself uses (`_run_constraint_labels_from()`)
    # rather than stopping here. If MORE constraints remain and one of
    # them pauses again, `_execute_contract()` writes its own fresh,
    # correctly-scoped checkpoint for THAT constraint, same as normal --
    # this fix only closes the gap where a genuine pass had nowhere to
    # hand off to.
    if (
        result.get("status") == "completed"
        and result.get("passed")
        and new_contract.constraint_status
    ):
        # Real, severe bug found live (2026-07-12, same task, right after
        # deploying the fix above): `list(new_contract.constraint_status.
        # keys())` was used as the CANONICAL ORDER of remaining
        # constraints -- but new_contract is read back from a Postgres
        # `jsonb` checkpoint column, and jsonb does NOT preserve object
        # key insertion order (confirmed live: a checkpoint written with
        # constraint_status keys in decomposition order came back as
        # ['issue_attachments', 'issue_title_field', 'issue_product_link',
        # 'issue_status_field', 'issue_priority_field',
        # 'issue_description_field', 'project_multiple_issues',
        # 'service_issue_project_link'] -- a completely different order).
        # Consequence: after constraint 2 ("project_multiple_issues")
        # genuinely passed, `current_index = constraint_labels.index(...)`
        # landed on 6 (not the true 1), making "service_issue_project_link"
        # -- already satisfied by constraint 1 -- look like the one
        # remaining constraint. It trivially re-passed (nothing left to
        # add), which then looked like the genuine LAST constraint,
        # firing clear_task_state() and marking the whole 8-constraint
        # task "completed" after only 3 of 8 had actually run.
        #
        # Phase 25A rewrite (2026-07-25): the original fix here recovered
        # order by regex-parsing it back out of `new_contract.goal`
        # prose -- fragile (coupled to exact sentence wording, and
        # duplicated across 3 files that each needed their own copy of
        # the same regex). `new_contract.constraint_order` (contracts/
        # schema.py) is now a real, explicit `list[str]` field set once
        # by `_run_decomposed_task()` and carried forward unchanged by
        # every `model_copy()` since, including through this exact
        # checkpoint round-trip -- JSON arrays (unlike JSON objects)
        # preserve order, so no reconstruction from prose is needed at
        # all; `_reconstruct_resume_order()` now just reads it directly.
        reconstructed = _reconstruct_resume_order(new_contract)
        if reconstructed is not None:
            full_order, satisfied, start_index = reconstructed
            if start_index < len(full_order):
                # Phase 25A: the base (unnarrowed) goal text is now read
                # directly from new_contract.original_goal (set once,
                # never touched by narrowing) instead of string-splitting
                # new_contract.goal on the narrowing sentence's own exact
                # wording.
                base_contract = new_contract.model_copy(
                    update={"goal": new_contract.original_goal or new_contract.goal}
                )
                return await _run_constraint_labels_from(
                    base_contract, full_order, satisfied, start_index,
                    module_lock_name, client, classifier_model, {"status": "not_a_correction"},
                    module_for_repeat_check, memory_block, constitution_text, sensitive_paths_raw,
                    manager_model=manager_model,
                )
            # This genuinely was the LAST constraint -- the whole
            # decomposed task is done, same as _run_constraint_labels_from()'s
            # own final return.
            clear_task_state(task_id)
            clear_cloud_spend(task_id)
        # reconstructed is None: order/completion could not be safely
        # determined (e.g. no "NOT yet in scope" marker and other labels
        # remain unaccounted for) -- deliberately do NOT clear_task_state
        # or otherwise finalize here. Redis stays "running" (set at this
        # round's own start), so the dashboard's own outcome-row check
        # (§ fix 4/5) correctly keeps showing this as in-progress rather
        # than falsely "completed" -- safer to look stuck than to lie.
    return result


def _commit_content_still_compiles(files_by_relpath: dict[str, str]) -> tuple[bool, str]:
    """Phase 30, P2 (§9), technical design step 2: "a resumed round must
    re-verify (not trust) the checked-out commit's content actually
    still compiles/parses before continuing -- a checkpoint taken
    mid-write is a real possible edge case." A plain, local, no-sandbox-
    needed check: every `.py` file must parse as valid Python (ast.parse
    -- the same real, minimal bar `promote_pending_validator.py` already
    uses for the same purpose elsewhere in this codebase); every `.xml`
    file must be well-formed (ElementTree.fromstring). Never claims the
    module INSTALLS cleanly -- that's what the resumed round's own,
    normal verification step already re-checks anyway; this is only the
    narrower, cheaper "not obviously corrupted mid-write" guard the
    plan asks for.
    """
    import ast
    import xml.etree.ElementTree as ET

    for relpath, content in files_by_relpath.items():
        if relpath.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as exc:
                return False, f"{relpath} does not parse as valid Python: {exc}"
        elif relpath.endswith(".xml"):
            try:
                ET.fromstring(content)
            except ET.ParseError as exc:
                return False, f"{relpath} is not well-formed XML: {exc}"
    return True, ""


async def resume_orphaned_task_at_startup(
    task_id: str,
    client: ModelGatewayClient,
    classifier_model: str,
    manager_model: str | None = None,
) -> dict:
    """Phase 30, P2 (§9, closes Problem F): resumes a task orphaned by an
    external service restart from its last durable state instead of the
    prior behavior (mark orphaned, discard, task lost -- 271 of 748 real
    tasks, 36%, hit exactly this before this fix).

    Deliberately generalizes `resume_task_after_checkpoint()`'s own
    mechanism rather than duplicating the round-loop plumbing: both
    ultimately just need SOME contract + commit_sha to hand to
    `_execute_contract()`. The one real difference, found by checking
    this priority's own plan premise against real code before building
    anything (`get_latest_checkpoint()`'s own docstring: "None if this
    task never escalated"): a task killed mid-round by a restart, unlike
    a tier-3/4 Continue, was very likely still actively working and
    never reached a PauseForOperator escalation, so a round_checkpoint often
    doesn't exist for it. `get_latest_resume_point()` (manager/
    replanning.py) covers both real, confirmed shapes (round_checkpoint
    when it exists -- 51% of real orphaned tasks; the latest replan_round
    otherwise -- covers 66% total); the remaining 34% (never even
    finished a first round) have nothing durable to resume from at all,
    and this function correctly returns a "not resumable" result for
    them rather than fabricating one.
    """
    resume_point = get_latest_resume_point(task_id)
    if resume_point is None:
        return {
            "status": "not_resumable", "task_id": task_id,
            "message": f"No round_checkpoint or replan_round found for task {task_id} -- "
                       "genuinely nothing durable to resume from (was still on its first, "
                       "never-yet-revised round when interrupted).",
        }

    commit_sha = resume_point["commit_sha"]
    files = None
    if commit_sha:
        from tools_odoo.module_dev.vcs import read_last_validated_commit

        files = await asyncio.to_thread(read_last_validated_commit, task_id)
        if files:
            ok, reason = _commit_content_still_compiles(files)
            if not ok:
                return {
                    "status": "not_resumable", "task_id": task_id,
                    "message": f"Checked-out commit for task {task_id} no longer compiles/parses "
                               f"cleanly ({reason}) -- refusing to resume onto corrupted state.",
                }

    last_contract = TaskContract.model_validate(resume_point["contract"])
    new_rules = [f"Original attempt summary: {resume_point['human_summary']}"]
    for r in resume_point["prior_rounds"]:
        vr = r.get("verification_result", {})
        new_rules.append(f"Round {r.get('round_number')}: {vr.get('notes', '')}")
    new_rules.append(
        "This task was interrupted mid-round by an external service restart and is being "
        "resumed automatically from its last durable state -- not a fresh attempt."
    )

    resume_update = {
        "rules": new_rules,
        "resumed_from_checkpoint_count": last_contract.resumed_from_checkpoint_count + 1,
        # Same real, confirmed bug and same fix as resume_task_after_checkpoint()'s own
        # sibling comment above -- reuses `files` (already fetched above for the compile-check,
        # when a commit_sha exists) rather than re-fetching, so a stale, original-dispatch-time
        # frozen snapshot is never blindly carried forward across this resume path either.
        "frozen_old_files_by_relpath": files,
    }
    original_capability_class = get_original_capability_class(task_id)
    if original_capability_class and last_contract.capability_class.value != original_capability_class:
        new_specialist_type, new_validation_by = select_specialist_and_validator(original_capability_class)
        resume_update["capability_class"] = CapabilityClass(original_capability_class)
        resume_update["specialist_type"] = new_specialist_type
        resume_update["validation_by"] = new_validation_by
        resume_update["inputs"] = []
    new_contract = last_contract.model_copy(update=resume_update)

    append_project_memory(
        event_type="branch_message", actor="manager", task_id=task_id, module=None,
        summary="Resumed automatically after a service restart (not a fresh attempt).",
        tags=["resumed", "resumed_at_startup"],
        detail={
            "role": "manager", "kind": "resumed_at_startup",
            "text": "Resumed automatically after a service restart (not a fresh attempt).",
            "resume_source": resume_point["source"],
        },
        verified=True,
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "delegate",
        "message": f"Resuming after restart (continuation #{new_contract.resumed_from_checkpoint_count}, "
                   f"from {resume_point['source']})",
        "status": "running",
    })

    module_for_repeat_check = resume_point["module_for_repeat_check"]
    memory_rows = read_project_memory(tags=None, limit=50)
    selector = ContextSelector()
    assembled = selector.select(candidate_rows=memory_rows, history=[], memory_query_ran=True)
    formatter = ContextFormatter()
    memory_block = formatter.format_system_block(assembled)
    constitution_text = load_manager_constitution()
    sensitive_paths_raw = load_sensitive_paths()
    module_lock_name = module_for_repeat_check or f"task:{task_id}"

    # See _terminal_node_state_for_result()'s own docstring -- same real gap this "resumed after
    # a service restart" path shares with the checkpoint-resume path above: a direct
    # _execute_contract() call bypasses run_graph_scheduler(), so without this, the node's graph
    # status never reflects that it's actually running again after the restart.
    _publish_node_state_changed(task_id, new_contract.current_constraint_label, "running")
    result = await _execute_contract(
        new_contract, module_lock_name, client, classifier_model, {"status": "not_a_correction"},
        module_for_repeat_check, memory_block, constitution_text, sensitive_paths_raw,
        manager_model=manager_model,
    )
    _terminal_state = _terminal_node_state_for_result(result)
    if _terminal_state:
        _publish_node_state_changed(task_id, new_contract.current_constraint_label, _terminal_state)

    # P12 Tier S item 4 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
    # ports the exact same 2026-07-12 decomposition-resume continuation fix
    # resume_task_after_checkpoint() already has (see that function's own extensive comments
    # right above it in this file for the full real-bug history) -- this function used to return
    # `result` unconditionally here, the identical bug: for a decomposed task
    # (new_contract.constraint_status non-empty), a genuine pass on the ONE sub-contract this
    # orphan-resume just re-ran has nowhere to hand off to, so every subsequent resume would keep
    # re-verifying the same already-satisfied constraint forever, never reaching the next one, and
    # Redis task state would never get cleared even when the task is genuinely fully done.
    if (
        result.get("status") == "completed"
        and result.get("passed")
        and new_contract.constraint_status
    ):
        reconstructed = _reconstruct_resume_order(new_contract)
        if reconstructed is not None:
            full_order, satisfied, start_index = reconstructed
            if start_index < len(full_order):
                base_contract = new_contract.model_copy(
                    update={"goal": new_contract.original_goal or new_contract.goal}
                )
                return await _run_constraint_labels_from(
                    base_contract, full_order, satisfied, start_index,
                    module_lock_name, client, classifier_model, {"status": "not_a_correction"},
                    module_for_repeat_check, memory_block, constitution_text, sensitive_paths_raw,
                    manager_model=manager_model,
                )
            # This genuinely was the LAST constraint -- the whole decomposed task is done.
            # Clearing Redis task state here is the other real half of this same port: without
            # it, a fully-completed decomposed task resumed from an orphan-restart would never
            # get marked done in Redis, identical to the pre-fix behavior
            # resume_task_after_checkpoint() itself used to have.
            clear_task_state(task_id)
            clear_cloud_spend(task_id)
        # reconstructed is None: order/completion could not be safely determined -- deliberately
        # do NOT clear_task_state or otherwise finalize here, same conservative posture as the
        # sibling function.
    return result


async def cleanup_module_from_failed_round(build_output_detail: dict, build_output_claims_complete: bool) -> tuple[bool, str] | None:
    """Real, general architectural fix (2026-07-17, Phase 20 Area 2):
    a round whose OWN install genuinely succeeded
    (build_output.claims_complete=True -- Build's real
    install_result.success) but gets rejected on a LATER, NORMAL
    verification failure (Code-Review, an "already real" model-name
    check, etc. -- not an exception) used to leave that real install
    permanently in place with zero cleanup; only PartialTaskFailure
    (an exception) ever triggered the existing compensating-actions
    uninstall in manager/compensations.py. Confirmed live, repeatedly:
    tasks defining a brand-new model (e.g. 'asset.registry',
    'vendor.review') round-1-installed for real, got rejected by
    Code-Review on an unrelated scope issue, and every SUBSEQUENT retry
    (even fresh task resubmissions days later) failed immediately
    because the model name was still genuinely real in the shared
    database -- a permanent, self-inflicted contamination that
    accumulated across the whole session.

    Fixed generally, not per-task: uninstall whatever this round
    actually installed the moment its OWN install succeeded but the
    round as a whole still failed, so the NEXT round (same task, same
    deterministic module name) always starts from a clean slate.

    Returns None when there's nothing to clean up (the round's own
    install never succeeded, e.g. it was rejected by pre-write
    validation and never reached the real install step at all -- the
    common, harmless case, not a bug). Returns (success, message) when
    a real cleanup was attempted -- success=False on a genuine
    uninstall failure is a real signal worth logging, but callers
    should treat this as best-effort and never let it crash the round
    loop; the round's own real failure is still the primary signal
    either way.
    """
    if not (
        build_output_claims_complete
        and "module_name" in build_output_detail
        and "db" in build_output_detail
    ):
        return None
    from tools_odoo.module_dev.toolchain import uninstall_module

    module_name = build_output_detail["module_name"]
    db = build_output_detail["db"]
    try:
        result = await asyncio.to_thread(uninstall_module, module_name, db)
        return result.success, result.message
    except Exception as exc:
        return False, f"uninstall raised: {exc!r}"


def _pop_llm_call_stats(client: ModelGatewayClient, task_id: str) -> dict:
    """Phase 30, P1d (Phase L, §15): thin adapter from
    ModelGatewayClient.pop_call_stats()'s own {"count", "duration_sec"}
    shape to ReplanRound's field names -- factored out so it's directly
    testable without constructing a real round loop.
    """
    stats = client.pop_call_stats(task_id)
    return {"llm_call_count": stats["count"], "llm_duration_sec": stats["duration_sec"]}


def run_phase32_post_completion_checks(
    goal_text: str, build_output_detail: dict, module_for_repeat_check: str | None, task_id: str,
) -> list[str]:
    """Phase 32 implementation (2026-08-11): the two DETECT-ONLY checks
    from docs/planning/PHASE32_RELIABILITY_ROOT_CAUSE_AND_ROLLOUT_STRATEGY_2026-08-11.md
    section 3.1, run once a task has already, genuinely passed every
    existing check -- these add a NEW dimension of correctness (what a
    human sees) rather than re-checking anything the pipeline already
    verifies. Never raises, never affects verification.passed or the
    task's own status -- a caller only ever appends the returned warning
    strings to human-facing notes.

    Deliberately best-effort: any missing db/model/network detail simply
    skips the relevant check rather than guessing, since a false ALARM
    here would train a human to ignore these warnings (the exact failure
    mode Phase 32 section 3.1 explicitly warns against).
    """
    warnings: list[str] = []
    db = build_output_detail.get("db")
    model_name = module_for_repeat_check or build_output_detail.get("module_name")
    # Set below if the model-reachability check detects a stale registry -- the
    # UI-action-gap check just below queries this SAME live server/registry, so its
    # result must not be trusted either while stale (see the comment at that check).
    _registry_stale_for_this_model = False

    if db and model_name and "." in model_name:  # a real Odoo model name, not a bare addon/module name
        try:
            from tools_odoo.live_server_health_check import (
                check_live_server_can_reach_model, format_stale_registry_warning,
            )
            health_result = check_live_server_can_reach_model(model_name, db)
            if health_result.stale_registry_detected:
                warnings.append(format_stale_registry_warning(health_result))
                _registry_stale_for_this_model = True
        except Exception as exc:  # noqa: BLE001 -- detect-only, must never affect the task's real outcome
            logger.warning("Task %s: live-server health check itself failed to run: %r", task_id, exc)

        try:
            from specialists.testing_qa.ui_action_presence_check import (
                check_goal_named_ui_actions_present, format_ui_action_gap_warning,
            )
            from tools_odoo.odoo_schema_client import _get_or_create_shared_key, _models_proxy

            uid, api_key = _get_or_create_shared_key(db, "Admin")
            models = _models_proxy(db)
            # Real, confirmed bug found live (2026-08-12 overnight Phase 35 Context-row/Tier-0
            # bake-in, recurred identically on 2 different real tasks -- ce70a850 during the
            # backfill script and b27bb30e from the live service itself): `args=[]` (a completely
            # empty list) was passed as this RPC call's positional args, but Odoo's own
            # `execute_kw` dispatch (odoo/service/model.py's `execute_kw`) ALWAYS treats
            # `args[0]` as the method's record-ids list, unconditionally, for ANY model method
            # called this way -- even `fields_view_get`, which doesn't logically need ids. An
            # empty `args` list has no `args[0]` at all, raising
            # `IndexError: tuple index out of range` inside Odoo's own dispatch before this
            # detect-only check's real work ever ran (caught by this function's own try/except,
            # so it never broke a real task, but it also meant the UI-action-presence check has
            # never actually run successfully in production). Fixed: `[[]]` -- a list containing
            # the (empty, correct) record-ids list -- matching the shape Odoo's own dispatch
            # actually requires.
            view_data = models.execute_kw(db, uid, api_key, model_name, "fields_view_get", [[]], {"view_type": "form"})
            arch = view_data.get("arch", "") if isinstance(view_data, dict) else ""
            ui_result = check_goal_named_ui_actions_present(goal_text, arch)
            # Real, confirmed root cause (2026-08-17, workflow_with_custom_buttons_or_cron
            # certification): this check reads the arch via the exact same live web server
            # (OMA_ODOO_URL, port 8071) already known -- and already exempted just above --
            # to serve a STALE in-memory view registry after an out-of-band sandbox/warm-
            # worker install. Only the model-reachability warning was exempted from
            # escalating the certification bar; this warning (a missing button/chatter)
            # was not, even though it's equally untrustworthy while the SAME registry is
            # stale. Confirmed live: all 3 real historical qualifying failures on this
            # direction (2026-08-13/15, agent_memory_events) show reproduction_confirmed=
            # True and passed=True -- the generated button/chatter content was genuinely
            # correct -- yet each still doubled the bar (29 -> 58 -> 116 -> 232) purely from
            # this warning. Skip trusting it when the registry is already known stale for
            # this exact model; a genuinely missing button on a FRESH registry still warns
            # and still resets the streak (qualifying_failure=bool(warnings) below still
            # sees the stale-registry warning itself), it just won't double-count.
            if ui_result.has_gap and not _registry_stale_for_this_model:
                warnings.append(format_ui_action_gap_warning(ui_result))
        except Exception as exc:  # noqa: BLE001 -- detect-only, must never affect the task's real outcome
            logger.warning("Task %s: UI-action-presence check itself failed to run: %r", task_id, exc)

    # Phase 35 §17.6.1: the post-install graph-diff check -- a NEW consultation point, added to
    # this SAME "detect-only, never affects verification.passed" family the two checks above
    # already belong to. Ships in log_only mode (manager/graph_governance_flags.py) and is
    # explicitly informational-only for a second, independent reason beyond the staged rollout:
    # the graph's own incremental-sync hook is async and NOT guaranteed complete by the time this
    # runs (see tools_odoo/knowledge_graph/build_safety_grounding.py's own module docstring for
    # the established freshness caveat this whole file already documents) -- a model genuinely
    # not yet appearing in the graph seconds after install is EXPECTED sync lag, not evidence of
    # a real problem, so this check can never safely escalate to a warning on a mismatch alone
    # (unlike its two siblings above, which check independently-verifiable facts). It only ever
    # logs a positive confirmation when the graph DOES already show the model, as a real,
    # low-noise signal of graph freshness for calibration purposes (§18.3) -- never a negative one.
    if db and model_name and "." in model_name:
        gate_mode = get_gate_mode("post_install_graph_diff")
        if gate_mode != GateMode.DISABLED:
            try:
                from infra.neo4j_client import get_neo4j_read_driver
                from manager.trace import publish_trace_event
                from tools_odoo.graph_queries import get_model_existence

                driver = get_neo4j_read_driver()
                in_progress, existence = get_model_existence(driver, model_name)
                if not in_progress and existence is not None:
                    publish_trace_event(task_id, {
                        "level": "manager", "actor": "manager", "phase": "post_install_graph_diff",
                        "message": (
                            f"[{gate_mode.value}] post_install_graph_diff: real graph already "
                            f"reflects {model_name!r} ({existence['field_count']} field(s), "
                            f"defined by {existence['defining_modules']}) -- graph freshness "
                            f"confirmed at task-completion time."
                        ),
                        "status": "running",
                    })
                # Deliberately no warning/else branch here -- see docstring above for why a
                # not-yet-visible model is expected async lag, not a confirmed defect.
            except Exception as exc:  # noqa: BLE001 -- detect-only, must never affect the task's real outcome
                logger.warning("Task %s: post-install graph-diff check itself failed to run: %r", task_id, exc)

    try:
        from manager.scope_certification import (
            classify_scope_with_confidence,
            get_adjudicator_review_sample,
            record_task_outcome,
        )

        # Phase 35 §11.1 fix 2 wiring (2026-08-12) -- a follow-up audit correctly caught that
        # an earlier pass added classify_scope_with_confidence() but never actually called it
        # from this, the one real production call site (still calling the plain classify_scope()
        # underneath it, exactly the same primary result, per that function's own documented
        # contract -- so this is not a behavior change for `scope` itself, only for whether the
        # ambiguity signal now gets acted on). A goal matching two or more scope patterns is
        # logged with elevated visibility, at the same "safety_net" trace-actor other Phase 35
        # mechanisms use, so a human reviewing this task can see the classification was
        # ambiguous -- never silently treated as an ordinary, confident classification either way.
        scope, is_ambiguous, matched_scopes = classify_scope_with_confidence(goal_text)
        if is_ambiguous:
            try:
                from manager.trace import publish_trace_event

                publish_trace_event(task_id, {
                    "level": "specialist", "actor": "safety_net",
                    "message": (
                        f"AMBIGUOUS CLASSIFICATION: this goal matched {len(matched_scopes)} scopes "
                        f"({matched_scopes}), not just {scope!r} -- flagged for human review."
                    ),
                    "status": "failed",
                })
            except Exception as trace_exc:  # noqa: BLE001 -- visibility must never break the real task
                logger.warning("Task %s: ambiguous-classification trace event itself failed: %r", task_id, trace_exc)

        # Phase 35 §15.8 wiring (2026-08-13) -- shadow-mode measurement only, per the design's
        # own explicit requirement that a generator earn trust before ever being live-gated.
        # Never affects this task's real outcome; purely logs whether a deterministic generator
        # would have extracted usable parameters, for the scopes that currently have one being
        # measured. §15's own gate in §15.5 is NOT wired here yet -- this is shadow measurement
        # only, not the live path.
        try:
            from manager.deterministic_generators.shadow import shadow_check_deterministic_extraction

            shadow_result = shadow_check_deterministic_extraction(scope, goal_text, task_id)
            if shadow_result is not None:
                from manager.trace import publish_trace_event

                publish_trace_event(task_id, {
                    "level": "specialist", "actor": "safety_net",
                    "message": f"SHADOW (§15.8, not live): deterministic-generator extraction result: {shadow_result}",
                    "status": "passed",
                })
        except Exception as exc:  # noqa: BLE001 -- shadow measurement must never affect the real task
            logger.warning("Task %s: §15.8 shadow-mode check itself failed to run: %r", task_id, exc)

        # A task that reaches this function already passed verification --
        # the two checks above are a SEPARATE dimension, so only THEIR
        # findings (not verification.passed, already true here) decide
        # whether this counts as a qualifying failure for bake-in purposes.
        # Phase 35 §1.1 item 2 / §11.1 fix 1 wiring (2026-08-12): goal_text is passed through
        # so is_scope_certified()'s real diversity floor has something to actually count --
        # this exact call site previously never passed it, meaning the floor added to
        # scope_certification.py could never be satisfied by real production data at all.
        # Real gap found live overnight (2026-08-14): a stale-registry-ONLY warning
        # (real, but a timing/propagation-lag fact about this environment, not evidence
        # the generated code is unreliable -- see record_task_outcome()'s own escalate_bar
        # docstring for the live incident this closes) should still reset the streak, but
        # must not trigger the SAME bar-doubling escalation as a genuine content defect
        # (a UI-action-gap warning). The stale-registry warning's own text always starts
        # with format_stale_registry_warning()'s stable "⚠ LIVE SERVER HEALTH CHECK:"
        # prefix -- escalate only if some OTHER warning (currently only the UI-action-gap
        # check) is present too.
        _escalate_bar = any(not w.startswith("⚠ LIVE SERVER HEALTH CHECK:") for w in warnings)
        record_task_outcome(
            scope, task_id=task_id, qualifying_failure=bool(warnings), goal_text=goal_text,
            escalate_bar=_escalate_bar,
        )

        # Phase 35 §11.1 fix 3 wiring (2026-08-12) -- the same follow-up audit found
        # get_adjudicator_review_sample() was built and unit-tested but never actually called
        # anywhere in the real pipeline. Checked cheaply after every outcome is recorded; the
        # function itself returns None on every call except the one that lands exactly on a
        # review boundary (every _ADJUDICATOR_REVIEW_EVERY_N-th qualifying failure), so this is
        # a near-no-op the rest of the time.
        review_sample = get_adjudicator_review_sample()
        if review_sample is not None:
            try:
                from manager.trace import publish_trace_event

                publish_trace_event(task_id, {
                    "level": "specialist", "actor": "safety_net",
                    "message": (
                        f"ADJUDICATOR SELF-REVIEW DUE: {len(review_sample)} past qualifying-failure "
                        f"adjudications sampled for blind re-review (scopes: "
                        f"{sorted({e['scope'] for e in review_sample})})."
                    ),
                    "status": "failed",
                })
            except Exception as trace_exc:  # noqa: BLE001 -- visibility must never break the real task
                logger.warning("Task %s: adjudicator-review trace event itself failed: %r", task_id, trace_exc)
    except Exception as exc:  # noqa: BLE001 -- tracking must never affect the task's real outcome
        logger.warning("Task %s: scope-certification recording itself failed to run: %r", task_id, exc)

    return warnings


def real_install_genuinely_failed(build_output_detail: dict, build_output_claims_complete: bool) -> bool:
    """Real bug found live (2026-07-20, Phase 20 Area 2 deep-reverify
    pass 5, task #49, hr.employee.badge_expiry_date): the same failure
    family as the sandbox_failed short-circuit in _execute_contract,
    one step later in the pipeline. specialists/build/specialist.py's
    _sandbox_preflight() had already passed and files WERE written for
    real, but the REAL install into self.db then failed
    (install_result.success=False, e.g. rc=255) -- yet nothing
    short-circuited Testing/QA, which went on to independently verify
    against the LIVE DB and (correctly, since nothing installed)
    reported "NO group restriction at all -- visible to everyone".
    That accurate-but-misleading symptom became the round's own
    reported failure reason for 3 consecutive rounds, while the real
    cause (the actual install error) was never once surfaced to
    Build/bug_fix's next-round retry -- an identical non-progress loop
    to the sandbox_failed case, just triggered by the LATER install
    call instead of the earlier preflight one.

    "db" in build_output_detail is only ever set at the point AFTER a
    real install attempt against the real target (see
    specialists/build/specialist.py's SpecialistOutput return right
    after install_module()) -- so combined with claims_complete=False
    (== install_result.success=False there), this is a precise,
    general signal that this round reached and failed a genuine
    install, not a pre-write validation rejection or a sandbox-only
    failure (which never sets "db" at all).
    """
    return "db" in build_output_detail and not build_output_claims_complete


def build_verification_notes_with_log_tail(summary: str, build_output_detail: dict) -> str:
    """Real, general fix (2026-07-21, Phase 20 Area 2): the
    sandbox_failed/real_install_genuinely_failed short-circuit in
    _execute_contract used to build `verification.notes` from
    `build_output.summary` ALONE -- a generic "Install failed (rc=255)
    -- see log_tail for detail" string with zero actual diagnostic
    content, even though _sandbox_preflight()/install_module()
    (specialists/build/specialist.py, tools_odoo/module_dev/
    toolchain.py) already compute a real, targeted error excerpt
    (_extract_error_excerpt(), centered on the FIRST error marker, not
    just a blind tail) and attach it to build_output.detail as
    "sandbox_log_tail"/"install_log_tail". That real content never made
    it past this point: classify_root_cause() reads `verification.notes`
    alone (so it was guessing "one_off" vs "pattern_worth_a_rule" with
    no real evidence -- see 2026 research on flaky-vs-real-bug
    misclassification, most "flaky" labels turn out wrong when nobody
    actually inspects the real failure content), and
    revise_contract_from_verification() feeds `verification.notes`
    straight into the NEXT round's own contract rules -- so Build was
    being told "see log_tail for detail" verbatim, never the actual
    detail, on every retry. Confirmed live: tasks #43/#49/#51/#54 each
    repeated an IDENTICAL generic rc=255 failure 3-5 rounds running with
    this exact information gap, immediately after an uninterrupted,
    no-mid-run-fixing 7-task retest pass specifically run to get an
    honest snapshot (2026-07-21).

    Pure, deterministic, no truncation applied here -- callers should still pass the result
    through fold_verification_notes() (never the plain fold_specialist_result() -- this always
    becomes VerificationResult.notes, exactly the field fold_verification_notes() exists to
    protect, per its own docstring; fixed in place here 2026-08-04 after a real, confirmed gap
    was found: this call's own ONE caller was still using the plain fold) for the existing
    char-limit safety net, same as every other VerificationResult.notes construction site.
    """
    log_tail = (
        build_output_detail.get("sandbox_log_tail")
        or build_output_detail.get("install_log_tail")
        or ""
    )
    if not log_tail:
        return summary
    return f"{summary}\n\nReal error detail (log_tail):\n{log_tail}"


async def resolve_missing_dependency_module(finding_texts: list[str]) -> tuple[str | None, str | None]:
    """Real, confirmed architectural gap found live: a task that
    references or extends a model built by an EARLIER task (e.g. "the
    service module") has no way to know which module actually defines
    it -- every scaffolded module gets a fresh, randomly-suffixed name,
    never a stable, guessable one. Build kept failing the identical
    "missing dependency" Code-Review finding round after round on a real
    live task, not from carelessness -- the correct module name
    genuinely wasn't knowable from the goal text alone. Rather than keep
    asking Build to guess, resolve it for real: when a round's own
    Code-Review finding names a missing-dependency model, look it up
    against every module actually on disk (find_module_defining_model(),
    the one real source of truth) and hand the next round the concrete,
    correct answer instead of the same unresolvable ambiguity again.

    Extracted as a standalone, directly-testable function (2026-07-21)
    after a second real bug was found live in what used to be inline
    code here -- see the module-name-vs-model-name comment below.

    Returns (dependency_model, resolved_module) -- dependency_model is
    the raw model-name-shaped string a finding named (for logging only);
    resolved_module is the real module name to add as a dependency, or
    None if nothing could be resolved.
    """
    from tools_odoo.codebase_read import find_module_defining_model, module_exists_on_disk

    dependency_model = None
    for text in finding_texts:
        match = re.search(
            r"depend[a-z]*[^.]*?['\"]([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+)['\"]",
            text, re.IGNORECASE,
        )
        if match:
            dependency_model = match.group(1)
            break
        # Real, confirmed gap found live: Code-Review sometimes names the
        # same thing with an underscore ("Depends on 'oma_service', which
        # is assumed to exist") instead of the real, dotted model-name
        # form ('oma.service') -- Code-Review itself is guessing at the
        # shape here, not being wrong exactly, just informal. The first
        # pattern above requires a literal dot (it's built for genuine
        # dotted model names); this second, looser pattern catches the
        # underscore-only phrasing and converts it to the real dotted
        # form Odoo model names actually use, so the same real lookup
        # still has a fair chance to run.
        match = re.search(
            r"depend[a-z]*[^.]*?['\"]([a-z][a-z0-9]*(?:_[a-z0-9]+)+)['\"]",
            text, re.IGNORECASE,
        )
        if match:
            # Real, confirmed bug found live (2026-07-21, Operator demo take
            # 4): a validator finding can ALSO name an already-real
            # MODULE name directly in quotes (e.g. "...defined by module
            # 'oma_create_a_small_new_...', which is NOT in the
            # manifest's depends list... Add 'oma_create_a_small_new_...'
            # to depends") -- this second pattern matched that fine, but
            # then always ran it through the dot-conversion built for a
            # MODEL name (e.g. 'oma_service' -> 'oma.service'), mangling
            # an already-correct module name into a nonsense dotted
            # string that find_module_defining_model() can never find.
            # resolved_module then stayed falsy every round, the
            # depends_on_module auto-fix never fired, and the exact same
            # finding recurred verbatim for 3 straight rounds even though
            # the module name was spelled out correctly in the finding
            # text the whole time. Fixed: check whether the raw matched
            # string is ALREADY a real module directory on disk first --
            # if so, it's a module name, not a model name, use it
            # directly and skip the model-name interpretation entirely.
            raw_match = match.group(1)
            if await asyncio.to_thread(module_exists_on_disk, raw_match):
                return raw_match, raw_match
            dependency_model = raw_match.replace("_", ".")
            break
    if not dependency_model:
        return None, None
    resolved_module = await asyncio.to_thread(find_module_defining_model, dependency_model)
    return dependency_model, resolved_module


def _write_gateway_unavailable_checkpoint(
    task_id: str,
    module_for_repeat_check: str | None,
    current_contract: TaskContract,
    prior_rounds: list[ReplanRound],
    pause: GatewayOutagePause,
) -> None:
    """P13 item 12c (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    §22.2): real, live-confirmed bug -- `GatewayOutagePause`'s own message tells Operator "I'll resume
    automatically once the gateway is back," but no `round_checkpoint` memory event was ever
    written at any of this function's `except GatewayOutagePause` return sites, so
    `resume_task_after_checkpoint()` (which strictly requires `get_latest_checkpoint()` to find one)
    always failed with "No round_checkpoint found" -- confirmed live, task_id `9aa73af4-...`
    (docs/reports/OMA_LIVE_MODEL_AB_TEST_2026-07-31.md).

    Deliberately does NOT call `summarize_escalation_for_operator()` (the LLM-generated human_summary
    the real PauseForOperator checkpoint uses) -- that call goes through the exact same model gateway
    this pause exists BECAUSE is unavailable, so requiring it here would make the checkpoint write
    itself fail during every real gateway outage, the one time this code path actually runs. Uses a
    plain, deterministic summary instead -- the only difference from the PauseForOperator checkpoint
    shape, everything else (`prior_rounds`, `last_contract`, `module_for_repeat_check`) matches.
    """
    human_summary = (
        f"Paused after {len(prior_rounds)} round(s) because the model gateway became unreachable "
        f"mid-task. No work was lost -- this will resume from exactly where it left off once the "
        f"gateway is back."
    )
    checkpoint_detail = {
        "human_summary": human_summary,
        "prior_rounds": [r.model_dump(mode="json") for r in prior_rounds],
        "round_count": len(prior_rounds),
        "last_contract": current_contract.model_dump(mode="json"),
        "escalation_reason": "gateway_unavailable",
        "module_for_repeat_check": module_for_repeat_check,
    }
    append_project_memory(
        event_type="round_checkpoint", actor="manager", task_id=task_id,
        module=module_for_repeat_check,
        summary=f"Paused after {len(prior_rounds)} round(s): gateway became unreachable.",
        tags=["round_checkpoint"], detail=checkpoint_detail, verified=True,
    )


def _write_task_cut_off_checkpoint(
    task_id: str,
    module_for_repeat_check: str | None,
    current_contract: TaskContract,
    prior_rounds: list[ReplanRound],
    pause: "TaskCutOffPause",
) -> None:
    """Real, confirmed gap found live (2026-08-17, overnight `record_rule_row_level_security`
    certification run): the sibling of `_write_gateway_unavailable_checkpoint()` above, for
    `TaskCutOffPause` -- the OTHER real cause of a repetition-loop pause (a `PartialTaskFailure`
    after at least one real step already executed, e.g. `LLMRepetitionLoopExhaustedError`
    surfacing mid-round rather than on the very first gateway call). Before this fix, this
    pause's own `except` site (below) returned immediately with NO `round_checkpoint` written
    at all -- meaning `resume_task_after_checkpoint()` always failed with "No round_checkpoint
    found... nothing to resume," identical to the exact bug `_write_gateway_unavailable_
    checkpoint()`'s own docstring already documents fixing for the sibling pause reason, just
    never applied here too. Confirmed live: `record_rule_row_level_security`'s own certification
    push hit `LLMRepetitionLoopExhaustedError` via THIS path (not the gateway-outage one) more
    often than the other, so closing only the gateway-outage half left the more common real
    case still needing a manual retry.

    Semantically safe to resume from, even though `handle_partial_task_failure()` has already
    run compensations (undone the partial work, e.g. removed a scaffolded module): resuming
    re-enters the SAME round loop from the SAME reconstructed contract/rules, which naturally
    re-scaffolds/re-executes from scratch on its own -- identical in shape to any ordinary round
    retry, never assumes any of the undone work still exists.
    """
    human_summary = (
        f"Paused after {len(prior_rounds)} round(s) -- {pause.message} No work was lost that "
        f"wasn't already cleanly undone by the compensating actions above; a retry re-executes "
        f"this round from scratch, the same as any ordinary round retry."
    )
    checkpoint_detail = {
        "human_summary": human_summary,
        "prior_rounds": [r.model_dump(mode="json") for r in prior_rounds],
        "round_count": len(prior_rounds),
        "last_contract": current_contract.model_dump(mode="json"),
        "escalation_reason": "task_cut_off",
        "module_for_repeat_check": module_for_repeat_check,
    }
    append_project_memory(
        event_type="round_checkpoint", actor="manager", task_id=task_id,
        module=module_for_repeat_check,
        summary=f"Paused after {len(prior_rounds)} round(s): task cut off mid-sequence.",
        tags=["round_checkpoint"], detail=checkpoint_detail, verified=True,
    )


def _terminal_node_state_for_result(result: dict) -> str | None:
    """Phase B UI fix (2026-08-09), the project owner's own direct live report: after resuming a paused
    task, the graph "showed absolutely nothing" -- no node ever flipped to 'running', the node
    status swatches stayed frozen at whatever they were before the resume, even though real
    Build/Review/QA activity (round/specialist/timing events) was streaming live the whole time.

    Root cause: `manager/graph_scheduler.py`'s `run_graph_scheduler()` dispatch loop is the ONLY
    place that ever called `_publish_node_state_changed()` -- correct for every constraint label
    dispatched THROUGH the graph scheduler (the normal, fresh-decomposition and mid-round-loop
    resume paths), but every single-constraint direct call to `_execute_contract()` (checkpoint
    resume -- `_resume_task_after_checkpoint_locked()` -- and the resume-after-service-restart
    path) bypasses the scheduler entirely, so the ONE node actually being worked on never got its
    `state` field live-published or durably updated at all. round_number/activeSpecialist DID
    keep updating live (those publishers already lived directly inside `_execute_contract()`'s own
    round loop), which is exactly why Operator saw specialist/round activity in the drawer but the
    graph's own node color/status and the "N running" chip never moved.

    This is the single-caller counterpart of `run_graph_scheduler()`'s own state transitions
    (satisfied/failing/paused) -- callers publish 'running' themselves right before invoking
    `_execute_contract()` (mirroring the scheduler's own dispatch-time publish), then pass this
    function's return value to `_publish_node_state_changed()` right after. Returns None for any
    result shape that isn't a real terminal state (e.g. 'rejected', 'cancelled', 'error') --
    callers must not publish a stale/misleading node state for those.
    """
    status = result.get("status")
    if status == "paused":
        return "paused"
    if status == "completed":
        return "satisfied" if result.get("passed") else "failing"
    return None


async def _execute_contract(
    contract: TaskContract,
    module_lock_name: str,
    client: ModelGatewayClient,
    classifier_model: str,
    correction_result: dict,
    module_for_repeat_check: str | None,
    memory_block: str,
    constitution_text: str,
    sensitive_paths_raw: list,
    manager_model: str | None = None,
) -> dict:
    """Phases 4b-6: delegate, verify, write the outcome. Split out from
    run_turn() so a tier-3/4 task's approved sign-off (resume_after_sign_off,
    arriving in a later, separate request) can run exactly this same
    back half -- never a second, drifting copy of this logic.

    Phase 15 (§19.6): phase five is now a BOUNDED reflect-and-retry round
    loop, not a single delegate -> verify pass. A failed round revises
    the contract from the SPECIFIC evidence in its own VerificationResult
    (and any Code-Review finding, §19.5) and retries -- up to
    contract.planning_round_budget rounds or round_wall_clock_cap_seconds,
    whichever comes first -- rather than reporting failure back to Operator
    on the very first attempt. Every round is logged as a real
    ReplanRound event, so the Manager's thinking is legible across
    attempts, not a black box. A genuinely unfixable task still stops --
    should_escalate_to_operator() raises PauseForOperator, caught here and turned
    into the same {"status": "paused", ...} shape every other pause
    condition already uses.

    Real, deliberate behavior change from Phases 1-14 (not silently
    absorbed): a task that used to come back as {"status": "completed",
    "passed": False} on its very first failure now retries first --
    tests written against the old one-shot-failure shape need updating
    to expect this, which is the intended point of this phase, not a
    regression.
    """
    task_id = str(contract.task_id)
    tier_value = contract.tier.value
    # Real fix (the project owner's own explicit finding, confirmed live): the
    # Manager's own real judgment calls -- the retry/specialist-switch
    # decision, the escalation summary and success digest written for
    # Operator -- were all silently reusing classifier_model (the cheap,
    # narrow-classification tier) because run_turn() was only ever given
    # one model parameter. OMA_MODEL_MANAGER already existed and was
    # already used for Code-Review's own calls, but never for the
    # Manager's own reasoning. Root-cause classification deliberately
    # stays on classifier_model -- it's a bounded 4-label classification,
    # the same shape as correction-detection/capability-classification,
    # not open judgment.
    manager_model = manager_model or os.environ.get("OMA_MODEL_MANAGER", "qwen3.6-27b")

    mark_task_running(task_id)
    # Real, confirmed gap found live (2026-08-17): mark_task_running() fires here on every
    # fresh AND resumed task -- the one universal "genuinely running again" signal. Clears any
    # stale gateway-auto-resume tracking entry (manager/gateway_auto_resume.py); if this exact
    # task pauses again for a gateway reason, record_gateway_pause() creates a fresh entry with
    # its own fresh backoff, so this is always safe to call unconditionally.
    try:
        from manager.gateway_auto_resume import clear_gateway_auto_resume_tracking

        clear_gateway_auto_resume_tracking(task_id)
    except Exception:  # noqa: BLE001 -- must never affect the real task
        pass

    # Phase 25B (2026-07-25): one schema-guided extraction of this
    # contract's own goal-stated field facts, run ONCE here (before
    # round 1), never re-run per round -- contract.goal never changes
    # across this function's own round loop (only contract.rules
    # grows), so the same extraction stays valid for every round.
    # Scoped to module_development/bug_fix exactly like
    # decompose_into_constraints() above it in the call chain -- the
    # only capability_class/specialist_type shape this extraction's
    # own consumers (manager/replanning.py's field-omission escalation,
    # specialists/build/specialist.py's field-omission autofix) apply
    # to at all. FAST_EXTRACTION_MODEL (this file's own module-level
    # constant) -- same "pure, short, structured extraction" tier
    # already used for correction-detection/capability-classification/
    # root-cause, not the heavier manager_model.
    if (
        contract.capability_class == CapabilityClass.module_development
        and contract.specialist_type == SpecialistType.bug_fix
        and not contract.goal_facts
    ):
        # P12 Tier B/C item 30: threads the round's own real, current focus label through
        # explicitly -- see extract_goal_field_facts()'s own docstring for the full reasoning.
        # None on a non-decomposed contract, exactly matching the function's own no-op default.
        goal_facts = await extract_goal_field_facts(
            contract.goal, client, FAST_EXTRACTION_MODEL, task_id=task_id,
            current_constraint_label=contract.current_constraint_label,
        )
        # Full dump, never exclude_none: consumers gate on
        # goal_facts["field_name"] matching the field they're asking
        # about, which must be reliably None (not merely absent) when
        # extraction found nothing -- an empty {} here would only ever
        # happen if this whole branch never ran, a distinct, separately
        # checked case (contract.specialist_type/capability_class).
        contract = contract.model_copy(update={"goal_facts": goal_facts.model_dump()})
        # P10 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
        # §18, Phase O, item 2): scale planning_round_budget when the goal's own field_type has
        # real, evidence-backed low combination/generation confidence -- instead of relying on
        # `anticipated_scope.planning_round_budget`, which the real chat UI's own code comment
        # confirms is never actually supplied in practice. Deliberately narrow: only field_type
        # (the one dimension already reliably extracted here, right where goal_facts is set,
        # timing-correct since should_escalate_to_operator() only ever reads this per-round, never
        # at contract-build time) -- the full multi-dimension complexity_signal item 1 describes
        # would need new, unvalidated free-text scope-extraction across 7 more real dimensions,
        # not attempted here as a guess. Only fires when planning_round_budget is still exactly
        # the schema default (5) -- an explicit caller-supplied override (contract construction's
        # own anticipated_scope.planning_round_budget hint) always wins, never silently overridden.
        if (
            contract.planning_round_budget == 5
            and field_type_has_low_coverage_confidence(goal_facts.field_type)
        ):
            contract = contract.model_copy(update={"planning_round_budget": 8})
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "note",
                "message": (
                    f"Widened planning_round_budget to 8 (from the default 5) -- "
                    f"{goal_facts.field_type!r} has real, evidence-backed low combination/"
                    f"generation confidence in coverage_data.json."
                ),
                "status": "info",
            })

    current_contract = contract
    round_number = 1
    started_at = time.monotonic()
    prior_rounds: list[ReplanRound] = []
    # Real fix (the project owner's own explicit design, confirmed against 2025-2026
    # multi-agent orchestration practice via direct research): the
    # Manager's OWN accumulating memory -- contract.goal + every round's
    # real detail -- was never checked against the Manager's own real
    # context window at all; only the NEXT SPECIALIST's window was
    # checked (estimate_context_pressure() above). If the Manager's own
    # memory crosses the soft threshold mid-loop, it summarizes the
    # round history so far (never the goal, never Operator's own words) and
    # keeps going -- this is NOT an escalation condition on its own,
    # unlike round_budget/wall_clock/context_pressure. manager_history_summary
    # accumulates the rolling summary across possibly multiple
    # compression events within the same round budget.
    manager_history_summary: str | None = None
    # A real gap found live (Phase 16 QA pass): when a retry round gets
    # routed through code_review instead of bug_fix, THAT round's own
    # build_output has no module_name/db (Code-Review reviews an
    # existing diff, it doesn't scaffold one) -- testing_qa validation
    # then has nothing to inject a verify_module: entry from and fails
    # instantly. Track the last round that WAS actually Build, so
    # await_verification() can fall back to it -- the module Code-Review
    # is looking at is still the one on disk from that last Build.
    last_known_module_name: str | None = None
    last_known_db: str | None = None

    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "delegate",
        "message": "Delegating to a specialist", "status": "passed",
    })

    try:
        while True:
            # Phase 16 (§20.6): a real, cooperative cancel point --
            # checked at the top of every round, alongside the existing
            # round-budget/wall-clock checks, never a hard kill mid-write.
            if is_cancel_requested(task_id):
                clear_cancel_request(task_id)
                release_module_lock(module_lock_name, task_id)
                append_project_memory(
                    event_type="decision",
                    actor="operator",
                    task_id=task_id,
                    module=module_for_repeat_check,
                    summary=f"Operator cancelled this task before round {round_number}: {contract.goal!r}",
                    tags=["cancelled_by_operator"],
                    detail={"round_number": round_number},
                    verified=True,
                )
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "task",
                    "message": "Cancelled by Operator.", "status": "failed",
                })
                return {
                    "status": "cancelled",
                    "reason": "cancelled_by_operator",
                    "message": "Cancelled -- stopped at a safe boundary before the next round started.",
                    "task_id": task_id,
                    "correction_result": correction_result,
                }

            # Real UX addition, 2026-08-08 (the project owner's own explicit request: "add time tracker and
            # counter for every round for every builder code reviewer tester orchestrator ...
            # everything all time"). _round_t0 marks the real wall-clock start of THIS round --
            # persisted as "round_total" once the round concludes (see _persist_round_timings_
            # for_this_round below, called at every real exit point this round can take).
            # _specialist_timings_this_round accumulates each specialist's own real measured
            # seconds as they individually finish, so "manager" (orchestrator overhead) can be
            # computed honestly as round_total minus every specialist's own real time, never a
            # guess -- exactly the real accounting the project owner asked for ("all processes").
            _round_t0 = time.monotonic()
            _specialist_timings_this_round: dict[str, float] = {}

            def _persist_round_timings_for_this_round() -> None:
                if not current_contract.current_constraint_label:
                    return
                _round_total = time.monotonic() - _round_t0
                # resume_index (2026-08-09, the project owner's own direct live report: "timer for different
                # resumes, it's staying the same... it should be separate sync"): tags every
                # timing entry with which attempt produced it, same real per-attempt isolation as
                # round_diffs/round_findings/round_checks -- see persist_node_round_timing()'s own
                # docstring for the full fix (a plain SET at (round, resume_index, actor) now,
                # since that key is only ever written once, replacing the earlier cross-attempt
                # ADD-accumulation this same round number used to need before attempts were
                # tagged at all).
                _resume_index = current_contract.resumed_from_checkpoint_count
                _all_timings = {}
                for _actor, _secs in _specialist_timings_this_round.items():
                    _all_timings[_actor] = persist_node_round_timing(
                        task_id, current_contract.current_constraint_label, round_number, _actor, _secs,
                        resume_index=_resume_index,
                    )
                _manager_secs = max(0.0, _round_total - sum(_specialist_timings_this_round.values()))
                _all_timings["manager"] = persist_node_round_timing(
                    task_id, current_contract.current_constraint_label, round_number, "manager", _manager_secs,
                    resume_index=_resume_index,
                )
                _all_timings["round_total"] = persist_node_round_timing(
                    task_id, current_contract.current_constraint_label, round_number, "round_total", _round_total,
                    resume_index=_resume_index,
                )
                # Real UX addition, 2026-08-08 (the project owner's own explicit, emphatic request:
                # "everything should update in UI the same speed as in backend, in millisecond...
                # without me refreshing pages"). persist_node_round_timing() above durably WRITES
                # this round's timing, but nothing ever told a live-watching browser tab it
                # happened -- the timer display could only ever catch up on a REST refetch,
                # never live. This is the missing live counterpart, same real node_id/round
                # scoping every other live specialist event on this stream already carries;
                # the frontend applies it directly onto the exact round+attempt key it belongs
                # to, no guessing, no full refetch needed just to see a timer tick.
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "node_round_timing",
                    "node_id": current_contract.current_constraint_label, "round": round_number,
                    "resume_index": _resume_index,
                    "timings": _all_timings, "status": "info",
                    "message": f"Round {round_number} timing updated",
                })

            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                "message": f"Round {round_number}", "status": "running",
            })
            _publish_node_round_advanced(
                task_id, current_contract.current_constraint_label, round_number,
            )
            _publish_node_specialist_changed(
                task_id, current_contract.current_constraint_label,
                "build" if current_contract.specialist_type == SpecialistType.bug_fix else "review",
            )
            publish_trace_event(task_id, {
                "level": "specialist", "actor": current_contract.specialist_type.value, "round": round_number, "node_id": current_contract.current_constraint_label,
                "message": "Running…", "status": "running",
            })
            try:
                _build_t0 = time.monotonic()
                build_output = await run_with_gateway_outage_handling(
                    task_id, module_lock_name, lambda: delegate_to_specialist(current_contract)
                )
                # Same real actor derivation as the specialist_changed publish two blocks above --
                # this call is Build (bug_fix) on most rounds, but IS Code-Review's own primary
                # call on a code_review-routed round (see the bounce-back routing comment further
                # down this file), so the timing must be attributed to whichever specialist this
                # round's own current_contract.specialist_type actually names, never assumed.
                _specialist_timings_this_round[
                    "build" if current_contract.specialist_type == SpecialistType.bug_fix else "review"
                ] = time.monotonic() - _build_t0
            except GatewayOutagePause as pause:
                _write_gateway_unavailable_checkpoint(
                    task_id, module_for_repeat_check, current_contract, prior_rounds, pause,
                )
                return {
                    "status": "paused",
                    "reason": "gateway_unavailable",
                    "message": pause.message,
                    "task_id": task_id,
                    "correction_result": correction_result,
                }
            except TaskCutOffPause as pause:
                _write_task_cut_off_checkpoint(
                    task_id, module_for_repeat_check, current_contract, prior_rounds, pause,
                )
                try:
                    from manager.gateway_auto_resume import record_gateway_pause

                    record_gateway_pause(task_id, module_for_repeat_check)
                except Exception:  # noqa: BLE001 -- scheduling must never affect the real pause
                    pass
                return {
                    "status": "paused",
                    "reason": "task_cut_off",
                    "message": pause.message,
                    "task_id": task_id,
                    "correction_result": correction_result,
                }

            if "module_name" in build_output.detail and "db" in build_output.detail:
                last_known_module_name = build_output.detail["module_name"]
                last_known_db = build_output.detail["db"]

            publish_trace_event(task_id, {
                "level": "specialist", "actor": current_contract.specialist_type.value, "round": round_number, "node_id": current_contract.current_constraint_label,
                "message": build_output.summary,
                "status": "passed" if build_output.claims_complete else "failed",
            })
            # Real UX addition, 2026-08-08 (the project owner's own explicit request: the granular,
            # real-time step narrative -- "Check this. Change that." -- the pre-Phase-31 UI
            # showed. Real one-line durable step, reusing the exact same real summary text just
            # published above, not new prose invented for this.
            if current_contract.current_constraint_label:
                persist_node_round_step(
                    task_id, current_contract.current_constraint_label, round_number,
                    "build", build_output.summary, "passed" if build_output.claims_complete else "failed",
                    resume_index=current_contract.resumed_from_checkpoint_count,
                )
            if build_output.detail.get("diff"):
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": current_contract.specialist_type.value, "round": round_number, "node_id": current_contract.current_constraint_label,
                    "kind": "result", "result": {"diff": build_output.detail["diff"]},
                    "message": "Code changes", "status": "passed" if build_output.claims_complete else "failed",
                })
                # Real UX addition, 2026-08-08 (the project owner's own explicit request: full historical
                # diffs preserved for every past round, not just whatever was live-streaming).
                # Durable counterpart to the SSE publish immediately above -- same data, written
                # to Postgres too, keyed by this exact round_number. Only meaningful for a
                # decomposed (constraint-graph) task; current_constraint_label is None for a
                # non-decomposed one. resume_index (2026-08-09): see persist_node_round_diff()'s
                # own docstring -- tags which resume attempt produced this diff, so a later
                # resume reusing this same round_number never destroys an earlier attempt's own
                # real diff, just appends alongside it.
                if current_contract.current_constraint_label:
                    persist_node_round_diff(
                        task_id, current_contract.current_constraint_label, round_number, build_output.detail["diff"],
                        resume_index=current_contract.resumed_from_checkpoint_count,
                    )

            # Phase 15 (§19.5): Code-Review genuinely engaged mid-loop
            # for module_dev tasks -- a module that works but has a
            # blocking finding is not actually done, even if Testing/QA's
            # own reproduction independently passes (Phase 13's real
            # task-2 finding: Testing/QA's field-existence check had no
            # way to catch the access.csv bug Code-Review caught).
            #
            # Real, severe root-cause bug found live (2026-07-15, the project owner
            # directly pushed back on "it's just a model limitation" and
            # was right): this condition only checked "module_name" in
            # build_output.detail -- true even on a SANDBOX-FAILED round,
            # since specialists/build/specialist.py's early-return path
            # (sandbox_result.success is False) includes module_name in
            # its detail dict too. But that same early-return path never
            # calls write_module_file() for manifest/models/views/
            # security -- those writes are gated strictly AFTER a
            # successful sandbox pre-flight. run_code_review_diff() ->
            # CodeReviewSpecialist._run_diff_review() calls
            # read_module_files(module_name), reading straight from the
            # real container's disk -- which, after a sandbox failure,
            # still holds whatever was there BEFORE this round (round
            # 1's raw stripped scaffold skeleton, or an earlier round's
            # stale content). Confirmed directly: pulled the raw model
            # response for a real failing task (badge_expiry_date on
            # hr.employee) and it was CORRECT -- proper `_inherit =
            # 'hr.employee'`, correct depends, correct view -- yet
            # Code-Review's own finding for that exact round described
            # "references views/templates.xml and demo/demo.xml",
            # "depends on base instead of hr" -- content that doesn't
            # match what was actually generated at all. That wrong,
            # misleading critique then became the "fix this" rule fed
            # back to Build for the next round, actively steering it
            # away from its own already-correct work. This is the real,
            # fixable, non-model root cause behind most of this
            # session's "scattered, different failure signature every
            # round" pattern -- not a model capability limit.
            review_output = None
            should_run_code_review = (
                current_contract.capability_class == CapabilityClass.module_development
                and "module_name" in build_output.detail
                and not build_output.detail.get("sandbox_failed")
                # Phase 26B follow-up (2026-07-27): a lock rejection (two
                # tasks genuinely contending for the same real Odoo
                # model, now actually reachable -- see specialists/build/
                # specialist.py's own comment on this exact flag) happens
                # even before the sandbox step, so `sandbox_failed` alone
                # doesn't catch it -- Code-Review would otherwise diff a
                # module directory that was never written at all,
                # producing a misleading "no files found" finding instead
                # of the real, simple "another task is editing this
                # model right now" reason.
                and not build_output.detail.get("lock_rejected")
            )
            verification_will_be_skipped = (
                current_contract.validation_by is None
                or build_output.detail.get("sandbox_failed")
                or build_output.detail.get("lock_rejected")
                or real_install_genuinely_failed(build_output.detail, build_output.claims_complete)
            )
            already_verified_concurrently = False

            # Phase 22 (2026-07-23): when BOTH Code-Review's mid-round
            # diff review AND Testing-QA's own verification are
            # genuinely about to run this round, run them CONCURRENTLY
            # -- confirmed independent: await_verification()'s own
            # code_review_output parameter is only ever consumed AFTER
            # Testing-QA's own validator.run() already returned (a pure
            # post-hoc fold, manager/tools.py's fold_code_review_findings(),
            # never fed into the validator_contract Testing-QA is given),
            # and both specialists only ever READ the same on-disk
            # module files, no write contention. should_run_code_review
            # being True structurally guarantees current_contract.
            # specialist_type != SpecialistType.code_review here (a
            # code_review-primary round's own build_output never has a
            # module_name key at all -- see the comment on the sequential
            # fallback branch below), so effective_code_review_output is
            # always review_output in this branch, never build_output --
            # no ambiguity to resolve. This runs on nearly every
            # module_development retry round, not just a rare case, so
            # the compounding savings across a whole task's round
            # history are real, not marginal. Every OTHER combination
            # (Code-Review runs but verification is skipped; verification
            # runs but Code-Review doesn't apply; neither runs) falls
            # through to the exact original sequential code below,
            # completely unchanged.
            if should_run_code_review and not verification_will_be_skipped:
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": "code_review", "round": round_number, "node_id": current_contract.current_constraint_label,
                    "message": "Reviewing the diff…", "status": "running",
                })
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": current_contract.validation_by, "round": round_number, "node_id": current_contract.current_constraint_label,
                    "message": "Reproducing independently…", "status": "running",
                })
                # Real, confirmed UI gap found live (2026-07-30): asyncio.
                # gather() only returns once BOTH coroutines finish, so
                # both trace "passed"/"failed" events below (published
                # after the gather resolves) get the SAME timestamp --
                # the slower specialist's own real finish time gets
                # correctly reported, but the faster one's own real
                # finish time was never captured anywhere, even though
                # they genuinely ran concurrently and finished at
                # different real moments. Wrapping each coroutine to
                # publish its own immediate, real completion event the
                # instant IT resolves (not waiting for the other) closes
                # this without changing the gather's own concurrency or
                # the existing post-gather fold/publish logic below.
                _code_review_t0 = time.monotonic()
                _testing_qa_t0 = time.monotonic()

                async def _timed_code_review():
                    # P12 Tier A item 16 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_
                    # 2026-07-30.md §4): real, confirmed asymmetry -- _timed_verification()
                    # right below wraps await_verification() in run_with_gateway_outage_
                    # handling(), but this sibling never wrapped run_code_review_diff() the
                    # same way, inside the SAME asyncio.gather() -- a genuine gateway outage
                    # during Code-Review's own call would raise a raw, unhandled
                    # GatewayUnavailableError instead of the same clean gateway_unavailable
                    # pause every other gateway-dependent call in this file gets.
                    result = await run_with_gateway_outage_handling(
                        task_id, module_lock_name,
                        lambda: run_code_review_diff(
                            current_contract, build_output.detail["module_name"],
                            self_report_uncertain=build_output.detail.get("self_report_uncertain"),
                        ),
                    )
                    _specialist_timings_this_round["review"] = time.monotonic() - _code_review_t0
                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": "code_review", "round": round_number, "node_id": current_contract.current_constraint_label,
                        "message": f"Code-Review finished in {time.monotonic() - _code_review_t0:.1f}s (real completion time).",
                        "status": "info",
                    })
                    return result

                async def _timed_verification():
                    # Real, known simplification (2026-08-08): this branch runs Code-Review and
                    # Testing/QA CONCURRENTLY (asyncio.gather below), which genuinely contradicts
                    # the UI design doc's own "at most one specialist active at once" model for a
                    # brief real window -- round-start already published 'build' (specialist_type
                    # is guaranteed bug_fix here, per the comment above this branch), and this
                    # publishes 'qa' as verification starts; the concurrent code_review call is
                    # not separately published in this specific branch, since activeSpecialist is
                    # a single value and 'qa' is the more informative of the two mid-flight.
                    _publish_node_specialist_changed(
                        task_id, current_contract.current_constraint_label, "qa",
                    )
                    result = await run_with_gateway_outage_handling(
                        task_id, module_lock_name,
                        lambda: await_verification(
                            current_contract, build_output, client, classifier_model,
                            code_review_output=None,  # folded in below, once both are back
                            fallback_module_name=last_known_module_name,
                            fallback_db=last_known_db,
                        ),
                    )
                    _specialist_timings_this_round["qa"] = time.monotonic() - _testing_qa_t0
                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": current_contract.validation_by, "round": round_number, "node_id": current_contract.current_constraint_label,
                        "message": f"Testing/QA finished in {time.monotonic() - _testing_qa_t0:.1f}s (real completion time).",
                        "status": "info",
                    })
                    return result

                try:
                    review_output, verification = await asyncio.gather(
                        _timed_code_review(), _timed_verification(),
                    )
                except GatewayOutagePause as pause:
                    _write_gateway_unavailable_checkpoint(
                        task_id, module_for_repeat_check, current_contract, prior_rounds, pause,
                    )
                    return {
                        "status": "paused",
                        "reason": "gateway_unavailable",
                        "message": pause.message,
                        "task_id": task_id,
                        "correction_result": correction_result,
                    }
                verification = fold_code_review_findings(verification, review_output)
                # Real, confirmed bug found live (2026-08-03, task019): the P12 item 11
                # confidence gate used to live inside await_verification() itself, where
                # code_review_output was deliberately passed as None above ("folded in below,
                # once both are back") -- structurally blind to review_output, which IS the real,
                # concurrently-obtained Code-Review result, now genuinely available right here.
                # Applied explicitly, after the real fold, so the gate sees what actually
                # happened this round instead of always assuming no corroboration occurred.
                verification = apply_unverified_shape_confidence_gate(verification, current_contract, review_output)
                blocking = [f for f in review_output.detail.get("findings", []) if f.get("severity") == "blocking"]
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": "code_review", "round": round_number, "node_id": current_contract.current_constraint_label,
                    "message": review_output.summary,
                    "status": "failed" if blocking else "passed",
                })
                if current_contract.current_constraint_label:
                    persist_node_round_step(
                        task_id, current_contract.current_constraint_label, round_number,
                        "review", review_output.summary, "failed" if blocking else "passed",
                        resume_index=current_contract.resumed_from_checkpoint_count,
                    )
                findings = serialize_code_review_findings(review_output.detail)
                if findings:
                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": "code_review", "round": round_number, "node_id": current_contract.current_constraint_label,
                        "kind": "result", "result": {"findings": findings},
                        "message": review_output.summary, "status": "failed" if blocking else "passed",
                    })
                    if current_contract.current_constraint_label:
                        persist_node_round_findings(
                            task_id, current_contract.current_constraint_label, round_number, findings,
                            resume_index=current_contract.resumed_from_checkpoint_count,
                        )
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": current_contract.validation_by, "round": round_number, "node_id": current_contract.current_constraint_label,
                    "message": verification.notes, "status": "passed" if verification.passed else "failed",
                })
                if current_contract.current_constraint_label:
                    persist_node_round_step(
                        task_id, current_contract.current_constraint_label, round_number,
                        "qa", verification.notes, "passed" if verification.passed else "failed",
                        resume_index=current_contract.resumed_from_checkpoint_count,
                    )
                checks = serialize_testing_qa_checks(verification.model_dump())
                if checks:
                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": current_contract.validation_by, "round": round_number, "node_id": current_contract.current_constraint_label,
                        "kind": "result", "result": {"checks": checks},
                        "message": verification.notes, "status": "passed" if verification.passed else "failed",
                    })
                    if current_contract.current_constraint_label:
                        persist_node_round_checks(
                            task_id, current_contract.current_constraint_label, round_number, checks,
                            resume_index=current_contract.resumed_from_checkpoint_count,
                        )
                already_verified_concurrently = True
            elif should_run_code_review:
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": "code_review", "round": round_number, "node_id": current_contract.current_constraint_label,
                    "message": "Reviewing the diff…", "status": "running",
                })
                # P12 Tier A item 16: same gateway-outage-handling wrap as the concurrent
                # _timed_code_review() path above -- this sequential fallback call site had
                # the identical unwrapped gap. Unlike the concurrent path (already inside its
                # own try/except GatewayOutagePause around the asyncio.gather() call), this
                # sequential branch has no enclosing handler at all, so a local try/except is
                # needed right here, same shape as every other real gateway-outage catch site
                # in this function.
                try:
                    _review_t0 = time.monotonic()
                    review_output = await run_with_gateway_outage_handling(
                        task_id, module_lock_name,
                        lambda: run_code_review_diff(
                            current_contract, build_output.detail["module_name"],
                            self_report_uncertain=build_output.detail.get("self_report_uncertain"),
                        ),
                    )
                    _specialist_timings_this_round["review"] = time.monotonic() - _review_t0
                except GatewayOutagePause as pause:
                    _write_gateway_unavailable_checkpoint(
                        task_id, module_for_repeat_check, current_contract, prior_rounds, pause,
                    )
                    return {
                        "status": "paused",
                        "reason": "gateway_unavailable",
                        "message": pause.message,
                        "task_id": task_id,
                        "correction_result": correction_result,
                    }
                blocking = [f for f in review_output.detail.get("findings", []) if f.get("severity") == "blocking"]
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": "code_review", "round": round_number, "node_id": current_contract.current_constraint_label,
                    "message": review_output.summary,
                    "status": "failed" if blocking else "passed",
                })
                if current_contract.current_constraint_label:
                    persist_node_round_step(
                        task_id, current_contract.current_constraint_label, round_number,
                        "review", review_output.summary, "failed" if blocking else "passed",
                        resume_index=current_contract.resumed_from_checkpoint_count,
                    )
                findings = serialize_code_review_findings(review_output.detail)
                if findings:
                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": "code_review", "round": round_number, "node_id": current_contract.current_constraint_label,
                        "kind": "result", "result": {"findings": findings},
                        "message": review_output.summary, "status": "failed" if blocking else "passed",
                    })
                    if current_contract.current_constraint_label:
                        persist_node_round_findings(
                            task_id, current_contract.current_constraint_label, round_number, findings,
                            resume_index=current_contract.resumed_from_checkpoint_count,
                        )

            # --- Phase 5: await verification (gateway-outage + root-cause wired in here) ---
            # Real bug found live (2026-07-15), same shape and same root
            # cause as the sandbox_failed Code-Review gate above, found
            # immediately after deploying that fix: skipping Code-Review
            # correctly stopped the bogus stale-disk critique, but
            # Testing/QA was STILL being invoked below on a
            # sandbox-failed round -- and since a sandbox failure's
            # detail dict never includes "db" (real install into the
            # real target never ran), testing_qa has nothing to verify
            # against and can only report "contract.inputs contains no
            # 'verify_module:' entry -- nothing concrete to verify" --
            # confirmed live: this exact uninformative message became
            # EVERY round's own feedback for a real, repeatedly-failing
            # task, meaning the round loop never once told Build the
            # REAL reason the sandbox install kept failing
            # (sandbox_result.message, already a specific, actionable
            # error -- see specialists/build/specialist.py's
            # _sandbox_preflight()). Skip the pointless verification
            # call entirely and use build_output's own real summary
            # directly, exactly like the readonly_investigation
            # short-circuit already does below for the same reason
            # (nothing meaningful for a second specialist to check).
            # real_install_genuinely_failed() (2026-07-20, Phase 20 Area
            # 2): the same failure family as the sandbox_failed case
            # above, one step later in the pipeline -- see its own
            # docstring for the full story (task #49,
            # hr.employee.badge_expiry_date).
            if already_verified_concurrently:
                pass  # verification (and its trace events) already computed above
            elif (
                current_contract.validation_by is None
                or build_output.detail.get("sandbox_failed")
                or build_output.detail.get("lock_rejected")
                or real_install_genuinely_failed(build_output.detail, build_output.claims_complete)
            ):
                # readonly_investigation: no second specialist to
                # independently verify a pure audit report against --
                # build_output's own claims_complete IS the terminal
                # result here, never routed through await_verification()
                # (which would otherwise try SpecialistType(None) and crash).
                #
                # See build_verification_notes_with_log_tail()'s own
                # docstring for the real bug this fixes (2026-07-21):
                # this branch used to drop the real install error detail
                # on the floor, classifying and retrying blind.
                verification = VerificationResult(
                    task_id=task_id,
                    passed=build_output.claims_complete,
                    reproduction_confirmed=build_output.claims_complete,
                    uncovered_paths=[],
                    coverage_diff="",
                    spot_check_mismatch=False,
                    # Phase 30 §26 follow-up (2026-08-04): real, confirmed gap -- this was the
                    # ONE remaining call site still using the plain fold_specialist_result() for
                    # VerificationResult.notes specifically, while every sibling call site
                    # (manager/tools.py's own two, the readonly_investigation early-return and
                    # the real await_verification() path) was already patched to
                    # fold_verification_notes() (P12 Tier B/C item 28). VerificationResult.notes
                    # is exactly the field build_verification_notes_with_log_tail() just spent
                    # this whole function attaching a real install log_tail to -- folding it with
                    # the naive middle-drop truncation risked silently dropping that same
                    # diagnostic content right back out on a long combined summary+log_tail
                    # string, undermining the fix this call is nested inside.
                    notes=fold_verification_notes(
                        build_verification_notes_with_log_tail(build_output.summary, build_output.detail)
                    ),
                    # Phase 26B follow-up (2026-07-27): a lock rejection is
                    # a clean, already-known, transient reason -- never a
                    # real generation defect worth an LLM root-cause
                    # classification call (skipped below via this being
                    # already set), and never "pattern_worth_a_rule"
                    # (there's no actual mistake to enshrine a rule about).
                    root_cause="one_off" if build_output.detail.get("lock_rejected") else None,
                    # P12 Tier B/C item 29 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_
                    # 2026-07-30.md §4, A Finding 26): real, confirmed gap -- this short-circuit
                    # path never copied build_output.detail["regressed_constraints"], unlike the
                    # normal path (manager/tools.py's await_verification()), silently defaulting
                    # to []. Pass/fail correctness was unaffected (claims_complete is already
                    # False whenever this branch is reached), but detect_oscillation() reads
                    # verification_result.regressed_constraints directly -- a round that
                    # regressed a constraint AND also failed via one of these short-circuit
                    # reasons (sandbox failure, lock rejection, a genuinely failed install) was
                    # invisible to oscillation detection's own remediation note, for parity with
                    # the normal path only in this narrow overlap case.
                    regressed_constraints=list(build_output.detail.get("regressed_constraints", [])),
                )
            else:
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": current_contract.validation_by, "round": round_number, "node_id": current_contract.current_constraint_label,
                    "message": "Reproducing independently…", "status": "running",
                })
                # A second, related real bug found alongside the
                # capability_class one above: when Code-Review IS the
                # round's own primary specialist (routed there by
                # select_specialist_for_retry()'s theme-recurrence
                # check), its own findings live in build_output, not in
                # the separate mid-round review_output (which only ever
                # runs for a module_development/Build round, per the
                # capability_class check above) -- so a blocking finding
                # from a code_review-primary round was silently never
                # checked at all, real or fixed by my earlier
                # capability_class fix alone.
                effective_code_review_output = review_output
                if current_contract.specialist_type == SpecialistType.code_review:
                    effective_code_review_output = build_output
                _publish_node_specialist_changed(
                    task_id, current_contract.current_constraint_label, "qa",
                )
                try:
                    _qa_t0 = time.monotonic()
                    verification = await run_with_gateway_outage_handling(
                        task_id, module_lock_name,
                        lambda: await_verification(
                            current_contract, build_output, client, classifier_model,
                            code_review_output=effective_code_review_output,
                            fallback_module_name=last_known_module_name,
                            fallback_db=last_known_db,
                        ),
                    )
                    _specialist_timings_this_round["qa"] = time.monotonic() - _qa_t0
                except GatewayOutagePause as pause:
                    _write_gateway_unavailable_checkpoint(
                        task_id, module_for_repeat_check, current_contract, prior_rounds, pause,
                    )
                    return {
                        "status": "paused",
                        "reason": "gateway_unavailable",
                        "message": pause.message,
                        "task_id": task_id,
                        "correction_result": correction_result,
                    }
                # Real, confirmed bug found live (2026-08-03, task019): the P12 item 11
                # confidence gate used to live inside await_verification() itself. In THIS
                # (sequential) path it already saw the real effective_code_review_output
                # correctly -- but the gate was moved out of await_verification() entirely (see
                # its own docstring) so both this path and the concurrent one above share one
                # real implementation, applied consistently right here with the same real value
                # already computed above, rather than two copies that could drift apart later.
                verification = apply_unverified_shape_confidence_gate(
                    verification, current_contract, effective_code_review_output,
                )
                publish_trace_event(task_id, {
                    "level": "specialist", "actor": current_contract.validation_by, "round": round_number, "node_id": current_contract.current_constraint_label,
                    "message": verification.notes, "status": "passed" if verification.passed else "failed",
                })
                if current_contract.current_constraint_label:
                    persist_node_round_step(
                        task_id, current_contract.current_constraint_label, round_number,
                        "qa", verification.notes, "passed" if verification.passed else "failed",
                        resume_index=current_contract.resumed_from_checkpoint_count,
                    )
                checks = serialize_testing_qa_checks(verification.model_dump())
                if checks:
                    publish_trace_event(task_id, {
                        "level": "specialist", "actor": current_contract.validation_by, "round": round_number, "node_id": current_contract.current_constraint_label,
                        "kind": "result", "result": {"checks": checks},
                        "message": verification.notes, "status": "passed" if verification.passed else "failed",
                    })
                    if current_contract.current_constraint_label:
                        persist_node_round_checks(
                            task_id, current_contract.current_constraint_label, round_number, checks,
                            resume_index=current_contract.resumed_from_checkpoint_count,
                        )

            if not verification.passed:
                # Real, general architectural fix (2026-07-17, Phase 20
                # Area 2) -- see cleanup_module_from_failed_round()'s own
                # docstring for the full story: a round whose OWN
                # install genuinely succeeded but got rejected on a
                # LATER, normal verification failure used to leave that
                # real install permanently contaminating the shared
                # database with no cleanup path at all.
                # Phase 22 (2026-07-23): cleanup_module_from_failed_round()
                # (mutates live Odoo state -- uninstalls the module) and
                # classify_root_cause() (a pure, stateless text
                # classification of verification.notes, no DB
                # interaction at all) are fully independent -- neither
                # depends on the other's result, confirmed by reading
                # both functions' own bodies. Gathered only in the
                # branch where classify_root_cause would have actually
                # run anyway (verification.root_cause already set means
                # it's skipped entirely, same as before -- this must
                # never add an LLM call that wasn't happening already).
                # Phase 22 follow-up (2026-07-23): converted from a
                # hand-picked gather()-or-not branch to the generic
                # manager.step_scheduler -- the root_cause step is only
                # ADDED to the step list at all when it would have run
                # anyway (verification.root_cause not already set),
                # otherwise its value is seeded directly into the
                # scheduler's starting context -- preserving the exact
                # same "never add an LLM call that wasn't happening
                # already" invariant as the branch this replaces.
                async def _step_cleanup(_context):
                    return {
                        "cleanup_result": await cleanup_module_from_failed_round(
                            build_output.detail, build_output.claims_complete
                        )
                    }

                cleanup_steps = [
                    Step(name="cleanup", reads=frozenset(), writes=frozenset({"cleanup_result"}), run=_step_cleanup),
                ]
                cleanup_context: dict = {}
                if not verification.root_cause:
                    # Phase 30, P2c (§13, item 5): the SAME task's own
                    # recent prior rounds, oldest first, last 4 -- real
                    # context so classify_root_cause() can recognize a
                    # recurring failure instead of judging this round's
                    # text in total isolation (see that function's own
                    # docstring for the real school_student incident
                    # this closes: 28/29 rounds mislabeled one_off).
                    recent_round_summaries = [
                        f"Round {r.round_number} ({r.verification_result.root_cause or 'unclassified'}): "
                        f"{r.verification_result.notes}"
                        for r in prior_rounds[-4:]
                    ]
                    # P12 Tier A item 22 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_
                    # 2026-07-30.md §4, A Finding 25): real, confirmed gap -- prior_rounds is
                    # reset to [] on mid-loop compression (see compress_round_history_mid_loop's
                    # own call site below), but manager_history_summary (the real, compressed
                    # text summary of exactly those dropped rounds) was never folded in here,
                    # so classify_root_cause() lost all visibility into any round that happened
                    # before the most recent compression -- a task that hit the same failure
                    # 3 times, then compressed, then hit it a 4th time would see recent_round_
                    # summaries as if this were only the 2nd occurrence. Prepended (oldest
                    # context first, matching the existing oldest-first ordering of prior_
                    # rounds[-4:] itself) only when real compressed content exists.
                    if manager_history_summary:
                        recent_round_summaries = [
                            f"Earlier rounds (compressed): {manager_history_summary}",
                            *recent_round_summaries,
                        ]

                    async def _step_root_cause(_context):
                        return {
                            "root_cause": await classify_root_cause(
                                failure_summary=verification.notes, client=client, model=FAST_EXTRACTION_MODEL,
                                recent_round_summaries=recent_round_summaries,
                            )
                        }

                    cleanup_steps.append(
                        Step(name="root_cause", reads=frozenset(), writes=frozenset({"root_cause"}), run=_step_root_cause)
                    )
                else:
                    cleanup_context["root_cause"] = verification.root_cause
                cleanup_step_results = await run_steps(cleanup_steps, cleanup_context)
                cleanup_result = cleanup_step_results["cleanup_result"]
                root_cause = cleanup_step_results["root_cause"]
                if cleanup_result is not None:
                    cleanup_ok, cleanup_message = cleanup_result
                    publish_trace_event(task_id, {
                        "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                        "message": (
                            f"Round {round_number}'s own install succeeded but the round failed "
                            f"verification -- cleanup {'succeeded' if cleanup_ok else 'FAILED (best-effort, continuing anyway)'}: "
                            f"{cleanup_message}"
                        ),
                        "status": "passed" if cleanup_ok else "failed",
                    })

                # A real, previously-masked gap (Phase 12): this only
                # ever worked before because await_verification() (in
                # manager/tools.py) separately assigned root_cause onto
                # the VerificationResult it returned -- this local
                # variable was never written back here. Fixed generally.
                verification = verification.model_copy(update={"root_cause": root_cause})
                # Per-round learning, distinct from replanning itself
                # (§19.1's own framing: error-learning is "learning for
                # next time"; replanning is "fix THIS task, right now") --
                # every round's failure is real evidence worth routing,
                # not just the final one.
                await handle_failed_verification(
                    task_id=task_id,
                    module=module_for_repeat_check,
                    failure_summary=verification.notes,
                    root_cause=root_cause,
                )

            if verification.passed:
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                    "message": f"Round {round_number}", "status": "passed",
                })
                _persist_round_timings_for_this_round()
                break

            elapsed_seconds = time.monotonic() - started_at
            _persist_round_timings_for_this_round()

            # Real live visibility into the proactive token-pressure
            # check (the project owner's own explicit request: "I don't see how many
            # tokens he already has right now" -- should_escalate_to_operator()
            # computed this every round already, but never surfaced the
            # real number anywhere -- it was a genuine black box). Publishes
            # every round regardless of whether the threshold is crossed,
            # so the live trend is visible before it ever triggers anything.
            pressure = estimate_context_pressure(current_contract)
            window = model_context_window(current_contract.specialist_type)
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "context_pressure", "round": round_number,
                "status": "info",
                "message": f"Round {round_number} context pressure: {pressure * 100:.1f}%",
                "context_pressure": {
                    "fraction": pressure, "soft_threshold": SOFT_TRIGGER_FRACTION,
                    "hard_threshold": HARD_CEILING_FRACTION, "window": window,
                },
            })

            # Real, confirmed structural bug found live: this escalation
            # check used to run BEFORE the round got recorded below
            # (prior_rounds.append + the durable replan_round write), so
            # whenever a round's own failure was what actually TRIGGERED
            # escalation, that round's own real work -- its verification
            # result, its Code-Review finding, everything -- was silently
            # discarded, never saved anywhere, never shown to Operator in the
            # escalation summary. Confirmed live: a real task escalated
            # after what its own summary called "4 round(s)" of retries,
            # but /api/rounds only ever had 3 recorded -- the 4th, the
            # one that actually caused the give-up, was the one lost.
            # Fix: always compute and record this round's own log first
            # (revise_contract_from_verification is deterministic, no
            # LLM call, safe to run regardless of what happens next);
            # only the escalation check itself, and whether the round
            # loop continues into a next iteration, are conditional.
            # P12 Tier A item 19: prior_rounds now includes this round's own just-recorded
            # entry (per the fix note right above this call -- recording always happens
            # before this check), so detect_verification_never_executed() sees the real,
            # current window.
            escalation_reason = should_escalate_to_operator(
                current_contract, round_number, elapsed_seconds, verification, prior_rounds=prior_rounds,
            )

            # P13 item 12b (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
            # 2026-07-29.md §22.2): a lightweight "second attempt, different shape" recovery pass
            # for root_cause=one_off failures -- these are, by Phase 29's own design, never
            # promoted to a rule/validator/P-item (only 3+-instance pattern_worth_a_rule clusters
            # are), so a genuine one-off gets zero systematic benefit from anything else this
            # codebase builds. Bounded, honest, and marginal by design (the item's own text:
            # "does not and cannot close the long-tail gap... converts some fraction into
            # occasionally-recoverable, at a known, capped cost"). Capped at EXACTLY one extra
            # attempt per task via one_off_recovery_used -- never compounds, never changes
            # round-budget semantics for skill_gap/pattern_worth_a_rule, which keep their own
            # existing systematic handling untouched. Only overrides the round_budget reason
            # specifically -- wall_clock/context_pressure/repeated_failure/gates_disagree all stay
            # real, hard stops (this is a budget EXTENSION, not a bypass of genuine infra/policy
            # limits).
            one_off_recovery_granted = False
            if (
                escalation_reason == "round_budget"
                and verification.root_cause == "one_off"
                and not current_contract.one_off_recovery_used
            ):
                escalation_reason = None
                one_off_recovery_granted = True
                current_contract = current_contract.model_copy(update={"one_off_recovery_used": True})
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                    "message": (
                        "Round budget reached, but root_cause=one_off and this task's own bounded "
                        "recovery attempt hasn't been used yet -- granting exactly one more round "
                        "with a structurally different framing before escalating."
                    ),
                    "status": "info",
                })

            # Real, confirmed bug found live (2026-07-28, Phase 28C,
            # school_student task's own live run): the SAME class of
            # problem the comment above this whole block already
            # documents (Code-Review's own text poisoning the next
            # round's prompt) in a genuinely different instantiation --
            # that earlier fix (the `not sandbox_failed` guard on
            # `should_run_code_review`) only ever prevented Code-Review
            # from running against STALE DISK CONTENT after a sandbox
            # failure. Here, Code-Review genuinely ran against the real,
            # current round's own real content and correctly found ZERO
            # blocking findings (`blocking == []`, confirmed live,
            # matching the round's own real `verification_result.notes`
            # showing no Code-Review complaints at all) -- but its own
            # separate, freeform `.summary` field ("The proposed change
            # fails to meet the round's scope constraints...") still
            # echoed a stale, unrelated rejection narrative from deep in
            # this task's own accumulated history. That summary text was
            # being unconditionally threaded through into `revision_
            # reasoning`/`code_review_finding_summary`, which the NEXT
            # round's own prompt trusts as real, current feedback --
            # confirmed live via the round loop's own escalation-summary
            # LLM call quoting it verbatim as "the exact same Code-Review
            # finding" across 5 straight rounds, even though none of
            # those 5 rounds' own real, structured findings ever
            # contained it. Only the STRUCTURED, deterministic findings
            # list is ever independently verified true-or-false by this
            # project's own `_filter_hallucinated_*` family (specialists/
            # code_review/specialist.py) -- the freeform summary text has
            # no equivalent safeguard and must never be trusted on its
            # own. Fixed by gating on `blocking` (only ever pass the
            # summary through when Code-Review's OWN structured findings
            # genuinely still contain a real blocking issue) rather than
            # merely on `review_output` being non-None.
            new_contract, reasoning = revise_contract_from_verification(
                current_contract, verification, round_number,
                code_review_finding=(review_output.summary if review_output and blocking else None),
            )

            # P13 item 8 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
            # 2026-07-29.md §22.2): deterministic, zero-LLM pre-check -- run immediately after
            # this round's own FailureRecord exists (item 7, just built above by
            # revise_contract_from_verification()), before the next round's Build call would
            # otherwise dispatch. Compares the newest record against current_contract's own
            # PRE-revision history (never against itself -- new_contract already has the
            # newest record appended, current_contract does not yet). Only sets
            # escalation_reason when nothing has already claimed it -- a genuine repeat is a
            # real reason to stop, but never overrides a different, already-decided escalation.
            #
            # Real, confirmed gap found and fixed 2026-08-02 (Sonnet 5 A/B follow-up, task005,
            # docs/reports/PHASE30_P14_ITEM3_SONNET5_FOLLOWUP_2026-08-02.md): this whole check was
            # previously gated on `current_contract.current_constraint_label`, which is only ever
            # set for a DECOMPOSED (3+-constraint) task -- a plain, single-requirement task (the
            # more common real shape, and exactly task005's own goal) never sets it, so this gate
            # never ran regardless of how many rounds repeated the identical finding. Confirmed
            # live: task005 hit the same real Code-Review finding (field never exposed on the form
            # view) in rounds 4 and 5, verbatim-adjacent, invisible to every existing oscillation
            # detector (detect_oscillation needs a prior SATISFIED-then-regressed constraint,
            # never true here; detect_shape_oscillation only fires on non-adjacent recurrence, by
            # design skipping an ordinary two-in-a-row repeat; detect_failure_diversity needs 3+
            # DISTINCT root causes, but classify_root_cause() has no cross-round context and
            # returned 'one_off' every single round). Fixed generally, not just for task005's own
            # shape: falls back to the contract's own flat failure_records history whenever no
            # constraint-node history exists, using the exact same is_known_repeat() detection
            # logic either way -- never a second, competing check for the non-decomposed case.
            if not escalation_reason:
                newest_failure_record = new_contract.failure_records[-1] if new_contract.failure_records else None
                if newest_failure_record:
                    prior_node = (
                        current_contract.constraint_nodes.get(current_contract.current_constraint_label)
                        if current_contract.current_constraint_label else None
                    )
                    repeat_history = prior_node.failure_records if prior_node else current_contract.failure_records
                    if repeat_history and is_known_repeat(newest_failure_record, repeat_history):
                        escalation_reason = "known_repeat_pre_check"

            # P13 item 12b: the "structurally different" shape this bounded recovery round asks
            # for -- reuses item 13's own external-role framing mitigation (Self-Correction
            # Illusion: 0-17% self-correction vs. 23-93 points higher when framed as an external
            # role's verdict), presenting the ask as if from an unrelated third party rather than
            # the model's own prior self-assessment, per the item's own explicit design ("not new
            # prompt engineering" -- the SAME mitigation, applied to this one bounded extra round).
            if one_off_recovery_granted:
                new_contract = new_contract.model_copy(update={
                    "rules": [
                        *new_contract.rules,
                        (
                            "An independent reviewer, looking at this task fresh after several "
                            "failed attempts, notes: the prior approach hasn't worked -- this is a "
                            "bounded, one-time extra attempt. Try a genuinely DIFFERENT concrete "
                            "approach than what was already tried, not a repetition of the same "
                            "shape with minor tweaks."
                        ),
                    ],
                })

            # Real, confirmed architectural gap found live: a task that
            # references or extends a model built by an EARLIER task
            # (e.g. "the service module") has no way to know which
            # module actually defines it -- every scaffolded module gets
            # a fresh, randomly-suffixed name, never a stable, guessable
            # one. Build kept failing the identical "missing dependency"
            # Code-Review finding round after round on a real live task,
            # not from carelessness -- the correct module name genuinely
            # wasn't knowable from the goal text alone. Rather than keep
            # asking Build to guess, resolve it for real: when a round's
            # own Code-Review finding names a missing-dependency model,
            # look it up against every module actually on disk
            # (find_module_defining_model(), the one real source of
            # truth) and hand the next round the concrete, correct
            # answer instead of the same unresolvable ambiguity again.
            finding_texts = []
            if review_output:
                finding_texts.extend(
                    f.get("explanation", "") for f in review_output.detail.get("findings", [])
                )
            finding_texts.append(verification.notes or "")
            dependency_model, resolved_module = await resolve_missing_dependency_module(finding_texts)
            if resolved_module:
                if resolved_module != module_for_repeat_check:
                    # Real, confirmed fix upgrade: a plain appended rules-
                    # list entry, even a correct and clearly-worded one,
                    # got buried and ignored once it was one of 16
                    # accumulated rules from a long round/resume history
                    # -- confirmed live, Build's own generated manifest
                    # never picked it up. BuildSpecialist already has a
                    # real, HARD-ENFORCED mechanism for exactly this
                    # shape ("depends_on_module:<name>" in contract.
                    # inputs -- originally built for outer-plan-item
                    # dependencies, but the convention itself is generic):
                    # it shows Build the target module's REAL source code
                    # directly in its own prompt, under a dedicated
                    # CRITICAL heading, and _validate_manifest_declares_
                    # dependency() outright REJECTS a manifest that
                    # doesn't declare it, forcing a real retry rather than
                    # hoping the model notices a buried instruction. Using
                    # the same real mechanism the codebase already trusts
                    # for this exact problem, instead of a second, weaker
                    # one, is both more effective and more consistent.
                    depends_on_entry = f"depends_on_module:{resolved_module}"
                    if depends_on_entry not in new_contract.inputs:
                        new_contract = new_contract.model_copy(
                            update={"inputs": [*new_contract.inputs, depends_on_entry]}
                        )
                    publish_trace_event(task_id, {
                        "level": "manager", "actor": "manager", "phase": "note", "round": round_number,
                        "message": f"Resolved missing dependency: {dependency_model} -> {resolved_module}",
                        "status": "info",
                    })

            next_specialist = await select_specialist_for_retry(
                current_contract, verification, prior_rounds, client, manager_model
            )
            if next_specialist == SpecialistType.code_review:
                # Real, confirmed bug (found live): routing a round's OWN
                # primary specialist to code_review only ever updated
                # specialist_type, never capability_class/inputs --
                # CodeReviewSpecialist.run() requires capability_class=
                # readonly_investigation and a real "diff_module:<name>"
                # input to work at all (the exact same transformation
                # run_code_review_diff()'s own mid-round consultation
                # already applies correctly). Without this, the round
                # errored out immediately ("only handles
                # capability_class=readonly_investigation"), the loop
                # fell through to Testing/QA's OWN functional check
                # alone, and a task genuinely passed with a real,
                # recurring Code-Review security finding never actually
                # re-verified as fixed -- confirmed live, not a
                # hypothetical. module_for_repeat_check is used as the
                # real target -- last_known_module_name is only set
                # AFTER a round that was itself Build, which this round
                # (by definition, since it's routing TO code_review) is
                # not guaranteed to have just been.
                target_module = last_known_module_name or module_for_repeat_check
                update = {"specialist_type": next_specialist, "capability_class": CapabilityClass.readonly_investigation}
                if target_module:
                    update["inputs"] = [f"diff_module:{target_module}"]
                new_contract = new_contract.model_copy(update=update)
            else:
                # The other half of the same bug, found live in the very
                # next round after the fix above: routing OUT of
                # code_review back to bug_fix/testing_qa (the real,
                # existing bounce-back rule in select_specialist_for_retry)
                # never reset capability_class back either -- it just
                # inherited readonly_investigation from the code_review
                # round before it, and BuildSpecialist immediately
                # rejected it ("does not handle capability_class=
                # readonly_investigation"). contract (the ORIGINAL,
                # never-mutated top-level contract for this task) is the
                # one honest source for "what capability_class should
                # this task actually run as" whenever we're not
                # deliberately in a code_review-only round.
                update = {"specialist_type": next_specialist}
                if current_contract.capability_class == CapabilityClass.readonly_investigation:
                    update["capability_class"] = contract.capability_class
                new_contract = new_contract.model_copy(update=update)

            # Real bug closed 2026-08-08 (the project owner's own direct investigation, task
            # 07936710-e7a0-41d2-bc96-9282feebe3a8): without this, the replan_round row about to
            # be written below starts its own constraint_nodes snapshot from whatever this
            # in-memory contract object already holds, which is disconnected from any live SQL
            # telemetry patch (round_number/active_specialist/state) that landed on the PRIOR
            # replan_round row while this round was actually running -- confirmed live, a node
            # correctly showing round_number=2 on one row reset to round_number=0 on the very
            # next row created afterward. Sync the real, currently-persisted telemetry into
            # new_contract.constraint_nodes now, right before it becomes the new row's own frozen
            # snapshot -- display-continuity only, no execution-path behavior changes.
            if new_contract.constraint_nodes:
                sync_live_node_telemetry_into_new_snapshot(task_id, new_contract.constraint_nodes)

            round_log = ReplanRound(
                round_number=round_number,
                previous_contract_task_id=task_id,
                verification_result=verification,
                # Same fix and same reasoning as the `code_review_finding`
                # gate above (see that comment for the full, real,
                # confirmed-live incident this closes) -- this durable
                # log record is what other consumers (recurrence
                # detection, token-budget estimation, manager/
                # replanning.py) read directly, so it needs the identical
                # `blocking`-gated fix, not just the in-memory prompt
                # construction above.
                code_review_finding_summary=(review_output.summary if review_output and blocking else None),
                revision_reasoning=reasoning,
                new_contract=new_contract,
                # Phase 18 (§22.4/§22.13): the real Gitea commit SHA this
                # round's output landed as, when BuildSpecialist reached
                # that point -- None for a round that never got there
                # (e.g. validator-rejected pre-write, or a non-module_dev
                # round with no commit_sha key in its own detail at all).
                commit_sha=build_output.detail.get("commit_sha"),
                # Phase 18 (§22.7/§22.13): how many candidates Build
                # actually generated this round -- 1 for the ordinary
                # single-candidate path, 2-3 when best-of-N sampling ran
                # (a round holding 2+ live constraints at once).
                candidate_count=build_output.detail.get("candidate_count", 1),
                # Phase 30, P1d (§15): popped (read + cleared) exactly
                # once per round, right where this round's own log gets
                # built -- every generate() call this round made, across
                # every specialist, was already accumulating against this
                # task_id since the LAST pop (the previous round's own,
                # or task start for round 1).
                **_pop_llm_call_stats(client, task_id),
            )
            prior_rounds.append(round_log)
            append_project_memory(
                event_type="replan_round",
                actor="manager",
                task_id=task_id,
                module=module_for_repeat_check,
                summary=f"Round {round_number} revised: {reasoning[:200]}",
                tags=["replan_round"],
                detail=round_log.model_dump(mode="json"),
            )
            # Phase 30, P1d (§15): informational only -- flags, never
            # gates or pauses. A signal for Phase E's (§8) quarterly
            # review, surfaced here (a note + a trace event) so it's
            # visible now rather than only discoverable by a future query.
            if round_log.llm_call_count:
                outlier = flag_llm_call_count_outlier(
                    round_log.llm_call_count, new_contract.capability_class.value,
                )
                if outlier:
                    append_project_memory(
                        event_type="note", actor="manager", task_id=task_id, module=module_for_repeat_check,
                        summary=outlier["message"], tags=["llm_call_count_outlier"], detail=outlier,
                        verified=True,
                    )
                    publish_trace_event(task_id, {
                        "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                        "message": outlier["message"], "status": "info",
                    })
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                "message": f"Round {round_number} failed -- {reasoning}", "status": "failed",
            })

            if escalation_reason:
                # prior_rounds now genuinely includes this round's own
                # record (the fix above) -- Operator's escalation summary and
                # the round-checkpoint used for Continue/resume both see
                # the real, complete history, including the very round
                # that triggered the give-up.
                ask_operator(
                    f"I've tried {round_number} round(s) on this task and it still isn't passing "
                    f"({verification.notes}). I don't think another retry on my own will help right "
                    f"now -- can you take a look?"
                )
                raise PauseForOperator(task_id, verification.notes, prior_rounds, reason=escalation_reason)

            # Real, proactive mid-loop check (the project owner's own explicit
            # design, confirmed against real 2025-2026 multi-agent
            # orchestration practice): the Manager's OWN memory, checked
            # every round, independent of and earlier than any
            # escalation condition. Crossing the soft threshold triggers
            # a real summarization -- never a stop, the loop continues
            # immediately after. contract.goal and every one of Operator's
            # own clarifications (folded into current_contract.rules
            # elsewhere, never into this check) are never compressed --
            # only the round-by-round history itself is.
            manager_pressure = estimate_manager_context_pressure(
                contract.goal, prior_rounds, manager_history_summary, manager_model,
            )
            publish_trace_event(task_id, {
                "level": "manager", "actor": "manager", "phase": "manager_context_pressure",
                "round": round_number, "status": "info",
                "message": f"Manager's own memory: {manager_pressure * 100:.1f}%",
                "manager_context_pressure": {"fraction": manager_pressure, "threshold": SOFT_TRIGGER_FRACTION},
            })
            if manager_pressure >= SOFT_TRIGGER_FRACTION:
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "branch_message",
                    "status": "running", "message": "My own memory is getting full -- summarizing older rounds…",
                })
                manager_history_summary = await compress_round_history_mid_loop(
                    contract.goal, prior_rounds, manager_history_summary, client, manager_model, task_id,
                )
                dropped_count = len(prior_rounds)
                prior_rounds = []
                append_project_memory(
                    event_type="note", actor="manager", task_id=task_id, module=module_for_repeat_check,
                    summary=f"Manager's own round history compressed mid-loop ({dropped_count} round(s) folded into a rolling summary)",
                    tags=["mid_loop_history_compressed"],
                    detail={"round_count_compressed": dropped_count, "summary": manager_history_summary},
                    verified=True,
                )
                mid_loop_text = "Summarized older rounds -- the goal and everything Operator said stay untouched."
                append_project_memory(
                    event_type="branch_message", actor="manager", task_id=task_id, module=None,
                    summary=mid_loop_text, tags=["mid_loop_compression"],
                    detail={"role": "manager", "kind": "mid_loop_compression", "text": mid_loop_text},
                    verified=True,
                )
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "branch_message", "status": "passed",
                    "message": mid_loop_text,
                    "branch_message": {"role": "manager", "kind": "mid_loop_compression", "text": mid_loop_text},
                })

            # Phase 18 (§22.11): a deterministic, code-only check for
            # the "fixing A drops B" pattern (regressed_constraints,
            # §22.10, across the last few rounds). The ONLY consequence
            # of firing is forcing a narrower, single-constraint focus
            # for the NEXT round -- never a cloud-model escalation (see
            # detect_oscillation()'s own docstring for why). Implemented
            # as a CRITICAL_RULE_PREFIX instruction (§22.5's own
            # mechanism for guaranteeing something survives compaction
            # and renders in Build's highest-salience section) rather
            # than routing through decompose_into_constraints()/
            # _run_decomposed_task() (§22.9), which only ever runs at
            # contract-creation time -- reaching it mid-flight, inside
            # an already-running round loop, would need a larger
            # restructuring than this phase's own scope justifies; both
            # mechanisms serve the same "work on one thing at a time"
            # goal, just triggered at different points in the loop.
            # Phase 30, P2c (§13, closes Problem I): the real "step back
            # and reconsider" move -- when oscillation has fired across a
            # MAJORITY of this task's own rounds, or the task shows
            # genuine problem-hopping (detect_failure_diversity()), the
            # honest answer to "would a strong coding agent keep
            # narrowing here" is no. Computed once, feeds every branch
            # below (including the new problem-hopping-only branch,
            # which neither detect_oscillation() nor
            # detect_shape_oscillation() can reach on their own, since
            # both require a shape to RECUR).
            reconsider_signal = oscillation_majority_signal(prior_rounds) or detect_failure_diversity(prior_rounds)
            if reconsider_signal:
                # Item 1's own real design: full current round history
                # (not just the latest diff/error) -- every prior round's
                # own real failure text, not a summary of a summary.
                full_round_history_text = "\n".join(
                    f"Round {r.round_number} ({r.verification_result.root_cause or 'unclassified'}): "
                    f"{r.verification_result.notes}"
                    for r in prior_rounds
                )
                reconsider_note = (
                    "STEP BACK AND RECONSIDER (not another narrow patch): this task has been "
                    "repeatedly failing without converging. Before making any further edit, read the "
                    "full round history below and explicitly decide whether the CURRENT approach is "
                    "structurally wrong -- not just which specific line to patch this time. If it is, "
                    "take one clean, different fix over another incremental patch, even if that means "
                    "restructuring the part of the module you've been touching. If it genuinely is not "
                    "(the approach is right, just not yet fully correct), say so and proceed narrowly "
                    "as before.\n\nFull round history so far:\n" + full_round_history_text
                )
                new_contract = new_contract.model_copy(
                    update={"rules": [*new_contract.rules, f"{CRITICAL_RULE_PREFIX}{reconsider_note}"]}
                )
                # Item 3's own fix: actively surfaced (a real, live trace
                # event a human watching this task would actually see),
                # not only a durable note nobody is looking at -- the
                # same "genuinely visible, not a log line" pattern P3
                # (§10) already established for governance rules.
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                    "message": (
                        "Reconsider-approach round triggered: this task has been oscillating across a "
                        "majority of its rounds, or hopping between different failure types without "
                        "converging -- routing to a full-context 'is this approach structurally wrong' "
                        "round instead of another narrow patch."
                    ),
                    "status": "paused",
                })
                append_project_memory(
                    event_type="note", actor="manager", task_id=task_id, module=module_for_repeat_check,
                    summary=(
                        "Reconsider-approach round triggered (oscillation majority and/or failure "
                        "diversity) -- given full round history and asked to judge whether the "
                        "current approach is structurally wrong, not just narrowed further."
                    ),
                    tags=["reconsider_approach_round", "oscillation_majority_signal"],
                    detail={
                        "prior_round_count": len(prior_rounds),
                        "oscillation_majority": oscillation_majority_signal(prior_rounds),
                        "failure_diversity": detect_failure_diversity(prior_rounds),
                    },
                    verified=True,
                )
            elif detect_oscillation(prior_rounds):
                most_recent_regression = round_log.verification_result.regressed_constraints
                focus_note = (
                    "Oscillation detected: fixing one constraint has been dropping another. "
                    "This round, work on EXACTLY ONE thing at a time -- re-confirm "
                    f"{most_recent_regression!r} without touching anything else the CONSTRAINT "
                    "STATUS ledger above already marks SATISFIED."
                )
                new_contract = new_contract.model_copy(
                    update={"rules": [*new_contract.rules, f"{CRITICAL_RULE_PREFIX}{focus_note}"]}
                )
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                    "message": "Oscillation pattern detected -- forcing single-constraint focus next round.",
                    "status": "info",
                })

            # Real, general bug found live (2026-07-11, same day as the
            # check above): detect_oscillation() is structurally blind to
            # a task that has never once satisfied any constraint --
            # regressed_constraints requires a prior SATISFIED state to
            # regress FROM, so it stays empty[] every round for a task
            # stuck at zero progress, no matter how much the underlying
            # code is genuinely alternating between unresolved approaches.
            # Confirmed live: Operator's service-management task oscillated
            # between two distinct uncovered_paths shapes across 10
            # straight rounds with this check never firing once.
            # detect_shape_oscillation() (manager/replanning.py) is the
            # complementary signal for exactly that population, using the
            # same uncovered_paths fuzzy-shape matching already built for
            # revise_contract_from_verification()'s own recurring-gap
            # escalation. Feeds the SAME existing remedy (force a single-
            # constraint focus for the next round) rather than a new
            # mechanism -- both are "stop alternating, commit to fixing
            # one thing" in substance, just triggered by different signals.
            elif detect_shape_oscillation(prior_rounds):
                gap = round_log.verification_result.uncovered_paths
                focus_note = (
                    "Oscillation detected: this task has been alternating between different "
                    "unresolved code shapes across rounds without ever converging on one -- not "
                    "linear progress. This round, commit to ONE concrete approach for the "
                    f"currently-untested code ({gap!r}) and see it all the way through: either "
                    "delete it (if out of scope) or make it genuinely covered/tested. Do not "
                    "restructure this part of the module again unless directly instructed to."
                )
                new_contract = new_contract.model_copy(
                    update={"rules": [*new_contract.rules, f"{CRITICAL_RULE_PREFIX}{focus_note}"]}
                )
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                    "message": "Shape-oscillation pattern detected -- forcing single-approach focus next round.",
                    "status": "info",
                })

            # Phase 26B follow-up (2026-07-27): a lock rejection is a
            # genuinely transient condition -- the other task holding
            # this same real model's lock is very likely to finish and
            # release it within a normal build/install cycle (measured
            # live: ~2-3 minutes for an ordinary successful round). Every
            # OTHER retry reason above loops back to the next round
            # immediately, correct for a real generation defect that
            # needs a different APPROACH, not more elapsed time -- but a
            # lock rejection needs neither a different approach nor an
            # instant retry; it needs the OTHER task to finish. Without
            # this, a genuinely resolvable collision (confirmed live: two
            # real tasks on the same model, one legitimately succeeded
            # while the other got rejected) burned its entire round
            # budget in under 8 seconds and escalated to Operator needlessly,
            # instead of the second task simply succeeding once the first
            # one finished. Bounded and short (never more than a handful
            # of seconds total across the whole round budget) -- this is
            # a real wait for a real, close-by event, never a guess at an
            # unknown duration.
            if build_output.detail.get("lock_rejected"):
                publish_trace_event(task_id, {
                    "level": "manager", "actor": "manager", "phase": "round", "round": round_number,
                    "message": (
                        f"Round {round_number} was rejected -- another task is currently editing "
                        f"the same model. Waiting briefly before retrying…"
                    ),
                    "status": "info",
                })
                await asyncio.sleep(_LOCK_REJECTED_RETRY_BACKOFF_SECONDS)

            current_contract = new_contract
            round_number += 1
    except PauseForOperator as pause:
        release_module_lock(module_lock_name, task_id)
        store_pending_escalation(task_id, pause.message, pause.prior_rounds)
        # Real, confirmed bug found live (2026-07-22, Operator demo prep): this
        # handler released the module lock and stored the pending escalation,
        # but never transitioned oma:task:<task_id>:state away from
        # STATE_RUNNING (set by mark_task_running() at the top of every
        # round) -- it only got cleared by a HUMAN manually clicking Pause
        # in the UI (POST /api/tasks/{id}/pause -> the same
        # PAUSE_ROUND_BUDGET_EXHAUSTED value set here), never automatically
        # on a genuine escalation itself. Confirmed live via direct Redis/
        # Postgres inspection: two tasks that had correctly, intentionally
        # reached this exact "waiting on Operator" state -- one hours earlier,
        # one moments before a routine service restart -- both still had
        # oma:task:<id>:state == "running". ui/chat/server.py's own startup
        # reconciliation sweep (lifespan()) treats ANY stale "running" key
        # with no recent trace activity as evidence of a process-killed
        # orphan, and had no way to tell that apart from a legitimate,
        # patiently-waiting escalation -- it wrote a false
        # "terminated_stuck_call" decision row for both and cleared their
        # real pending_escalation entries. That corrupted
        # manager.dashboard._standalone_task_status() two different ways:
        # a single-contract task fell into the cancelled/terminated_stuck_call
        # branch and showed "failed -- Stopped -- was stuck on a hung call"
        # instead of its true escalated state; a decomposed task with an
        # earlier constraint's own intermediate `outcome=passed` row (see
        # that function's own Phase-18 comment) had Redis state cleared to
        # None by the sweep, which made the outcome-row-trust check treat
        # the STALE, INTERMEDIATE pass as the whole task's final word,
        # showing a task that genuinely escalated as "passed". Fixed at the
        # source: mark this Redis state the same way the manual Pause
        # button already does, at the moment of the real escalation itself,
        # not only when a human happens to click Pause afterward -- this
        # also makes dashboard.py's existing PAUSE_ROUND_BUDGET_EXHAUSTED
        # check (checked before pending_escalation_ids) correctly show the
        # task as paused even if the pending_escalation entry is ever lost,
        # and -- the actual fix for the corruption above -- keeps the
        # startup sweep from ever matching it at all, since it only
        # targets keys literally equal to STATE_RUNNING, never any
        # paused:* variant.
        set_task_state(task_id, PAUSE_ROUND_BUDGET_EXHAUSTED)

        # Phase 17 (§21.5.3): a real, human-facing summary -- separate
        # from pause.message (the existing technical verification.notes
        # text, still shown, unchanged) -- plus the cold-tier checkpoint
        # this whole escalation/resume mechanism is built on.
        #
        # Live delivery (added after the project owner's own "I want to see this
        # happening, not just the end state" request): a real "working on
        # it" event goes out on the SAME oma:trace:{task_id} channel the
        # Agents panel already listens on, before the summary exists --
        # branch_messages previously only reached the UI on the next 6s
        # poll, which read as static/dead while a real LLM call was
        # actually in flight.
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "branch_message",
            "status": "running", "message": "Summarizing what happened across these rounds…",
        })
        human_summary = await summarize_escalation_for_operator(
            contract.goal, pause.prior_rounds, pause.reason, client, manager_model,
            task_id=task_id, pre_compressed_summary=manager_history_summary,
        )
        checkpoint_detail = {
            "human_summary": human_summary,
            "prior_rounds": [r.model_dump(mode="json") for r in pause.prior_rounds],
            "round_count": len(pause.prior_rounds),
            "last_contract": current_contract.model_dump(mode="json"),
            "escalation_reason": pause.reason,
            "module_for_repeat_check": module_for_repeat_check,
        }
        append_project_memory(
            event_type="round_checkpoint", actor="manager", task_id=task_id,
            module=module_for_repeat_check,
            summary=f"Escalated after {len(pause.prior_rounds)} round(s): {human_summary[:200]}",
            tags=["round_checkpoint"], detail=checkpoint_detail, verified=True,
        )
        # Phase 26B follow-up (2026-07-27): real, independently-confirmed
        # bug -- separate from, and compounding, audit Finding #2's own
        # module-identity mismatch. check_repeated_failures() has NEVER
        # been reachable in production for a second, independent reason:
        # no code path anywhere in this file ever wrote a genuine
        # `event_type="outcome"` row tagged "failed" -- a failed round
        # writes `event_type="replan_round"` (tags=["replan_round"]),
        # and a final escalation writes `event_type="round_checkpoint"`
        # (tags=["round_checkpoint"]) -- neither shape check_repeated_
        # failures()'s own query (`event_type == "outcome" and "failed"
        # in tags`) has ever matched. The success path's own outcome
        # write (below, Phase 6) is the ONLY "outcome" row this codebase
        # ever produced, confirmed via exhaustive grep. Mirrors that same
        # write's shape for the failure case, at the one point a task
        # definitively gives up (an escalation, not merely one retried
        # round -- matching check_repeated_failures()'s own "attempt
        # number N at an issue" semantics, which is about repeated
        # SEPARATE task submissions, not a single task's own internal
        # round-retry loop).
        last_verification = pause.prior_rounds[-1].verification_result if pause.prior_rounds else None
        outcome_tags = ["failed"]
        if module_for_repeat_check:
            outcome_tags.append(module_for_repeat_check)
        append_project_memory(
            event_type="outcome", actor=current_contract.specialist_type.value, task_id=task_id,
            module=module_for_repeat_check,
            summary=last_verification.notes if last_verification else pause.message,
            tags=outcome_tags,
            detail={
                "passed": False,
                "root_cause": last_verification.root_cause if last_verification else None,
                "capability_class": current_contract.capability_class.value,
                "tier": tier_value,
                "rounds_taken": len(pause.prior_rounds),
                "escalation_reason": pause.reason,
            },
            verified=False,
        )
        append_project_memory(
            event_type="branch_message", actor="manager", task_id=task_id, module=None,
            summary=human_summary[:300], tags=["escalation_summary"],
            detail={"role": "manager", "kind": "escalation_summary", "text": human_summary},
            verified=True,
        )
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "branch_message", "status": "passed",
            "message": human_summary,
            "branch_message": {"role": "manager", "kind": "escalation_summary", "text": human_summary},
        })

        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task", "message": pause.message, "status": "paused",
        })
        return {
            "status": "paused",
            "reason": "ask_operator",
            "message": pause.message,
            "human_summary": human_summary,
            "escalation_reason": pause.reason,
            "task_id": task_id,
            "correction_result": correction_result,
            "prior_rounds": pause.prior_rounds,
        }
    except Exception as exc:
        # Real bug found live (2026-07-14, running the Phase 20 §24.14
        # field-add batch): NOTHING below `except PauseForOperator` used to
        # exist -- any other exception (confirmed live: subprocess.
        # TimeoutExpired from spot_check.py's coverage run, itself
        # already correctly timeout-bounded at 180s, just never CAUGHT
        # by anything above it) propagated all the way out of this
        # function, through run_turn(), through the ASGI request in
        # ui/chat/server.py's post_message() -- crashing that one HTTP
        # request with a real logged traceback, but leaving this task's
        # oma:task:<id>:state at STATE_RUNNING forever: mark_task_running()
        # had already fired at the top of this function, nothing ever
        # ran to supersede it, and the module lock never got released
        # either. The task became a permanent, invisible zombie -- stuck
        # "running" in the dashboard with no further trace events, no
        # error surfaced to Operator anywhere, discoverable only by reading
        # this service's own systemd journal. This is exactly the
        # dashboard's own already-anticipated-but-never-written
        # "terminated_stuck_call" tag (manager/dashboard.py's
        # _cancelled_row_for_task() has checked for it since Phase 17;
        # nothing before this fix ever produced one) -- writing it here
        # closes that real gap generally, not just for this one failure
        # mode, since ANY future uncaught exception anywhere in a round
        # now lands here instead of orphaning the task silently.
        release_module_lock(module_lock_name, task_id)
        clear_task_state(task_id)
        clear_cloud_spend(task_id)
        logger.exception("Task %s crashed with an uncaught exception mid-round", task_id)
        append_project_memory(
            event_type="decision", actor="manager", task_id=task_id, module=module_for_repeat_check,
            summary=f"Stopped -- was stuck on a hung call: {exc!r}",
            tags=["cancelled_by_operator", "terminated_stuck_call"],
            detail={"round_number": round_number, "exception": repr(exc)},
            verified=True,
        )
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": f"Stopped -- was stuck on a hung call ({exc!r}).", "status": "failed",
        })
        return {
            "status": "failed",
            "reason": "uncaught_exception",
            "message": f"Task crashed mid-round: {exc!r}",
            "task_id": task_id,
            "correction_result": correction_result,
        }

    # --- Phase 6: write the outcome to memory, compose the reply ---
    # Only ever reached with verification.passed=True -- a failure that
    # exhausts its rounds/wall-clock cap escalates via PauseForOperator
    # above instead of falling through to here.
    outcome_tags = ["succeeded"]
    if module_for_repeat_check:
        outcome_tags.append(module_for_repeat_check)

    # Phase 32 implementation (2026-08-11): the two new detect-only checks
    # (live-server registry-staleness, goal-named UI action presence) plus
    # scope-certification bake-in tracking -- run only now, after every
    # existing check has already passed, since these check a DIFFERENT
    # dimension of correctness than anything above. Never affects
    # verification.passed; only ever adds human-facing warnings.
    phase32_warnings = run_phase32_post_completion_checks(
        current_contract.goal, build_output.detail, module_for_repeat_check, task_id,
    )
    if phase32_warnings:
        publish_trace_event(task_id, {
            "level": "manager", "actor": "manager", "phase": "task",
            "message": "\n".join(phase32_warnings), "status": "passed",
        })

    append_project_memory(
        event_type="outcome",
        actor=current_contract.specialist_type.value,
        task_id=task_id,
        module=module_for_repeat_check,
        summary=verification.notes,
        tags=outcome_tags,
        detail={
            "passed": verification.passed,
            "root_cause": verification.root_cause,
            "capability_class": current_contract.capability_class.value,
            "tier": tier_value,
            "rounds_taken": round_number,
            # Phase 30, P1d follow-up (2026-07-30): real, confirmed gap --
            # _pop_llm_call_stats() was only ever called on the round-
            # revision path above (building a ReplanRound), which is
            # never reached for a task that succeeds on its first round.
            # Confirmed live: a fresh, ordinary round-1 success wrote
            # zero llm_call_count/llm_duration_sec anywhere. Same call,
            # same pop-once-per-round contract as the revision path.
            **_pop_llm_call_stats(client, task_id),
        },
        verified=verification.passed,
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "task",
        "message": verification.notes, "status": "passed",
    })

    # Phase 17 (§21.5.6): a real, human-facing completion digest --
    # closes the gap where a passing task told Operator nothing beyond a
    # bare status flip. No compression here -- there's no continuation
    # in flight, nothing to summarize; this reads the full, real,
    # uncompressed round history every time.
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "branch_message",
        "status": "running", "message": "Writing up what actually happened…",
    })
    success_digest = await summarize_success_for_operator(
        contract.goal, prior_rounds, verification.notes, client, manager_model,
        task_id=task_id, pre_compressed_summary=manager_history_summary,
    )
    append_project_memory(
        event_type="branch_message", actor="manager", task_id=task_id, module=None,
        summary=success_digest["headline"][:300], tags=["success_digest"],
        detail={"role": "manager", "kind": "success_digest", **success_digest},
        verified=True,
    )
    publish_trace_event(task_id, {
        "level": "manager", "actor": "manager", "phase": "branch_message", "status": "passed",
        "message": success_digest["headline"],
        "branch_message": {"role": "manager", "kind": "success_digest", **success_digest},
    })

    return {
        "status": "completed",
        "task_id": task_id,
        "passed": verification.passed,
        "tier": tier_value,
        "capability_class": current_contract.capability_class.value,
        "build_output": build_output,
        "verification": verification,
        "correction_result": correction_result,
        "learning_routing": None,  # per-round learning is already routed inline above
        "rounds_taken": round_number,
        "memory_block_used": memory_block,
        "constitution_loaded_chars": len(constitution_text),
        "sensitive_paths_rule_count": len(sensitive_paths_raw),
        "success_digest": success_digest,
    }


async def resume_after_sign_off(
    task_id: str, approved: bool, client: ModelGatewayClient, classifier_model: str,
    manager_model: str | None = None,
) -> dict:
    """The other half of the tier-3/4 gate: called from a LATER, separate
    request (a real chat UI's own approve/reject action) once Operator has
    actually decided. Never re-classifies or re-builds the contract --
    uses the exact one that was proposed and shown to Operator, per the
    plan/act pattern's own point (approve what was actually shown, not
    a freshly re-derived guess).
    """
    payload = get_pending_contract(task_id)
    if payload is None:
        return {
            "status": "error",
            "task_id": task_id,
            "message": (
                f"No pending sign-off found for task {task_id} -- it may have already been "
                f"resolved, or its 24h window expired."
            ),
        }

    contract = TaskContract.model_validate(payload["contract"])
    module_lock_name = payload["module_lock_name"]
    correction_result = payload["correction_result"]
    module_for_repeat_check = payload.get("module_for_repeat_check")
    # Phase 22 (2026-07-23): this same back half is now also reused by
    # the diagnose-then-confirm pause (manager/loop.py's pause_if
    # gate) -- payload["diagnosis"] is only ever set on that path (the
    # plain tier-3/4 sign-off never stores one), so it's a reliable
    # signal for which flow this resume is actually completing,
    # without needing a second, near-duplicate function.
    is_diagnosis_flow = payload.get("diagnosis") is not None
    clear_pending_contract(task_id)

    if not approved:
        if is_diagnosis_flow:
            summary = f"Operator rejected a proposed plan before it touched Odoo: {contract.goal!r}"
            tags = ["rejected", "diagnosis_confirmation"]
        else:
            summary = f"Operator rejected a tier-{contract.tier.value} task before it touched Odoo: {contract.goal!r}"
            tags = ["rejected", "sign_off"]
        append_project_memory(
            event_type="decision",
            actor="operator",
            task_id=task_id,
            module=None,
            summary=summary,
            tags=tags,
            detail={"tier": contract.tier.value, "capability_class": contract.capability_class.value},
            verified=True,
        )
        return {
            "status": "rejected",
            "task_id": task_id,
            "message": "Understood -- this task is cancelled, nothing was ever delegated to a specialist.",
            "correction_result": correction_result,
        }

    constitution_text = load_manager_constitution()
    sensitive_paths_raw = load_sensitive_paths()
    memory_rows = read_project_memory(tags=None, limit=50)
    selector = ContextSelector()
    assembled = selector.select(candidate_rows=memory_rows, history=[], memory_query_ran=True)
    formatter = ContextFormatter()
    memory_block = formatter.format_system_block(assembled)

    return await _execute_contract(
        contract, module_lock_name, client, classifier_model, correction_result,
        module_for_repeat_check, memory_block, constitution_text, sensitive_paths_raw,
        manager_model=manager_model,
    )


## P12 Tier A item 20 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
# A Finding 21): real, confirmed gap -- no aggregate round/wall-clock ceiling exists across a
# decomposed task's sub-contracts, AND (independently re-confirmed in the critique) the same
# gap extends to this separate `run_plan()` multi-item executor -- each item only ever has its
# OWN per-item `planning_round_budget`/`round_wall_clock_cap_seconds`, with nothing capping the
# PLAN as a whole. A plan with many items could in principle consume item_count * 5 rounds and
# item_count * per-item wall-clock cap with no overall ceiling at all. Generous defaults
# (roughly 5x a single item's own typical per-item budget) -- this is meant to catch a
# genuinely runaway plan, never to interrupt a normal multi-item plan working as intended.
_DEFAULT_PLAN_ROUND_BUDGET = 25
_DEFAULT_PLAN_WALL_CLOCK_CAP_SECONDS = 13500


async def run_plan(
    operator_request: str,
    item_contracts: list[TaskContract],
    client: ModelGatewayClient,
    classifier_model: str,
    manager_model: str | None = None,
    plan_round_budget: int = _DEFAULT_PLAN_ROUND_BUDGET,
    plan_wall_clock_cap_seconds: float = _DEFAULT_PLAN_WALL_CLOCK_CAP_SECONDS,
) -> dict:
    """Phase 15 (§19.7): the outer plan entry point, wired into the
    loop for a request that genuinely decomposes into more than one
    dependent TaskContract (task 2/3's shape: a base model before the
    scheduling/product-selection logic that depends on it existing).
    A single-item request skips this entirely and goes through
    run_turn() exactly as it always has -- plan_id/plan_item_id/
    blocked_by all default to nothing for a standalone contract.

    Honest, flagged limitation, same shape as Phase 6's own: deciding
    WHETHER a request decomposes into multiple items is still real
    scope-extraction, which doesn't exist yet -- item_contracts is
    caller-supplied here (a test, the CLI, a future UI action), exactly
    like anticipated_scope elsewhere in this file, never derived from
    free text automatically.
    """
    plan_id = create_task_plan(operator_request, item_contracts)
    item_results: dict[str, dict] = {}
    plan_start_time = time.monotonic()
    total_rounds_taken = 0

    while True:
        next_item = next_runnable_item(plan_id)
        if next_item is None:
            break

        # P12 Tier A item 20: the aggregate ceiling check, ahead of every other per-item gate
        # below -- a plan that has already burned its own aggregate budget across EARLIER items
        # must not even start the next one.
        plan_elapsed_seconds = time.monotonic() - plan_start_time
        if total_rounds_taken >= plan_round_budget or plan_elapsed_seconds >= plan_wall_clock_cap_seconds:
            reason = "plan_round_budget" if total_rounds_taken >= plan_round_budget else "plan_wall_clock"
            mark_plan_item_status(
                plan_id, next_item.plan_item_id, "blocked",
                detail={"pause_reason": "plan_budget_exhausted", "plan_ceiling_reason": reason},
            )
            pause_result = {
                "status": "paused",
                "reason": "plan_budget_exhausted",
                "message": (
                    f"This plan has already used {total_rounds_taken} round(s) across "
                    f"{len(item_results)} completed item(s), {plan_elapsed_seconds:.0f}s elapsed -- "
                    f"the plan's own aggregate budget ({plan_round_budget} rounds / "
                    f"{plan_wall_clock_cap_seconds:.0f}s) is exhausted, so the next item "
                    f"({next_item.plan_item_id!r}) was never even started. This is a whole-plan "
                    f"ceiling, distinct from any single item's own per-item budget."
                ),
                "task_id": str(next_item.task_id),
                "correction_result": {"status": "not_a_correction"},
            }
            return {
                "status": "paused",
                "plan_id": plan_id,
                "item_results": item_results,
                "paused_on": next_item.plan_item_id,
                "pause_result": pause_result,
            }

        # A real, structural fix (found during the real Phase 15
        # verification pass, not a synthetic test): a blocked_by-dependent
        # item must extend its blocking item's REAL module -- via a
        # genuine, separate Odoo module with _inherit, the way Odoo is
        # actually meant to be extended -- never by regenerating another
        # item's own module from scratch. The blocking item's real,
        # recorded module_name (persisted on its own 'passed' row, the
        # exact same place verify_module: already reads a build's own
        # module_name from) is injected here as a depends_on_module:
        # marker, using the same "<marker>:<value> in contract.inputs"
        # convention diff_module:/verify_module: already established --
        # not a new pattern.
        if next_item.blocked_by:
            items = list_plan_items(plan_id)
            module_names_by_item = {
                row["plan_item_id"]: (row.get("detail") or {}).get("module_name") for row in items
            }
            depends_markers = [
                f"depends_on_module:{module_names_by_item[dep]}"
                for dep in next_item.blocked_by
                if module_names_by_item.get(dep)
            ]
            if depends_markers:
                next_item = next_item.model_copy(
                    update={"inputs": [*next_item.inputs, *depends_markers]}
                )

        mark_plan_item_status(plan_id, next_item.plan_item_id, "in_progress")
        module_lock_name = f"plan:{plan_id}:item:{next_item.plan_item_id}"

        # Real, confirmed bug found live (2026-07-23, Phase 22 plan):
        # run_turn()'s own tier-3/4 sign-off gate (line ~371 above) only
        # ever runs in run_turn() itself, BEFORE _execute_contract() is
        # called -- a gated task never reaches _execute_contract() on
        # its first pass at all. run_plan()'s own loop calls
        # _execute_contract() directly for every item, with NO
        # equivalent check anywhere in this function -- meaning a
        # tier-3/4 plan item (touching financial fields, schema,
        # security/permissions) bypassed Operator's required sign-off
        # entirely when submitted via the outer-plan executor instead
        # of a single-item run_turn() request. Fixed by duplicating the
        # exact same guard here rather than adding a check inside
        # _execute_contract() itself -- that shared function is also
        # the back half resume_after_sign_off() calls for an ALREADY-
        # approved tier-3/4 task, so a check inside it would need to
        # somehow distinguish "never gated" from "already approved,"
        # which the guard here avoids needing to do at all.
        item_tier_value = next_item.tier.value
        if item_tier_value >= _SIGN_OFF_TIER_THRESHOLD:
            item_task_id = str(next_item.task_id)
            set_task_state(item_task_id, PAUSE_SIGN_OFF_REQUIRED)
            store_pending_contract(
                item_task_id, next_item, module_lock_name,
                correction_result={"status": "not_a_correction"},
                module_for_repeat_check=None,
            )
            publish_trace_event(item_task_id, {
                "level": "manager", "actor": "manager", "phase": "delegate",
                "message": f"Tier {item_tier_value} -- waiting on your sign-off before delegating.",
                "status": "paused",
            })
            pause_result = {
                "status": "paused",
                "reason": "sign_off_required",
                "message": (
                    f"This plan item is tier {item_tier_value} and needs your sign-off before I "
                    f"proceed -- nothing has touched Odoo yet. Goal: {next_item.goal!r}. "
                    f"Reply with a sign-off decision for task {item_task_id} to continue."
                ),
                "task_id": item_task_id,
                "contract": next_item,
                "correction_result": {"status": "not_a_correction"},
            }
            mark_plan_item_status(
                plan_id, next_item.plan_item_id, "blocked",
                detail={"pause_reason": "sign_off_required"},
            )
            return {
                "status": "paused",
                "plan_id": plan_id,
                "item_results": item_results,
                "paused_on": next_item.plan_item_id,
                "pause_result": pause_result,
            }

        result = await _execute_contract(
            next_item, module_lock_name, client, classifier_model,
            correction_result={"status": "not_a_correction"},
            module_for_repeat_check=None,
            memory_block="",
            constitution_text=load_manager_constitution(),
            sensitive_paths_raw=load_sensitive_paths(),
            manager_model=manager_model,
        )
        item_results[next_item.plan_item_id] = result
        # P12 Tier A item 20: accumulated toward the plan-level aggregate ceiling checked above,
        # regardless of this item's own outcome -- rounds spent on a failed/paused item still
        # count against the plan's own real total.
        total_rounds_taken += result.get("rounds_taken", 0)

        if result["status"] == "completed" and result["passed"]:
            mark_plan_item_status(
                plan_id, next_item.plan_item_id, "passed",
                detail={"module_name": result["build_output"].detail.get("module_name")},
            )
        elif result["status"] == "paused":
            mark_plan_item_status(
                plan_id, next_item.plan_item_id, "blocked",
                detail={"pause_reason": result.get("reason")},
            )
            return {
                "status": "paused",
                "plan_id": plan_id,
                "item_results": item_results,
                "paused_on": next_item.plan_item_id,
                "pause_result": result,
            }
        else:
            mark_plan_item_status(plan_id, next_item.plan_item_id, "failed")

    # P7b item 3 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
    # §17): the cross-module analog of the single-module whole-assembly install check -- verifies
    # every module this plan's own passed items built actually installs correctly TOGETHER, not
    # just each in isolation. Deliberately NOT wired as a blocking gate on the plan's own
    # "completed"/item "passed" statuses -- each item genuinely did pass its own real, individual
    # verification already; surfaced as an additional, honest signal on the return value instead,
    # matching this project's own flag-only discipline for a finding that doesn't cleanly map onto
    # an existing pass/fail boundary. Uses OMA_ODOO_DB_DUPLICATE_FOR_BUILD -- the SAME real,
    # already-established env var this exact file's own `_step_existing_module_target()` (above)
    # already uses for build-related target-db resolution at this identical orchestration layer,
    # not a guessed value -- resolves the earlier deferral's own "no settled db-target convention"
    # blocker by direct reference to that real precedent. Gracefully skipped (never raises, never
    # blocks the plan's own return) when that env var isn't configured, or fewer than 2 real
    # module names are available -- matching every other "can't determine this" early-return
    # already in this file.
    combined_install_check = None
    passed_module_names = sorted({
        (row.get("detail") or {}).get("module_name")
        for row in list_plan_items(plan_id)
        if row.get("status") == "passed" and (row.get("detail") or {}).get("module_name")
    })
    target_db = os.environ.get("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", "")
    if target_db and len(passed_module_names) >= 2:
        from tools_odoo.module_dev.toolchain import verify_combined_install

        install_result = await asyncio.to_thread(verify_combined_install, passed_module_names, target_db)
        combined_install_check = {
            "module_names": passed_module_names,
            "success": install_result.success,
            "message": install_result.message,
        }

    return {
        "status": "completed", "plan_id": plan_id, "item_results": item_results,
        "combined_install_check": combined_install_check,
    }
