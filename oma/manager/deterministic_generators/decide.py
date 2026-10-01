"""Phase 35 §15.5 -- the actual decision chain, real and callable, not
prose. Ties together extraction (§15.1), the evidence-backed-space
check (§14.2), the live/disabled flag (§15.9), direction certification
(§15.5's own explicit "not the same claim" fix), and live preconditions
(§15.6) into one function a caller can invoke.
"""

from __future__ import annotations

from dataclasses import dataclass

from manager.deterministic_generators.extraction import extract_single_field_addition_params
from manager.deterministic_generators.preconditions import check_single_field_addition_preconditions
from manager.deterministic_generators.registry import is_generator_live
from manager.deterministic_generators.single_new_field import (
    StructuralPreconditionError,
    UnsupportedFieldTypeError,
    render_single_field_addition,
)


@dataclass
class DeterministicDecision:
    used_deterministic_path: bool
    reason: str
    edit: dict | None = None


def decide_single_new_field(
    goal_text: str, scope: str, db: str, current_models_py: str | None,
    *, is_scope_certified: bool,
) -> DeterministicDecision:
    """§15.5 step 1, made real. Returns a DeterministicDecision --
    used_deterministic_path=True only if every real gate passes;
    otherwise the caller falls back to §15.5 path 2/3 (retrieval, then
    cold LLM generation) exactly as today's system already does, per
    this function's own contract of never guessing past a failed gate.
    """
    if scope != "single_new_field":
        return DeterministicDecision(False, f"no deterministic generator built for scope {scope!r}")

    if not is_generator_live(scope):
        return DeterministicDecision(False, "generator exists but is not flagged live (§15.9 default-off)")

    if not is_scope_certified:
        return DeterministicDecision(False, "direction's own certification is not currently valid (§15.5's explicit second gate condition)")

    params = extract_single_field_addition_params(goal_text)
    if params is None:
        return DeterministicDecision(False, "extraction failed -- goal_text did not match the known structured shape")

    if not params.is_in_evidence_backed_space():
        return DeterministicDecision(False, f"field_type={params.field_type!r}/security_group={params.security_group!r} outside evidence-backed space")

    precondition = check_single_field_addition_preconditions(
        params.module_name, params.model_name, params.field_name, db
    )
    if not precondition.ok:
        return DeterministicDecision(False, f"live precondition failed: {precondition.reason}")

    if current_models_py is None:
        return DeterministicDecision(False, "no current models.py content supplied to render against")

    try:
        edit = render_single_field_addition(params, current_models_py)
    except (UnsupportedFieldTypeError, StructuralPreconditionError) as exc:
        return DeterministicDecision(False, f"render failed: {exc}")

    return DeterministicDecision(True, "all gates passed", edit=edit)
