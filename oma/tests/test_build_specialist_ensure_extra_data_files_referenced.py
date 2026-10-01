"""Phase 29 (2026-07-29): unit test for
_autofix_ensure_extra_data_files_are_referenced_in_manifest() -- the
real, confirmed live root cause behind the school_student task's
automated_tests round failing `test_student_demo_data_loaded` even
after --without-demo=False was made explicit: Build generated real
content for `data/demo_data.xml` in extra_data_files, but never
referenced it in manifest_fields.data at all, so Odoo never loaded it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_ensure_extra_data_files_are_referenced_in_manifest,
)


def _make_generated(data: list[str], extra_data_files: dict[str, str] | None) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Education", summary="x", author="x", depends=["base"], data=data,
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="x",
        extra_data_files=extra_data_files, notes="",
    )


def test_adds_the_real_missing_demo_data_reference():
    generated = _make_generated(
        data=["security/ir.model.access.csv"],
        extra_data_files={"data/demo_data.xml": "<odoo><data noupdate=\"1\"></data></odoo>"},
    )
    _autofix_ensure_extra_data_files_are_referenced_in_manifest(generated)
    assert "data/demo_data.xml" in generated.manifest_fields.data
    assert "security/ir.model.access.csv" in generated.manifest_fields.data
    print("PASS: the real, confirmed live missing demo_data.xml reference is added")


def test_never_touches_an_already_referenced_file():
    generated = _make_generated(
        data=["data/demo_data.xml", "security/ir.model.access.csv"],
        extra_data_files={"data/demo_data.xml": "<odoo></odoo>"},
    )
    before = list(generated.manifest_fields.data)
    _autofix_ensure_extra_data_files_are_referenced_in_manifest(generated)
    assert generated.manifest_fields.data == before
    print("PASS: never touches a file that's already correctly referenced")


def test_no_op_when_extra_data_files_is_none():
    generated = _make_generated(data=["security/ir.model.access.csv"], extra_data_files=None)
    before = list(generated.manifest_fields.data)
    _autofix_ensure_extra_data_files_are_referenced_in_manifest(generated)
    assert generated.manifest_fields.data == before
    print("PASS: no-op when extra_data_files is None")


def test_never_invents_a_reference_for_a_key_that_does_not_match_the_data_xml_shape():
    generated = _make_generated(
        data=["security/ir.model.access.csv"],
        extra_data_files={"notes.txt": "irrelevant"},
    )
    before = list(generated.manifest_fields.data)
    _autofix_ensure_extra_data_files_are_referenced_in_manifest(generated)
    assert generated.manifest_fields.data == before
    print("PASS: never adds a key that doesn't match the data/*.xml shape")


def test_falls_back_to_old_files_by_relpath_when_this_rounds_extra_data_files_omits_it():
    """Real, confirmed live follow-up (2026-07-29): a scoped-edit round
    whose own LLM completion never touched demo_data.xml can leave it
    out of THIS round's own extra_data_files, even though the file is
    still physically on disk from the round that originally wrote it
    (write_module_file() is purely additive). old_files_by_relpath (the
    prior committed file set) is where that still-real content is
    findable.
    """
    generated = _make_generated(
        data=["security/ir.model.access.csv"],
        extra_data_files=None,
    )
    old_files = {"data/demo_data.xml": "<odoo><data></data></odoo>", "models/models.py": "x"}
    _autofix_ensure_extra_data_files_are_referenced_in_manifest(generated, old_files)
    assert "data/demo_data.xml" in generated.manifest_fields.data
    print("PASS: falls back to old_files_by_relpath when this round's own extra_data_files omits it")


def test_no_op_when_neither_source_has_the_file():
    generated = _make_generated(data=["security/ir.model.access.csv"], extra_data_files=None)
    before = list(generated.manifest_fields.data)
    _autofix_ensure_extra_data_files_are_referenced_in_manifest(generated, {"models/models.py": "x"})
    assert generated.manifest_fields.data == before
    print("PASS: no-op when neither extra_data_files nor old_files_by_relpath has the file")


if __name__ == "__main__":
    test_adds_the_real_missing_demo_data_reference()
    test_never_touches_an_already_referenced_file()
    test_no_op_when_extra_data_files_is_none()
    test_never_invents_a_reference_for_a_key_that_does_not_match_the_data_xml_shape()
    test_falls_back_to_old_files_by_relpath_when_this_rounds_extra_data_files_omits_it()
    test_no_op_when_neither_source_has_the_file()
    print("\nALL ENSURE-EXTRA-DATA-FILES-REFERENCED TESTS PASSED")
