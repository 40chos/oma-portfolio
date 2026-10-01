"""Phase 35 §12.2 item 5 / §13.3 -- wires the graph's real safety queries
(get_module_grounding, get_blast_radius, get_views_referencing_field_key)
into the real Build pipeline, not just tested in isolation against live
data:

1. Pre-generation grounding (§12.2 item 5, the TDAD-inspired highest-leverage
   addition): before Build writes any edit, show it what the target module
   already declares (models, fields, views) per the live graph, so it can
   avoid re-declaring or colliding with its own prior work at the source,
   instead of relying entirely on downstream verification to catch it after.

2. Pre-write blast-radius BLOCKING gate (2026-08-13, upgraded from an
   earlier, weaker post-write-only trace event after live testing showed a
   log line alone doesn't satisfy this section's own real goal -- "prevent
   a task from breaking something already built," not just notice it did):
   run in the SAME pre-write validator chain as
   validate_new_primary_view_ids_dont_collide_with_graph
   (specialists/build/graph_structural_gate.py), computed entirely from
   this round's own old-vs-new models.py diff, never from the graph's own
   freshness for THIS module (which the async incremental-sync hook only
   starts, never guarantees complete by write time):
   - A field this round just ADDED that another real, already-installed
     module ALREADY declares on the same model -- the crm.lead/hr.leave
     duplicate-field failure class §12 was built to close.
   - A field this round just REMOVED that another real module's VIEW still
     references -- the exact crm.lead incident (19+ modules broken by one
     field removal, invisible until an unrelated later install forced Odoo
     to revalidate the whole registry) that motivated this whole section.
   A genuine finding raises ValueError, caught by the SAME
   (ValueError, GatewayUnavailableError) except clause every other
   pre-write validator already raises into -- the round fails this
   candidate with a real, specific, actionable message, and the normal
   retry loop treats it exactly like any other validation failure (the
   "give it to the AI to fix" mechanism already proven for view-ID
   collisions, reused here rather than invented new).

Every graph read here fails open on any error (missing graph, network
issue, unexpected schema, mid-import) -- a graph outage must never block or
crash a real task; it just means this round runs without the extra check,
same as before this existed.
"""

from __future__ import annotations

import ast
import logging

logger = logging.getLogger(__name__)


def resolve_current_module_grounding_block(module_name: str) -> str:
    """Pre-generation grounding. "" (never raises) on any graph error, a
    graph mid-import, or a module with nothing recorded yet -- same
    fail-open contract as resolve_current_schema_block/resolve_current_acl_target_block.
    """
    try:
        from infra.neo4j_client import get_neo4j_driver
        from tools_odoo.graph_queries import get_module_grounding

        driver = get_neo4j_driver()
        in_progress, grounding = get_module_grounding(driver, module_name)
        if in_progress:
            return ""
        defines = sorted(grounding.get("defines") or [])
        reopens = sorted(grounding.get("reopens") or [])
        extends_fields = sorted(grounding.get("extends_fields") or [])
        declares_views = sorted(grounding.get("declares_views") or [])
        if not (defines or reopens or extends_fields or declares_views):
            return ""
        lines = [f"Module {module_name!r} already declares, per the live structural graph:"]
        if defines:
            lines.append(f"  - Defines models: {', '.join(defines)}")
        if reopens:
            lines.append(f"  - Extends/reopens models: {', '.join(reopens)}")
        if extends_fields:
            lines.append(f"  - Fields already added (model.field): {', '.join(extends_fields)}")
        if declares_views:
            lines.append(f"  - Views already declared: {', '.join(declares_views)}")
        lines.append(
            "Do not redeclare any of the above as new -- check for name collisions with these "
            "before adding a new field or view this round."
        )
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 -- grounding is an optional hint, never a hard dependency
        logger.warning(
            "module grounding lookup failed for %r, continuing without it", module_name, exc_info=True
        )
        return ""


