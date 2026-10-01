"""Phase 29B auto-generated test for the_module_does_not_implement_the_requir.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="hr_dept_auto",
        version="1.0.0",
        category="Human Resources",
        summary="Auto-fill manager on department change",
        author="QA Bot",
        depends=["base", "hr"],
        data=[]
    )

    # Case 1: Missing implementation (should raise ValueError)
    notes_missing = "The module must implement the required _onchange_department_id method to auto-fill manager_id from department_id.manager_id."
    models_py_missing = """from odoo import models

class HrEmployee(models.Model):
 _name = 'hr.employee'
 department_id = models.Many2one('hr.department')
 manager_id = models.Many2one('hr.employee')
"""
    gen_missing = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_missing,
        views_xml=None,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        security_xml=None,
        notes=notes_missing
    )

    try:
        _validate_missing_onchange_method(gen_missing)
        assert False, "Expected ValueError for missing _onchange_department_id"
    except ValueError as e:
        assert "_onchange_department_id" in str(e)

    # Case 2: Correct implementation (should NOT raise ValueError)
    notes_present = "The module must implement the required _onchange_department_id method to auto-fill manager_id from department_id.manager_id."
    models_py_present = """from odoo import models

class HrEmployee(models.Model):
 _name = 'hr.employee'
 department_id = models.Many2one('hr.department')
 manager_id = models.Many2one('hr.employee')

 def _onchange_department_id(self):
     if self.department_id:
         self.manager_id = self.department_id.manager_id
"""
    gen_present = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py=models_py_present,
        views_xml=None,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        security_xml=None,
        notes=notes_present
    )

    try:
        _validate_missing_onchange_method(gen_present)
    except ValueError:
        assert False, "Unexpected ValueError for correctly implemented _onchange_department_id"
