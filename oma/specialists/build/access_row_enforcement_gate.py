"""Phase 34 execution (2026-08-11): deterministic autofix + hard enforcement gate
for model-level access-row (ir.model.access.csv) requests, mirroring the exact
pattern already proven tonight for field/button ACL (extraction -> validation ->
deterministic autofix -> hard gate, never trusting LLM freehand compliance).
"""

from __future__ import annotations

import re

from tools_odoo.access_row_extractor import (
    ResolvedAccessRowChanges,
    extract_access_row_request,
    resolve_access_row_request,
)


def _model_bare(model_name: str) -> str:
    return "model_" + model_name.replace(".", "_")


def _find_csv_row_for_group(csv_text: str, model_bare: str, group_ref: str) -> tuple[int, list[str]] | None:
    """Returns (line_index, columns) for the row matching this model+group, or None."""
    lines = csv_text.splitlines()
    if len(lines) < 2:
        return None
    header = [h.strip() for h in lines[0].split(",")]
    try:
        model_idx = header.index("model_id:id")
        group_idx = header.index("group_id:id")
    except ValueError:
        return None
    group_bare = group_ref.rsplit(".", 1)[-1]
    for idx, line in enumerate(lines[1:], start=1):
        cols = line.split(",")
        if len(cols) <= max(model_idx, group_idx):
            continue
        row_model_bare = cols[model_idx].strip().rsplit(".", 1)[-1]
        row_group_bare = cols[group_idx].strip().rsplit(".", 1)[-1]
        if row_model_bare == model_bare and row_group_bare == group_bare:
            return idx, cols
    return None


def autofix_apply_resolved_access_row_changes(generated, goal: str, module_identity: str | None, db: str) -> None:
    """Deterministically rewrites security/ir.model.access.csv to match the
    resolved, validated access-row changes -- never guesses a group reference or
    silently drops the narrowing instruction the way freehand generation did live.
    No-op (never raises) when there's no access-row-change shape, no resolvable
    model, or no matching row to modify/add to.
    """
    if not module_identity:
        return
    extracted = extract_access_row_request(goal)
    if extracted is None:
        return
    resolved = resolve_access_row_request(extracted, module_identity, db)
    if not isinstance(resolved, ResolvedAccessRowChanges):
        return

    csv_text = getattr(generated, "security_csv", "") or ""
    if not csv_text.strip():
        return
    lines = csv_text.splitlines()
    header = [h.strip() for h in lines[0].split(",")]
    try:
        model_idx = header.index("model_id:id")
        group_idx = header.index("group_id:id")
        read_idx = header.index("perm_read")
        write_idx = header.index("perm_write")
        create_idx = header.index("perm_create")
        unlink_idx = header.index("perm_unlink")
    except ValueError:
        return
    model_bare = _model_bare(resolved.model_name)

    if resolved.narrow:
        found = _find_csv_row_for_group(csv_text, model_bare, resolved.narrow.group_ref)
        if found:
            idx, cols = found
            cols[read_idx] = "1" if resolved.narrow.perm_read else "0"
            cols[write_idx] = "1" if resolved.narrow.perm_write else "0"
            cols[create_idx] = "1" if resolved.narrow.perm_create else "0"
            cols[unlink_idx] = "1" if resolved.narrow.perm_unlink else "0"
            lines[idx] = ",".join(cols)

    if resolved.add:
        found = _find_csv_row_for_group(csv_text, model_bare, resolved.add.group_ref)
        if not found:
            new_row = [""] * len(header)
            id_idx = header.index("id") if "id" in header else None
            name_idx = header.index("name") if "name" in header else None
            group_bare = resolved.add.group_ref.rsplit(".", 1)[-1]
            if id_idx is not None:
                new_row[id_idx] = f"access_{model_bare}_{group_bare}"
            if name_idx is not None:
                new_row[name_idx] = f"access.{model_bare}.{group_bare}"
            new_row[model_idx] = model_bare
            new_row[group_idx] = resolved.add.group_ref
            new_row[read_idx] = "1" if resolved.add.perm_read else "0"
            new_row[write_idx] = "1" if resolved.add.perm_write else "0"
            new_row[create_idx] = "1" if resolved.add.perm_create else "0"
            new_row[unlink_idx] = "1" if resolved.add.perm_unlink else "0"
            lines.append(",".join(new_row))

    generated.security_csv = "\n".join(lines) + "\n"


_XMLID_LIKE_RE = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\b")


def validate_resolved_access_row_changes_are_applied(generated, goal: str, module_identity: str | None, db: str) -> None:
    """Hard-fails the round unless every resolved access-row change (narrow
    and/or add) is actually reflected in the final security_csv -- never fires
    when there's no access-row-change shape or nothing could be resolved."""
    if not module_identity:
        return
    extracted = extract_access_row_request(goal)
    if extracted is None:
        return
    resolved = resolve_access_row_request(extracted, module_identity, db)
    if not isinstance(resolved, ResolvedAccessRowChanges):
        return

    csv_text = getattr(generated, "security_csv", "") or ""
    model_bare = _model_bare(resolved.model_name)
    problems = []

    if resolved.narrow:
        found = _find_csv_row_for_group(csv_text, model_bare, resolved.narrow.group_ref)
        if not found:
            problems.append(f"no row found at all for group {resolved.narrow.group_ref!r} on {resolved.model_name!r} to narrow")
        else:
            _, cols = found
            header = [h.strip() for h in csv_text.splitlines()[0].split(",")]
            read_idx, write_idx = header.index("perm_read"), header.index("perm_write")
            create_idx, unlink_idx = header.index("perm_create"), header.index("perm_unlink")
            actual = (cols[read_idx].strip() == "1", cols[write_idx].strip() == "1",
                      cols[create_idx].strip() == "1", cols[unlink_idx].strip() == "1")
            expected = (resolved.narrow.perm_read, resolved.narrow.perm_write,
                        resolved.narrow.perm_create, resolved.narrow.perm_unlink)
            if actual != expected:
                problems.append(
                    f"row for group {resolved.narrow.group_ref!r} on {resolved.model_name!r} has "
                    f"permissions {actual}, expected {expected} (the goal's own narrowing instruction)"
                )

    if resolved.add:
        found = _find_csv_row_for_group(csv_text, model_bare, resolved.add.group_ref)
        if not found:
            problems.append(f"no row was added for CONFIRMED REAL group {resolved.add.group_ref!r} on {resolved.model_name!r}")

    if problems:
        raise ValueError(
            "the goal's access-row changes were resolved and validated against the live database, "
            "but the generated security/ir.model.access.csv does not correctly reflect them: "
            + "; ".join(problems)
            + ". Use the exact resolved group references and permission values -- do not invent "
              "different ones, and do not submit this round without applying every change."
        )