def extract_field_names_for_model(models_py_source: str, model_technical_name: str) -> set[str]:
    """AST-based: real field names (`fields.X(...)` assignments) declared on
    the class whose `_name`/`_inherit` (str or list form) matches
    model_technical_name. Returns an empty set (never raises) on unparseable
    source or no matching class -- same fail-open contract as every function
    in this module.
    """
    try:
        tree = ast.parse(models_py_source)
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        matches = False
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            target_names = {t.id for t in stmt.targets if isinstance(t, ast.Name)}
            if not ({"_name", "_inherit"} & target_names):
                continue
            try:
                value = ast.literal_eval(stmt.value)
            except (ValueError, SyntaxError, TypeError):
                continue
            if value == model_technical_name or (
                isinstance(value, list) and model_technical_name in value
            ):
                matches = True
        if not matches:
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            call = stmt.value
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "fields"
            ):
                continue
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def extract_touched_model_names(models_py_source: str) -> set[str]:
    """AST-based: every real Odoo model technical name this source touches,
    from every class's own `_name` (str) and `_inherit` (str or list form).
    Returns an empty set (never raises) on unparseable source -- same
    fail-open contract as every function in this module. Self-contained
    (no dependency on a TaskContract or any other caller-side context),
    since the pre-write validator chain that calls this has no
    `contract.module_identity` of its own to reuse.
    """
    try:
        tree = ast.parse(models_py_source)
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            target_names = {t.id for t in stmt.targets if isinstance(t, ast.Name)}
            if not ({"_name", "_inherit"} & target_names):
                continue
            try:
                value = ast.literal_eval(stmt.value)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(value, str):
                names.add(value)
            elif isinstance(value, list):
                names.update(v for v in value if isinstance(v, str))
    return names


def check_blast_radius_field_collisions(
    module_name: str, model_technical_name: str, new_field_names: set[str]
) -> list[str]:
    """Post-write collision check. `new_field_names` are field names this
    round's own diff just added to model_technical_name (computed by the
    caller via extract_field_names_for_model on old vs. new models.py, NOT
    read back from the graph -- avoids any dependency on this round's own
    freshly-written data having already reached the graph, only siblings'
    pre-existing, already-synced data is queried). Returns [] (never raises)
    on any graph error, an empty blast radius, or no real collision.

    Real gap found live (2026-08-13, direct reproduction against real data:
    crm_google_ads/crm_google_ads_inter's genuine shared crm.lead field): a
    single slow/flaky sibling lookup (a real Neo4j transaction timeout, not
    hypothetical) previously aborted the WHOLE check via the outer try/except,
    discarding collision findings already computed for every other sibling.
    Each sibling's own lookup is now individually guarded, so one bad sibling
    only loses that one sibling's signal, never every other sibling's.
    """
    if not new_field_names:
        return []
    try:
        from infra.neo4j_client import get_neo4j_driver
        from tools_odoo.graph_queries import get_blast_radius, get_module_grounding

        driver = get_neo4j_driver()
        in_progress, siblings = get_blast_radius(driver, model_technical_name, excluding_module=module_name)
        if in_progress or not siblings:
            return []

        own_keys = {f"{model_technical_name}.{name}" for name in new_field_names}
        findings: list[str] = []
        for sibling in sorted(siblings):
            try:
                sib_in_progress, sib_grounding = get_module_grounding(driver, sibling)
            except Exception:  # noqa: BLE001 -- one flaky sibling must not lose every other one
                logger.warning(
                    "blast-radius sibling grounding lookup failed for sibling=%r "
                    "(module=%r model=%r), skipping just this sibling",
                    sibling, module_name, model_technical_name, exc_info=True,
                )
                continue
            if sib_in_progress:
                continue
            collisions = own_keys & set(sib_grounding.get("extends_fields") or [])
            for key in sorted(collisions):
                findings.append(
                    f"blast-radius: field {key!r} is declared by BOTH {module_name!r} (this "
                    f"round) and sibling module {sibling!r}, which also touches "
                    f"{model_technical_name!r} -- confirm this is an intentional shared field, "
                    "not an accidental duplicate declaration."
                )
        return findings
    except Exception:  # noqa: BLE001 -- a safety signal must never crash or block the real task
        logger.warning(
            "blast-radius collision check failed for module=%r model=%r, continuing without it",
            module_name, model_technical_name, exc_info=True,
        )
        return []


