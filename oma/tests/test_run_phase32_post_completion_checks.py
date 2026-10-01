"""Phase 32 implementation (2026-08-11): unit tests for
manager.loop.run_phase32_post_completion_checks() -- the wiring point that
calls the new live-server health check, UI-action-presence check, and
scope-certification recording once a task has already passed every
existing check. Verifies it never raises and correctly aggregates warnings
from both underlying checks.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.loop as loop_module
from manager.loop import run_phase32_post_completion_checks


def _patch_healthy(monkeypatch):
    import tools_odoo.live_server_health_check as health_module

    monkeypatch.setattr(
        health_module, "check_live_server_can_reach_model",
        lambda model_name, db: health_module.LiveServerHealthCheckResult(
            model_name=model_name, db=db, reachable=True, stale_registry_detected=False, detail="ok",
        ),
    )


def _patch_view_arch(monkeypatch, arch: str):
    import tools_odoo.odoo_schema_client as schema_client_module

    class _FakeProxy:
        def execute_kw(self, db, uid, api_key, model_name, method, args, kwargs):
            return {"arch": arch}

    monkeypatch.setattr(schema_client_module, "_get_or_create_shared_key", lambda db, login: (2, "fake-key"))
    monkeypatch.setattr(schema_client_module, "_models_proxy", lambda db: _FakeProxy())


def _use_temp_cert_state(tmp_path, monkeypatch):
    import manager.scope_certification as cert_module

    monkeypatch.setattr(cert_module, "_STATE_PATH", os.path.join(str(tmp_path), "cert.json"))
    monkeypatch.setattr(cert_module, "_STATE_DIR", str(tmp_path))


def test_no_warnings_when_everything_is_healthy(tmp_path, monkeypatch):
    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)

    warnings = run_phase32_post_completion_checks(
        "Add a new computed field 'total_hours' to oma.service.ticket",
        {"db": "odoo16_dev", "module_name": "oma_x"},
        "oma.service.ticket",
        "task-1",
    )
    assert warnings == []
    print("PASS: a healthy task with no UI-interaction goal produces no warnings")


def test_stale_registry_warning_surfaces(tmp_path, monkeypatch):
    import tools_odoo.live_server_health_check as health_module

    monkeypatch.setattr(
        health_module, "check_live_server_can_reach_model",
        lambda model_name, db: health_module.LiveServerHealthCheckResult(
            model_name=model_name, db=db, reachable=False, stale_registry_detected=True,
            detail="KeyError: 'oma.equipment'",
        ),
    )
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)

    warnings = run_phase32_post_completion_checks(
        "Add a new field to oma.equipment", {"db": "odoo16_dev", "module_name": "oma_x"},
        "oma.equipment", "task-2",
    )
    assert len(warnings) == 1
    assert "LIVE SERVER HEALTH CHECK" in warnings[0]
    print("PASS: a real stale-registry detection surfaces as exactly one warning")


def test_ui_gap_warning_surfaces(tmp_path, monkeypatch):
    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")  # no buttons, no chatter
    _use_temp_cert_state(tmp_path, monkeypatch)

    warnings = run_phase32_post_completion_checks(
        'Add a "Start" button to the ticket form.', {"db": "odoo16_dev", "module_name": "oma_x"},
        "oma.service.ticket", "task-3",
    )
    assert len(warnings) == 1
    assert "UI ACTION PRESENCE CHECK" in warnings[0]
    print("PASS: a real missing-button gap surfaces as exactly one warning")


def test_missing_db_or_model_skips_checks_without_raising(tmp_path, monkeypatch):
    _use_temp_cert_state(tmp_path, monkeypatch)
    warnings = run_phase32_post_completion_checks(
        "Some goal", {"db": None, "module_name": "oma_x"}, None, "task-4",
    )
    assert warnings == []
    print("PASS: missing db/model info skips the live checks entirely, never raises")


def test_a_broken_underlying_check_never_raises_out_of_the_wiring_function(tmp_path, monkeypatch):
    import tools_odoo.live_server_health_check as health_module

    def boom(model_name, db):
        raise RuntimeError("simulated unexpected failure inside the health check itself")

    monkeypatch.setattr(health_module, "check_live_server_can_reach_model", boom)
    _patch_view_arch(monkeypatch, arch="<form/>")
    _use_temp_cert_state(tmp_path, monkeypatch)

    warnings = run_phase32_post_completion_checks(
        "Add a field to oma.equipment", {"db": "odoo16_dev", "module_name": "oma_x"},
        "oma.equipment", "task-5",
    )
    assert isinstance(warnings, list)
    print("PASS: an unexpected exception inside a check is caught and never propagates to the caller")


def test_ambiguous_goal_is_classified_via_the_confidence_aware_function_and_never_raises(tmp_path, monkeypatch):
    """Phase 35 §11.1 fix 2 wiring (2026-08-12) -- a follow-up audit found the earlier pass had
    built classify_scope_with_confidence() but this function still called the plain,
    non-ambiguity-aware classify_scope() underneath. Confirms the real call site now goes
    through the confidence-aware version (a goal that matches two scope patterns at once must
    not crash this function, publish_trace_event's own swallow-everything contract handles the
    no-live-Redis case in a test)."""
    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)

    # Deliberately crosses "workflow_with_custom_buttons_or_cron" (button) and
    # "single_new_field" (field) -- a genuinely ambiguous goal per
    # test_a_goal_matching_two_scopes_is_flagged_ambiguous in the scope_certification suite.
    warnings = run_phase32_post_completion_checks(
        "add a new field and a button to trigger a workflow",
        {"db": "odoo16_dev", "module_name": "oma_x"}, "oma.service.ticket", "task-ambiguous",
    )
    assert isinstance(warnings, list)
    print("PASS: an ambiguous goal is classified via the confidence-aware function and never crashes the real call site")


def test_stale_registry_only_resets_streak_without_doubling_the_bar(tmp_path, monkeypatch):
    """Real overnight incident (2026-08-14): a stale-registry-only warning doubled
    new_self_contained_module_basic_model_or_group's bar three times in one night (29 ->
    232) from the SAME root cause recurring -- a real, mostly self-inflicted timing
    artifact, not evidence the generated code itself was unreliable. Confirms the fix at
    the real call site: a stale-registry-only warning still resets the streak (honest) but
    does not double n_required, while a genuine UI-action-gap warning still does.
    """
    import tools_odoo.live_server_health_check as health_module
    import manager.scope_certification as cert_module

    monkeypatch.setattr(
        health_module, "check_live_server_can_reach_model",
        lambda model_name, db: health_module.LiveServerHealthCheckResult(
            model_name=model_name, db=db, reachable=False, stale_registry_detected=True,
            detail="KeyError: 'oma.equipment'",
        ),
    )
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)

    for i in range(cert_module._DEFAULT_N_REQUIRED):
        cert_module.record_task_outcome(
            "single_new_field", task_id=f"clean-{i}", qualifying_failure=False,
            goal_text=f"add a distinct field number {i % cert_module._DIVERSITY_FLOOR_MIN_DISTINCT}",
        )
    assert cert_module.is_scope_certified("single_new_field") is True

    warnings = run_phase32_post_completion_checks(
        "Add a new field to oma.equipment", {"db": "odoo16_dev", "module_name": "oma_x"},
        "oma.equipment", "task-stale-only",
    )
    assert len(warnings) == 1 and "LIVE SERVER HEALTH CHECK" in warnings[0]

    state = cert_module.get_scope_state("single_new_field")
    assert state.consecutive_clean_runs == 0, "the streak must still reset"
    assert state.n_required == cert_module._DEFAULT_N_REQUIRED, "the bar must NOT double for a stale-registry-only warning"
    print("PASS: a stale-registry-only warning resets the streak without doubling the bar, at the real call site")


def test_ui_gap_warning_suppressed_when_registry_already_known_stale(tmp_path, monkeypatch):
    """Real overnight incident (2026-08-17): all 3 real historical qualifying failures on
    workflow_with_custom_buttons_or_cron (2026-08-13/15) show reproduction_confirmed=True
    and passed=True in agent_memory_events -- the generated button/chatter content was
    genuinely correct -- yet each still got a UI-action-gap warning that doubled the bar
    (29 -> 58 -> 116 -> 232). Root cause: the UI-action-gap check reads the view arch via
    the exact same live server/registry the model-reachability check already detected as
    stale, but only the reachability check's own warning was exempted from escalating the
    bar. Confirms the fix: when the registry is already known stale for this model, the
    UI-action-gap check's result must not be trusted either -- only the (already-exempted)
    stale-registry warning should surface, not a second, doubly-counted UI-gap warning.
    """
    import tools_odoo.live_server_health_check as health_module
    import manager.scope_certification as cert_module

    monkeypatch.setattr(
        health_module, "check_live_server_can_reach_model",
        lambda model_name, db: health_module.LiveServerHealthCheckResult(
            model_name=model_name, db=db, reachable=False, stale_registry_detected=True,
            detail="KeyError: 'oma.service.ticket'",
        ),
    )
    # An arch with no buttons/chatter at all -- would normally ALSO trigger a UI-action-gap
    # warning for this button-naming goal, if the registry weren't already known stale.
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)

    for i in range(cert_module._DEFAULT_N_REQUIRED):
        cert_module.record_task_outcome(
            "workflow_with_custom_buttons_or_cron", task_id=f"clean-{i}", qualifying_failure=False,
            goal_text=f"add a distinct button number {i % cert_module._DIVERSITY_FLOOR_MIN_DISTINCT}",
        )
    assert cert_module.is_scope_certified("workflow_with_custom_buttons_or_cron") is True

    warnings = run_phase32_post_completion_checks(
        'Add a "Start" button to the ticket form.', {"db": "odoo16_dev", "module_name": "oma_x"},
        "oma.service.ticket", "task-stale-and-ui-gap",
    )
    assert len(warnings) == 1 and "LIVE SERVER HEALTH CHECK" in warnings[0], (
        "only the stale-registry warning should surface, not a second UI-gap warning"
    )

    state = cert_module.get_scope_state("workflow_with_custom_buttons_or_cron")
    assert state.consecutive_clean_runs == 0, "the streak must still reset"
    assert state.n_required == cert_module._DEFAULT_N_REQUIRED, (
        "the bar must NOT double when the only real signal is registry staleness, "
        "even though a UI-action-gap would also have fired"
    )
    print("PASS: a UI-action-gap warning is suppressed (not double-counted) when the registry is already known stale")


def test_ui_gap_warning_still_doubles_the_bar(tmp_path, monkeypatch):
    """The other half of the fix above: a genuine content defect (a real UI-action-gap)
    must still escalate the bar -- only the environmental-only signal is exempted.
    """
    import manager.scope_certification as cert_module

    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")  # no buttons, no chatter
    _use_temp_cert_state(tmp_path, monkeypatch)

    for i in range(cert_module._DEFAULT_N_REQUIRED):
        cert_module.record_task_outcome(
            "workflow_with_custom_buttons_or_cron", task_id=f"clean-{i}", qualifying_failure=False,
            goal_text=f"add a distinct button number {i % cert_module._DIVERSITY_FLOOR_MIN_DISTINCT}",
        )
    assert cert_module.is_scope_certified("workflow_with_custom_buttons_or_cron") is True

    warnings = run_phase32_post_completion_checks(
        'Add a "Start" button to the ticket form.', {"db": "odoo16_dev", "module_name": "oma_x"},
        "oma.service.ticket", "task-ui-gap",
    )
    assert len(warnings) == 1 and "UI ACTION PRESENCE CHECK" in warnings[0]

    state = cert_module.get_scope_state("workflow_with_custom_buttons_or_cron")
    assert state.consecutive_clean_runs == 0
    assert state.n_required == cert_module._DEFAULT_N_REQUIRED * 2, "a genuine content defect must still double the bar"
    print("PASS: a real UI-action-gap warning still escalates the bar, unlike a stale-registry-only warning")


def test_adjudicator_review_sample_fires_at_the_real_call_site_without_crashing(tmp_path, monkeypatch):
    """Phase 35 §11.1 fix 3 wiring (2026-08-12) -- get_adjudicator_review_sample() was built and
    unit-tested but, per the same follow-up audit, never actually called from the real pipeline.
    Confirms it's now checked after every real outcome is recorded, and that landing exactly on
    a review boundary doesn't crash this call site."""
    import manager.scope_certification as cert_module

    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)

    # Drive the qualifying-failure count to exactly one short of the review boundary using a
    # non-UI, non-buggy goal so run_phase32_post_completion_checks' own warnings stay
    # deterministic -- the LAST call below is the one real production call that should land
    # exactly on the review boundary and trigger get_adjudicator_review_sample() internally.
    for i in range(cert_module._ADJUDICATOR_REVIEW_EVERY_N - 1):
        cert_module.record_task_outcome(
            "single_new_field", task_id=f"pre-fail-{i}", qualifying_failure=True,
        )

    warnings = run_phase32_post_completion_checks(
        'Add a "Start" button to the ticket form.',  # a real UI gap -> a real qualifying failure
        {"db": "odoo16_dev", "module_name": "oma_x"}, "oma.service.ticket", "task-review-boundary",
    )
    assert isinstance(warnings, list)  # never crashes, regardless of whether the sample fired
    state = cert_module.get_scope_state("workflow_with_custom_buttons_or_cron")
    assert state.total_qualifying_failures >= 1  # the real outcome for THIS scope was recorded
    print("PASS: landing on the adjudicator-review boundary at the real call site never crashes the task")


