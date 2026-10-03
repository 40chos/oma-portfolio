"""Phase 4 tests: the capability-class cascade, against a genuine
labeled test set (per the build plan's own explicit requirement, not
just an exploratory "try some inputs and see" pass) -- each of Operator's
four real tasks, one deliberately ambiguous request that must fall
through to the LLM stage, one harmless-sounding request that should
still trip the sensitive-paths check, and one nonsense/garbage input
that must fail safe rather than error out. Run against the real
gateway (qwen3.6-27b, GPU Worker 02 -- qwen3-14b is retired as of
2026-07-13).

Also proves the explicit completeness-check discipline actually works:
a forced-truncation call must be caught and fall back to the
conservative default, not crash or silently return garbage.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.gateway_client import ModelGatewayClient
from manager.charter import TIER_PAUSE_BEFORE, check_sensitive_paths
from manager.classify import (
    CONSERVATIVE_DEFAULT,
    DATA_CHANGE,
    MODULE_DEV,
    READONLY,
    ClassificationIncompleteError,
    _fast_path,
    _llm_classify,
    classify_capability_class,
)

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")

# The labeled test set, written down in advance per the build plan's
# own requirement.
LABELED_CASES = [
    ("Add a 'preferred_language' field to contacts, visible on the contact form.", MODULE_DEV),
    ("Build a service module linking a new service concept to projects, customers, and employees.", MODULE_DEV),
    ("Build the advanced service module with service grouping, calendar scheduling, and product selection scoped to a customer's project.", MODULE_DEV),
    ("Do a full read-only quality audit of the custom Odoo codebase -- look for hardcoded values, dead code, and inconsistent patterns.", READONLY),
    # Real, confirmed bug found live (2026-07-16, Phase 20 Area 2): this
    # exact phrasing shape ("Create a small new Odoo module that...")
    # fell through the original adjacency-only patterns on every one of
    # ~59 real batch submissions, forcing all of them through the flaky
    # LLM fallback -- which then non-deterministically misclassified
    # roughly 15% of them as "readonly", each surfacing as a nonsensical
    # "I can run that audit..." clarification with no real path forward.
    ("Create a small new Odoo module that adds a new security group called "
     "'Tasks Managers' and grants that group full read, write, create, and "
     "delete access to the project.task model via a new access rule. No "
     "other existing groups should be affected.", MODULE_DEV),
    ("Create a small new Odoo module that restricts visibility of the CRM "
     "top-level application menu so that only members of a new security "
     "group called 'CRM Menu Access' can see and access it in the main "
     "navigation -- other users should not see this menu at all.", MODULE_DEV),
    # Real, distinct second bug found live (2026-07-16, same session as
    # the create/module fix above): the READONLY \bread[- ]?only\b
    # pattern was too broad and matched "read-only access" -- a normal
    # Odoo permission-level description, not an audit request -- still
    # misclassifying every "read-only access" Shape A task even after
    # the first fix landed.
    ("Create a small new Odoo module that adds a new security group called "
     "'Sales Orders Read Only' and grants that group read-only access to "
     "the sale.order model via a new access rule. This model already has "
     "a separate group called 'Sales Orders Managers' from another module "
     "with its own access rule -- that existing group's access must "
     "continue to work unchanged after this module installs.", MODULE_DEV),
    # Real, THIRD bug found live (2026-07-17, Phase 20 Area 2): the
    # 2026-07-16 fix only excluded "read-only access" specifically via a
    # negative lookahead -- but "...should not see this field at all,
    # not even as read-only" (a genuine field-visibility restriction
    # task) ALSO matched standalone "read-only", silently blocking the
    # ENTIRE restrict_field_visibility_to_group batch shape from ever
    # creating a real task at all (confirmed via direct DB query: zero
    # task_created events across the whole submission window, twice in
    # a row). A negative lookahead can only exclude phrasings anticipated
    # in advance; fixed generally by requiring "read-only" to co-occur
    # with a genuine audit/investigation-intent word nearby, rather than
    # trusting "read-only" alone as sufficient signal.
    ("Create a small new Odoo module that restricts visibility of the "
     "existing badge_expiry_date field on the hr.employee model so that "
     "only members of a new security group called 'Badge Expiry Viewers' "
     "can see it -- other users should not see this field at all, not "
     "even as read-only. Use Odoo's field-level access restriction (a "
     "field-level groups attribute or an equivalent view-level "
     "restriction), not a whole-model access rule.", MODULE_DEV),
    # Real, FOURTH bug found live (2026-07-27, Phase 26A's own live
    # verification of an unrelated fix): this project's own established
    # goal-metadata convention -- a literal "Field: <name> (<Type>)"
    # line, used by every real module_dev task this whole project
    # submits -- matched NEITHER the readonly nor the module_dev fast
    # paths at all, forcing it through the same "confirmed
    # non-deterministic... ~15% spurious-readonly" LLM fallback
    # documented above, and it misfired twice in a row live, on an
    # ordinary field-restriction request, surfacing the identical
    # nonsensical "I can run that audit, but I need to know what to
    # point it at" clarification. Fixed by adding a fast-path pattern
    # recognizing this project's own "Field:" convention directly.
    ("On the fieldjob form, I want an internal notes field that only "
     "developers/technical staff can see and edit -- regular users and "
     "managers should never see it at all.\n\n"
     "Module: project_fieldjob\nModel: project.fieldjob\n"
     "Field: developer_notes (Text)\n"
     "Restrict to group: mis_base_extend.group_user_developer_access_fields\n", MODULE_DEV),
    # Real, FIFTH bug found live (2026-08-06, task027 of the SITE 50-task fix-pass): a standing
    # "whenever X changes to Y, automatically do Z" automation-rule request is structurally
    # module_dev (needs a write() override / automated action shipped in code), not data_change
    # (a one-off mutation against a specific existing record) -- but neither fast-path list
    # caught this shape, so it fell to the flaky LLM classifier and was misclassified as
    # data_change, which then hard-blocked the task outright via capability_readiness.py's
    # deliberate data_change gate before a single round could start.
    ("Whenever a fieldjob record's state changes to 'accepted', automatically post a note in "
     "the chatter of the linked project saying 'Extra work MW-XXXX accepted by customer.'", MODULE_DEV),
]

# Real audit-shaped request that also happens to mention "module" --
# must still resolve to READONLY (the fast path checks readonly
# patterns before module_dev ones, specifically to keep this case
# correct) despite the broadened module_dev patterns added above.
AUDIT_MENTIONING_MODULE_CASE = "Do a read-only audit of the new stock module for hardcoded values."

# Deliberately ambiguous -- vague enough that no keyword fast-path
# pattern should confidently fire, forcing the LLM stage.
AMBIGUOUS_CASE = "Can you take a look at how the customer discount thing behaves and let me know if it seems off?"

# Sounds harmless/casual, but concretely means writing to account.move --
# must trip the sensitive-paths tier check even though the phrasing
# itself is casual.
HARMLESS_SOUNDING_SENSITIVE_CASE = "Just double check the invoice totals look right and adjust them if needed."

GARBAGE_CASE = "asdkjqwe 12939 !!! %%% ???"


def test_fast_path_catches_all_four_real_tasks_without_a_model_call():
    for text, expected in LABELED_CASES:
        result = _fast_path(text)
        assert result == expected, f"fast path got {result!r}, expected {expected!r} for: {text!r}"
    print("PASS: all 4 of Operator's real tasks caught by the zero-model-call keyword fast path, "
          "each with the correct expected label")


def test_readonly_audit_mentioning_module_still_wins_over_broadened_module_dev_patterns():
    result = _fast_path(AUDIT_MENTIONING_MODULE_CASE)
    assert result == READONLY, (
        f"expected READONLY (readonly patterns checked first) even though this text also "
        f"matches the broadened module_dev patterns, got {result!r}"
    )
    print("PASS: an audit request that happens to mention 'module' still correctly resolves "
          "to READONLY, not module_dev, despite the broadened patterns")


def test_ambiguous_case_is_not_caught_by_fast_path():
    result = _fast_path(AMBIGUOUS_CASE)
    assert result is None, (
        f"expected the ambiguous case to fall through to the LLM stage, "
        f"but the fast path already matched it as {result!r}"
    )
    print("PASS: the deliberately ambiguous case correctly falls through past the keyword fast path")


def test_edit_existing_installed_module_resolves_to_module_dev_not_data_change():
    """Real, confirmed gap found live (2026-08-11, Phase 34 Batch M execution): this exact
    convention ("Edit the existing, already-installed module X directly") fell through to the
    LLM classifier, which conflated editing a module's own source CSV file with editing a live
    data record, misclassifying it as data_change -- a permanently-gated capability class -- and
    blocking the task outright before any round could even start."""
    goal = (
        "Edit the existing, already-installed module oma_add_a_risk_register_383d30e7 directly: "
        "its own security/ir.model.access.csv currently grants base.group_user full access to "
        "oma.risk.register -- narrow that row to read-only, and add a separate read/write row "
        "for a group named 'Settings'."
    )
    result = _fast_path(goal)
    assert result == MODULE_DEV, f"expected MODULE_DEV, got {result!r}"
    print("PASS: 'edit the existing, already-installed module X directly' resolves to MODULE_DEV")


def test_already_installed_module_phrase_alone_resolves_to_module_dev():
    result = _fast_path("Fix a bug in the already-installed module oma_x's own models.py file.")
    assert result == MODULE_DEV, f"expected MODULE_DEV, got {result!r}"
    print("PASS: 'already-installed module' alone resolves to MODULE_DEV")


def test_scheduled_batch_action_resolves_to_module_dev_not_data_change():
    """Real, confirmed gap found live (2026-08-11, Phase 34 Batch L survey): this exact
    real goal shape (Batch L item 1) fell through to the LLM classifier, landed on data_change,
    and was blocked outright by the permanent capability_readiness.py gate -- the single most
    common cause of Batch L's own historical 0% pass rate."""
    goal = (
        "Every night at midnight, automatically change any project.punchlist item that has "
        "been in 'open' status for more than 60 days to 'escalated'. Don't touch records in "
        "any other status."
    )
    result = _fast_path(goal)
    assert result == MODULE_DEV, f"expected MODULE_DEV, got {result!r}"
    print("PASS: a scheduled batch-action goal resolves to MODULE_DEV, not data_change")


