"""Phase 29B auto-generated test for the_goal_unambiguously_states_new_model.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
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

    # Case 1: Goal requests new model, but models_py only has _inherit
    bad_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass PartnerExt(models.Model):\n    _inherit = 'res.partner'\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes="create a new model for tracking shipments"
    )

    try:
        _validate_goal_new_model_declared(bad_generated)
        assert False, "Expected ValueError to be raised"
    except ValueError as e:
        assert "lacks a '_name=' declaration" in str(e)

    # Case 2: Goal requests new model, and models_py correctly has _name
    good_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass ShipmentTracking(models.Model):\n    _name = 'shipment.tracking'\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes="add a new model for tracking shipments"
    )

    # Should not raise
    _validate_goal_new_model_declared(good_generated)
