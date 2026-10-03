"""Real, confirmed false-pass found live (2026-08-06, fix-pass task 007): task 007's real goal
("In the fieldjob list, I want a quick filter button called 'Accepted' that shows only accepted
records, and another called 'My records' that shows only records assigned to me.") produced, in
ALL 3 best-of-N candidates, a tree-view decoration-* attribute (Odoo's row-COLORING mechanism)
instead of a real <filter> element inside a <search> view (Odoo's actual clickable quick-filter-
button mechanism) -- the requested deliverable was never implemented at all. Code-Review
correctly caught this ("does not add any quick filter buttons to the list view as requested")
but graded it "major", not "blocking" -- and the fold logic only gates a round on "blocking"
findings by design, so the round passed anyway.

_validate_goal_named_quick_filter_buttons_are_real_filter_elements() is the fix: a deterministic,
non-LLM ground-truth check that a goal asking for "quick filter"/"filter button" must produce a
real <filter> element somewhere in views_xml, independent of any LLM's own severity judgment.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_goal_named_quick_filter_buttons_are_real_filter_elements,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)

_TASK_007_GOAL = (
    "In the fieldjob list, I want a quick filter button called 'Accepted' that shows only "
    "accepted records, and another called 'My records' that shows only records assigned to me."
)


def _gen(views_xml=None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py="", views_xml=views_xml,
        security_csv="x", security_xml=None, extra_data_files=None, tests_py=None, notes="",
    )


def _raises(fn, *args):
    try:
        fn(*args)
        return None
    except ValueError as exc:
        return str(exc)


def test_raises_on_task007_own_real_shape_decoration_instead_of_filter():
    """The exact real defect: decoration-* attributes on the tree view, no <filter> anywhere."""
    views_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
        '  <record id="view_project_fieldjob_list_inherit" model="ir.ui.view">\n'
        '    <field name="name">project.fieldjob.tree.inherit</field>\n'
        '    <field name="model">project.fieldjob</field>\n'
        '    <field name="inherit_id" ref="project_fieldjob.view_project_fieldjob_tree"/>\n'
        '    <field name="arch" type="xml">\n'
        '      <xpath expr="//tree" position="attributes">\n'
        '        <attribute name="decoration-success">is_accepted</attribute>\n'
        '        <attribute name="decoration-info">is_my_record</attribute>\n'
        '      </xpath>\n'
        '    </field>\n'
        '  </record>\n</odoo>\n'
    )
    notes = _raises(
        _validate_goal_named_quick_filter_buttons_are_real_filter_elements, _gen(views_xml), _TASK_007_GOAL,
    )
    assert notes is not None
    assert "<filter>" in notes
    print("PASS: the real task 007 defect (decoration instead of <filter>) is caught")


def test_passes_with_a_real_filter_element():
    views_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
        '  <record id="view_project_fieldjob_search_inherit" model="ir.ui.view">\n'
        '    <field name="name">project.fieldjob.search.inherit</field>\n'
        '    <field name="model">project.fieldjob</field>\n'
        '    <field name="inherit_id" ref="project_fieldjob.view_project_fieldjob_search"/>\n'
        '    <field name="arch" type="xml">\n'
        '      <xpath expr="//search" position="inside">\n'
        '        <filter name="accepted" string="Accepted" domain="[(\'state\',\'=\',\'accepted\')]"/>\n'
        '        <filter name="my_records" string="My records" domain="[(\'user_id\',\'=\',uid)]"/>\n'
        '      </xpath>\n'
        '    </field>\n'
        '  </record>\n</odoo>\n'
    )
    assert _raises(
        _validate_goal_named_quick_filter_buttons_are_real_filter_elements, _gen(views_xml), _TASK_007_GOAL,
    ) is None
    print("PASS: a real <filter> element satisfies the check, never a false positive")


def test_never_fires_for_an_unrelated_goal():
    assert _raises(
        _validate_goal_named_quick_filter_buttons_are_real_filter_elements,
        _gen(views_xml="<odoo></odoo>"),
        "Add a single new field 'preferred_language' (Char) to res.partner.",
    ) is None
    print("PASS: a goal that never mentions quick filters/filter buttons is a complete no-op")


def test_never_fires_when_views_xml_is_none():
    assert _raises(
        _validate_goal_named_quick_filter_buttons_are_real_filter_elements, _gen(views_xml=None), _TASK_007_GOAL,
    ) is not None, "no views_xml at all, but the goal asks for filter buttons -- still a real gap"
    print("PASS: a completely missing views_xml is still correctly caught as a gap, not silently skipped")


if __name__ == "__main__":
    test_raises_on_task007_own_real_shape_decoration_instead_of_filter()
    test_passes_with_a_real_filter_element()
    test_never_fires_for_an_unrelated_goal()
    test_never_fires_when_views_xml_is_none()
    print("\nALL QUICK-FILTER-BUTTON DETERMINISTIC CHECK TESTS PASSED")
