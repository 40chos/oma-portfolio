"""The capability-class cascade -- Phase 4 step 4, shaped after Nexo's
real complexity_classifier.py pattern (§2.5): a cheap keyword/pattern
fast-path with zero model calls for the confident majority of cases,
falling through to a cached, whitelisted, temperature-zero LLM call
only when genuinely ambiguous, defaulting to the more
conservative/more-tool-restricted class whenever unsure.

This decides `capability_class` only (data_change / module_dev /
readonly) -- NOT `tier`, which stays entirely separate and mechanical
(manager/charter.py's check_sensitive_paths(), no model involved at
all, per §2.5's own instruction that the two must not be conflated).

Model: qwen3.6-27b (revised 2026-07-13 -- see infra/gateway_client.py's
module docstring for the infra change this follows: qwen3-14b is
retired, every non-coder role including this one now shares the single
resident reasoning model on GPU Worker 02). Phase 3's original finding
still holds and is exactly why this file already treats /no_think as
unreliable rather than trusted: qwen3.6-27b ignores /no_think entirely
and always produces a hidden chain-of-thought, so `_llm_classify()`
below never assumes thinking was skipped -- it requires an explicit
</think> tag before trusting the response, treating its absence as
truncation rather than "the model skipped thinking," the same
discipline compress_session() uses in Phase 3. This was already correct
for qwen3.6-27b before this file switched to it full-time; only the
model name changed, not the completeness-check logic.
"""

from __future__ import annotations

import json
import re

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import strip_think_block

DATA_CHANGE = "data_change"
MODULE_DEV = "module_dev"
READONLY = "readonly"
VALID_LABELS = {DATA_CHANGE, MODULE_DEV, READONLY}

# An ambiguous case should default toward FEWER tools available, not
# more -- readonly is the most tool-restricted of the three (no write
# capability, no deployment capability at all), so it's the safe default
# whenever the cascade can't confidently decide.
CONSERVATIVE_DEFAULT = READONLY

# Phase 30 §26 follow-up (2026-08-04): explicit decision, closed out -- this classifier does NOT
# need live-schema access (§26's own fix for Build/Code-Review), and this is a considered "no,"
# not an unexamined gap. What it decides (data_change vs. module_dev vs. readonly, see
# _LLM_PROMPT_TEMPLATE below) is a coarse, structural judgment about the SHAPE of the request --
# "modifying existing data" vs. "writing/extending module code" vs. "investigation only" -- driven
# by the request's own verb and intent, not by which fields a target model happens to have. "Add a
# field for X" is module_dev whether or not the target model already has 3 fields or 300; "update
# order #123's status" is data_change regardless of order.state's real selection values; "show me
# overdue orders" is readonly regardless of schema. Unlike Build (writing field-referencing code)
# or Code-Review (catching a hallucinated field reference), real field-level detail here would be
# irrelevant noise to the ONE decision this call makes, not missing grounding. If this classifier
# is ever asked to also make a MORE granular decision that genuinely depends on real schema
# content, this reasoning should be re-examined then -- it is not a permanent, unconditional "no."


class ClassificationIncompleteError(RuntimeError):
    """Raised internally when the model's response was cut off before
    it finished thinking -- caught by the caller, which falls back to
    CONSERVATIVE_DEFAULT rather than trusting a truncated response.
    """


