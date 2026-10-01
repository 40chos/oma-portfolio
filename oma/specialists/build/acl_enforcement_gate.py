"""Phase 33 follow-up (2026-08-11): a real, confirmed gap found live RIGHT AFTER
wiring resolve_current_acl_target_block() (tools_odoo/acl_request_extractor.py)
into Build's prompt -- the resolved, validated group name context block alone was
NOT enough, in two separate ways, both confirmed live against the real running
pipeline (after fixing a stale-process bug that had silently been serving
pre-Phase-33 code for the first two retests):

1. A context block changes what an LLM KNOWS, never guarantees what it DOES --
   two retests submitted a plain field redefinition with ZERO groups=/view-level
   restriction applied at all, even with a resolved group name sitting right
   there in context. Closed by making this a hard, deterministic gate instead
   of a hint.
2. Even once THIS gate started firing and forcing a retry, Build's next attempt
   used `groups="base.group_user"` and a fabricated `"project.group_admin"` --
   confirmed root cause: the group name this pipeline was resolving and handing
   to Build was the human-readable DISPLAY name ("Settings"), not Odoo's real
   `groups=` external-ID syntax ("base.group_system") -- a bare display name
   doesn't look like valid `groups=` syntax at all, so Build ignored it and
   invented its own equally-wrong external-ID-shaped guess instead. Closed by
   resolving and checking against the real external ID
   (ResolvedAclTarget.resolved_group_external_id), never the display name.
"""

from __future__ import annotations

import re

from tools_odoo.acl_request_extractor import (
    ResolvedAclTarget,
    extract_acl_request,
    resolve_acl_request,
)

_FIELD_CONVENTION_RE = re.compile(r"Field:\s*(\w+)")
_GROUPS_VALUE_RE = re.compile(r'groups\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)


def _external_id_present_in(text: str, external_id: str) -> bool:
    for match in _GROUPS_VALUE_RE.finditer(text):
        entries = [e.strip() for e in match.group(1).split(",")]
        if external_id in entries:
            return True
    return False


def validate_resolved_acl_target_is_actually_applied(generated, goal: str, module_identity: str | None, db: str) -> None:
    """Hard-fails the round unless the EXACT resolved external ID (never just
    "any groups= is present") appears where Odoo's own real security model
    requires it. Never fires when extraction found no ACL-restriction shape, or
    when extraction couldn't validate a real group+external-ID pair (that
    failure is already surfaced separately via the context block; this gate
    only enforces a CONFIRMED, fully-resolvable case).

    Real, confirmed gap found live (2026-08-11), fixed here: for a FIELD
    target, view-level-only restriction was previously accepted as sufficient
    -- but Code-Review correctly rejected that live, since a view-level-only
    restriction still leaves the field readable via the ORM/API/other views;
    Odoo's real data-layer security requires the `groups=` kwarg on the field
    DECLARATION itself. A field target now REQUIRES the field-declaration
    restriction specifically -- view-level alone no longer satisfies this gate.
    An action/button target (which has no equivalent "data layer" the same
    way) still accepts either.
    """
    extracted = extract_acl_request(goal)
    if extracted is None or not module_identity:
        return
    resolved = resolve_acl_request(extracted, module_identity, db)
    if not isinstance(resolved, ResolvedAclTarget):
        return

    models_py = getattr(generated, "models_py", "") or ""
    views_xml = getattr(generated, "views_xml", "") or ""
    external_id = resolved.resolved_group_external_id

    field_match = _FIELD_CONVENTION_RE.search(goal)
    field_name = field_match.group(1) if field_match else None

    field_declaration_restricted = False
    if field_name:
        field_decl_match = re.search(
            rf"{re.escape(field_name)}\s*=\s*fields\.\w+\([^)]*\)", models_py, re.DOTALL,
        )
        if field_decl_match and _external_id_present_in(field_decl_match.group(0), external_id):
            field_declaration_restricted = True

    view_restricted = _external_id_present_in(views_xml, external_id)

    if resolved.is_field:
        satisfied = field_declaration_restricted
    else:
        satisfied = field_declaration_restricted or view_restricted

    if not satisfied:
        data_layer_note = (
            " (a view-level-only restriction is NOT sufficient -- it still leaves the field "
            "readable via the ORM/API/other views; Odoo's real data-layer security requires this "
            "on the field declaration itself)" if resolved.is_field else ""
        )
        raise ValueError(
            f"the goal requires restricting {resolved.field_or_action_name!r} to the CONFIRMED "
            f"REAL group {resolved.resolved_group_name!r} -- its real external ID "
            f"{external_id!r} was already resolved and validated against the live database, but "
            f"it does not appear (as the EXACT external ID, not a display name or a different "
            f"group) on the {'field declaration in models.py' if resolved.is_field else 'field declaration or view'}"
            f"{data_layer_note}. Add "
            f'`groups="{external_id}"` EXACTLY as written directly to the '
            f"{f'{field_name!r} ' if field_name else ''}field declaration in models.py -- do not "
            f"invent, shorten, or substitute a different external ID, and do not submit this "
            f"round without it."
        )
