"""P14 item 1 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_2026-07-29.md
§18c.4b, ranked item 1: "treat P6's data_change skill-wiring gap and P12 item 1 as one unit of
work, not two separately-owned pieces"): schema-guided structured extraction of a
`capability_class=data_change` goal's own concrete write operation.

P12 item 1 (already shipped, `manager/capability_readiness.py`) correctly disabled the
`data_change` route at a deterministic pre-flight gate, since `BuildSpecialist._run_data_change()`
was, and until this item, remained, a permanent, documented, never-exercised stub -- the honest
fix at the time, not a workaround. P6 (`docs/reports/PHASE30_P6_SKILL_STALENESS_2026-07-30.md`)
separately found `skills/odoo-xmlrpc-operations/SKILL.md` is real, well-written, and genuinely
orphaned -- no live code path ever reads it. This module is the first half of actually closing
both gaps as one real capability: turning free text into the exact `(model, record identity,
field, new value)` tuple the skill's own documented procedure needs, extracted the same way
`contracts/goal_facts.py` already does for `module_dev` goals -- never guessed, never invented.

Lives in `contracts/`, matching every other structured-extraction module's own placement
rationale: both `manager/` and `specialists/` already import freely from `contracts/`.
"""

from __future__ import annotations

from pydantic import BaseModel

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import call_structured


class DataChangeOperation(BaseModel):
    """The one, concrete write operation a `data_change` goal describes -- every field defaults
    to "not stated" (None), never a guess, matching `contracts/goal_facts.py`'s own established
    discipline. Deliberately scoped to a single-field WRITE against an EXISTING record found by
    an exact-match search field -- the concrete example both P12 item 1's own source finding
    ("flip `tracking=True` on a field") and the skill's own documented procedure describe. `create`/
    `unlink` operations, multi-field writes, and non-exact-match record search are explicitly out
    of scope here -- real, separate, larger-scoped follow-up work, not attempted as a guess.
    """

    model: str | None = None
    record_search_field: str | None = None
    record_search_value: str | None = None
    field: str | None = None
    new_value: str | None = None


_EXTRACTION_PROMPT_TEMPLATE = """\
Read this Odoo task goal. It describes a single, concrete change to ONE FIELD on an EXISTING \
record of an existing Odoo model -- e.g. "set tracking=True on the priority field of the record \
named X" or "update the phone number on the contact whose email is x@example.com". Extract ONLY \
facts the goal ACTUALLY STATES -- never invent, guess, or infer a value that isn't concretely \
present in the text, even if it seems like a reasonable default.

- model: the real, dotted Odoo model name the goal names or clearly implies (e.g. "res.partner"). \
null if the goal doesn't clearly name or imply one real model.
- record_search_field: the real field name used to FIND the one target record (e.g. "email", \
"name", "id") -- ONLY if the goal states an exact value to search by. null if the goal doesn't \
identify the target record by an exact, searchable value.
- record_search_value: the exact value to search for in record_search_field. null if none stated.
- field: the real field name being changed. null if the goal doesn't name one exact field.
- new_value: the exact new value to write, as the goal states it (e.g. "True", "42", \
"new phone number"). null if the goal doesn't state one concrete value.

If the goal describes creating a new record, deleting a record, changing more than one field, or \
searching by anything other than one exact field/value match, set every field to null -- this \
schema is deliberately narrow and must never be stretched to cover those shapes.

Task goal:
{goal}

Respond with ONLY a single JSON object matching the schema."""


async def extract_data_change_operation(
    goal: str, client: ModelGatewayClient, model: str, task_id: str | None = None,
) -> DataChangeOperation:
    """Never raises -- a failed/malformed extraction (gateway outage, retry-budget exhaustion)
    returns an all-"not stated" DataChangeOperation, which the real caller
    (`BuildSpecialist._run_data_change()`) treats identically to "cannot proceed, report honestly,"
    never a guessed default.
    """
    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(goal=goal)
    try:
        return await call_structured(
            client=client, model=model, prompt=prompt, schema=DataChangeOperation,
            no_think=True, temperature=0.0, task_id=task_id, actor="manager",
            call_label="Extracting the data_change operation", use_grammar=True,
        )
    except Exception:
        return DataChangeOperation()


def is_complete(operation: DataChangeOperation) -> bool:
    """True only when every real field this operation needs to be executed is concretely
    present -- a partial extraction (e.g. model + field named, but no record_search_value) is
    never treated as "close enough," matching every other structured-extraction consumer in this
    codebase's own conservative posture.
    """
    return bool(
        operation.model and operation.record_search_field and operation.record_search_value
        and operation.field and operation.new_value is not None
    )
