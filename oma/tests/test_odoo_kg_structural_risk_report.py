"""Phase 36 §0.8a item 2 / §7 item 0 -- unit tests for
scripts/odoo_kg_structural_risk_report.py's pure computation functions
(compute_model_cascade_risk / compute_module_communities), plus mocked-driver tests for
check_not_mid_import's gating. The real live-instance run (real Cypher against the real
migrated graph) is exercised manually, not by this file -- same split as
test_odoo_kg_to_neo4j.py (mocked) vs test_graph_queries_live.py (real).
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "odoo_kg_structural_risk_report.py"
_spec = importlib.util.spec_from_file_location("odoo_kg_structural_risk_report", _SCRIPT_PATH)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


class TestComputeModelCascadeRisk:
    def test_empty_edges_returns_empty_report(self):
        assert report.compute_model_cascade_risk([]) == []

    def test_base_model_extended_by_many_scores_highest(self):
        # child1, child2, child3 all extend 'base' directly -- 'base' should be the top-
        # ranked cascade risk (every path from a grandchild to 'base' passes through the
        # direct extenders, and 'base' itself is the universal sink here).
        edges = [
            ("child1", "hub"), ("child2", "hub"), ("child3", "hub"),
            ("grandchild", "child1"),
        ]
        result = report.compute_model_cascade_risk(edges, top_n=10)
        names = [r["model"] for r in result]
        assert "child1" in names  # sits on the grandchild -> hub path
        for row in result:
            assert row["betweenness_centrality"] > 0

    def test_zero_score_models_are_dropped(self):
        # A simple two-node chain: no node has any node routing THROUGH it (betweenness
        # requires a path of length >= 2 through a third node), so this should drop to empty.
        edges = [("a", "b")]
        assert report.compute_model_cascade_risk(edges) == []

    def test_respects_top_n(self):
        edges = [(f"c{i}", "hub") for i in range(10)] + [(f"gc{i}", f"c{i}") for i in range(10)]
        result = report.compute_model_cascade_risk(edges, top_n=3)
        assert len(result) <= 3


class TestComputeModuleCommunities:
    def test_empty_edges_returns_empty_report(self):
        assert report.compute_module_communities([]) == []

    def test_two_disjoint_clusters_detected(self):
        edges = [
            ("a", "b"), ("b", "c"), ("a", "c"),  # cluster 1: a/b/c triangle
            ("x", "y"), ("y", "z"), ("x", "z"),  # cluster 2: x/y/z triangle
        ]
        result = report.compute_module_communities(edges)
        all_modules = {m for c in result for m in c["modules"]}
        assert all_modules == {"a", "b", "c", "x", "y", "z"}
        # the two triangles must land in different communities (no edges between them)
        cluster_of = {m: c["community_id"] for c in result for m in c["modules"]}
        assert cluster_of["a"] == cluster_of["b"] == cluster_of["c"]
        assert cluster_of["x"] == cluster_of["y"] == cluster_of["z"]
        assert cluster_of["a"] != cluster_of["x"]

    def test_deterministic_across_runs(self):
        edges = [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a"), ("e", "f")]
        first = report.compute_module_communities(edges)
        second = report.compute_module_communities(edges)
        assert first == second


class TestCheckNotMidImport:
    def test_raises_when_import_in_progress(self):
        fake_driver = MagicMock()
        fake_session = MagicMock()
        fake_driver.session.return_value.__enter__.return_value = fake_session
        fake_session.run.return_value.single.return_value = {"in_progress": True}

        with pytest.raises(report.GraphMidImportError):
            report.check_not_mid_import(fake_driver)

    def test_does_not_raise_when_not_in_progress(self):
        fake_driver = MagicMock()
        fake_session = MagicMock()
        fake_driver.session.return_value.__enter__.return_value = fake_session
        fake_session.run.return_value.single.return_value = {"in_progress": False}

        report.check_not_mid_import(fake_driver)  # must not raise

    def test_does_not_raise_when_no_import_metadata_row(self):
        fake_driver = MagicMock()
        fake_session = MagicMock()
        fake_driver.session.return_value.__enter__.return_value = fake_session
        fake_session.run.return_value.single.return_value = None

        report.check_not_mid_import(fake_driver)  # must not raise


class TestRun:
    def test_run_assembles_full_report_shape(self):
        with patch.object(report, "get_neo4j_read_driver", return_value="fake-driver"), \
             patch.object(report, "check_not_mid_import"), \
             patch.object(report, "fetch_model_extends_edges", return_value=[("a", "b"), ("c", "b")]), \
             patch.object(report, "fetch_module_depends_edges", return_value=[("x", "y")]):
            result = report.run(top_n=5)

        assert set(result.keys()) == {"generated_at", "model_cascade_risk", "module_communities"}
        assert isinstance(result["model_cascade_risk"], list)
        assert isinstance(result["module_communities"], list)

    def test_run_propagates_mid_import_error(self):
        with patch.object(report, "get_neo4j_read_driver", return_value="fake-driver"), \
             patch.object(report, "check_not_mid_import", side_effect=report.GraphMidImportError("busy")):
            with pytest.raises(report.GraphMidImportError):
                report.run()
