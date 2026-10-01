"""Phase 30, P2 (§9, closes Problem F): tests for the pieces of
startup orphan-resume that don't need a live gateway/sandbox --
_commit_content_still_compiles() (manager/loop.py), the plain re-
verification gate the plan's own technical design step 2 requires
before ever resuming onto a checked-out commit. The wider mechanism
(get_latest_resume_point()'s round_checkpoint/replan_round fallback,
resume_orphaned_task_at_startup()'s end-to-end resume) was verified
live against the real system instead -- restarting oma-chat-ui.service
mid-round on a real task and confirming, from the real DB, that it
resumed from its last commit_sha rather than restarting from round 1
or being silently dropped (see docs/reports/PHASE30_P2_CHECKPOINT_
RESUME_VERIFICATION_2026-07-30.md for the full real trace).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.loop import _commit_content_still_compiles


def test_accepts_genuinely_valid_python_and_xml():
    files = {
        "my_module/models/models.py": "from odoo import models, fields\n\nclass X(models.Model):\n    _name = 'x'\n",
        "my_module/views/views.xml": "<odoo><record id='a' model='ir.ui.view'/></odoo>",
    }
    ok, reason = _commit_content_still_compiles(files)
    assert ok is True
    assert reason == ""
    print("PASS: genuinely valid Python + XML content is accepted")


def test_rejects_a_python_file_with_a_real_syntax_error():
    files = {
        "my_module/models/models.py": "def create(self, vals\n    pass\n",  # missing colon
    }
    ok, reason = _commit_content_still_compiles(files)
    assert ok is False
    assert "models.py" in reason
    print("PASS: a Python file that no longer parses is correctly rejected, not trusted")


def test_rejects_malformed_xml():
    files = {
        "my_module/views/views.xml": "<odoo><record id='a'></odoo>",  # unclosed record tag
    }
    ok, reason = _commit_content_still_compiles(files)
    assert ok is False
    assert "views.xml" in reason
    print("PASS: malformed XML (a checkpoint taken mid-write) is correctly rejected")


def test_ignores_non_code_files():
    files = {
        "my_module/README.md": "not valid python at all ((((",
        "my_module/security/ir.model.access.csv": "id,name,model_id:id\n",
    }
    ok, reason = _commit_content_still_compiles(files)
    assert ok is True
    print("PASS: only .py/.xml files are re-verified -- other file types never falsely block a resume")


if __name__ == "__main__":
    test_accepts_genuinely_valid_python_and_xml()
    test_rejects_a_python_file_with_a_real_syntax_error()
    test_rejects_malformed_xml()
    test_ignores_non_code_files()
    print("\nALL STARTUP-ORPHAN-RESUME TESTS PASSED")
