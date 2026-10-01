"""P11 findings #8 and #11 (docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md
§1.8/§1.11, real tasks a4f8972d-... / oma_add_mail_thread_support_606a2791): tests for
_autofix_ir_rule_groups_many2many_ref_syntax() and _validate_no_ref_call_inside_ir_rule_domain_force().
Both are real, distinct ir.rule-generation bugs confirmed via live sandbox crashes. Pure,
synchronous, zero LLM/GPU calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_ir_rule_groups_many2many_ref_syntax,
    _validate_no_ref_call_inside_ir_rule_domain_force,
)


def _manifest():
    return ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )


def test_autofix_rewrites_many2one_ref_syntax_to_many2many_eval():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            '<field name="name">X</field>'
            '<field name="model_id" ref="model_res_partner"/>'
            '<field name="groups" ref="base.group_user"/>'
            '<field name="domain_force">[(1,\'=\',1)]</field>'
            '</record></odoo>'
        ),
    )
    _autofix_ir_rule_groups_many2many_ref_syntax(generated)
    assert '<field name="groups" ref=' not in generated.security_xml
    assert '<field name="groups_id" eval="[(4, ref(\'base.group_user\'))]"' in generated.security_xml
    print("PASS: the real, confirmed Many2one ref= syntax on ir.rule's own groups_id field is "
          "rewritten to the correct field name AND Many2many eval= form")


def test_autofix_rewrites_wrong_field_name_alone_when_syntax_was_already_correct():
    """Real, confirmed correction found live (2026-08-17, 2 separate real task occurrences,
    Code-Review's own live message: "Field 'groups' is invalid for ir.rule; must use
    'groups_id'"): the ORIGINAL P11 finding #8 fix (2026-07-31) got the syntax half right
    (ref= -> eval=[(4, ref(...))]) but kept the wrong field NAME (`groups` instead of the real
    `groups_id`) -- this covers the case where Build already used the correct eval= syntax but
    the wrong field name, which the original fix silently left untouched."""
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            '<field name="groups" eval="[(4, ref(\'base.group_user\'))]"/>'
            '</record></odoo>'
        ),
    )
    _autofix_ir_rule_groups_many2many_ref_syntax(generated)
    assert '<field name="groups" eval=' not in generated.security_xml
    assert '<field name="groups_id" eval="[(4, ref(\'base.group_user\'))]"' in generated.security_xml
    print("PASS: the wrong field name alone (correct eval= syntax) is corrected to groups_id")


def test_autofix_never_touches_groups_outside_an_ir_rule_record():
    # A different model's own `groups` field using ref= (e.g. a genuinely different, real
    # Many2one-shaped field elsewhere) must never be touched -- scoped strictly to ir.rule blocks.
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="other" model="some.other.model">'
            '<field name="groups" ref="base.group_user"/>'
            '</record></odoo>'
        ),
    )
    original = generated.security_xml
    _autofix_ir_rule_groups_many2many_ref_syntax(generated)
    assert generated.security_xml == original
    print("PASS: a groups field outside a real ir.rule record is never touched")


def test_autofix_is_a_no_op_when_already_correct():
    correct = (
        '<odoo><record id="rule_x" model="ir.rule">'
        '<field name="groups_id" eval="[(4, ref(\'base.group_user\'))]"/>'
        '</record></odoo>'
    )
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=correct,
    )
    _autofix_ir_rule_groups_many2many_ref_syntax(generated)
    assert generated.security_xml == correct
    print("PASS: already-correct eval= syntax is left untouched")


def test_rejects_bare_ref_call_inside_domain_force():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            '<field name="model_id" ref="model_project_task"/>'
            "<field name=\"domain_force\">[('create_uid','in',[(ref('base.group_user')).users.ids])]</field>"
            '</record></odoo>'
        ),
    )
    raised = False
    try:
        _validate_no_ref_call_inside_ir_rule_domain_force(generated)
    except ValueError as e:
        raised = True
        assert "ref" in str(e)
    assert raised, "a bare ref(...) call inside domain_force must be rejected"
    print("PASS: the real, confirmed bare ref(...) call inside domain_force is rejected")


def test_ordinary_domain_force_without_ref_is_never_flagged():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="rule_x" model="ir.rule">'
            '<field name="model_id" ref="model_project_task"/>'
            '<field name="domain_force">[(\'create_uid\',\'=\',user.id)]</field>'
            '</record></odoo>'
        ),
    )
    _validate_no_ref_call_inside_ir_rule_domain_force(generated)  # must not raise
    print("PASS: an ordinary domain_force with no ref() call is never flagged")


if __name__ == "__main__":
    test_autofix_rewrites_many2one_ref_syntax_to_many2many_eval()
    test_autofix_rewrites_wrong_field_name_alone_when_syntax_was_already_correct()
    test_autofix_never_touches_groups_outside_an_ir_rule_record()
    test_autofix_is_a_no_op_when_already_correct()
    test_rejects_bare_ref_call_inside_domain_force()
    test_ordinary_domain_force_without_ref_is_never_flagged()
    print("\nALL IR_RULE GROUPS/DOMAIN-REF TESTS PASSED")
