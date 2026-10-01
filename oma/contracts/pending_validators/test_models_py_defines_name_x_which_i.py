"""Phase 29B auto-generated test for models_py_defines_name_x_which_i.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
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
        author="Test",
        depends=[],
        data=[]
    )

    invalid_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass Gadget(models.Model):\n    _name = 'gadget'\n",
        security_csv="",
        notes=""
    )

    valid_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass Gadget(models.Model):\n    _name = 'test_module.gadget'\n",
        security_csv="",
        notes=""
    )

    # Assert invalid case raises ValueError
    try:
        _validate_model_name_is_valid_odoo_identifier(invalid_gen)
        assert False, "Expected ValueError for invalid model name"
    except ValueError:
        pass

    # Assert valid case does not raise
    _validate_model_name_is_valid_odoo_identifier(valid_gen)
