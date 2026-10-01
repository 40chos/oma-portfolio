"""Phase 33 implementation (2026-08-11): the deterministic code-generation half
(§3 item 2b) of the visibility/ACL fix -- consumes ONLY the validated, structured
output of tools_odoo/acl_request_extractor.py's resolve_acl_request() (§3 item 2a),
never free text. Per the plan's own explicit warning: building this half alone,
without 2a, would not close the real gap -- it would just move the same guessing
problem one step earlier. This module must never be called with anything other
than a real ResolvedAclTarget.
"""

from __future__ import annotations

import re

from tools_odoo.acl_request_extractor import ResolvedAclTarget

_FIELD_CONVENTION_RE = re.compile(r"Field:\s*(\w+)")


def generate_field_groups_kwarg_snippet(resolved: ResolvedAclTarget, field_declaration_line: str) -> str:
    """Given an existing field declaration line (e.g. `internal_approver_id = fields.Many2one(...)`),
    inserts a groups= kwarg naming the resolved, confirmed-real group -- mechanical
    string work only, since the hard part (which group, does it exist) was already
    resolved and validated upstream."""
    if not resolved.is_field:
        raise ValueError("generate_field_groups_kwarg_snippet() requires a field target, not an action/button")
    if "groups=" in field_declaration_line:
        raise ValueError("field declaration already has a groups= kwarg -- do not double-restrict")
    group_ref = resolved.resolved_group_external_id
    stripped = field_declaration_line.rstrip()
    if stripped.endswith(")"):
        return f'{stripped[:-1]}, groups="{group_ref}")'
    return f'{stripped}, groups="{group_ref}"'


def autofix_apply_resolved_acl_target_to_models_py(generated, goal: str, module_identity: str | None, db: str) -> None:
    """Phase 33 follow-up (2026-08-11): real, confirmed gap found live -- the
    deterministic view-XML override (specialists/build/specialist.py's
    `_render_view_field_tags()`) correctly hides a restricted field in the UI,
    but Code-Review correctly rejected that as insufficient on its own: a
    view-level-only restriction still leaves the field readable via the ORM/
    API/other views -- Odoo's own real data-layer security requires the
    `groups=` kwarg on the FIELD DECLARATION itself, not just the view. This
    autofix applies that field-level restriction deterministically, using the
    SAME validated ResolvedAclTarget as the view-level fix, so both layers are
    always correct together rather than depending on the LLM to remember the
    second one. Mutates `generated.models_py` in place; a no-op (never raises)
    when there's no ACL shape, no resolvable target, no matching field
    declaration, or the field is already restricted.
    """
    from tools_odoo.acl_request_extractor import extract_acl_request, resolve_acl_request

    if not module_identity:
        return
    extracted = extract_acl_request(goal)
    if extracted is None:
        return
    resolved = resolve_acl_request(extracted, module_identity, db)
    if not isinstance(resolved, ResolvedAclTarget) or not resolved.is_field:
        return

    field_match = _FIELD_CONVENTION_RE.search(goal)
    if not field_match:
        return
    field_name = field_match.group(1)

    models_py = getattr(generated, "models_py", "") or ""
    decl_match = re.search(
        rf"^([ \t]*{re.escape(field_name)}\s*=\s*fields\.\w+\([^)]*\))", models_py, re.MULTILINE | re.DOTALL,
    )
    if not decl_match:
        return
    existing_line = decl_match.group(1)
    if "groups=" in existing_line:
        return  # already restricted (correctly or not) -- the hard enforcement gate judges that
    new_line = generate_field_groups_kwarg_snippet(resolved, existing_line)
    generated.models_py = models_py[: decl_match.start(1)] + new_line + models_py[decl_match.end(1):]


def generate_button_groups_attribute_snippet(resolved: ResolvedAclTarget, button_tag_line: str) -> str:
    """Given an existing <button .../> XML line, inserts a groups= attribute naming
    the resolved, confirmed-real group."""
    if resolved.is_field:
        raise ValueError("generate_button_groups_attribute_snippet() requires an action/button target, not a field")
    if "groups=" in button_tag_line:
        raise ValueError("button tag already has a groups= attribute -- do not double-restrict")
    group_ref = resolved.resolved_group_external_id
    stripped = button_tag_line.rstrip()
    for closer in ("/>", ">"):
        if stripped.endswith(closer):
            return f'{stripped[: -len(closer)]} groups="{group_ref}"{closer}'
    raise ValueError(f"button tag line does not look like a valid XML tag: {button_tag_line!r}")
