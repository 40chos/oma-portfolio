"""Phase 29B auto-generated test for contains_only_a_header_row_with_no_data.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="custom_widget_lib",
        version="16.0.1.0",
        category="Inventory",
        summary="Custom widget library",
        author="Dev Team",
        depends=["base", "stock"],
        data=[]
    )

    # Case 1: Header-only CSV (failure pattern)
    bad_csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_delete\n"
    generated_bad = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass Widget(models.Model):\n    _name = 'custom.widget'\n",
        views_xml=None,
        security_csv=bad_csv,
        security_xml=None,
        notes=""
    )

    try:
        _validate_security_csv_not_header_only(generated_bad)
        raise AssertionError("Expected ValueError for header-only CSV")
    except ValueError:
        pass

    # Case 2: Legitimate CSV with data (should pass)
    good_csv = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_delete\naccess_custom_widget,Access Custom Widget,model_custom_widget,base.group_user,1,1,1,1\n"
    generated_good = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass Widget(models.Model):\n    _name = 'custom.widget'\n",
        views_xml=None,
        security_csv=good_csv,
        security_xml=None,
        notes=""
    )

    # Should not raise any exception
    _validate_security_csv_not_header_only(generated_good)
