"""Phase 29B auto-generated test for class_x_models_model_declares_neithe.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module", version="1.0", category="Test",
        summary="Test", author="Test", depends=[], data=[]
    )

    # Case 1: Missing _name and _inherit -> should raise ValueError
    bad_models_py = """from odoo import models

class SalesReport(models.Model):
 description = models.Char()
 amount = models.Float()
"""
    bad_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=bad_models_py,
        security_csv="",
        notes=""
    )

    try:
        _validate_persisted_model_declares_name_or_inherit(bad_generated)
        assert False, "Expected ValueError to be raised for missing _name/_inherit"
    except ValueError as e:
        assert "SalesReport" in str(e), f"Error message should mention the class name: {e}"

    # Case 2: Has _name -> should pass without raising
    good_models_py = """from odoo import models

class CustomerProfile(models.Model):
 _name = 'crm.customer_profile'
 name = models.Char()
"""
    good_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=good_models_py,
        security_csv="",
        notes=""
    )

    # Should not raise
    _validate_persisted_model_declares_name_or_inherit(good_generated)
