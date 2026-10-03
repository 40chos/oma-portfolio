"""Phase 36 §4.4 (Code-Review's graph-grounded field check) and §4.5 item 2 (Code-Review's
ungated-access-model annotation) -- unit tests, mock-driven exactly like
tests/test_graph_queries.py's own established convention (a fake Neo4j driver/session, no
real Neo4j instance and no real Odoo container required for this file), since these tests
exercise the WIRING (specialist.py calling the right graph_queries.py function with the
right arguments and reacting correctly to what it returns) rather than the underlying Cypher
itself, which test_graph_queries.py already covers directly.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from specialists.code_review.specialist import (
    ReviewFinding,
    _extract_touched_model_technical_names,
    _filter_hallucinated_direct_field_missing_findings,
    _flag_ungated_access_model_touched,
    _get_graph_verified_field_names,
)


# ---------------------------------------------------------------------------
# §4.4: _get_graph_verified_field_names / the graph-backed leg of
# _filter_hallucinated_direct_field_missing_findings
# ---------------------------------------------------------------------------

def test_get_graph_verified_field_names_returns_names_from_a_healthy_graph():
    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_field_types_for_model",
             return_value=(False, [{"name": "name"}, {"name": "partner_id"}, {"name": None}]),
         ) as mock_query:
        result = _get_graph_verified_field_names("res.partner")

    assert result == {"name", "partner_id"}
    mock_query.assert_called_once_with("fake-driver", "res.partner")


def test_get_graph_verified_field_names_import_in_progress_returns_empty():
    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_field_types_for_model",
             return_value=(True, [{"name": "name"}]),
         ):
        result = _get_graph_verified_field_names("res.partner")

    assert result == set()


def test_get_graph_verified_field_names_fails_open_on_any_exception():
    with patch("infra.neo4j_client.get_neo4j_read_driver", side_effect=RuntimeError("no route to host")):
        result = _get_graph_verified_field_names("res.partner")

    assert result == set()  # never raises -- fail-open per §4.4's own discipline


def test_get_graph_verified_field_names_fails_open_when_module_missing():
    # Simulates "graph modules not importable in this environment" -- the ImportError path.
    with patch.dict(sys.modules, {"infra.neo4j_client": None}):
        result = _get_graph_verified_field_names("res.partner")

    assert result == set()


def test_direct_field_filter_downgrades_using_graph_alone_when_rpc_path_unconfigured():
    """§4.4's core new behavior: the graph source alone (no OMA_ODOO_DB_DUPLICATE_FOR_BUILD
    set at all, so the RPC path is never even attempted) is now enough to downgrade a
    verified-real-field claim -- this was NOT possible before this revision, when only the
    live RPC call could ever supply `real_fields`.
    """
    os.environ.pop("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", None)
    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
        ),
        "/mnt/extra-addons/oma_x/data/mail_template_data.xml": (
            '<odoo><record><field name="body_html">{{ object.name }}</field></record></odoo>'
        ),
    }
    findings = [
        ReviewFinding(
            location="data/mail_template_data.xml:1", severity="blocking",
            explanation=(
                "The template body references object.name, but the model project.fieldjob "
                "may not have a name field; verify field existence to prevent rendering errors."
            ),
        ),
    ]

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_field_types_for_model",
             return_value=(False, [{"name": "name"}]),
         ):
        out = _filter_hallucinated_direct_field_missing_findings(findings, files)

    assert out[0].severity == "info"
    assert "DOES exist" in out[0].explanation


def test_direct_field_filter_unions_graph_and_rpc_sources():
    """Two fields referenced, one confirmed only by the graph and one confirmed only by the
    (mocked) live RPC call -- §4.4's own "the two sources are complementary" design: the
    union closes more false-negative surface than either source alone.
    """
    os.environ["OMA_ODOO_DB_DUPLICATE_FOR_BUILD"] = "odoo16_dev"
    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
        ),
        "/mnt/extra-addons/oma_x/data/mail_template_data.xml": (
            '<odoo><record><field name="body_html">'
            '{{ object.graph_only_field }} {{ object.rpc_only_field }}'
            '</field></record></odoo>'
        ),
    }
    findings = [
        ReviewFinding(
            location="data/mail_template_data.xml:1", severity="blocking",
            explanation=(
                "object.graph_only_field may not exist and object.rpc_only_field may not "
                "have this field either; verify field existence."
            ),
        ),
    ]

    try:
        with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
             patch(
                 "tools_odoo.graph_queries.get_field_types_for_model",
                 return_value=(False, [{"name": "graph_only_field"}]),
             ), \
             patch("tools_odoo.odoo_schema_client.is_fast_path_eligible", return_value=True), \
             patch("tools_odoo.odoo_schema_client.get_model_fields_fast", return_value=["rpc_only_field"]):
            out = _filter_hallucinated_direct_field_missing_findings(findings, files)
    finally:
        os.environ.pop("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", None)

    assert out[0].severity == "info"
    assert "graph_only_field" in out[0].explanation
    assert "rpc_only_field" in out[0].explanation


def test_direct_field_filter_still_leaves_finding_untouched_when_both_sources_empty():
    os.environ.pop("OMA_ODOO_DB_DUPLICATE_FOR_BUILD", None)
    files = {
        "/mnt/extra-addons/oma_x/models/models.py": (
            "from odoo import models\n\nclass ProjectFieldjob(models.Model):\n"
            "    _inherit = 'project.fieldjob'\n"
        ),
        "/mnt/extra-addons/oma_x/data/mail_template_data.xml": (
            '<odoo><record><field name="body_html">{{ object.name }}</field></record></odoo>'
        ),
    }
    findings = [
        ReviewFinding(
            location="data/mail_template_data.xml:1", severity="blocking",
            explanation="object.name may not exist; verify field existence.",
        ),
    ]

    with patch("infra.neo4j_client.get_neo4j_read_driver", side_effect=RuntimeError("down")):
        out = _filter_hallucinated_direct_field_missing_findings(findings, files)

    assert out[0].severity == "blocking"  # untouched -- neither source had a confident answer


# ---------------------------------------------------------------------------
# §4.5 item 2: _extract_touched_model_technical_names / _flag_ungated_access_model_touched
# ---------------------------------------------------------------------------

def test_extract_touched_model_technical_names_from_inherit_and_new_model():
    files = {
        "models.py": (
            "from odoo import models, fields\n\n"
            "class Extended(models.Model):\n"
            "    _inherit = 'res.partner'\n\n"
            "class Brand(models.Model):\n"
            "    _name = 'x_custom.brand'\n"
        ),
    }
    assert _extract_touched_model_technical_names(files) == ["res.partner", "x_custom.brand"]


def test_extract_touched_model_technical_names_empty_when_no_models_py():
    files = {"views/view.xml": "<odoo/>"}
    assert _extract_touched_model_technical_names(files) == []


def test_flag_ungated_access_model_touched_appends_info_finding_for_ungated_model():
    files = {
        "models.py": (
            "from odoo import models\n\nclass Extended(models.Model):\n"
            "    _inherit = 'res.partner'\n"
        ),
    }
    findings = [ReviewFinding(location="models.py:1", severity="minor", explanation="pre-existing")]

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch(
             "tools_odoo.graph_queries.get_ungated_models",
             return_value=(False, ["res.partner"]),
         ) as mock_query:
        out = _flag_ungated_access_model_touched(findings, files)

    mock_query.assert_called_once_with("fake-driver", ["res.partner"])
    assert len(out) == 2
    assert out[0].explanation == "pre-existing"  # original findings never mutated
    assert out[1].severity == "info"
    assert "res.partner" in out[1].explanation
    assert "ungated" in out[1].explanation.lower()


def test_flag_ungated_access_model_touched_never_produces_a_blocking_finding():
    """§4.5 item 2's own explicit design: informational-only until a graduation trigger is
    met (neither trigger is met in this revision) -- this must never emit severity=blocking.
    """
    files = {"models.py": "class X(models.Model):\n    _inherit = 'res.partner'\n"}
    findings: list[ReviewFinding] = []

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch("tools_odoo.graph_queries.get_ungated_models", return_value=(False, ["res.partner"])):
        out = _flag_ungated_access_model_touched(findings, files)

    assert all(f.severity != "blocking" for f in out)


def test_flag_ungated_access_model_touched_no_op_when_no_models_touched():
    findings = [ReviewFinding(location="x", severity="minor", explanation="only")]
    out = _flag_ungated_access_model_touched(findings, {"views/view.xml": "<odoo/>"})
    assert out == findings  # short-circuits before ever calling the graph


def test_flag_ungated_access_model_touched_no_op_when_nothing_ungated():
    files = {"models.py": "class X(models.Model):\n    _inherit = 'res.partner'\n"}
    findings = [ReviewFinding(location="x", severity="minor", explanation="only")]

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch("tools_odoo.graph_queries.get_ungated_models", return_value=(False, [])):
        out = _flag_ungated_access_model_touched(findings, files)

    assert out == findings


def test_flag_ungated_access_model_touched_no_op_when_import_in_progress():
    files = {"models.py": "class X(models.Model):\n    _inherit = 'res.partner'\n"}
    findings = [ReviewFinding(location="x", severity="minor", explanation="only")]

    with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
         patch("tools_odoo.graph_queries.get_ungated_models", return_value=(True, ["res.partner"])):
        out = _flag_ungated_access_model_touched(findings, files)

    assert out == findings  # graph mid-import -- don't trust a possibly-incomplete answer


def test_flag_ungated_access_model_touched_fails_open_on_graph_exception():
    files = {"models.py": "class X(models.Model):\n    _inherit = 'res.partner'\n"}
    findings = [ReviewFinding(location="x", severity="minor", explanation="only")]

    with patch("infra.neo4j_client.get_neo4j_read_driver", side_effect=RuntimeError("unreachable")):
        out = _flag_ungated_access_model_touched(findings, files)

    assert out == findings  # never raises, never blocks the review on a graph outage


def test_flag_ungated_access_model_touched_respects_test_only_escape_hatch():
    os.environ["OMA_SKIP_UNGATED_ACCESS_CHECK"] = "1"
    try:
        files = {"models.py": "class X(models.Model):\n    _inherit = 'res.partner'\n"}
        findings = [ReviewFinding(location="x", severity="minor", explanation="only")]
        with patch("infra.neo4j_client.get_neo4j_read_driver", return_value="fake-driver"), \
             patch("tools_odoo.graph_queries.get_ungated_models", return_value=(False, ["res.partner"])) as mock_query:
            out = _flag_ungated_access_model_touched(findings, files)
        mock_query.assert_not_called()
        assert out == findings
    finally:
        os.environ.pop("OMA_SKIP_UNGATED_ACCESS_CHECK", None)
