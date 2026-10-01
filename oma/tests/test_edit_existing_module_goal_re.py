"""Phase 35 fix (2026-08-13 overnight) -- manager.loop._EDIT_EXISTING_MODULE_GOAL_RE broadened
to also recognize "...NOT a new [extension] module" phrasing, not just "already-installed
module"/"edit the existing...module". Real, confirmed root cause found live: this regex mirrors
manager.scope_certification.classify_scope()'s own edit_existing_module_access_or_files trigger
phrase on purpose -- meaning a goal deliberately phrased to avoid THAT trigger (so it can be
real evidence for a different, not-yet-certified scope) also, as an unintended side effect,
never got the strong `edit_existing_module:` signal here, falling back to the weaker
`depends_on_module:` marker -- confirmed live, repeatedly, NOT reliable enough to stop Build
from scaffolding a brand-new, disconnected module even when the goal explicitly said "in that
module's own models/models.py file, NOT a new extension module."
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.loop import _EDIT_EXISTING_MODULE_GOAL_RE  # noqa: E402


def test_matches_the_original_already_installed_module_phrasing():
    goal = "Edit the existing, already-installed module oma_x directly: add a field..."
    assert _EDIT_EXISTING_MODULE_GOAL_RE.search(goal)
    print("PASS: still matches the original 'already-installed module' phrasing")


def test_matches_the_original_edit_the_existing_phrasing():
    goal = "Please edit the existing project.task module to add a new field."
    assert _EDIT_EXISTING_MODULE_GOAL_RE.search(goal)
    print("PASS: still matches the original 'edit the existing...module' phrasing")


def test_now_matches_the_not_a_new_extension_module_phrasing():
    goal = (
        "Add a new field named 'x' (Char) to the res.partner model, in the oma_y module's "
        "own models/models.py file, NOT a new extension module. Do not add any view changes."
    )
    assert _EDIT_EXISTING_MODULE_GOAL_RE.search(goal), (
        "the real, live-confirmed 2026-08-13 phrasing must now trigger the strong "
        "edit_existing_module: signal"
    )
    print("PASS: now matches the real single_new_field-batch phrasing ('NOT a new extension module')")


def test_still_does_not_match_a_goal_with_no_edit_in_place_signal_at_all():
    goal = "Build a brand new module that defines a new custom model for tracking widgets."
    assert not _EDIT_EXISTING_MODULE_GOAL_RE.search(goal), (
        "a genuinely new-module goal must never accidentally trigger the edit-in-place signal"
    )
    print("PASS: a genuinely new-module goal is correctly never matched")


if __name__ == "__main__":
    test_matches_the_original_already_installed_module_phrasing()
    test_matches_the_original_edit_the_existing_phrasing()
    test_now_matches_the_not_a_new_extension_module_phrasing()
    test_still_does_not_match_a_goal_with_no_edit_in_place_signal_at_all()
    print("ALL PASSED")
