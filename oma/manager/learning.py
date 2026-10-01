"""The error-learning mechanism (§2.7), built for real -- the piece that
determines whether this system actually gets better over time or just
accumulates a thorough log of the same mistakes recurring.

Two concrete pieces, per the build plan's Phase 6 step 4:
  1. Root-cause classification on every failed VerificationResult:
     one_off / skill_gap / pattern_worth_a_rule / unclear.
     - skill_gap routes to a skill-revision note for Code-Review.
     - pattern_worth_a_rule routes into the EXACT SAME shadow/
       confirm-first pipeline as a Operator-originated correction
       (manager.correction's propose-a-rule shape) -- a self-detected
       pattern is no more automatically trustworthy than a misheard
       correction, and deserves the same human check.
     - one_off / unclear: the already-written outcome row stands as-is,
       nothing further.
  2. The repeated-failure check: extends the pre-action gate so that
     2+ prior failed outcome rows on what looks like the same
     underlying issue trips a pause, surfaced to Operator in plain
     language, rather than a silent third attempt.

Phase 29D (2026-07-29): before routing a `pattern_worth_a_rule` failure
into a new `proposed_rule` row, check whether this exact claim-shape was
ALREADY resolved by a real validator during a prior backlog triage pass
(scripts/rule_backlog_triage.py) -- i.e. whether this is the pipeline
about to re-report something it already caught and fixed once. Real,
confirmed motivation: tonight's exhaustive triage found 512+ historical
proposed-rule rows that were exact-shape duplicates of patterns already
covered by existing code, discovered only by manual after-the-fact
review. Without this check, the backlog silently re-accumulates the
same already-covered patterns forever, one round at a time, which is
the exact failure mode root_cause.md itself described ("catches its own
mistakes every time, but never remembers having caught them"). This
check closes that loop going forward without requiring bucket 2's
retrieval/vector infrastructure (explicitly out of scope) -- it's a
same-normalization-signature match against already-`superseded` rows
this codebase already writes.
"""

from __future__ import annotations

import json
import re

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import generate_checked
from manager.memory import read_project_memory
from manager.tools import append_project_memory

ONE_OFF = "one_off"
SKILL_GAP = "skill_gap"
PATTERN_WORTH_A_RULE = "pattern_worth_a_rule"
UNCLEAR = "unclear"
# P12 Tier A item 18 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4,
# A Finding 17): real, confirmed gap -- the original four-way taxonomy has no category for "the
# orchestration/pipeline itself cannot succeed here" (e.g. Bug #2's data_change stub -- no LLM
# call involved at all, no skill/prompt revision could ever fix it, yet the classifier was
# forced to pick one of one_off/skill_gap/pattern_worth_a_rule/unclear, and whichever it picked
# actively misdirected remediation toward revising a skill or proposing a policy rule, when the
# real fix is an engineering change to the Manager's/a specialist's own wiring). P12 item 1's
# capability-readiness gate already intercepts that ONE specific instance before any round
# starts, but the taxonomy gap itself is general -- any other structural dead-path bug would
# hit the exact same misclassification.
PIPELINE_DEFECT = "pipeline_defect"
VALID_ROOT_CAUSES = {ONE_OFF, SKILL_GAP, PATTERN_WORTH_A_RULE, PIPELINE_DEFECT, UNCLEAR}

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_CLASSIFY_PROMPT_TEMPLATE = """\
A task failed verification. Decide which of these five categories the \
failure's root cause belongs to:

- "one_off": a specific bug, correctly caught, nothing generalizable \
about it. Most failures belong here.
- "skill_gap": the specialist followed its skill's instructions \
correctly, but the skill itself was wrong or incomplete.
- "pattern_worth_a_rule": a recurring pattern that should become a \
durable, mechanically-checked constraint going forward.
- "pipeline_defect": the failure has nothing to do with the generated \
content or a skill at all -- the orchestration/pipeline itself cannot \
succeed here (e.g. a documented stub with no real handler, no LLM call \
even involved, a structurally dead route). No skill revision or policy \
rule would ever fix this -- it needs an engineering change to the \
pipeline's own wiring.
- "unclear": genuinely no obvious generalizable lesson. This is a \
legitimate answer, not a failure to classify -- do not force one of \
the other four if it doesn't really fit.
{recent_round_context}
Failure summary: {failure_summary}

Respond with ONLY a JSON object: {{"root_cause": "one_off" | "skill_gap" \
| "pattern_worth_a_rule" | "pipeline_defect" | "unclear"}}"""

