"""Phase 29B auto-generated test for method_x_calls_self_message_post.py -- executed for real during drafting (not just compile-checked) and passed. Review this test's own correctness alongside the validator before promotion; a generated test is only as trustworthy as a human confirms it to be.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from specialists.build.specialist import GeneratedModuleFiles, ManifestFields

def test_draft_catches_the_pattern():
    # 1. Bad case: should raise ValueError
    bad_models_py = """
from odoo import models

class ProjectTask(models.Model):
    _name = 'project.task'

    def _notify_manager(self):
        self.message_post(body="Task completed")
"""
    bad_manifest = ManifestFields(
        name="test_bad_module",
        version="1.0",
        category="Test",
        summary="Test",
        author="Test",
        depends=[],
        data=[]
    )
    bad_generated = GeneratedModuleFiles(
        manifest_fields=bad_manifest,
        models_py=bad_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    try:
        _validate_message_post_requires_partner_ids_or_subscribe(bad_generated)
        assert False, "Expected ValueError for bad code"
    except ValueError:
        pass

    # 2. Good case: should NOT raise
    good_models_py = """
from odoo import models

class ProjectTask(models.Model):
    _name = 'project.task'

    def _notify_manager(self):
        self.message_subscribe(partner_ids=[1, 2])
        self.message_post(body="Task completed")
"""
    good_manifest = ManifestFields(
        name="test_good_module",
        version="1.0",
        category="Test",
        summary="Test",
        author="Test",
        depends=[],
        data=[]
    )
    good_generated = GeneratedModuleFiles(
        manifest_fields=good_manifest,
        models_py=good_models_py,
        views_xml=None,
        security_csv="",
        security_xml=None,
        extra_data_files=None,
        tests_py=None,
        notes=""
    )

    try:
        _validate_message_post_requires_partner_ids_or_subscribe(good_generated)
    except ValueError:
        assert False, "Unexpected ValueError for good code"
