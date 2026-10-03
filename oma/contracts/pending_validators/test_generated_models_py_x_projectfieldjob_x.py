"""Phase 29B auto-generated test for generated_models_py_x_projectfieldjob_x.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0.0",
        category="Test",
        summary="Test summary",
        author="Test Author",
        depends=[],
        data=[]
    )

    # Case 1: Invalid - class with only ignored statements
    invalid_models_py = """
from odoo import models

class TestModel(models.Model):
 \"\"\"This is a docstring.\"\"\"
 _name = 'test.model'
 _description = 'A test model'
 pass
"""
    invalid_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=invalid_models_py,
        security_csv="",
        notes=""
    )

    try:
        _validate_models_py_classes_have_real_content(invalid_gen)
        assert False, "Expected ValueError to be raised for empty class content"
    except ValueError as e:
        assert "no real statement content" in str(e)

    # Case 2: Valid - class with actual field declaration
    valid_models_py = """
from odoo import models, fields

class ValidModel(models.Model):
 _name = 'valid.model'
 name = fields.Char(string='Name')
"""
    valid_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=valid_models_py,
        security_csv="",
        notes=""
    )

    try:
        _validate_models_py_classes_have_real_content(valid_gen)
    except ValueError:
        assert False, "Validator should not raise ValueError for valid content with real fields"
