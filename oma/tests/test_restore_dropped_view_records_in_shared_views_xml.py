"""Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run -- discovered while
investigating a hallucinated `helpdesk.helpdesk_ticket_view_tree` xmlid reference on the
`ticket_workflow_and_logging`/`ticket_bulk_close` node): `ticket_list_view`'s own real,
already-satisfied `view_service_ticket_tree` record had silently disappeared from
`views/views.xml` by the time the `project_ticket_counts` round's own button_box record was
added -- confirmed live via `read_last_validated_commit()`: the real, currently-committed
`views/views.xml` contained ONLY the `view_project_form_inherit` record, with
`view_service_ticket_tree` gone entirely, even though `ticket_list_view` was never re-opened.
This is the `views_xml`-specific counterpart of the already-fixed sibling bug for `data/*.xml`
files (`_autofix_restore_dropped_records_in_shared_data_xml_file`) -- `views_xml` is its own
dedicated `GeneratedModuleFiles` field, never covered by that fix's own `data/`-prefix filter.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_restore_dropped_view_records_in_shared_views_xml,
)

# Exact real shape from the live bug's own earlier, already-committed round.
_OLD_VIEWS_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
    '    <record id="view_service_ticket_tree" model="ir.ui.view">\n'
    '        <field name="name">oma.service.ticket.tree</field>\n'
    '        <field name="model">oma.service.ticket</field>\n'
    '        <field name="arch" type="xml">\n'
    "            <tree string=\"Service Tickets\">\n"
    '                <field name="name"/>\n'
    "            </tree>\n"
    "        </field>\n"
    "    </record>\n</odoo>\n"
)
# Exact real broken shape from the live bug: view_service_ticket_tree is gone, only the
# project_ticket_counts round's own button_box record remains.
_NEW_VIEWS_XML_MISSING_OLD_RECORD = (
    '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
    '    <record id="view_project_form_inherit" model="ir.ui.view">\n'
    '        <field name="name">project.project.form.inherit</field>\n'
    '        <field name="model">project.project</field>\n'
    '        <field name="inherit_id" ref="project.edit_project"/>\n'
    '        <field name="arch" type="xml">\n'
    '            <xpath expr="//div[@name=\'button_box\']" position="inside"/>\n'
    "        </field>\n"
    "    </record>\n</odoo>\n"
)


def _make_generated(views_xml: str | None) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="x",
        security_xml=None, views_xml=views_xml, notes="",
    )


def test_restores_the_real_dropped_record_closing_the_live_gap():
    generated = _make_generated(_NEW_VIEWS_XML_MISSING_OLD_RECORD)

    _autofix_restore_dropped_view_records_in_shared_views_xml(
        generated, _OLD_VIEWS_XML, constraint_status={"ticket_list_view": "satisfied"},
    )

    result = generated.views_xml
    assert 'id="view_service_ticket_tree"' in result, (
        f"expected the earlier, already-committed view_service_ticket_tree record to be "
        f"restored -- got: {result!r}"
    )
    assert 'id="view_project_form_inherit"' in result, (
        "the round's own new view_project_form_inherit record must still be present, unchanged"
    )
    assert result.count("<record") == 2
    print("PASS: the real, confirmed dropped view_service_ticket_tree record is restored "
          "alongside the round's own new record, closing the real live gap found on task "
          "07141af5's flagship run")


def test_never_touches_a_round_that_wrote_no_views_xml_at_all():
    generated = _make_generated(None)

    _autofix_restore_dropped_view_records_in_shared_views_xml(generated, _OLD_VIEWS_XML)

    assert generated.views_xml is None, (
        "a round that never wrote views_xml at all is the whole-file restore sibling's own "
        "concern, not this one's -- must remain untouched"
    )
    print("PASS: never touches a round whose own views_xml is genuinely empty/missing")


def test_is_a_noop_when_nothing_is_actually_missing():
    both_records = _OLD_VIEWS_XML.replace(
        "</odoo>",
        _NEW_VIEWS_XML_MISSING_OLD_RECORD.split("<odoo>\n", 1)[1].replace("</odoo>\n", ""),
    )
    generated = _make_generated(both_records)
    original = generated.views_xml

    _autofix_restore_dropped_view_records_in_shared_views_xml(generated, _OLD_VIEWS_XML)

    assert generated.views_xml == original, (
        "must never duplicate or otherwise modify content when every old record is already "
        "genuinely present in the new content"
    )
    print("PASS: a no-op when the round's own new content already includes every old record")


def test_is_a_noop_when_there_is_no_prior_views_xml_to_compare_against():
    generated = _make_generated(_NEW_VIEWS_XML_MISSING_OLD_RECORD)
    original = generated.views_xml

    _autofix_restore_dropped_view_records_in_shared_views_xml(
        generated, old_views_xml="", constraint_status={"ticket_list_view": "satisfied"},
    )

    assert generated.views_xml == original
    print("PASS: a no-op when there is genuinely no prior committed views.xml to compare "
          "against (e.g. this is genuinely the first view this task ever created)")


def test_never_restores_when_nothing_views_related_is_satisfied_yet():
    """Real, confirmed bug found live (2026-08-10, task e65381cc, equipment_views_menu node,
    recurring identically across 5 straight rounds): with NO views/menu-shaped constraint
    genuinely satisfied yet, the "old" views_xml is exactly as likely to be a PRIOR round's own
    out-of-scope, Code-Review-rejected content that Build correctly, deliberately dropped this
    round (after an explicit instruction to remove it) as it is to be an accidental regression.
    Before this fix, this function had no way to tell the difference and always restored --
    permanently defeating every instruction to remove premature content, no matter how explicit.
    """
    generated = _make_generated(_NEW_VIEWS_XML_MISSING_OLD_RECORD)
    original = generated.views_xml

    _autofix_restore_dropped_view_records_in_shared_views_xml(
        generated, _OLD_VIEWS_XML, constraint_status={"ticket_list_view": "pending"},
    )

    assert generated.views_xml == original, (
        "must never resurrect dropped content when nothing views-related has genuinely been "
        "satisfied yet -- there is no 'earned' content to protect"
    )
    print("PASS: never restores a dropped record when nothing views-related is satisfied yet, "
          "closing the real live non-convergence bug found on task e65381cc")


def test_never_restores_with_no_constraint_status_at_all():
    """The default (no constraint_status passed, e.g. a genuinely non-decomposed task) must be
    the same safe no-op, not an implicit 'always restore.'"""
    generated = _make_generated(_NEW_VIEWS_XML_MISSING_OLD_RECORD)
    original = generated.views_xml

    _autofix_restore_dropped_view_records_in_shared_views_xml(generated, _OLD_VIEWS_XML)

    assert generated.views_xml == original
    print("PASS: no constraint_status at all is the same safe no-op, never an implicit restore")


if __name__ == "__main__":
    test_restores_the_real_dropped_record_closing_the_live_gap()
    test_never_touches_a_round_that_wrote_no_views_xml_at_all()
    test_is_a_noop_when_nothing_is_actually_missing()
    test_is_a_noop_when_there_is_no_prior_views_xml_to_compare_against()
    test_never_restores_when_nothing_views_related_is_satisfied_yet()
    test_never_restores_with_no_constraint_status_at_all()
    print("\nALL RESTORE-DROPPED-VIEW-RECORDS-IN-SHARED-VIEWS-XML TESTS PASSED")
