# SYNTHETIC TEST FIXTURE -- not real Odoo source. Deliberately contains the
# non-static patterns the parser must flag under needs_llm_review instead of
# guessing: dynamic _inherit, dynamic getattr(), a compute= with no resolvable
# @api.depends(), a non-literal relational comodel, and an ir.rule domain.
{
    "name": "Fixture Dynamic",
    "version": "16.0.1.0.0",
    "depends": ["fixture_base"],
    "data": ["security/ir_rule.xml"],
}