_RECENT_ROUND_CONTEXT_TEMPLATE = """
This task's own recent prior rounds (check whether THIS failure matches \
one you've already seen in THIS SAME task -- if so, it is NOT one_off, \
it is pattern_worth_a_rule, even if an earlier round was mislabeled):
{round_summaries}
"""


async def classify_root_cause(
    failure_summary: str,
    client: ModelGatewayClient,
    model: str,
    max_tokens: int = 1500,
    task_id: str | None = None,
    recent_round_summaries: list[str] | None = None,
) -> str:
    """Same cheap, cached-classification discipline as the
    capability-class cascade (§2.5) -- here without a keyword fast path
    since root-cause genuinely needs the failure's specifics read, but
    with the same shared generate_checked() completeness discipline
    (IncompleteResponseError -> safe default UNCLEAR, not a crash) on
    any failure. UNCLEAR is a legitimate answer, not a failure of the
    classifier -- so this is also the correct fallback on error.

    Phase 30, P2c (§13, item 5): real, confirmed reliability gap found
    live on the school_student task -- this call used to judge each
    round's failure text in total isolation, with no visibility into
    the SAME task's own recent history, so it labeled 28 of 29 rounds
    hitting the literal same underlying failure `one_off`, only once
    correctly `pattern_worth_a_rule`. `recent_round_summaries`, when
    given (the caller's last few round notes, oldest first), is folded
    into the prompt as real context so the model can recognize "I've
    seen this exact shape before, in this same task." Optional and
    additive -- omitting it (the caller's own choice, e.g. round 1 with
    no history yet) reproduces the exact prior prompt shape unchanged.
    """
    recent_round_context = ""
    if recent_round_summaries:
        recent_round_context = _RECENT_ROUND_CONTEXT_TEMPLATE.format(
            round_summaries="\n".join(f"- {s}" for s in recent_round_summaries)
        )
    prompt = _CLASSIFY_PROMPT_TEMPLATE.format(
        failure_summary=failure_summary, recent_round_context=recent_round_context,
    )
    try:
        cleaned = await generate_checked(
            client, model, [{"role": "user", "content": prompt}], max_tokens=max_tokens,
            task_id=task_id, actor="manager", call_label="Classifying failure root cause",
        )
        match = _JSON_RE.search(cleaned)
        if not match:
            return UNCLEAR
        parsed = json.loads(match.group(0))
        label = str(parsed.get("root_cause", "")).strip().lower()
        return label if label in VALID_ROOT_CAUSES else UNCLEAR
    except Exception:
        return UNCLEAR


def find_existing_superseded_match(failure_summary: str) -> str | None:
    """Phase 29D: real Postgres lookup, not a heuristic guess. Reuses
    the EXACT SAME normalize_summary() signature the manual triage tool
    already proved works for clustering (strips quoted strings,
    bracketed lists, UUIDs, numbers -- collapsing different literal
    values of the same underlying claim to the same signature). Returns
    the validator name this pattern was already superseded by, or None
    if genuinely no match (the normal, common case).

    Deliberately conservative: this compares the FULL normalized
    signature (no truncation to the first N chars the way the triage
    tool's own display clustering does), so a partial/coincidental
    overlap between two otherwise-different claims cannot produce a
    false match here -- a missed match just means a new proposed_rule
    row gets created as before (safe, the pre-Phase-29D behavior),
    never a wrongly-suppressed genuine new pattern.
    """
    import psycopg2
    import psycopg2.extras

    from infra.settings import load_postgres_settings
    from scripts.rule_backlog_triage import normalize_summary

    target_sig = normalize_summary(failure_summary)
    if not target_sig:
        return None

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT summary, detail->>'superseded_by' AS superseded_by FROM agent_memory_events "
            "WHERE event_type = 'rule' AND detail->>'status' = 'superseded'"
        )
        rows = cur.fetchall()
    except Exception:
        # Same conservative posture as classify_root_cause()'s own
        # try/except: a genuine DB hiccup here must never block or
        # crash the real failure-handling path it's a side-check for --
        # falling through to "no match" just means the normal,
        # pre-Phase-29D behavior (create the proposed_rule row).
        return None
    finally:
        conn.close()

    for row in rows:
        if normalize_summary(row["summary"] or "") == target_sig:
            return row["superseded_by"]
    return None


