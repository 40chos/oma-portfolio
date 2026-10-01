"""Phase 29B auto-generated test for the_access_control_file_is_empty_failin.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

from specialists.build.specialist import _validate_security_csv_not_empty


def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="hr_custom_extension",
        version="1.0.0",
        category="Human Resources",
        summary="Adds custom fields to HR module",
        author="QA Team",
        depends=["base", "hr"],
        data=[]
    )

    # Case 1: Empty/whitespace security CSV (matches the failure pattern)
    generated_empty = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass HrEmployeeCustom(models.Model):\n    _name = 'hr.employee.custom'\n",
        views_xml=None,
        security_csv="   \n  ",
        security_xml=None,
        notes=""
    )
    try:
        _validate_security_csv_not_empty(generated_empty)
        assert False, "Expected ValueError to be raised for empty security CSV"
    except ValueError:
        pass

    # Case 2: Valid security CSV (should pass without raising)
    generated_valid = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\n\nclass HrEmployeeCustom(models.Model):\n    _name = 'hr.employee.custom'\n",
        views_xml=None,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_hr_employee_custom,access.hr.employee.custom,model_hr_employee_custom,base.group_user,1,0,0,0\n",
        security_xml=None,
        notes=""
    )
    _validate_security_csv_not_empty(generated_valid)
    assert True
