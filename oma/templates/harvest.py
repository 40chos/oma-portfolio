"""Component 4 (build plan §24.4/§24.7): harvest-on-success. Reads
agent_memory_events (SELECT only -- never writes there directly; the
one 'template_harvested' event this module does write goes through
manager.tools.append_project_memory(), the enforced single insert
path) and turns verified, successfully-completed real tasks into
Level-0/Level-1 templates in agent_templates.

This is deliberately a batch job run by hand or on a schedule, not a
live hook on task completion -- matches §24.7's own framing ("harvest
trigger: any task that completes... is a harvest candidate", extracted
from already-durable history, not captured inline during the run).

Scope for this first real run (2026-07-15): the "add a field to a
model" task shape from Area 1 + its regression-check re-run, matched by
goal-text pattern rather than a dedicated task_shape column on
task_created (which doesn't exist yet -- adding one is a natural later
refinement once a second shape needs harvesting, not needed for this
first shape). Tasks whose goal text doesn't match a known pattern are
silently skipped, not errored -- this module intentionally does not
try to be a general goal-text classifier; that job belongs to the
two-stage classifier (Component 2), not the harvester.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass

import psycopg2
import psycopg2.extras

from infra.settings import load_postgres_settings
from manager.tools import append_project_memory
from templates.schema import (
    ApplicabilitySignature,
    CodeExample,
    ConstraintSlot,
    Template,
    TemplateLevel,
    TemplateStatus,
)
from templates.store import insert_template, list_templates
from tools_odoo.module_dev.vcs import VcsError, read_last_validated_commit

# §24.11.5's margin rule: a cluster verified across >=2 independent real
# task runs auto-promotes to ACTIVE; a single verified instance queues
# as NEEDS_REVIEW (Operator's batched digest, per that section -- this
# module only sets the status; the actual digest surfacing is Phase 19
# UI wiring, unchanged by this harvest run).
_AUTO_PROMOTE_MIN_INSTANCES = 2

_SINGLE_FIELD_RE = re.compile(
    r"adds? a single new field (\w+) \(([^)]+)\) to the ([\w.]+) model"
)
_MULTI_FIELD_RE = re.compile(
    r"adds? two new fields to the ([\w.]+) model: (\w+) \(([^)]+)\) and (\w+) \(([^)]+)\)"
)


@dataclass
class HarvestedInstance:
    task_id: str
    goal: str
    model_name: str
    field_specs: list[tuple[str, str]]  # [(field_name, field_type), ...]


def _get_conn():
    s = load_postgres_settings()
    return psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)


def _parse_goal(goal: str) -> tuple[str, list[tuple[str, str]]] | None:
    """Returns (model_name, [(field_name, field_type), ...]) if this
    goal matches a known "add a field to a model" shape, else None.
    """
    m = _SINGLE_FIELD_RE.search(goal)
    if m:
        field_name, field_type, model_name = m.group(1), m.group(2), m.group(3)
        return model_name, [(field_name, field_type)]
    m = _MULTI_FIELD_RE.search(goal)
    if m:
        model_name = m.group(1)
        return model_name, [(m.group(2), m.group(3)), (m.group(4), m.group(5))]
    return None


def fetch_verified_field_add_instances() -> list[HarvestedInstance]:
    """Real, durable query -- no re-run needed (confirmed live 2026-07-15
    against odoo_manager_dev: full round/outcome history survives across
    session and service restarts, since agent_memory_events is a plain
    Postgres table, not an in-memory or TTL'd store).
    """
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT tc.task_id, tc.summary AS goal
            FROM agent_memory_events tc
            JOIN agent_memory_events o
                ON o.task_id = tc.task_id AND o.event_type = 'outcome'
            WHERE tc.event_type = 'task_created'
              AND o.verified = true
              AND (o.detail->>'passed')::boolean = true
            """
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    instances = []
    for row in rows:
        parsed = _parse_goal(row["goal"])
        if parsed is None:
            continue
        model_name, field_specs = parsed
        instances.append(
            HarvestedInstance(
                task_id=str(row["task_id"]), goal=row["goal"],
                model_name=model_name, field_specs=field_specs,
            )
        )
    return instances


def fetch_known_pitfalls_for_task_ids(task_ids: list[str]) -> list[str]:
    """Pulls real revision_reasoning text from any replan_round events
    belonging to these tasks -- this is what lets a harvested template
    carry forward the ACTUAL failure signatures seen for this cluster
    (e.g. hr.employee's real "Install failed (rc=255)" near-miss
    history), not a guessed or generic pitfall list.
    """
    if not task_ids:
        return []
    conn = _get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT DISTINCT detail->>'revision_reasoning' AS reason
            FROM agent_memory_events
            WHERE event_type = 'replan_round' AND task_id = ANY(%s::uuid[])
              AND detail->>'revision_reasoning' IS NOT NULL
            """,
            (task_ids,),
        )
        rows = cur.fetchall()
        cur.close()
        return [r["reason"] for r in rows if r["reason"]]
    finally:
        conn.close()


def _build_code_example(inst: "HarvestedInstance", field_name: str, field_type: str) -> CodeExample | None:
    """Pulls the real, verified working code for one instance from its
    source task's own last validated Gitea commit -- never invented,
    never templated from a guess (§24.14.7's decision: real code in the
    same Postgres row, no separate git-backed content store). Returns
    None (not a placeholder) if the branch is genuinely unreachable or
    the expected files aren't on it -- a missing example is honest;
    a fabricated one is not.
    """
    try:
        files = read_last_validated_commit(inst.task_id)
    except VcsError:
        return None
    if not files:
        return None
    models_py = next((content for path, content in files.items() if path.endswith("models/models.py")), None)
    views_xml = next((content for path, content in files.items() if path.endswith("views/views.xml")), None)
    if models_py is None:
        return None
    return CodeExample(
        field_name=field_name, field_type=field_type, source_task_id=inst.task_id,
        models_py=models_py, views_xml=views_xml,
    )


def harvest_field_add_templates() -> dict:
    """Runs the full Component 4 pass for the "add a field to a model"
    shape: clusters verified instances by target model, harvests one
    Level-0 template per model for the single-field shape, one Level-1
    template per model (composed from that Level-0 template) for the
    two-field shape wherever we actually saw one. Idempotent per model:
    skips a cluster whose Level-0/Level-1 template (matched by
    applicability_signature) already exists in the store, so re-running
    this after new tasks land only adds what's new.
    """
    instances = fetch_verified_field_add_instances()

    by_model_single: dict[str, list[HarvestedInstance]] = defaultdict(list)
    by_model_multi: dict[str, list[HarvestedInstance]] = defaultdict(list)
    for inst in instances:
        if len(inst.field_specs) == 1:
            by_model_single[inst.model_name].append(inst)
        else:
            by_model_multi[inst.model_name].append(inst)

    existing = list_templates()
    existing_signatures = {
        (t.applicability_signature.task_shape, t.applicability_signature.odoo_module_area, int(t.level))
        for t in existing
    }

    created: list[Template] = []

    # Level 0: one template per model, from the single-field instances.
    level0_id_by_model: dict[str, str] = {t.applicability_signature.odoo_module_area: t.template_id
                                           for t in existing
                                           if t.applicability_signature.task_shape == "add_field_to_model"
                                           and int(t.level) == 0}
    for model_name, insts in by_model_single.items():
        sig_key = ("add_field_to_model", model_name, 0)
        if sig_key in existing_signatures:
            continue
        task_ids = [i.task_id for i in insts]
        field_types_seen = sorted({ft for i in insts for _, ft in i.field_specs})
        pitfalls = fetch_known_pitfalls_for_task_ids(task_ids)

        # One representative instance per distinct field type actually
        # verified -- not just the first instance overall (the gap
        # found 2026-07-15, see §24.14.7).
        representative_by_type: dict[str, HarvestedInstance] = {}
        for inst in insts:
            field_name, field_type = inst.field_specs[0]
            representative_by_type.setdefault(field_type, inst)
        code_examples = []
        for field_type, inst in representative_by_type.items():
            field_name, _ = inst.field_specs[0]
            example = _build_code_example(inst, field_name, field_type)
            if example is not None:
                code_examples.append(example)

        template_id = uuid.uuid4().hex
        status = TemplateStatus.ACTIVE if len(insts) >= _AUTO_PROMOTE_MIN_INSTANCES else TemplateStatus.NEEDS_REVIEW
        t = Template(
            template_id=template_id,
            version=1,
            created_from=task_ids[0],
            level=TemplateLevel.ATOMIC_SKILL,
            applicability_signature=ApplicabilitySignature(
                task_shape="add_field_to_model", odoo_module_area=model_name, scope_class="single_file",
            ),
            constraint_template=[
                "module_installs_cleanly",
                f"field_exists_on_{model_name.replace('.', '_')}",
                "field_exposed_in_form_view",
            ],
            slots=[
                ConstraintSlot(name="field_name", example_value=insts[0].field_specs[0][0]),
                ConstraintSlot(name="field_type", example_value=insts[0].field_specs[0][1]),
            ],
            known_pitfalls=pitfalls,
            code_examples=code_examples,
            composed_from=[],
            status=status,
            success_count=len(insts),
        )
        insert_template(t)
        append_project_memory(
            event_type="template_harvested", actor="manager",
            summary=f"Harvested Level-0 template for 'add a field to {model_name}' "
                    f"({len(insts)} verified instance(s), field types seen: {field_types_seen}, "
                    f"{len(code_examples)} real code example(s) attached)",
            task_id=task_ids[0], module=model_name,
            detail={"template_id": template_id, "level": 0, "instance_task_ids": task_ids},
        )
        created.append(t)
        level0_id_by_model[model_name] = template_id
        existing_signatures.add(sig_key)

    # Level 1: one template per model, from the two-field instances --
    # each is genuinely a small pipeline of two Level-0 "add a field"
    # skills against the same model, per §24.12.1's own definition, not
    # a separate atomic skill of its own.
    for model_name, insts in by_model_multi.items():
        sig_key = ("add_two_fields_to_model", model_name, 1)
        if sig_key in existing_signatures:
            continue
        level0_id = level0_id_by_model.get(model_name)
        if level0_id is None:
            # No verified Level-0 single-field instance for this model yet
            # -- per §24.12.2, a Level-1 template must be assembled from
            # an already-verified Level-0 piece, so this cluster is
            # skipped until one exists (it will, next harvest run, if a
            # single-field task for this model is ever run).
            continue
        task_ids = [i.task_id for i in insts]
        pitfalls = fetch_known_pitfalls_for_task_ids(task_ids)

        # One representative real code example for the two-field combo
        # actually verified (only one distinct combo per model exists
        # in today's data, so this is a single lookup, not a per-type
        # loop like the Level-0 case above).
        first = insts[0]
        combo_name = f"{first.field_specs[0][0]}+{first.field_specs[1][0]}"
        combo_type = f"{first.field_specs[0][1]}+{first.field_specs[1][1]}"
        combo_example = _build_code_example(first, combo_name, combo_type)
        code_examples = [combo_example] if combo_example is not None else []

        template_id = uuid.uuid4().hex
        status = TemplateStatus.ACTIVE if len(insts) >= _AUTO_PROMOTE_MIN_INSTANCES else TemplateStatus.NEEDS_REVIEW
        t = Template(
            template_id=template_id,
            version=1,
            created_from=task_ids[0],
            level=TemplateLevel.PIPELINE,
            applicability_signature=ApplicabilitySignature(
                task_shape="add_two_fields_to_model", odoo_module_area=model_name, scope_class="single_file",
            ),
            constraint_template=[
                "module_installs_cleanly",
                f"both_fields_exist_on_{model_name.replace('.', '_')}",
                "both_fields_exposed_in_form_view",
            ],
            slots=[
                ConstraintSlot(name="field_1_name", example_value=insts[0].field_specs[0][0]),
                ConstraintSlot(name="field_1_type", example_value=insts[0].field_specs[0][1]),
                ConstraintSlot(name="field_2_name", example_value=insts[0].field_specs[1][0]),
                ConstraintSlot(name="field_2_type", example_value=insts[0].field_specs[1][1]),
            ],
            known_pitfalls=pitfalls,
            code_examples=code_examples,
            composed_from=[level0_id],
            status=status,
            success_count=len(insts),
        )
        insert_template(t)
        append_project_memory(
            event_type="template_harvested", actor="manager",
            summary=f"Harvested Level-1 pipeline template for 'add two fields to {model_name}' "
                    f"({len(insts)} verified instance(s)), composed from Level-0 template {level0_id}",
            task_id=task_ids[0], module=model_name,
            detail={"template_id": template_id, "level": 1, "instance_task_ids": task_ids,
                    "composed_from": [level0_id]},
        )
        created.append(t)
        existing_signatures.add(sig_key)

    return {
        "instances_seen": len(instances),
        "models_with_single_field_instances": sorted(by_model_single.keys()),
        "models_with_two_field_instances": sorted(by_model_multi.keys()),
        "templates_created": len(created),
        "templates_created_ids": [t.template_id for t in created],
    }


if __name__ == "__main__":
    import json as _json
    result = harvest_field_add_templates()
    print(_json.dumps(result, indent=2))
