"""Odoo Knowledge Graph -- fixed, parameterized Cypher query functions (Phase 36 §4.2).

Per the llm_graph_query_patterns research cited in §4.2, this module is a small set of
plain Python functions, each wrapping ONE fixed, tested Cypher query with driver-level
parameters (never string-interpolated / never LLM-assembled Cypher). Every function:

  * opens its session in READ access mode via infra.neo4j_client.get_neo4j_read_driver()
    (§3.3's server-enforced misuse guard for Community Edition's lack of custom roles);
  * wraps its Cypher text in neo4j.Query(text, timeout=settings.query_timeout_s) so the
    server-side transaction timeout (§3.2) actually applies -- session.run(query, timeout=...)
    is NOT a real keyword accepted by the installed neo4j driver (confirmed by direct
    inspection of neo4j.Session.run's signature during implementation: unrecognized kwargs
    passed to session.run() are merged into the query PARAMETERS, not transaction config --
    the real mechanism is the neo4j.Query wrapper class's own `timeout=` argument);
  * issues exactly ONE Cypher statement per call, with the :ImportMetadata.import_in_progress
    check as the leading clause of the SAME statement as the substantive MATCH (the
    `CALL { WITH in_progress WHERE NOT in_progress ... }` pattern, §2.2 step 4 / §4.2), so
    there is no second network round-trip and no TOCTOU window between the check and the
    read -- Neo4j evaluates a single Cypher statement against one consistent view of the
    graph for its whole execution;
  * returns a (in_progress: bool, data: ...) pair from that one round-trip and never raises
    on "the graph legitimately has nothing to say" -- it DOES let real driver/connectivity
    exceptions (ServiceUnavailable, AuthError, transaction-timeout errors, ...) propagate to
    the caller uncaught. Catching those and degrading to fail-open ("" / empty context) is
    resolve_current_graph_context_block's / graph_grounding_gate.py's job (§4.1, §4.3), not
    this module's -- a query function here has no way to know whether its caller wants
    fail-open degradation (Build's runtime path) or a hard failure (a diagnostic CLI, a
    structural-risk report script that should exit non-zero on a real outage).

Deliberate implementation simplification, noted explicitly rather than left implicit:
get_field_impact_analysis's three OPTIONAL MATCH branches (extending modules / dependent
modules / access groups) run in one flat scope rather than three nested CALL {} subqueries,
so they form a cartesian join before the final collect(DISTINCT ...) per column. This is
correct (DISTINCT still yields the right set per column) but not maximally efficient for a
model with very large fan-out on all three axes simultaneously. Given this graph's real size
(240 modules, ~6,836 fields, per §2.1/source-data), this is not expected to matter in
practice; a future optimization pass could split it into three correlated CALL {} subqueries
if a specific model is ever shown to be slow.

Phase 36 §4.5 addition (seventh-audit revision, 2026-08-13): get_ungated_models, the one new
query needed by the security-posture consumer -- a fixed-parameterized read against the
:AccessGroup/RESTRICTED_TO/has_ungated_access_rule schema §2.1's sixth-audit revision loaded
but which had zero operational consumer until this revision. Same fail-open, single-round-trip,
READ-mode discipline as every other function in this module.

Phase 36 §13 addition (2026-08-13): get_field_dependency_chain, get_blast_radius,
get_module_grounding are all real, additional fixed READs, same discipline as everything
above. update_module_install_state is DIFFERENT and is called out explicitly here because it
breaks this module's own stated "every function opens READ access mode" convention above: it
is the ONE deliberately write-capable function in this file (live install-state tracking,
§13.2/§13.4) -- see its own docstring for why it exists here rather than in a separate
write-only module (this module is still the single, tested home for every Odoo-KG Cypher
query/write this codebase issues; splitting one write function out into its own file for
purity would cost more than it buys) and why it is deliberately NOT gated on
`NOT import_in_progress` the way every read above is.
"""

