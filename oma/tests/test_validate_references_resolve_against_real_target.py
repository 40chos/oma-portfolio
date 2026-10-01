"""P11 addition (2026-07-31), filed via
docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md item #1 --
tests for _validate_references_resolve_against_real_target(), built
directly to catch the exact real bug shapes P7's Tier 3 found live: a
hallucinated external id (base.view_res_partner_tree vs. the real
base.view_partner_tree) and a hallucinated xpath target
(//group[@name='contact'] against base.view_partner_form's real arch --
the SAME hallucination independently generated on two separate real
tasks). Uses a mocked schema-client (no live DB connection needed) so
these tests run anywhere, matching this project's own established
pattern (test_list_custom_models_fast_batching.py).
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _unescape_xml_attr,
    _validate_references_resolve_against_real_target,
)

_REAL_PARTNER_FORM_ARCH = """<form string="Partners">
    <sheet>
        <group>
            <group name="main">
                <field name="name"/>
            </group>
        </group>
    </sheet>
</form>"""


def _make_generated(views_xml: str | None = None, security_xml: str | None = None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="oma_test", version="16.0.1.0.0", category="Uncategorized", summary="test",
            author="test", depends=["base"], data=[],
        ),
        models_py="from odoo import models\n",
        views_xml=views_xml,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        security_xml=security_xml,
        notes="",
    )


def test_unescape_xml_attr():
    assert _unescape_xml_attr("&#39;contact&#39;") == "'contact'"
    assert _unescape_xml_attr("&quot;x&quot;") == '"x"'
    print("PASS: _unescape_xml_attr handles the real XML entity forms")


def test_rejects_hallucinated_external_id():
    views_xml = (
        '<odoo><record id="v1" model="ir.ui.view">'
        '<field name="name">x</field>'
        '<field name="inherit_id" ref="base.view_res_partner_tree"/>'
        '</record></odoo>'
    )
    generated = _make_generated(views_xml=views_xml)
    with patch(
        "specialists.build.specialist.external_id_exists_fast",
        return_value=False,
    ), patch(
        "specialists.build.specialist.get_view_arch_by_xmlid_fast",
        return_value=None,
    ):
        try:
            asyncio.run(_validate_references_resolve_against_real_target(
                generated, db="odoo16_dev", module_name="oma_test",
            ))
            raise AssertionError("expected ValueError for hallucinated external id")
        except ValueError as exc:
            assert "base.view_res_partner_tree" in str(exc)
    print("PASS: rejects a hallucinated external id (base.view_res_partner_tree)")


def test_rejects_hallucinated_xpath_target():
    views_xml = (
        '<odoo><record id="v1" model="ir.ui.view">'
        '<field name="name">x</field>'
        '<field name="inherit_id" ref="base.view_partner_form"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//group[@name=&#39;contact&#39;]" position="inside">'
        '<field name="loyalty_points"/>'
        '</xpath>'
        '</field>'
        '</record></odoo>'
    )
    generated = _make_generated(views_xml=views_xml)
    with patch(
        "specialists.build.specialist.external_id_exists_fast",
        return_value=True,
    ), patch(
        "specialists.build.specialist.get_view_arch_by_xmlid_fast",
        return_value=_REAL_PARTNER_FORM_ARCH,
    ):
        try:
            asyncio.run(_validate_references_resolve_against_real_target(
                generated, db="odoo16_dev", module_name="oma_test",
            ))
            raise AssertionError("expected ValueError for hallucinated xpath target")
        except ValueError as exc:
            assert "contact" in str(exc)
            assert "base.view_partner_form" in str(exc)
    print("PASS: rejects a hallucinated xpath target (//group[@name='contact'])")


def test_accepts_real_reference_and_real_xpath():
    views_xml = (
        '<odoo><record id="v1" model="ir.ui.view">'
        '<field name="name">x</field>'
        '<field name="inherit_id" ref="base.view_partner_form"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//group[@name=&#39;main&#39;]" position="inside">'
        '<field name="loyalty_points"/>'
        '</xpath>'
        '</field>'
        '</record></odoo>'
    )
    generated = _make_generated(views_xml=views_xml)
    with patch(
        "specialists.build.specialist.external_id_exists_fast",
        return_value=True,
    ), patch(
        "specialists.build.specialist.get_view_arch_by_xmlid_fast",
        return_value=_REAL_PARTNER_FORM_ARCH,
    ):
        asyncio.run(_validate_references_resolve_against_real_target(
            generated, db="odoo16_dev", module_name="oma_test",
        ))
    print("PASS: accepts a real external id and a real, resolvable xpath target")


def test_skips_self_module_references():
    views_xml = (
        '<odoo><record id="v1" model="ir.ui.view">'
        '<field name="name">x</field>'
        '<field name="inherit_id" ref="oma_test.some_other_view"/>'
        '</record></odoo>'
    )
    generated = _make_generated(views_xml=views_xml)
    with patch(
        "specialists.build.specialist.external_id_exists_fast",
        side_effect=AssertionError("should never be called for a same-module ref"),
    ):
        asyncio.run(_validate_references_resolve_against_real_target(
            generated, db="odoo16_dev", module_name="oma_test",
        ))
    print("PASS: never checks a ref pointing at this same generated module (not real yet either way)")


def test_skips_complex_xpath_outside_elementtree_subset():
    views_xml = (
        '<odoo><record id="v1" model="ir.ui.view">'
        '<field name="name">x</field>'
        '<field name="inherit_id" ref="base.view_partner_form"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//group[1]/field[position()=2]" position="after">'
        '<field name="loyalty_points"/>'
        '</xpath>'
        '</field>'
        '</record></odoo>'
    )
    generated = _make_generated(views_xml=views_xml)
    with patch(
        "specialists.build.specialist.external_id_exists_fast",
        return_value=True,
    ), patch(
        "specialists.build.specialist.get_view_arch_by_xmlid_fast",
        return_value=_REAL_PARTNER_FORM_ARCH,
    ):
        # Should not raise -- outside the simple //tag[@attr='value'] subset, skipped entirely.
        asyncio.run(_validate_references_resolve_against_real_target(
            generated, db="odoo16_dev", module_name="oma_test",
        ))
    print("PASS: never guesses on an xpath expression outside ElementTree's supported subset")


if __name__ == "__main__":
    test_unescape_xml_attr()
    test_rejects_hallucinated_external_id()
    test_rejects_hallucinated_xpath_target()
    test_accepts_real_reference_and_real_xpath()
    test_skips_self_module_references()
    test_skips_complex_xpath_outside_elementtree_subset()
    print("\nALL TESTS PASSED")
