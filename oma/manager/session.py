"""Session facts (a smaller, direct version of Pulsar's background fact
extractor) + the token-triggered summarizer -- Phase 3 of the build plan,
§2.2's pattern: two distinct mechanisms working alongside each other, not
one, mirrored from agents/src/pulsar/pipeline/session_memory.py and
agents/src/pulsar/context/summarizer.py.

At this scale -- one Manager, one conversation with Operator at a time, not
many concurrent sessions -- an in-process dict is genuinely sufficient
for session facts, per the build plan's own instruction not to reach for
Redis here without a real multi-process need.

The compression threshold is NOT inherited from Pulsar's own two disagreeing
numbers (its config schema defaults to 20,000 tokens; summarizer.py's own
header comment records the live deployed value as 27,000 -- an
unresolved discrepancy inside Pulsar's own codebase, per §2.2's correction).
Picked fresh here based on the Manager's own model: qwen3.6-27b has a
verified 65,536-token context window (§0.5.2); 20,000 leaves comfortable
room for the system prompt, project-memory block, and the model's own
output.

A real, confirmed model-behavior finding from testing this phase against
the real gateway: qwen3.6-27b does NOT honor the `/no_think` convention
at all (confirmed directly -- identical verbose "thinking process" output
with or without it). Unlike qwen3-14b (§2.4/Phase 2's finding), which at
least sometimes emits a bare, quick answer, qwen3.6-27b always produces a
long, real chain-of-thought -- structured the same way as the Phase 2
finding (the opening `<think>` tag is part of the invisible prompt
template; only the closing `</think>` appears in the visible content) --
before its actual answer. The practical consequence: any call to this
model needs a MUCH larger max_tokens budget than the actual desired
output would suggest, specifically to leave room for the hidden thinking
to complete and reach `</think>` before truncation cuts it off. A first
pass of this compression code used max_tokens=1024, which was too small:
the model's thinking alone exceeded it, so `</think>` was never reached
and strip_think_block() had nothing to strip up to -- the raw thinking
monologue leaked straight into the "summary." Fixed by raising the
compression call's budget substantially (below).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from infra.gateway_client import ModelGatewayClient

# qwen3.6-27b's verified context window is 65,536 tokens (§0.5.2).
# 20,000 leaves comfortable room for the system prompt, the project
# memory block, and the model's own reply -- picked deliberately for
# this model, not inherited from either of Pulsar's two disagreeing values.
COMPRESSION_TRIGGER_TOKENS = 20_000
KEEP_RECENT_TURNS = 8  # turn PAIRS kept verbatim, matching Pulsar's own choice

SESSION_FACT_TTL_SECONDS = 86_400.0  # 24h, matching Pulsar's own SessionMemoryStore

_EXTRACT_SYSTEM = """\
You extract durable named facts from a conversation turn between Operator \
(the product owner) and the Odoo Manager Agent.

Extract ONLY facts Operator explicitly stated (numbers, names, decisions, \
constraints, corrections). Do NOT extract questions asked, the Manager's \
own text, or general knowledge.

ALWAYS respond with valid JSON in this exact format:
{"facts": [{"key": "snake_case_id", "value": "exact value", "entity": "subject"}]}

If no facts found, respond with exactly: {"facts": []}
"""


def estimate_token_count(messages: list[dict]) -> int:
    """Same cheap heuristic as Pulsar's real estimate_token_count: UTF-8
    byte length // 3 over content fields only. A real tokenizer call is
    unnecessary precision for a trigger check.
    """
    total_bytes = sum(len(str(m.get("content") or "").encode("utf-8")) for m in messages)
    return total_bytes // 3


@dataclass
class SessionFact:
    key: str
    value: str
    entity: str
    turn_number: int
    extracted_at: float = field(default_factory=time.time)


@dataclass
class _SessionState:
    session_id: str
    facts: dict[str, SessionFact] = field(default_factory=dict)
    turn_count: int = 0
    last_activity: float = field(default_factory=time.time)


class SessionFactsStore:
    """Process-level in-process store. Evicts sessions idle for more
    than SESSION_FACT_TTL_SECONDS.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}

    def _get_or_create(self, session_id: str) -> _SessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = _SessionState(session_id=session_id)
        state = self._sessions[session_id]
        state.last_activity = time.time()
        return state

    def update_facts(self, session_id: str, facts: list[dict], turn_number: int) -> int:
        state = self._get_or_create(session_id)
        stored = 0
        for f in facts:
            key = (f.get("key") or "").strip()
            value = str(f.get("value") or "").strip()
            entity = str(f.get("entity") or "").strip()
            if not key or not value:
                continue
            state.facts[key] = SessionFact(key=key, value=value, entity=entity, turn_number=turn_number)
            stored += 1
        state.turn_count = turn_number
        return stored

    def get_facts_block(self, session_id: str) -> str:
        """AUTHORITATIVE block, prepended to every subsequent prompt --
        if a fact here conflicts with something in project memory, the
        fact wins, matching Pulsar's real convention. Returns "" when
        empty so callers can distinguish "no facts" cleanly.
        """
        state = self._sessions.get(session_id)
        if not state or not state.facts:
            return ""
        lines = [
            "<session_facts>",
            "AUTHORITATIVE Operator-stated values for this conversation.",
            "These override project memory when they conflict.",
        ]
        for fact in sorted(state.facts.values(), key=lambda f: f.turn_number):
            lines.append(f"  • [{fact.entity}] {fact.key} = {fact.value}")
        lines.append("</session_facts>")
        return "\n".join(lines)

    def evict_expired(self) -> int:
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_activity > SESSION_FACT_TTL_SECONDS
        ]
        for sid in expired:
            del self._sessions[sid]
        return len(expired)

    def fact_count(self, session_id: str) -> int:
        state = self._sessions.get(session_id)
        return len(state.facts) if state else 0


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _parse_facts_json(raw: str) -> list[dict]:
    if not raw:
        return []
    text = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            facts = parsed.get("facts", [])
            return facts if isinstance(facts, list) else []
        if isinstance(parsed, list):
            return parsed
        return []
    except json.JSONDecodeError:
        return []


