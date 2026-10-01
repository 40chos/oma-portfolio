"""_autofix_missing_model_create_multi_decorator() -- Phase 29B auto-generated draft, corrected
2026-08-02 for P11 fifth pass item 171
(docs/planning/PHASE30_SECOND_PASS_FINAL_CONSOLIDATED_2026-07-30.md §1.1): the original draft's
own Case 1 asserted the decorator gets added to a `def create(self, vals):` (singular) override --
which is the EXACT real bug item 171 identifies: decorating a singular-signature override with
`@api.model_create_multi` produces a real `AttributeError`/logic bug the first time `create()` is
actually called with a list, since the decorator guarantees a list but the old-style body expects
one dict. Corrected: Case 1 now uses the genuinely-multi `vals_list` signature (the only case this
mechanical fix can safely decorate), and a new case confirms the singular-`vals` signature is
correctly left untouched.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

from specialists.build.specialist import _autofix_missing_model_create_multi_decorator


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

    # Case 1: missing decorator on a genuinely multi-record signature -- must be decorated.
    models_py_missing = (
        "from odoo import models\n\n"
        "class LogisticsShipment(models.Model):\n"
        "    _name = 'logistics.shipment'\n\n"
        "    def create(self, vals_list):\n"
        "        return super().create(vals_list)\n"
    )
    gen_missing = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_missing,
        security_csv="",
        notes=""
    )

    _autofix_missing_model_create_multi_decorator(gen_missing)

    assert "@api.model_create_multi" in gen_missing.models_py
    assert "    @api.model_create_multi\n    def create(self, vals_list):" in gen_missing.models_py

    # Case 2: Already correct
    models_py_correct = (
        "from odoo import models\n\n"
        "class LogisticsShipment(models.Model):\n"
        "    _name = 'logistics.shipment'\n\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        return super().create(vals_list)\n"
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


def test_item171_never_decorates_a_singular_vals_signature():
    """The real fix: an old-style, previously-correct override (def create(self, vals): -- takes
    ONE dict, not a list) must never be decorated -- that would guarantee create() is called with a
    list while the body still expects a single dict, a real, live AttributeError/logic bug.
    """
    manifest = ManifestFields(
        name="logistics_core", version="16.0.1.0.0", category="Inventory/Logistics",
        summary="x", author="x", depends=["base"], data=[],
    )
    models_py = (
        "from odoo import models\n\n"
        "class LogisticsShipment(models.Model):\n"
        "    _name = 'logistics.shipment'\n\n"
        "    def create(self, vals):\n"
        "        vals['ref'] = self.env['ir.sequence'].next_by_code('logistics.shipment')\n"
        "        return super().create(vals)\n"
    )
    generated = GeneratedModuleFiles(manifest_fields=manifest, models_py=models_py, security_csv="", notes="")
    before = generated.models_py
    _autofix_missing_model_create_multi_decorator(generated)
    assert generated.models_py == before
    assert "@api.model_create_multi" not in generated.models_py
    print("PASS item171: a singular-vals create() override is never decorated")


if __name__ == "__main__":
    test_draft_catches_the_pattern()
    test_item171_never_decorates_a_singular_vals_signature()
    print("\nALL TESTS PASSED")
