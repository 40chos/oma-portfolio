"""Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
flagship run, service_ticket_model node): the third confirmed instance of the "shared file gets
wholesale-replaced instead of additively edited" bug class (siblings: data-XML records, models.py
classes) -- a round meant to ONLY add a new access_oma_service_ticket row regenerated
security/ir.model.access.csv containing ONLY that row, deleting the earlier, already-installed
access_oma_equipment row.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_restore_dropped_rows_in_shared_security_csv,
)

_HEADER = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink"
# Exact real content from the live bug's own earlier, already-committed round.
_OLD_CSV = (
    f"{_HEADER}\n"
    "access_oma_equipment,oma.equipment,model_oma_equipment,base.group_user,1,1,1,0\n"
)
# Exact real content from the live bug's own broken new round -- equipment row is gone.
_NEW_CSV_MISSING_OLD_ROW = (
    f"{_HEADER}\n"
    "access_oma_service_ticket,oma.service.ticket,model_oma_service_ticket,base.group_user,1,1,1,0\n"
)


def _make_generated(security_csv: str) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv=security_csv,
        security_xml=None, views_xml=None, notes="",
    )


def test_restores_the_real_dropped_row_closing_the_live_gap():
    generated = _make_generated(_NEW_CSV_MISSING_OLD_ROW)
    old_files_by_relpath = {"security/ir.model.access.csv": _OLD_CSV}

    _autofix_restore_dropped_rows_in_shared_security_csv(generated, old_files_by_relpath)

    assert "access_oma_equipment" in generated.security_csv, (
        f"expected the earlier, already-committed equipment access row to be restored -- got: "
        f"{generated.security_csv!r}"
    )
    assert "access_oma_service_ticket" in generated.security_csv, (
        "the round's own new access row must still be present, unchanged"
    )
    print("PASS: the real, confirmed dropped access_oma_equipment row is restored alongside "
          "the round's own new row, closing the real live gap found on task 07141af5's "
          "service_ticket_model node")


def test_never_touches_a_file_this_round_wrote_no_csv_for():
    generated = _make_generated("")
    old_files_by_relpath = {"security/ir.model.access.csv": _OLD_CSV}

    _autofix_restore_dropped_rows_in_shared_security_csv(generated, old_files_by_relpath)

    assert generated.security_csv == "", "must remain untouched when this round wrote no CSV at all"
    print("PASS: never touches a round whose own security_csv is genuinely empty")


def test_is_a_noop_when_nothing_is_actually_missing():
    both_rows = _OLD_CSV + _NEW_CSV_MISSING_OLD_ROW.split("\n", 1)[1]
    generated = _make_generated(both_rows)
    old_files_by_relpath = {"security/ir.model.access.csv": _OLD_CSV}
    original = generated.security_csv

    _autofix_restore_dropped_rows_in_shared_security_csv(generated, old_files_by_relpath)

    assert generated.security_csv == original, (
        "must never duplicate or otherwise modify content when the old row is already "
        "genuinely present in the new content"
    )
    print("PASS: a no-op when the round's own new content already includes every old row")


def test_is_a_noop_when_old_files_by_relpath_has_no_prior_csv():
    generated = _make_generated(_NEW_CSV_MISSING_OLD_ROW)
    original = generated.security_csv

    _autofix_restore_dropped_rows_in_shared_security_csv(generated, old_files_by_relpath=None)
    assert generated.security_csv == original

    _autofix_restore_dropped_rows_in_shared_security_csv(generated, old_files_by_relpath={})
    assert generated.security_csv == original
    print("PASS: a no-op when there is genuinely no prior committed CSV to compare against")


def test_is_a_noop_on_a_genuinely_different_header_shape():
    different_header_old = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write\n"
        "access_oma_equipment,oma.equipment,model_oma_equipment,base.group_user,1,1\n"
    )
    generated = _make_generated(_NEW_CSV_MISSING_OLD_ROW)
    original = generated.security_csv

    _autofix_restore_dropped_rows_in_shared_security_csv(
        generated, {"security/ir.model.access.csv": different_header_old},
    )
    assert generated.security_csv == original, (
        "a genuinely different column shape is not this fix's safe territory -- must never "
        "attempt to merge mismatched schemas"
    )
    print("PASS: a no-op when the old CSV's own header shape genuinely differs from the new one")


if __name__ == "__main__":
    test_restores_the_real_dropped_row_closing_the_live_gap()
    test_never_touches_a_file_this_round_wrote_no_csv_for()
    test_is_a_noop_when_nothing_is_actually_missing()
    test_is_a_noop_when_old_files_by_relpath_has_no_prior_csv()
    test_is_a_noop_on_a_genuinely_different_header_shape()
    print("\nALL RESTORE-DROPPED-ROWS-IN-SHARED-SECURITY-CSV TESTS PASSED")
