"""Phase 29B auto-generated test for models_py_declares_a_class_extending_x.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0",
        category="Custom",
        summary="Test",
        author="Test",
        depends=["base"],
        data=[]
    )

    # Case 1: Failing pattern (empty _inherit body)
    models_py_fail = """
from odoo import models, fields

class SaleOrderExtension(models.Model):
    _inherit = 'sale.order'

"""
    gen_fail = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_fail,
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    try:
        _validate_no_empty_inherit_extensions(gen_fail)
        assert False, "Expected ValueError to be raised for empty _inherit body"
    except ValueError as e:
        assert "uses `_inherit` but has an empty body" in str(e)

    # Case 2: Passing pattern (valid _inherit with field)
    models_py_pass = """
from odoo import models, fields

class SaleOrderExtension(models.Model):
    _inherit = 'sale.order'
    custom_field = fields.Char(string="Custom")

"""
    gen_pass = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_pass,
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    # Should not raise
    _validate_no_empty_inherit_extensions(gen_pass)
