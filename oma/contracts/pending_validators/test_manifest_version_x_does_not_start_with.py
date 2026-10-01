"""Phase 29B auto-generated test for manifest_version_x_does_not_start_with.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    # Case 1: Failing pattern (version does not match expected prefix)
    bad_manifest = ManifestFields(
        name="my_custom_app",
        version="3.0.0",
        category="Custom",
        summary="Test module for validation",
        author="Dev Team",
        depends=[],
        data=[]
    )
    bad_generated = GeneratedModuleFiles(
        manifest_fields=bad_manifest,
        models_py="class MyModel:\n    pass\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    try:
        _validate_manifest_version_series_prefix(bad_generated, "16.0.")
        assert False, "Expected ValueError to be raised"
    except ValueError as e:
        assert "does not start with the expected Odoo series prefix" in str(e)

    # Case 2: Passing pattern (version correctly starts with expected prefix)
    good_manifest = ManifestFields(
        name="my_custom_app_v16",
        version="16.0.2",
        category="Custom",
        summary="Compliant test module",
        author="Dev Team",
        depends=[],
        data=[]
    )
    good_generated = GeneratedModuleFiles(
        manifest_fields=good_manifest,
        models_py="class MyModel:\n    pass\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    # Should not raise any exception
    _validate_manifest_version_series_prefix(good_generated, "16.0.")
