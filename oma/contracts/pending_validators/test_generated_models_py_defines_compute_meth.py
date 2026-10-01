"""Phase 29B auto-generated test for generated_models_py_defines_compute_meth.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0",
        category="Sales",
        summary="Test",
        author="Test Author",
        depends=["base"],
        data=[]
    )

    # Case 1: Orphaned compute method (should raise ValueError)
    models_py_orphaned = """
from odoo import models, fields

class TestModel(models.Model):
    _name = 'test.model'

    total_value = fields.Float()

    def _compute_total_value(self):
        self.total_value = 50.0
"""
    gen_orphaned = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_orphaned,
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    try:
        _validate_no_orphaned_compute_methods(gen_orphaned)
        assert False, "Expected ValueError for orphaned compute method"
    except ValueError:
        pass

    # Case 2: Properly wired compute method (should NOT raise)
    models_py_valid = """
from odoo import models, fields

class TestModel(models.Model):
    _name = 'test.model'

    total_value = fields.Float(compute='_compute_total_value')

    def _compute_total_value(self):
        self.total_value = 50.0
"""
    gen_valid = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_valid,
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    _validate_no_orphaned_compute_methods(gen_valid)
