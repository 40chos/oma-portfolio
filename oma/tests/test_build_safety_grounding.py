"""Phase 35 §12.2 item 5 / §13.3 -- unit tests for
tools_odoo/knowledge_graph/build_safety_grounding.py, the module that wires
the two "ship first" graph queries (get_module_grounding, get_blast_radius)
into the real Build pipeline (pre-generation grounding + post-write
blast-radius collision check), not just tested in isolation.

Real bug found live during this task's own verification (2026-08-13): the
first draft of build_safety_grounding.py named the graph functions' first
return value "ok" and checked `if not ok`, but the real, established,
tested contract across this whole codebase (graph_structural_gate.py,
test_graph_queries_live.py) is `in_progress` -- True means an import is
currently running and the call should be treated as unusable, the exact
opposite polarity. Fixed in the source; every mock below uses the real
polarity (False = safe to use, True = mid-import/unsafe).
"""

from __future__ import annotations

from unittest.mock import patch

from tools_odoo.knowledge_graph.build_safety_grounding import (
    ChangeRadius,
    check_blast_radius_field_collisions,
    check_removed_field_still_referenced,
    compute_change_radius,
    extract_field_names_for_model,
    extract_touched_model_names,
    resolve_current_module_grounding_block,
)


class TestExtractFieldNamesForModel:
    def test_finds_fields_on_matching_name_class(self):
        source = (
            "from odoo import fields, models\n\n"
            "class Foo(models.Model):\n"
            "    _name = 'x.model'\n"
            "    bar = fields.Char()\n"
            "    baz = fields.Integer()\n"
        )
        assert extract_field_names_for_model(source, "x.model") == {"bar", "baz"}

    def test_finds_fields_on_matching_inherit_str(self):
        source = (
            "from odoo import fields, models\n\n"
            "class Foo(models.Model):\n"
            "    _inherit = 'stock.picking'\n"
            "    gate_pass_code = fields.Char()\n"
        )
        assert extract_field_names_for_model(source, "stock.picking") == {"gate_pass_code"}

    def test_finds_fields_on_matching_inherit_list(self):
        source = (
            "from odoo import fields, models\n\n"
            "class Foo(models.Model):\n"
            "    _name = 'x.new.model'\n"
            "    _inherit = ['mail.thread', 'x.new.model']\n"
            "    note = fields.Text()\n"
        )
        assert extract_field_names_for_model(source, "x.new.model") == {"note"}

    def test_no_match_returns_empty_set(self):
        source = (
            "from odoo import fields, models\n\n"
            "class Foo(models.Model):\n"
            "    _name = 'other.model'\n"
            "    bar = fields.Char()\n"
        )
        assert extract_field_names_for_model(source, "x.model") == set()

    def test_empty_source_returns_empty_set(self):
        assert extract_field_names_for_model("", "x.model") == set()

    def test_unparseable_source_returns_empty_set_never_raises(self):
        assert extract_field_names_for_model("this is not ( python", "x.model") == set()

    def test_non_fields_assignment_ignored(self):
        source = (
            "class Foo(models.Model):\n"
            "    _name = 'x.model'\n"
            "    bar = fields.Char()\n"
            "    _description = 'Something'\n"
            "    CONST = 5\n"
        )
        assert extract_field_names_for_model(source, "x.model") == {"bar"}