def test_automatically_before_every_hour_phrasing_also_resolves_to_module_dev():
    result = _fast_path(
        "Every hour, automatically check stock.picking records overdue for their scheduled "
        "date and post a chatter note flagging the delay."
    )
    assert result == MODULE_DEV, f"expected MODULE_DEV, got {result!r}"
    print("PASS: 'automatically... every hour' (reversed word order) also resolves to MODULE_DEV")


async def _run_full_cascade_tests():
    client = ModelGatewayClient()
    try:
        # The 4 real tasks, through the FULL cascade (not just the fast path).
        for text, expected in LABELED_CASES:
            result = await classify_capability_class(text, client, CLASSIFIER_MODEL, use_cache=False)
            assert result == expected, f"full cascade got {result!r}, expected {expected!r} for: {text!r}"
        print("PASS: all 4 real tasks correctly classified through the full cascade")

        # Ambiguous case -- must resolve to SOME valid label via the LLM stage.
        result = await classify_capability_class(AMBIGUOUS_CASE, client, CLASSIFIER_MODEL, use_cache=False)
        assert result in (DATA_CHANGE, MODULE_DEV, READONLY)
        print(f"PASS: ambiguous case resolved via LLM stage to a valid label: {result!r}")

        # Garbage input -- must fail safe to the conservative default, not error out.
        result = await classify_capability_class(GARBAGE_CASE, client, CLASSIFIER_MODEL, use_cache=False)
        assert result == CONSERVATIVE_DEFAULT, (
            f"garbage input should fail safe to {CONSERVATIVE_DEFAULT!r}, got {result!r}"
        )
        print(f"PASS: nonsense/garbage input failed safe to the conservative default ({result!r}), "
              f"did not raise")

        # Caching: same input, same result, second call should hit cache
        # (functionally verified by just calling it again and getting the
        # same answer -- cache correctness, not a timing assertion).
        cached_result = await classify_capability_class(GARBAGE_CASE, client, CLASSIFIER_MODEL, use_cache=True)
        cached_result_2 = await classify_capability_class(GARBAGE_CASE, client, CLASSIFIER_MODEL, use_cache=True)
        assert cached_result == cached_result_2
        print("PASS: cache returns a stable result across repeated calls")
    finally:
        await client.aclose()


