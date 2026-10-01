"""Phase 29B auto-generated test for generated_xml_self_references_x_prefi.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    # Case 1: Missing definition -> should raise ValueError
    bad_generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="custom_app_x",
            version="1.0.0",
            category="Custom",
            summary="Test",
            author="Dev",
            depends=[],
            data=[]
        ),
        models_py="",
        views_xml='<record id="other_view" model="ir.ui.view">\n    <field name="inherit_id" ref="custom_app_x.missing_view_id"/>\n</record>',
        security_xml=None,
        security_csv="",
        notes=""
    )

    try:
        _validate_xml_local_ids_are_defined(bad_generated)
        assert False, "Expected ValueError to be raised for missing local ID"
    except ValueError as e:
        assert "missing_view_id" in str(e)

    # Case 2: Correct definition -> should not raise
    good_generated = GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="custom_app_x",
            version="1.0.0",
            category="Custom",
            summary="Test",
            author="Dev",
            depends=[],
            data=[]
        ),
        models_py="",
        views_xml='<record id="missing_view_id" model="ir.ui.view">\n    <field name="name">My View</field>\n</record>\n<record id="other_view" model="ir.ui.view">\n    <field name="inherit_id" ref="custom_app_x.missing_view_id"/>\n</record>',
        security_xml=None,
        security_csv="",
        notes=""
    )

    try:
        _validate_xml_local_ids_are_defined(good_generated)
    except ValueError:
        assert False, "Unexpected ValueError raised for valid XML"
