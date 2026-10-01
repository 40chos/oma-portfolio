"""Phase 35 §15.8 -- shadow-mode measurement for deterministic
generators. Never affects a real task's outcome, never submits
anything -- computes whether the generator WOULD have handled a real,
already-passed task, and records that as a real, queryable signal.

Scope, stated honestly: this module measures EXTRACTION accuracy only
(§15.6/§15.12 item 1's concrete mechanism, modeled on the real,
working get_adjudicator_review_sample() pattern) -- whether
extract_single_field_addition_params() succeeds and lands inside the
generator's evidence-backed space, for a real task's real goal_text.
It does not yet render-and-diff-compare against the real applied edit
(that needs the real prior models.py content, which isn't cleanly
available at this call site today) -- that is real, disclosed,
follow-up work, not silently claimed as already covered.

Wired from manager/loop.py's existing run_phase32_post_completion_checks()
-- the same detect-only, never-affects-the-real-outcome hook Phase 32's
own checks already use, per that function's own docstring.
"""

from __future__ import annotations

import logging

from manager.deterministic_generators.extraction import extract_single_field_addition_params

logger = logging.getLogger(__name__)

_SHADOW_SUPPORTED_SCOPES = ("single_new_field",)


def shadow_check_deterministic_extraction(scope: str, goal_text: str, task_id: str) -> dict | None:
    """Returns a result dict for logging/metrics, or None if this scope
    has no deterministic generator being shadow-tested yet. Never
    raises -- a failure in this measurement must never affect the real
    task, the same discipline every other Phase 32 detect-only check
    in this codebase already follows.
    """
    if scope not in _SHADOW_SUPPORTED_SCOPES:
        return None

    try:
        params = extract_single_field_addition_params(goal_text)
    except Exception as exc:  # noqa: BLE001 -- shadow measurement must never break the real task
        logger.warning("Task %s: shadow extraction itself failed to run: %r", task_id, exc)
        return None

    if params is None:
        return {"scope": scope, "extraction_succeeded": False, "in_evidence_backed_space": False}

    return {
        "scope": scope,
        "extraction_succeeded": True,
        "in_evidence_backed_space": params.is_in_evidence_backed_space(),
        "module_name": params.module_name,
        "model_name": params.model_name,
        "field_name": params.field_name,
        "field_type": params.field_type,
    }
