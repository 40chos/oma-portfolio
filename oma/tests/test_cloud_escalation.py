"""P13 items 22/23/24 (docs/planning/PHASE30_OMA_SEAM_COVERAGE_AND_COMBINATORIAL_HARDENING_
2026-07-29.md §22.2): tests for infra/cloud_escalation.py's trigger condition, cost ceiling, and
logging shape. Pure, synchronous, zero LLM/GPU/network calls -- this module makes no calls itself.

2026-08-02: extended with tests for the real cloud API client (call_cloud_escalation_model),
the per-task cost tracker, and maybe_escalate_to_cloud() -- all real behavior, all with httpx
mocked out so zero live API spend occurs during testing (per the project owner's own explicit instruction).

2026-08-02 (later same day): further extended with tests for truncate_for_cloud_prompt() and a
real end-to-end proof that a deliberately oversized goal/request_text at the two wired call sites
that embed raw caller text (classify_capability_class, decompose_into_constraints_with_artifacts)
still respects the cost ceiling -- the real gap an independent review agent found in the first
build (decide_cloud_escalation() only ever checked spend already recorded, never the size of the
call about to be made).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from infra.cloud_escalation import (
    CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD,
    CloudEscalationError,
    build_cloud_escalation_log_event,
    call_cloud_escalation_model,
    cloud_escalation_enabled,
    clear_cloud_spend,
    decide_cloud_escalation,
    get_cloud_spend_so_far,
    is_eligible_call_site,
    maybe_escalate_to_cloud,
    record_cloud_spend,
    truncate_for_cloud_prompt,
)


def test_only_the_three_named_call_sites_are_eligible():
    assert is_eligible_call_site("classify_capability_class") is True
    assert is_eligible_call_site("decompose_into_constraints") is True
    assert is_eligible_call_site("select_specialist_for_retry") is True
    assert is_eligible_call_site("delegate_to_specialist") is False
    assert is_eligible_call_site("run_code_review_diff") is False
    assert is_eligible_call_site("await_verification") is False
    print("PASS: only the exact 3 named call sites are eligible -- never Build/Code-Review/Testing-QA")


def test_disabled_by_default_with_no_env_vars():
    with patch.dict(os.environ, {}, clear=True):
        assert cloud_escalation_enabled() is False
    print("PASS: cloud escalation is off by default with no env vars set")


def test_requires_both_api_key_and_explicit_opt_in():
    with patch.dict(os.environ, {"OMA_CLOUD_ESCALATION_API_KEY": "real-key"}, clear=True):
        assert cloud_escalation_enabled() is False, "a key alone, without the opt-in flag, must not enable this"
    with patch.dict(os.environ, {"OMA_CLOUD_ESCALATION_ENABLED": "true"}, clear=True):
        assert cloud_escalation_enabled() is False, "the opt-in flag alone, without a real key, must not enable this"
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        assert cloud_escalation_enabled() is True
    print("PASS: both a real API key AND the explicit opt-in flag are required, neither alone suffices")


def test_decision_is_false_for_an_ineligible_call_site_even_when_enabled():
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        decision = decide_cloud_escalation("delegate_to_specialist", local_model_uncertain=True, task_cost_so_far_usd=0.0)
    assert decision.should_escalate is False
    print("PASS: an ineligible call site never escalates, even with the mechanism fully enabled")


def test_decision_is_false_when_not_enabled_even_for_an_eligible_site():
    with patch.dict(os.environ, {}, clear=True):
        decision = decide_cloud_escalation("classify_capability_class", local_model_uncertain=True, task_cost_so_far_usd=0.0)
    assert decision.should_escalate is False
    print("PASS: an eligible call site never escalates while the mechanism itself is disabled")


def test_decision_is_false_without_flagged_uncertainty():
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        decision = decide_cloud_escalation("classify_capability_class", local_model_uncertain=False, task_cost_so_far_usd=0.0)
    assert decision.should_escalate is False
    print("PASS: no escalation without caller-flagged uncertainty -- never auto-detected/invented")


def test_decision_is_false_once_the_per_task_cost_ceiling_is_reached():
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        decision = decide_cloud_escalation(
            "classify_capability_class", local_model_uncertain=True,
            task_cost_so_far_usd=CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD,
        )
    assert decision.should_escalate is False
    print("PASS: the per-task cost ceiling is a real, hard cutoff")


def test_decision_is_true_when_every_condition_is_real_and_met():
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        decision = decide_cloud_escalation("select_specialist_for_retry", local_model_uncertain=True, task_cost_so_far_usd=0.02)
    assert decision.should_escalate is True
    print("PASS: escalation genuinely triggers when every real condition is met")


def test_log_event_shape_matches_the_p1d_informational_pattern():
    decision = decide_cloud_escalation("classify_capability_class", local_model_uncertain=False, task_cost_so_far_usd=0.0)
    event = build_cloud_escalation_log_event("classify_capability_class", decision, 0.0)
    assert event["should_escalate"] is False
    assert "message" in event and "not triggered" in event["message"]
    assert event["cost_ceiling_usd"] == CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD
    print("PASS: the log event carries a real, informational message and the cost ceiling for context")


def _patched_async_client(post_mock: AsyncMock):
    """Builds a patch target for httpx.AsyncClient matching this module's own
    `async with httpx.AsyncClient(...) as http: await http.post(...)` usage -- the context
    manager's __aenter__ returns an object whose .post is the given AsyncMock.
    """
    client_instance = MagicMock()
    client_instance.post = post_mock
    async_client_cm = MagicMock()
    async_client_cm.__aenter__ = AsyncMock(return_value=client_instance)
    async_client_cm.__aexit__ = AsyncMock(return_value=False)
    return patch("infra.cloud_escalation.httpx.AsyncClient", return_value=async_client_cm)


def _mock_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    return resp


def test_call_cloud_escalation_model_raises_without_an_api_key():
    with patch.dict(os.environ, {}, clear=True):
        try:
            asyncio.run(call_cloud_escalation_model("hello"))
            assert False, "must raise when no API key is configured"
        except CloudEscalationError as exc:
            assert "OMA_CLOUD_ESCALATION_API_KEY" in str(exc)
    print("PASS: call_cloud_escalation_model refuses to run without a real, configured API key")


def test_call_cloud_escalation_model_extracts_text_and_computes_real_cost():
    response = _mock_response(
        status_code=200,
        json_body={
            "content": [{"type": "text", "text": "hello from the cloud"}],
            "usage": {"input_tokens": 1000, "output_tokens": 2000},
        },
    )
    post_mock = AsyncMock(return_value=response)
    with patch.dict(os.environ, {"OMA_CLOUD_ESCALATION_API_KEY": "real-key"}, clear=True):
        with _patched_async_client(post_mock):
            result = asyncio.run(call_cloud_escalation_model("hello", max_tokens=50))
    assert result.text == "hello from the cloud"
    assert result.input_tokens == 1000
    assert result.output_tokens == 2000
    expected_cost = 1000 * (3.0 / 1_000_000) + 2000 * (15.0 / 1_000_000)
    assert abs(result.cost_usd - expected_cost) < 1e-9
    # The real key must never leak into the outgoing request's own visible call args in a way
    # this test would need to inspect -- confirm the header carries it (real usage), not that it
    # appears anywhere else.
    _, kwargs = post_mock.call_args
    assert kwargs["headers"]["x-api-key"] == "real-key"
    print("PASS: a successful cloud call extracts real text and computes real cost from real usage")


def test_call_cloud_escalation_model_raises_on_non_200_without_leaking_key():
    response = _mock_response(status_code=500, json_body={"error": "boom"})
    post_mock = AsyncMock(return_value=response)
    with patch.dict(os.environ, {"OMA_CLOUD_ESCALATION_API_KEY": "super-secret-key"}, clear=True):
        with _patched_async_client(post_mock):
            try:
                asyncio.run(call_cloud_escalation_model("hello"))
                assert False, "must raise on a non-200 response"
            except CloudEscalationError as exc:
                assert "super-secret-key" not in str(exc)
                assert "500" in str(exc)
    print("PASS: a non-200 response raises CloudEscalationError without leaking the API key")


def test_call_cloud_escalation_model_raises_on_network_error():
    post_mock = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
    with patch.dict(os.environ, {"OMA_CLOUD_ESCALATION_API_KEY": "real-key"}, clear=True):
        with _patched_async_client(post_mock):
            try:
                asyncio.run(call_cloud_escalation_model("hello"))
                assert False, "must raise on a real network error"
            except CloudEscalationError:
                pass
    print("PASS: a real network error raises CloudEscalationError, never a silent guess")


def test_call_cloud_escalation_model_raises_on_malformed_response():
    response = _mock_response(status_code=200, json_body={"nope": "no content field at all"})
    post_mock = AsyncMock(return_value=response)
    with patch.dict(os.environ, {"OMA_CLOUD_ESCALATION_API_KEY": "real-key"}, clear=True):
        with _patched_async_client(post_mock):
            try:
                asyncio.run(call_cloud_escalation_model("hello"))
                assert False, "must raise on a malformed/missing-field response"
            except CloudEscalationError:
                pass
    print("PASS: a malformed response body raises CloudEscalationError rather than a guessed result")


def test_cost_tracker_accumulates_and_pops_per_task():
    task_id = "test-task-cost-tracker-001"
    clear_cloud_spend(task_id)  # ensure a clean slate regardless of test order
    assert get_cloud_spend_so_far(task_id) == 0.0
    record_cloud_spend(task_id, 0.01)
    record_cloud_spend(task_id, 0.015)
    assert abs(get_cloud_spend_so_far(task_id) - 0.025) < 1e-9
    popped = clear_cloud_spend(task_id)
    assert abs(popped - 0.025) < 1e-9
    assert get_cloud_spend_so_far(task_id) == 0.0, "clear_cloud_spend must pop, not just peek"
    print("PASS: the per-task cost tracker accumulates across calls and clear_cloud_spend pops it")


def test_cost_tracker_handles_missing_task_id_gracefully():
    assert get_cloud_spend_so_far(None) == 0.0
    record_cloud_spend(None, 0.05)  # must not raise, must not create a phantom entry
    assert get_cloud_spend_so_far(None) == 0.0
    assert clear_cloud_spend(None) == 0.0
    print("PASS: the cost tracker no-ops safely for a missing/None task_id")


def test_maybe_escalate_to_cloud_returns_none_when_disabled():
    with patch.dict(os.environ, {}, clear=True):
        result = asyncio.run(
            maybe_escalate_to_cloud("classify_capability_class", local_model_uncertain=True, task_id="t1", prompt="hi")
        )
    assert result is None
    print("PASS: maybe_escalate_to_cloud returns None (never raises) when the mechanism is disabled")


def test_maybe_escalate_to_cloud_returns_none_for_an_ineligible_call_site():
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        result = asyncio.run(
            maybe_escalate_to_cloud("delegate_to_specialist", local_model_uncertain=True, task_id="t2", prompt="hi")
        )
    assert result is None
    print("PASS: maybe_escalate_to_cloud returns None for a call site outside the 3 approved ones")


def test_maybe_escalate_to_cloud_falls_back_to_none_on_a_real_client_failure():
    task_id = "test-task-fallback-002"
    clear_cloud_spend(task_id)
    post_mock = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        with _patched_async_client(post_mock):
            result = asyncio.run(
                maybe_escalate_to_cloud("classify_capability_class", local_model_uncertain=True, task_id=task_id, prompt="hi")
            )
    assert result is None, "a real cloud failure must fall back to None (local model), never raise"
    assert get_cloud_spend_so_far(task_id) == 0.0, "a failed call must not record any spend"
    clear_cloud_spend(task_id)
    print("PASS: maybe_escalate_to_cloud fails open to None (local fallback) on a genuine cloud error, records no spend")


def test_maybe_escalate_to_cloud_succeeds_and_records_real_spend():
    task_id = "test-task-success-003"
    clear_cloud_spend(task_id)
    response = _mock_response(
        status_code=200,
        json_body={
            "content": [{"type": "text", "text": "the real cloud answer"}],
            "usage": {"input_tokens": 100, "output_tokens": 100},
        },
    )
    post_mock = AsyncMock(return_value=response)
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        with _patched_async_client(post_mock):
            result = asyncio.run(
                maybe_escalate_to_cloud("select_specialist_for_retry", local_model_uncertain=True, task_id=task_id, prompt="hi")
            )
    assert result == "the real cloud answer"
    expected_cost = 100 * (3.0 / 1_000_000) + 100 * (15.0 / 1_000_000)
    assert abs(get_cloud_spend_so_far(task_id) - expected_cost) < 1e-9
    clear_cloud_spend(task_id)
    print("PASS: a genuine successful escalation returns the real cloud text and records its real cost")


def test_per_task_cost_ceiling_is_actually_enforced_end_to_end():
    """The user's own explicit ask: not just the pure decide_cloud_escalation() unit test above,
    but a real end-to-end proof that repeated real (mocked) cloud calls accumulating real spend
    eventually hit the $0.05/task ceiling and the mechanism genuinely stops escalating -- not
    merely asserted from a single hand-fed cost_so_far value.
    """
    task_id = "test-task-ceiling-e2e-004"
    clear_cloud_spend(task_id)
    # Each mocked call costs 100 input + 100 output tokens =
    # 100*3e-6 + 100*15e-6 = 0.0018 USD. 0.05 / 0.0018 ~= 27.8, so after ~28 calls the running
    # total crosses the $0.05 ceiling and the 29th (or earlier) call must be refused.
    response = _mock_response(
        status_code=200,
        json_body={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 100, "output_tokens": 100},
        },
    )
    post_mock = AsyncMock(return_value=response)
    per_call_cost = 100 * (3.0 / 1_000_000) + 100 * (15.0 / 1_000_000)
    calls_to_exceed_ceiling = int(CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD // per_call_cost) + 2

    escalated_count = 0
    declined_count = 0
    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        with _patched_async_client(post_mock):
            for _ in range(calls_to_exceed_ceiling):
                result = asyncio.run(
                    maybe_escalate_to_cloud(
                        "classify_capability_class", local_model_uncertain=True, task_id=task_id, prompt="hi",
                    )
                )
                if result is not None:
                    escalated_count += 1
                else:
                    declined_count += 1

    final_spend = get_cloud_spend_so_far(task_id)
    clear_cloud_spend(task_id)
    assert escalated_count > 0, "at least the early calls, while under budget, must genuinely escalate"
    assert declined_count > 0, "at least the later calls, once over budget, must genuinely decline"
    assert final_spend < CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD + per_call_cost, (
        "real spend must stop accumulating essentially right at the ceiling, not run away past it"
    )
    assert post_mock.call_count == escalated_count, (
        "the real API client must only actually be invoked for the calls that were genuinely allowed to escalate"
    )
    print(
        f"PASS: the $0.05/task ceiling is enforced end-to-end across real accumulated spend -- "
        f"{escalated_count} escalated, {declined_count} declined, final spend ${final_spend:.4f}"
    )


def test_concurrent_escalations_never_overrun_the_ceiling():
    """Phase 31 §1's own real, concrete fix: architect-stage drafts now call
    maybe_escalate_to_cloud() concurrently (asyncio.gather) from the SAME call site, which
    previously had a genuine check-then-act race against `_task_cloud_spend` -- N concurrent
    callers could all read the same cost_so_far below the ceiling, all decide to escalate, and
    all record afterward, together overrunning the ceiling by close to N calls' worth of cost.

    This fires 10 GENUINELY CONCURRENT calls (an artificial delay inside the mocked network call
    forces real overlap, not just interleaved coroutines that happen to run sequentially) against
    a fresh task and asserts the final recorded spend never exceeds the ceiling by more than one
    call's own real cost -- proving the atomic reserve-then-refund fix actually holds under real
    concurrency, not just in a single-caller unit test.
    """
    task_id = "test-task-concurrency-race-005"
    clear_cloud_spend(task_id)
    per_call_output_tokens = 1000
    per_call_real_cost = per_call_output_tokens * (15.0 / 1_000_000)  # 0.015 USD/call

    async def delayed_post(*args, **kwargs):
        await asyncio.sleep(0.05)  # forces real overlap across all concurrent callers
        return _mock_response(
            status_code=200,
            json_body={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 0, "output_tokens": per_call_output_tokens},
            },
        )

    post_mock = AsyncMock(side_effect=delayed_post)

    async def run_all():
        with _patched_async_client(post_mock):
            return await asyncio.gather(*(
                maybe_escalate_to_cloud(
                    "classify_capability_class", local_model_uncertain=True, task_id=task_id,
                    prompt="hi", max_tokens=per_call_output_tokens,
                )
                for _ in range(10)
            ))

    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        results = asyncio.run(run_all())

    escalated_count = sum(1 for r in results if r is not None)
    final_spend = get_cloud_spend_so_far(task_id)
    clear_cloud_spend(task_id)

    assert 0 < escalated_count < 10, (
        f"the ceiling must genuinely block SOME of the 10 concurrent callers, got {escalated_count} escalated"
    )
    assert post_mock.call_count == escalated_count, (
        "the real API client must only be invoked for calls the atomic reservation genuinely admitted"
    )
    assert final_spend <= CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD + per_call_real_cost + 1e-9, (
        f"final spend ${final_spend:.4f} must never overrun the ${CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD:.4f} "
        f"ceiling by more than one real call's cost (${per_call_real_cost:.4f}), even under real concurrency -- "
        f"got {escalated_count} concurrent escalations through"
    )
    print(
        f"PASS: 10 genuinely concurrent callers against a fresh task -- only {escalated_count} admitted by the "
        f"atomic reservation, final spend ${final_spend:.4f} stays within one call's cost of the "
        f"${CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD:.4f} ceiling, never overrun by the race"
    )


def test_truncate_for_cloud_prompt_leaves_short_text_untouched():
    short_text = "a normal, short task goal"
    assert truncate_for_cloud_prompt(short_text) == short_text
    print("PASS: text under the cap passes through truncate_for_cloud_prompt() unchanged")


def test_truncate_for_cloud_prompt_caps_long_text_with_a_visible_marker():
    long_text = "x" * 50_000
    truncated = truncate_for_cloud_prompt(long_text, max_chars=6000)
    assert len(truncated) < len(long_text)
    assert truncated.startswith("x" * 6000)
    assert "[truncated for cloud escalation]" in truncated
    print("PASS: text over the cap is cut to the exact limit with a visible truncation marker, "
          "not silently handed to the cloud model as if it were complete")


def test_classify_capability_class_oversized_request_text_respects_the_ceiling_end_to_end():
    """The real gap an independent review agent found: decide_cloud_escalation()'s cost-ceiling
    check only guards against spend ALREADY recorded, never the size of the call about to be
    made. Proves the fix -- an oversized request_text at this real wired call site results in a
    bounded outgoing prompt and a bounded real cost, not an unbounded single-call overshoot.
    """
    task_id = "test-task-oversized-classify-005"
    clear_cloud_spend(task_id)
    oversized_request_text = "zqxjw " * 20_000  # ~120,000 chars, well past any fast-path keyword

    captured_prompts = []

    async def _fake_post(url, headers=None, json=None):
        prompt = json["messages"][0]["content"]
        captured_prompts.append(prompt)
        # Simulate realistic tokenization scaling with the REAL outgoing prompt length (~4
        # chars/token), so the mocked cost genuinely reflects what was actually sent, not a
        # fixed stand-in value that would hide an unbounded prompt.
        input_tokens = len(prompt) // 4
        return _mock_response(
            status_code=200,
            json_body={
                "content": [{"type": "text", "text": '{"capability_class": "readonly"}'}],
                "usage": {"input_tokens": input_tokens, "output_tokens": 10},
            },
        )

    post_mock = AsyncMock(side_effect=_fake_post)

    from manager.classify import classify_capability_class

    fake_client = MagicMock()
    fake_client.generate = AsyncMock(
        side_effect=AssertionError("local model must not be called -- cloud escalation succeeded")
    )

    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        with _patched_async_client(post_mock):
            label = asyncio.run(
                classify_capability_class(
                    oversized_request_text, fake_client, "x", use_cache=False, task_id=task_id,
                )
            )

    assert label == "readonly"
    assert len(captured_prompts) == 1
    sent_prompt = captured_prompts[0]
    # The fixed template text around {request_text} is short and constant; the embedded task
    # text itself must never exceed the 6000-char cap plus its own truncation marker.
    assert len(sent_prompt) < 7500, (
        f"the real outgoing cloud prompt was {len(sent_prompt)} chars -- the oversized "
        f"request_text was not actually capped before being sent (fixed template text "
        f"(~590 chars) + the 6000-char cap + truncation marker (~40 chars) expected)"
    )
    final_spend = get_cloud_spend_so_far(task_id)
    clear_cloud_spend(task_id)
    assert final_spend < 0.01, (
        f"a single call against a deliberately oversized request_text cost ${final_spend:.4f} -- "
        f"should be bounded to a small fraction of the ${CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD} ceiling"
    )
    print(f"PASS: classify_capability_class() caps an oversized request_text before it reaches "
          f"the cloud -- outgoing prompt {len(sent_prompt)} chars, real cost ${final_spend:.4f}, "
          f"local model never called")


def test_decompose_into_constraints_with_artifacts_oversized_goal_respects_the_ceiling_end_to_end():
    """Same real gap, same fix, at the second call site that embeds raw caller text directly."""
    task_id = "test-task-oversized-decompose-006"
    clear_cloud_spend(task_id)
    oversized_goal = "add a field for tracking widget status " * 5000  # ~200,000 chars

    captured_prompts = []

    async def _fake_post(url, headers=None, json=None):
        prompt = json["messages"][0]["content"]
        captured_prompts.append(prompt)
        input_tokens = len(prompt) // 4
        return _mock_response(
            status_code=200,
            json_body={
                "content": [{
                    "type": "text",
                    "text": '{"constraints": [{"label": "widget_status_field", "creates": [], "requires": []}]}',
                }],
                "usage": {"input_tokens": input_tokens, "output_tokens": 20},
            },
        )

    post_mock = AsyncMock(side_effect=_fake_post)

    from manager.replanning import decompose_into_constraints_with_artifacts

    fake_client = MagicMock()

    with patch.dict(
        os.environ,
        {"OMA_CLOUD_ESCALATION_API_KEY": "real-key", "OMA_CLOUD_ESCALATION_ENABLED": "true"},
        clear=True,
    ):
        with _patched_async_client(post_mock):
            with patch("manager.replanning.call_structured", AsyncMock(
                side_effect=AssertionError("local model must not be called -- cloud escalation succeeded")
            )):
                items = asyncio.run(
                    decompose_into_constraints_with_artifacts(oversized_goal, fake_client, "x", task_id=task_id)
                )

    assert len(items) == 1 and items[0].label == "widget_status_field"
    assert len(captured_prompts) == 1
    sent_prompt = captured_prompts[0]
    assert len(sent_prompt) < 9000, (
        f"the real outgoing cloud prompt was {len(sent_prompt)} chars -- the oversized goal was "
        f"not actually capped before being sent (fixed instruction text (~1800 chars) + the "
        f"6000-char cap + truncation marker + JSON-shape suffix (~150 chars) expected)"
    )
    final_spend = get_cloud_spend_so_far(task_id)
    clear_cloud_spend(task_id)
    assert final_spend < 0.01, (
        f"a single call against a deliberately oversized goal cost ${final_spend:.4f} -- should "
        f"be bounded to a small fraction of the ${CLOUD_ESCALATION_COST_CEILING_PER_TASK_USD} ceiling"
    )
    print(f"PASS: decompose_into_constraints_with_artifacts() caps an oversized goal before it "
          f"reaches the cloud -- outgoing prompt {len(sent_prompt)} chars, real cost "
          f"${final_spend:.4f}, local model never called")


if __name__ == "__main__":
    test_only_the_three_named_call_sites_are_eligible()
    test_disabled_by_default_with_no_env_vars()
    test_requires_both_api_key_and_explicit_opt_in()
    test_decision_is_false_for_an_ineligible_call_site_even_when_enabled()
    test_decision_is_false_when_not_enabled_even_for_an_eligible_site()
    test_decision_is_false_without_flagged_uncertainty()
    test_decision_is_false_once_the_per_task_cost_ceiling_is_reached()
    test_decision_is_true_when_every_condition_is_real_and_met()
    test_log_event_shape_matches_the_p1d_informational_pattern()
    test_call_cloud_escalation_model_raises_without_an_api_key()
    test_call_cloud_escalation_model_extracts_text_and_computes_real_cost()
    test_call_cloud_escalation_model_raises_on_non_200_without_leaking_key()
    test_call_cloud_escalation_model_raises_on_network_error()
    test_call_cloud_escalation_model_raises_on_malformed_response()
    test_cost_tracker_accumulates_and_pops_per_task()
    test_cost_tracker_handles_missing_task_id_gracefully()
    test_maybe_escalate_to_cloud_returns_none_when_disabled()
    test_maybe_escalate_to_cloud_returns_none_for_an_ineligible_call_site()
    test_maybe_escalate_to_cloud_falls_back_to_none_on_a_real_client_failure()
    test_maybe_escalate_to_cloud_succeeds_and_records_real_spend()
    test_per_task_cost_ceiling_is_actually_enforced_end_to_end()
    test_concurrent_escalations_never_overrun_the_ceiling()
    test_truncate_for_cloud_prompt_leaves_short_text_untouched()
    test_truncate_for_cloud_prompt_caps_long_text_with_a_visible_marker()
    test_classify_capability_class_oversized_request_text_respects_the_ceiling_end_to_end()
    test_decompose_into_constraints_with_artifacts_oversized_goal_respects_the_ceiling_end_to_end()
    print("\nALL CLOUD-ESCALATION GOVERNANCE + CLIENT + WIRING TESTS PASSED")
