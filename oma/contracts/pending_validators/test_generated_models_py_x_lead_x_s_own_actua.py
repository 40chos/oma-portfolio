"""Phase 29B auto-generated test for generated_models_py_x_lead_x_s_own_actua.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    # Case 1: Should raise ValueError (class has only allowed metadata attributes)
    bad_models_py = """from odoo import models

class Invoice(models.Model):
 \"\"\"Invoice model\"\"\"
 _name = 'account.invoice'
 _inherit = 'mail.thread'
 _description = 'Invoice'
"""
    bad_gen = GeneratedModuleFiles(
        manifest_fields=ManifestFields(name='test_mod', version='1.0.0', category='Hidden', summary='Test', author='Test', depends=[], data=[]),
        models_py=bad_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    try:
        _validate_models_py_classes_have_real_content(bad_gen)
        assert False, "Expected ValueError to be raised for empty class"
    except ValueError as e:
        assert "no real statement content" in str(e), f"Unexpected error message: {e}"

    # Case 2: Should NOT raise (class has a real field declaration)
    good_models_py = """from odoo import models, fields

class Invoice(models.Model):
 _name = 'account.invoice'
 total_amount = fields.Float(string='Total')
"""
    good_gen = GeneratedModuleFiles(
        manifest_fields=ManifestFields(name='test_mod', version='1.0.0', category='Hidden', summary='Test', author='Test', depends=[], data=[]),
        models_py=good_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    # Should complete without raising
    _validate_models_py_classes_have_real_content(good_gen)
