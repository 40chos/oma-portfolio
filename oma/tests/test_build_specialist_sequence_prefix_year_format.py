"""2026-08-05 (same-night full 30-task sweep, task006): unit tests for
_validate_sequence_prefix_matches_goal_example_year_format() -- the real, confirmed root cause
found live.

Root cause: task006's own goal explicitly gives an example format with a year component ("like
MW-2026-0001"), but the generated ir.sequence record's own prefix ("MW-") never included any
year placeholder at all -- producing 'MW-0001', silently missing the year the goal's own example
shows. Confirmed directly against the real committed sequence_data.xml content (read off gitea).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_sequence_prefix_matches_goal_example_year_format,
)

_MANIFEST = ManifestFields(
    name="x", version="1.0.0", category="Tools", summary="x", author="x", depends=["base"], data=[],
)

_TASK006_GOAL = (
    "Every fieldjob record should get a unique reference number like MW-2026-0001, "
    "auto-generated when created. The user should never have to type this."
)

_TASK006_OWN_REAL_SEQUENCE_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
    '  <record id="seq_project_fieldjob_ref" model="ir.sequence">\n'
    '    <field name="name">Project Fieldjob Reference</field>\n'
    '    <field name="code">project.fieldjob.ref</field>\n'
    '    <field name="prefix">MW-</field>\n'
    '    <field name="padding">4</field>\n'
    '    <field name="number_next">1</field>\n'
    '    <field name="number_increment">1</field>\n'
    '    <field name="implementation">no_gap</field>\n'
    "  </record>\n</odoo>"
)


def _generated(extra_data_files: dict[str, str]) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=_MANIFEST, models_py="", security_csv="x",
        extra_data_files=extra_data_files, notes="",
    )


def test_flags_task006s_own_real_missing_year_shape():
    generated = _generated({"data/sequence_data.xml": _TASK006_OWN_REAL_SEQUENCE_XML})
    raised = False
    try:
        _validate_sequence_prefix_matches_goal_example_year_format(generated, _TASK006_GOAL)
    except ValueError as exc:
        raised = True
        assert "MW-2026-0001" in str(exc)
        assert "%(year)s" in str(exc)
    assert raised, "a sequence prefix with no year placeholder, against a goal example that shows one, must be flagged"


def test_passes_when_prefix_already_has_year_placeholder():
    xml = (
        '<odoo><record id="x" model="ir.sequence">'
        '<field name="prefix">MW-%(year)s-</field>'
        '<field name="padding">4</field>'
        "</record></odoo>"
    )
    generated = _generated({"data/sequence_data.xml": xml})
    _validate_sequence_prefix_matches_goal_example_year_format(generated, _TASK006_GOAL)  # must not raise


def test_passes_when_suffix_has_the_year_placeholder_instead():
    xml = (
        '<odoo><record id="x" model="ir.sequence">'
        '<field name="prefix">MW-</field>'
        '<field name="suffix">-%(y)s</field>'
        "</record></odoo>"
    )
    generated = _generated({"data/sequence_data.xml": xml})
    _validate_sequence_prefix_matches_goal_example_year_format(generated, _TASK006_GOAL)  # must not raise


def test_no_op_when_goal_gives_no_concrete_year_example():
    """A goal that just says 'auto-generated reference' with no concrete LETTERS-YYYY-NNNN
    example at all must never be flagged -- inferring a year requirement from nothing is exactly
    the kind of guess this validator must never make."""
    goal = "Every fieldjob record should get a unique reference number, auto-generated when created."
    generated = _generated({"data/sequence_data.xml": _TASK006_OWN_REAL_SEQUENCE_XML})
    _validate_sequence_prefix_matches_goal_example_year_format(generated, goal)  # must not raise


def test_no_op_when_no_ir_sequence_record_present_at_all():
    generated = _generated({})
    _validate_sequence_prefix_matches_goal_example_year_format(generated, _TASK006_GOAL)  # must not raise


def test_no_op_when_prefix_letters_do_not_match_the_sequence_at_all():
    """Scoped to the SAME prefix letters the goal's own example names -- a completely unrelated
    sequence record elsewhere in the file must never be misjudged."""
    xml = (
        '<odoo><record id="x" model="ir.sequence">'
        '<field name="prefix">INV-</field>'
        "</record></odoo>"
    )
    generated = _generated({"data/other_sequence.xml": xml})
    _validate_sequence_prefix_matches_goal_example_year_format(generated, _TASK006_GOAL)  # must not raise
