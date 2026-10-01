"""Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
flagship run, service_ticket_model node): the same "shared file gets wholesale-replaced instead
of additively edited" bug class as the sibling data-XML fix
(tests/test_restore_dropped_records_in_shared_data_xml.py), but for models.py's own top-level
model classes -- a round meant to ONLY add a new ServiceTicket class regenerated models.py
containing ONLY that class, completely deleting the earlier, already-installed Equipment class.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_restore_dropped_model_classes_in_shared_models_py,
)

# Exact real content from the live bug's own earlier, already-committed round.
_OLD_MODELS_PY = (
    "from odoo import models, fields, api\n\n"
    "class Equipment(models.Model):\n"
    "    _name = 'oma.equipment'\n"
    "    _description = 'Equipment Registry'\n\n"
    "    name = fields.Char(required=True, string='Name')\n"
    "    tracking_number = fields.Char(string='Tracking Number', required=True, copy=False, index=True)\n\n"
    "    @api.model\n"
    "    def create(self, vals):\n"
    "        if 'tracking_number' not in vals:\n"
    "            vals['tracking_number'] = self.env['ir.sequence'].next_by_code('oma.equipment')\n"
    "        return super().create(vals)\n"
)
# Exact real content from the live bug's own broken new round -- Equipment class is gone.
_NEW_MODELS_PY_MISSING_EQUIPMENT = (
    "from odoo import models, fields, api\n\n"
    "class ServiceTicket(models.Model):\n"
    "    _name = 'oma.service.ticket'\n"
    "    _description = 'Service Ticket'\n\n"
    "    name = fields.Char(string='Ticket Number', required=True, copy=False, index=True)\n"
)


def _make_generated(models_py: str) -> GeneratedModuleFiles:
    manifest = ManifestFields(
        name="x", version="1.0.0", category="Tools", summary="x", author="x",
        depends=["base"], data=[],
    )
    return GeneratedModuleFiles(
        manifest_fields=manifest, models_py=models_py, security_csv="x",
        security_xml=None, views_xml=None, notes="",
    )


def test_restores_the_real_dropped_class_closing_the_live_gap():
    generated = _make_generated(_NEW_MODELS_PY_MISSING_EQUIPMENT)
    old_files_by_relpath = {"models/models.py": _OLD_MODELS_PY}

    _autofix_restore_dropped_model_classes_in_shared_models_py(generated, old_files_by_relpath)

    assert "class Equipment(models.Model):" in generated.models_py, (
        f"expected the earlier, already-committed Equipment class to be restored -- got: "
        f"{generated.models_py!r}"
    )
    assert "_name = 'oma.equipment'" in generated.models_py
    assert "class ServiceTicket(models.Model):" in generated.models_py, (
        "the round's own new ServiceTicket class must still be present, unchanged"
    )
    print("PASS: the real, confirmed dropped Equipment class is restored alongside the round's "
          "own new class, closing the real live gap found on task 07141af5's "
          "service_ticket_model node")


def test_never_touches_a_round_that_wrote_no_models_py_at_all():
    generated = _make_generated("")
    old_files_by_relpath = {"models/models.py": _OLD_MODELS_PY}

    _autofix_restore_dropped_model_classes_in_shared_models_py(generated, old_files_by_relpath)

    assert generated.models_py == "", (
        "a round that never touched models_py at all is a different, unrelated shape -- must "
        "remain untouched"
    )
    print("PASS: never touches a round whose own models_py is genuinely empty")


def test_is_a_noop_when_nothing_is_actually_missing():
    combined = _OLD_MODELS_PY + "\n\n\n" + _NEW_MODELS_PY_MISSING_EQUIPMENT.split("\n\n", 1)[1]
    generated = _make_generated(combined)
    old_files_by_relpath = {"models/models.py": _OLD_MODELS_PY}
    original = generated.models_py

    _autofix_restore_dropped_model_classes_in_shared_models_py(generated, old_files_by_relpath)

    assert generated.models_py == original, (
        "must never duplicate or otherwise modify content when the old class is already "
        "genuinely present in the new content"
    )
    print("PASS: a no-op when the round's own new content already includes every old model class")


def test_is_a_noop_when_old_files_by_relpath_has_no_prior_models_py():
    generated = _make_generated(_NEW_MODELS_PY_MISSING_EQUIPMENT)
    original = generated.models_py

    _autofix_restore_dropped_model_classes_in_shared_models_py(generated, old_files_by_relpath=None)
    assert generated.models_py == original

    _autofix_restore_dropped_model_classes_in_shared_models_py(generated, old_files_by_relpath={})
    assert generated.models_py == original
    print("PASS: a no-op when there is genuinely no prior committed models.py to compare "
          "against (e.g. this is genuinely the first model this task ever created)")


def test_never_restores_a_pure_inherit_extension_class():
    """A class with no `_name` of its own (a pure `_inherit` extension) is deliberately left
    out -- whether it's still needed each round is a real judgment call this function has no
    safe way to make.
    """
    old_with_extension = _OLD_MODELS_PY + (
        "\n\nclass ResPartnerExtension(models.Model):\n"
        "    _inherit = 'res.partner'\n\n"
        "    custom_field = fields.Char()\n"
    )
    generated = _make_generated(_NEW_MODELS_PY_MISSING_EQUIPMENT)
    old_files_by_relpath = {"models/models.py": old_with_extension}

    _autofix_restore_dropped_model_classes_in_shared_models_py(generated, old_files_by_relpath)

    assert "class Equipment(models.Model):" in generated.models_py
    assert "ResPartnerExtension" not in generated.models_py, (
        "a pure _inherit-only extension class must never be auto-restored"
    )
    print("PASS: a pure _inherit-only extension class (no _name of its own) is never "
          "auto-restored, only genuine _name-declared models")


if __name__ == "__main__":
    test_restores_the_real_dropped_class_closing_the_live_gap()
    test_never_touches_a_round_that_wrote_no_models_py_at_all()
    test_is_a_noop_when_nothing_is_actually_missing()
    test_is_a_noop_when_old_files_by_relpath_has_no_prior_models_py()
    test_never_restores_a_pure_inherit_extension_class()
    print("\nALL RESTORE-DROPPED-MODEL-CLASSES-IN-SHARED-MODELS-PY TESTS PASSED")
