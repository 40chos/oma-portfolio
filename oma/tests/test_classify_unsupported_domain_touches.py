"""Phase 30, P0 (Phase K, §14): unit tests for
classify_unsupported_domain_touches() -- the semantic (not
keyword/regex) classifier that detects whether a task goal touches a
domain with zero real generation support (contracts/
unsupported_domains.json), so manager/loop.py can gate that piece
instead of silently best-effort attempting it.
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.replanning import (
    _UnsupportedDomainTouch,
    _UnsupportedDomainTouches,
    _load_unsupported_domains,
    classify_unsupported_domain_touches,
)


def test_unsupported_domains_json_is_real_and_loadable():
    domains = _load_unsupported_domains()
    names = {d["domain"] for d in domains}
    assert "qweb_report" in names
    assert "translation" in names
    assert "automation_trigger" in names
    # wizard_transient_model deliberately removed from this list Phase 34 (2026-08-11/12,
    # contracts/unsupported_domains.json's own _comment) -- general infra + two dedicated
    # validators (specialists/build/wizard_structural_gate.py) now handle it correctly.
    assert "wizard_transient_model" not in names
    assert "templated_email" in names
    for d in domains:
        assert d["description"].strip(), f"domain {d['domain']!r} must have a real, non-empty description"
    print(f"PASS: contracts/unsupported_domains.json loads {len(domains)} real domains")


def test_returns_empty_list_for_an_ordinary_supported_goal():
    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(return_value=_UnsupportedDomainTouches(touches=[])),
        ):
            return await classify_unsupported_domain_touches("Add a text field to crm.lead", client=None, model="x")

    touches = asyncio.run(run())
    assert touches == []
    print("PASS: an ordinary, fully-supported goal returns no touches")


def test_returns_the_real_touch_for_a_qweb_goal():
    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(return_value=_UnsupportedDomainTouches(touches=[
                _UnsupportedDomainTouch(domain="qweb_report", goal_snippet="generate a PDF invoice report"),
            ])),
        ):
            return await classify_unsupported_domain_touches(
                "Add a field AND generate a PDF invoice report", client=None, model="x",
            )

    touches = asyncio.run(run())
    assert touches == [{"domain": "qweb_report", "goal_snippet": "generate a PDF invoice report"}]
    print("PASS: a real QWeb-report touch is correctly returned with its goal snippet")


def test_filters_out_a_hallucinated_domain_name_not_in_the_real_list():
    """A model that invents a domain name not in unsupported_domains.json
    must never leak through -- this function is the one place deciding
    what's real, not a passthrough of whatever the model says.
    """
    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(return_value=_UnsupportedDomainTouches(touches=[
                _UnsupportedDomainTouch(domain="not_a_real_domain", goal_snippet="something"),
            ])),
        ):
            return await classify_unsupported_domain_touches("some goal", client=None, model="x")

    touches = asyncio.run(run())
    assert touches == []
    print("PASS: a hallucinated domain name not in the real list is filtered out")


def test_filters_out_a_touch_with_an_empty_snippet():
    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(return_value=_UnsupportedDomainTouches(touches=[
                _UnsupportedDomainTouch(domain="qweb_report", goal_snippet="   "),
            ])),
        ):
            return await classify_unsupported_domain_touches("some goal", client=None, model="x")

    touches = asyncio.run(run())
    assert touches == []
    print("PASS: a touch with a blank/empty goal_snippet is filtered out")


def test_fails_open_never_blocks_on_a_classification_error():
    async def run():
        with patch(
            "manager.replanning.call_structured",
            new=AsyncMock(side_effect=RuntimeError("gateway hiccup")),
        ):
            return await classify_unsupported_domain_touches("some goal", client=None, model="x")

    touches = asyncio.run(run())
    assert touches == [], "a classification failure must degrade to 'assume supported', never block a task"
    print("PASS: a real classification failure fails open (assumes supported) rather than blocking")


def test_prompt_carves_out_ordinary_cron_from_automation_trigger():
    """Real, confirmed gap found live (2026-08-05) during the 50-task benchmark's own out-of-
    scope review: the prompt already had an explicit wizard_transient_model carve-out (a stock
    mail.compose.message wizard does NOT count) but no equivalent carve-out for automation_trigger,
    even though its own domain description ('base.automation rules, webhook endpoints, or custom
    controller-triggered automation code') never covers an ordinary ir.cron scheduled action --
    Odoo's own built-in, fully-supported periodic-task mechanism. Without a counter-example, a
    goal like task018's ("Every night at midnight, automatically change...") is exactly the shape
    a semantic classifier could plausibly over-flag, since it says "automatically" and describes a
    schedule. This locks in the fix: the prompt must explicitly name the cron/scheduled-action
    exception, the same way it already does for stock wizards.
    """
    captured = {}

    async def fake_call_structured(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return _UnsupportedDomainTouches(touches=[])

    async def run():
        with patch("manager.replanning.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            return await classify_unsupported_domain_touches("some goal", client=None, model="x")

    asyncio.run(run())
    prompt = captured["prompt"]
    assert "ir.cron" in prompt or "scheduled action" in prompt
    assert "does NOT touch automation_trigger" in prompt or "does NOT touch\nautomation_trigger" in prompt
    assert "message_post" in prompt and "templated_email" in prompt, (
        "the prompt must also carve out message_post-satisfiable emails from templated_email -- "
        "see test_live_message_post_email_is_not_flagged_as_templated_email for the real, live-"
        "confirmed behavioral fix this prompt text alone does not prove"
    )
    assert "per-language" in prompt and "translation" in prompt, (
        "the prompt must also carve out a plain per-language dict from the translation domain -- "
        "see test_live_message_post_email_is_not_flagged_as_templated_email for the real, live-"
        "confirmed behavioral fix this prompt text alone does not prove"
    )
    print("PASS: the prompt explicitly carves out ordinary cron/scheduled actions from automation_trigger")


def test_prompt_asks_for_semantic_not_literal_keyword_matching():
    """Locks in the explicit §14 step 1 correction: literal keyword
    matching is not enough (real ordinary phrasing like 'migrate the
    data' or 'log who changed what' maps to a gated domain without
    naming it) -- the prompt sent to the model must say so.
    """
    captured = {}

    async def fake_call_structured(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return _UnsupportedDomainTouches(touches=[])

    async def run():
        with patch("manager.replanning.call_structured", new=AsyncMock(side_effect=fake_call_structured)):
            return await classify_unsupported_domain_touches("some goal", client=None, model="x")

    asyncio.run(run())
    prompt = captured["prompt"]
    assert "real MEANING" in prompt or "not literal keyword" in prompt
    print("PASS: the prompt explicitly instructs semantic, not literal-keyword, classification")


def test_live_sub_contract_scoping_does_not_leak_a_later_rounds_domain():
    """Real, confirmed live bug found (2026-07-30) during P0's own end-
    to-end verification via the real chat API: submitting the goal
    "Add a discount_reason field to sale.order, and generate a PDF
    invoice report (QWeb) that shows it" correctly decomposed into two
    sub-contracts ('discount_reason_field', 'pdf_invoice_report'), but
    the FIRST (genuinely buildable) sub-contract's own narrowed
    goal_text -- which still embeds the full original goal verbatim for
    context, plus an explicit "NOT yet in scope for this round:
    ['pdf_invoice_report']" list, exactly as
    manager/loop.py's _run_constraint_labels_from() really builds it --
    got incorrectly flagged as touching qweb_report anyway, pausing the
    genuinely buildable first piece instead of letting it proceed.
    Fixed by explicitly instructing the classifier to judge only the
    stated current-round focus, never content marked not-yet-in-scope
    or belonging to the full multi-round original goal. Requires a real
    live gateway call (no mock could have caught the original bug,
    which was a real model reasoning failure, not a code bug) -- run
    with the project's .env sourced and the gateway reachable.
    """
    import asyncio as _asyncio

    from infra.gateway_client import ModelGatewayClient

    field_goal_text = (
        "Build a new custom Odoo module. Add a discount_reason (Char) field to sale.order, and "
        "generate a PDF invoice report (QWeb) that shows the discount reason on it.\n\n"
        "This round's own NEW focus is ONLY: 'discount_reason_field'. Add ONLY the code this one "
        "constraint strictly requires. The following constraints are NOT yet in scope for this "
        "round and must NOT be implemented even partially -- no fields, methods, or view elements "
        "for any of them yet, no matter how related they seem: ['pdf_invoice_report']. Each one "
        "will get its own dedicated round later."
    )
    report_goal_text = (
        "Build a new custom Odoo module. Add a discount_reason (Char) field to sale.order, and "
        "generate a PDF invoice report (QWeb) that shows the discount reason on it.\n\n"
        "This round's own NEW focus is ONLY: 'pdf_invoice_report'. Add ONLY the code this one "
        "constraint strictly requires. The following constraints are ALREADY satisfied by earlier "
        "work on this same task and MUST continue to hold: ['discount_reason_field']."
    )

    async def run():
        client = ModelGatewayClient()
        model = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")
        field_touches = await classify_unsupported_domain_touches(
            field_goal_text, client, model, task_id="test-p0-subcontract-scoping-field",
        )
        report_touches = await classify_unsupported_domain_touches(
            report_goal_text, client, model, task_id="test-p0-subcontract-scoping-report",
        )
        return field_touches, report_touches

    field_touches, report_touches = asyncio.run(run())
    assert field_touches == [], (
        f"the genuinely buildable field sub-contract must NOT be flagged just because the QWeb "
        f"piece is mentioned in the original goal text or the not-yet-in-scope list: got {field_touches}"
    )
    assert any(t["domain"] == "qweb_report" for t in report_touches), (
        f"the sub-contract whose OWN focus really is the QWeb report must still be correctly "
        f"flagged: got {report_touches}"
    )
    print("PASS: per-sub-contract classification correctly scopes to the current round's own focus, "
          "live, against the real gateway -- the buildable piece is never gated by a later round's "
          "unsupported-domain content")


def test_live_message_post_email_is_not_flagged_as_templated_email():
    """Real, confirmed live bug found (2026-08-05) during the 50-task benchmark's own out-of-
    scope review, task020: the prompt already had explicit wizard_transient_model/automation_
    trigger carve-outs, but templated_email had none -- and unlike the cron case, adding a first,
    plainer carve-out sentence did NOT change the real model's live classification (confirmed via
    3 repeated live calls, all still wrongly flagging task020's own real goal). The domain's own
    description in contracts/unsupported_domains.json already says "distinct from mail.thread-
    style chatter/messaging mixins which ARE supported" -- that alone was not enough for the live
    classifier to act on. The surface wording is genuinely deceptive: almost every templated_email
    goal AND almost every message_post goal both use the word "email"/"send", so a carve-out has
    to say that explicitly (word choice alone proves nothing) rather than just assert the outcome.
    The stronger, second rewrite (naming the ONE real signal -- an explicit ask for a reusable/
    editable/bulk template -- and explicitly disclaiming the word "email" as evidence) substantially
    fixed live model behavior: measured 10/11 clean across repeated live calls during this same
    investigation (up from 0/3 before the fix), a genuine, large improvement, but real LLM sampling
    variance (temperature > 0) means it is NOT 100% deterministic -- this test asserts a majority
    of N repeated live calls come back clean, not every single one, to avoid a flaky false failure
    on the same real variance already honestly measured, rather than either hiding the improvement
    behind a single lucky call or demanding perfection this fix was never going to reach. Requires
    a real live gateway call -- a mocked test could never have caught this, since the original bug
    (and the first, insufficient fix attempt) was a real model reasoning failure, not a prompt-
    text-presence bug.

    Same investigation also found the SAME class of bug for the `translation` domain: task020's
    own goal text ("in their own language") started correctly NOT touching templated_email after
    the fix above, but then began triggering translation instead -- a genuine finding, not a
    regression, since task020's OWN literal `_()`-wrapped strings would need real .po-file work to
    actually translate anything. The correct fix (mirroring task044's own already-established
    pattern in the adjusted 50-task file: a plain per-language Python dict, never translate=True/
    .po files) is the same shape of carve-out, added and verified the same way.
    """
    from infra.gateway_client import ModelGatewayClient

    task020_goal = (
        "When a meerwerk is marked as accepted, automatically send a confirmation email to the "
        "customer in their own language. Include the meerwerk reference, total amount, and "
        "expected finish date."
    )
    genuine_template_goal = (
        "Set up a reusable branded email template in Settings that marketing can edit, and send "
        "it as a one-off promotional blast to all customers."
    )
    genuine_translation_goal = (
        "Make the product name field translatable so each language shows its own version, "
        "editable per-language in the product form."
    )
    N = 8

    async def run():
        client = ModelGatewayClient()
        model = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")
        repeated = []
        for i in range(N):
            r = await classify_unsupported_domain_touches(
                task020_goal, client, model, task_id=f"test-templated-email-carveout-{i}",
            )
            repeated.append(r)
        genuine_template_touches = await classify_unsupported_domain_touches(
            genuine_template_goal, client, model, task_id="test-templated-email-carveout-genuine",
        )
        genuine_translation_touches = await classify_unsupported_domain_touches(
            genuine_translation_goal, client, model, task_id="test-translation-carveout-genuine",
        )
        return repeated, genuine_template_touches, genuine_translation_touches

    repeated, genuine_template_touches, genuine_translation_touches = asyncio.run(run())
    clean_count = sum(1 for r in repeated if r == [])
    assert clean_count > N / 2, (
        f"a goal satisfiable via mail.thread message_post() + a plain per-language dict must NOT "
        f"be flagged as templated_email/translation in the majority of repeated live calls "
        f"(measured 10/11 clean when this fix was built): got {clean_count}/{N} clean, results={repeated}"
    )
    assert any(t["domain"] == "templated_email" for t in genuine_template_touches), (
        f"a goal genuinely asking for a reusable/editable template record must still be correctly "
        f"flagged: got {genuine_template_touches}"
    )
    assert any(t["domain"] == "translation" for t in genuine_translation_touches), (
        f"a goal genuinely asking for field-level translate=True must still be correctly flagged: "
        f"got {genuine_translation_touches}"
    )
    print(f"PASS: the templated_email/translation carve-outs correctly distinguish a message_post/"
          f"per-language-dict-satisfiable goal from a genuine template/translate=True goal, live, "
          f"against the real gateway ({clean_count}/{N} clean on the repeated goal)")


if __name__ == "__main__":
    test_unsupported_domains_json_is_real_and_loadable()
    test_returns_empty_list_for_an_ordinary_supported_goal()
    test_returns_the_real_touch_for_a_qweb_goal()
    test_filters_out_a_hallucinated_domain_name_not_in_the_real_list()
    test_filters_out_a_touch_with_an_empty_snippet()
    test_fails_open_never_blocks_on_a_classification_error()
    test_prompt_asks_for_semantic_not_literal_keyword_matching()
    print("(skipping live-gateway test in __main__ -- run via pytest with .env sourced)")
    print("\nALL CLASSIFY-UNSUPPORTED-DOMAIN-TOUCHES TESTS PASSED")
