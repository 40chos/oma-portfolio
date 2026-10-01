"""Phase 29B auto-generated test for generated_models_py_assigns_x_more_tha.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    # Case 1: Duplicate special attribute should raise ValueError
    bad_manifest = ManifestFields(
        name="test_module_bad",
        version="1.0.0",
        category="Testing",
        summary="Module with duplicate attrs",
        author="Validator Tester",
        depends=["base"],
        data=[]
    )
    bad_models_py = """from odoo import models

class TestModel(models.Model):
 _name = 'test.model'
 _description = 'Test Model'
 _name = 'test.model.duplicate'
"""
    bad_generated = GeneratedModuleFiles(
        manifest_fields=bad_manifest,
        models_py=bad_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    try:
        _validate_no_duplicate_special_model_attributes(bad_generated)
        assert False, "Expected ValueError to be raised for duplicate _name"
    except ValueError as e:
        assert "Duplicate assignment of special model attribute '_name'" in str(e)

    # Case 2: Legitimate content should NOT raise ValueError
    good_manifest = ManifestFields(
        name="test_module_good",
        version="1.0.0",
        category="Testing",
        summary="Module with unique attrs",
        author="Validator Tester",
        depends=["base"],
        data=[]
    )
    good_models_py = """from odoo import models

class AnotherModel(models.Model):
 _name = 'another.model'
 _description = 'Another Model'
 _rec_name = 'display_name'
"""
    good_generated = GeneratedModuleFiles(
        manifest_fields=good_manifest,
        models_py=good_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    try:
        _validate_no_duplicate_special_model_attributes(good_generated)
    except ValueError:
        assert False, "Unexpected ValueError raised for valid models_py"
