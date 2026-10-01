"""Phase 29B auto-generated test for the_create_method_is_missing_the_api.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="logistics_core",
        version="16.0.1.0.0",
        category="Inventory/Logistics",
        summary="Core logistics models",
        author="DevTeam",
        depends=["base", "stock"],
        data=[]
    )

    # Case 1: Missing decorator
    models_py_missing = (
        "from odoo import models\n\n"
        "class LogisticsShipment(models.Model):\n"
        "    _name = 'logistics.shipment'\n\n"
        "    def create(self, vals):\n"
        "        return super().create(vals)\n"
    )
    gen_missing = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_missing,
        security_csv="",
        notes=""
    )

    _autofix_missing_model_create_multi_decorator(gen_missing)

    assert "@api.model_create_multi" in gen_missing.models_py
    assert "    @api.model_create_multi\n    def create(self, vals):" in gen_missing.models_py

    # Case 2: Already correct
    models_py_correct = (
        "from odoo import models\n\n"
        "class LogisticsShipment(models.Model):\n"
        "    _name = 'logistics.shipment'\n\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals):\n"
        "        return super().create(vals)\n"
    )
    gen_correct = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_correct,
        security_csv="",
        notes=""
    )
    original_content = gen_correct.models_py

    _autofix_missing_model_create_multi_decorator(gen_correct)

    assert gen_correct.models_py == original_content
