"""P12 Tier S/A item 6 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
§4, item 5 in the source synthesis's own numbering): `RoundScope` -- the single, structured
source of truth for "what is this round actually scoped to," replacing the triplicated regex
re-derivation confirmed live in `specialists/code_review/specialist.py` (three independent
`_REVIEW_NOT_YET_IN_SCOPE_RE.search(goal or "")` call sites -- `_filter_hallucinated_empty_
security_csv_findings`, `_filter_hallucinated_out_of_scope_field_required_findings`,
`_filter_hallucinated_stale_scope_exclusion_findings` -- each independently re-parsing the
SAME rendered prose sentence out of `contract.goal`, structurally able to drift out of sync
with each other and with the real, already-structured fields underneath it).

`TaskContract.current_constraint_label` / `.remaining_constraint_labels`
(`contracts/schema.py`) already carry this exact information as real, typed fields --
`_run_constraint_labels_from()` (`manager/loop.py`) sets both of them at the SAME point it
renders the equivalent sentences into `goal` for the LLM's own benefit, from the same source
data, so they can never drift from each other. `derive_round_scope()` reads those fields
directly -- zero regex, zero prose-parsing, nothing to desync.

Lives in `contracts/` (not `manager/`) deliberately: `contracts/` is already imported by
every specialist (this project's own manager/specialists layering forbids specialists
importing from `manager/` or from each other's internals), so this is importable from
Code-Review, Testing/QA, or Build without violating that boundary.
"""

from __future__ import annotations

from pydantic import BaseModel

from contracts.schema import TaskContract


class RoundScope(BaseModel):
    """`current_focus`: this round's own single constraint label, or None for a
    non-decomposed task. `not_yet_in_scope`: the ordered list of constraint labels this
    round's goal explicitly defers, lower-cased (matching every existing consumer's own
    case-insensitive comparison convention). `is_decomposed`: whether this task is a
    decomposed multi-constraint task at all -- a plain task has an empty `not_yet_in_scope`
    for a structurally different reason (nothing was ever deferred) than a decomposed task's
    final round (everything has now been satisfied); callers that need to tell those two
    apart read this field instead of inferring it from an empty list.
    """

    current_focus: str | None = None
    not_yet_in_scope: list[str] = []
    is_decomposed: bool = False


def derive_round_scope(contract: TaskContract) -> RoundScope:
    """Builds a `RoundScope` directly from `contract`'s own already-structured fields --
    never from `contract.goal` prose. `is_decomposed` reads `constraint_status` (non-empty
    only for a decomposed task, set once by `_run_decomposed_task()`/`_run_constraint_labels_
    from()` and carried forward unchanged), the same field P12 item 4's continuation fix
    already gates on.
    """
    return RoundScope(
        current_focus=contract.current_constraint_label,
        not_yet_in_scope=[label.strip().lower() for label in contract.remaining_constraint_labels if label.strip()],
        is_decomposed=bool(contract.constraint_status),
    )
