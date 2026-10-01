"""The structured-output layer, built as a thin wrapper ON TOP OF
ModelGatewayClient, per the build plan's Phase 2 step 4 -- never folded
into the client itself. This tracks its own retry count
(retry_sub_budget) completely separately from the client's own
network-level retries: a network retry means "the HTTP call itself
failed" (timeout, connection error, 5xx); a structured-output retry
means "the call succeeded but the model's response didn't validate
against the schema, so re-ask with the validation error included."
Conflating these two was exactly the gap the technical document's §7
flagged.

Uses an instructor-style validate-and-reask loop: ask for JSON matching
a Pydantic schema, parse it, and if it doesn't validate, feed the
validation error back to the model and ask again -- up to
retry_sub_budget times.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from infra.gateway_client import ModelGatewayClient

T = TypeVar("T", bound=BaseModel)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_UNMATCHED_CLOSE_THINK_RE = re.compile(r"^.*?</think>", re.DOTALL)


def strip_think_block(text: str) -> str:
    """deepseek-r1-distill-qwen-32b emits a visible <think>...</think>
    trace before its actual answer -- strip it before trying to parse
    anything out of the response.

    Also handles a real, confirmed quirk of qwen3-14b/qwen3.6-27b on
    this dev host's vLLM/llama-swap deployment: even with `/no_think`,
    the chat template opens the `<think>` tag itself as part of the
    (invisible-to-us) prompt, so the model's actual sampled content
    only contains the closing `</think>` with no matching opening tag
    -- e.g. a real observed response to a `/no_think` "reply with
    exactly: pong" request came back as literally
    ": pong\n\n</think>\n\npong". A regex requiring both tags misses
    this entirely and would leave the artifact in the "cleaned" text.
    So: first strip any genuine <think>...</think> pair, then strip
    anything before and including a remaining, unmatched closing tag.
    """
    text = _THINK_BLOCK_RE.sub("", text)
    text = _UNMATCHED_CLOSE_THINK_RE.sub("", text)
    return text.strip()


def _extract_json(text: str) -> str:
    """Models often wrap JSON in prose or a ```json fence even when
    asked not to -- pull out the first {...} or [...] block rather than
    assuming the whole response is bare JSON.
    """
    text = strip_think_block(text)
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    brace_match = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
    if brace_match:
        return brace_match.group(0)
    return text


class StructuredOutputError(RuntimeError):
    """Raised when the model still hasn't produced a valid instance
    after the full retry_sub_budget is exhausted.
    """


class IncompleteResponseError(RuntimeError):
    """Raised by generate_checked() when a thinking-mode model's
    response was cut off before it finished thinking (no closing
    </think> reached within the token budget given) -- the shared
    version of a check that was independently hand-copied at three
    separate call sites (Phase 3's compress_session(), Phase 4's
    classify.py, Phase 6's correction.py/learning.py) before being
    factored out here. Extracted the first time a third call site
    needed the exact same logic, per the project's own carried-forward
    open item from the Phase 4/5 reports.
    """


async def generate_checked(
    client: ModelGatewayClient,
    model: str,
    messages: list[dict],
    max_tokens: int = 6000,
    no_think: bool = True,
    temperature: float = 0.0,
    timeout_sec: float | None = None,
    task_id: str | None = None,
    actor: str | None = None,
    call_label: str | None = None,
    response_format: dict | None = None,
    node_id: str | None = None,
) -> str:
    """A single generate() call with the completeness discipline built
    in: if the model's response shows clear evidence of being cut off
    mid-thought, raise IncompleteResponseError rather than silently
    returning a truncated, unfinished reasoning trace as if it were a
    valid answer. Callers that want a safe default on failure should
    catch this themselves (each caller's own conservative default
    differs -- "readonly" for capability class, "unclear" for root
    cause, etc. -- so this stays a raise, not a swallow, at the
    shared-helper level).

    Real, confirmed false-positive bug found live (2026-07-13, the same
    day as the two-GPU-host migration): the original version of this
    check required `</think>` to appear ANYWHERE in the raw response,
    on the strength of a repeated finding that qwen3-14b/qwen3.6-27b on
    the old shared host always produced at least an empty think block.
    That assumption does NOT hold on GPU Worker 02's dedicated
    qwen3.6-27b instance (`JA-GPU2-27B-INT4-64K`) -- confirmed directly
    (a raw HTTP call with `finish_reason: "stop"`, and separately, three
    consecutive real code-review calls that returned complete, correct,
    well-formed JSON with zero `<think>` markers anywhere) that this
    host's deployment can complete a full response without ever
    emitting a think block at all. The old check was rejecting three
    perfectly good responses in a row as "truncated" purely because they
    never contained the substring `</think>` -- max_tokens and timeout
    were both red herrings chased before this was found; raising either
    made no difference because nothing was actually being cut off.

    The check now only fires on the one shape that's unambiguous
    evidence of a real cut-off trace: a visible opening `<think>` with
    no matching closing tag. Absence of both tags is treated as "this
    model chose not to think for this response," not truncation.
    Residual risk, noted rather than hidden: a free-text (non-JSON)
    caller whose response was genuinely truncated mid-thought, with the
    invisible-opening-tag-only pattern `strip_think_block()` documents,
    could in principle slip through unnoticed by this check alone --
    JSON-consuming callers (call_structured()) still have a real safety
    net underneath this (invalid/incomplete JSON fails schema validation
    and triggers its own reask loop), but a caller using generate_checked()
    directly for free text does not. None of today's real call sites hit
    this gap in testing; flagging it here for whoever adds the next one.

    Returns the CLEANED text (strip_think_block() already applied).

    response_format (2026-07-22, same finding as call_structured()'s
    use_grammar): grammar-constrained decoding confirmed live to fully
    bypass this model's "always writes a free-text reasoning trace even
    with /no_think" behavior -- a real, measured 41.84s call (self-report
    coverage prompt, no grammar) dropped to 5.37s with response_format
    set, producing a correct, complete answer with zero prose preamble.
    None (the default) preserves prior behavior for callers not yet
    opted in.
    """
    kwargs = {}
    if timeout_sec is not None:
        kwargs["timeout_sec"] = timeout_sec
    if response_format is not None:
        kwargs["response_format"] = response_format
    raw = await client.generate(
        model=model,
        messages=messages,
        no_think=no_think,
        temperature=temperature,
        max_tokens=max_tokens,
        task_id=task_id,
        actor=actor,
        call_label=call_label,
        node_id=node_id,
        **kwargs,
    )
    if "<think>" in raw and "</think>" not in raw:
        raise IncompleteResponseError(
            f"response truncated mid-think (opening <think> with no closing tag) within "
            f"max_tokens={max_tokens} -- raw={raw[:200]!r}"
        )
    return strip_think_block(raw)


def _strip_descriptions(node: object) -> object:
    """Pydantic's model_json_schema() auto-populates the top-level
    "description" key from the class's own docstring -- and every
    schema in this codebase writes that docstring for a DEVELOPER
    reading the source (long "why this exists" explanations, some
    1000+ chars), never for a model consuming the schema as a prompt.

    Real, confirmed bug found live (2026-07-22, chasing why
    SecurityAccessClaim's extraction took 30-50s in production against
    a model that answers equivalent prompts in ~1-3s isolated): with
    the docstring-bloated schema (2749 chars) in the system prompt,
    qwen3-9b-fast-extraction didn't answer at all -- it echoed the
    entire schema back verbatim as its "response" (response length
    matched the schema length exactly), which then failed validation
    and burned a full call_structured() retry, compounding the cost.
    Stripping every "description" key (recursively -- nested $defs for
    referenced sub-models get the same docstring treatment) dropped the
    same call to ~4.2s, a ~10x improvement, confirmed directly against
    the real inference host. No project schema uses Field(description=...)
    for genuine field-level hints (checked: only vendored library code
    does), so this strips pure bloat with nothing lost.
    """
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(v) for v in node]
    return node


def _prompt_schema(schema: type[BaseModel]) -> dict:
    return _strip_descriptions(schema.model_json_schema())


def _json_schema_response_format(schema: type[BaseModel]) -> dict:
    """Phase 18 (§22.12 Component 8): builds an OpenAI-compatible
    `response_format` dict from a Pydantic schema, for grammar-
    constrained decoding on the serving stack -- confirmed live against
    the real dev-build inference host as vLLM 0.24.0 behind `llama-swap`,
    which honors `response_format: json_schema` and returns
    schema-conformant JSON. This forces syntactically well-formed
    structured output BEFORE any of this project's own pre-write
    validators even run -- a cheaper, earlier filter for a class of
    malformed-output error (truncated JSON, wrong field names,
    structurally invalid content), never a replacement for those
    validators, which still run unchanged on whatever comes back.
    """
    return {
        "type": "json_schema",
        "json_schema": {"name": schema.__name__, "schema": _prompt_schema(schema)},
    }


async def call_structured(
    client: ModelGatewayClient,
    model: str,
    prompt: str,
    schema: type[T],
    retry_sub_budget: int = 3,
    temperature: float = 0.0,
    no_think: bool = True,
    task_id: str | None = None,
    actor: str | None = None,
    call_label: str | None = None,
    use_grammar: bool = False,
    timeout_sec: float | None = None,
    node_id: str | None = None,
) -> T:
    """Ask the model for JSON matching `schema`, validating and
    re-asking (with the validation error fed back in) up to
    retry_sub_budget times. This budget is entirely separate from the
    gateway client's own network-level retry count.

    no_think defaults True: structured extraction is exactly the
    "cheap, direct" case §0.5.2 says to run in no-think mode for
    qwen3-14b/qwen3.6-27b. Pass False explicitly if the schema-filling
    task genuinely needs the model's full reasoning.

    use_grammar (Phase 18, §22.12 Component 8): when True, threads a
    real `response_format: json_schema` grammar matching `schema`
    through to ModelGatewayClient.generate() -- confirmed available on
    the real dev-build inference host (vLLM 0.24.0 behind `llama-swap`).
    Defaults False so every existing call site keeps its exact prior
    behavior; opt in per call site once the serving stack that call site
    actually targets is confirmed to support it.

    timeout_sec (2026-07-30): real, confirmed gap found live on a
    9500+-round task -- every call through this function was stuck at
    ModelGatewayClient.generate()'s own default (DEFAULT_TIMEOUT_SEC,
    90s) with no way for a caller whose OWN prompt is unusually large
    (e.g. Build's scoped-edit prompt on a task with a huge accumulated
    round history) to ask for more headroom, unlike generate_checked()'s
    own callers, which already can. None (default) preserves every
    existing call site's exact prior behavior.
    """
    schema_json = json.dumps(_prompt_schema(schema), indent=2)
    messages = [
        {
            "role": "system",
            "content": (
                "Respond with ONLY a single JSON object matching this JSON "
                f"schema. No prose, no markdown fence, no explanation.\n\n{schema_json}"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    response_format = _json_schema_response_format(schema) if use_grammar else None

    generate_kwargs = {} if timeout_sec is None else {"timeout_sec": timeout_sec}
    last_error: Exception | None = None
    for attempt in range(1, retry_sub_budget + 1):
        raw = await client.generate(
            model=model, messages=messages, temperature=temperature, no_think=no_think,
            task_id=task_id, actor=actor, call_label=call_label, response_format=response_format,
            node_id=node_id,
            **generate_kwargs,
        )
        candidate = _extract_json(raw)
        try:
            parsed = json.loads(candidate)
            return schema.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            if attempt < retry_sub_budget:
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"That response didn't validate against the schema: {exc}\n"
                            "Respond again with ONLY a corrected JSON object matching "
                            "the schema above."
                        ),
                    }
                )

    raise StructuredOutputError(
        f"Model {model!r} failed to produce a valid {schema.__name__} after "
        f"{retry_sub_budget} attempts: {last_error}"
    ) from last_error
