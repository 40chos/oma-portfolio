"""Phase 30, P2c (§13, item 5): real, confirmed reliability gap found
live on the school_student task -- classify_root_cause() used to judge
each round's failure text in total isolation, with no visibility into
the SAME task's own recent history, mislabeling 28 of 29 rounds hitting
the literal same failure `one_off`. These tests confirm the prompt
actually carries the recent-round context through when given, and stays
unchanged (backward compatible) when omitted -- without needing a real
gateway call, by capturing the prompt generate_checked() receives.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.gateway_client import ModelGatewayClient
from manager.learning import classify_root_cause


def test_omitting_recent_round_summaries_reproduces_the_old_prompt_shape():
    captured = {}

    async def _fake_generate_checked(client, model, messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return '{"root_cause": "one_off"}'

    async def _run():
        with patch("manager.learning.generate_checked", new=AsyncMock(side_effect=_fake_generate_checked)):
            return await classify_root_cause("a plain failure", ModelGatewayClient(), "test-model")

    result = asyncio.run(_run())
    assert result == "one_off"
    assert "recent prior rounds" not in captured["prompt"]
    assert "Failure summary: a plain failure" in captured["prompt"]
    print("PASS: omitting recent_round_summaries reproduces the exact old prompt shape")


def test_recent_round_summaries_are_folded_into_the_real_prompt():
    captured = {}

    async def _fake_generate_checked(client, model, messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return '{"root_cause": "pattern_worth_a_rule"}'

    async def _run():
        with patch("manager.learning.generate_checked", new=AsyncMock(side_effect=_fake_generate_checked)):
            return await classify_root_cause(
                "hardcoded test assertion rejected again", ModelGatewayClient(), "test-model",
                recent_round_summaries=[
                    "Round 27 (one_off): hardcoded test assertion Code-Review keeps rejecting",
                    "Round 28 (one_off): same hardcoded assertion issue again",
                ],
            )

    result = asyncio.run(_run())
    assert result == "pattern_worth_a_rule"
    assert "recent prior rounds" in captured["prompt"]
    assert "Round 27 (one_off): hardcoded test assertion" in captured["prompt"]
    assert "Round 28 (one_off): same hardcoded assertion" in captured["prompt"]
    assert "it is pattern_worth_a_rule" in captured["prompt"]
    print("PASS: recent_round_summaries are genuinely folded into the real prompt text")


if __name__ == "__main__":
    test_omitting_recent_round_summaries_reproduces_the_old_prompt_shape()
    test_recent_round_summaries_are_folded_into_the_real_prompt()
    print("\nALL CLASSIFY-ROOT-CAUSE-RECENT-CONTEXT TESTS PASSED")
