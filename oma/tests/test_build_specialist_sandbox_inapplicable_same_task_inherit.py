"""Phase 20 Area 2 (2026-07-21): unit tests for
specialists/build/specialist.py's
_sandbox_preflight_inapplicable_for_same_task_inherit() -- pure logic
against a monkeypatched list_custom_models, no live SSH/DB needed.

Real bug this fixes: found live during a Operator demo run (task
'warranty.claim'). The sandbox pre-flight is a genuinely fresh,
disposable DB+filesystem every round, but the scaffold module
DIRECTORY is reused across rounds of the same task. When a later
constraint's own `_inherit = 'X'` targets a model an EARLIER
constraint of the SAME task already created for real, writing this
round's content into the sandbox OVERWRITES the earlier constraint's
own `_name = 'X'` definition -- the only place in the sandbox that
ever defined the model -- so a fresh sandbox install crashes
deterministically with `TypeError: Model 'X' does not exist in
registry.`, even though the REAL target (where the earlier
constraint's own install genuinely persists) has no such problem.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_specialist
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _sandbox_preflight_inapplicable_for_same_task_inherit,
)


def _make_generated(models_py: str) -> GeneratedModuleFiles:
    return GeneratedModuleFiles(
        manifest_fields=ManifestFields(
            name="oma_test", version="0.1", category="Uncategorized", summary="", author="",
            depends=["base"], data=[],
        ),
        models_py=models_py,
        security_csv="id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        notes="",
    )


def test_true_when_inherit_target_owned_by_this_same_task(monkeypatch):
    monkeypatch.setattr(
        build_specialist, "list_custom_models",
        lambda db: [("warranty.claim", "oma_create_a_small_new_6da83730")],
    )
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class WarrantyClaim(models.Model):\n"
        "    _inherit = 'warranty.claim'\n"
    )
    result = asyncio.run(_sandbox_preflight_inapplicable_for_same_task_inherit(
        generated, "oma_create_a_small_new_6da83730", "odoo16_dev",
    ))
    assert result is True
    print("PASS: correctly identifies a same-task _inherit target -- sandbox should be skipped")


def test_false_when_no_inherit_target_at_all():
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class WarrantyClaim(models.Model):\n"
        "    _name = 'warranty.claim'\n"
    )
    # list_custom_models is never even called since there's no _inherit at all --
    # no monkeypatch needed; proves the early-exit gate short-circuits first.
    result = asyncio.run(_sandbox_preflight_inapplicable_for_same_task_inherit(
        generated, "oma_create_a_small_new_6da83730", "odoo16_dev",
    ))
    assert result is False
    print("PASS: a plain _name definition (no _inherit) never skips the sandbox")


def test_false_when_inherit_target_owned_by_different_module(monkeypatch):
    monkeypatch.setattr(
        build_specialist, "list_custom_models",
        lambda db: [("warranty.claim", "oma_create_a_small_new_UNRELATED")],
    )
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class WarrantyClaim(models.Model):\n"
        "    _inherit = 'warranty.claim'\n"
    )
    result = asyncio.run(_sandbox_preflight_inapplicable_for_same_task_inherit(
        generated, "oma_create_a_small_new_6da83730", "odoo16_dev",
    ))
    assert result is False
    print("PASS: an _inherit target owned by a DIFFERENT module never skips the sandbox -- a "
          "genuine cross-module issue still gets the sandbox's own real pre-flight check")


def test_false_when_ownership_cannot_be_determined(monkeypatch):
    monkeypatch.setattr(build_specialist, "list_custom_models", lambda db: None)
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class WarrantyClaim(models.Model):\n"
        "    _inherit = 'warranty.claim'\n"
    )
    result = asyncio.run(_sandbox_preflight_inapplicable_for_same_task_inherit(
        generated, "oma_create_a_small_new_6da83730", "odoo16_dev",
    ))
    assert result is False
    print("PASS: genuine uncertainty (list_custom_models returns None) never skips a real safety check")


if __name__ == "__main__":
    import types

    class _MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    mp = _MonkeyPatch()
    test_true_when_inherit_target_owned_by_this_same_task(mp)
    test_false_when_no_inherit_target_at_all()
    test_false_when_inherit_target_owned_by_different_module(mp)
    test_false_when_ownership_cannot_be_determined(mp)
    print("\nALL SANDBOX-INAPPLICABLE-SAME-TASK-INHERIT TESTS PASSED")