async def extract_and_store_facts(
    client: ModelGatewayClient,
    model: str,
    user_message: str,
    session_id: str,
    store: SessionFactsStore,
    turn_number: int,
) -> None:
    """Fire-and-forget background call, exactly matching Pulsar's real
    shape: non-blocking, degrades silently (just doesn't add a fact) on
    any failure -- never blocks or breaks the main conversation.
    """
    if not user_message.strip():
        return
    try:
        raw = await client.generate(
            model=model,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": f"Operator said: {user_message[:1200]}"},
            ],
            no_think=True,
            temperature=0.0,
            max_tokens=1024,
        )
        from infra.structured_output import strip_think_block
        parsed = _parse_facts_json(strip_think_block(raw))
        if parsed:
            store.update_facts(session_id, parsed, turn_number)
    except Exception:
        # Silent degradation is the whole point -- a failed background
        # fact extraction must never block or break the conversation.
        return


_SUMMARY_PROMPT_TEMPLATE = """\
Summarize the following conversation between Operator and the Odoo Manager \
Agent into exactly 5 sections. Keep all concrete facts, decisions, \
constraints, and numbers -- especially any constraint Operator stated early \
in the conversation, even if the conversation has since moved on to \
other topics. Do not add anything not in the conversation. Output ONLY \
the 5 sections, no preamble.

{conversation}

---
## What's currently being worked on
[1-3 sentences: current goal, task context]

## Decisions made this conversation
[Bullet list: decisions, sign-offs, explicit approvals]

## Facts established
[Bullet list: specific facts, numbers, names Operator has stated]

## Tasks currently in flight
[Bullet list: delegated tasks, their status]

## Anything still open
[Bullet list: unresolved questions, pending sign-offs, constraints still in force]"""


def _format_turns_for_compression(
    messages: list[dict], keep_recent: int
) -> tuple[list[dict], list[dict]]:
    existing_summaries = [m for m in messages if m.get("role") == "summary"]
    raw_turns = [m for m in messages if m.get("role") != "summary"]
    keep_count = keep_recent * 2
    if len(raw_turns) <= keep_count:
        return [], messages
    to_compress_raw = raw_turns[:-keep_count] if keep_count else raw_turns
    to_keep = raw_turns[-keep_count:] if keep_count else []
    return existing_summaries + to_compress_raw, to_keep


def _build_compression_prompt(to_compress: list[dict]) -> str:
    lines = []
    for msg in to_compress:
        role = msg.get("role", "user")
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        if role == "summary":
            lines.append(f"[Previous Summary]\n{content}")
        elif role == "user":
            lines.append(f"Operator: {content}")
        elif role == "assistant":
            lines.append(f"Manager: {content}")
    return _SUMMARY_PROMPT_TEMPLATE.format(conversation="\n\n".join(lines))


async def compress_session(
    client: ModelGatewayClient,
    model: str,
    messages: list[dict],
    keep_recent: int = KEEP_RECENT_TURNS,
) -> tuple[list[dict], bool]:
    """Returns (new_messages, success). On failure, returns the
    untrimmed messages unchanged (degraded mode) rather than
    crashing or blocking -- matching Pulsar's real behavior exactly.
    """
    to_compress, to_keep = _format_turns_for_compression(messages, keep_recent)
    if not to_compress:
        return messages, True

    prompt = _build_compression_prompt(to_compress)
    try:
        from infra.structured_output import strip_think_block
        raw = await client.generate(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            no_think=True,
            temperature=0.1,
            # Generous budget, not a tight one: qwen3.6-27b's real,
            # confirmed behavior (see module docstring) is to always
            # produce a long hidden chain-of-thought before its actual
            # answer, with only the closing </think> tag visible in the
            # content. Too small a budget truncates before </think> is
            # ever reached, leaving the raw thinking monologue as the
            # "summary" -- caught below rather than silently accepted.
            max_tokens=6000,
            timeout_sec=180.0,
        )
        if "<think>" in raw and "</think>" not in raw:
            # Revised 2026-07-13: only a visible opening <think> with no
            # matching close is real evidence thinking never completed --
            # see infra/structured_output.py's generate_checked()
            # docstring for the full story (a real false-positive bug:
            # GPU Worker 02's dedicated qwen3.6-27b instance can return a
            # complete, valid answer with no think markers at all, unlike
            # the old shared host this module's original comment above
            # was written against).
            return messages, False
        summary_text = strip_think_block(raw)
        if not summary_text:
            return messages, False
        new_messages = [{"role": "summary", "content": summary_text}] + to_keep
        return new_messages, True
    except Exception:
        return messages, False