from __future__ import annotations

from neo4j import READ_ACCESS, Driver, Query

from infra.settings import load_neo4j_settings

_IMPORT_METADATA_ID = "odoo_full_module_graph"


def _run_gated_read(driver: Driver, cypher: str, params: dict) -> "neo4j.Record | None":  # noqa: F821
    """Run one atomic, :ImportMetadata-gated, READ-mode, timeout-bounded Cypher
    statement and return its single result record (or None if the query
    produced no rows at all -- not expected for any function below, since
    each one's final RETURN is an aggregate with no grouping key, which
    always yields exactly one row even over zero matched input rows; see
    each function's own query for why that guarantee holds here).
    """
    settings = load_neo4j_settings()
    query = Query(cypher, timeout=settings.query_timeout_s)
    with driver.session(database=settings.database, default_access_mode=READ_ACCESS) as session:
        result = session.run(query, params)
        return result.single()


def get_field_types_for_model(driver: Driver, technical_name: str) -> tuple[bool, list[dict]]:
    """Every field (name/ttype/required/computed/relation_target/relation_kind) HAS_FIELD-owned
    by the given model. §2.1 (`:Field.ttype`), §4.2's own worked example, §4.4 (Code-Review's
    `_filter_hallucinated_direct_field_missing_findings` consumer).
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (m:Model {technical_name: $technical_name})-[:HAS_FIELD]->(f:Field)
      RETURN collect({name: f.name, ttype: f.ttype, required: f.required,
                       computed: f.computed, relation_target: f.relation_target,
                       relation_kind: f.relation_kind}) AS fields
    }
    RETURN in_progress, fields
    """
    record = _run_gated_read(
        driver,
        cypher,
        {"import_metadata_id": _IMPORT_METADATA_ID, "technical_name": technical_name},
    )
    if record is None:
        return False, []
    return bool(record["in_progress"]), list(record["fields"] or [])


def get_model_existence(driver: Driver, technical_name: str) -> tuple[bool, dict | None]:
    """Phase 35 §17.2.1: the real query behind the task-intake existence/dedup check --
    "does a :Model with this exact technical_name already exist, and if so, which real
    module defines/reopens it and how many fields does it already have." Distinct from
    get_module_grounding() above (which answers "what does THIS NAMED MODULE declare") --
    this answers "does a model with THIS NAME exist at all, anywhere," the question intake
    needs answered before a module name is even chosen.

    Returns (in_progress, None) if no such model exists (or the graph is mid-import);
    (False, {"technical_name": ..., "defining_modules": [...], "field_count": int}) on a
    real hit -- `defining_modules` covers both DEFINES and REOPENS, since either one means
    "this model concept already exists for real," the distinction OMA's own downstream
    consumers don't need to make at intake time.
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      OPTIONAL MATCH (m:Model {technical_name: $technical_name})
      OPTIONAL MATCH (definer:Module)-[:DEFINES]->(m)
      OPTIONAL MATCH (reopener:Module)-[:REOPENS]->(m)
      OPTIONAL MATCH (m)-[:HAS_FIELD]->(f:Field)
      RETURN
        m.technical_name AS found_name,
        [x IN collect(DISTINCT definer.name) WHERE x IS NOT NULL] +
        [x IN collect(DISTINCT reopener.name) WHERE x IS NOT NULL] AS defining_modules,
        count(DISTINCT f) AS field_count
    }
    RETURN in_progress, found_name, defining_modules, field_count
    """
    record = _run_gated_read(
        driver, cypher, {"import_metadata_id": _IMPORT_METADATA_ID, "technical_name": technical_name},
    )
    if record is None:
        return False, None
    in_progress = bool(record["in_progress"])
    if in_progress or not record["found_name"]:
        return in_progress, None
    return False, {
        "technical_name": record["found_name"],
        "defining_modules": sorted(set(record["defining_modules"] or [])),
        "field_count": int(record["field_count"] or 0),
    }


