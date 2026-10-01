"""Phase 35 §17.2.1: the task-intake existence/dedup check -- a NEW consultation point, run once
per incoming goal, BEFORE decomposition, using the same real `get_module_grounding()`-family graph
primitives Build's own pre-write gate already uses. Never blocks by itself; see
manager/graph_governance_flags.py for the log_only/enforced staging this gate ships behind.

Implements the structural half of §17.2.1's "run both, not either/or" recommendation (structural
grounding via a real graph query). The embedding-similarity half (cosine-similarity fuzzy dedup
against existing model names) is explicitly NOT implemented here -- it needs its own embedding
model call and a real similarity index this codebase doesn't have yet, and is called out honestly
as a gap rather than faked with a placeholder. §17.2.1's decision table degrades gracefully without
it: this module can still return `exists_verbatim`, `no_signal`, `ungrounded`, and
`graph_query_failed`; it can never return `probable_duplicate_fuzzy`, since that verdict requires
the embedding half. Tracked as a real follow-up, not silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import call_structured
from pydantic import BaseModel
from tools_odoo.graph_queries import get_model_existence

# §17.2.1's own structured-extraction schema.
class IntakeCandidateEntities(BaseModel):
    candidate_models: list[str] = []
    candidate_fields: list[str] = []
    candidate_views: list[str] = []
    confidence: float = 0.0


_EXTRACTION_PROMPT = """\
Read this task goal for an Odoo development system. Extract any Odoo technical names it \
concretely mentions or clearly implies -- model technical names (dotted lowercase, e.g. \
"crm.lead", "oma.badge.scan.log"), field names (snake_case), and view xml_ids, if any. Only \
extract names actually present or unambiguously implied by the text -- never invent one. If the \
goal describes creating something brand new with a name that doesn't yet exist, still extract \
that name as a candidate_model (this check's job is to verify whether it already exists, not to \
assume it doesn't).

- candidate_models: real or plausible Odoo model technical names mentioned or implied.
- candidate_fields: field names mentioned or implied.
- candidate_views: view xml_ids mentioned or implied (rare -- usually empty).
- confidence: your confidence (0.0-1.0) that you extracted the goal's real target entities \
correctly and completely. Low confidence (<0.3) if the goal is vague, non-technical, or you \
couldn't find any clear entity names.

Task goal:
{goal}

Respond with ONLY a single JSON object matching the schema."""

# §17.2.1.1 item 2: the deterministic, non-LLM signal required before a task can route to the
# "ungrounded" fast path -- a dotted lowercase Odoo-style technical name pattern, scanned directly
# against the raw goal text, independent of whatever the LLM extraction did or didn't find.
_TECHNICAL_NAME_PATTERN = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,4}\b")


async def extract_intake_candidates(
    goal: str, client: ModelGatewayClient, model: str, task_id: str | None = None,
) -> IntakeCandidateEntities:
    """Never raises -- a failed/malformed extraction returns an empty, confidence=0.0 result,
    which downstream treats identically to "nothing extractable," per the same fail-open
    contract contracts/goal_facts.py's sibling extraction function already uses."""
    try:
        return await call_structured(
            client=client, model=model, prompt=_EXTRACTION_PROMPT.format(goal=goal),
            schema=IntakeCandidateEntities, temperature=0.0, no_think=True, task_id=task_id,
            actor="manager", call_label="intake_grounding",
        )
    except Exception:  # noqa: BLE001 -- fail-open, see module docstring
        return IntakeCandidateEntities()


def deterministic_technical_name_scan(goal: str) -> list[str]:
    """§17.2.1.1 item 2's deterministic scan -- returns every dotted lowercase technical-name-
    shaped token found in the raw goal text, regardless of what the LLM extraction found. Used
    both to corroborate/extend the LLM's own candidate_models list and as the second, required
    signal before a task can be routed to the "ungrounded" fast path."""
    return sorted(set(_TECHNICAL_NAME_PATTERN.findall(goal)))


class IntakeGroundingVerdict(str, Enum):
    EXISTS_VERBATIM = "exists_verbatim"
    PROBABLE_DUPLICATE_FUZZY = "probable_duplicate_fuzzy"  # never returned, see module docstring
    NO_SIGNAL = "no_signal"
    UNGROUNDED = "ungrounded"
    GRAPH_QUERY_FAILED = "graph_query_failed"


@dataclass(frozen=True)
class IntakeGroundingResult:
    verdict: IntakeGroundingVerdict
    matched_models: list[str]
    candidates_checked: list[str]
    extraction_confidence: float
    detail: str


async def check_intake_grounding(
    goal: str, driver, client: ModelGatewayClient, model: str, task_id: str | None = None,
) -> IntakeGroundingResult:
    """The real §17.2.1 verdict, computed for real against the live graph. Always returns a
    result (never raises) -- the caller (manager/loop.py's Step wiring) decides what to DO with
    it based on manager/graph_governance_flags.py's log_only/enforced mode for this gate; this
    function's job is only to compute the real, honest verdict.
    """
    extracted = await extract_intake_candidates(goal, client, model, task_id=task_id)
    deterministic_hits = deterministic_technical_name_scan(goal)
    all_candidates = sorted(set(extracted.candidate_models) | set(deterministic_hits))

    if not all_candidates:
        if extracted.confidence < 0.3:
            return IntakeGroundingResult(
                verdict=IntakeGroundingVerdict.UNGROUNDED,
                matched_models=[], candidates_checked=[],
                extraction_confidence=extracted.confidence,
                detail="Neither LLM extraction nor the deterministic technical-name scan found "
                       "any candidate entities -- routed to the ungrounded fast path per §17.2.1.1.",
            )
        return IntakeGroundingResult(
            verdict=IntakeGroundingVerdict.NO_SIGNAL,
            matched_models=[], candidates_checked=[],
            extraction_confidence=extracted.confidence,
            detail="No candidate model names found to check.",
        )

    matched: list[str] = []
    any_in_progress = False
    for candidate in all_candidates:
        try:
            in_progress, existence = get_model_existence(driver, candidate)
        except Exception:  # noqa: BLE001 -- a single bad query must not abort the whole check
            any_in_progress = True
            continue
        any_in_progress = any_in_progress or in_progress
        if existence is not None:
            matched.append(candidate)

    if any_in_progress and not matched:
        return IntakeGroundingResult(
            verdict=IntakeGroundingVerdict.GRAPH_QUERY_FAILED,
            matched_models=[], candidates_checked=all_candidates,
            extraction_confidence=extracted.confidence,
            detail="Graph import in progress or query failure during intake grounding -- "
                   "per §0 rule 6, this must never be treated as a confirmed non-match.",
        )

    if matched:
        return IntakeGroundingResult(
            verdict=IntakeGroundingVerdict.EXISTS_VERBATIM,
            matched_models=matched, candidates_checked=all_candidates,
            extraction_confidence=extracted.confidence,
            detail=f"The following candidate model(s) already exist in the real graph: {matched}.",
        )

    return IntakeGroundingResult(
        verdict=IntakeGroundingVerdict.NO_SIGNAL,
        matched_models=[], candidates_checked=all_candidates,
        extraction_confidence=extracted.confidence,
        detail="Candidate model name(s) checked, none matched an existing real model.",
    )
