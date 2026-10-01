"""Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
flagship run, service_ticket_model node, recurring identically across 2 straight rounds even
after an explicit correction note): a data XML file (data/sequences.xml) that an EARLIER
constraint already created (the equipment model's own 'seq_equipment' record) got REPLACED
wholesale by a LATER round's own new content for the same file (the ticket model's own
'seq_service_ticket' record), silently deleting the earlier record -- confirmed live via direct
Gitea inspection of the exact committed file.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_restore_dropped_records_in_shared_data_xml_file,
)

# Exact real content from the live bug's own earlier, already-committed round.
_OLD_SEQUENCES_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
    '  <record id="seq_equipment" model="ir.sequence">\n'
    "    <field name=\"name\">Equipment Sequence</field>\n"
    "    <field name=\"code\">oma.equipment</field>\n"
    "    <field name=\"prefix\">EQ-</field>\n"
    "    <field name=\"padding\">5</field>\n"
    "    <field name=\"number_next\">1</field>\n"
    "    <field name=\"number_increment\">1</field>\n"
    "  </record>\n</odoo>\n"
)
# Exact real content from the live bug's own broken new round -- 'seq_equipment' is gone.
_NEW_SEQUENCES_XML_MISSING_OLD_RECORD = (
    '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
    '  <record id="seq_service_ticket" model="ir.sequence">\n'
    "    <field name=\"name\">Service Ticket Sequence</field>\n"
    "    <field name=\"code\">oma.service.ticket</field>\n"
    "    <field name=\"prefix\">ST-</field>\n"
    "    <field name=\"padding\">5</field>\n"
    "    <field name=\"number_next\">1</field>\n"
    "    <field name=\"number_increment\">1</field>\n"
    "  </record>\n</odoo>\n"
)


def _make_generated(extra_data_files: dict) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="x",
        security_xml=None, views_xml=None, notes="", extra_data_files=extra_data_files,
    )


def test_restores_the_real_dropped_record_closing_the_live_gap():
    generated = _make_generated({"data/sequences.xml": _NEW_SEQUENCES_XML_MISSING_OLD_RECORD})
    old_files_by_relpath = {"data/sequences.xml": _OLD_SEQUENCES_XML}

    _autofix_restore_dropped_records_in_shared_data_xml_file(generated, old_files_by_relpath)

    result = generated.extra_data_files["data/sequences.xml"]
    assert 'id="seq_equipment"' in result, (
        f"expected the earlier, already-committed seq_equipment record to be restored -- got: "
        f"{result!r}"
    )
    assert 'id="seq_service_ticket"' in result, (
        "the round's own new seq_service_ticket record must still be present, unchanged"
    )
    assert result.count("<record") == 2
    print("PASS: the real, confirmed dropped seq_equipment record is restored alongside the "
          "round's own new record, closing the real live gap found on task 07141af5's "
          "service_ticket_model node")


def test_never_touches_a_file_this_round_did_not_write_new_content_for():
    generated = _make_generated({})  # this round wrote nothing new to any data XML file
    old_files_by_relpath = {"data/sequences.xml": _OLD_SEQUENCES_XML}

    _autofix_restore_dropped_records_in_shared_data_xml_file(generated, old_files_by_relpath)

    assert generated.extra_data_files == {}, (
        "a shared file this round genuinely never touched is the sibling restore-when-missing "
        "autofixes' concern, not this one's -- must remain untouched"
    )
    print("PASS: never touches a shared data file this round's own generation didn't write to")


def test_is_a_noop_when_nothing_is_actually_missing():
    both_records = _OLD_SEQUENCES_XML.replace(
        "</odoo>", _NEW_SEQUENCES_XML_MISSING_OLD_RECORD.split("<odoo>\n", 1)[1].replace("</odoo>\n", ""),
    )
    generated = _make_generated({"data/sequences.xml": both_records})
    old_files_by_relpath = {"data/sequences.xml": _OLD_SEQUENCES_XML}
    original = generated.extra_data_files["data/sequences.xml"]

    _autofix_restore_dropped_records_in_shared_data_xml_file(generated, old_files_by_relpath)

    assert generated.extra_data_files["data/sequences.xml"] == original, (
        "must never duplicate or otherwise modify content when every old record is already "
        "genuinely present in the new content"
    )
    print("PASS: a no-op when the round's own new content already includes every old record")


def test_is_a_noop_when_old_files_by_relpath_has_no_prior_version_of_this_file():
    generated = _make_generated({"data/sequences.xml": _NEW_SEQUENCES_XML_MISSING_OLD_RECORD})
    original = generated.extra_data_files["data/sequences.xml"]

    _autofix_restore_dropped_records_in_shared_data_xml_file(generated, old_files_by_relpath=None)
    assert generated.extra_data_files["data/sequences.xml"] == original

    _autofix_restore_dropped_records_in_shared_data_xml_file(generated, old_files_by_relpath={})
    assert generated.extra_data_files["data/sequences.xml"] == original
    print("PASS: a no-op when there is genuinely no prior committed version of this file to "
          "compare against (e.g. this file is genuinely brand new)")


if __name__ == "__main__":
    test_restores_the_real_dropped_record_closing_the_live_gap()
    test_never_touches_a_file_this_round_did_not_write_new_content_for()
    test_is_a_noop_when_nothing_is_actually_missing()
    test_is_a_noop_when_old_files_by_relpath_has_no_prior_version_of_this_file()
    print("\nALL RESTORE-DROPPED-RECORDS-IN-SHARED-DATA-XML TESTS PASSED")
