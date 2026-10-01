"""Phase 20 Area 2 (2026-07-20, UPDATE 21): unit tests for
specialists/build/specialist.py's
_validate_manifest_declares_core_dependencies() -- pure logic against
a monkeypatched list_custom_models, no live SSH/DB needed.

Real bug this fixes: found live on task #43, immediately after the
own-task model-name-to-inherit fix (UPDATE 21) started correctly
rewriting `_name = 'service.ticket'` to `_inherit = 'service.ticket'`
on a decomposed task's second constraint. This validator's own naive
heuristic (`target_model.split('.')[0]` assumed to be a real Odoo
addon name) then misfired: 'service.ticket'.split('.')[0] == 'service'
is not a real Odoo addon at all (it's a custom oma_*-owned model from
this SAME task's own earlier constraint) -- raising a bogus "add
'service' to depends" error before the sibling
_validate_inherit_target_resolved() (which already has the CORRECT
custom-model-ownership resolution logic) ever got a chance to
correctly exempt this exact self-reference case.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_specialist
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _validate_manifest_declares_core_dependencies,
)


def _make_generated(models_py: str, depends: list[str]) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="oma_test", version="0.1", category="Uncategorized", summary="", author="",
            depends=depends, data=[],
        ),
        models_py=models_py,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        notes="",
    )


def test_does_not_raise_for_inherit_target_that_is_a_known_custom_model(monkeypatch):
    monkeypatch.setattr(
        build_specialist, "list_custom_models",
        lambda db: [("service.ticket", "oma_create_a_small_new_7c619489")],
    )
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _inherit = 'service.ticket'\n"
        "    is_urgent = fields.Boolean()\n",
        depends=["base"],
    )
    # Must not raise -- previously this raised "add ['service'] to depends"
    asyncio.run(_validate_manifest_declares_core_dependencies(generated, [], "odoo16_dev"))
    print("PASS: an _inherit target that's a known custom model is exempted from the naive prefix check")


def test_still_raises_for_a_genuinely_missing_standard_odoo_dependency(monkeypatch):
    monkeypatch.setattr(build_specialist, "list_custom_models", lambda db: [])
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class HrEmployee(models.Model):\n"
        "    _inherit = 'hr.employee'\n"
        "    badge_number = fields.Char()\n",
        depends=["base"],
    )
    raised = False
    try:
        asyncio.run(_validate_manifest_declares_core_dependencies(generated, [], "odoo16_dev"))
    except ValueError as exc:
        raised = True
        assert "hr" in str(exc)
    assert raised
    print("PASS: a genuinely missing standard Odoo module dependency is still caught")


def test_still_exempts_a_relation_to_this_rounds_own_new_model(monkeypatch):
    monkeypatch.setattr(build_specialist, "list_custom_models", lambda db: [])
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'service.ticket'\n"
        "    line_ids = fields.One2many('service.ticket.line', 'ticket_id')\n",
        depends=["base"],
    )
    # 'service.ticket.line' shares the new model's own prefix -- must not raise
    asyncio.run(_validate_manifest_declares_core_dependencies(generated, ["service.ticket"], "odoo16_dev"))
    print("PASS: the original 'relation to this round's own new model' exemption still works")


if __name__ == "__main__":
    import types

    class _MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    mp = _MonkeyPatch()
    test_does_not_raise_for_inherit_target_that_is_a_known_custom_model(mp)
    test_still_raises_for_a_genuinely_missing_standard_odoo_dependency(mp)
    test_still_exempts_a_relation_to_this_rounds_own_new_model(mp)
    print("\nALL CORE-DEPS CUSTOM-MODEL-EXEMPTION TESTS PASSED")
