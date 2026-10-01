"""Phase 29B auto-generated test for generated_xml_references_external_id_s.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    # Ensure KNOWN_EXTERNAL_IDS is available for the drafted validator
    # (In a real run this would be imported/defined globally, but we define it here for self-containment)
    import builtins
    if not hasattr(builtins, 'KNOWN_EXTERNAL_IDS'):
        globals()['KNOWN_EXTERNAL_IDS'] = {"base.view_res_partner_form", "sale.action_orders"}

    manifest = ManifestFields(
        name="test_module",
        version="1.0",
        category="Custom",
        summary="Test",
        author="Test Author",
        depends=["base"],
        data=["test_view.xml"]
    )

    # 1. Hallucinated reference (different from examples)
    bad_xml = '<menuitem ref="hr_expense.expense_type_draft"/>'
    gen_bad = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass M(models.Model):\n    _name = 'm.m'\n",
        views_xml=bad_xml,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    try:
        _validate_no_hallucinated_external_id_refs(gen_bad)
        assert False, "Expected ValueError for hallucinated ref"
    except ValueError as e:
        assert "hr_expense.expense_type_draft" in str(e)

    # 2. Legitimate reference
    good_xml = '<menuitem ref="base.view_res_partner_form"/>'
    gen_good = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass M(models.Model):\n    _name = 'm.m'\n",
        views_xml=good_xml,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    # Should not raise
    _validate_no_hallucinated_external_id_refs(gen_good)