class TestResolveCurrentModuleGroundingBlock:
    def test_empty_when_graph_unavailable(self):
        with patch(
            "tools_odoo.graph_queries.get_module_grounding",
            side_effect=RuntimeError("no graph"),
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            assert resolve_current_module_grounding_block("some_module") == ""

    def test_empty_when_import_in_progress(self):
        with patch(
            "tools_odoo.graph_queries.get_module_grounding", return_value=(True, {})
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            assert resolve_current_module_grounding_block("some_module") == ""

    def test_empty_when_nothing_declared(self):
        empty = {"defines": [], "reopens": [], "extends_fields": [], "declares_views": []}
        with patch(
            "tools_odoo.graph_queries.get_module_grounding", return_value=(False, empty)
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            assert resolve_current_module_grounding_block("some_module") == ""

    def test_renders_declared_content(self):
        data = {
            "defines": ["x.model"],
            "reopens": ["stock.picking"],
            "extends_fields": ["stock.picking.gate_pass_code"],
            "declares_views": ["view_x_form"],
        }
        with patch(
            "tools_odoo.graph_queries.get_module_grounding", return_value=(False, data)
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            block = resolve_current_module_grounding_block("oma_test_module")
        assert "oma_test_module" in block
        assert "x.model" in block
        assert "stock.picking" in block
        assert "stock.picking.gate_pass_code" in block
        assert "view_x_form" in block

    def test_never_raises_on_missing_driver(self):
        with patch(
            "infra.neo4j_client.get_neo4j_driver", side_effect=RuntimeError("missing env var")
        ):
            assert resolve_current_module_grounding_block("some_module") == ""


class TestCheckBlastRadiusFieldCollisions:
    def test_empty_new_field_names_short_circuits(self):
        assert check_blast_radius_field_collisions("mod_a", "x.model", set()) == []

    def test_empty_when_graph_unavailable(self):
        with patch(
            "infra.neo4j_client.get_neo4j_driver", side_effect=RuntimeError("no graph")
        ):
            result = check_blast_radius_field_collisions("mod_a", "x.model", {"foo"})
        assert result == []

    def test_empty_when_blast_radius_import_in_progress(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", return_value=(True, [])
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_blast_radius_field_collisions("mod_a", "x.model", {"foo"})
        assert result == []

    def test_empty_when_no_siblings(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", return_value=(False, [])
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_blast_radius_field_collisions("mod_a", "x.model", {"foo"})
        assert result == []

    def test_finds_real_collision(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", return_value=(False, ["mod_b"])
        ), patch(
            "tools_odoo.graph_queries.get_module_grounding",
            return_value=(False, {"extends_fields": ["x.model.foo"]}),
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_blast_radius_field_collisions("mod_a", "x.model", {"foo"})
        assert len(result) == 1
        assert "foo" in result[0] or "x.model.foo" in result[0]
        assert "mod_a" in result[0]
        assert "mod_b" in result[0]

    def test_no_collision_when_sibling_has_different_field(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", return_value=(False, ["mod_b"])
        ), patch(
            "tools_odoo.graph_queries.get_module_grounding",
            return_value=(False, {"extends_fields": ["x.model.bar"]}),
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_blast_radius_field_collisions("mod_a", "x.model", {"foo"})
        assert result == []

    def test_sibling_grounding_failure_is_skipped_not_fatal(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", return_value=(False, ["mod_b"])
        ), patch(
            "tools_odoo.graph_queries.get_module_grounding", return_value=(True, {})
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_blast_radius_field_collisions("mod_a", "x.model", {"foo"})
        assert result == []

    def test_never_raises_on_unexpected_exception(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", side_effect=Exception("boom")
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_blast_radius_field_collisions("mod_a", "x.model", {"foo"})
        assert result == []

    def test_one_flaky_sibling_does_not_lose_other_siblings_findings(self):
        """Real gap found live (2026-08-13): a single sibling's own grounding
        lookup raising (a real Neo4j transaction timeout, reproduced live)
        must not discard collision findings already available for OTHER
        siblings in the same blast radius.
        """
        def grounding_side_effect(driver, name):
            if name == "mod_flaky":
                raise Exception("transaction timed out")
            return (False, {"extends_fields": ["x.model.foo"]})

        with patch(
            "tools_odoo.graph_queries.get_blast_radius",
            return_value=(False, ["mod_flaky", "mod_good"]),
        ), patch(
            "tools_odoo.graph_queries.get_module_grounding", side_effect=grounding_side_effect
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_blast_radius_field_collisions("mod_a", "x.model", {"foo"})
        assert len(result) == 1
        assert "mod_good" in result[0]


class TestExtractTouchedModelNames:
    def test_finds_name_and_inherit(self):
        source = (
            "class A(models.Model):\n"
            "    _name = 'x.model'\n"
            "class B(models.Model):\n"
            "    _inherit = 'stock.picking'\n"
        )
        assert extract_touched_model_names(source) == {"x.model", "stock.picking"}

    def test_finds_inherit_list(self):
        source = (
            "class A(models.Model):\n"
            "    _name = 'x.new.model'\n"
            "    _inherit = ['mail.thread', 'x.new.model']\n"
        )
        assert extract_touched_model_names(source) == {"x.new.model", "mail.thread"}

    def test_empty_source_returns_empty_set(self):
        assert extract_touched_model_names("") == set()

    def test_unparseable_source_returns_empty_set_never_raises(self):
        assert extract_touched_model_names("not ( python") == set()


class TestCheckRemovedFieldStillReferenced:
    def test_empty_removed_field_names_short_circuits(self):
        assert check_removed_field_still_referenced("mod_a", "x.model", set()) == []

    def test_empty_when_graph_unavailable(self):
        with patch(
            "infra.neo4j_client.get_neo4j_driver", side_effect=RuntimeError("no graph")
        ):
            result = check_removed_field_still_referenced("mod_a", "x.model", {"foo"})
        assert result == []

    def test_empty_when_import_in_progress(self):
        with patch(
            "tools_odoo.graph_queries.get_views_referencing_field_key", return_value=(True, [])
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_removed_field_still_referenced("mod_a", "x.model", {"foo"})
        assert result == []

    def test_empty_when_no_refs(self):
        with patch(
            "tools_odoo.graph_queries.get_views_referencing_field_key", return_value=(False, [])
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_removed_field_still_referenced("mod_a", "x.model", {"foo"})
        assert result == []

    def test_finds_real_reference_from_other_module(self):
        with patch(
            "tools_odoo.graph_queries.get_views_referencing_field_key",
            return_value=(False, [{"view_xml_id": "view_x_form", "declaring_module": "mod_b"}]),
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_removed_field_still_referenced("mod_a", "x.model", {"foo"})
        assert len(result) == 1
        assert "x.model.foo" in result[0]
        assert "view_x_form" in result[0]
        assert "mod_b" in result[0]

    def test_ignores_reference_from_own_module(self):
        with patch(
            "tools_odoo.graph_queries.get_views_referencing_field_key",
            return_value=(False, [{"view_xml_id": "view_x_form", "declaring_module": "mod_a"}]),
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_removed_field_still_referenced("mod_a", "x.model", {"foo"})
        assert result == []

    def test_one_flaky_field_does_not_lose_other_fields_findings(self):
        def side_effect(driver, field_key):
            if "flaky" in field_key:
                raise Exception("boom")
            return (False, [{"view_xml_id": "view_x_form", "declaring_module": "mod_b"}])

        with patch(
            "tools_odoo.graph_queries.get_views_referencing_field_key", side_effect=side_effect
        ), patch("infra.neo4j_client.get_neo4j_driver", return_value=object()):
            result = check_removed_field_still_referenced(
                "mod_a", "x.model", {"flaky", "good"}
            )
        assert len(result) == 1
        assert "x.model.good" in result[0]

    def test_never_raises_on_unexpected_exception(self):
        with patch(
            "infra.neo4j_client.get_neo4j_driver", return_value=object()
        ), patch(
            "tools_odoo.graph_queries.get_views_referencing_field_key", side_effect=Exception("boom")
        ):
            result = check_removed_field_still_referenced("mod_a", "x.model", {"foo"})
        assert result == []


class TestComputeChangeRadius:
    def test_empty_touched_models_returns_zero_radius(self):
        with patch("tools_odoo.graph_queries.get_blast_radius") as mock_gbr:
            result = compute_change_radius(object(), [], excluding_module=None)
        mock_gbr.assert_not_called()
        assert result.affected_module_count == 0
        assert result.in_progress is False

    def test_unions_modules_across_multiple_touched_models(self):
        def side_effect(driver, model, excluding_module=None):
            return {
                "crm.lead": (False, ["mod_a", "mod_b"]),
                "res.partner": (False, ["mod_b", "mod_c"]),
            }[model]

        with patch("tools_odoo.graph_queries.get_blast_radius", side_effect=side_effect):
            result = compute_change_radius(
                object(), ["crm.lead", "res.partner"], excluding_module=None,
            )
        assert result.affected_modules == ["mod_a", "mod_b", "mod_c"]
        assert result.affected_module_count == 3
        assert result.in_progress is False

    def test_passes_excluding_module_through_to_every_call(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", return_value=(False, [])
        ) as mock_gbr:
            compute_change_radius(object(), ["crm.lead"], excluding_module="oma_my_module")
        _, kwargs = mock_gbr.call_args
        assert kwargs["excluding_module"] == "oma_my_module"

    def test_any_in_progress_propagates_even_if_others_succeed(self):
        def side_effect(driver, model, excluding_module=None):
            if model == "crm.lead":
                return (True, [])
            return (False, ["mod_a"])

        with patch("tools_odoo.graph_queries.get_blast_radius", side_effect=side_effect):
            result = compute_change_radius(
                object(), ["crm.lead", "res.partner"], excluding_module=None,
            )
        assert result.in_progress is True
        # Once ANY model reports in_progress, the whole radius must be treated as untrustworthy
        # -- res.partner's own real "mod_a" hit is deliberately not surfaced here, even though
        # its own individual query succeeded, since a caller must never partially trust a radius
        # that's overall marked in_progress.
        assert result.affected_modules == []

    def test_query_exception_treated_as_in_progress_not_a_confirmed_zero_radius(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", side_effect=RuntimeError("boom"),
        ):
            result = compute_change_radius(object(), ["crm.lead"], excluding_module=None)
        assert result.in_progress is True

    def test_touched_models_deduplicated(self):
        with patch(
            "tools_odoo.graph_queries.get_blast_radius", return_value=(False, ["mod_a"]),
        ) as mock_gbr:
            result = compute_change_radius(
                object(), ["crm.lead", "crm.lead"], excluding_module=None,
            )
        assert mock_gbr.call_count == 1
        assert result.touched_models == ["crm.lead"]

    def test_change_radius_repr_does_not_raise(self):
        cr = ChangeRadius(in_progress=False, affected_modules=["a"], touched_models=["m"])
        assert "affected_module_count=1" in repr(cr)
