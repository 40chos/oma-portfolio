# SYNTHETIC TEST FIXTURE -- not real Odoo source.
from odoo import models


class DemoNote(models.Model):
    _inherit = "demo.note"

    def some_behavior_override(self):
        return True
