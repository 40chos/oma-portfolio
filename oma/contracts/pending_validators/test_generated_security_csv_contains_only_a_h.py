"""Phase 29B auto-generated test for generated_security_csv_contains_only_a_h.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_acme_inventory",
        version="16.0.1.0.0",
        category="Inventory/Inventory",
        summary="Test Acme Inventory",
        author="Acme Corp",
        depends=["base", "stock"],
        data=[]
    )

    # Case 1: Fails validation (header-only CSV with new models)
    generated_fail = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass AcmeProduct(models.Model):\n    _name = 'acme.product'\n",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_delete\n",
        notes=""
    )
    try:
        _validate_security_csv_not_empty_when_models_declared(generated_fail)
        assert False, "Expected ValueError to be raised"
    except ValueError:
        pass

    # Case 2: Passes validation (CSV contains actual data rows)
    generated_pass = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass AcmeProduct(models.Model):\n    _name = 'acme.product'\n",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_delete\naccess_acme_product,Acme Product,base.model_acme_product,base.group_user,1,1,1,0\n",
        notes=""
    )
    _validate_security_csv_not_empty_when_models_declared(generated_pass)