async def handle_failed_verification(
    task_id: str,
    module: str | None,
    failure_summary: str,
    root_cause: str,
    actor: str = "manager",
) -> dict:
    """Routes a classified failure to its concrete consequence. Called
    AFTER classify_root_cause() has already produced `root_cause` (kept
    as a separate step so the caller controls when/whether to persist
    the VerificationResult's own root_cause field vs. this routing
    action -- they're related but not the same write).
    """
    if root_cause == SKILL_GAP:
        row_id = append_project_memory(
            event_type="note",
            actor=actor,
            task_id=task_id,
            module=module,
            summary=f"Skill-revision needed: {failure_summary[:300]}",
            tags=["skill_gap", "needs_code_review"],
            detail={"root_cause": SKILL_GAP, "failure_summary": failure_summary},
            verified=False,
        )
        return {"routed_to": "skill_revision", "row_id": row_id}

    if root_cause == PIPELINE_DEFECT:
        # Distinct from SKILL_GAP's "skill-revision needed" queue and PATTERN_WORTH_A_RULE's
        # rule-proposal backlog, per this item's own explicit fix shape -- an engineering-facing
        # note, since neither a skill revision nor a policy rule could ever address a structural
        # dead-path bug in the pipeline's own wiring.
        row_id = append_project_memory(
            event_type="note",
            actor=actor,
            task_id=task_id,
            module=module,
            summary=f"Pipeline defect, needs an engineering fix (not a skill/rule change): {failure_summary[:300]}",
            tags=["pipeline_defect", "needs_engineering_fix"],
            detail={"root_cause": PIPELINE_DEFECT, "failure_summary": failure_summary},
            verified=False,
        )
        return {"routed_to": "engineering_fix_needed", "row_id": row_id}

    if root_cause == PATTERN_WORTH_A_RULE:
        # Phase 29D: check first whether this exact claim-shape was
        # already resolved by a real validator during a prior triage
        # pass -- if so, log that fact (for the audit trail) instead of
        # re-adding a duplicate row to the confirmation backlog.
        existing_match = find_existing_superseded_match(failure_summary)
        if existing_match:
            row_id = append_project_memory(
                event_type="note",
                actor=actor,
                task_id=task_id,
                module=module,
                summary=f"Already covered by {existing_match}: {failure_summary[:300]}",
                tags=["proposed_rule_already_covered", "self_detected"],
                detail={
                    "root_cause": PATTERN_WORTH_A_RULE,
                    "failure_summary": failure_summary,
                    "already_covered_by": existing_match,
                },
                verified=True,
            )
            return {"routed_to": "already_covered_by_existing_validator", "row_id": row_id, "validator": existing_match}

        # The EXACT SAME shadow/confirm-first shape as a Operator-originated
        # correction (manager.correction.handle_correction_detection),
        # just with a different origin tag so the audit trail can tell
        # "Operator told us this" apart from "we noticed this ourselves."
        row_id = append_project_memory(
            event_type="rule",
            actor=actor,
            task_id=task_id,
            module=module,
            summary=f"[self-detected, pending confirmation] {failure_summary[:300]}",
            tags=["proposed_rule", "self_detected"],
            detail={
                "status": "proposed",
                "directive": failure_summary,
                "applicability_condition": module or "unspecified",
                "origin": "self_detected_pattern",
            },
            verified=False,
        )
        return {"routed_to": "proposed_rule_pending_confirmation", "row_id": row_id}

    # one_off / unclear: the already-written outcome row stands as-is.
    return {"routed_to": "none", "root_cause": root_cause}


def check_repeated_failures(module: str, min_prior_failures: int = 2) -> dict:
    """The other half of "not repeating the same mistake": before
    delegating, check whether 2+ prior failed outcome rows already
    exist for this module. A cheap count against the memory log, not a
    new mechanism -- but a real, distinct pause condition, not something
    to silently hand down a third attempt against.
    """
    rows = read_project_memory(tags=None, limit=200)
    prior_failures = [
        r for r in rows
        if r.event_type == "outcome" and r.module == module and "failed" in (r.tags or [])
    ]
    if len(prior_failures) >= min_prior_failures:
        return {
            "should_pause": True,
            "message": (
                f"This looks like attempt number {len(prior_failures) + 1} at an issue "
                f"in {module}; the previous {len(prior_failures)} failed for related "
                f"reasons -- keep trying the same approach, or should we look at this "
                f"differently?"
            ),
            "prior_failure_count": len(prior_failures),
        }
    return {"should_pause": False, "prior_failure_count": len(prior_failures)}


