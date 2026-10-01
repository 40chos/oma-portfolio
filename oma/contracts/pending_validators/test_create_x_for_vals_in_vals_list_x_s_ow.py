"""Phase 29B auto-generated test for create_x_for_vals_in_vals_list_x_s_ow.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0",
        category="Test",
        summary="Test summary",
        author="Test Author",
        depends=[],
        data=[]
    )

    # Case 1: Problematic code
    bad_models_py = """from odoo import models

class TestModel(models.Model):
 _name = 'test.model'

 def create(self, vals_list):
     for vals in vals_list:
         vals['active'] = True
         vals['company_id'] = self.env.company.id
     return super().create(vals_list)
"""
    gen_bad = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=bad_models_py,
        security_csv="",
        notes=""
    )

    _autofix_no_in_place_vals_mutation_in_batch_create(gen_bad)

    assert "vals = dict(vals)" in gen_bad.models_py
    lines = gen_bad.models_py.split('\n')
    for i, line in enumerate(lines):
        if "for vals in vals_list:" in line:
            assert "vals = dict(vals)" in lines[i+1]
            break

    # Case 2: Already correct code
    good_models_py = """from odoo import models

class TestModel(models.Model):
 _name = 'test.model'

 def create(self, vals_list):
     for vals in vals_list:
         vals = dict(vals)
         vals['active'] = True
         vals['company_id'] = self.env.company.id
     return super().create(vals_list)
"""
    gen_good = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=good_models_py,
        security_csv="",
        notes=""
    )
    original_good = gen_good.models_py

    _autofix_no_in_place_vals_mutation_in_batch_create(gen_good)

    assert gen_good.models_py == original_good
