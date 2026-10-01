"""Phase 36 §0.9a/§4.1 -- Build pre-write gate against the Odoo Knowledge
Graph's real, live :View/DECLARES_VIEW data.

Extracts every NEW primary (no inherit_id) `<record model="ir.ui.view">`
this round's own generated.views_xml/extra_data_files declares, and checks
each candidate's bare record id against tools_odoo.graph_queries.
get_view_id_collisions -- the real, fixed-parameterized Cypher query
(§4.2) built for exactly this purpose. A non-empty result means the graph
already shows this exact bare id ambiguously shared by two or more
DIFFERENT already-migrated modules; this round declaring the same id in a
THIRD (or reinforcing an existing) module would only compound that
ambiguity, so it hard-fails the write, same discipline as every other
validator in this pre-write chain (specialists/build/specialist.py's
_validate_generated_module()).

Known, deliberate limitation (flagged rather than silently assumed away):
get_view_id_collisions only detects an id that is ALREADY ambiguous
between two or more EXISTING graph modules -- it does not check "does
this brand-new candidate id already belong to exactly one other single
existing module" (a simpler, different query graph_queries.py does not
currently expose). Extending the query layer for that narrower case is
future work, not done here, since this task's scope is wiring the
existing, already-tested query into Build, not redesigning it.

Fail-open posture (§4.3): any Neo4j connectivity/timeout error is caught
and this validator no-ops for the round rather than blocking a build on
an unrelated graph-service outage -- the same posture every live-registry
validator in specialist.py already uses (e.g.
_validate_many2one_comodel_target_exists's `real_fields is None ->
continue`). A genuine, confirmed collision is the only thing that raises.
"""

from __future__ import annotations

import asyncio
import re

_RECORD_RE = re.compile(r"<record\b([^>]*)>(.*?)</record>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_INHERIT_ID_FIELD_RE = re.compile(r'<field\s+name="inherit_id"')


def find_new_primary_view_ids(generated) -> list[str]:
    """Every bare record id of a NEW `<record model="ir.ui.view">` this
    round declares with no `<field name="inherit_id">` -- i.e. a primary
    view declaration, not an inherited-view extension. Scans both
    views_xml and any extra_data_files (mirrors the scanning scope
    wizard_structural_gate.py's window-action check already uses)."""
    views_xml = getattr(generated, "views_xml", "") or ""
    extra_data_files = getattr(generated, "extra_data_files", None) or {}
    candidate_ids: list[str] = []
    for content in (views_xml, *extra_data_files.values()):
        if not content:
            continue
        for record_match in _RECORD_RE.finditer(content):
            attrs = dict(_ATTR_RE.findall(record_match.group(1)))
            if attrs.get("model") != "ir.ui.view":
                continue
            record_id = attrs.get("id")
            if not record_id:
                continue
            if _INHERIT_ID_FIELD_RE.search(record_match.group(2)):
                continue  # inherited view, not a primary declaration
            candidate_ids.append(record_id)
    return candidate_ids


async def validate_new_primary_view_ids_dont_collide_with_graph(generated, task_id: str | None = None) -> None:
    candidate_ids = find_new_primary_view_ids(generated)
    if not candidate_ids:
        return

    try:
        from infra.neo4j_client import get_neo4j_read_driver
        from tools_odoo.graph_queries import get_view_id_collisions

        driver = get_neo4j_read_driver()
    except Exception:  # noqa: BLE001 -- fail-open, see module docstring
        return

    for candidate_id in candidate_ids:
        try:
            in_progress, collisions = await asyncio.to_thread(get_view_id_collisions, driver, candidate_id)
        except Exception:  # noqa: BLE001 -- fail-open, see module docstring
            continue
        if in_progress or not collisions:
            continue
        modules = sorted({c["module_a"] for c in collisions} | {c["module_b"] for c in collisions})
        raise ValueError(
            f"view id {candidate_id!r} (a new primary <record model=\"ir.ui.view\"> this round "
            f"declares) is already ambiguously shared by existing modules {modules} in the Odoo "
            f"Knowledge Graph -- declaring it again would compound a real xml_id collision. "
            f"Rename this view's id to something unique."
        )


async def validate_no_blast_radius_collisions_or_breakage(
    generated, old_models_py: str, module_name: str, task_id: str | None = None
) -> None:
    """Phase 35 §12.2 / §13.3 (2026-08-13) -- the field-level sibling of
    validate_new_primary_view_ids_dont_collide_with_graph above, same
    fail-open-except-on-a-confirmed-real-finding discipline. Computed
    entirely from THIS round's own old-vs-new models.py diff (never from
    the graph's own freshness for this module, which the async
    incremental-sync hook only starts, never guarantees complete by write
    time) -- only OTHER, already-synced modules' graph data is ever read.

    Two real, confirmed failure classes, both named directly in §12's own
    motivating incidents:
    1. A field this round just ADDED that another real module ALREADY
       declares on the same model (crm.lead/hr.leave duplicate-field
       collisions).
    2. A field this round just REMOVED that another real module's VIEW
       still references (the crm.lead incident: 19+ modules silently
       broken by one field removal, invisible until a later, unrelated
       install forced Odoo to revalidate the whole registry).
    """
    new_models_py = getattr(generated, "models_py", "") or ""
    if not new_models_py and not old_models_py:
        return

    try:
        from tools_odoo.knowledge_graph.build_safety_grounding import (
            check_blast_radius_field_collisions, check_removed_field_still_referenced,
            extract_field_names_for_model, extract_touched_model_names,
        )
    except Exception:  # noqa: BLE001 -- fail-open, see module docstring
        return

    touched_models = extract_touched_model_names(new_models_py) | extract_touched_model_names(old_models_py)
    for model in sorted(touched_models):
        try:
            old_fields = extract_field_names_for_model(old_models_py, model)
            new_fields = extract_field_names_for_model(new_models_py, model)
        except Exception:  # noqa: BLE001 -- fail-open, see module docstring
            continue
        added = new_fields - old_fields
        removed = old_fields - new_fields

        try:
            added_findings = await asyncio.to_thread(
                check_blast_radius_field_collisions, module_name, model, added
            )
        except Exception:  # noqa: BLE001 -- fail-open, see module docstring
            added_findings = []
        try:
            removed_findings = await asyncio.to_thread(
                check_removed_field_still_referenced, module_name, model, removed
            )
        except Exception:  # noqa: BLE001 -- fail-open, see module docstring
            removed_findings = []

        findings = added_findings + removed_findings
        if findings:
            raise ValueError(
                "Odoo Knowledge Graph blast-radius check found real, confirmed issue(s) with "
                f"this round's own field changes to {model!r}: " + "; ".join(findings)
            )