async def _run_harmless_sounding_sensitive_case_test():
    """This request sounds like a casual double-check, but concretely
    means a write to account.move -- the sensitive-paths tier check
    must fire regardless of how harmless the phrasing sounds. Since
    Phase 4 doesn't build scope-extraction NLP yet (that's a later
    phase's job), the anticipated scope here is what a human/the
    Manager would recognize this request as touching -- annotated
    directly, the same way a real task contract's anticipated_scope
    would eventually be built.
    """
    tier = check_sensitive_paths(models=["account.move"], is_write=True)
    assert tier == TIER_PAUSE_BEFORE, (
        "a harmless-sounding request touching account.move must still trip tier 3"
    )
    print("PASS: harmless-sounding invoice request correctly resolves to tier 3 "
          "via the mechanical sensitive-paths check")

    client = ModelGatewayClient()
    try:
        label = await classify_capability_class(
            HARMLESS_SOUNDING_SENSITIVE_CASE, client, CLASSIFIER_MODEL, use_cache=False
        )
        assert label in (DATA_CHANGE, MODULE_DEV, READONLY)
        print(f"PASS: harmless-sounding sensitive request classified as capability_class={label!r} "
              f"(tier is decided separately and mechanically, per §2.5 -- confirmed above)")
    finally:
        await client.aclose()


