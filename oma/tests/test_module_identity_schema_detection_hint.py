"""Phase 30 §26 follow-up (2026-08-04): regression tests for manager.loop._schema_detected_
model_hint() -- the real fix for a confirmed-live gap: resolve_module_identity()'s own prose
fallback only matches a literal `Model: X` line or a dotted identifier appearing verbatim in the
goal text, and 100% of a real 24-task benchmark's plain-English goals ("the meerwerk form", never
"project.meerwerk") failed to resolve module_identity at all, silently disabling Build's own
live-schema-grounding prompt injection (§26 item 1). This closes the gap by converting an
already-detected existing-module target (detect_existing_custom_module_target()'s own real,
live-schema-grounded detection, already computed at turn-start for a different purpose) into the
model-name hint resolve_module_identity() already knows how to prioritize.

No live network/SSH calls -- tools_odoo.odoo_schema_client.list_module_models_fast is patched
with realistic fixture data, matching this repo's own established mocking-at-the-boundary
convention (e.g. tests/test_oma_debris_sweep.py).
"""
from unittest.mock import patch

import manager.loop as loop_module
from contracts.module_identity import resolve_module_identity
from manager.loop import _schema_detected_model_hint


def test_returns_none_when_no_module_was_detected():
    assert _schema_detected_model_hint(None, "odoo16_dev") is None


def test_returns_none_when_target_db_is_empty():
    """Real bug found and fixed live (2026-08-04): the real call site derives target_db from
    OMA_ODOO_DB_DUPLICATE_FOR_BUILD, which can be unset (empty string) -- must degrade to None,
    not attempt a live lookup against an empty db name."""
    assert _schema_detected_model_hint("project_meerwerk", "") is None


def test_resolves_the_single_owned_model():
    with patch.object(loop_module, "list_module_models_fast", return_value=["project.meerwerk"]):
        assert _schema_detected_model_hint("project_meerwerk", "odoo16_dev") == "project.meerwerk"


def test_prefers_the_owned_model_whose_name_exactly_matches_the_module_name():
    """Real gap found and fixed live (2026-08-04, second retest pass): project_meerwerk owns FOUR
    models (project.meerwerk, .batch.invoice, .line, plus its own project.project extension) -- a
    real, common shape (a primary model plus its own line-items/sub-models), not a hypothetical
    edge case. The "exactly one owned model" rule alone left this genuinely resolvable case
    unresolved even with a correct module pick already in hand. Fixed: prefer whichever owned
    model's dotted name, underscored, exactly equals the module's own name -- a safe, structural
    Odoo naming convention (the module is named after its own primary model), not a guess among
    equally-plausible candidates.
    """
    with patch.object(loop_module, "list_module_models_fast", return_value=[
        "project.meerwerk", "project.meerwerk.batch.invoice", "project.meerwerk.line", "project.project",
    ]):
        assert _schema_detected_model_hint("project_meerwerk", "odoo16_dev") == "project.meerwerk"


def test_returns_none_for_a_genuinely_ambiguous_multi_model_module():
    """Deliberately conservative, matching detect_existing_custom_module_target()'s own "a wrong
    guess is worse than no guess" philosophy -- when NEITHER owned model's name matches the
    module's own name, there is no safe signal for which one is "the" target, so this must still
    return None rather than guess."""
    with patch.object(loop_module, "list_module_models_fast", return_value=["foo.bar", "foo.baz"]):
        assert _schema_detected_model_hint("custom_stuff", "odoo16_dev") is None


def test_returns_none_when_the_live_lookup_itself_finds_nothing():
    with patch.object(loop_module, "list_module_models_fast", return_value=None):
        assert _schema_detected_model_hint("project_meerwerk", "odoo16_dev") is None


def test_never_raises_on_a_live_lookup_failure():
    with patch.object(loop_module, "list_module_models_fast", side_effect=RuntimeError("SSH timeout")):
        assert _schema_detected_model_hint("project_meerwerk", "odoo16_dev") is None


def test_plain_english_goal_with_no_dotted_model_name_now_resolves_via_schema_detection():
    """The exact failure shape confirmed live, 2026-08-04: a goal describing the target in plain
    English ("the meerwerk form"), never naming "project.meerwerk" anywhere in its own text --
    resolve_module_identity()'s prose fallback alone returns None for this (confirmed separately,
    asserted below too), but with the schema-detected hint now wired in as resolve_module_identity()'s
    own `hint` parameter, module_identity correctly resolves to the real model.
    """
    goal = (
        "The 'Send to customer' button on the meerwerk form should only be visible to System "
        "Administrators. Normal users and managers should not see it."
    )
    # Confirm the prose fallback genuinely can't resolve this alone -- the real bug this fixes.
    assert resolve_module_identity(goal) is None

    with patch.object(loop_module, "list_module_models_fast", return_value=[
        "project.meerwerk", "project.meerwerk.batch.invoice", "project.meerwerk.line", "project.project",
    ]):
        hint = _schema_detected_model_hint("project_meerwerk", "odoo16_dev")
    assert hint == "project.meerwerk"

    # The real end-to-end call resolve_module_identity() sees in manager/loop.py's own call site.
    resolved = resolve_module_identity(goal, hint=hint)
    assert resolved == "project.meerwerk", (
        "a plain-English goal with no dotted model name must now resolve module_identity via "
        "live-schema detection, not stay None -- this is the actual fix for §26's own found gap"
    )
