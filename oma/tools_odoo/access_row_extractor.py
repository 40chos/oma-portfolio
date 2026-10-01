"""Phase 34 execution (2026-08-11): the extraction-and-validation mechanism for
MODEL-LEVEL access-row (ir.model.access.csv) requests -- a structurally distinct
third category from the field/button `groups=` restriction already fixed
tonight (tools_odoo/acl_request_extractor.py). Real, confirmed gap found live
during Batch M execution: a goal narrowing an existing group's CRUD access on a
whole model, and/or adding a new group's access row, was left to Build's
freehand generation, which got the group reference wrong and never applied the
narrowing at all -- the same "LLM guesses a specific identifier instead of using
a deterministic, validated lookup" root cause as every other ACL bug tonight,
just for a different artifact (a CSV row's four boolean permission columns,
not a field/button's `groups=` kwarg).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_XMLID_RE = re.compile(r"\b([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)\b")

# Permission phrasings observed directly in real production goal text. Deliberately
# several entries, not one -- matching this session's own repeated lesson that a
# single exact phrasing is always eventually dodged by a rephrasing. Checked in
# order; the first matching entry wins (more specific phrases before "full access").
_PERM_PHRASES: list[tuple[re.Pattern, tuple[bool, bool, bool, bool]]] = [
    (re.compile(r"read.?only", re.IGNORECASE), (True, False, False, False)),
    (re.compile(r"read\s+and\s+write.{0,30}not\s+create\s+or\s+delete", re.IGNORECASE), (True, True, False, False)),
    (re.compile(r"read\s+and\s+write.{0,30}not\s+create", re.IGNORECASE), (True, True, False, False)),
    (re.compile(r"read.?/.?write\b", re.IGNORECASE), (True, True, False, False)),
    (re.compile(r"read\s+and\s+write\s+access\b", re.IGNORECASE), (True, True, False, False)),
    (re.compile(r"full\s+(read,?\s*write,?\s*create,?\s*(and\s+)?delete|access)", re.IGNORECASE), (True, True, True, True)),
]


@dataclass
class AccessRowSpec:
    group_ref: str  # a real xmlid ("base.group_user") or a display name to resolve
    perm_read: bool
    perm_write: bool
    perm_create: bool
    perm_unlink: bool


@dataclass
class ExtractedAccessRowChanges:
    narrow_group_ref: str | None
    narrow_target_perms: tuple[bool, bool, bool, bool] | None
    add_group_ref: str | None
    add_perms: tuple[bool, bool, bool, bool] | None


@dataclass
class ResolvedAccessRowChanges:
    model_name: str
    narrow: AccessRowSpec | None
    add: AccessRowSpec | None


@dataclass
class AccessRowResolutionFailure:
    reason: str


def _match_perm_phrase(window: str) -> tuple[bool, bool, bool, bool] | None:
    for pattern, flags in _PERM_PHRASES:
        if pattern.search(window):
            return flags
    return None


def extract_access_row_request(goal_text: str) -> ExtractedAccessRowChanges | None:
    """Deterministic extraction only -- no live validation here. Returns None if
    the goal names neither a narrow-existing-row instruction nor an add-new-row
    instruction."""
    narrow_group_ref = None
    narrow_perms = None
    add_group_ref = None
    add_perms = None

    grant_match = re.search(
        r"grants?\s+([\w.]+)\s+.{0,40}?access\s+to\b", goal_text, re.IGNORECASE,
    )
    narrow_instruction = re.search(
        r"(?:narrow|restrict)\s+(?:that row|this row|the existing row)?.{0,10}to\s+([\w\-/ ,]{3,30}?)(?:[,.]|$| and)",
        goal_text, re.IGNORECASE,
    )
    if grant_match and narrow_instruction:
        narrow_perms = _match_perm_phrase(narrow_instruction.group(1))
        if narrow_perms is not None:
            narrow_group_ref = grant_match.group(1)

    add_match = re.search(
        r"add\s+(?:a\s+|another\s+)?separate\s+([\w\-/ ]{3,30}?)\s+row\s+for\s+"
        r"(?:the\s+existing\s+|a\s+|an\s+)?group\s+(?:named\s+)?['\"]?([\w. ]+?)['\"]?[.,]",
        goal_text, re.IGNORECASE,
    ) or re.search(
        r"add\s+(?:a\s+|another\s+)?separate\s+([\w\-/ ]{3,30}?)\s+row\s+for\s+"
        r"(?:the\s+existing\s+|a\s+|an\s+)?(\w+)\s+group\b",
        goal_text, re.IGNORECASE,
    )
    if add_match:
        add_perms = _match_perm_phrase(add_match.group(1))
        if add_perms is not None:
            add_group_ref = add_match.group(2).strip()

    if narrow_group_ref is None and add_group_ref is None:
        return None
    return ExtractedAccessRowChanges(
        narrow_group_ref=narrow_group_ref, narrow_target_perms=narrow_perms,
        add_group_ref=add_group_ref, add_perms=add_perms,
    )


def _resolve_one_group_ref(
    group_ref: str, db: str, check_group_exists_fn, resolve_group_external_id_fn,
) -> str | None:
    """A group_ref that's already xmlid-shaped (contains a dot, e.g. base.group_user)
    is trusted as-is only if a real group with that name portion resolves; a plain
    display name is resolved to its real external id -- never guessed either way."""
    if _XMLID_RE.fullmatch(group_ref):
        # Still validated, not blindly trusted: the module/group must really exist.
        return group_ref if check_group_exists_fn(db, group_ref) else None
    exists = check_group_exists_fn(db, group_ref)
    if not exists:
        return None
    return resolve_group_external_id_fn(db, group_ref)


def resolve_access_row_request(
    extracted: ExtractedAccessRowChanges,
    model_name: str,
    db: str,
    *,
    check_group_exists_fn=None,
    resolve_group_external_id_fn=None,
) -> ResolvedAccessRowChanges | AccessRowResolutionFailure:
    if check_group_exists_fn is None:
        from tools_odoo.odoo_schema_client import check_group_exists_fast
        check_group_exists_fn = check_group_exists_fast
    if resolve_group_external_id_fn is None:
        from tools_odoo.odoo_schema_client import resolve_group_external_id_fast
        resolve_group_external_id_fn = resolve_group_external_id_fast

    narrow_spec = None
    if extracted.narrow_group_ref is not None:
        resolved_ref = _resolve_one_group_ref(
            extracted.narrow_group_ref, db, check_group_exists_fn, resolve_group_external_id_fn,
        )
        if resolved_ref is None:
            return AccessRowResolutionFailure(
                reason=f"group {extracted.narrow_group_ref!r} (to narrow) does not resolve to a real group on {db!r}",
            )
        r, w, c, u = extracted.narrow_target_perms
        narrow_spec = AccessRowSpec(group_ref=resolved_ref, perm_read=r, perm_write=w, perm_create=c, perm_unlink=u)

    add_spec = None
    if extracted.add_group_ref is not None:
        resolved_ref = _resolve_one_group_ref(
            extracted.add_group_ref, db, check_group_exists_fn, resolve_group_external_id_fn,
        )
        if resolved_ref is None:
            return AccessRowResolutionFailure(
                reason=f"group {extracted.add_group_ref!r} (to add) does not resolve to a real group on {db!r}",
            )
        r, w, c, u = extracted.add_perms
        add_spec = AccessRowSpec(group_ref=resolved_ref, perm_read=r, perm_write=w, perm_create=c, perm_unlink=u)

    return ResolvedAccessRowChanges(model_name=model_name, narrow=narrow_spec, add=add_spec)


def format_resolved_access_row_block(resolved: ResolvedAccessRowChanges) -> str:
    lines = ["RESOLVED ACCESS-ROW CHANGES (validated against the real database, not guessed):"]
    if resolved.narrow:
        s = resolved.narrow
        lines.append(
            f"  narrow the existing row for group {s.group_ref!r} on {resolved.model_name!r} to "
            f"read={s.perm_read},write={s.perm_write},create={s.perm_create},unlink={s.perm_unlink}"
        )
    if resolved.add:
        s = resolved.add
        lines.append(
            f"  add a new row for CONFIRMED REAL group {s.group_ref!r} on {resolved.model_name!r} with "
            f"read={s.perm_read},write={s.perm_write},create={s.perm_create},unlink={s.perm_unlink}"
        )
    lines.append("Use these exact group references and permission values -- do not invent different ones.")
    return "\n".join(lines)


def format_access_row_resolution_failure_block(failure: AccessRowResolutionFailure) -> str:
    return (
        f"ACCESS-ROW EXTRACTION COULD NOT BE VALIDATED: {failure.reason}. "
        f"Do not guess a group reference -- stop and ask a human to confirm the correct real group."
    )