# Ordered so the most distinctive, least-ambiguous phrasings are checked
# first. A keyword hit returns immediately with zero model calls.
_READONLY_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\baudit\b", r"\bcode review\b", r"\breview the codebase\b",
        r"\bquality (audit|review)\b", r"\binvestigat\w*\b(?!.*\b(fix|change|update)\b)",
        # Real, general fix found live (2026-07-16, Phase 20 Area 2,
        # then found INSUFFICIENT live again 2026-07-17 -- confirmed via
        # a direct reproducible failure, not guessed): this pattern's
        # own job is catching "give me a read-only audit/investigation"
        # -- but "read-only"/"read only" ALONE is a genuinely ambiguous
        # signal, since it's also an ordinary Odoo permission-level
        # description that appears constantly in this project's own
        # module_dev tasks. The 2026-07-16 fix added a negative lookahead
        # excluding "read-only access" specifically -- but that's just
        # ONE of many legitimate non-audit phrasings; confirmed live
        # 2026-07-17: "...should not see this field at all, not even as
        # read-only" (a field-visibility restriction task, module_dev)
        # ALSO matched, silently blocking the ENTIRE
        # restrict_field_visibility_to_group batch shape (tasks #49-54)
        # from ever creating a real task at all, twice in a row --
        # confirmed via direct DB query showing zero task_created events
        # across the whole submission window. A negative lookahead can
        # only ever exclude phrasings anticipated in advance; the real,
        # general fix is requiring READ-ONLY to actually CO-OCCUR with a
        # genuine audit/investigation-intent word nearby (the thing this
        # whole pattern list is actually trying to detect), rather than
        # trusting "read-only" alone as sufficient signal -- this can't
        # be defeated by yet another permission-description phrasing no
        # one anticipated, since it requires the REAL intent word too.
        r"\bread[- ]?only\b.{0,40}\b(audit|review|investigat\w*)\b",
        r"\b(audit|review|investigat\w*)\b.{0,40}\bread[- ]?only\b",
        r"\bdiagnos\w*\b", r"\btechnical debt\b",
        r"\bdead code\b", r"\bhardcoded values\b",
    ]
]
_MODULE_DEV_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bnew module\b", r"\badd\b.{0,40}\bfields?\b", r"\bscaffold\b",
        r"\bbuild (a |an )?(new )?module\b", r"\bcreate (a |an )?(new )?module\b",
        # Real, general fix found live (2026-07-16, Phase 20 Area 2): the
        # two patterns above require "create"/"new" immediately adjacent
        # to "module" -- real request phrasing routinely has descriptive
        # words in between ("Create a small new Odoo module that adds a
        # new security group..."), which fell through this fast path
        # entirely on every one of ~59 real batch submissions, forcing
        # every single one through the LLM classifier fallback below.
        # That fallback is confirmed non-deterministic under this
        # project's own batched inference serving (temp=0.0 is not
        # sufficient for bit-identical output under vLLM-style
        # continuous batching), and its own prompt explicitly defaults
        # to "readonly" when genuinely unsure -- together this produced
        # a real, confirmed ~15% spurious-"readonly" misclassification
        # rate on unambiguous module-dev requests, each one surfacing as
        # a nonsensical "I can run that audit, but I need to know what
        # to point it at" clarification with no path to resolve it.
        # These two broader patterns catch the same intent with
        # arbitrary text in between, keeping the whole match on this
        # free, deterministic fast path instead of the flaky LLM one.
        r"\bcreate\b.{0,40}\bmodule\b", r"\bnew\b.{0,40}\bmodule\b",
        r"\bservice module\b", r"\bcalendar\b.*\bschedul\w*\b", r"\bproduct selection\b",
        r"\blink(ing|ed)? .* to (projects?|customers?|employees?)\b",
        # Real, general fix found live (2026-07-27, Phase 26A's own live
        # verification): a goal using this project's OWN established
        # structured goal-metadata convention -- a literal "Field: <name>
        # (<Type>)" line, used by essentially every real module_dev task
        # this project submits (every one of tasks 001-009, and every
        # goal-authoring convention documented across the Phase 25/26
        # reports) -- fell through both fast-path lists exactly the same
        # way the 2026-07-16 fix above describes for "create a module"
        # phrasing: nothing here recognized it, forcing the request
        # through the same "confirmed non-deterministic... ~15%
        # spurious-readonly" LLM fallback, and it misfired twice in a
        # row, live, on an ordinary, unambiguous field-restriction
        # request ("Field: developer_notes (Text)\nRestrict to group:
        # ..."), surfacing the identical nonsensical "I can run that
        # audit, but I need to know what to point it at" clarification.
        # This is a deterministic, zero-ambiguity signal -- a goal
        # naming a concrete field via this project's own convention is
        # never, in this codebase's real usage, anything other than a
        # module_dev request -- so it belongs on the free, deterministic
        # fast path, not gambled on the flaky LLM classifier.
        r"(?m)^\s*Field(?:\s*name)?:\s*`?\w+`?",
        # Real bug found live (2026-08-06, task027 of the SITE 50-task fix-pass): a request
        # describing a STANDING automation rule -- "whenever a record's state changes to X,
        # automatically do Y" -- is structurally identical in shape to task020's own
        # already-fixed state-transition-trigger tasks (both require writing new module code,
        # e.g. a write() override or an automated action, to react to a change going forward)
        # and is NOT a one-off data_change like "update order #123's status" (a single mutation
        # against a specific existing record, right now). Neither fast-path list had a pattern
        # for this "trigger on future change" shape, so it fell through to the flaky LLM
        # classifier (documented above as non-deterministic) and landed on data_change instead
        # of module_dev -- confirmed live: task027 was blocked outright by the deliberate
        # capability_readiness.py data_change gate before a single round could even start, on a
        # request that was never a data_change request in the first place. Catching the
        # "whenever/every time X changes to Y" shape deterministically here, before the LLM
        # fallback, is the same "free, deterministic fast path instead of the flaky LLM one"
        # discipline every other fix in this list already follows.
        r"\b(whenever|every time|each time)\b.{0,80}\bchanges? to\b",
        r"\boverride\b.{0,20}\bwrite\(\)",
        # Real, general fix found live (2026-08-11, Phase 34 batch execution): "Edit the
        # existing, already-installed module X directly" -- this project's own established
        # convention for the edit_existing_module: input shape (see manager/loop.py's own
        # docstring on that exact input key) -- fell through both fast-path lists the same way
        # task027's "whenever X changes to Y" shape did above, landing on data_change and getting
        # blocked outright by capability_readiness.py's deliberate data_change gate, on a request
        # that edits a MODULE'S OWN SOURCE FILE (e.g. security/ir.model.access.csv, loaded at
        # install time) -- structurally module_dev, never a live-record data_change, regardless
        # of the file happening to be a CSV whose own content LOOKS like data rows. Confirmed
        # live: the LLM fallback's own prompt defines data_change as "modifying existing Odoo
        # data/records," and conflated "editing a row in a source CSV file" with that definition.
        r"\balready-installed module\b", r"\bedit the existing\b.{0,20}\bmodule\b",
        # Real, general fix found live (2026-08-11, Phase 34 Batch L survey): a scheduled BATCH
        # transformation ("every night at midnight, automatically change all X that have been in
        # Y for N days to Z") is the same structural shape as the "whenever X changes to Y"
        # event-triggered case above -- both require writing new module code (here, an ir.cron +
        # a batch-update method) to react going forward, never a one-off data_change against a
        # specific record right now -- but the phrasing differs just enough ("every
        # night/hour/week... automatically" instead of "whenever/every time") that the pattern
        # above never matched it, and it fell through to the same data_change misclassification
        # and the same capability_readiness.py permanent block, on a request that was never a
        # data_change request in the first place. This is the entire Batch L (cron) direction's
        # own historical 0% pass rate's most common single cause, confirmed live.
        r"\bevery\s+(night|hour|day|week|morning|midnight)\b.{0,80}\bautomatically\b",
        r"\bautomatically\b.{0,80}\bevery\s+(night|hour|day|week|morning|midnight)\b",
        # Real, general fix found live (2026-08-17, overnight `workflow_with_custom_
        # buttons_or_cron` certification push): "add a button ... [to the] form view"
        # -- the entire defining shape of this direction -- had NO fast-path coverage
        # at all, unlike fields/modules/CSV edits above. Confirmed live: 2 of 2 real
        # attempts on this exact phrasing fell through to the flaky LLM fallback
        # (documented above as a confirmed ~15% spurious-readonly rate) and both landed
        # on readonly, producing the same nonsensical "I can run that audit, but I need
        # to know what to point it at" clarification. Same fix discipline as every
        # other pattern in this list: a deterministic, zero-ambiguity signal belongs on
        # the free fast path, not gambled on the flaky classifier.
        r"\badd (a |an )?button\b", r"\bbutton\b.{0,60}\bform view\b",
        r"\bscheduled action\b", r"\bcron job\b", r"\bir\.cron\b",
        # Real, general fix found live (2026-08-17, overnight `record_rule_row_level_
        # security` certification push): "add a record rule ..." -- the entire defining
        # shape of this direction -- had NO fast-path coverage either, the exact same
        # gap class as the button pattern above. Confirmed live: 2 consecutive real
        # attempts on this phrasing (2 different target models, ruling out a model-
        # specific fluke) fell through to the flaky LLM fallback and both landed on
        # readonly, the identical nonsensical "I can run that audit, but I need to know
        # what to point it at" clarification. Same fix discipline: a deterministic,
        # zero-ambiguity signal belongs on the free fast path.
        r"\badd (a |an )?record rule\b", r"\brecord rule\b.{0,60}\bir\.rule\b",
        r"\bir\.rule\b.{0,60}\brecord rule\b",
    ]
]
_DATA_CHANGE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bupdate (the )?(record|data|value)s?\b", r"\bfix (the )?data\b",
        r"\bcorrect (the )?value\b", r"\bchange (the )?field value\b",
        r"\badjust\b.*\b(invoice|total|amount|price)\b",
    ]
]

