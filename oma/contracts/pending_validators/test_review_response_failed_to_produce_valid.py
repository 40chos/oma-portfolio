"""Phase 29B auto-generated test for review_response_failed_to_produce_valid.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="validation_test_module",
        version="16.0.1.0.0",
        category="Testing",
        summary="Module for validator testing",
        author="Test Author",
        depends=["base", "mail"],
        data=["test_data.xml"]
    )

    # Test 1: Malformed notes matching the exact failure pattern (new literals)
    bad_notes = "review response failed to produce valid JSON after 5 attempts: Expecting ',' delimiter: line 3042 column 8 (char 112450)"
    generated_bad = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass TestModel(models.Model):\n    _name = 'test.model'\n",
        views_xml=None,
        security_csv="id,name,perm_read,perm_write,perm_create,perm_unlink\n1,group_test,1,1,1,1\n",
        security_xml=None,
        notes=bad_notes
    )

    try:
        _validate_review_response_json_parsing_integrity(generated_bad)
        assert False, "Expected ValueError for malformed review response"
    except ValueError:
        pass

    # Test 2: Legitimate notes that should not trigger the validator
    good_notes = "Review completed successfully. All JSON payloads parsed correctly. No syntax errors detected."
    generated_good = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass ValidModel(models.Model):\n    _name = 'valid.model'\n",
        views_xml=None,
        security_csv="id,name,perm_read,perm_write,perm_create,perm_unlink\n1,group_valid,1,1,1,1\n",
        security_xml=None,
        notes=good_notes
    )

    try:
        _validate_review_response_json_parsing_integrity(generated_good)
    except ValueError:
        assert False, "Unexpected ValueError raised for valid notes"