def check_repeated_failures_across_modules(root_cause_signature: str, min_prior_failures: int = 2) -> dict:
    """Phase 15 (§19.4): the real, specific gap Phase 13 found, flagged
    honestly rather than glossed over -- check_repeated_failures() above
    is scoped per-module-name, so a systemic specialist skill gap
    recurring identically across DIFFERENT modules (Phase 13's real
    ir.model.access.csv bug: oma.service, then oma.svc.record/oma.svc.group)
    would never be flagged as "the same issue repeating" by that function
    alone. This is the companion check: matches on root_cause_signature
    (an explicit description of the specific failure pattern -- e.g.
    "skill_gap: ir.model.access.csv references the wrong model" --
    supplied by the caller, since a bare root_cause label like
    'skill_gap' alone is far too coarse to mean "the same issue") found
    as a substring in prior FAILED outcome rows' own summaries,
    regardless of which module each one concerns. Both signals matter --
    this is a companion to check_repeated_failures(), never a
    replacement for it.
    """
    rows = read_project_memory(tags=None, limit=200)
    prior_failures = [
        r for r in rows
        if r.event_type == "outcome" and "failed" in (r.tags or [])
        and root_cause_signature.lower() in (r.summary or "").lower()
    ]
    if len(prior_failures) >= min_prior_failures:
        modules = sorted({r.module for r in prior_failures if r.module})
        return {
            "should_pause": True,
            "message": (
                f"This same underlying issue ({root_cause_signature!r}) has now failed "
                f"{len(prior_failures)} times across {len(modules)} different module(s) "
                f"({', '.join(modules) if modules else 'unspecified'}) -- this looks like a "
                f"systemic gap, not a one-off, even though no single module repeated on its own."
            ),
            "prior_failure_count": len(prior_failures),
            "modules_affected": modules,
        }
    return {"should_pause": False, "prior_failure_count": len(prior_failures)}


def median_llm_calls_per_round(capability_class: str, min_samples: int = 5) -> float | None:
    """Phase 30, P1d (Phase L, §15): real per-capability_class historical
    median of llm_call_count across real, already-logged replan_round
    events -- explicitly scoped to VISIBILITY (see this phase's own "does
    not propose a cost ceiling" note), never used to gate or block.
    Returns None (never a fabricated number) when fewer than
    `min_samples` real rounds with a populated llm_call_count exist for
    this capability_class -- expected and normal immediately after this
    field starts being written, since no historical round before it
    shipped has real data to compute a median from.
    """
    import statistics

    import psycopg2
    import psycopg2.extras

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT (detail->>'llm_call_count')::int AS llm_call_count "
            "FROM agent_memory_events "
            "WHERE event_type='replan_round' "
            "AND detail->'new_contract'->>'capability_class' = %s "
            "AND detail->>'llm_call_count' IS NOT NULL",
            (capability_class,),
        )
        counts = [r["llm_call_count"] for r in cur.fetchall() if r["llm_call_count"] is not None]
    finally:
        conn.close()
    if len(counts) < min_samples:
        return None
    return statistics.median(counts)


def flag_llm_call_count_outlier(
    llm_call_count: int, capability_class: str, multiplier: float = 3.0,
) -> dict | None:
    """Phase 30, P1d: returns a real, informational flag dict when
    `llm_call_count` crosses roughly `multiplier`x the real historical
    median for this capability_class -- None (nothing to flag) whenever
    there's no real median to compare against yet, or the round is
    within normal range. Callers must only ever LOG/SURFACE this, never
    gate or pause a round on it -- this function has no side effects and
    makes no such decision itself.
    """
    median = median_llm_calls_per_round(capability_class)
    if median is None or median <= 0:
        return None
    threshold = median * multiplier
    if llm_call_count < threshold:
        return None
    return {
        "llm_call_count": llm_call_count,
        "historical_median": median,
        "threshold": threshold,
        "capability_class": capability_class,
        "message": (
            f"This round made {llm_call_count} LLM calls -- roughly "
            f"{llm_call_count / median:.1f}x the real historical median ({median:.1f}) for "
            f"{capability_class!r} tasks. Informational only, not a pause or gate."
        ),
    }
