"""Phase 33 follow-up (2026-08-11): unit tests for
specialists/build/specialist.py's _goal_states_field_group_restriction() and its
wiring into _render_view_field_tags() -- routing an ACL restriction through the
SAME deterministic, never-guesses-a-reference view-XML builder that already
handles readonly/field insertion, closing the whole family of live-confirmed
downstream bugs (wrong group, display name vs. external ID, fabricated view
xmlid) at their shared root instead of patching each symptom separately.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    _goal_states_field_group_restriction,
    _render_view_field_tags,
)

_REAL_GOAL = (
    "On the meerwerk form, only Administrators should see the internal audit note field.\n\n"
    "Module: project_meerwerk\nModel: project.meerwerk (inherit)\nField: internal_audit_note (Text)\n"
)


def _patch_group_checks(monkeypatch, external_id="base.group_system"):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: name == "Settings")
    monkeypatch.setattr(schema_client_module, "resolve_group_external_id_fast", lambda db, name: external_id)


def test_resolves_the_real_external_id_for_the_named_field(monkeypatch):
    _patch_group_checks(monkeypatch)
    result = _goal_states_field_group_restriction(_REAL_GOAL, "internal_audit_note", "project.meerwerk", "odoo16_dev")
    assert result == "base.group_system"
    print("PASS: resolves the real external ID for the exact field the goal names")


def test_a_different_field_on_the_same_goal_is_not_restricted(monkeypatch):
    _patch_group_checks(monkeypatch)
    result = _goal_states_field_group_restriction(_REAL_GOAL, "some_other_field", "project.meerwerk", "odoo16_dev")
    assert result is None
    print("PASS: a field the goal does NOT name for restriction gets None, never a guess")


def test_no_acl_shape_in_goal_returns_none():
    result = _goal_states_field_group_restriction(
        "Add a field to project.meerwerk.", "some_field", "project.meerwerk", "odoo16_dev",
    )
    assert result is None
    print("PASS: a goal with no ACL-restriction shape returns None")


def test_unresolvable_group_returns_none_never_a_guess(monkeypatch):
    import tools_odoo.odoo_schema_client as schema_client_module
    monkeypatch.setattr(schema_client_module, "check_group_exists_fast", lambda db, name: False)
    result = _goal_states_field_group_restriction(_REAL_GOAL, "internal_audit_note", "project.meerwerk", "odoo16_dev")
    assert result is None
    print("PASS: an unresolvable group returns None rather than a guessed value")


def test_render_view_field_tags_includes_the_real_external_id(monkeypatch):
    _patch_group_checks(monkeypatch)
    result = _render_view_field_tags(["internal_audit_note"], _REAL_GOAL, model_name="project.meerwerk", db="odoo16_dev")
    assert 'groups="base.group_system"' in result
    print("PASS: the rendered field tag carries the real, resolved external ID")


def test_render_view_field_tags_never_restricts_when_model_or_db_missing(monkeypatch):
    _patch_group_checks(monkeypatch)
    result = _render_view_field_tags(["internal_audit_note"], _REAL_GOAL)
    assert "groups=" not in result
    print("PASS: without model_name/db, the ACL check is skipped entirely rather than guessing")


def test_render_view_field_tags_combines_readonly_and_group_restriction(monkeypatch):
    _patch_group_checks(monkeypatch)
    goal = _REAL_GOAL.replace("(Text)", "(Text, readonly)")
    result = _render_view_field_tags(["internal_audit_note"], goal, model_name="project.meerwerk", db="odoo16_dev")
    assert 'readonly="1"' in result and 'groups="base.group_system"' in result
    print("PASS: readonly and group restriction combine correctly on the same field tag")


if __name__ == "__main__":
    print("(this file requires pytest's monkeypatch fixture; run via pytest)")
