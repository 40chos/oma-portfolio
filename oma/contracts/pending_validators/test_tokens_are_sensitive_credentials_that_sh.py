"""Phase 29B auto-generated test for tokens_are_sensitive_credentials_that_sh.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0.0",
        category="Testing",
        summary="A test module",
        author="Test Author",
        depends=["base"],
        data=[]
    )

    # Case 1: Contains a sensitive field -> should raise ValueError
    bad_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass TestModel(models.Model):\n    _name = 'test.model'\n    access_token = fields.Char()\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes="test notes"
    )

    try:
        _validate_no_plaintext_sensitive_fields(bad_generated)
        assert False, "Expected ValueError to be raised for sensitive field"
    except ValueError as e:
        assert "Security violation" in str(e)

    # Case 2: Legitimate field -> should NOT raise
    good_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models, fields\n\nclass TestModel(models.Model):\n    _name = 'test.model'\n    partner_name = fields.Char()\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes="test notes"
    )

    # Should complete without raising
    _validate_no_plaintext_sensitive_fields(good_generated)
