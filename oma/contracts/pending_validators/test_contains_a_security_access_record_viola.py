"""Phase 29B auto-generated test for contains_a_security_access_record_viola.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    # Case 1: Contains security records (should raise ValueError)
    manifest_bad = ManifestFields(
        name="hr_custom_extension",
        version="16.0.1.0.0",
        category="Human Resources",
        summary="Custom HR module for testing",
        author="Dev Team",
        depends=["base", "hr"],
        data=[]
    )
    generated_bad = GeneratedModuleFiles(
        manifest_fields=manifest_bad,
        models_py="from odoo import models\n\nclass HrEmployeeCustom(models.Model):\n    _name = 'hr.employee.custom'\n    custom_field = models.Char()\n",
        views_xml=None,
        security_csv="id,name,perm_read,perm_write,perm_create,perm_unlink\n1,access_hr_employee_custom,1,1,1,1\n",
        security_xml=None,
        notes="Generated with security records by mistake"
    )
    try:
        _validate_no_security_records(generated_bad)
        assert False, "Expected ValueError to be raised"
    except ValueError as e:
        assert "security access records" in str(e)

    # Case 2: No security records (should pass without raising)
    manifest_good = ManifestFields(
        name="sale_custom_extension",
        version="16.0.1.0.0",
        category="Sales",
        summary="Custom Sale module for testing",
        author="Dev Team",
        depends=["base", "sale"],
        data=[]
    )
    generated_good = GeneratedModuleFiles(
        manifest_fields=manifest_good,
        models_py="from odoo import models\n\nclass SaleOrderCustom(models.Model):\n    _name = 'sale.order.custom'\n    extra_note = models.Text()\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes="Generated correctly without security records"
    )
    _validate_no_security_records(generated_good)
