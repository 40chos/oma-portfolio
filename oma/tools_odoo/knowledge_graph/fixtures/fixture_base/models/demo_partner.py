# SYNTHETIC TEST FIXTURE -- not real Odoo source. Analogous to Odoo's real
# `res.partner` model in `base`, deliberately simplified.
from odoo import api, fields, models


class DemoPartner(models.Model):
    _name = "demo.partner"
    _description = "Fixture Partner"

    name = fields.Char(required=True)
    email = fields.Char()
    company_id = fields.Many2one("demo.company", string="Company")
    active = fields.Boolean(default=True)
    display_name = fields.Char(compute="_compute_display_name", store=True)

    @api.depends("name", "company_id")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.name} ({rec.company_id.name})"


class DemoCompany(models.Model):
    _name = "demo.company"
    _description = "Fixture Company"

    name = fields.Char(required=True)
    partner_ids = fields.One2many("demo.partner", "company_id")