def check_removed_field_still_referenced(
    module_name: str, model_technical_name: str, removed_field_names: set[str]
) -> list[str]:
    """§12.2 item 3's real, motivating failure class: a field this round's
    own diff just REMOVED from model_technical_name that some OTHER real,
    already-installed module's view still references -- the exact crm.lead
    incident (a field removed by one task left a view in a different,
    already-installed module still referencing it, invisible until an
    unrelated, later install of anything touching the same shared model
    forced Odoo to revalidate the whole registry and crashed) that
    originally motivated this whole section.

    `removed_field_names` are computed by the caller from old-vs-new
    models.py (extract_field_names_for_model on each, set-differenced),
    never read back from the graph -- this function only asks the graph
    what OTHER modules' views already, historically reference, which needs
    no freshness for THIS round's own just-removed field at all. Returns []
    (never raises) on any graph error or no real reference found.
    """
    if not removed_field_names:
        return []
    try:
        from infra.neo4j_client import get_neo4j_driver
        from tools_odoo.graph_queries import get_views_referencing_field_key

        driver = get_neo4j_driver()
        findings: list[str] = []
        for field_name in sorted(removed_field_names):
            field_key = f"{model_technical_name}.{field_name}"
            try:
                in_progress, refs = get_views_referencing_field_key(driver, field_key)
            except Exception:  # noqa: BLE001 -- one flaky field lookup must not lose the others
                logger.warning(
                    "removed-field reference check failed for field_key=%r "
                    "(module=%r), skipping just this field",
                    field_key, module_name, exc_info=True,
                )
                continue
            if in_progress or not refs:
                continue
            other_module_refs = [
                r for r in refs if r.get("declaring_module") != module_name
            ]
            for ref in other_module_refs:
                findings.append(
                    f"blast-radius: field {field_key!r} was just removed by "
                    f"{module_name!r} this round, but view {ref.get('view_xml_id')!r} "
                    f"in module {ref.get('declaring_module')!r} still references it -- "
                    "removing it as-is would break that other module's real view."
                )
        return findings
    except Exception:  # noqa: BLE001 -- a safety signal must never crash or block the real task
        logger.warning(
            "removed-field reference check failed for module=%r model=%r, continuing without it",
            module_name, model_technical_name, exc_info=True,
        )
        return []


class ChangeRadius:
    """Phase 35 §17.0.0's own real spec: the module-level fan-out metric every pipeline-wide
    stage in §17 (decomposition, concurrent-claim admission, pre-install, post-install) uses.
    Deliberately NOT a Field/View-level node count -- get_blast_radius() only ever answers
    "which modules touch this model," a coarser, cheaper signal; a true per-node blast-radius
    query is out of scope for v1 (§17.10)."""

    __slots__ = ("in_progress", "affected_module_count", "affected_modules", "touched_models")

    def __init__(self, in_progress: bool, affected_modules: list[str], touched_models: list[str]):
        self.in_progress = in_progress
        self.affected_modules = affected_modules
        self.affected_module_count = len(affected_modules)
        self.touched_models = touched_models

    def __repr__(self) -> str:  # pragma: no cover -- debug/log convenience only
        return (
            f"ChangeRadius(in_progress={self.in_progress}, "
            f"affected_module_count={self.affected_module_count}, "
            f"touched_models={self.touched_models})"
        )


def compute_change_radius(
    driver, touched_models: list[str], excluding_module: str | None,
) -> ChangeRadius:
    """Phase 35 §17.0.0's real wrapper: loops get_blast_radius() over every touched model,
    unions the returned module sets, reports in_progress if ANY call reports in_progress.

    `excluding_module` is REQUIRED (not defaulted) per §17.0.0.0a's own fix -- every call site
    must pass an explicit, reasoned value (the module being written/installed, or None when no
    module identity exists yet, e.g. at decomposition time before Build has run). Passing None
    silently by omission was the exact bug §17.0.0.0a's own fix closes; this signature makes
    that a conscious choice at every call site instead.

    Synchronous by design, matching get_blast_radius()'s own real (non-async) signature -- no
    new async wrapper needed; callers already inside an async context should offload via
    asyncio.to_thread(), per this project's own established event-loop-safety discipline
    (see specialists/build/specialist.py's asyncio.to_thread usage for install_module() etc).
    """
    from tools_odoo.graph_queries import get_blast_radius

    any_in_progress = False
    per_model_modules: list[set[str]] = []
    for model in sorted(set(touched_models)):
        try:
            in_progress, modules = get_blast_radius(driver, model, excluding_module=excluding_module)
        except Exception:  # noqa: BLE001 -- one bad model lookup must not lose the others
            logger.warning(
                "compute_change_radius: get_blast_radius failed for model=%r, treating as "
                "in_progress (fail-open to the conservative/unsafe-to-trust side)",
                model, exc_info=True,
            )
            any_in_progress = True
            continue
        any_in_progress = any_in_progress or in_progress
        per_model_modules.append(set(modules))

    # Matches this codebase's own established in_progress-gated contract (e.g.
    # graph_queries.get_view_id_collisions): if ANY part of this radius computation is
    # untrustworthy, the WHOLE result is -- never hand back a partial union that looks
    # complete. A caller must check `.in_progress` before trusting `.affected_modules` at all.
    if any_in_progress:
        return ChangeRadius(
            in_progress=True, affected_modules=[], touched_models=sorted(set(touched_models)),
        )
    affected_modules: set[str] = set()
    for modules in per_model_modules:
        affected_modules.update(modules)
    return ChangeRadius(
        in_progress=False,
        affected_modules=sorted(affected_modules),
        touched_models=sorted(set(touched_models)),
    )
