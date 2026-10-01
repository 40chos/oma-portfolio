"""Real, confirmed bug found live (2026-08-10, task 4724a61f, the flagship task's own dedicated
follow-up correction task): `detect_existing_custom_module_target()`'s candidate list came ONLY
from `list_custom_site_module_names()` (`/opt/site/site16`, genuine external customer modules),
so a plain-text goal explicitly naming this pipeline's OWN previously scaffolded module
(`oma_build_a_complete_field_ab52b7f8`, already real and installed under `/mnt/extra-addons`)
was never even offered as a candidate -- `existing_module_dependency` silently resolved to None,
`contract.inputs` stayed `[]`, and Build scaffolded a brand-new, disconnected module with a
manifest `depends: ['base']` that never declared the real dependency, later failing sandbox
install with "Model not found: oma.equipment". See manager/scope_detection.py's
`detect_existing_custom_module_target()` and `_fuzzy_match_existing_custom_module()`, and
tools_odoo/module_dev/toolchain.py's `list_own_scaffolded_module_names()`/
`is_own_scaffolded_module()`, for the full incident and fix.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.scope_detection import _fuzzy_match_existing_custom_module

_CANDIDATES = ["oma_build_a_complete_field_ab52b7f8", "project_meerwerk", "mis_base_extend"]


def test_exact_full_name_verbatim_in_goal_matches_even_when_no_single_token_would():
    goal = (
        "Extend the existing field-service operations module "
        "oma_build_a_complete_field_ab52b7f8 (already installed, on top of project.project, "
        "with models oma.equipment and oma.service.ticket) to fix three specific things that "
        "were missed when it was originally built."
    )
    assert _fuzzy_match_existing_custom_module(goal, _CANDIDATES) == "oma_build_a_complete_field_ab52b7f8"
    print("PASS: exact verbatim full module name in goal text is matched")


def test_still_matches_single_distinctive_token_case():
    assert _fuzzy_match_existing_custom_module(
        "the meerwerk form needs a new field", _CANDIDATES,
    ) == "project_meerwerk"
    print("PASS: existing single-distinctive-token fuzzy match still works")


def test_no_match_returns_none():
    assert _fuzzy_match_existing_custom_module("build something totally new", _CANDIDATES) is None
    print("PASS: unrelated goal text still returns None")


def test_exact_match_is_word_boundary_guarded_not_a_bare_substring():
    # A longer real-world module name must not spuriously match as a substring of another token.
    candidates = ["oma_foo", "oma_foo_bar"]
    goal = "extend oma_foo_bar please"
    result = _fuzzy_match_existing_custom_module(goal, candidates)
    assert result == "oma_foo_bar", f"expected exact longer match, got {result!r}"
    print("PASS: word-boundary-guarded exact match picks the real, full name, not a truncation")


if __name__ == "__main__":
    test_exact_full_name_verbatim_in_goal_matches_even_when_no_single_token_would()
    test_still_matches_single_distinctive_token_case()
    test_no_match_returns_none()
    test_exact_match_is_word_boundary_guarded_not_a_bare_substring()
    print("\nALL DETECT-OWN-SCAFFOLDED-MODULE-TARGET TESTS PASSED")
