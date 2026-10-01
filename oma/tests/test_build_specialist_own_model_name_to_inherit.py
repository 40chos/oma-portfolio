"""Phase 20 Area 2 (2026-07-20, UPDATE 20): unit tests for
specialists/build/specialist.py's
_autofix_rewrite_own_task_model_name_to_inherit() -- pure logic
against a monkeypatched get_model_fields, no live SSH/DB needed.

Real bug this fixes: found live on task #43's SECOND decomposed
constraint (round 1, then identically again round 2, self-flagged
'pattern_worth_a_rule' by the manager's own root-cause classifier):
once constraint 1 (service_ticket_model) genuinely installed the
model for real, constraint 2's own round regenerated
`_name = 'service.ticket'` again instead of
`_inherit = 'service.ticket'`, tripping the "already exists" guard
against its OWN task's already-successful earlier work.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import specialists.build.specialist as build_specialist
from specialists.build.specialist import (
    GeneratedModuleFiles,
    ManifestFields,
    _autofix_rewrite_own_task_model_name_to_inherit,
    _validate_new_model_name_is_valid_odoo_identifier,
)

_REAL_43_CONSTRAINT_2_GOAL = (
    "Create a small new Odoo module that defines a brand new custom model called "
    "service.ticket (a simple support ticket with a name and description field), and "
    "adds a new security group called 'Service Ticket Managers' with full read, write, "
    "create, and delete access to this new model via its own access rule row. "
    "This round's own NEW focus is ONLY: 'ticket_access_rule'."
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


def test_rewrites_name_to_inherit_when_model_is_named_in_the_goal_and_already_real(monkeypatch):
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda name, db: ["name", "description"])
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'service.ticket'\n"
        "    _description = 'Service Ticket'\n"
    )
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(generated, _REAL_43_CONSTRAINT_2_GOAL, "odoo16_dev"))
    assert "_inherit = 'service.ticket'" in generated.models_py
    assert "_name = 'service.ticket'" not in generated.models_py
    print("PASS: _name is rewritten to _inherit when the model is named in the task's own goal and already real")


def test_does_not_rewrite_when_model_not_named_in_goal():
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ResPartner(models.Model):\n"
        "    _name = 'res.partner'\n"
    )
    # get_model_fields is never even called since the name-in-goal gate fails first --
    # no monkeypatch needed; a real call here would fail loudly in a unit test env,
    # which is exactly the point (proves the gate short-circuits before any DB call).
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(generated, _REAL_43_CONSTRAINT_2_GOAL, "odoo16_dev"))
    assert "_name = 'res.partner'" in generated.models_py
    print("PASS: an unrelated model name never named in the goal is never touched (safety gate)")


def test_does_not_rewrite_when_model_is_genuinely_new(monkeypatch):
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda name, db: None)
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'service.ticket'\n"
    )
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(generated, _REAL_43_CONSTRAINT_2_GOAL, "odoo16_dev"))
    assert "_name = 'service.ticket'" in generated.models_py
    print("PASS: a genuinely new (not-yet-real) model name is left as _name, never touched")


def test_no_op_for_empty_goal(monkeypatch):
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda name, db: ["name"])
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'service.ticket'\n"
    )
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(generated, "", "odoo16_dev"))
    assert "_name = 'service.ticket'" in generated.models_py
    print("PASS: an empty/missing goal is a safe no-op")


def test_does_not_rewrite_when_own_module_name_given_and_ownership_matches(monkeypatch):
    """CORRECTED (2026-07-21, later the same day, live during a Operator
    demo, task 'warranty.claim'): this used to assert the OPPOSITE --
    that a same-task self-reference SHOULD be rewritten to _inherit.
    That was wrong: `_inherit` only works when SOME module's currently
    loaded code still declares `_name = 'X'` -- for a genuine same-task
    self-continuation, THIS module's own class is the ONLY place that
    ever declares the model, so converting it to `_inherit` makes the
    model vanish from Odoo's registry entirely. Confirmed live,
    reproduced twice (sandbox AND the real target):
    `TypeError: Model 'warranty.claim' does not exist in registry.`
    The correct behavior is to leave `_name` as-is and let
    _validate_new_model_name_not_already_real()'s own matching
    ownership exemption allow it through instead of raising.
    """
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda name, db: ["name", "description"])
    monkeypatch.setattr(
        build_specialist, "list_custom_models",
        lambda db: [("service.ticket", "oma_create_a_small_new_559f773d")],
    )
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'service.ticket'\n"
    )
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(
        generated, _REAL_43_CONSTRAINT_2_GOAL, "odoo16_dev", "oma_create_a_small_new_559f773d",
    ))
    assert "_name = 'service.ticket'" in generated.models_py
    assert "_inherit = 'service.ticket'" not in generated.models_py
    print("PASS: a same-task self-reference is left as _name, never rewritten to _inherit "
          "(which would make the model vanish from the registry)")


def test_rewrites_when_own_module_name_given_and_ownership_does_not_match(monkeypatch):
    """Real bug found live (2026-07-21, Phase 20 Area 2 pass 18, #43/#44):
    a genuinely FRESH task (round 1 of its own first constraint, no
    outcome=succeeded event existed for it at all -- confirmed via
    direct DB inspection) hit the OLD validator's hard error, because
    leftover, still-installed debris from an UNRELATED, much earlier
    task attempt happened to already own a model with the same name.
    For THIS case (a genuinely different module's real model), the
    rewrite to `_inherit` is correct -- unlike the same-task case above,
    that OTHER module's code really does still declare `_name = 'X'`
    somewhere, so `_inherit` correctly resolves against it.
    """
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda name, db: ["name", "description"])
    monkeypatch.setattr(
        build_specialist, "list_custom_models",
        lambda db: [("service.ticket", "oma_create_a_small_new_bd57bb93")],
    )
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'service.ticket'\n"
    )
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(
        generated, _REAL_43_CONSTRAINT_2_GOAL, "odoo16_dev", "oma_create_a_small_new_ff0532e0",
    ))
    assert "_inherit = 'service.ticket'" in generated.models_py
    assert "_name = 'service.ticket'" not in generated.models_py
    print("PASS: a real model owned by a genuinely DIFFERENT module is still correctly rewritten to _inherit")


def test_rewrites_when_ownership_cannot_be_determined(monkeypatch):
    """Genuine uncertainty (list_custom_models returns None) falls back
    to the original, pre-ownership-check default: rewrite to _inherit.
    This matches the far more common real case (a genuinely different
    module or a standard Odoo core model) rather than assuming the
    rarer same-task case without evidence.
    """
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda name, db: ["name", "description"])
    monkeypatch.setattr(build_specialist, "list_custom_models", lambda db: None)
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'service.ticket'\n"
    )
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(
        generated, _REAL_43_CONSTRAINT_2_GOAL, "odoo16_dev", "oma_create_a_small_new_ff0532e0",
    ))
    assert "_inherit = 'service.ticket'" in generated.models_py
    print("PASS: genuine uncertainty about ownership falls back to the safe, original default (rewrite)")


# --- Real, confirmed gap found live (2026-08-03, task016's own real escalation) ---
# The full dotted model name ('project.meerwerk') never appears verbatim in ordinary, casual
# goal phrasing -- every one of these real tasks refers to "a meerwerk record", never the
# technical dotted name. Widened to also accept the model's own last dot-segment as a match.

_REAL_TASK016_GOAL = (
    "I want to see in the chatter history whenever someone changes the 'state' or 'user_id' "
    "on a meerwerk record — who changed it and what it changed from/to."
)


def test_rewrites_name_to_inherit_when_goal_only_casually_mentions_the_model(monkeypatch):
    monkeypatch.setattr(build_specialist, "get_model_fields", lambda name, db: ["state", "user_id"])
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ProjectMeerwerk(models.Model):\n"
        "    _name = 'project.meerwerk'\n"
    )
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(generated, _REAL_TASK016_GOAL, "odoo16_dev"))
    assert "_inherit = 'project.meerwerk'" in generated.models_py
    assert "_name = 'project.meerwerk'" not in generated.models_py
    print("PASS: task016's real casual 'meerwerk record' phrasing (not the full dotted name) "
          "still passes the goal-mention safety gate")


def test_still_does_not_rewrite_when_last_segment_also_absent_from_goal():
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ResPartner(models.Model):\n"
        "    _name = 'res.partner'\n"
    )
    # Neither 'res.partner' nor 'partner' appears anywhere in this goal -- get_model_fields is
    # never even called, proving the widened gate still correctly excludes a genuinely unrelated
    # model, not just the exact-full-name case the original test already covered.
    asyncio.run(_autofix_rewrite_own_task_model_name_to_inherit(generated, _REAL_43_CONSTRAINT_2_GOAL, "odoo16_dev"))
    assert "_name = 'res.partner'" in generated.models_py
    print("PASS: an unrelated model whose last segment also never appears in the goal is still never touched")


# --- _validate_new_model_name_is_valid_odoo_identifier (real task016 shape) ---
# Real, confirmed bug found live (2026-08-03, task016's own real sandbox install crash): a
# `_name` value that reuses the Python CLASS name (PascalCase) as the model identifier is never
# a real, existing Odoo model -- so the "already real" collision check above never catches it --
# yet it's also never a VALID Odoo model identifier (Odoo requires lowercase, dot-separated).
# Odoo's own real install crashed with "The _name attribute ProjectMeerwerk is not valid."

def test_raises_on_pascalcase_name_reused_from_the_class():
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ProjectMeerwerk(models.Model):\n"
        "    _name = 'ProjectMeerwerk'\n"
    )
    try:
        _validate_new_model_name_is_valid_odoo_identifier(generated)
        assert False, "must raise on a PascalCase _name value"
    except ValueError as exc:
        assert "ProjectMeerwerk" in str(exc)
    print("PASS: task016's real PascalCase _name (class name reused as model identifier) raises")


def test_never_raises_on_a_valid_dotted_name():
    generated = _make_generated(
        "from odoo import models, fields\n\n"
        "class ServiceTicket(models.Model):\n"
        "    _name = 'service.ticket'\n"
    )
    _validate_new_model_name_is_valid_odoo_identifier(generated)
    print("PASS: a genuinely valid dotted model name never raises")


def test_never_touches_inherit_only_name():
    generated = _make_generated(
        "from odoo import models\n\nclass X(models.Model):\n    _inherit = 'ProjectMeerwerk'\n"
    )
    _validate_new_model_name_is_valid_odoo_identifier(generated)
    print("PASS: _inherit is never checked, only _name")


if __name__ == "__main__":
    import types

    class _MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    mp = _MonkeyPatch()
    test_rewrites_name_to_inherit_when_model_is_named_in_the_goal_and_already_real(mp)
    test_does_not_rewrite_when_model_not_named_in_goal()
    test_does_not_rewrite_when_model_is_genuinely_new(mp)
    test_no_op_for_empty_goal(mp)
    test_does_not_rewrite_when_own_module_name_given_and_ownership_matches(mp)
    test_rewrites_when_own_module_name_given_and_ownership_does_not_match(mp)
    test_rewrites_when_ownership_cannot_be_determined(mp)
    print("\nALL OWN-TASK-MODEL-NAME-TO-INHERIT TESTS PASSED")
