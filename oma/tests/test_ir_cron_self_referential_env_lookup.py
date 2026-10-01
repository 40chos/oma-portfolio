"""P11 finding #13 (docs/planning/PHASE30_P11_ADDITIONS_FROM_P7_TIER3_2026-07-31.md §1.13, real
task oma_vehicle_inspection_tracker): tests for
_validate_ir_cron_no_self_referential_env_lookup() -- Odoo's own ir.cron execution context already
binds `model` to the target recordset; re-looking it up via `model.env['same.model']` is always a
real generation mistake, but `model.env['different.model']` is legitimate and must never be
flagged. Pure, synchronous, zero LLM/GPU calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_ir_cron_no_self_referential_env_lookup,
)


def _manifest():
    return ManifestFields(
        name="x", version="1.0", category="Uncategorized", summary="", author="", depends=["base"], data=[],
    )


def test_rejects_the_real_confirmed_self_referential_lookup():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(),
        models_py=(
            "from odoo import models, fields\n\n"
            "class VehicleInspection(models.Model):\n"
            "    _name = 'vehicle.inspection'\n"
            "    vehicle_name = fields.Char()\n"
        ),
        views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="cron_x" model="ir.cron">'
            '<field name="model_id" ref="model_vehicle_inspection"/>'
            "<field name=\"code\">model.env['vehicle.inspection'].archive_old_inspections()</field>"
            '</record></odoo>'
        ),
    )
    raised = False
    try:
        _validate_ir_cron_no_self_referential_env_lookup(generated)
    except ValueError as e:
        raised = True
        assert "vehicle.inspection" in str(e)
    assert raised, "the real, confirmed self-referential model.env[...] lookup must be rejected"
    print("PASS: the real, confirmed self-referential ir.cron env lookup is rejected")


def test_genuinely_different_model_lookup_is_never_flagged():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(),
        models_py=(
            "from odoo import models, fields\n\n"
            "class VehicleInspection(models.Model):\n"
            "    _name = 'vehicle.inspection'\n"
        ),
        views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="cron_x" model="ir.cron">'
            '<field name="model_id" ref="model_vehicle_inspection"/>'
            "<field name=\"code\">model.env['mail.activity'].create({'summary': 'x'})</field>"
            '</record></odoo>'
        ),
    )
    _validate_ir_cron_no_self_referential_env_lookup(generated)  # must not raise
    print("PASS: a genuinely different model lookup (mail.activity) is legitimate and never flagged")


def test_direct_model_method_call_is_never_flagged():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(),
        models_py="from odoo import models\n\nclass X(models.Model):\n    _name = 'vehicle.inspection'\n",
        views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="cron_x" model="ir.cron">'
            "<field name=\"code\">model.archive_old_inspections()</field>"
            '</record></odoo>'
        ),
    )
    _validate_ir_cron_no_self_referential_env_lookup(generated)  # must not raise
    print("PASS: the real, correct model.method() form is never flagged")


def test_no_models_declared_yet_is_a_safe_no_op():
    generated = GeneratedModuleFiles(
        manifest_fields=_manifest(), models_py="", views_xml="", security_csv="", notes="",
        security_xml=(
            '<odoo><record id="cron_x" model="ir.cron">'
            "<field name=\"code\">model.env['vehicle.inspection'].archive_old_inspections()</field>"
            '</record></odoo>'
        ),
    )
    _validate_ir_cron_no_self_referential_env_lookup(generated)  # must not raise -- nothing to compare against
    print("PASS: with no models declared yet, the check safely no-ops rather than guessing")


if __name__ == "__main__":
    test_rejects_the_real_confirmed_self_referential_lookup()
    test_genuinely_different_model_lookup_is_never_flagged()
    test_direct_model_method_call_is_never_flagged()
    test_no_models_declared_yet_is_a_safe_no_op()
    print("\nALL IR_CRON SELF-REFERENTIAL ENV LOOKUP TESTS PASSED")