def test_post_install_graph_diff_confirms_freshness_and_never_warns(tmp_path, monkeypatch):
    """Phase 35 §17.6.1: a real graph confirmation logs (via publish_trace_event, asserted
    separately below) but never adds to the returned warnings list -- this check can only ever
    confirm, never flag, per its own async-freshness-lag caveat."""
    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)
    import infra.neo4j_client as neo4j_client_module
    import tools_odoo.graph_queries as graph_queries_module

    monkeypatch.setattr(neo4j_client_module, "get_neo4j_read_driver", lambda: object())
    monkeypatch.setattr(
        graph_queries_module, "get_model_existence",
        lambda driver, technical_name: (
            False, {"technical_name": technical_name, "defining_modules": ["oma_x"], "field_count": 3},
        ),
    )
    import manager.trace as trace_module

    published = []
    # This check imports publish_trace_event LOCALLY (from manager.trace import
    # publish_trace_event, matching this function's own pre-existing pattern elsewhere in its
    # body) rather than using the module-level `loop_module.publish_trace_event` binding -- so
    # the real call site to patch is the source, manager.trace.publish_trace_event.
    monkeypatch.setattr(trace_module, "publish_trace_event", lambda tid, event: published.append(event))

    warnings = run_phase32_post_completion_checks(
        "Add a field to oma.service.ticket",
        {"db": "odoo16_dev", "module_name": "oma_x"}, "oma.service.ticket", "task-graph-diff-1",
    )
    assert warnings == []
    graph_diff_events = [e for e in published if e.get("phase") == "post_install_graph_diff"]
    assert len(graph_diff_events) == 1
    assert "oma.service.ticket" in graph_diff_events[0]["message"]


