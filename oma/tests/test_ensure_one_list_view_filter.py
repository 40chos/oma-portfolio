"""Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
project_ticket_counts node, resumes r189 and r191): Code-Review repeatedly claimed
"action_open_tickets uses self.ensure_one() which will fail when called from list view with
multiple records selected" -- confirmed false both times by reading the real, currently-committed
views/views.xml: the method is wired ONLY as a form-view smart button (button_box), never a
<tree>/<list> view button. See specialists/code_review/specialist.py's
_filter_hallucinated_ensure_one_list_view_findings for the full incident.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.code_review.specialist import (
    ReviewFinding,
    _filter_hallucinated_ensure_one_list_view_findings,
)

_MODELS_PY = (
    "from odoo import models, fields, api\n\n"
    "class ProjectProject(models.Model):\n"
    "    _inherit = 'project.project'\n\n"
    "    def action_open_tickets(self):\n"
    "        self.ensure_one()\n"
    "        return {}\n\n"
    "    def action_overdue_tickets(self):\n"
    "        self.ensure_one()\n"
    "        return {}\n"
)

_FORM_ONLY_VIEWS_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<odoo>\n"
    '    <record id="view_project_form_inherit" model="ir.ui.view">\n'
    '        <field name="name">project.project.form.inherit</field>\n'
    '        <field name="model">project.project</field>\n'
    '        <field name="arch" type="xml">\n'
    '            <form>\n'
    '                <div name="button_box">\n'
    '                    <button type="object" name="action_open_tickets"/>\n'
    '                    <button type="object" name="action_overdue_tickets"/>\n'
    "                </div>\n"
    "            </form>\n"
    "        </field>\n"
    "    </record>\n"
    "</odoo>\n"
)


def test_downgrades_named_claim_when_method_is_only_a_form_smart_button():
    files = {"models/models.py": _MODELS_PY, "views/views.xml": _FORM_ONLY_VIEWS_XML}
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation=(
                "action_open_tickets uses self.ensure_one() which will fail when called from "
                "list view with multiple records selected."
            ),
        ),
    ]
    out = _filter_hallucinated_ensure_one_list_view_findings(findings, files)
    assert out[0].severity == "info", f"expected downgrade, got {out[0].severity!r}"
    print("PASS: named form-only smart-button claim downgraded")


def test_downgrades_generic_claim_when_no_method_is_list_view_wired():
    files = {"models/models.py": _MODELS_PY, "views/views.xml": _FORM_ONLY_VIEWS_XML}
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation="Action methods use ensure_one() which will fail when called from list view with multiple records selected.",
        ),
    ]
    out = _filter_hallucinated_ensure_one_list_view_findings(findings, files)
    assert out[0].severity == "info", f"expected downgrade, got {out[0].severity!r}"
    print("PASS: generic claim downgraded when no method is actually list/tree-wired")


def test_never_downgrades_a_genuinely_list_view_wired_method():
    list_wired_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<odoo>\n"
        '    <record id="view_ticket_tree" model="ir.ui.view">\n'
        '        <field name="model">oma.service.ticket</field>\n'
        '        <field name="arch" type="xml">\n'
        "            <tree>\n"
        '                <button type="object" name="action_bulk_close"/>\n'
        "            </tree>\n"
        "        </field>\n"
        "    </record>\n"
        "</odoo>\n"
    )
    files = {
        "models/models.py": (
            "from odoo import models\n\n"
            "class ServiceTicket(models.Model):\n"
            "    _name = 'oma.service.ticket'\n\n"
            "    def action_bulk_close(self):\n"
            "        self.ensure_one()\n"
        ),
        "views/views.xml": list_wired_xml,
    }
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation=(
                "action_bulk_close uses self.ensure_one() which will fail when called from "
                "list view with multiple records selected."
            ),
        ),
    ]
    out = _filter_hallucinated_ensure_one_list_view_findings(findings, files)
    assert out[0].severity == "blocking", (
        f"a genuinely list-view-wired ensure_one() bug must never be downgraded -- got "
        f"{out[0].severity!r}"
    )
    print("PASS: a real list-view-wired ensure_one() bug is left untouched")


def test_downgrades_self_id_backtick_phrasing_variant():
    """Real, confirmed follow-up bug found live (2026-08-10, resume r193): the same false
    premise resurfaced in a THIRD phrasing that never says "ensure_one()" at all --
    "Action method `action_open_tickets` uses `self.id` in domain, which fails when called on
    multiple records (list view multi-select)." Same underlying claim (this method can't handle
    multiple records), same real answer (it's a form-only smart button), different technical
    justification and backtick quoting style.
    """
    files = {"models/models.py": _MODELS_PY, "views/views.xml": _FORM_ONLY_VIEWS_XML}
    findings = [
        ReviewFinding(
            location="models/models.py:5", severity="blocking",
            explanation=(
                "Action method `action_open_tickets` uses `self.id` in domain, which fails when "
                "called on multiple records (list view multi-select)."
            ),
        ),
    ]
    out = _filter_hallucinated_ensure_one_list_view_findings(findings, files)
    assert out[0].severity == "info", f"expected downgrade, got {out[0].severity!r}"
    print("PASS: backtick-quoted 'self.id ... multiple records' phrasing downgraded")


if __name__ == "__main__":
    test_downgrades_named_claim_when_method_is_only_a_form_smart_button()
    test_downgrades_generic_claim_when_no_method_is_list_view_wired()
    test_never_downgrades_a_genuinely_list_view_wired_method()
    test_downgrades_self_id_backtick_phrasing_variant()
    print("\nALL ENSURE_ONE LIST-VIEW FILTER TESTS PASSED")
