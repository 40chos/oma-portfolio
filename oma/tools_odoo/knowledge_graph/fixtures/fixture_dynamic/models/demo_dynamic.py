# SYNTHETIC TEST FIXTURE -- not real Odoo source. Every construct in this file is
# intentionally something a static parser cannot confidently resolve.
from odoo import api, fields, models

_TARGET_MODEL = "demo.partner"  # deliberately indirect, not a literal in the class body


class DemoDynamicInherit(models.Model):
    # Dynamic _inherit -- built from a module-level variable, not a literal.
    _inherit = _TARGET_MODEL

    dynamic_field = fields.Char()

    def _touch_dynamic_attr(self, attr_name):
        # Dynamic getattr/setattr -- the attribute name is a runtime value, not
        # a string literal, so a static parser cannot know which field it touches.
        return getattr(self, attr_name)

    unresolved_compute_field = fields.Integer(compute="_compute_missing")
    # Note: '_compute_missing' is intentionally never defined in this class.


class DemoDynamicComodel(models.Model):
    _name = "demo.dynamic_comodel"
    _description = "Fixture Dynamic Comodel"

    other_model = _TARGET_MODEL
    # Relational field whose comodel is a variable, not a string literal.
    ref_id = fields.Many2one(other_model)
