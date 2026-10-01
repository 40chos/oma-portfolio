"""Phase 25B: schema-guided structured extraction of a task goal's own
stated field facts -- the shared replacement for the regex-over-prose
family (`_GOAL_FIELD_TYPE_LINE_RE`, `_GOAL_FIELD_INLINE_TYPE_RE`,
`_GOAL_FIELD_SELECTION_OPTIONS_RE`, and their Phase 25A-follow-up
siblings) that used to be independently duplicated in both
`manager/replanning.py` and `specialists/build/specialist.py`.

Lives here, in `contracts/` -- not `manager/` or `specialists/` -- on
purpose: import-linter's own contract (pyproject.toml) forbids `manager`
importing anything under `specialists` (except the specialist registry),
but both already import freely from `contracts/` and `infra/`. Putting
the ONE extraction function here, used identically by both consumers,
is what actually closes the "two skeleton builders drift apart" defect
class Phase 25A's own follow-up fix patched with duplicated regex --
this phase removes the duplication itself, not just one instance of it.

Design, matching the Phase 25 plan doc's own stated principle (state is
data; prompts are a rendered view of data): this is a real, JSON-Schema-
constrained extraction call (`use_grammar=True` -- Level-3 structured
output, not a freeform completion parsed with more regex), run ONCE per
task/sub-contract and cached on `TaskContract.goal_facts`
(contracts/schema.py) -- never re-derived per round. Every consumer
treats a populated, matching `goal_facts` as the PREFERRED source and
falls back to the existing regex family unchanged when it's absent or
doesn't apply -- this is an additive safety net, not a replacement of
the regex extraction wholesale (per this project's own explicit
incremental-rollout discipline: only route the shapes this phase
actually improves through structured extraction, leave everything else
alone until independently tested).
"""

from __future__ import annotations

from pydantic import BaseModel

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import call_structured


class GoalFieldFacts(BaseModel):
    """Facts about the ONE field a module-development goal's own text
    concretely states -- every field defaults to "not stated" (None /
    False / empty list), never a guess. `field_name` here is used only
    to confirm this extraction is actually talking about the SAME field
    a caller is asking about (a goal naming a different or no field at
    all must never be silently applied) -- it is NOT a replacement for
    the existing `_GOAL_NAMED_FIELD_RE` omission-detection regex, which
    stays exactly as it is.
    """

    field_name: str | None = None
    field_type: str | None = None
    is_computed: bool = False
    selection_options: list[str] = []
    selection_default: str | None = None
    # Phase 25C addition (2026-07-25): the sequence-assigned-reference-
    # number shape -- a `name`-style field that's a plain, stored, once-
    # editable Char (never `compute=`), but auto-populated from an
    # `ir.sequence` the first time a record is created (Odoo's own
    # standard `vals.get('name', 'New') == 'New'` idiom, the same one
    # `sale.order`/`account.move` use). Deliberately its own boolean,
    # not folded into `is_computed` -- a sequence-assigned field is a
    # real, directly-writable stored column, structurally different
    # from a `compute=` field this schema already refuses to guess a
    # skeleton for.
    is_sequence_assigned: bool = False


_EXTRACTION_PROMPT_TEMPLATE = """\
Read this Odoo module-development task goal. It describes adding ONE \
field to an Odoo model. Extract ONLY facts the goal ACTUALLY STATES -- \
never invent, guess, or infer a value that isn't concretely present in \
the text, even if it seems like a reasonable default.
{focus_note}

- field_name: the exact snake_case Odoo field name, if the goal states \
one (e.g. "order_priority", "is_vip_client"). null if none is stated.
- field_type: the Odoo field type word, one of: Text, Char, Boolean, \
Integer, Float, Monetary, Date, Datetime, Selection, Html, Binary, \
Image, Many2one, One2many, Many2many. null if the goal doesn't state \
or clearly imply one of these.
- is_computed: true if the goal says this field is COMPUTED, RELATED, \
or auto-filled via ONCHANGE (e.g. "compute=", "related=", "onchange=", \
"computed automatically from other fields", "auto-fill", "calculated") \
-- false for a plain, directly-editable field.
- selection_options: ONLY if field_type is Selection AND the goal states \
concrete option values -- the exact list of option value slugs in the \
order given (lowercase, underscore-separated, e.g. ["low", "normal", \
"high"]). Empty list if field_type isn't Selection, or if the goal \
doesn't state concrete option values.
- selection_default: the default option value, ONLY if the goal states \
one explicitly AND it is one of selection_options. null otherwise.
- is_sequence_assigned: true ONLY if the goal describes this field as an \
auto-generated REFERENCE NUMBER assigned from a SEQUENCE the first time \
a record is created (e.g. "sequence-generated", "auto-numbered \
reference", "assign automatically using ir.sequence", "like \
INV2026-0042", "auto-assign a reference number"). This is a plain, \
directly-editable stored field once set, NOT a compute= field -- if \
is_sequence_assigned is true, is_computed must be false.

Task goal:
{goal}

Respond with ONLY a single JSON object matching the schema."""


async def extract_goal_field_facts(
    goal: str, client: ModelGatewayClient, model: str, task_id: str | None = None,
    current_constraint_label: str | None = None,
) -> GoalFieldFacts:
    """Never raises -- a failed/malformed extraction (gateway outage,
    retry-budget exhaustion) returns an all-"not stated" GoalFieldFacts,
    which every consumer already treats identically to "no goal_facts
    available," falling back to the existing regex family unchanged.
    Structured extraction is a strict improvement when it works and a
    guaranteed no-op when it doesn't -- never a new failure mode.

    P12 Tier B/C item 30 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    §4, A Finding 28): real, confirmed gap -- this prompt's own docstring/template both assume
    "this goal describes adding ONE field," but for a decomposed sub-contract, `goal` embeds
    the FULL original multi-field goal plus a "This round's own NEW focus is ONLY: <label>"
    sentence (`manager/loop.py`'s `_run_constraint_labels_from()`) -- the extraction call
    genuinely sees every field mentioned anywhere, not just the current round's. Every real
    consumer already gates on `goal_facts.field_name == <the field being asked about>` before
    applying anything (a genuine safety net, not a false one -- this was never confirmed to
    misfire live, Confidence C), but `current_constraint_label`, when given, is threaded into
    the prompt explicitly so the model is told which field to focus on directly, instead of
    relying entirely on that downstream guard being the only thing standing between this and a
    misapplied extraction.
    """
    focus_note = (
        f"\nThis round's own current focus is ONLY: {current_constraint_label!r} -- if the "
        f"goal text above mentions other fields (belonging to other, not-yet-reached rounds of "
        f"this same multi-part task), extract facts about ONLY the field matching THIS round's "
        f"own focus, ignore the others entirely.\n"
        if current_constraint_label else ""
    )
    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(goal=goal, focus_note=focus_note)
    try:
        return await call_structured(
            client=client, model=model, prompt=prompt, schema=GoalFieldFacts,
            no_think=True, temperature=0.0, task_id=task_id, actor="manager",
            call_label="Extracting structured goal field facts", use_grammar=True,
        )
    except Exception:
        return GoalFieldFacts()
