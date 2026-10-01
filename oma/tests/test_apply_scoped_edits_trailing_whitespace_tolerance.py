"""Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run, project_ticket_counts
node), root-caused via direct inspection of the actual Redis-logged model output (not inferred):
the model was correctly writing the fix -- the full ProjectProject class with both fields and the
compute method -- essentially verbatim, on scoped-edit and full-rewrite attempts alike. But
`_apply_scoped_edits()`'s own `content.count(edit.target)` exact-match check rejected a genuinely
correct edit purely because of an incidental trailing-whitespace difference: the model's own
`target` ended `"...project.project'\n    \n    "`, while the real file content had one
additional trailing blank line the target never included. The model had correctly identified and
reproduced the real span it meant to replace; only the exact byte-count of trailing whitespace
differed -- never load-bearing for WHICH span is meant, and never a genuine content mismatch.

The fix: on an exact-match failure, retry tolerantly -- match the target's own meaningful content
exactly (everything up to its own trailing whitespace), but allow the ACTUAL trailing whitespace
in the real file to differ in amount. Deliberately narrow: only trailing whitespace becomes
flexible; every other character (including leading whitespace and anything in the middle) must
still match exactly, so this can never paper over a genuine content mismatch.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleEdit,
    ScopedEditApplicationError,
    _apply_scoped_edits,
)


def test_trailing_blank_line_mismatch_still_applies_the_edit():
    # The exact real incident shape: the real file has ONE extra trailing blank line the
    # model's own target didn't include.
    prior_files = {
        "models/models.py": (
            "from odoo import models, fields\n\n\n"
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n"
            "    \n"
            "    \n"  # the extra trailing blank line the model's target never included
        ),
    }
    edit = GeneratedModuleEdit(
        file="models/models.py",
        operation="search_replace",
        target=(
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n"
            "    \n"
            "    "  # note: no trailing newline, one fewer blank-line character than the real file
        ),
        content=(
            "class ProjectProject(models.Model):\n"
            "    _inherit = 'project.project'\n\n"
            "    open_ticket_count = fields.Integer(compute='_compute_ticket_counts')\n\n"
            "    def _compute_ticket_counts(self):\n"
            "        pass\n"
        ),
    )
    result = _apply_scoped_edits(prior_files, [edit])
    assert "open_ticket_count" in result["models/models.py"], (
        f"the field must actually land in the applied result -- got: {result['models/models.py']!r}"
    )
    assert "_compute_ticket_counts" in result["models/models.py"]
    print("PASS: a trailing-whitespace-only mismatch no longer rejects an otherwise correct edit")


def test_a_genuine_content_mismatch_still_raises():
    # The tolerance must be narrow -- a target that's wrong anywhere OTHER than trailing
    # whitespace must still fail loudly, never silently guess.
    prior_files = {"models/models.py": "class Foo(models.Model):\n    _name = 'oma.foo'\n"}
    edit = GeneratedModuleEdit(
        file="models/models.py",
        operation="search_replace",
        target="class Foo(models.Model):\n    _name = 'oma.bar'\n",  # wrong model name, not just whitespace
        content="class Foo(models.Model):\n    _name = 'oma.foo'\n    x = fields.Integer()\n",
    )
    try:
        _apply_scoped_edits(prior_files, [edit])
        assert False, "a genuine content mismatch (not just trailing whitespace) must still raise"
    except ScopedEditApplicationError as exc:
        assert "was not found" in str(exc)
        print("PASS: a genuine content mismatch (beyond trailing whitespace) still raises, never silently guessed")


def test_exact_match_still_wins_when_available_no_behavior_change():
    # When the target already matches exactly, behavior must be byte-for-byte identical to
    # before this fix -- the tolerant fallback must never even be consulted.
    prior_files = {"models/models.py": "class Foo(models.Model):\n    _name = 'oma.foo'\n"}
    edit = GeneratedModuleEdit(
        file="models/models.py",
        operation="search_replace",
        target="_name = 'oma.foo'",
        content="_name = 'oma.foo'\n    x = fields.Integer()",
    )
    result = _apply_scoped_edits(prior_files, [edit])
    assert result["models/models.py"] == (
        "class Foo(models.Model):\n    _name = 'oma.foo'\n    x = fields.Integer()\n"
    )
    print("PASS: an already-exact match is applied unchanged, no behavior change for the common case")


def test_ambiguous_tolerant_match_still_raises():
    # If the trailing-whitespace-tolerant pattern matches more than once, this must still
    # refuse to guess, exactly like the existing exact-match ambiguity check.
    prior_files = {
        "models/models.py": (
            "class Foo(models.Model):\n    _inherit = 'x'\n  \nclass Bar(models.Model):\n"
            "    _inherit = 'x'\n \n"
        ),
    }
    edit = GeneratedModuleEdit(
        file="models/models.py",
        operation="search_replace",
        target="    _inherit = 'x'\n ",
        content="    _inherit = 'x'\n    y = fields.Integer()\n",
    )
    try:
        _apply_scoped_edits(prior_files, [edit])
        assert False, "an ambiguous tolerant match (2+ spans) must still raise, never guess"
    except ScopedEditApplicationError as exc:
        assert "ambiguous" in str(exc)
        print("PASS: an ambiguous trailing-whitespace-tolerant match still raises, never guesses")


if __name__ == "__main__":
    test_trailing_blank_line_mismatch_still_applies_the_edit()
    test_a_genuine_content_mismatch_still_raises()
    test_exact_match_still_wins_when_available_no_behavior_change()
    test_ambiguous_tolerant_match_still_raises()
    print("\nALL APPLY-SCOPED-EDITS TRAILING-WHITESPACE-TOLERANCE TESTS PASSED")