_classification_cache: dict[str, str] = {}


# Real bug found live (2026-08-06, task027 of the SITE 50-task fix-pass): the original
# `'[^']*'` alternative treated ANY apostrophe as a potential opening quote, including a
# possessive contraction's apostrophe ("meerwerk record's state changes to 'accepted'") -- the
# contraction's apostrophe was greedily paired with the NEXT real closing quote, silently
# stripping genuine intent-bearing text in between ("'s state changes to '" -- eating "state
# changes to") as if it had been quoted literal content. Fixed by requiring a single-quote
# opening/closing pair to NOT be adjacent to a word character on the outside (a real quoted
# literal is always preceded/followed by whitespace, punctuation, or string boundaries; a
# contraction's apostrophe is always adjacent to letters on both sides).
_QUOTED_LITERAL_RE = re.compile(r"(?<!\w)'[^']*'(?!\w)|\"[^\"]*\"")


def _fast_path(text: str) -> str | None:
    # Real, general fix found live (2026-07-16, Phase 20 Area 2): a
    # single-quoted literal name in the request (a group name, field
    # name, menu name -- e.g. 'Sales Orders Read Only') can coincidentally
    # contain any word, including ones the READONLY patterns below key
    # on ("read only" appearing INSIDE a group's own chosen name, not as
    # a description of the task's own intent) -- confirmed live: this
    # produced a real, repeat misclassification of a module_dev request
    # to READONLY even after the negative-lookahead fix immediately
    # below already handled the more common "grants ... read-only
    # access" phrasing. Quoted literal names are never the intent-
    # bearing part of a sentence, so stripping them before matching is
    # a general fix, not a narrow patch for this one name.
    intent_text = _QUOTED_LITERAL_RE.sub(" ", text)

    # Readonly checked first: an audit/review request that happens to
    # also mention "module" (e.g. "audit the service module") should
    # still be readonly, not module_dev.
    for pattern in _READONLY_PATTERNS:
        if pattern.search(intent_text):
            return READONLY
    for pattern in _MODULE_DEV_PATTERNS:
        if pattern.search(intent_text):
            return MODULE_DEV
    for pattern in _DATA_CHANGE_PATTERNS:
        if pattern.search(intent_text):
            return DATA_CHANGE
    return None


