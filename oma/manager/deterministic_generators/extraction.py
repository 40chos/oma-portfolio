"""Phase 35 §15.1's "structured extraction" step, for `single_new_field`
only. Parses the real, structured footer every real single_new_field
goal_text in this codebase's own history actually has -- checked
directly against state/scope_certification.json's real 13 recorded
outcomes, not assumed -- rather than attempting general natural-
language understanding this module does not need for its evidence-
backed space.

Real goal_text shape, from real recorded history (verbatim example):

    Add a new field named 'last_review_date' (Date) to the res.partner
    model, in the oma_i_want_to_mark_e58d9194 module's own
    models/models.py file, NOT a new extension module. Do not add any
    view changes.

    Module: oma_i_want_to_mark_e58d9194
    Model: res.partner
    Field: last_review_date (Date)

This extractor deliberately parses only the structured "Module:
/Model:/Field:" footer, not the free-form leading sentence -- the
footer is the real, consistent, machine-generated part of every real
example this generator's evidence is drawn from; the leading sentence
is free text and is NOT what this narrow extractor is built to handle.
A goal_text without that footer returns None -- §15.5's "extraction
fails" branch, routed to LLM generation, never guessed at.
"""

from __future__ import annotations

import re

from manager.deterministic_generators.single_new_field import SingleFieldAdditionParams

_FOOTER_RE = re.compile(
    r"Module:\s*(?P<module>\S+)\s*\n"
    r"Model:\s*(?P<model>\S+)\s*\n"
    r"Field:\s*(?P<field>\w+)\s*\("
    r"(?P<type>Char|Boolean|Integer|Date|Text|Selection)"
    r"(?::\s*(?P<selection_raw>[^)]+))?"
    r"(?:,\s*default\s+(?P<default>\w+))?"
    r"\)",
    re.IGNORECASE,
)

_TYPE_CANONICAL = {t.lower(): t for t in ("Char", "Boolean", "Integer", "Date", "Text", "Selection")}


def _parse_default(field_type: str, raw: str | None) -> bool | int | str | None:
    if raw is None:
        return None
    if field_type == "Boolean":
        return raw.strip().lower() in ("true", "1", "yes")
    if field_type == "Integer":
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return raw.strip()


def extract_single_field_addition_params(goal_text: str) -> SingleFieldAdditionParams | None:
    """Returns None on any extraction failure -- §15.5's "extraction
    fails" branch. Never raises, never guesses a partial result.
    """
    match = _FOOTER_RE.search(goal_text)
    if match is None:
        return None

    field_type = _TYPE_CANONICAL.get(match.group("type").lower())
    if field_type is None:
        return None

    selection_options: list[tuple[str, str]] | None = None
    if field_type == "Selection":
        raw = match.group("selection_raw") or ""
        # Real recorded shape: "low, normal, urgent" or "low/normal/urgent"
        # -- both seen in real history (order_priority used '/', later
        # examples used ', '). Split on either, never guess a label beyond
        # the raw token itself (this extractor does not invent human-
        # readable labels -- it only knows what the goal_text states).
        parts = [p.strip() for p in re.split(r"[,/]", raw) if p.strip()]
        if not parts:
            return None
        selection_options = [(p, p.replace("_", " ").capitalize()) for p in parts]

    default = _parse_default(field_type, match.group("default"))

    try:
        return SingleFieldAdditionParams(
            module_name=match.group("module"),
            model_name=match.group("model"),
            field_name=match.group("field"),
            field_type=field_type,
            default=default,
            selection_options=selection_options,
        )
    except ValueError:
        # Schema validation itself rejected it (e.g. malformed field name)
        # -- extraction "succeeded" at the regex level but produced an
        # invalid contract. Still a real extraction failure, not a
        # generator failure -- same "fall back, never guess" contract.
        return None
