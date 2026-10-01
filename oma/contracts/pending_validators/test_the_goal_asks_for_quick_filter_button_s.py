"""Phase 29B auto-generated test for the_goal_asks_for_quick_filter_button_s.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="project_task_custom",
        version="16.0.1.0.0",
        category="Project Management",
        summary="Custom task filters",
        author="DevTeam",
        depends=["project"],
        data=["views/task_views.xml"]
    )

    # Case 1: Intent present, but <filter> missing -> should raise ValueError
    notes_fail = "Please add a quick filter button to toggle completed tasks in the list view."
    views_xml_fail = """<?xml version="1.0" encoding="utf-8"?>
    <odoo>
        <record id="view_task_tree" model="ir.ui.view">
            <field name="name">project.task.tree</field>
            <field name="model">project.task</field>
            <field name="arch" type="xml">
                <tree decoration-muted="is_done==True">
                    <field name="name"/>
                    <field name="is_done"/>
                </tree>
            </field>
        </record>
    </odoo>"""
    gen_fail = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass Task(models.Model):\n    _name = 'project.task'\n",
        views_xml=views_xml_fail,
        security_csv="",
        security_xml=None,
        notes=notes_fail
    )

    try:
        _validate_quick_filter_elements_in_search_view(gen_fail)
        assert False, "Expected ValueError to be raised for missing <filter> elements"
    except ValueError:
        pass

    # Case 2: Intent present, and <filter> exists -> should NOT raise
    notes_pass = "Include a search filter for high priority items."
    views_xml_pass = """<?xml version="1.0" encoding="utf-8"?>
    <odoo>
        <record id="view_task_search" model="ir.ui.view">
            <field name="name">project.task.search</field>
            <field name="model">project.task</field>
            <field name="arch" type="xml">
                <search>
                    <filter name="priority_high" string="High Priority" domain="[('priority', '=', '3')]"/>
                </search>
            </field>
        </record>
    </odoo>"""
    gen_pass = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass Task(models.Model):\n    _name = 'project.task'\n",
        views_xml=views_xml_pass,
        security_csv="",
        security_xml=None,
        notes=notes_pass
    )

    try:
        _validate_quick_filter_elements_in_search_view(gen_pass)
    except ValueError:
        assert False, "Validator incorrectly raised ValueError on valid XML containing <filter>"
