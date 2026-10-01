"""Phase 29B auto-generated test for raise_validationerror_usererror_x_use.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0.0",
        category="Custom",
        summary="Test",
        author="Test Author",
        depends=[],
        data=[]
    )

    bad_models_py = """
from odoo import models, fields, _
from odoo.exceptions import ValidationError

class TestModel(models.Model):
    _name = 'test.model'
    value = fields.Float()

    def check_value(self):
        if self.value < 0:
            raise ValidationError('Value must be positive.')
"""
    bad_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=bad_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    raised_error = False
    try:
        _validate_raise_error_strings_use_translate_call(bad_generated)
    except ValueError:
        raised_error = True
    assert raised_error, "Validator should raise ValueError for untranslated string literals."

    good_models_py = """
from odoo import models, fields, _
from odoo.exceptions import ValidationError

class TestModel(models.Model):
    _name = 'test.model'
    value = fields.Float()

    def check_value(self):
        if self.value < 0:
            raise ValidationError(_('Value must be positive.'))
"""
    good_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=good_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    _validate_raise_error_strings_use_translate_call(good_generated)