def get_module_dependency_closure(driver: Driver, module_name: str) -> tuple[bool, set[str]]:
    """Full transitive DEPENDS_ON closure of the given module (every module it depends on,
    directly or indirectly) -- §7 item 1's "what breaks if I remove this module" use case and
    the dependent-module leg of get_field_impact_analysis below.
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (m:Module {name: $module_name})-[:DEPENDS_ON*1..]->(dep:Module)
      RETURN collect(DISTINCT dep.name) AS deps
    }
    RETURN in_progress, deps
    """
    record = _run_gated_read(
        driver,
        cypher,
        {"import_metadata_id": _IMPORT_METADATA_ID, "module_name": module_name},
    )
    if record is None:
        return False, set()
    return bool(record["in_progress"]), set(record["deps"] or [])


def get_field_impact_analysis(driver: Driver, model: str, field_name: str) -> tuple[bool, dict]:
    """"What breaks if I change/remove this field" (§7 item 1): the union of

      * modules that EXTENDS_FIELD this exact field,
      * modules that transitively DEPENDS_ON this model's owner_module (i.e. any module that
        could be relying on the model this field lives on being present at all), and
      * AccessGroups with a RESTRICTED_TO rule scoped to this model (who can currently access
        it at all, independent of this specific field).

    Returned data dict shape: {"extending_modules": [str, ...], "dependent_modules": [str, ...],
    "access_groups": [{"group": xml_id, "perms": perms_string}, ...]}.
    """
    field_key = f"{model}.{field_name}"
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      OPTIONAL MATCH (target:Model {technical_name: $model})
      OPTIONAL MATCH (extending:Module)-[:EXTENDS_FIELD]->(:Field {key: $field_key})
      OPTIONAL MATCH (owner:Module)-[:DEFINES]->(target)
      OPTIONAL MATCH (dependent:Module)-[:DEPENDS_ON*1..]->(owner)
      OPTIONAL MATCH (target)-[r:RESTRICTED_TO]->(g:AccessGroup)
      RETURN
        [x IN collect(DISTINCT extending.name) WHERE x IS NOT NULL] AS extending_modules,
        [x IN collect(DISTINCT dependent.name) WHERE x IS NOT NULL] AS dependent_modules,
        [x IN collect(DISTINCT {group: g.xml_id, perms: r.perms}) WHERE x.group IS NOT NULL]
          AS access_groups
    }
    RETURN in_progress, extending_modules, dependent_modules, access_groups
    """
    record = _run_gated_read(
        driver,
        cypher,
        {
            "import_metadata_id": _IMPORT_METADATA_ID,
            "model": model,
            "field_key": field_key,
        },
    )
    if record is None:
        return False, {"extending_modules": [], "dependent_modules": [], "access_groups": []}
    return bool(record["in_progress"]), {
        "extending_modules": list(record["extending_modules"] or []),
        "dependent_modules": list(record["dependent_modules"] or []),
        "access_groups": [dict(g) for g in (record["access_groups"] or [])],
    }


def get_view_id_collisions(driver: Driver, candidate_xml_id: str) -> tuple[bool, list[dict]]:
    """§0.9a's direct fix for the self-inheriting-view incident: two DIFFERENT modules each
    DECLARES_VIEW-ing a primary (no inherit_id) view under the same fully-qualified xml_id.
    Wired into Build's pre-write gate (§0.9a) before committing a new
    `<record id="X" model="ir.ui.view">` with no inherit_id -- a non-empty result blocks the
    write. Returned list entries: {"module_a": str, "module_b": str, "xml_id": str}, one per
    colliding pair (module_a < module_b lexicographically, so each real collision appears
    exactly once, not twice for (m1, m2) and (m2, m1)).
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (m1:Module)-[:DECLARES_VIEW]->(v:View {xml_id: $candidate_xml_id})<-[:DECLARES_VIEW]-(m2:Module)
      WHERE m1.name < m2.name
      RETURN collect({module_a: m1.name, module_b: m2.name, xml_id: v.xml_id}) AS collisions
    }
    RETURN in_progress, collisions
    """
    record = _run_gated_read(
        driver,
        cypher,
        {"import_metadata_id": _IMPORT_METADATA_ID, "candidate_xml_id": candidate_xml_id},
    )
    if record is None:
        return False, []
    return bool(record["in_progress"]), list(record["collisions"] or [])


def get_orphaned_view_field_refs(driver: Driver, model_name: str) -> tuple[bool, list[dict]]:
    """§0.9b's cross-module drift check: a View's own REFERENCES_FIELD edge (resolved at ETL
    time against that model's fields, per §0.9b) pointing at a :Field node that the model no
    longer HAS_FIELD -- e.g. a later incremental sync (§0.9c) removed the field from the
    model's own schema without the referencing view (from a DIFFERENT, untouched module) ever
    being re-checked. This is deliberately a live re-derivation against the graph's CURRENT
    :HAS_FIELD state, not a read of the ETL-time-only :UnresolvedItem snapshot (§0.9b's own
    "placeholder form -- real query is the inverse" note): a field ref that resolved cleanly
    at import time can still go stale later purely from a different module's incremental sync,
    which is exactly the whole-graph-drift case §0.9b calls out as its reason for existing as
    a periodic report (`scripts/odoo_kg_structural_risk_report.py`) rather than only a
    pre-write gate. Returned list entries: {"view_xml_id": str, "field_name": str}.
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (v:View)-[:REFERENCES_FIELD]->(f:Field {model: $model_name})
      WHERE NOT (:Model {technical_name: $model_name})-[:HAS_FIELD]->(f)
      RETURN collect({view_xml_id: v.xml_id, field_name: f.name}) AS orphaned
    }
    RETURN in_progress, orphaned
    """
    record = _run_gated_read(
        driver,
        cypher,
        {"import_metadata_id": _IMPORT_METADATA_ID, "model_name": model_name},
    )
    if record is None:
        return False, []
    return bool(record["in_progress"]), list(record["orphaned"] or [])


def get_views_referencing_field_key(driver: Driver, field_key: str) -> tuple[bool, list[dict]]:
    """Phase 35 SS12.2 item 3 / SS13.3 (2026-08-13) -- real, pre-write-safe
    sibling of get_orphaned_view_field_refs(): that function re-derives
    orphaned refs against the model's CURRENT :HAS_FIELD state, which
    depends on the graph already having this round's own removal synced
    (a freshness assumption this function deliberately avoids). This one
    instead answers a narrower, freshness-independent question directly:
    "which real views, in which real modules, currently reference this
    EXACT field key" -- callable BEFORE a round's own field removal has
    reached the graph at all, since it only reads what's already there
    for OTHER modules' views, never this round's own freshly-written
    state. Returned list entries: {"view_xml_id": str, "declaring_module": str | None}.
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (v:View)-[:REFERENCES_FIELD]->(:Field {key: $field_key})
      OPTIONAL MATCH (mod:Module)-[:DECLARES_VIEW]->(v)
      RETURN collect(DISTINCT {view_xml_id: v.xml_id, declaring_module: mod.name}) AS refs
    }
    RETURN in_progress, refs
    """
    record = _run_gated_read(
        driver,
        cypher,
        {"import_metadata_id": _IMPORT_METADATA_ID, "field_key": field_key},
    )
    if record is None:
        return False, []
    return bool(record["in_progress"]), list(record["refs"] or [])


def get_ungated_models(driver: Driver, technical_names: list[str]) -> tuple[bool, list[str]]:
    """Phase 36 §4.5 item 2 -- Code-Review's `_flag_ungated_access_model_touched` consumer.

    Given the diff's touched-model technical names, returns the subset that currently carry
    `has_ungated_access_rule = true` (i.e. at least one `:RESTRICTED_TO` row for that model has
    a null/absent group -- an access rule with no security-group restriction at all, per §2.2
    step 11's load rule). Same fixed-parameterized, fail-open, single-round-trip discipline as
    every other function in this module -- an empty/missing `technical_names` list is valid
    input and simply yields an empty match set, never a special-cased short-circuit that would
    skip the :ImportMetadata gate.
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (m:Model)
      WHERE m.technical_name IN $technical_names AND m.has_ungated_access_rule = true
      RETURN collect(DISTINCT m.technical_name) AS ungated
    }
    RETURN in_progress, ungated
    """
    record = _run_gated_read(
        driver,
        cypher,
        {"import_metadata_id": _IMPORT_METADATA_ID, "technical_names": list(technical_names or [])},
    )
    if record is None:
        return False, []
    return bool(record["in_progress"]), list(record["ungated"] or [])


def get_field_dependency_chain(driver: Driver, model: str, field_name: str) -> tuple[bool, list[dict]]:
    """Phase 36 S13.5 -- follows the (:Field)-[:DEPENDS_ON]->(:Field) edge
    (added by odoo_kg_to_neo4j.py's build_field_depends_rows/
    CYPHER_MERGE_FIELD_DEPENDS_ON, same-model only) from a given
    model.field and returns every dangling/missing depended-on field: a
    real DEPENDS_ON edge target that does NOT itself appear on any :Model
    via a real HAS_FIELD edge.

    Real schema correction versus the design doc this implements (flagged
    per this task's own instruction to trust the real schema over an
    invented one): "a field exists on its model" is
    `(:Model)-[:HAS_FIELD]->(:Field)`, not a `(d)-[:ON]->(:OdooModel)`
    edge -- no such edge or label exists anywhere in this real, migrated
    graph (see graph_queries.py's own module docstring / the real schema
    this whole file is built against).
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (f:Field {model: $model, name: $field_name})-[:DEPENDS_ON]->(d)
      WHERE NOT EXISTS { MATCH (:Model)-[:HAS_FIELD]->(d) }
      RETURN collect({key: d.key, model: d.model, name: d.name}) AS dangling
    }
    RETURN in_progress, dangling
    """
    record = _run_gated_read(
        driver,
        cypher,
        {"import_metadata_id": _IMPORT_METADATA_ID, "model": model, "field_name": field_name},
    )
    if record is None:
        return False, []
    return bool(record["in_progress"]), list(record["dangling"] or [])


def get_blast_radius(
    driver: Driver, model_technical_name: str, excluding_module: str | None = None
) -> tuple[bool, list[str]]:
    """Phase 36 S13.3 -- the load-bearing "what else touches this model"
    query: every OTHER real module that touches `model_technical_name` via
    (a) inheriting/extending/reopening it (EXTENDS' `via_module` property
    plus the new REOPENS edge -- EXTENDS itself has no module-typed
    endpoint, only a `via_module` string property, so that leg is read
    from the edge property, not a graph traversal), (b) a view TARGETING
    it (the new :View-[:TARGETS]->:Model edge), or (c) an access rule
    DECLARES_ACCESS_RULE-scoped to it (the new edge, module-attributed
    from real per-module security rows) -- unioned, deduplicated, sorted.
    `excluding_module`, if given, is dropped from the final result (e.g.
    "what does changing this model affect, other than the module I'm
    already editing").
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      OPTIONAL MATCH (target:Model {technical_name: $model})
      OPTIONAL MATCH (:Model)-[ex:EXTENDS]->(target)
      OPTIONAL MATCH (reopener:Module)-[:REOPENS]->(target)
      OPTIONAL MATCH (viewer:View)-[:TARGETS]->(target)
      OPTIONAL MATCH (viewer)<-[:DECLARES_VIEW]-(view_module:Module)
      OPTIONAL MATCH (access_module:Module)-[:DECLARES_ACCESS_RULE]->(target)
      RETURN
        [x IN collect(DISTINCT ex.via_module) WHERE x IS NOT NULL] AS extends_modules,
        [x IN collect(DISTINCT reopener.name) WHERE x IS NOT NULL] AS reopens_modules,
        [x IN collect(DISTINCT view_module.name) WHERE x IS NOT NULL] AS view_modules,
        [x IN collect(DISTINCT access_module.name) WHERE x IS NOT NULL] AS access_modules
    }
    RETURN in_progress, extends_modules, reopens_modules, view_modules, access_modules
    """
    record = _run_gated_read(
        driver, cypher, {"import_metadata_id": _IMPORT_METADATA_ID, "model": model_technical_name}
    )
    if record is None:
        return False, []
    modules: set[str] = set()
    for key in ("extends_modules", "reopens_modules", "view_modules", "access_modules"):
        modules.update(record[key] or [])
    if excluding_module:
        modules.discard(excluding_module)
    return bool(record["in_progress"]), sorted(modules)


def get_module_grounding(driver: Driver, module_name: str) -> tuple[bool, dict]:
    """Phase 36 S12.2 item 5 -- everything a module already declares:
    models it DEFINES, models it REOPENS (the new edge, S13.2), fields it
    EXTENDS_FIELD, and views it DECLARES_VIEW. Returned data dict shape:
    {"defines": [str, ...], "reopens": [str, ...],
    "extends_fields": [str, ...], "declares_views": [str, ...]}.
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      OPTIONAL MATCH (mod:Module {name: $module_name})
      OPTIONAL MATCH (mod)-[:DEFINES]->(defined:Model)
      OPTIONAL MATCH (mod)-[:REOPENS]->(reopened:Model)
      OPTIONAL MATCH (mod)-[:EXTENDS_FIELD]->(f:Field)
      OPTIONAL MATCH (mod)-[:DECLARES_VIEW]->(v:View)
      RETURN
        [x IN collect(DISTINCT defined.technical_name) WHERE x IS NOT NULL] AS defines,
        [x IN collect(DISTINCT reopened.technical_name) WHERE x IS NOT NULL] AS reopens,
        [x IN collect(DISTINCT f.key) WHERE x IS NOT NULL] AS extends_fields,
        [x IN collect(DISTINCT v.xml_id) WHERE x IS NOT NULL] AS declares_views
    }
    RETURN in_progress, defines, reopens, extends_fields, declares_views
    """
    record = _run_gated_read(driver, cypher, {"import_metadata_id": _IMPORT_METADATA_ID, "module_name": module_name})
    if record is None:
        return False, {"defines": [], "reopens": [], "extends_fields": [], "declares_views": []}
    return bool(record["in_progress"]), {
        "defines": list(record["defines"] or []),
        "reopens": list(record["reopens"] or []),
        "extends_fields": list(record["extends_fields"] or []),
        "declares_views": list(record["declares_views"] or []),
    }


def get_all_module_names(driver: Driver) -> tuple[bool, list[str]]:
    """Phase 36 §13.4 -- the full, real `:Module.name` sweep needed by
    `scripts/odoo_kg_install_state_reconciliation_sweep.py` to know which
    modules to re-check. Same fixed, single-round-trip, fail-open READ
    discipline as every other function above `update_module_install_state`.
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (m:Module)
      RETURN collect(m.name) AS names
    }
    RETURN in_progress, names
    """
    record = _run_gated_read(driver, cypher, {"import_metadata_id": _IMPORT_METADATA_ID})
    if record is None:
        return False, []
    return bool(record["in_progress"]), list(record["names"] or [])


def get_module_real_install_state(driver: Driver, module_name: str) -> tuple[bool, str | None]:
    """Phase 36 §13.4 -- reads back the `real_install_state` property
    `update_module_install_state` above writes, for the reconciliation
    sweep's own drift comparison. Returns (in_progress, state_or_None);
    None means either the module doesn't exist or it has never had a
    real state written onto it yet (both are real, distinct "nothing to
    compare against" outcomes the sweep treats as its own kind of drift).
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      OPTIONAL MATCH (m:Module {name: $module_name})
      RETURN m.real_install_state AS state
    }
    RETURN in_progress, state
    """
    record = _run_gated_read(driver, cypher, {"import_metadata_id": _IMPORT_METADATA_ID, "module_name": module_name})
    if record is None:
        return False, None
    return bool(record["in_progress"]), record["state"]


def update_module_install_state(driver: Driver, module_name: str, state: str, confirmed_at: str) -> bool:
    """Phase 36 S13.2/S13.4 -- THE ONE WRITE-CAPABLE FUNCTION IN THIS
    MODULE. Every other function in this file is a fixed READ (module
    docstring, "opens its session in READ access mode"); this one MERGEs
    `real_install_state`/`last_confirmed_at` onto the EXISTING
    `:Module {name: module_name}` node (never a duplicate node -- the
    module node itself already exists from the ETL's own CYPHER_MERGE_MODULE
    step; this only adds two new properties to it).

    Deliberately NOT gated on `NOT import_in_progress` the way every read
    above is: a targeted single-node property SET is not a structural read
    over a graph that might be mid-reload, and blocking it would mean a
    module's real live install state could go stale for the entire
    duration of a full reimport (which touches ~240 modules and can run
    for a while) -- exactly the window this capability exists to stay
    accurate through. Still opens in WRITE access mode against the same
    :ImportMetadata-aware settings (query timeout, database) as every
    other function here, for consistency, just without the gate itself.
    Returns True if a real :Module node was found and updated, False if
    `module_name` does not exist in the graph at all (a real, distinct
    outcome from "succeeded" -- callers should not assume success).
    """
    settings = load_neo4j_settings()
    cypher = """
    MATCH (m:Module {name: $module_name})
    SET m.real_install_state = $state, m.last_confirmed_at = $confirmed_at
    RETURN m.name AS name
    """
    query = Query(cypher, timeout=settings.query_timeout_s)
    with driver.session(database=settings.database) as session:
        result = session.run(
            query, {"module_name": module_name, "state": state, "confirmed_at": confirmed_at}
        )
        record = result.single()
    return record is not None


def get_all_ungated_models(driver: Driver) -> tuple[bool, list[str]]:
    """Phase 36 §4.5 item 1 -- `scripts/security_posture_alert_check.py`'s consumer. The
    full, un-scoped sweep of every model currently carrying `has_ungated_access_rule = true`
    (§2.2 step 11's load rule), diffed daily by the caller against the prior day's snapshot
    to find `false -> true` transitions worth a `SECURITY`-level Loki alert. Deliberately a
    separate function from `get_ungated_models` above rather than that function called with
    an unbounded/wildcard `technical_names` list: this one's fixed Cypher has no
    `WHERE m.technical_name IN $technical_names` clause at all, keeping the "one fixed
    query per call site" discipline (§4.2) honest -- a caller that wants "all of them" gets a
    query written for that, not a workaround using the scoped function's parameter.
    """
    cypher = """
    MATCH (i:ImportMetadata {id: $import_metadata_id})
    WITH coalesce(i.import_in_progress, false) AS in_progress
    CALL {
      WITH in_progress
      WITH in_progress WHERE NOT in_progress
      MATCH (m:Model)
      WHERE m.has_ungated_access_rule = true
      RETURN collect(DISTINCT m.technical_name) AS ungated
    }
    RETURN in_progress, ungated
    """
    record = _run_gated_read(driver, cypher, {"import_metadata_id": _IMPORT_METADATA_ID})
    if record is None:
        return False, []
    return bool(record["in_progress"]), list(record["ungated"] or [])
