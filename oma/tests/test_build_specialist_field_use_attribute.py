"""2026-08-05 (same-night full 30-task sweep, task030): unit tests for
_autofix_strip_invalid_field_use_attribute() -- the real, confirmed root cause found live.

Root cause: the LLM wrote `<field name="body_html" use="1">` on a mail.template record's own
CDATA body field. `use=` has no real meaning there -- confirmed directly against Odoo's own real
`import_xml.rng` RelaxNG schema (the `field` element's own grammar): `use=` is ONLY ever a legal
`<field>` attribute when paired with `search=`. Standalone, it has no matching grammar branch,
and RelaxNG degrades to a misleading, unrelated error at the ENCLOSING <odoo> level ("Element
odoo has extra content: record") rather than naming the actual field/attribute at fault.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_strip_invalid_field_use_attribute,
)


def _make_generated(
    views_xml: str | None = None,
    security_xml: str | None = None,
    extra_data_files: dict[str, str] | None = None,
) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py="", security_csv="id,name\n",
        views_xml=views_xml, security_xml=security_xml, extra_data_files=extra_data_files, notes="",
    )


_TASK030_OWN_REAL_SHAPE = (
    '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n'
    '  <record id="email_template_fieldjob_customer" model="mail.template">\n'
    '    <field name="name">Fieldjob Customer Email Template</field>\n'
    '    <field name="model_id" ref="project_fieldjob.model_project_fieldjob"/>\n'
    '    <field name="subject">Fieldjob - {{object.name}}</field>\n'
    '    <field name="body_html" use="1"><![CDATA[<p>Hello,</p>]]></field>\n'
    "  </record>\n</odoo>"
)


def test_strips_invalid_use_attribute_from_task030s_own_real_shape():
    generated = _make_generated(extra_data_files={"data/email_templates.xml": _TASK030_OWN_REAL_SHAPE})
    _autofix_strip_invalid_field_use_attribute(generated)
    fixed = generated.extra_data_files["data/email_templates.xml"]
    assert "use=" not in fixed
    assert "<![CDATA[<p>Hello,</p>]]>" in fixed, "the field's own real content must never be touched"


def test_preserves_use_attribute_when_legitimately_paired_with_search():
    """The one real, legal pairing (search=...use=...) must never be touched."""
    xml = (
        '<odoo>\n  <record id="x" model="ir.actions.act_window">\n'
        '    <field name="domain" search="[]" use="1"/>\n'
        "  </record>\n</odoo>"
    )
    generated = _make_generated(views_xml=xml)
    before = generated.views_xml
    _autofix_strip_invalid_field_use_attribute(generated)
    assert generated.views_xml == before


def test_is_a_no_op_when_no_use_attribute_present():
    xml = '<odoo>\n  <record id="x" model="mail.template">\n    <field name="name">x</field>\n  </record>\n</odoo>'
    generated = _make_generated(views_xml=xml)
    before = generated.views_xml
    _autofix_strip_invalid_field_use_attribute(generated)
    assert generated.views_xml == before


def test_covers_security_xml_and_views_xml_too():
    xml = '<odoo>\n  <record id="x" model="mail.template">\n    <field name="body" use="1">x</field>\n  </record>\n</odoo>'
    generated = _make_generated(views_xml=xml, security_xml=xml)
    _autofix_strip_invalid_field_use_attribute(generated)
    assert "use=" not in generated.views_xml
    assert "use=" not in generated.security_xml


def test_no_op_when_all_xml_sources_are_none():
    generated = _make_generated()
    _autofix_strip_invalid_field_use_attribute(generated)  # must not raise
