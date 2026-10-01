"""Phase 29B auto-generated test for generated_module_references_method_s_by.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
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
        author="Test Author",
        depends=[],
        data=[]
    )

    # Case 1: Failing pattern (button references a method not in models_py)
    failing_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass TestModel(models.Model):\n    _name = 'test.model'\n\n    def action_draft_invoice(self):\n        pass\n",
        views_xml='<form><button type="object" name="action_approve_invoice">Approve</button></form>',
        security_csv="id,name,group_id\n",
        notes=""
    )

    try:
        _validate_view_button_methods_exist(failing_gen)
        assert False, "Expected ValueError to be raised for missing method"
    except ValueError:
        pass

    # Case 2: Passing pattern (button references a method that IS in models_py)
    passing_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass TestModel(models.Model):\n    _name = 'test.model'\n\n    def action_approve_invoice(self):\n        pass\n",
        views_xml='<form><button type="object" name="action_approve_invoice">Approve</button></form>',
        security_csv="id,name,group_id\n",
        notes=""
    )

    try:
        _validate_view_button_methods_exist(passing_gen)
    except ValueError:
        assert False, "Validator incorrectly raised ValueError on valid content"
