"""Phase 36 §0.9a/§4.1 -- unit tests for specialists/build/
graph_structural_gate.py.

find_new_primary_view_ids() is tested with real regex parsing (no mocks
needed). validate_new_primary_view_ids_dont_collide_with_graph() is tested
with a monkeypatched get_view_id_collisions so no real Neo4j connection is
required -- the live, real-server behavior of get_view_id_collisions
itself is covered separately by tests/test_graph_queries_live.py.
"""

import asyncio
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.graph_structural_gate as gate


@dataclass
class _FakeGenerated:
    views_xml: str = ""
    extra_data_files: dict | None = None


class TestFindNewPrimaryViewIds:
    def test_primary_view_with_no_inherit_id_is_a_candidate(self):
        generated = _FakeGenerated(views_xml=(
            '<record id="view_my_model_form" model="ir.ui.view">'
            '<field name="name">my.model.form</field>'
            '<field name="model">my.model</field>'
            '<field name="arch" type="xml"><form/></field>'
            "</record>"
        ))
        assert gate.find_new_primary_view_ids(generated) == ["view_my_model_form"]

    def test_inherited_view_is_not_a_candidate(self):
        generated = _FakeGenerated(views_xml=(
            '<record id="view_my_model_form_inherit" model="ir.ui.view">'
            '<field name="inherit_id" ref="base_module.view_my_model_form"/>'
            '<field name="arch" type="xml"><xpath expr="//form"/></field>'
            "</record>"
        ))
        assert gate.find_new_primary_view_ids(generated) == []

    def test_non_view_records_are_ignored(self):
        generated = _FakeGenerated(views_xml=(
            '<record id="action_my_model" model="ir.actions.act_window">'
            '<field name="name">My Model</field>'
            "</record>"
        ))
        assert gate.find_new_primary_view_ids(generated) == []

    def test_scans_extra_data_files_too(self):
        generated = _FakeGenerated(
            views_xml="",
            extra_data_files={
                "views/other.xml": (
                    '<record id="view_other_form" model="ir.ui.view">'
                    '<field name="model">other.model</field>'
                    '<field name="arch" type="xml"><form/></field>'
                    "</record>"
                )
            },
        )
        assert gate.find_new_primary_view_ids(generated) == ["view_other_form"]

    def test_no_records_produces_empty_list(self):
        assert gate.find_new_primary_view_ids(_FakeGenerated()) == []


class TestValidateNewPrimaryViewIdsDontCollideWithGraph:
    def test_no_candidates_short_circuits_without_touching_neo4j(self):
        # No views_xml/extra_data_files content -> find_new_primary_view_ids()
        # returns [] -> the function must return before ever importing/calling
        # infra.neo4j_client at all. No monkeypatch needed: if it tried to
        # connect to a real (unreachable in this test env) Neo4j instance,
        # this would hang/raise instead of returning cleanly.
        asyncio.run(gate.validate_new_primary_view_ids_dont_collide_with_graph(_FakeGenerated()))

    def test_fails_open_when_driver_construction_raises(self, monkeypatch):
        generated = _FakeGenerated(views_xml=(
            '<record id="view_x" model="ir.ui.view">'
            '<field name="model">x</field>'
            '<field name="arch" type="xml"><form/></field>'
            "</record>"
        ))

        def _boom():
            raise RuntimeError("no connection")

        import infra.neo4j_client as neo4j_client_module

        monkeypatch.setattr(neo4j_client_module, "get_neo4j_read_driver", _boom)
        # Must not raise -- fail-open per module docstring.
        asyncio.run(gate.validate_new_primary_view_ids_dont_collide_with_graph(generated))

    def test_raises_on_genuine_collision(self, monkeypatch):
        generated = _FakeGenerated(views_xml=(
            '<record id="view_x" model="ir.ui.view">'
            '<field name="model">x</field>'
            '<field name="arch" type="xml"><form/></field>'
            "</record>"
        ))

        import infra.neo4j_client as neo4j_client_module
        import tools_odoo.graph_queries as graph_queries_module

        monkeypatch.setattr(neo4j_client_module, "get_neo4j_read_driver", lambda: object())
        monkeypatch.setattr(
            graph_queries_module,
            "get_view_id_collisions",
            lambda driver, candidate_id: (
                False,
                [{"module_a": "sale", "module_b": "purchase", "xml_id": candidate_id}],
            ),
        )
        try:
            asyncio.run(gate.validate_new_primary_view_ids_dont_collide_with_graph(generated))
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "view_x" in str(exc)
            assert "purchase" in str(exc) and "sale" in str(exc)

    def test_no_collision_does_not_raise(self, monkeypatch):
        generated = _FakeGenerated(views_xml=(
            '<record id="view_x" model="ir.ui.view">'
            '<field name="model">x</field>'
            '<field name="arch" type="xml"><form/></field>'
            "</record>"
        ))

        import infra.neo4j_client as neo4j_client_module
        import tools_odoo.graph_queries as graph_queries_module

        monkeypatch.setattr(neo4j_client_module, "get_neo4j_read_driver", lambda: object())
        monkeypatch.setattr(graph_queries_module, "get_view_id_collisions", lambda driver, candidate_id: (False, []))
        asyncio.run(gate.validate_new_primary_view_ids_dont_collide_with_graph(generated))

    def test_import_in_progress_skips_even_with_collision_data(self, monkeypatch):
        generated = _FakeGenerated(views_xml=(
            '<record id="view_x" model="ir.ui.view">'
            '<field name="model">x</field>'
            '<field name="arch" type="xml"><form/></field>'
            "</record>"
        ))

        import infra.neo4j_client as neo4j_client_module
        import tools_odoo.graph_queries as graph_queries_module

        monkeypatch.setattr(neo4j_client_module, "get_neo4j_read_driver", lambda: object())
        monkeypatch.setattr(
            graph_queries_module,
            "get_view_id_collisions",
            lambda driver, candidate_id: (True, [{"module_a": "a", "module_b": "b", "xml_id": candidate_id}]),
        )
        asyncio.run(gate.validate_new_primary_view_ids_dont_collide_with_graph(generated))


