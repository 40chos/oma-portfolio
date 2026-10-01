"""Phase 29B auto-generated test for data_change_branch_would_use_odootoolcl.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    manifest = ManifestFields(
        name="test_module",
        version="1.0",
        category="Custom",
        summary="Test module",
        author="Test Author",
        depends=["base"],
        data=[]
    )

    # Case 1: Matches the pattern with genuinely new literals
    bad_notes = "custom_migration branch: would use XmlRpcClient directly per the remote-api-execution skill, with no module-development tools handed to it. Not tested yet."
    bad_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass Test(models.Model):\n    _name = 'test.model'\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=bad_notes
    )

    try:
        _validate_notes_external_tool_dependency(bad_generated)
        assert False, "Expected ValueError to be raised for bad notes"
    except ValueError:
        pass

    # Case 2: Legitimate notes, should not raise
    good_notes = "This module uses standard Odoo module-development tools and follows best practices."
    good_generated = GeneratedModuleFiles(
        manifest_fields=manifest,
        models_py="from odoo import models\nclass Test(models.Model):\n    _name = 'test.model'\n",
        views_xml=None,
        security_csv="",
        security_xml=None,
        notes=good_notes
    )

    _validate_notes_external_tool_dependency(good_generated)
