"""Phase 29B auto-generated test for generated_models_py_defines_method_s_x.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0.0",
        category="Custom",
        summary="Test module",
        author="Tester",
        depends=[],
        data=[]
    )

    # Case 1: Duplicate method definitions (should raise ValueError)
    models_py_dup = """from odoo import models

class TestModel(models.Model):
 _name = 'test.model'

 def compute_total_amount(self):
     return 100.0

 def compute_total_amount(self):
     return 200.0
"""
    gen_dup = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_dup,
        views_xml=None,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    try:
        _validate_no_duplicate_method_definitions_in_models_py(gen_dup)
        assert False, "Expected ValueError to be raised for duplicate method definitions"
    except ValueError as e:
        assert "compute_total_amount" in str(e)

    # Case 2: Valid, no duplicates (should not raise)
    models_py_valid = """from odoo import models

class TestModel(models.Model):
 _name = 'test.model'

 def compute_total_amount(self):
     return 100.0

 def compute_tax_rate(self):
     return 0.1
"""
    gen_valid = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_valid,
        views_xml=None,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    _validate_no_duplicate_method_definitions_in_models_py(gen_valid)
    assert True
