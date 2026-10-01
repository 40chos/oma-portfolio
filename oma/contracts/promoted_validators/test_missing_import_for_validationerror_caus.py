"""Phase 29B auto-generated test for missing_import_for_validationerror_caus.py -- executed for real during drafting (not just compile-checked) and passed. Review this test own correctness alongside the validator before promotion.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

def test_draft_catches_the_pattern():
       from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

       manifest = ManifestFields(
           name="test_module",
           version="1.0",
           category="Test",
           summary="Test summary",
           author="Test Author",
           depends=["base"],
           data=[]
       )

       # Case 1: Missing import for ValidationError
       models_py_bad = "from odoo import models\n\nclass TestModel(models.Model):\n    _name = 'test.model'\n\n    def _check(self):\n        raise ValidationError('Invalid')\n"
       gen_bad = GeneratedModuleFiles(
           manifest_fields=manifest,
           models_py=models_py_bad,
           security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
           notes=""
       )

       _autofix_missing_validation_error_import(gen_bad)
       assert "from odoo.exceptions import ValidationError" in gen_bad.models_py
       assert gen_bad.models_py.startswith("from odoo.exceptions import ValidationError\n\n")

       # Case 2: Already correct (import present)
       models_py_good = "from odoo import models\nfrom odoo.exceptions import ValidationError\n\nclass TestModel(models.Model):\n    _name = 'test.model'\n\n    def _check(self):\n        raise ValidationError('Invalid')\n"
       gen_good = GeneratedModuleFiles(
           manifest_fields=manifest,
           models_py=models_py_good,
           security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
           notes=""
       )
       original_good = gen_good.models_py

       _autofix_missing_validation_error_import(gen_good)
       assert gen_good.models_py == original_good
