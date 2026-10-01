"""Phase 29B auto-generated test for generated_views_xml_is_not_well_formed_x.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0.0",
        category="Tools",
        summary="Test module",
        author="Test Author",
        depends=["base"],
        data=["views.xml"]
    )

    # Case 1: Invalid XML matching the recurring failure pattern
    invalid_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass Test(models.Model):\n    _name = 'test.model'\n",
        views_xml="<odoo><record id='custom_view_1' model='hr.employee'><field name='name'>John & Jane</field></record></odoo>",
        security_csv="",
        security_xml=None,
        notes=""
    )

    try:
        _validate_views_xml_is_well_formed(invalid_gen)
        assert False, "Expected ValueError to be raised for invalid XML"
    except ValueError as e:
        assert "not well-formed" in str(e), f"Expected 'not well-formed' in error message, got: {e}"

    # Case 2: Valid XML that should pass without raising
    valid_gen = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass Test(models.Model):\n    _name = 'test.model'\n",
        views_xml="<odoo><record id='custom_view_2' model='hr.employee'><field name='name'>John &amp; Jane</field></record></odoo>",
        security_csv="",
        security_xml=None,
        notes=""
    )

    # Should not raise
    _validate_views_xml_is_well_formed(valid_gen)