@dataclass
class _FakeGeneratedModelsPy:
    models_py: str = ""


class TestValidateNoBlastRadiusCollisionsOrBreakage:
    """Phase 35 §12.2/§13.3 -- the field-level sibling of
    TestValidateNewPrimaryViewIdsDontCollideWithGraph above. Delegates its
    real graph logic to tools_odoo.knowledge_graph.build_safety_grounding
    (already exhaustively unit-tested in test_build_safety_grounding.py) --
    these tests cover the composition/wiring itself: touched-model
    extraction, added/removed field diffing, and the raise/no-raise/
    fail-open decision.
    """

    def test_no_models_py_at_all_short_circuits(self):
        generated = _FakeGeneratedModelsPy(models_py="")
        asyncio.run(gate.validate_no_blast_radius_collisions_or_breakage(generated, "", "mod_a"))

    def test_raises_on_a_real_added_field_collision(self, monkeypatch):
        old = "class Foo(models.Model):\n    _inherit = 'x.model'\n"
        new = (
            "class Foo(models.Model):\n"
            "    _inherit = 'x.model'\n"
            "    bar = fields.Char()\n"
        )
        generated = _FakeGeneratedModelsPy(models_py=new)

        import tools_odoo.knowledge_graph.build_safety_grounding as bsg_module

        monkeypatch.setattr(
            bsg_module, "check_blast_radius_field_collisions",
            lambda module_name, model, fields_: ["a real collision finding"],
        )
        monkeypatch.setattr(
            bsg_module, "check_removed_field_still_referenced",
            lambda module_name, model, fields_: [],
        )
        try:
            asyncio.run(
                gate.validate_no_blast_radius_collisions_or_breakage(generated, old, "mod_a")
            )
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "a real collision finding" in str(exc)

    def test_raises_on_a_real_removed_field_still_referenced(self, monkeypatch):
        old = (
            "class Foo(models.Model):\n"
            "    _inherit = 'x.model'\n"
            "    bar = fields.Char()\n"
        )
        new = "class Foo(models.Model):\n    _inherit = 'x.model'\n"
        generated = _FakeGeneratedModelsPy(models_py=new)

        import tools_odoo.knowledge_graph.build_safety_grounding as bsg_module

        monkeypatch.setattr(
            bsg_module, "check_blast_radius_field_collisions",
            lambda module_name, model, fields_: [],
        )
        monkeypatch.setattr(
            bsg_module, "check_removed_field_still_referenced",
            lambda module_name, model, fields_: ["a real removed-field finding"],
        )
        try:
            asyncio.run(
                gate.validate_no_blast_radius_collisions_or_breakage(generated, old, "mod_a")
            )
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "a real removed-field finding" in str(exc)

    def test_no_findings_does_not_raise(self, monkeypatch):
        old = "class Foo(models.Model):\n    _inherit = 'x.model'\n"
        new = (
            "class Foo(models.Model):\n"
            "    _inherit = 'x.model'\n"
            "    bar = fields.Char()\n"
        )
        generated = _FakeGeneratedModelsPy(models_py=new)

        import tools_odoo.knowledge_graph.build_safety_grounding as bsg_module

        monkeypatch.setattr(
            bsg_module, "check_blast_radius_field_collisions",
            lambda module_name, model, fields_: [],
        )
        monkeypatch.setattr(
            bsg_module, "check_removed_field_still_referenced",
            lambda module_name, model, fields_: [],
        )
        asyncio.run(gate.validate_no_blast_radius_collisions_or_breakage(generated, old, "mod_a"))

    def test_fails_open_when_safety_grounding_import_raises(self, monkeypatch):
        old = "class Foo(models.Model):\n    _inherit = 'x.model'\n"
        new = (
            "class Foo(models.Model):\n"
            "    _inherit = 'x.model'\n"
            "    bar = fields.Char()\n"
        )
        generated = _FakeGeneratedModelsPy(models_py=new)

        import builtins
        real_import = builtins.__import__

        def _boom_import(name, *args, **kwargs):
            if name == "tools_odoo.knowledge_graph.build_safety_grounding":
                raise RuntimeError("boom")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _boom_import)
        asyncio.run(gate.validate_no_blast_radius_collisions_or_breakage(generated, old, "mod_a"))
