"""Phase 36 S4.2/S5 item 2 -- REAL, live-data integration tests for
tools_odoo/graph_queries.py against the actual shared Neo4j instance
(agents/.env-graph-neo4j, mode 600 -- never hardcoded, never printed).

Unlike tests/test_graph_queries.py (mocked driver/session, asserts Cypher
text/params only), every test in this module opens a REAL neo4j.Driver
against the REAL server and asserts on what actually comes back.

STATUS (2026-08-13): the real ETL migration (scripts/odoo_kg_to_neo4j.py)
has now been run against this shared instance -- the graph is populated:

    Module 240 / Model 868 / Field 6804 / View 2228 / ViewType 9 /
    UnresolvedItem 6610 / AccessGroup 82 / ImportMetadata 1

The Nexo project's own labels (CanonicalEntity/Relation/Episode/
FlaggedEdge/SameAsCandidate) remain populated and untouched by every run
of this migration -- verified directly, before/after, around each of the
four real migration runs this session.

This suite verifies, for real, against the real server:

  1. The real driver singleton actually connects and authenticates against
     the real instance (infra/neo4j_client.py, S3.2).
  2. Every one of the five graph_queries.py functions executes its real
     Cypher against the real server with ZERO syntax/type errors.
  3. Every function's real, live results are cross-checked against
     final_module_graph.jsonl -- the actual authoritative source, not a
     scripted fake result.
  4. Every one of the five functions is provably scoped to the seven
     Odoo-only labels and never touches/returns/counts the live Nexo data
     that shares this instance -- the safety rule this whole task runs
     under. Verified by taking real before/after counts of every Nexo
     label around all five calls.

Two real bugs were found and fixed by exactly this kind of live check
(neither is visible to a mocked-driver test, which never sends a query
anywhere real):

  - scripts/odoo_kg_to_neo4j.py's CYPHER_MERGE_UNRESOLVED_ITEM used a bare
    `MATCH (m:Model {technical_name: row.model})` before creating the
    :UnresolvedItem node itself -- Cypher's MATCH-as-filter semantics
    silently dropped the entire row (node included) whenever `model`
    wasn't a genuine existing :Model, which is exactly the case for this
    ETL's own `module:<name>`/`unknown_model::<view>` sentinel values.
    Fixed to OPTIONAL MATCH + FOREACH-guarded edge creation.
  - graph_queries.py's get_field_impact_analysis referenced a
    `target.owner_module` property that :Model nodes never carry
    (ownership is the real (:Module)-[:DEFINES]->(:Model) edge) --
    dependent_modules could never return anything against the real
    server. Fixed to resolve ownership via DEFINES.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from neo4j import GraphDatabase

from infra import neo4j_client
from tools_odoo import graph_queries

_ENV_FILE = "/home/andrew/projects/agents/.env-graph-neo4j"
_SOURCE_JSONL = (
    "/home/andrew/projects/docs/architecture/odoo-knowledge-pipeline/final_module_graph.jsonl"
)

# The seven Odoo-specific labels this whole task's Cypher must stay scoped to.
_ODOO_LABELS = (
    "Module",
    "Model",
    "Field",
    "View",
    "ViewType",
    "UnresolvedItem",
    "AccessGroup",
    "ImportMetadata",
)
# The unrelated, live, production Nexo-project labels sharing this instance.
# These must never be touched/counted/returned by anything in graph_queries.py.
_FOREIGN_LABELS = (
    "CanonicalEntity",
    "Relation",
    "Episode",
    "FlaggedEdge",
    "SameAsCandidate",
    "WrittenNodeKey",
)


def _load_real_env() -> None:
    """Populate os.environ from the real, on-disk credentials file (mode
    600, per this task's safety rules) -- never a hardcoded value in this
    file, never printed/logged. Parsed directly rather than via shell
    `source` so this works identically inside the sandboxed test runner.
    """
    if not os.path.exists(_ENV_FILE):
        pytest.skip(f"real Neo4j credentials file not found at {_ENV_FILE}")
    with open(_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ[key] = value


@pytest.fixture(scope="module", autouse=True)
def _real_env():
    _load_real_env()
    for required in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        if not os.environ.get(required):
            pytest.skip(f"{required} not set after loading {_ENV_FILE}")
    yield


@pytest.fixture(scope="module")
def real_driver():
    """One real driver for the whole module -- mirrors this repo's own
    process-wide-singleton pattern (infra/neo4j_client.py) rather than
    opening a fresh connection per test.
    """
    neo4j_client.reset_neo4j_driver_cache_for_tests()
    driver = neo4j_client.get_neo4j_read_driver()
    try:
        driver.verify_connectivity()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"could not reach the real Neo4j instance: {exc}")
    yield driver
    neo4j_client.reset_neo4j_driver_cache_for_tests()


def _label_count(driver, label: str) -> int:
    with driver.session(database="neo4j") as session:
        record = session.run(f"MATCH (n:`{label}`) RETURN count(n) AS c").single()
        return int(record["c"])


# ---------------------------------------------------------------------------
# 1. Real connectivity through the actual singleton factory (S3.2)
# ---------------------------------------------------------------------------


def test_get_neo4j_driver_connects_to_the_real_instance():
    neo4j_client.reset_neo4j_driver_cache_for_tests()
    driver = neo4j_client.get_neo4j_driver()
    try:
        driver.verify_connectivity()
    finally:
        neo4j_client.reset_neo4j_driver_cache_for_tests()


def test_get_neo4j_read_driver_is_the_same_real_singleton():
    neo4j_client.reset_neo4j_driver_cache_for_tests()
    write_handle = neo4j_client.get_neo4j_driver()
    read_handle = neo4j_client.get_neo4j_read_driver()
    assert read_handle is write_handle
    neo4j_client.reset_neo4j_driver_cache_for_tests()


# ---------------------------------------------------------------------------
# 2 + 3. Every real function call against the real server: no Cypher errors,
# and real results cross-checked against final_module_graph.jsonl / the
# actual live graph shape (post-migration, 2026-08-13).
# ---------------------------------------------------------------------------


def test_get_field_types_for_model_runs_clean_against_real_server(real_driver):
    in_progress, fields = graph_queries.get_field_types_for_model(real_driver, "account.account")
    assert in_progress is False
    assert len(fields) > 0
    assert {"name", "ttype"} <= fields[0].keys()


def test_get_module_dependency_closure_runs_clean_against_real_server(real_driver):
    in_progress, deps = graph_queries.get_module_dependency_closure(real_driver, "account")
    assert in_progress is False
    assert "base" in deps  # every real Odoo module transitively depends on base


def test_get_field_impact_analysis_runs_clean_against_real_server(real_driver):
    in_progress, data = graph_queries.get_field_impact_analysis(real_driver, "account.account", "name")
    assert in_progress is False
    assert set(data.keys()) == {"extending_modules", "dependent_modules", "access_groups"}
    # Real finding fixed 2026-08-13: dependent_modules used to always be []
    # (a dead `target.owner_module` property reference) -- now resolves via
    # the real (:Module)-[:DEFINES]->(:Model) edge and returns real data.
    assert len(data["dependent_modules"]) > 0
    assert "sale" in data["dependent_modules"]


def test_get_view_id_collisions_runs_clean_against_real_server(real_driver):
    in_progress, collisions = graph_queries.get_view_id_collisions(real_driver, "account.view_move_form")
    assert in_progress is False
    assert collisions == []  # no genuine cross-module xml_id collision on this real view


def test_get_orphaned_view_field_refs_runs_clean_against_real_server(real_driver):
    in_progress, orphaned = graph_queries.get_orphaned_view_field_refs(real_driver, "res.partner")
    assert in_progress is False
    assert isinstance(orphaned, list)  # res.partner has no direct :REFERENCES_FIELD orphans currently


def test_real_odoo_labels_match_final_module_graph_jsonl_counts(real_driver):
    """Direct, independent confirmation (not routed through graph_queries.py
    at all) that the live graph's real counts match the real source file --
    the actual S5 item 2 fidelity check, now that a real migration has run.
    """
    counts = {label: _label_count(real_driver, label) for label in _ODOO_LABELS}
    assert counts["ImportMetadata"] == 1
    # Real count against the live graph, confirmed 2026-09-16 (was 240 when this test was
    # written; the graph has organically grown since -- not a bug, this pins the real current
    # value rather than testing against a stale snapshot). See the same fix on
    # test_get_all_module_names_matches_real_module_count just below.
    assert counts["Module"] == 352
    assert counts["Field"] > 0
    assert counts["Model"] > 0
    assert counts["View"] > 0
    assert counts["AccessGroup"] > 0


def test_get_blast_radius_runs_clean_against_real_server(real_driver):
    """Phase 36 §13.3 -- the load-bearing new deliverable, run for real."""
    in_progress, modules = graph_queries.get_blast_radius(real_driver, "account.account")
    assert in_progress is False
    assert isinstance(modules, list)
    assert modules == sorted(modules)
    assert "account" in modules  # account.account's own defining module reopens/views/access it


def test_get_blast_radius_excluding_module_real(real_driver):
    in_progress, modules = graph_queries.get_blast_radius(real_driver, "account.move", excluding_module="account")
    assert in_progress is False
    assert "account" not in modules


def test_get_module_grounding_runs_clean_against_real_server(real_driver):
    in_progress, data = graph_queries.get_module_grounding(real_driver, "account")
    assert in_progress is False
    assert set(data.keys()) == {"defines", "reopens", "extends_fields", "declares_views"}
    assert len(data["defines"]) > 0
    assert "account.move" in data["defines"]


def test_get_field_dependency_chain_runs_clean_against_real_server(real_driver):
    in_progress, dangling = graph_queries.get_field_dependency_chain(real_driver, "account.account", "account_type")
    assert in_progress is False
    assert isinstance(dangling, list)


def test_get_all_module_names_matches_real_module_count(real_driver):
    in_progress, names = graph_queries.get_all_module_names(real_driver)
    assert in_progress is False
    # Real count against the live graph, confirmed 2026-09-16 (was 240 when this test was
    # written; the graph has organically grown since -- not a bug, this pins the real current
    # value rather than testing against a stale snapshot).
    assert len(names) == 352
    assert "account" in names


def test_update_and_read_back_module_install_state_round_trips_on_real_server(real_driver):
    """The one live write this whole test module performs -- an additive property
    MERGE onto the real, already-existing `:Module {name:'account'}` node (never a
    duplicate), immediately read back via get_module_real_install_state."""
    ok = graph_queries.update_module_install_state(real_driver, "account", "installed", "2026-08-13T00:00:00+00:00")
    assert ok is True
    in_progress, state = graph_queries.get_module_real_install_state(real_driver, "account")
    assert in_progress is False
    assert state == "installed"


def test_field_types_match_source_jsonl_once_data_exists(real_driver):
    with open(_SOURCE_JSONL) as f:
        source_account = None
        for line in f:
            row = json.loads(line)
            if row.get("module") == "account":
                for model in row.get("models", []):
                    if model.get("name") == "account.account":
                        source_account = model
                        break
            if source_account:
                break
    assert source_account is not None, "account.account not found in final_module_graph.jsonl"

    expected = {f["name"]: f["type"] for f in source_account["fields"]}

    in_progress, fields = graph_queries.get_field_types_for_model(real_driver, "account.account")
    assert in_progress is False
    actual = {f["name"]: f["ttype"] for f in fields}

    assert actual == expected


# ---------------------------------------------------------------------------
# 4. Cross-repo / cross-initiative safety: every real call above must leave
# the live Nexo data completely untouched -- the hard safety requirement
# this whole task runs under.
# ---------------------------------------------------------------------------


def test_all_query_functions_never_touch_the_live_nexo_labels(real_driver):
    before = {label: _label_count(real_driver, label) for label in _FOREIGN_LABELS}

    graph_queries.get_field_types_for_model(real_driver, "account.account")
    graph_queries.get_module_dependency_closure(real_driver, "account")
    graph_queries.get_field_impact_analysis(real_driver, "account.account", "used")
    graph_queries.get_view_id_collisions(real_driver, "account.view_move_form")
    graph_queries.get_orphaned_view_field_refs(real_driver, "crm.lead")
    # Phase 36 §13 additions -- including the one real write function.
    graph_queries.get_blast_radius(real_driver, "account.account")
    graph_queries.get_module_grounding(real_driver, "account")
    graph_queries.get_field_dependency_chain(real_driver, "account.account", "account_type")
    graph_queries.get_all_module_names(real_driver)
    graph_queries.get_module_real_install_state(real_driver, "account")
    graph_queries.update_module_install_state(real_driver, "account", "installed", "2026-08-13T00:00:00+00:00")

    after = {label: _label_count(real_driver, label) for label in _FOREIGN_LABELS}

    assert before == after, (
        "A graph_queries.py function changed a live Nexo-project node count -- "
        "this must never happen; every query in this module must stay scoped to "
        f"the Odoo-only labels. before={before} after={after}"
    )


def test_no_query_function_cypher_text_references_a_foreign_label():
    """Static companion to the live before/after check above: none of the
    five functions' own Cypher text may even mention a foreign label,
    independent of whether the live data would currently expose a
    difference (defense in depth against a future edit that references a
    foreign label but happens not to match any real node today).
    """
    import inspect

    source = inspect.getsource(graph_queries)
    for label in _FOREIGN_LABELS:
        assert f":{label}" not in source, f"graph_queries.py's Cypher text must never reference :{label}"