async def _run_truncation_safety_net_test():
    """Force a truncated response (max_tokens=1, guaranteed to cut off
    before </think>) and confirm: (a) _llm_classify itself raises
    ClassificationIncompleteError rather than silently returning
    garbage, and (b) the public classify_capability_class() catches
    that and falls back to the conservative default rather than
    crashing or propagating the error to the caller.
    """
    client = ModelGatewayClient()
    try:
        raised = False
        try:
            await _llm_classify(AMBIGUOUS_CASE, client, CLASSIFIER_MODEL, max_tokens=1)
        except ClassificationIncompleteError:
            raised = True
        assert raised, "expected ClassificationIncompleteError from a forced-truncation call"
        print("PASS: a forced-truncation call correctly raises ClassificationIncompleteError")

        # The public function must NOT propagate this -- it should catch
        # it internally and fail safe. We can't force max_tokens=1
        # through the public API (by design), so this proves the
        # try/except in classify_capability_class already covers
        # exactly this exception type via the broader Exception catch.
        import manager.classify as classify_mod
        original = classify_mod._llm_classify

        async def _forced_incomplete(*args, **kwargs):
            raise ClassificationIncompleteError("forced for test")

        classify_mod._llm_classify = _forced_incomplete
        try:
            result = await classify_capability_class(
                "some genuinely ambiguous text that will not hit any fast-path keyword at all",
                client, CLASSIFIER_MODEL, use_cache=False,
            )
            assert result == CONSERVATIVE_DEFAULT
            print("PASS: classify_capability_class() catches ClassificationIncompleteError "
                  "and falls back to the conservative default rather than raising")
        finally:
            classify_mod._llm_classify = original
    finally:
        await client.aclose()


if __name__ == "__main__":
    test_fast_path_catches_all_four_real_tasks_without_a_model_call()
    test_readonly_audit_mentioning_module_still_wins_over_broadened_module_dev_patterns()
    test_ambiguous_case_is_not_caught_by_fast_path()
    asyncio.run(_run_full_cascade_tests())
    asyncio.run(_run_harmless_sounding_sensitive_case_test())
    asyncio.run(_run_truncation_safety_net_test())
    print("\nALL MANAGER CLASSIFY TESTS PASSED")
