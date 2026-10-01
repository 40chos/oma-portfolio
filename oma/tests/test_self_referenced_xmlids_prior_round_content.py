"""Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run, ticket_bulk_close
node): `_validate_self_referenced_xmlids_are_defined` only ever scanned THIS round's own fresh
`generated` content for a matching `<record id="...">` -- but a self-prefixed ref very commonly,
legitimately points at a record an EARLIER round of the same decomposed task already created and
committed. Confirmed live: `view_service_ticket_tree` (created by the `ticket_list_view`
constraint, several rounds earlier) was flagged as "no matching <record id> actually defines" it,
purely because THIS round's own new content doesn't re-declare it -- real, correct content
silently treated as broken.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_self_referenced_xmlids_are_defined,
)

_MANIFEST = ManifestFields(
    name="x", version="16.0.1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)


def _gen(views_xml=None, security_xml=None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py="", views_xml=views_xml,
        security_csv="x", security_xml=security_xml, notes="",
    )


def test_recognizes_a_record_defined_only_in_an_earlier_committed_round():
    old_views_xml = (
        '<record id="view_service_ticket_tree" model="ir.ui.view">'
        '<field name="name">oma.service.ticket.tree</field>'
        '<field name="model">oma.service.ticket</field>'
        "</record>"
    )
    new_views_xml = (
        '<record id="view_service_ticket_tree_bulk_close_inherit" model="ir.ui.view">'
        '<field name="inherit_id" ref="oma_build_a_complete_field_ab52b7f8.view_service_ticket_tree"/>'
        "</record>"
    )
    generated = _gen(views_xml=new_views_xml)
    old_files_by_relpath = {"views/views.xml": old_views_xml}

    # Must NOT raise -- the referenced record genuinely exists, just in an earlier round's own
    # already-committed content, not this round's own fresh diff.
    _validate_self_referenced_xmlids_are_defined(
        generated, "oma_build_a_complete_field_ab52b7f8", old_files_by_relpath,
    )
    print("PASS: a self-ref to a record defined only in an earlier committed round is recognized")


def test_still_raises_when_genuinely_undefined_anywhere():
    new_views_xml = (
        '<record id="view_x" model="ir.ui.view">'
        '<field name="inherit_id" ref="oma_test_module.view_ghost"/>'
        "</record>"
    )
    generated = _gen(views_xml=new_views_xml)
    old_files_by_relpath = {"views/views.xml": "<odoo></odoo>"}

    raised = False
    try:
        _validate_self_referenced_xmlids_are_defined(
            generated, "oma_test_module", old_files_by_relpath,
        )
    except ValueError as exc:
        raised = True
        assert "view_ghost" in str(exc)
    assert raised, "a genuinely undefined self-reference must still be caught"
    print("PASS: a genuinely undefined self-reference (absent from both new AND old content) "
          "is still caught")


def test_backward_compatible_when_old_files_by_relpath_is_not_passed():
    new_views_xml = (
        '<record id="view_x" model="ir.ui.view">'
        '<field name="inherit_id" ref="oma_test_module.view_ghost"/>'
        "</record>"
    )
    generated = _gen(views_xml=new_views_xml)
    raised = False
    try:
        _validate_self_referenced_xmlids_are_defined(generated, "oma_test_module")
    except ValueError:
        raised = True
    assert raised, "existing callers that don't pass old_files_by_relpath must keep working unchanged"
    print("PASS: existing 2-arg callers keep working unchanged (old_files_by_relpath defaults to None)")


if __name__ == "__main__":
    test_recognizes_a_record_defined_only_in_an_earlier_committed_round()
    test_still_raises_when_genuinely_undefined_anywhere()
    test_backward_compatible_when_old_files_by_relpath_is_not_passed()
    print("\nALL SELF-REFERENCED-XMLIDS-PRIOR-ROUND-CONTENT TESTS PASSED")
