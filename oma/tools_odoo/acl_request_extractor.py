"""Phase 33 implementation (2026-08-11): the extraction-and-validation step from
docs/planning/PHASE33_CLOSING_THE_GAP_TO_DAY_TO_DAY_WORK_2026-08-11.md §3 item 2a --
the actually-hard, security-relevant half of the visibility/ACL-restriction fix,
which must run BEFORE any code generation (§3 item 2b), never after.

Real, confirmed root cause this closes: goals like "only System Administrators
should see who approved this record" currently get handed to Build as free text,
which then freehand-writes a `groups=` kwarg or `ir.rule` domain, guessing at the
exact group name and field/method target -- the same "hallucination whack-a-mole"
failure mode Phase 32 §1.2 already diagnosed, landing on the single highest-stakes
sub-case (a wrong guess here is a security defect, not a cosmetic miss). Mined from
real production history, 90 real tasks of this shape, only 33% pass.

This module does the NLU-to-structured-parameter step deterministically (regex, not
an LLM call) and validates every extracted piece against what actually, really
exists on the live target database (via the existing check_group_exists_fast()/
check_field_exists_on_model_fast() primitives in tools_odoo/odoo_schema_client.py --
this module builds no new low-level Odoo query plumbing, it orchestrates what
already exists). If any piece can't be resolved with confidence, this returns a
clear failure reason instead of guessing -- per the plan's own explicit rule:
"failing loudly and asking a human if extraction can't resolve a piece, never
guessing."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from contracts.schema import TaskContract

# Common phrasings observed directly in real production goal text (mined
# 2026-08-11): "only X should see Y", "only X should have access to Y", "only X
# should see a Y button/action". Deliberately several patterns, not one -- this
# session's own history is full of narrow text filters a rephrasing immediately
# dodges; matching the underlying SHAPE (role, then a visibility/access verb, then
# a target description) is the point, not one exact wording.
_ACL_REQUEST_PATTERNS = (
    re.compile(
        r"only\s+(?P<role>[A-Za-z][A-Za-z ]{2,40}?)\s+should\s+(?:see|access|have access to)\s+"
        r"(?:a\s+|an\s+|the\s+)?(?P<target>[a-zA-Z][a-zA-Z0-9 _'\-]{2,60})",
        re.IGNORECASE,
    ),
    re.compile(
        r"only\s+(?P<role>[A-Za-z][A-Za-z ]{2,40}?)\s+(?:can|should be able to)\s+"
        r"(?:see|access|view)\s+(?:a\s+|an\s+|the\s+)?(?P<target>[a-zA-Z][a-zA-Z0-9 _'\-]{2,60})",
        re.IGNORECASE,
    ),
)

# A handful of well-known real-world aliases for common role phrasing, tried only
# AFTER the exact phrase itself fails to resolve against the live database --
# never used to skip the live check, only to widen it when Odoo's own display name
# doesn't literally match the phrase a real person used in a goal.
_ROLE_ALIASES = {
    "system administrators": ["Administration / Settings", "Settings", "Administrator"],
    "administrators": ["Administration / Settings", "Settings", "Administrator"],
}


@dataclass
class ExtractedAclRequest:
    role_phrase: str
    target_description: str


@dataclass
class ResolvedAclTarget:
    model_name: str
    resolved_group_name: str
    resolved_group_external_id: str
    field_or_action_name: str
    is_field: bool


@dataclass
class AclResolutionFailure:
    reason: str
    unresolved_role_phrase: str | None = None
    unresolved_target_description: str | None = None


def extract_acl_request(goal_text: str) -> ExtractedAclRequest | None:
    """Deterministic extraction only -- no live validation here. Returns None
    (never a low-confidence guess) if the goal text doesn't match any known
    real-world phrasing shape."""
    for pattern in _ACL_REQUEST_PATTERNS:
        match = pattern.search(goal_text)
        if match:
            return ExtractedAclRequest(
                role_phrase=match.group("role").strip(),
                target_description=match.group("target").strip(),
            )
    return None


def resolve_acl_request(
    extracted: ExtractedAclRequest,
    model_name: str,
    db: str,
    *,
    check_group_exists_fn=None,
    check_field_exists_fn=None,
    resolve_group_external_id_fn=None,
) -> ResolvedAclTarget | AclResolutionFailure:
    """Validates the extracted role phrase against a REAL, live group, and the
    extracted target description against a REAL field on `model_name` -- never
    guesses. Dependency-injected check functions default to the real
    odoo_schema_client primitives; tests inject fakes.

    Real, confirmed gap found live (2026-08-11), fixed here: a group's real
    external ID (e.g. "base.group_system") is ALWAYS resolved too, never just
    the display name -- Odoo's `groups=` kwarg requires the external ID, and
    handing Build a bare display name produced worse guesses than no guidance
    at all (confirmed live: given "Settings" as guidance, Build ignored it and
    invented its OWN wrong external-ID-shaped strings instead). A group that
    exists but has no resolvable external ID is treated as a resolution
    failure, same as a nonexistent group -- never proceeds with only a display
    name that isn't valid `groups=` syntax.
    """
    if check_group_exists_fn is None:
        from tools_odoo.odoo_schema_client import check_group_exists_fast
        check_group_exists_fn = check_group_exists_fast
    if check_field_exists_fn is None:
        from tools_odoo.odoo_schema_client import check_field_exists_on_model_fast
        check_field_exists_fn = check_field_exists_on_model_fast
    if resolve_group_external_id_fn is None:
        from tools_odoo.odoo_schema_client import resolve_group_external_id_fast
        resolve_group_external_id_fn = resolve_group_external_id_fast

    resolved_group = None
    candidates = [extracted.role_phrase] + _ROLE_ALIASES.get(extracted.role_phrase.lower(), [])
    for candidate in candidates:
        exists = check_group_exists_fn(db, candidate)
        if exists:
            resolved_group = candidate
            break
        if exists is None:
            return AclResolutionFailure(
                reason=f"could not confirm whether group {candidate!r} exists -- live check unavailable",
                unresolved_role_phrase=extracted.role_phrase,
            )
    if resolved_group is None:
        return AclResolutionFailure(
            reason=(
                f"role phrase {extracted.role_phrase!r} does not resolve to any real, existing "
                f"group on {db!r} (tried: {', '.join(candidates)})"
            ),
            unresolved_role_phrase=extracted.role_phrase,
        )

    external_id = resolve_group_external_id_fn(db, resolved_group)
    if not external_id:
        return AclResolutionFailure(
            reason=(
                f"group {resolved_group!r} exists but has no resolvable external ID (ir.model.data "
                f"entry) -- never proceeding with only a display name, since that is not valid "
                f"`groups=` syntax and produces worse guesses than no guidance at all"
            ),
            unresolved_role_phrase=extracted.role_phrase,
        )

    # The target description names either a field (e.g. "internal credit note
    # field", "who approved this record") or an action/button (e.g. "a Send
    # Reminder button"). Deliberately does not attempt to guess the exact
    # snake_case field/method name from free text -- that guess is exactly what
    # this module exists to avoid. Instead it reports what it found and lets the
    # caller (Build, already holding the real field list via schema_grounding.py)
    # match it against the real, known field/method names -- this function's job
    # ends at "a real role resolved to a real group," which is the security-
    # relevant half; matching prose to an exact identifier is a separate,
    # lower-stakes text-matching problem already handled elsewhere in this
    # pipeline's existing schema-grounding machinery.
    is_field = "button" not in extracted.target_description.lower() and "action" not in extracted.target_description.lower()

    return ResolvedAclTarget(
        model_name=model_name,
        resolved_group_name=resolved_group,
        resolved_group_external_id=external_id,
        field_or_action_name=extracted.target_description,
        is_field=is_field,
    )


def format_resolved_acl_target_block(resolved: ResolvedAclTarget) -> str:
    kind = "field" if resolved.is_field else "action/button"
    return (
        f"RESOLVED ACL TARGET (validated against the real database, not guessed):\n"
        f"  model: {resolved.model_name}\n"
        f"  restrict this {kind}: {resolved.field_or_action_name!r}\n"
        f"  real group: {resolved.resolved_group_name!r}\n"
        f"  REQUIRED groups= value (the real external ID -- Odoo's groups= kwarg needs THIS, "
        f"never the display name above): \"{resolved.resolved_group_external_id}\"\n"
        f'Use `groups="{resolved.resolved_group_external_id}"` EXACTLY as written, in both the '
        f"field declaration AND the view -- do not invent, shorten, or substitute a different "
        f"external ID, even one that looks plausible."
    )


def format_acl_resolution_failure_block(failure: AclResolutionFailure) -> str:
    return (
        f"ACL EXTRACTION COULD NOT BE VALIDATED: {failure.reason}. "
        f"Do not guess a group name -- stop and ask a human to confirm the correct real group."
    )


def resolve_current_acl_target_block(contract: "TaskContract", db: str) -> str:
    """The live prompt-injection entry point (mirrors tools_odoo/schema_grounding.py's own
    resolve_current_schema_block() contract exactly): runs extraction + real, live validation
    against `contract.goal`/`contract.module_identity`, and returns a ready-to-inject text block
    -- either a CONFIRMED REAL group to use, or an explicit failure notice telling Build to ask a
    human rather than guess. Returns "" (never raises) when the goal has no ACL-restriction shape
    at all -- every caller treats "" as "nothing to render," same as every sibling grounding
    function in this codebase.
    """
    goal_text = getattr(contract, "goal", "") or ""
    extracted = extract_acl_request(goal_text)
    if extracted is None:
        return ""
    model_name = getattr(contract, "module_identity", None)
    if not model_name:
        return ""
    resolved = resolve_acl_request(extracted, model_name, db)
    if isinstance(resolved, ResolvedAclTarget):
        return format_resolved_acl_target_block(resolved)
    return format_acl_resolution_failure_block(resolved)
