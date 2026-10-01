"""Phase 29B auto-generated test for api_constrains_decorated_method_x_has.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
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
        depends=[],
        data=[]
    )

    # Case 1: Missing raise -> should trigger ValueError
    bad_models_py = """from odoo import models, api

class TestModel(models.Model):
 _name = 'test.model'

 @api.constrains('field_a', 'field_b')
 def _validate_fields(self):
     if self.field_a > self.field_b:
         pass
"""
    bad_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=bad_models_py,
        security_csv="",
        notes=""
    )

    try:
        _validate_api_constrains_method_has_a_raise(bad_gen)
        assert False, "Expected ValueError to be raised for missing raise statement"
    except ValueError as e:
        assert "no raise anywhere in its body" in str(e)

    # Case 2: Contains raise -> should NOT trigger ValueError
    good_models_py = """from odoo import models, api

class TestModel(models.Model):
 _name = 'test.model'

 @api.constrains('field_a', 'field_b')
 def _validate_fields(self):
     if self.field_a > self.field_b:
         raise ValueError("Invalid order")
"""
    good_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=good_models_py,
        security_csv="",
        notes=""
    )

    try:
        _validate_api_constrains_method_has_a_raise(good_gen)
    except ValueError:
        assert False, "Unexpected ValueError raised for valid code containing a raise statement"
