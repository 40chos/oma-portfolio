# SYNTHETIC TEST FIXTURE -- not real Odoo source. Analogous to Odoo's real
# `mail.thread` mixin plus its extension of `res.partner`.
from odoo import api, fields, models


class DemoThread(models.AbstractModel):
    _name = "demo.thread"
    _description = "Fixture Thread Mixin"

    message_ids = fields.One2many("demo.message", "thread_ref")
    message_count = fields.Integer(compute="_compute_message_count")

    @api.depends("message_ids")
    def _compute_message_count(self):
        for rec in self:
            rec.message_count = len(rec.message_ids)


class DemoMessage(models.Model):
    _name = "demo.message"
    _description = "Fixture Message"

    body = fields.Text()
    thread_ref = fields.Many2one("demo.thread")


class DemoPartnerExtension(models.Model):
    """Extension-in-place: no _name, only _inherit -- adds fields to
    fixture_base's demo.partner. This is the EXTENDS test case."""

    _inherit = "demo.partner"

    thread_message_count = fields.Integer(compute="_compute_thread_message_count")

    @api.depends("display_name")
    def _compute_thread_message_count(self):
        for rec in self:
            rec.thread_message_count = 0
