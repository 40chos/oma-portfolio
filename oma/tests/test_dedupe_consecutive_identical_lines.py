"""Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
flagship run, service_ticket_model node): recurred identically across 5+ straight rounds, even
after multiple increasingly explicit correction notes -- the model kept writing
`vals_list = [dict(v) for v in vals_list]` two or three times in a row, back to back, at the top
of a create() override. One round even added a `# Remove redundant list comprehension` COMMENT
directly above the still-duplicated lines, correctly recognizing the problem in prose while
failing to actually fix it in code. Prompting alone never converged.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_dedupe_consecutive_identical_lines,
)


def _make_generated(models_py: str) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py=models_py, security_csv="x",
        security_xml=None, views_xml=None, notes="",
    )


def test_collapses_the_real_live_duplicate_closing_the_gap():
    models_py = (
        "from odoo import models, fields, api\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'oma.service.ticket'\n\n"
        "    name = fields.Char()\n\n"
        "    @api.model_create_multi\n"
        "    def create(self, vals_list):\n"
        "        vals_list = [dict(v) for v in vals_list]\n"
        "        vals_list = [dict(v) for v in vals_list]\n"
        "        # Remove redundant list comprehension\n"
        "        for vals in vals_list:\n"
        "            if not vals.get('name'):\n"
        "                vals['name'] = self.env['ir.sequence'].next_by_code('oma.service.ticket')\n"
        "        return super().create(vals_list)\n"
    )
    generated = _make_generated(models_py)
    _autofix_dedupe_consecutive_identical_lines(generated)

    assert generated.models_py.count("vals_list = [dict(v) for v in vals_list]") == 1, (
        f"expected the duplicate line collapsed to a single occurrence -- got:\n"
        f"{generated.models_py!r}"
    )
    assert "if not vals.get('name'):" in generated.models_py
    print("PASS: the real, confirmed live duplicate line is collapsed to one occurrence, "
          "closing the real live gap found on task 07141af5's service_ticket_model node "
          "(recurred across 5+ straight rounds/resumes)")


def test_collapses_three_consecutive_repeats_to_one():
    models_py = (
        "class X(models.Model):\n"
        "    def create(self, vals):\n"
        "        vals['x'] = 1\n"
        "        vals['x'] = 1\n"
        "        vals['x'] = 1\n"
        "        return super().create(vals)\n"
    )
    generated = _make_generated(models_py)
    _autofix_dedupe_consecutive_identical_lines(generated)
    assert generated.models_py.count("vals['x'] = 1") == 1
    print("PASS: three consecutive identical repeats collapse to a single occurrence")


def test_never_touches_non_consecutive_identical_lines():
    """Two identical lines that are NOT adjacent (real, separate statements in different
    branches, or a genuinely reused expression) must never be touched -- only an immediately
    repeated line is redundant by this fix's own safety argument.
    """
    models_py = (
        "class X(models.Model):\n"
        "    def create(self, vals):\n"
        "        vals['x'] = 1\n"
        "        vals['y'] = 2\n"
        "        vals['x'] = 1\n"
        "        return super().create(vals)\n"
    )
    generated = _make_generated(models_py)
    original = generated.models_py
    _autofix_dedupe_consecutive_identical_lines(generated)
    assert generated.models_py == original, (
        "non-adjacent identical lines are a completely different, legitimate shape -- must "
        "never be collapsed"
    )
    print("PASS: never touches identical lines that are not directly adjacent to each other")


def test_never_touches_consecutive_blank_lines_or_comments():
    models_py = (
        "class X(models.Model):\n"
        "    # a comment\n"
        "    # a comment\n"
        "\n"
        "\n"
        "    def create(self, vals):\n"
        "        return super().create(vals)\n"
    )
    generated = _make_generated(models_py)
    original = generated.models_py
    _autofix_dedupe_consecutive_identical_lines(generated)
    assert generated.models_py == original, (
        "repeated blank lines and repeated comment lines are never a behavioral bug this fix's "
        "own safety argument covers -- must remain untouched"
    )
    print("PASS: never touches consecutive blank lines or consecutive comment lines")


def test_is_a_noop_when_nothing_is_duplicated():
    models_py = (
        "class X(models.Model):\n"
        "    def create(self, vals):\n"
        "        vals['x'] = 1\n"
        "        return super().create(vals)\n"
    )
    generated = _make_generated(models_py)
    original = generated.models_py
    _autofix_dedupe_consecutive_identical_lines(generated)
    assert generated.models_py == original
    print("PASS: a no-op on genuinely clean, non-duplicated content")


if __name__ == "__main__":
    test_collapses_the_real_live_duplicate_closing_the_gap()
    test_collapses_three_consecutive_repeats_to_one()
    test_never_touches_non_consecutive_identical_lines()
    test_never_touches_consecutive_blank_lines_or_comments()
    test_is_a_noop_when_nothing_is_duplicated()
    print("\nALL DEDUPE-CONSECUTIVE-IDENTICAL-LINES TESTS PASSED")
