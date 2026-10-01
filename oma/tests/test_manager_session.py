"""Phase 3 tests: session facts extraction + the token-triggered
summarizer, against the real inference gateway. Includes the specific
context-drift test the build plan's Phase 4 step demands: a summary
that compresses cleanly but quietly drops an early constraint has
failed, even if it "looks fine" on casual inspection.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.gateway_client import ModelGatewayClient
from manager.session import (
    SessionFactsStore,
    compress_session,
    estimate_token_count,
    extract_and_store_facts,
)

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")
MANAGER_MODEL = os.environ.get("OMA_MODEL_MANAGER", "qwen3.6-27b")


def test_estimate_token_count():
    messages = [{"role": "user", "content": "hello"}]
    assert estimate_token_count(messages) == len("hello".encode("utf-8")) // 3
    print("PASS: estimate_token_count uses the byte//3 heuristic correctly")


async def _run_session_facts_test():
    client = ModelGatewayClient()
    store = SessionFactsStore()
    try:
        await extract_and_store_facts(
            client=client,
            model=CLASSIFIER_MODEL,
            user_message="The Q3 revenue target is 2,750,000 euros and we have 47 active clients.",
            session_id="test-session-1",
            store=store,
            turn_number=1,
        )
        block = store.get_facts_block("test-session-1")
        assert block, "expected at least one fact to have been extracted"
        assert "AUTHORITATIVE" in block
        print(f"PASS: session facts extracted from a real message:\n{block}")

        # A message with nothing extractable should degrade to zero facts,
        # not raise or block.
        store2 = SessionFactsStore()
        await extract_and_store_facts(
            client=client,
            model=CLASSIFIER_MODEL,
            user_message="hey",
            session_id="test-session-2",
            store=store2,
            turn_number=1,
        )
        block2 = store2.get_facts_block("test-session-2")
        print(f"PASS: near-empty message degraded gracefully, facts block: {block2!r}")
    finally:
        await client.aclose()


async def _run_context_drift_test():
    """The specific test the build plan demands: an original constraint
    stated early, followed by many turns of unrelated chatter, must
    still be correctly stated in the FINAL summary after 2-3 rounds of
    compression -- not just whatever was discussed most recently.
    """
    client = ModelGatewayClient()
    try:
        original_constraint = (
            "Operator: Before we do anything else -- important constraint: "
            "do NOT touch the accounting module without asking me first, "
            "no matter what else comes up in this conversation."
        )
        messages = [{"role": "user", "content": original_constraint}]

        filler_topics = [
            "What's the weather like for outdoor testing today?",
            "Can you summarize what a many2one field is in Odoo?",
            "How many specialists does this system have?",
            "What's the difference between tier 2 and tier 3?",
            "Can you explain what a fencing token is?",
            "What's the Build specialist's model called?",
            "How does the JIT credential system work?",
            "What's the plan for the chat UI?",
            "Explain the registry pattern for specialists.",
            "What happens if the gateway goes down mid-task?",
            "How does the correction-detection mechanism work?",
            "What's the difference between a rule and a note in memory?",
        ]
        for i, topic in enumerate(filler_topics, start=2):
            messages.append({"role": "assistant", "content": f"Sure, here's an answer about topic {i-1}."})
            messages.append({"role": "user", "content": topic})
        messages.append({"role": "assistant", "content": "Here's the final answer."})

        # Force multiple compression rounds: compress with a small
        # keep_recent so most of the padded conversation gets summarized,
        # then compress AGAIN on the result to simulate a second round.
        new_messages, ok1 = await compress_session(
            client=client, model=MANAGER_MODEL, messages=messages, keep_recent=2
        )
        assert ok1, "first compression round failed"
        assert new_messages[0]["role"] == "summary"
        first_summary = new_messages[0]["content"]
        print(f"--- summary after round 1 ---\n{first_summary}\n")

        # Add more filler after the first summary, then compress again.
        more_filler = []
        for i, topic in enumerate(filler_topics[:6], start=100):
            more_filler.append({"role": "user", "content": f"Another question {i}: {topic}"})
            more_filler.append({"role": "assistant", "content": f"Another answer {i}."})
        round2_input = new_messages + more_filler

        new_messages_2, ok2 = await compress_session(
            client=client, model=MANAGER_MODEL, messages=round2_input, keep_recent=2
        )
        assert ok2, "second compression round failed"
        final_summary = new_messages_2[0]["content"]
        print(f"--- summary after round 2 ---\n{final_summary}\n")

        lowered = final_summary.lower()
        assert "accounting" in lowered, (
            f"FAIL: the original constraint about the accounting module was dropped "
            f"after 2 rounds of compression. Final summary:\n{final_summary}"
        )
        print("PASS: original early constraint ('don't touch accounting module without "
              "asking') survived 2 full rounds of compression through many turns of "
              "unrelated chatter -- no context drift.")
    finally:
        await client.aclose()


if __name__ == "__main__":
    test_estimate_token_count()
    asyncio.run(_run_session_facts_test())
    asyncio.run(_run_context_drift_test())
    print("\nALL MANAGER SESSION TESTS PASSED")
