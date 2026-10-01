"""Phase 35 §17.3.1: tests for manager/loop.py's _log_decomposition_blast_radius() -- the
decomposition-level plan blast-radius check. Mocks the graph driver and compute_change_radius()
directly, zero live graph calls.
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module
from manager.graph_governance_flags import GateMode


def _piece(creates=None, requires=None):
    return SimpleNamespace(creates=creates or [], requires=requires or [])


def _run(goal, pieces, task_id="t1"):
    return asyncio.run(loop_module._log_decomposition_blast_radius(goal, pieces, task_id))


def test_no_touched_models_is_a_silent_noop():
    with patch("infra.neo4j_client.get_neo4j_read_driver") as mock_driver, \
         patch("tools_odoo.knowledge_graph.build_safety_grounding.compute_change_radius") as mock_ccr, \
         patch.object(loop_module, "publish_trace_event") as mock_publish:
        _run("Please improve things generally.", [_piece()])
    mock_driver.assert_not_called()
    mock_ccr.assert_not_called()
    mock_publish.assert_not_called()


def test_disabled_gate_never_even_computes():
    with patch("manager.loop.get_gate_mode", return_value=GateMode.DISABLED), \
         patch("infra.neo4j_client.get_neo4j_read_driver") as mock_driver:
        _run("Add a field to crm.lead", [_piece(creates=["crm.lead.foo"])])
    mock_driver.assert_not_called()


def test_real_radius_logged_at_log_only_never_blocks():
    fake_radius = SimpleNamespace(in_progress=False, affected_module_count=2, affected_modules=["mod_a", "mod_b"])
    with patch("manager.loop.get_gate_mode", return_value=GateMode.LOG_ONLY), \
         patch("infra.neo4j_client.get_neo4j_read_driver", return_value=object()), \
         patch(
             "tools_odoo.knowledge_graph.build_safety_grounding.compute_change_radius",
             return_value=fake_radius,
         ), \
         patch.object(loop_module, "publish_trace_event") as mock_publish:
        _run("Add a field to crm.lead", [_piece(creates=["crm.lead.foo"])])
    assert mock_publish.called
    event = mock_publish.call_args[0][1]
    assert "mod_a" in event["message"]
    assert "log_only" in event["message"]


def test_in_progress_radius_is_not_reported_as_a_confirmed_result():
    fake_radius = SimpleNamespace(in_progress=True, affected_module_count=0, affected_modules=[])
    with patch("manager.loop.get_gate_mode", return_value=GateMode.LOG_ONLY), \
         patch("infra.neo4j_client.get_neo4j_read_driver", return_value=object()), \
         patch(
             "tools_odoo.knowledge_graph.build_safety_grounding.compute_change_radius",
             return_value=fake_radius,
         ), \
         patch.object(loop_module, "publish_trace_event") as mock_publish:
        _run("Add a field to crm.lead", [_piece(creates=["crm.lead.foo"])])
    mock_publish.assert_not_called()


def test_graph_failure_fails_open_and_logs_the_failure_not_a_crash():
    with patch("manager.loop.get_gate_mode", return_value=GateMode.LOG_ONLY), \
         patch("infra.neo4j_client.get_neo4j_read_driver", side_effect=RuntimeError("no graph")), \
         patch.object(loop_module, "publish_trace_event") as mock_publish:
        _run("Add a field to crm.lead", [_piece(creates=["crm.lead.foo"])])  # must not raise
    assert mock_publish.called
    event = mock_publish.call_args[0][1]
    assert "failed open" in event["message"]


def test_extracts_touched_models_from_creates_and_requires_and_raw_goal():
    fake_radius = SimpleNamespace(in_progress=False, affected_module_count=0, affected_modules=[])
    captured = {}

    def fake_compute(driver, touched_models, excluding_module):
        captured["touched_models"] = touched_models
        captured["excluding_module"] = excluding_module
        return fake_radius

    with patch("manager.loop.get_gate_mode", return_value=GateMode.LOG_ONLY), \
         patch("infra.neo4j_client.get_neo4j_read_driver", return_value=object()), \
         patch(
             "tools_odoo.knowledge_graph.build_safety_grounding.compute_change_radius",
             side_effect=fake_compute,
         ):
        _run(
            "Also touch res.partner while at it",
            [_piece(creates=["crm.lead.foo"], requires=["hr.leave.state"])],
        )
    # The deterministic scan captures the whole dotted token as-is (same behavior as
    # manager/intake_grounding.py's identical regex) -- it can't distinguish a 3-segment
    # "model.field" string from a genuine 3-segment model technical name without a schema
    # lookup. compute_change_radius() harmlessly returns no match for a non-model token
    # (get_blast_radius() queries by exact technical_name), so passing the raw token through
    # is safe, just imprecise -- documented here rather than silently assumed.
    assert "crm.lead.foo" in captured["touched_models"]
    assert "hr.leave.state" in captured["touched_models"]
    assert "res.partner" in captured["touched_models"]
    assert captured["excluding_module"] is None
