# SYNTHETIC TEST FIXTURE -- not real Odoo source.
from odoo import fields, models


class DemoThing(models.Model):
    _name = "demo.thing"

    name = fields.Char(required=True)
