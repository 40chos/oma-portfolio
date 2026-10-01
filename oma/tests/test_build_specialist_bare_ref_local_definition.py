"""Phase 20 Area 2 (2026-07-20, UPDATE 24): unit tests for
specialists/build/specialist.py's
_autofix_xml_bare_ref_qualifies_locally_defined_record() -- pure
string logic, no SSH/DB needed.

Real bug this fixes: found live on #43, pass 9/10, a 3-round identical
non-progress loop. Build generated `<record id="group_ticket_managers"
model="res.groups">...</record>` AND, elsewhere in the same
security_xml, `ref="group_ticket_managers"` (bare, no module prefix at
all) referencing that same record -- a completely legitimate
self-reference that fell through every existing check: the sibling
model_XXX autofix only matches model ids, the self-referenced-xmlid
validator only inspects refs that ALREADY carry the module prefix, and
the external-ref validator then (correctly, given how little it knows)
rejected the bare ref as a nonexistent external id.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_xml_bare_ref_qualifies_locally_defined_record,
)


def _make_generated(security_xml: str = "", views_xml: str | None = None) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="oma_test", version="0.1", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py="from odoo import models\n",
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        security_xml=security_xml,
        views_xml=views_xml,
        notes="",
    )


def test_qualifies_a_bare_ref_to_a_locally_defined_group():
    generated = _make_generated(
        security_xml=(
            '<odoo><record id="group_ticket_managers" model="res.groups">'
            '<field name="name">Ticket Managers</field></record>'
            '<record id="rule_ticket" model="ir.rule">'
            '<field name="groups" eval="[(4, ref(\'group_ticket_managers\'))]"/>'
            "</record></odoo>"
        ),
    )
    _autofix_xml_bare_ref_qualifies_locally_defined_record(generated, "oma_create_a_small_new_eff0ca1c")
    assert "ref('oma_create_a_small_new_eff0ca1c.group_ticket_managers')" in generated.security_xml
    assert "ref('group_ticket_managers')" not in generated.security_xml
    print("PASS: a bare ref() call to a locally-defined record is qualified with the own module prefix")


def test_qualifies_a_bare_attribute_style_ref_too():
    generated = _make_generated(
        security_xml=(
            '<odoo><record id="group_x" model="res.groups"><field name="name">X</field></record>'
            '<record id="view_x" model="ir.ui.view"><field name="groups_id" ref="group_x"/></record></odoo>'
        ),
    )
    _autofix_xml_bare_ref_qualifies_locally_defined_record(generated, "oma_test_mod")
    assert 'ref="oma_test_mod.group_x"' in generated.security_xml
    print("PASS: a bare ref=\"...\" attribute to a locally-defined record is also qualified")


def test_does_not_touch_a_ref_that_is_not_locally_defined():
    generated = _make_generated(
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            "<field name=\"groups\" eval=\"[(4, ref('base.group_user'))]\"/>"
            "</record></odoo>"
        ),
    )
    original = generated.security_xml
    _autofix_xml_bare_ref_qualifies_locally_defined_record(generated, "oma_test_mod")
    assert generated.security_xml == original
    print("PASS: an already-qualified, genuinely external ref is left untouched")


def test_no_op_when_nothing_locally_defined_at_all():
    generated = _make_generated(security_xml="")
    _autofix_xml_bare_ref_qualifies_locally_defined_record(generated, "oma_test_mod")
    assert generated.security_xml == ""
    print("PASS: empty security_xml is a safe no-op")


if __name__ == "__main__":
    test_qualifies_a_bare_ref_to_a_locally_defined_group()
    test_qualifies_a_bare_attribute_style_ref_too()
    test_does_not_touch_a_ref_that_is_not_locally_defined()
    test_no_op_when_nothing_locally_defined_at_all()
    print("\nALL BARE-REF-LOCAL-DEFINITION TESTS PASSED")
