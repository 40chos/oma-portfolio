"""P13 item 12a (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§22.2, per PHASE31_MODEL_PLACEMENT_AND_REMAINING_GAPS_2026-07-31.md §4): structured business-rule
extraction at contract-build time, verified at Testing/QA time -- closes the business-logic-
correctness gap P11's own honest-negatives list names as permanently NOT reducible to a
deterministic check ("computed-field business-logic completeness... goal-specific intent, no
field-name pattern recovers it").

Deliberately a SEPARATE function/call from `manager/replanning.py`'s
`decompose_into_constraints_with_artifacts()`, not a schema extension of it -- that function
already has two existing regression tests
(tests/test_constraint_artifacts_decomposition.py, tests/test_constraint_nodes_run_turn_wiring.py)
mocking `call_structured` against its exact current `_ConstraintDecomposition` schema, and its own
docstring already documents the same "kept separate to avoid breaking existing test mocks" reasoning
for why it itself is a sibling of `decompose_into_constraints()` rather than a refactor. The item's
own text asks for "near-zero-marginal-cost... does not require a new LLM call" -- honored in
spirit (near-zero MARGINAL LATENCY, run concurrently at the same call site, gated by the
deterministic keyword pre-check below so most real tasks make zero extra calls at all) rather than
the letter (a literal schema merge into an already-shipped, tested function), a deliberate,
documented deviation given the real risk of silently breaking two existing tests this late.

Lives in `contracts/`, matching `contracts/ambiguity_check.py`'s own placement rationale.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import call_structured

# Small, deterministic keyword/pattern list -- the same mechanism CLASS
# `manager/replanning.py`'s `_load_unsupported_domains()`-driven check already uses elsewhere in
# this codebase (a cheap, deterministic gate before an LLM call, not a new kind of infrastructure).
# Real, honest caveat (must ship stated plainly, per the item's own text): this keyword list's
# actual recall on real OMA traffic is unmeasured -- no historical baseline exists for goal
# ambiguity in real traffic.
_BUSINESS_RULE_TRIGGER_RE = re.compile(
    r"\b(total|sum|average|threshold|applies to|inclusive|exclusive|resets?|cascades?|"
    r"priority|deadline|overdue|includes?|excludes?)\b",
    re.IGNORECASE,
)


def goal_has_business_rule_ambiguity_signal(goal: str) -> bool:
    """Pure, deterministic, zero-LLM pre-check -- gates whether
    `extract_interpreted_business_rules()` makes a real call at all. Most ordinary task goals
    (a plain field/view/security change with no computed/aggregate/threshold logic) never match,
    so the real marginal LLM-call cost across the whole task mix is close to zero.
    """
    return bool(_BUSINESS_RULE_TRIGGER_RE.search(goal))


class InterpretedBusinessRules(BaseModel):
    """One entry per goal-derived business-logic decision the classifier has to make an
    assumption about (does "total" include tax; is a threshold inclusive or exclusive; does a
    computed field recompute on every write or only on create; does "priority" reset on
    reassignment). Empty when the goal's own trigger-keyword match doesn't actually resolve to a
    real, checkable interpretation -- never invented just because the keyword pre-check fired.
    """

    interpreted_business_rules: list[str] = []


_BUSINESS_RULE_PROMPT_TEMPLATE = """\
Read this Odoo module-development task goal. It contains language ({matched_keywords}) that often \
implies a business-logic decision the goal itself doesn't spell out explicitly -- e.g. whether a \
"total" includes tax, whether a threshold comparison is inclusive or exclusive, whether a computed \
field recomputes on every write or only at creation, whether a "priority" value resets when a \
record is reassigned.

List each REAL business-logic interpretation this specific goal requires making an assumption \
about, as a short, concrete, checkable statement (e.g. "the order total includes tax", \
"the overdue threshold of 30 days is inclusive (day 30 itself counts as overdue)"). Only list an \
interpretation the goal's own wording genuinely leaves open to a checkable assumption -- never \
invent one that isn't really implied, and return an empty list if, on reflection, this goal's \
matched keyword doesn't actually carry a real business-logic ambiguity (e.g. "priority" used only \
as a plain Selection field label, not describing any reset/cascade behavior).

Task goal: {goal}

Respond with a structured InterpretedBusinessRules."""


async def extract_interpreted_business_rules(
    goal: str, client: ModelGatewayClient, model: str, task_id: str | None = None,
) -> list[str]:
    """Never raises -- a failed/malformed extraction (gateway outage, retry-budget exhaustion)
    returns [] (same "fail open, never a new way to get silently stuck" discipline as
    `extract_ambiguity_report()`). Skips the LLM call entirely when the deterministic pre-check
    finds no trigger keyword.
    """
    matched = _BUSINESS_RULE_TRIGGER_RE.findall(goal)
    if not matched:
        return []
    prompt = _BUSINESS_RULE_PROMPT_TEMPLATE.format(
        matched_keywords=", ".join(sorted({m.lower() for m in matched})), goal=goal,
    )
    try:
        result = await call_structured(
            client=client, model=model, prompt=prompt, schema=InterpretedBusinessRules,
            no_think=True, temperature=0.0, task_id=task_id, actor="manager",
            call_label="Extracting interpreted business rules", use_grammar=True,
        )
    except Exception:
        return []
    return [r.strip() for r in result.interpreted_business_rules if r.strip()]