_LLM_PROMPT_TEMPLATE = """\
Classify the following task request into EXACTLY ONE of these three \
capability classes:

- "data_change": modifying existing Odoo data/records, no new code or module.
- "module_dev": writing or extending Odoo module code (new fields, new \
models, new views, new modules).
- "readonly": investigation, diagnosis, or an audit only -- no write, \
no deployment, at all.

If genuinely unsure which one applies, choose "readonly" -- it is the \
safest, most restricted option.

Request: {request_text}

Respond with ONLY a JSON object of the exact shape:
{{"capability_class": "data_change" | "module_dev" | "readonly"}}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_capability_class_response(raw: str, max_tokens: int) -> str:
    """The shared JSON-extraction/validation tail both the local and cloud-escalated paths use --
    never duplicated, so a fix to one path's own parsing always applies to both.

    Revised 2026-07-13 (real false-positive bug found live the same
    day as the two-GPU-host migration -- see
    infra/structured_output.py's generate_checked() docstring for the
    full story): the old assumption that this model always includes at
    least an empty closing </think> before its real answer was true on
    the old shared host but does NOT hold on GPU Worker 02's dedicated
    instance, which can complete a full, valid, correctly-classified
    response with no think markers at all. Only a visible opening
    <think> with no matching close is unambiguous evidence of a real
    cut-off trace; absence of both tags means the model chose not to
    think, not that it was truncated.
    """
    if "<think>" in raw and "</think>" not in raw:
        raise ClassificationIncompleteError(
            f"Response truncated mid-think (opening <think> with no closing tag) within "
            f"max_tokens={max_tokens}. Raw: {raw[:200]!r}"
        )

    cleaned = strip_think_block(raw)
    match = _JSON_RE.search(cleaned)
    if not match:
        raise ClassificationIncompleteError(f"No JSON object found in cleaned response: {cleaned!r}")

    parsed = json.loads(match.group(0))
    label = str(parsed.get("capability_class", "")).strip().lower()
    if label not in VALID_LABELS:
        return CONSERVATIVE_DEFAULT
    return label


async def _llm_classify(
    request_text: str,
    client: ModelGatewayClient,
    model: str,
    max_tokens: int = 1500,
    task_id: str | None = None,
) -> str:
    prompt = _LLM_PROMPT_TEMPLATE.format(request_text=request_text)
    raw = await client.generate(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        no_think=True,
        temperature=0.0,
        max_tokens=max_tokens,
        task_id=task_id, actor="manager", call_label="Classifying capability class",
    )
    return _parse_capability_class_response(raw, max_tokens)


async def classify_capability_class(
    request_text: str,
    client: ModelGatewayClient,
    model: str,
    use_cache: bool = True,
    task_id: str | None = None,
) -> str:
    """The full cascade: fast keyword path first, real cloud-tier escalation second (item 12/22,
    2026-08-02 -- ONLY when infra.cloud_escalation.cloud_escalation_enabled() is actually on, off
    by default), cached local LLM fallback third, conservative default on any failure. Never
    raises -- a caller always gets back one of the three valid labels.

    `fast_result is None` (the deterministic keyword path found nothing) IS this call site's own
    real, non-invented local-model-uncertain signal -- reaching for an LLM at all already means
    the fast path was genuinely unsure, exactly the caller-supplied uncertainty
    infra.cloud_escalation.decide_cloud_escalation() requires (never a fabricated confidence
    score). A cloud-escalation attempt that fails for any reason (disabled, network error,
    malformed response) silently falls through to the existing local LLM path -- a cloud outage
    must never block classification.
    """
    cache_key = request_text.strip().lower()
    if use_cache and cache_key in _classification_cache:
        return _classification_cache[cache_key]

    fast_result = _fast_path(request_text)
    if fast_result is not None:
        if use_cache:
            _classification_cache[cache_key] = fast_result
        return fast_result

    from infra.cloud_escalation import maybe_escalate_to_cloud, truncate_for_cloud_prompt

    cloud_raw = await maybe_escalate_to_cloud(
        "classify_capability_class", local_model_uncertain=True, task_id=task_id,
        prompt=_LLM_PROMPT_TEMPLATE.format(request_text=truncate_for_cloud_prompt(request_text)),
        max_tokens=1500,
    )
    if cloud_raw is not None:
        try:
            label = _parse_capability_class_response(cloud_raw, 1500)
        except Exception:
            label = None
    else:
        label = None

    if label is None:
        try:
            label = await _llm_classify(request_text, client, model, task_id=task_id)
        except Exception:
            label = CONSERVATIVE_DEFAULT

    if use_cache:
        _classification_cache[cache_key] = label
    return label
