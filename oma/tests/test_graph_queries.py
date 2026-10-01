"""Phase 36 §4.2 -- tools_odoo/graph_queries.py: fixed, parameterized Cypher
query functions. Every test drives a FAKE driver/session/result (no real
Neo4j instance is loaded with Odoo data yet -- that happens in a later
migration phase; live-data integration testing is a documented follow-up,
see the implementation report) and asserts two things per function: (1) the
right Cypher text/parameters get sent to session.run(), and (2) the right
Python shape gets parsed back out of a mocked result record.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from neo4j import READ_ACCESS, Query

from tools_odoo import graph_queries


class _FakeResult:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record


class _FakeSession:
    """Records every session.run() call and hands back a scripted result.
    Also a context manager, matching real neo4j.Session usage
    (`with driver.session(...) as session:`).
    """

    def __init__(self, record):
        self.record = record
        self.run_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, params):
        self.run_calls.append((query, params))
        return _FakeResult(self.record)


class _FakeDriver:
    def __init__(self, record):
        self._session = _FakeSession(record)
        self.session_calls = []

    def session(self, **kwargs):
        self.session_calls.append(kwargs)
        return self._session


@pytest.fixture(autouse=True)
def _neo4j_env():
    env = {
        "NEO4J_URI": "bolt://10.1.13.151:7687",
        "NEO4J_USER": "test-user",
        "NEO4J_PASSWORD": "test-password",
    }
    with patch.dict(os.environ, env, clear=False):
        yield


def _last_call(fake_driver):
    query, params = fake_driver._session.run_calls[-1]
    return query, params


def test_get_field_types_for_model_sends_expected_query_and_params():
    driver = _FakeDriver({
        "in_progress": False,
        "fields": [
            {"name": "name", "ttype": "Char", "required": True, "computed": False,
             "relation_target": None, "relation_kind": None},
        ],
    })

    in_progress, fields = graph_queries.get_field_types_for_model(driver, "res.partner")

    assert in_progress is False
    assert fields == [
        {"name": "name", "ttype": "Char", "required": True, "computed": False,
         "relation_target": None, "relation_kind": None},
    ]

    query, params = _last_call(driver)
    assert isinstance(query, Query)
    # 2026-08-13: default raised 8.0 -> 30.0 (infra/settings.py) -- the grown
    # live graph (9.2k+ Field nodes after the EXTENDS_FIELD fix) was timing
    # out real grounding/blast-radius queries at the old default.
    assert query.timeout == 30.0
    assert "HAS_FIELD" in query.text
    assert "WHERE NOT in_progress" in query.text
    assert params == {"import_metadata_id": "odoo_full_module_graph", "technical_name": "res.partner"}

    session_kwargs = driver.session_calls[-1]
    assert session_kwargs["default_access_mode"] == READ_ACCESS
    assert session_kwargs["database"] == "neo4j"


def test_get_field_types_for_model_import_in_progress_returns_flag_and_data_as_given():
    # The gate is enforced server-side (the CALL {} subquery never runs the MATCH when
    # in_progress is true); the fake driver here scripts what a real server would return
    # in that case -- an empty fields list -- and the test asserts the Python function
    # faithfully surfaces in_progress=True to the caller rather than hiding it.
    driver = _FakeDriver({"in_progress": True, "fields": []})

    in_progress, fields = graph_queries.get_field_types_for_model(driver, "res.partner")

    assert in_progress is True
    assert fields == []


def test_get_field_types_for_model_no_row_fails_open_as_not_in_progress_empty():
    driver = _FakeDriver(None)

    in_progress, fields = graph_queries.get_field_types_for_model(driver, "res.partner")

    assert in_progress is False
    assert fields == []


def test_get_module_dependency_closure_sends_expected_query_and_returns_a_set():
    driver = _FakeDriver({"in_progress": False, "deps": ["base", "mail", "base"]})

    in_progress, deps = graph_queries.get_module_dependency_closure(driver, "account")

    assert in_progress is False
    assert deps == {"base", "mail"}
    assert isinstance(deps, set)

    query, params = _last_call(driver)
    assert "DEPENDS_ON*1.." in query.text
    assert params == {"import_metadata_id": "odoo_full_module_graph", "module_name": "account"}


def test_get_field_impact_analysis_sends_expected_query_and_shape():
    driver = _FakeDriver({
        "in_progress": False,
        "extending_modules": ["sale_extra"],
        "dependent_modules": ["sale", "sale_stock"],
        "access_groups": [{"group": "account.group_account_manager", "perms": "rwcu"}],
    })

    in_progress, data = graph_queries.get_field_impact_analysis(driver, "account.account", "used")

    assert in_progress is False
    assert data == {
        "extending_modules": ["sale_extra"],
        "dependent_modules": ["sale", "sale_stock"],
        "access_groups": [{"group": "account.group_account_manager", "perms": "rwcu"}],
    }

    query, params = _last_call(driver)
    assert "EXTENDS_FIELD" in query.text
    assert "RESTRICTED_TO" in query.text
    assert "DEPENDS_ON*1.." in query.text
    # Real finding (2026-08-13): :Model never carries an `owner_module`
    # property (ownership is the (:Module)-[:DEFINES]->(:Model) edge) --
    # the original Cypher referenced target.owner_module, a property that
    # never exists, so dependent_modules could never return anything
    # against the real server. A mocked-driver test can't see a property
    # silently always being null, so this asserts the query text resolves
    # ownership via the real DEFINES edge instead.
    assert "[:DEFINES]->(target)" in query.text
    assert "owner_module" not in query.text
    assert params == {
        "import_metadata_id": "odoo_full_module_graph",
        "model": "account.account",
        "field_key": "account.account.used",
    }


def test_get_field_impact_analysis_no_row_fails_open_to_empty_shape():
    driver = _FakeDriver(None)

    in_progress, data = graph_queries.get_field_impact_analysis(driver, "account.account", "used")

    assert in_progress is False
    assert data == {"extending_modules": [], "dependent_modules": [], "access_groups": []}


def test_get_view_id_collisions_sends_expected_query_and_dedupes_direction():
    driver = _FakeDriver({
        "in_progress": False,
        "collisions": [
            {"module_a": "account", "module_b": "sale", "xml_id": "account.view_move_form"},
        ],
    })

    in_progress, collisions = graph_queries.get_view_id_collisions(driver, "account.view_move_form")

    assert in_progress is False
    assert collisions == [
        {"module_a": "account", "module_b": "sale", "xml_id": "account.view_move_form"},
    ]

    query, params = _last_call(driver)
    assert "DECLARES_VIEW" in query.text
    assert "m1.name < m2.name" in query.text
    assert params == {
        "import_metadata_id": "odoo_full_module_graph",
        "candidate_xml_id": "account.view_move_form",
    }


def test_get_view_id_collisions_empty_when_no_collision():
    driver = _FakeDriver({"in_progress": False, "collisions": []})

    in_progress, collisions = graph_queries.get_view_id_collisions(driver, "account.view_move_form")

    assert in_progress is False
    assert collisions == []


def test_get_orphaned_view_field_refs_sends_expected_query_and_shape():
    driver = _FakeDriver({
        "in_progress": False,
        "orphaned": [{"view_xml_id": "crm.crm_lead_view_form", "field_name": "old_field"}],
    })

    in_progress, orphaned = graph_queries.get_orphaned_view_field_refs(driver, "crm.lead")

    assert in_progress is False
    assert orphaned == [{"view_xml_id": "crm.crm_lead_view_form", "field_name": "old_field"}]

    query, params = _last_call(driver)
    assert "REFERENCES_FIELD" in query.text
    assert "HAS_FIELD" in query.text
    assert params == {"import_metadata_id": "odoo_full_module_graph", "model_name": "crm.lead"}


def test_get_ungated_models_sends_expected_query_and_params():
    driver = _FakeDriver({"in_progress": False, "ungated": ["res.partner"]})

    in_progress, ungated = graph_queries.get_ungated_models(driver, ["res.partner", "sale.order"])

    assert in_progress is False
    assert ungated == ["res.partner"]  # only res.partner comes back ungated, per the fixture

    query, params = _last_call(driver)
    assert "has_ungated_access_rule = true" in query.text
    assert "IN $technical_names" in query.text
    assert params == {
        "import_metadata_id": "odoo_full_module_graph",
        "technical_names": ["res.partner", "sale.order"],
    }


def test_get_ungated_models_empty_technical_names_list_is_valid_input():
    driver = _FakeDriver({"in_progress": False, "ungated": []})

    in_progress, ungated = graph_queries.get_ungated_models(driver, [])

    assert in_progress is False
    assert ungated == []
    query, params = _last_call(driver)
    assert params["technical_names"] == []
    assert "ImportMetadata" in query.text  # gate still runs even for an empty input list


def test_get_ungated_models_none_technical_names_coerces_to_empty_list():
    driver = _FakeDriver({"in_progress": False, "ungated": []})

    graph_queries.get_ungated_models(driver, None)

    _, params = _last_call(driver)
    assert params["technical_names"] == []


def test_get_ungated_models_import_in_progress_returns_flag_and_empty_data():
    driver = _FakeDriver({"in_progress": True, "ungated": []})

    in_progress, ungated = graph_queries.get_ungated_models(driver, ["res.partner"])

    assert in_progress is True
    assert ungated == []


def test_get_ungated_models_no_row_fails_open_as_not_in_progress_empty():
    driver = _FakeDriver(None)

    in_progress, ungated = graph_queries.get_ungated_models(driver, ["res.partner"])

    assert in_progress is False
    assert ungated == []


def test_get_all_ungated_models_sends_expected_query_with_no_scoping_parameter():
    driver = _FakeDriver({"in_progress": False, "ungated": ["res.partner", "sale.order"]})

    in_progress, ungated = graph_queries.get_all_ungated_models(driver)

    assert in_progress is False
    assert ungated == ["res.partner", "sale.order"]

    query, params = _last_call(driver)
    assert "has_ungated_access_rule = true" in query.text
    assert "technical_names" not in query.text  # unscoped sweep, no IN-list clause at all
    assert params == {"import_metadata_id": "odoo_full_module_graph"}


def test_get_all_ungated_models_import_in_progress_returns_flag_and_empty_data():
    driver = _FakeDriver({"in_progress": True, "ungated": []})

    in_progress, ungated = graph_queries.get_all_ungated_models(driver)

    assert in_progress is True
    assert ungated == []


def test_get_all_ungated_models_no_row_fails_open_as_not_in_progress_empty():
    driver = _FakeDriver(None)

    in_progress, ungated = graph_queries.get_all_ungated_models(driver)

    assert in_progress is False
    assert ungated == []


def test_all_query_functions_gate_on_the_same_import_metadata_id_and_leading_call_pattern():
    driver = _FakeDriver({
        "in_progress": False, "fields": [], "deps": [], "extending_modules": [],
        "dependent_modules": [], "access_groups": [], "collisions": [], "orphaned": [],
        "ungated": [],
    })

    graph_queries.get_field_types_for_model(driver, "res.partner")
    graph_queries.get_module_dependency_closure(driver, "account")
    graph_queries.get_field_impact_analysis(driver, "account.account", "used")
    graph_queries.get_view_id_collisions(driver, "account.view_move_form")
    graph_queries.get_orphaned_view_field_refs(driver, "crm.lead")
    graph_queries.get_ungated_models(driver, ["res.partner"])
    graph_queries.get_all_ungated_models(driver)

    assert len(driver._session.run_calls) == 7
    for query, params in driver._session.run_calls:
        assert params["import_metadata_id"] == "odoo_full_module_graph"
        assert "ImportMetadata" in query.text
        assert "CALL {" in query.text
        assert "WHERE NOT in_progress" in query.text
        # 2026-08-13: default raised 8.0 -> 30.0 (infra/settings.py) -- the grown
    # live graph (9.2k+ Field nodes after the EXTENDS_FIELD fix) was timing
    # out real grounding/blast-radius queries at the old default.
    assert query.timeout == 30.0


# ==========================================================================
# Phase 36 S13 additions
# ==========================================================================


def test_get_field_dependency_chain_returns_dangling_depended_on_fields():
    driver = _FakeDriver({
        "in_progress": False,
        "dangling": [{"key": "account.account.missing_field", "model": "account.account", "name": "missing_field"}],
    })

    in_progress, dangling = graph_queries.get_field_dependency_chain(driver, "account.account", "account_type")

    assert in_progress is False
    assert dangling == [{"key": "account.account.missing_field", "model": "account.account", "name": "missing_field"}]

    query, params = _last_call(driver)
    assert "DEPENDS_ON" in query.text
    assert "HAS_FIELD" in query.text
    assert "OdooModel" not in query.text  # real schema, not the design doc's invented label
    assert params == {
        "import_metadata_id": "odoo_full_module_graph",
        "model": "account.account",
        "field_name": "account_type",
    }


def test_get_field_dependency_chain_no_row_fails_open():
    driver = _FakeDriver(None)
    in_progress, dangling = graph_queries.get_field_dependency_chain(driver, "x", "y")
    assert in_progress is False
    assert dangling == []


def test_get_field_dependency_chain_import_in_progress():
    driver = _FakeDriver({"in_progress": True, "dangling": []})
    in_progress, dangling = graph_queries.get_field_dependency_chain(driver, "x", "y")
    assert in_progress is True
    assert dangling == []


def test_get_blast_radius_unions_and_dedupes_all_three_legs():
    driver = _FakeDriver({
        "in_progress": False,
        "extends_modules": ["sale", "purchase"],
        "reopens_modules": ["mass_mailing_crm", "sale"],
        "view_modules": ["account"],
        "access_modules": ["account", "sale"],
    })

    in_progress, modules = graph_queries.get_blast_radius(driver, "account.move")

    assert in_progress is False
    assert modules == ["account", "mass_mailing_crm", "purchase", "sale"]  # sorted, deduped


def test_get_blast_radius_excludes_given_module():
    driver = _FakeDriver({
        "in_progress": False,
        "extends_modules": ["sale"],
        "reopens_modules": [],
        "view_modules": [],
        "access_modules": [],
    })

    in_progress, modules = graph_queries.get_blast_radius(driver, "account.move", excluding_module="sale")

    assert modules == []

    query, params = _last_call(driver)
    assert "TARGETS" in query.text
    assert "DECLARES_ACCESS_RULE" in query.text
    assert "REOPENS" in query.text
    assert "via_module" in query.text


def test_get_blast_radius_no_row_fails_open():
    driver = _FakeDriver(None)
    in_progress, modules = graph_queries.get_blast_radius(driver, "x")
    assert in_progress is False
    assert modules == []


def test_get_module_grounding_returns_all_four_facets():
    driver = _FakeDriver({
        "in_progress": False,
        "defines": ["account.account"],
        "reopens": ["crm.lead"],
        "extends_fields": ["res.partner.credit"],
        "declares_views": ["account.view_account_form"],
    })

    in_progress, data = graph_queries.get_module_grounding(driver, "account")

    assert in_progress is False
    assert data == {
        "defines": ["account.account"],
        "reopens": ["crm.lead"],
        "extends_fields": ["res.partner.credit"],
        "declares_views": ["account.view_account_form"],
    }
    query, params = _last_call(driver)
    assert params["module_name"] == "account"


def test_get_module_grounding_no_row_fails_open_to_empty_dict_shape():
    driver = _FakeDriver(None)
    in_progress, data = graph_queries.get_module_grounding(driver, "x")
    assert in_progress is False
    assert data == {"defines": [], "reopens": [], "extends_fields": [], "declares_views": []}


class _FakeWriteResult:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record


class _FakeWriteSession:
    def __init__(self, record):
        self.record = record
        self.run_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, params):
        self.run_calls.append((query, params))
        return _FakeWriteResult(self.record)


class _FakeWriteDriver:
    def __init__(self, record):
        self._session = _FakeWriteSession(record)
        self.session_calls = []

    def session(self, **kwargs):
        self.session_calls.append(kwargs)
        return self._session


def test_update_module_install_state_merges_onto_existing_module_and_returns_true():
    driver = _FakeWriteDriver({"name": "account"})

    ok = graph_queries.update_module_install_state(driver, "account", "installed", "2026-08-13T00:00:00Z")

    assert ok is True
    query, params = driver._session.run_calls[-1]
    assert "SET m.real_install_state" in query.text
    assert "MATCH (m:Module" in query.text
    assert "MERGE" not in query.text or "MATCH (m:Module" in query.text  # matches existing node, never CREATEs a dup
    assert "CREATE" not in query.text
    assert params == {"module_name": "account", "state": "installed", "confirmed_at": "2026-08-13T00:00:00Z"}
    # No import_in_progress gating -- module_name/state/confirmed_at are the only params.
    assert "import_in_progress" not in query.text


def test_update_module_install_state_returns_false_when_module_not_found():
    driver = _FakeWriteDriver(None)
    ok = graph_queries.update_module_install_state(driver, "does_not_exist", "installed", "2026-08-13T00:00:00Z")
    assert ok is False


def test_get_all_module_names_returns_the_full_sweep():
    driver = _FakeDriver({"in_progress": False, "names": ["account", "sale", "base"]})
    in_progress, names = graph_queries.get_all_module_names(driver)
    assert in_progress is False
    assert names == ["account", "sale", "base"]


def test_get_all_module_names_no_row_fails_open():
    driver = _FakeDriver(None)
    in_progress, names = graph_queries.get_all_module_names(driver)
    assert in_progress is False
    assert names == []


def test_get_module_real_install_state_returns_the_stored_property():
    driver = _FakeDriver({"in_progress": False, "state": "installed"})
    in_progress, state = graph_queries.get_module_real_install_state(driver, "account")
    assert in_progress is False
    assert state == "installed"


def test_get_module_real_install_state_none_when_never_written():
    driver = _FakeDriver({"in_progress": False, "state": None})
    in_progress, state = graph_queries.get_module_real_install_state(driver, "account")
    assert in_progress is False
    assert state is None


def test_get_model_existence_sends_expected_query_and_params():
    driver = _FakeDriver({
        "in_progress": False,
        "found_name": "oma.badge.scan.log",
        "defining_modules": ["oma_badge_scan_tracker", "oma_badge_scan_tracker"],
        "field_count": 3,
    })

    in_progress, result = graph_queries.get_model_existence(driver, "oma.badge.scan.log")

    assert in_progress is False
    assert result == {
        "technical_name": "oma.badge.scan.log",
        "defining_modules": ["oma_badge_scan_tracker"],
        "field_count": 3,
    }
    query, params = _last_call(driver)
    assert "HAS_FIELD" in query.text
    assert "DEFINES" in query.text
    assert "REOPENS" in query.text
    assert "WHERE NOT in_progress" in query.text
    assert params == {"import_metadata_id": "odoo_full_module_graph", "technical_name": "oma.badge.scan.log"}


def test_get_model_existence_model_not_found_returns_none():
    driver = _FakeDriver({
        "in_progress": False, "found_name": None, "defining_modules": [], "field_count": 0,
    })
    in_progress, result = graph_queries.get_model_existence(driver, "oma.does.not.exist")
    assert in_progress is False
    assert result is None


def test_get_model_existence_import_in_progress_returns_none_not_a_false_hit():
    driver = _FakeDriver({
        "in_progress": True, "found_name": "oma.badge.scan.log",
        "defining_modules": ["x"], "field_count": 1,
    })
    in_progress, result = graph_queries.get_model_existence(driver, "oma.badge.scan.log")
    assert in_progress is True
    assert result is None


def test_get_model_existence_no_row_fails_open():
    driver = _FakeDriver(None)
    in_progress, result = graph_queries.get_model_existence(driver, "oma.badge.scan.log")
    assert in_progress is False
    assert result is None