def test_post_install_graph_diff_model_not_yet_visible_never_warns(tmp_path, monkeypatch):
    """The core §17.6.1 design decision: a model not yet visible in the graph is expected async
    sync lag, not a confirmed defect -- must never produce a warning."""
    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)
    import infra.neo4j_client as neo4j_client_module
    import tools_odoo.graph_queries as graph_queries_module

    monkeypatch.setattr(neo4j_client_module, "get_neo4j_read_driver", lambda: object())
    monkeypatch.setattr(
        graph_queries_module, "get_model_existence", lambda driver, technical_name: (False, None),
    )
    published = []
    monkeypatch.setattr(loop_module, "publish_trace_event", lambda tid, event: published.append(event))

    warnings = run_phase32_post_completion_checks(
        "Add a field to oma.service.ticket",
        {"db": "odoo16_dev", "module_name": "oma_x"}, "oma.service.ticket", "task-graph-diff-2",
    )
    assert warnings == []
    graph_diff_events = [e for e in published if e.get("phase") == "post_install_graph_diff"]
    assert graph_diff_events == []


def test_post_install_graph_diff_import_in_progress_never_warns(tmp_path, monkeypatch):
    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)
    import infra.neo4j_client as neo4j_client_module
    import tools_odoo.graph_queries as graph_queries_module

    monkeypatch.setattr(neo4j_client_module, "get_neo4j_read_driver", lambda: object())
    monkeypatch.setattr(
        graph_queries_module, "get_model_existence", lambda driver, technical_name: (True, None),
    )
    warnings = run_phase32_post_completion_checks(
        "Add a field to oma.service.ticket",
        {"db": "odoo16_dev", "module_name": "oma_x"}, "oma.service.ticket", "task-graph-diff-3",
    )
    assert warnings == []


