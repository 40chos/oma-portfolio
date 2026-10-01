"""Postgres-backed CRUD over agent_templates (Component 1, build plan
§24.4/§24.5). Mirrors manager/tools.py's own connection pattern
(load_postgres_settings() -> psycopg2.connect, autocommit for writes)
rather than inventing a new one -- this is a different table, not
agent_memory_events, so the "append_project_memory() is the only
INSERT path" rule (manager/tools.py's own docstring) does not apply
here; that rule is scoped specifically to the insert-only historical
log, not every table in this database.
"""

from __future__ import annotations

import json

import psycopg2
import psycopg2.extras

from infra.settings import load_postgres_settings
from templates.schema import (
    ApplicabilitySignature,
    CodeExample,
    ConstraintSlot,
    Template,
    TemplateLevel,
    TemplateStatus,
)


def _get_conn():
    s = load_postgres_settings()
    return psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)


def _row_to_template(row: dict) -> Template:
    sig = row["applicability_signature"]
    return Template(
        template_id=row["template_id"],
        version=row["version"],
        created_from=row["created_from"],
        level=TemplateLevel(row["level"]),
        applicability_signature=ApplicabilitySignature(
            task_shape=sig["task_shape"],
            odoo_module_area=sig["odoo_module_area"],
            scope_class=sig.get("scope_class", "single_file"),
        ),
        constraint_template=list(row["constraint_template"]),
        slots=[ConstraintSlot(**s) for s in row["slots"]],
        known_pitfalls=list(row["known_pitfalls"]),
        code_examples=[CodeExample(**c) for c in row["code_examples"]],
        composed_from=list(row["composed_from"] or []),
        status=TemplateStatus(row["status"]),
        success_count=row["success_count"],
        fallback_override_count=row["fallback_override_count"],
        last_used_at=row["last_used_at"],
        created_at=row["created_at"],
    )


def insert_template(t: Template) -> None:
    """Inserts a new template row. Raises on a duplicate template_id --
    callers (harvest.py) are responsible for generating a fresh id per
    template, never reusing one.
    """
    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_templates
                (template_id, version, created_from, level,
                 applicability_signature, constraint_template, slots,
                 known_pitfalls, code_examples, composed_from, status,
                 success_count, fallback_override_count, last_used_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                t.template_id, t.version, t.created_from, int(t.level),
                psycopg2.extras.Json(t.applicability_signature.as_dict()),
                psycopg2.extras.Json(list(t.constraint_template)),
                psycopg2.extras.Json([{"name": s.name, "example_value": s.example_value} for s in t.slots]),
                psycopg2.extras.Json(list(t.known_pitfalls)),
                psycopg2.extras.Json([
                    {"field_name": c.field_name, "field_type": c.field_type,
                     "source_task_id": c.source_task_id, "models_py": c.models_py,
                     "views_xml": c.views_xml}
                    for c in t.code_examples
                ]),
                t.composed_from,
                t.status.value, t.success_count, t.fallback_override_count, t.last_used_at,
            ),
        )
        cur.close()
    finally:
        conn.close()


def get_template(template_id: str) -> Template | None:
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agent_templates WHERE template_id = %s", (template_id,))
        row = cur.fetchone()
        cur.close()
        return _row_to_template(dict(row)) if row else None
    finally:
        conn.close()


def list_templates(
    status: str | None = None,
    level: int | None = None,
    odoo_module_area: str | None = None,
) -> list[Template]:
    """Read-back for the saturation checker and any future classifier
    narrowing stage. Filters are all optional AND-combined; passing
    none returns the whole library (fine at this table's expected
    eventual size, low hundreds of rows per §24.1's own ~150 estimate).
    """
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        clauses = []
        params: list = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if level is not None:
            clauses.append("level = %s")
            params.append(level)
        if odoo_module_area is not None:
            clauses.append("applicability_signature->>'odoo_module_area' = %s")
            params.append(odoo_module_area)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cur.execute(f"SELECT * FROM agent_templates {where} ORDER BY created_at ASC", params)
        rows = cur.fetchall()
        cur.close()
        return [_row_to_template(dict(r)) for r in rows]
    finally:
        conn.close()


def set_status(template_id: str, status: TemplateStatus) -> None:
    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agent_templates SET status = %s WHERE template_id = %s",
            (status.value, template_id),
        )
        cur.close()
    finally:
        conn.close()


def record_usage(template_id: str, matched_but_overridden: bool) -> None:
    """Called once a template-matched task resolves (Component 2/3, not
    built yet -- but the counters this feeds, success_count/
    fallback_override_count, are exactly what §24.7's retire signal and
    §24.11.5's promotion margin both read, so the write path exists now
    even though nothing calls it live yet).
    """
    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        if matched_but_overridden:
            cur.execute(
                "UPDATE agent_templates SET fallback_override_count = fallback_override_count + 1, "
                "last_used_at = now() WHERE template_id = %s",
                (template_id,),
            )
        else:
            cur.execute(
                "UPDATE agent_templates SET success_count = success_count + 1, "
                "last_used_at = now() WHERE template_id = %s",
                (template_id,),
            )
        cur.close()
    finally:
        conn.close()


def delete_template(template_id: str) -> None:
    """Test/cleanup helper -- not part of the normal harvest/promote/
    retire lifecycle (Component 5's merge/prune uses set_status(...,
    RETIRED), never a hard delete, so retired templates stay auditable).
    """
    conn = _get_conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM agent_templates WHERE template_id = %s", (template_id,))
        cur.close()
    finally:
        conn.close()
