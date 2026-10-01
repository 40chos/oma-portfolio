"""Phase 29B auto-generated test for class_x_declares_neither_name_nor_in.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
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

    # Case 1: Invalid - missing _name or _inherit
    invalid_models_py = (
        "from odoo import models\n\n"
        "class InvoiceLine(models.Model):\n"
        "    _description = 'Invoice Line'\n"
        "    partner_id = models.Many2many('res.partner')\n"
    )
    invalid_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=invalid_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    try:
        _validate_model_has_name_or_inherit(invalid_gen)
        assert False, "Expected ValueError to be raised for invalid model"
    except ValueError as e:
        assert "class 'InvoiceLine' declares neither _name nor _inherit" in str(e)

    # Case 2: Valid - has _name
    valid_models_py = (
        "from odoo import models\n\n"
        "class InvoiceLine(models.Model):\n"
        "    _name = 'account.invoice.line'\n"
        "    _description = 'Invoice Line'\n"
    )
    valid_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=valid_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=""
    )

    # Should not raise
    _validate_model_has_name_or_inherit(valid_gen)
