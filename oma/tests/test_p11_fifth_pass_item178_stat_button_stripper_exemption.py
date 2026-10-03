"""P11 fifth pass item 178 (docs/planning/PHASE30_SECOND_PASS_FINAL_CONSOLIDATED_2026-07-30.md
§1.1): a stat-button-shaped goal must never have its own generated views_xml nulled out by
_autofix_strip_unrequested_views_xml_for_pure_behavior_task() -- that content is exactly what the
later stat-button autofix (_autofix_goal_named_stat_button_missing) needs to rebuild around two
steps later. Real bug this closes: a stat-button goal satisfies NEITHER of the stripper's own
original two exemptions (no `View:`-family metadata line, no new field via
_extract_inherited_field_names since the stat button's own Integer field may not be declared yet)
-- so the stripper would null out real, already-correct view content, and the stat-button
autofix's own `if not generated.views_xml: return` guard would then silently no-op.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)

_STAT_BUTTON_GOAL = (
    "On the project form, I want to see a button in the top-right corner that shows how many "
    "fieldjob records exist for this project.\n\n"
    "Field: fieldjob_count (Integer, computed)\n"
    "Action method: action_view_fieldjob\n"
)

_REAL_VIEWS_XML = (
    '<odoo><record id="view_project_fieldjob_count" model="ir.ui.view">'
    '<field name="name">project.project.fieldjob.count</field>'
    '<field name="model">project.project</field>'
    '<field name="inherit_id" ref="project.edit_project"/>'
    '<field name="arch" type="xml"><sheet position="inside">'
    '<field name="fieldjob_count"/>'
    "</sheet></field></record></odoo>"
)


def _gen(views_xml, models_py="") -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py=models_py, views_xml=views_xml,
        security_csv="x", notes="",
    )


def test_stat_button_goal_views_xml_is_never_stripped():
    generated = _gen(_REAL_VIEWS_XML)
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(generated, _STAT_BUTTON_GOAL)
    assert generated.views_xml == _REAL_VIEWS_XML
    print("PASS item178: a stat-button-shaped goal's own views_xml survives the pure-behavior stripper")


def test_ordinary_pure_behavior_goal_is_still_stripped():
    """The pre-existing, correct behavior must be unchanged for a genuine pure-behavior task."""
    goal = "When I select a project on the fieldjob form, auto-fill the 'Assigned to' field."
    generated = _gen(_REAL_VIEWS_XML)
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(generated, goal)
    assert generated.views_xml is None
    print("PASS item178: an ordinary pure-behavior goal (no stat-button intent) is still stripped, unchanged")


def test_top_right_phrase_alone_without_action_method_line_is_still_stripped():
    """Both signals must be present -- the phrase alone isn't enough, matching the stat-button
    autofix's own equally narrow trigger condition.
    """
    goal = "Add a button in the top-right corner of the form."
    generated = _gen(_REAL_VIEWS_XML)
    _autofix_strip_unrequested_views_xml_for_pure_behavior_task(generated, goal)
    assert generated.views_xml is None
    print("PASS item178: the phrase alone, without an Action method: line, does not exempt from stripping")


if __name__ == "__main__":
    test_stat_button_goal_views_xml_is_never_stripped()
    test_ordinary_pure_behavior_goal_is_still_stripped()
    test_top_right_phrase_alone_without_action_method_line_is_still_stripped()
    print("\nALL P11 FIFTH-PASS ITEM 178 TESTS PASSED")
