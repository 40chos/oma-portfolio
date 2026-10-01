"""Phase 35 §15.9 (kill-switch/registry) and §15.11 (collision
pre-check) unit tests. Registry tests use a temp state file, never the
real state/deterministic_generators.json -- same discipline
tests/test_scope_certification.py already applies to its own state.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager.deterministic_generators.registry as registry_module
from manager.deterministic_generators.collision import deterministic_edit_survives_prior_edits


def test_registry_defaults_to_not_live(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_module, "_STATE_PATH", str(tmp_path / "gen.json"))
    assert registry_module.is_generator_live("single_new_field") is False


def test_registry_enable_then_disable(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_module, "_STATE_PATH", str(tmp_path / "gen.json"))
    registry_module.enable_generator("single_new_field", reason="test", actor="test-actor")
    assert registry_module.is_generator_live("single_new_field") is True
    state = registry_module.get_generator_state("single_new_field")
    assert state["enabled_by"] == "test-actor"

    registry_module.disable_generator("single_new_field", reason="test kill-switch", actor="test-adjudicator")
    assert registry_module.is_generator_live("single_new_field") is False
    state = registry_module.get_generator_state("single_new_field")
    assert state["disabled_by"] == "test-adjudicator"


def test_collision_no_prior_edits_survives():
    assert deterministic_edit_survives_prior_edits("class X:\n    pass\n", "class X:\n    pass\n", []) is True


def test_collision_unrelated_prior_edit_survives():
    file_before = "class X:\n    pass\n\nclass Y:\n    pass\n"
    prior_edits = [{"operation": "search_replace", "target": "class Y:\n    pass\n", "content": "class Y:\n    z = 1\n"}]
    assert deterministic_edit_survives_prior_edits("class X:\n    pass\n", file_before, prior_edits) is True


def test_collision_prior_edit_removes_the_target():
    file_before = "class X:\n    pass\n"
    prior_edits = [{"operation": "search_replace", "target": "class X:\n    pass\n", "content": "class X:\n    other = 1\n"}]
    # The deterministic edit's own target no longer exists after the prior edit -- real collision.
    assert deterministic_edit_survives_prior_edits("class X:\n    pass\n", file_before, prior_edits) is False


def test_collision_prior_edit_creates_a_duplicate_of_the_target():
    file_before = "class X:\n    pass\n\nclass Z:\n    other = 1\n"
    prior_edits = [{"operation": "search_replace", "target": "class Z:\n    other = 1\n", "content": "class X:\n    pass\n"}]
    # After the prior edit, "class X:\n    pass\n" now appears twice -- ambiguous, must refuse.
    assert deterministic_edit_survives_prior_edits("class X:\n    pass\n", file_before, prior_edits) is False