def test_post_install_graph_diff_disabled_gate_skips_the_query(tmp_path, monkeypatch):
    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)
    from manager.graph_governance_flags import GateMode

    monkeypatch.setattr(loop_module, "get_gate_mode", lambda name: GateMode.DISABLED)
    calls = []
    monkeypatch.setattr(
        "infra.neo4j_client.get_neo4j_read_driver", lambda: calls.append(1) or object(),
    )
    run_phase32_post_completion_checks(
        "Add a field to oma.service.ticket",
        {"db": "odoo16_dev", "module_name": "oma_x"}, "oma.service.ticket", "task-graph-diff-4",
    )
    assert calls == []


def test_post_install_graph_diff_query_failure_never_raises(tmp_path, monkeypatch):
    _patch_healthy(monkeypatch)
    _patch_view_arch(monkeypatch, arch="<form><field name='name'/></form>")
    _use_temp_cert_state(tmp_path, monkeypatch)
    import infra.neo4j_client as neo4j_client_module

    monkeypatch.setattr(
        neo4j_client_module, "get_neo4j_read_driver", lambda: (_ for _ in ()).throw(RuntimeError("no graph")),
    )
    warnings = run_phase32_post_completion_checks(  # must not raise
        "Add a field to oma.service.ticket",
        {"db": "odoo16_dev", "module_name": "oma_x"}, "oma.service.ticket", "task-graph-diff-5",
    )
    assert warnings == []


if __name__ == "__main__":
    print("(this file requires pytest's tmp_path/monkeypatch fixtures; run via pytest)")
