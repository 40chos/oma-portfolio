# SYNTHETIC TEST FIXTURE -- not real Odoo source. Depends on fixture_mail,
# analogous to Odoo's real `note` module (simple consumer of the mail mixin).
{
    "name": "Fixture Note",
    "version": "16.0.1.0.0",
    "depends": ["fixture_mail"],
    "data": ["views/demo_note_views.xml", "security/ir.model.access.csv"],
}
