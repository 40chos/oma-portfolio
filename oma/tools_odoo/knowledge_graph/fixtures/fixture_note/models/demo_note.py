# SYNTHETIC TEST FIXTURE -- not real Odoo source. Analogous to Odoo's real `note`
# module: a new model that inherits a mixin (list _inherit + _name), plus a
# cross-module EXTENDS addition with a cross-model @api.depends() reference.
from odoo import api, fields, models


class DemoNote(models.Model):
    _name = "demo.note"
    _inherit = ["demo.thread"]
    _description = "Fixture Note"

    memo = fields.Text()
    partner_id = fields.Many2one("demo.partner")
    summary = fields.Char(compute="_compute_summary")

    @api.depends("memo")
    def _compute_summary(self):
        for rec in self:
            rec.summary = (rec.memo or "")[:20]


class DemoPartnerNoteExtension(models.Model):
    _inherit = "demo.partner"

    note_count = fields.Integer(compute="_compute_note_count")

    @api.depends("demo.note.partner_id")
    def _compute_note_count(self):
        for rec in self:
            rec.note_count = 0
