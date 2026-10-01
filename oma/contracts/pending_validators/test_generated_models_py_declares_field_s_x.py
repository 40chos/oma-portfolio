"""Phase 29B auto-generated test for generated_models_py_declares_field_s_x.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0.0",
        category="Test",
        summary="Test module",
        author="Tester",
        depends=[],
        data=[]
    )

    # Case 1: Contains duplicate field declaration (should raise ValueError)
    models_py_dup = """from odoo import models, fields

class TestModel(models.Model):
 _name = 'test.model'

 name = fields.Char(string='Name')
 inspection_date = fields.Date(string='Inspection Date')
 status = fields.Selection([('draft', 'Draft'), ('done', 'Done')])
 inspection_date = fields.Date(string='Inspection Date Duplicate')
"""
    generated_dup = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_dup,
        security_csv="",
        notes=""
    )

    try:
        _validate_no_duplicate_field_declarations(generated_dup)
        assert False, "Expected ValueError to be raised for duplicate fields"
    except ValueError as e:
        assert "Duplicate field declaration" in str(e)

    # Case 2: Valid content with no duplicates (should not raise)
    models_py_valid = """from odoo import models, fields

class TestModel(models.Model):
 _name = 'test.model'

 name = fields.Char(string='Name')
 inspection_date = fields.Date(string='Inspection Date')
 status = fields.Selection([('draft', 'Draft'), ('done', 'Done')])
"""
    generated_valid = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_valid,
        security_csv="",
        notes=""
    )

    # Should complete without raising any exception
    _validate_no_duplicate_field_declarations(generated_valid)
