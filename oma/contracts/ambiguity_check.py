"""P12 Tier B/C item 26 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
§4, source C's §3 Role 1 proposal, independently corroborated in the synthesis's §3.3):
a real, structured "Ambiguity Report" pre-build check for genuinely unresolvable spec
ambiguity.

Real, confirmed gap this closes: OMA's closest existing equivalent is
`contracts/goal_facts.py`'s field-extraction step and `manager/classify.py`'s capability
classification -- neither produces a structured "here is what's genuinely ambiguous, here's
what I need a human to decide before any code gets written" artifact.
`should_escalate_to_operator()` (`manager/replanning.py`) does escalate to a human, but only
REACTIVELY, after real rounds have already been spent discovering the ambiguity the expensive
way. Governance gates (`manager/governance.py`) halt on policy violations, not spec ambiguity.

Lives in `contracts/`, matching `contracts/goal_facts.py`'s own placement rationale exactly:
import-linter forbids `manager` importing anything under `specialists`, but both already import
freely from `contracts/`.

Deliberately conservative in scope, matching this priority's own "moderate priority, lower
urgency than the scope-drift/single-vote-veto gaps" framing (§3.3's own assessment: OMA's
existing REACTIVE escalation path does eventually catch genuinely unresolvable ambiguity, just
expensively -- this is a real improvement, not a fix for an actively-wrong silent outcome):
`has_blocking_ambiguity` is deliberately a high bar (a genuine "cannot proceed at all without a
human decision" case, e.g. contradictory requirements or a materially undefined term the goal
depends on), never a general-purpose nitpick list -- a task with minor, resolvable ambiguity
(the normal case) must never be blocked by this check.
"""

from __future__ import annotations

from pydantic import BaseModel

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import call_structured


class AmbiguityReport(BaseModel):
    """`has_blocking_ambiguity`: True only for a genuine "cannot proceed at all without a human
    decision" case -- contradictory requirements, or a materially undefined term the goal's own
    core deliverable depends on. `blocking_questions`: the exact, concrete question(s) a human
    would need to answer before this task could be built at all -- empty when
    `has_blocking_ambiguity` is False. `reasoning`: a short, honest explanation either way (why
    this genuinely can't proceed, or why the goal is well-specified enough despite not being
    perfectly detailed).
    """

    has_blocking_ambiguity: bool = False
    blocking_questions: list[str] = []
    reasoning: str = ""


_AMBIGUITY_PROMPT_TEMPLATE = """\
Read this Odoo module-development task goal. Decide whether it has GENUINE, BLOCKING ambiguity \
-- something that makes it impossible to start building at all without a human decision first, \
not just a minor detail that could reasonably be resolved by inference or a sensible default.

Examples of GENUINE blocking ambiguity:
- Contradictory requirements (e.g. "this field must always be required" and "this field is \
optional" both stated about the same field).
- A core deliverable depends on a term or concept the goal never defines and that has no \
obvious, single reasonable reading (e.g. "route it to the right team" with no team defined \
anywhere, and the routing logic IS the whole point of the task).
- The goal names two mutually exclusive approaches with no way to tell which one is actually \
wanted, and the choice would produce genuinely different code.

Examples that are NOT blocking ambiguity (do not flag these):
- A field's exact technical name isn't given -- a reasonable name can be inferred.
- Minor formatting/UI details are left to reasonable convention.
- The goal is terse but the intent is clear from context.

Most real, ordinary task goals have NO blocking ambiguity. Only flag it when you are genuinely \
confident a human decision is required before ANY code could be written.

Task goal: {goal}

Respond with a structured AmbiguityReport."""


async def extract_ambiguity_report(
    goal: str, client: ModelGatewayClient, model: str, task_id: str | None = None,
) -> AmbiguityReport:
    """Never raises -- a failed/malformed extraction (gateway outage, retry-budget exhaustion)
    returns the default `AmbiguityReport()` (`has_blocking_ambiguity=False`), matching
    `extract_goal_field_facts()`'s own "guaranteed no-op when it doesn't work" discipline: this
    check must never itself become a NEW way for a real task to get silently stuck (fail open,
    same posture as `check_hard_governance_gates()` on a DB error).
    """
    prompt = _AMBIGUITY_PROMPT_TEMPLATE.format(goal=goal)
    try:
        return await call_structured(
            client=client, model=model, prompt=prompt, schema=AmbiguityReport,
            no_think=True, temperature=0.0, task_id=task_id, actor="manager",
            call_label="Extracting the ambiguity report", use_grammar=True,
        )
    except Exception:
        return AmbiguityReport()
