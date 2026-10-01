"""Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup): the goal
explicitly describes REVOKING an existing access grant via a `post_init_hook` -- none of
`_extract_security_access_claim()`'s own 3 defined claim shapes, and its own prompt already
correctly says to set `applicable=false` for exactly this case. The fast extraction model still
returned `applicable=true` with no group_name filled in, permanently failing
`_verify_security_access_claim()` with "security claim incomplete" no matter how many rounds
were retried -- a goal shape this checker was never built to verify, not a real regression.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from specialists.testing_qa.specialist import (
    SecurityAccessClaim,
    _correct_hook_revocation_goal_misclassified_as_security_claim,
)

HOOK_GOAL = (
    "Build a new Odoo module that depends on the existing module oma_build_a_complete_field_"
    "ab52b7f8. The correct mechanism is a post_init_hook (registered in the new module's own "
    "__manifest__.py under 'post_init_hook') that runs automatically right after install and "
    "resolves the external id 'oma_build_a_complete_field_ab52b7f8.access_oma_service_ticket' "
    "via env.ref(...), unlinking that one specific record."
)


def test_overrides_empty_claim_hallucinated_applicable_for_a_hook_revocation_goal():
    hallucinated = SecurityAccessClaim(applicable=True)
    result = _correct_hook_revocation_goal_misclassified_as_security_claim(hallucinated, HOOK_GOAL)
    assert result.applicable is False, (
        "a goal literally naming post_init_hook, with no group/field/button ever extracted, "
        "must never be treated as an applicable, checkable security claim"
    )
    print("PASS: an empty, hallucinated applicable=true claim for a hook-revocation goal is corrected to False")


def test_never_touches_a_genuinely_non_applicable_claim():
    claim = SecurityAccessClaim(applicable=False)
    result = _correct_hook_revocation_goal_misclassified_as_security_claim(claim, HOOK_GOAL)
    assert result is claim, "an already-correct applicable=false claim must be returned unchanged"
    print("PASS: an already-correct non-applicable claim is left untouched")


def test_never_touches_a_claim_with_a_real_group_name_even_if_goal_mentions_the_hook():
    claim = SecurityAccessClaim(
        applicable=True, group_name="Field Technician", model="oma.service.ticket",
        expects_read=True, expects_write=True,
    )
    result = _correct_hook_revocation_goal_misclassified_as_security_claim(claim, HOOK_GOAL)
    assert result is claim, (
        "a genuinely fully-specified claim (real group_name) must never be overridden just "
        "because the goal also happens to mention post_init_hook"
    )
    print("PASS: a fully-specified claim survives untouched even when the goal also mentions the hook")


def test_never_fires_when_goal_never_mentions_post_init_hook():
    claim = SecurityAccessClaim(applicable=True)
    ordinary_goal = "Restrict field 'salary' to group 'HR Managers' on res.partner."
    result = _correct_hook_revocation_goal_misclassified_as_security_claim(claim, ordinary_goal)
    assert result is claim, (
        "an ordinary security-claim goal with no post_init_hook mention must never be touched, "
        "even if its own claim happens to be empty for some other reason"
    )
    print("PASS: an ordinary goal with no post_init_hook mention is never touched")


if __name__ == "__main__":
    test_overrides_empty_claim_hallucinated_applicable_for_a_hook_revocation_goal()
    test_never_touches_a_genuinely_non_applicable_claim()
    test_never_touches_a_claim_with_a_real_group_name_even_if_goal_mentions_the_hook()
    test_never_fires_when_goal_never_mentions_post_init_hook()
    print("\nALL SECURITY-CLAIM POST_INIT_HOOK MISCLASSIFICATION TESTS PASSED")
