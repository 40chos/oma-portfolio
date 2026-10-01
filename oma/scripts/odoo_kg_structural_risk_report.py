#!/usr/bin/env python3
"""Phase 36 §0.8a item 2 / §7 item 0 -- structural risk report: betweenness centrality
(model cascade risk) + Louvain community detection (de facto module coupling) over the
live Odoo Knowledge Graph.

Real, flagged implementation choice (2026-08-13): §0.9's tenth revision says this ships
"via Neo4j GDS (Community Edition, confirmed zero additional license cost)... rather than
networkx directly," on the assumption GDS would be available on the real provisioned
instance. Direct inspection of the REAL, live instance this migration actually runs
against (`SHOW PROCEDURES YIELD name WHERE name STARTS WITH 'gds'`) returns zero rows --
GDS is not installed, matching APOC's own confirmed absence (see scripts/
odoo_kg_to_neo4j.py's backup/wipe rewrite). Installing a new plugin on a shared, live,
production Neo4j instance is a consequential infra change outside this task's authority to
make unilaterally, so this script instead implements the plan's own already-specified
fallback -- §0.8b's v1 form: "networkx.algorithms.centrality.betweenness_centrality and
networkx.algorithms.community.louvain_communities run directly against the in-memory
DEPENDS_ON/EXTENDS_FIELD subgraph." The only difference from that v1 text is the subgraph
is fetched fresh via plain Cypher against the real live Neo4j instance (not the flat
JSONL files v1 originally meant, since that instance is real and already migrated) rather
than run in-process against Neo4j's own GDS procedures.

Real interpretation choice on the graph shape: the plan's literal text describes ONE
"DEPENDS_ON/EXTENDS_FIELD graph projection" feeding both algorithms, but DEPENDS_ON
connects (:Module)->(:Module) while EXTENDS_FIELD connects (:Module)->(:Field) -- mixing
those two edge/node-label shapes into one typed graph doesn't cleanly answer either of the
two questions §7 item 0 actually asks. This instead builds two separate, purpose-fit graphs,
each matching one stated question exactly:

  - Betweenness centrality over (:Model)-[:EXTENDS]->(:Model) -- "which model, if broken,
    cascades to the most other models via `_inherit`" -- a direct schema edge already
    present and tested against real migrated data (238 real EXTENDS edges).
  - Louvain community detection over (:Module)-[:DEPENDS_ON]->(:Module) (treated as
    undirected for clustering -- coupling is symmetric regardless of dependency direction)
    -- "which modules form a de facto coupled cluster the declared depends structure
    doesn't show on its own."

Exit code: non-zero on any real failure (Neo4j unreachable, mid-import, query error) --
this is a diagnostic report script, not a runtime fail-open Build/Code-Review consumer.
tools_odoo/graph_queries.py's own module docstring draws this exact distinction explicitly
("a diagnostic/report script should exit non-zero on a real outage").

Usage:
    python3 scripts/odoo_kg_structural_risk_report.py [--top-n N] [--output PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import networkx as nx
from neo4j import READ_ACCESS, Query

from infra.neo4j_client import get_neo4j_read_driver
from infra.settings import load_neo4j_settings

_IMPORT_METADATA_ID = "odoo_full_module_graph"


class GraphMidImportError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def check_not_mid_import(driver) -> None:
    """Raises GraphMidImportError if the last ETL run is still in progress -- refuses to
    report against a possibly-inconsistent, partially-written snapshot. A small TOCTOU
    window exists between this check and the two fetch_* queries below (unlike
    graph_queries.py's single-statement CALL{} pattern); acceptable here since this is a
    periodic diagnostic report, not a security-relevant runtime gate.
    """
    settings = load_neo4j_settings()
    query = Query(
        "MATCH (i:ImportMetadata {id: $id}) RETURN coalesce(i.import_in_progress, false) AS in_progress",
        timeout=settings.query_timeout_s,
    )
    with driver.session(database=settings.database, default_access_mode=READ_ACCESS) as session:
        record = session.run(query, {"id": _IMPORT_METADATA_ID}).single()
    if record and record["in_progress"]:
        raise GraphMidImportError(
            "Odoo Knowledge Graph import is currently in progress -- refusing to report "
            "against a possibly-inconsistent snapshot. Retry after the current import completes."
        )


def fetch_model_extends_edges(driver) -> list[tuple[str, str]]:
    settings = load_neo4j_settings()
    query = Query(
        "MATCH (child:Model)-[:EXTENDS]->(base:Model) "
        "RETURN child.technical_name AS child, base.technical_name AS base",
        timeout=settings.query_timeout_s,
    )
    with driver.session(database=settings.database, default_access_mode=READ_ACCESS) as session:
        return [(r["child"], r["base"]) for r in session.run(query)]


def fetch_module_depends_edges(driver) -> list[tuple[str, str]]:
    settings = load_neo4j_settings()
    query = Query(
        "MATCH (a:Module)-[:DEPENDS_ON]->(b:Module) RETURN a.name AS a, b.name AS b",
        timeout=settings.query_timeout_s,
    )
    with driver.session(database=settings.database, default_access_mode=READ_ACCESS) as session:
        return [(r["a"], r["b"]) for r in session.run(query)]


def compute_model_cascade_risk(edges: list[tuple[str, str]], top_n: int = 20) -> list[dict]:
    """Betweenness centrality over the directed Model EXTENDS graph -- a high-scoring model
    sits on many child->base inheritance paths, so breaking it cascades widely. Zero-score
    models are dropped (not "at risk" by this measure); empty input yields an empty report,
    never an error.
    """
    graph = nx.DiGraph()
    graph.add_edges_from(edges)
    if graph.number_of_nodes() == 0:
        return []
    scores = nx.betweenness_centrality(graph, normalized=True)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"model": name, "betweenness_centrality": round(score, 6)}
        for name, score in ranked[:top_n]
        if score > 0
    ]


def compute_module_communities(edges: list[tuple[str, str]]) -> list[dict]:
    """Louvain community detection over the undirected Module DEPENDS_ON graph -- surfaces
    de facto coupled module clusters. Singleton communities (a module with no real coupling
    to any other, post-clustering) are dropped -- not a "cluster" worth reporting.
    `seed=0` for deterministic, reproducible report output across runs.
    """
    graph = nx.Graph()
    graph.add_edges_from(edges)
    if graph.number_of_nodes() == 0:
        return []
    communities = nx.algorithms.community.louvain_communities(graph, seed=0)
    return [
        {"community_id": i, "modules": sorted(community)}
        for i, community in enumerate(communities)
        if len(community) > 1
    ]


def run(top_n: int = 20) -> dict:
    driver = get_neo4j_read_driver()
    check_not_mid_import(driver)
    model_edges = fetch_model_extends_edges(driver)
    module_edges = fetch_module_depends_edges(driver)
    return {
        "generated_at": _now_iso(),
        "model_cascade_risk": compute_model_cascade_risk(model_edges, top_n=top_n),
        "module_communities": compute_module_communities(module_edges),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=20, help="how many top-risk models to report")
    parser.add_argument("--output", type=Path, default=None, help="write JSON report here (default: stdout)")
    args = parser.parse_args(argv)

    report = run(top_n=args.top_n)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
