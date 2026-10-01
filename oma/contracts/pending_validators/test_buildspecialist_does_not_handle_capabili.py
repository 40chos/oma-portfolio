"""Phase 29B auto-generated test for buildspecialist_does_not_handle_capabili.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0",
        category="Test",
        summary="Test summary",
        author="Test Author",
        depends=[],
        data=[]
    )

    # Case 1: Should raise ValueError due to out-of-scope capability reference
    failing_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="capability_class=<CapabilityClass.custom_audit_log: 'audit'>\n",
        security_csv="id,name\n1,test\n",
        notes=""
    )

    try:
        _validate_no_out_of_scope_capabilities(failing_generated)
        assert False, "Expected ValueError to be raised"
    except ValueError as e:
        assert "out-of-scope capability references" in str(e)

    # Case 2: Legitimate content, should not raise
    passing_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass TestModel(models.Model):\n    _name = 'test.model'\n",
        security_csv="id,name\n1,test\n",
        notes=""
    )

    _validate_no_out_of_scope_capabilities(passing_generated)
    assert True
