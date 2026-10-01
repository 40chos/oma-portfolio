# SYNTHETIC TEST FIXTURE -- not real Odoo source. Tests a real gap found via
# §11.9 validation (2026-07-21): a module using a non-standard singular `model/`
# directory (instead of Odoo's real convention `models/`) was previously
# invisible to Stage A entirely -- the whole module, not just one field
# (real example found: mis_gpeople/model/res_config_settings.py).
{
    "name": "Fixture Singular Model Dir",
    "version": "16.0.1.0.0",
    "depends": [],
    "data": [],
}
