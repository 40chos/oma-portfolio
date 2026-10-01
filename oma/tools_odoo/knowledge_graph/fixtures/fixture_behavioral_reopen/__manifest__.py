# SYNTHETIC TEST FIXTURE -- not real Odoo source. Tests a real gap found via
# §11.9 validation (2026-07-21): a class that reopens a model purely for method
# overrides, with zero new fields, must still show up in `reopens`, even though
# it produces nothing in `extends` (real example found: mass_mailing_crm's
# `_inherit = 'crm.lead'` / `_mailing_enabled = True`, no new fields at all).
{
    "name": "Fixture Behavioral Reopen",
    "version": "16.0.1.0.0",
    "depends": ["fixture_note"],
    "data": [],
}
