"""The Code-Review specialist -- Phase 10. Reviews any proposed change
from another specialist (a diff review) against the real Odoo
Development Agent Constitution, and, repurposed for task 4, performs a
full read-only quality audit of the actual custom Odoo codebase. Per
the vision document's own repeated insistence: generation and
verification must never be the same agent instance -- this specialist
never writes anything to Odoo or to the codebase, in either mode.

Runs on qwen3.6-27b (the Manager's own model tier -- judgment, not code
generation), using the shared generate_checked() completeness
discipline (§0.5.2's own finding: this model ignores /no_think entirely
and always produces a long hidden <think> trace, so a generous
max_tokens budget and an explicit </think>-reached check are required,
never optional, for any call to it -- the same discipline already used
in manager/correction.py and manager/learning.py).

Two distinct TaskContract shapes route through the same run(), per a
small, explicit `inputs` convention (no new schema field needed):
  - `"diff_module:<module_name>"` -- review a Build specialist's real,
    already-written module in /mnt/extra-addons.
  - `"full_codebase_audit:<relative_path>"` -- task 4's read-only audit
    of the real custom Odoo codebase under /opt/site/site16.

Task 4's read-only property is enforced STRUCTURALLY, not by promise:
this module (and tools_odoo.codebase_read, which it exclusively uses
for all file access) never imports anything write-capable -- no
OdooToolClient, no ModuleDevToolchain, no write_module_file. There is
no write path to reach for even if a prompt or a bug tried to.
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from contracts.schema import CapabilityClass, SpecialistOutput, TaskContract, split_critical_rules
from contracts.scope import RoundScope, derive_round_scope
from infra.gateway_client import ModelGatewayClient
from infra.structured_output import IncompleteResponseError, _strip_descriptions, generate_checked
from manager.trace import publish_trace_event
from paths import skill_path
from specialists.constitution import ConstitutionNotFoundError, load_odoo_development_constitution
from tools_odoo.codebase_read import CodebaseReadError, read_codebase_tree, read_module_files
from tools_odoo.schema_grounding import resolve_current_schema_block

_AUDIT_SKILL_PATH = skill_path("odoo-codebase-audit")
_DEFAULT_MODEL_ENV = "OMA_MODEL_MANAGER"
_DEFAULT_MODEL_FALLBACK = "qwen3.6-27b"
# Real, confirmed regression found live (2026-07-13, same day as the
# two-GPU-host migration): 3/3 retries reliably truncated once this
# specialist's calls moved onto GPU Worker 02's dedicated qwen3.6-27b
# instance (`JA-GPU2-27B-INT4-64K`). First suspected max_tokens was too
# small -- doubling 6000 -> 16000 made ZERO difference (identical
# truncation point, byte-for-byte, on both a tiny one-file diff review
# AND a full codebase audit). That ruled out max_tokens: raising it
# again below is still reasonable generous headroom, but it was never
# the actual constraint. Direct diagnostic calls against this host
# confirmed the real cause is TIME, not tokens: this specialist retries
# 3x with the SAME _TIMEOUT_SEC on every attempt (see the retry loop
# below), and this host's real, measured generation speed for a
# substantial reasoning response is on the order of 60s for ~2000
# characters -- a genuinely long, multi-finding review response can
# need several times that. All 3 attempts were hitting the same 240s
# wall-clock ceiling and getting cut off at roughly the same point each
# time, which is exactly the "identical truncation regardless of
# max_tokens" symptom observed. Confirmed this host is genuinely slower
# for this workload than whatever served this same logical model name
# on the old shared host, which never surfaced this before.
_MAX_TOKENS = 16000  # generous headroom for this model's always-on <think> trace
# Real, confirmed bug found live (2026-07-29, school_student task, after
# 9500+ rounds of accumulated round history): a fixed max_tokens, added
# to a genuinely large prompt (round-history-heavy tasks like this one
# routinely exceed 49000 input tokens), can push the total past the
# backing model's own real context window -- confirmed live via a real
# 400 Bad Request: "maximum context length is 65536 tokens... prompt
# contains at least 49537 input tokens... total of at least 65537" (ONE
# token over). This surfaced as a hard, deterministic, un-retryable
# failure the gateway client reports as "gateway_unavailable" -- every
# retry hit the identical wall, since the prompt itself never shrinks
# between attempts. `_MODEL_CONTEXT_WINDOW`/`_max_tokens_for_prompt()`
# below cap the requested output to whatever headroom the ACTUAL prompt
# leaves, rather than assuming the fixed constant always fits -- never
# below a floor still large enough to produce a real review response.
_MODEL_CONTEXT_WINDOW = 65536
_ABSOLUTE_MIN_MAX_TOKENS = 500
_CONTEXT_SAFETY_MARGIN = 3000


def _max_tokens_for_prompt(prompt: str, requested: int = _MAX_TOKENS) -> int:
    """Conservative estimate: 1.3 chars/token. Real, confirmed live
    finding (2026-07-29/30, school_student task, a 9500+ round task
    whose own review prompt keeps growing as round history
    accumulates): a fixed chars/token ratio is fundamentally unreliable
    for this specialist's actual content -- the real, MEASURED ratio
    moved between two live failures of this exact function (~2.97
    chars/token on one call, ~1.95 on a later, larger call), and no
    tokenizer for the actual backing model is available in this
    project's own venv (a real tiktoken/AutoTokenizer dependency was
    considered and rejected: it would need network access to fetch
    encoding data, a real new external dependency this project's own
    "sovereign code" discipline argues against for a one-line safety
    margin calculation). 1.3 chars/token (well below both observed
    real ratios) plus a large fixed safety margin trade some unused
    output budget on an ordinary prompt for a real, durable guarantee
    against this exact class of hard, non-retryable 400 on a dense one.

    Real, confirmed follow-up bug found live in THIS function's own
    first fix attempt: a fixed "usable" floor (4000) that unconditionally
    overrode `available` reintroduced the exact same overflow class it
    was meant to prevent, the moment a real prompt was already large
    enough that even 4000 tokens of headroom didn't exist (the second
    live failure above: 59586 real input tokens left only ~2950 tokens
    of real headroom after this margin, and the floor pushed the
    request back up past that). `available` -- how much genuinely,
    arithmetically fits -- must always win; `_ABSOLUTE_MIN_MAX_TOKENS`
    exists only to keep the request itself sane (never zero or
    negative), never to override a real fit constraint. When even that
    minimum doesn't fit, the prompt itself is too large for this
    model's context window -- no output-token value can fix that; the
    caller still gets a small, honest, best-effort request rather than
    silently pretending safety this function cannot actually provide.
    """
    estimated_prompt_tokens = int(len(prompt) / 1.3)
    available = _MODEL_CONTEXT_WINDOW - estimated_prompt_tokens - _CONTEXT_SAFETY_MARGIN
    # `available` (what genuinely fits) always wins over `requested` --
    # never overridden upward by a floor, only ever floored at
    # _ABSOLUTE_MIN_MAX_TOKENS so the request itself stays sane (never
    # zero/negative) in the extreme case where the prompt alone leaves
    # no real headroom at all.
    return max(_ABSOLUTE_MIN_MAX_TOKENS, min(requested, available)) if available >= _ABSOLUTE_MIN_MAX_TOKENS \
        else max(1, available)
# This specialist's prompt is unusually large (the full Constitution +
# full skill text + real file contents, potentially several files) --
# confirmed via a real timeout that the default 90s per-attempt budget
# (fine for the Manager's own, much smaller correction/learning calls)
# isn't enough here. Raised again 2026-07-13 (240 -> 600) after the
# GPU Worker 02 migration -- this host's real measured speed for a
# substantial response needs more headroom than 240s reliably provides.
# Generous, not unbounded.
_TIMEOUT_SEC = 600.0

_VALID_SEVERITIES = {"blocking", "major", "minor", "info"}

_DIFF_PREFIX = "diff_module:"
_AUDIT_PREFIX = "full_codebase_audit:"


class ReviewFinding(BaseModel):
    location: str
    # P12 Tier A item 14 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
    # constrained to the exact four values the real generation prompt itself already asks for
    # ("a severity of exactly one of blocking/major/minor/info" -- see _run_review()'s own
    # prompt text below), matching what the LLM is actually instructed to produce, not just the
    # two values ("blocking"/"info") every filter in this file happens to check for today.
    severity: Literal["blocking", "major", "minor", "info"]
    explanation: str


class ReviewResult(BaseModel):
    findings: list[ReviewFinding]
    overall_assessment: str


class NoReviewTargetError(RuntimeError):
    """Raised when a TaskContract's inputs contain neither a
    diff_module: nor a full_codebase_audit: entry -- this specialist
    has nothing to review, and must never silently guess a target.
    """


class ReviewGenerationError(RuntimeError):
    """Raised when the model's review response was cut off before
    completing, or didn't parse as valid JSON matching ReviewResult --
    a real failure of this specialist's own review step, distinct from
    CodebaseReadError (a file-access failure) or a missing Constitution.
    """


_SCOPE_CLAIM_RE = re.compile(
    r"\b(round constraint|scope constraint|out[- ]of[- ]scope|explicit constraint|"
    r"strict round scope|constraint to exclude|constraint that)\b",
    re.IGNORECASE,
)
_GENUINE_SCOPE_LANGUAGE_RE = re.compile(
    r"\b(only this round|scope|exclude|out of scope|this round should|separately|later round)\b",
    re.IGNORECASE,
)


def _filter_hallucinated_scope_findings(findings: list[ReviewFinding], contract: TaskContract) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-07-19, Phase 20 Area 2)
    -- a prose-only prompt instruction (added earlier the same day,
    asking Code-Review to 'quote the exact phrase' before flagging
    something out of scope) was CONFIRMED, via a fresh live
    reproduction, to NOT stop this: a task's real goal explicitly said
    'adds a new security group... via its own access rule row', yet
    Code-Review invented 'violating the explicit round constraint to
    exclude security records' on round 1 itself, with nothing in the
    real goal ever mentioning scope limits at all, and kept repeating
    the identical hallucinated phrase verbatim across every round.

    Critical subtlety this filter is built around: `contract.rules`
    CANNOT be trusted as a source of truth for "was a scope constraint
    genuinely stated" -- rules accumulate each round's OWN prior
    failure text verbatim (see manager/loop.py's "Prior attempt (round
    N) failed..." construction), so a round-1 hallucination becomes
    round 2's own "rules" input, self-reinforcing indefinitely; only
    `contract.goal` is immutable across every round of a task and can
    be trusted as the real, original source of any genuine scope
    limit. Deterministic and general: applies to ANY future task where
    Code-Review invents a scope constraint, not hand-coded per task.

    A BLOCKING finding whose own text uses scope/constraint-hallucination
    -shaped language is downgraded to 'info' (kept visible, not silently
    dropped, but no longer blocks the round) UNLESS the task's own
    real, original goal text genuinely contains scope-limiting language
    itself -- in which case the finding is trusted as-is.
    """
    if _GENUINE_SCOPE_LANGUAGE_RE.search(contract.goal or ""):
        return findings  # the goal itself genuinely describes a scope limit -- trust the finding

    filtered = []
    for f in findings:
        if f.severity == "blocking" and _SCOPE_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- claimed a scope/round constraint not present "
                    f"in the task's own real goal text] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_LABEL_STOPWORDS = frozenset({
    "is", "the", "a", "an", "of", "for", "on", "to", "field", "fields", "method", "methods",
    "computed", "action", "button", "hook", "model", "flag", "value", "data",
})


def _label_significant_words(text: str) -> set[str]:
    """Mirrors specialists/build/specialist.py's own function of the same name -- duplicated,
    not imported, per this codebase's established specialist-layering convention.
    """
    words = re.split(r"[_\s]+", text.lower())
    return {w for w in words if w and w not in _LABEL_STOPWORDS}


def _label_fuzzy_matches_identifier(label: str, identifier: str) -> bool:
    """Mirrors specialists/build/specialist.py's own function of the same name exactly --
    duplicated, not imported. See that function's own docstring for the full reasoning.
    """
    label_lower, identifier_lower = label.lower(), identifier.lower()
    if label_lower in identifier_lower or identifier_lower in label_lower:
        return True
    return bool(_label_significant_words(label) & _label_significant_words(identifier))


_WRONG_CONSTRAINT_ATTRIBUTION_RE = re.compile(
    r"\b(?:Field|Method)\s+['\"]?(\w+)['\"]?.{0,60}?\bbelongs to\s+['\"]?([\w]+)['\"]?",
    re.IGNORECASE,
)
# Real, confirmed follow-up bug found live (2026-08-10, resume r204): the exact same
# scope-misattribution claim resurfaced in TWO further real phrasings the same round -- one
# simply dropped "is defined in this round but" (now covered by the widened gap `.{0,60}?`
# above), and one described the SAME cron `<field name="code">` reference this file's own
# `_CRON_CODE_METHOD_CALL_RE` sibling (specialists/build/specialist.py) already knows to parse,
# but phrased as "Cron record references 'model.run_daily_escalation_check()' which is a method
# from 'ticket_workflow_and_logging'" -- a structurally different sentence shape the main regex
# above was never built to match (the method name is embedded inside a `model.X()` call
# expression, not bare-quoted, and the verb is "is a method from" rather than "belongs to").
_WRONG_CONSTRAINT_ATTRIBUTION_CRON_REF_RE = re.compile(
    r"references\s+['\"]?[\w.]*?\.(\w+)\(\).{0,60}?\bis a method from\s+['\"]?([\w]+)['\"]?",
    re.IGNORECASE,
)


def _filter_hallucinated_wrong_constraint_attribution_findings(
    findings: list[ReviewFinding], contract: TaskContract,
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    daily_escalation_cron node, resumes r202 and r203): Code-Review repeatedly claimed "Field
    'escalation_message_posted' is defined in this round but belongs to
    'ticket_workflow_and_logging' which is explicitly NOT in scope for this round" (and the
    identical claim for the sibling method `run_daily_escalation_check`) -- factually false: the
    task's own ORIGINAL goal text assigns exactly this content ("run a daily automated check
    that flags any ticket still open more than 3 days after being assigned and posts an
    escalation message directly on that ticket") to THIS round's own real focus,
    `daily_escalation_cron` -- `ticket_workflow_and_logging`'s own goal text is a completely
    different requirement (state-change buttons/chatter logging). An explicit retraction note
    (r203) did not stop the identical claim recurring verbatim the very next round -- 2+
    recurrence of the same claim shape, meeting the standing rule for a general pipeline fix.

    Deliberately narrow and mechanical, reusing the SAME fuzzy word-overlap heuristic the
    Build-side scope validators/strippers already trust for the identical judgment (`_label_
    fuzzy_matches_identifier`, duplicated here per this codebase's own specialist-layering
    convention): a "belongs to constraint Y" claim about a named field/method X is downgraded
    only when X fuzzy-matches THIS round's own real focus label (`contract.current_
    constraint_label`) but does NOT fuzzy-match the claimed label Y -- i.e. the claim's own two
    halves disagree with each other on the mechanical, word-overlap evidence. A claim where X
    genuinely fuzzy-matches the claimed label Y (a plausible, real misplacement) is left
    completely untouched, since that shape is a genuine, checkable signal this filter has no
    business overriding.
    """
    current_focus = getattr(contract, "current_constraint_label", None) or ""
    if not current_focus:
        return findings
    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        match = (
            _WRONG_CONSTRAINT_ATTRIBUTION_RE.search(f.explanation)
            or _WRONG_CONSTRAINT_ATTRIBUTION_CRON_REF_RE.search(f.explanation)
        )
        if not match:
            filtered.append(f)
            continue
        member_name, claimed_label = match.group(1), match.group(2)
        matches_current_focus = _label_fuzzy_matches_identifier(current_focus, member_name)
        matches_claimed_label = _label_fuzzy_matches_identifier(claimed_label, member_name)
        if matches_current_focus and not matches_claimed_label:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {member_name!r} fuzzy-matches THIS round's "
                    f"own real focus ({current_focus!r}) but not the claimed constraint "
                    f"({claimed_label!r}) -- likely a scope misattribution, not a real "
                    f"violation] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# Real, general fix (2026-07-26, Phase 25D, task 004's own resubmission,
# right after specialists/build/specialist.py's own sum-compute autofix
# was corrected to overwrite an invented-subfield dependency): once the
# field/compute method deterministically matched the goal's own EXACT
# stated dependency (`depends on line_ids.price_unit`, verbatim, the
# same dependency this project's own site_50_tasks.md spec states),
# Code-Review invented a NEW complaint in the opposite direction --
# "ignores quantity and discount... incorrect financial totals" -- a
# business-logic requirement the goal never actually stated. This is
# the mirror image of `_filter_hallucinated_scope_findings` above (that
# one catches Code-Review inventing a constraint that EXCLUDES real,
# requested work; this one catches Code-Review inventing ADDITIONAL
# work never requested at all) -- same underlying failure mode,
# opposite direction.
_COMPUTE_DEPENDS_PHRASE_RE = re.compile(
    r"^\s*Field(?:\s*name)?:\s*`?\w+`?\s*\([^)]*\bdepends on\s+([\w.,\s]+?)(?:,\s*\w+\s*=|\))",
    re.IGNORECASE | re.MULTILINE,
)
_DEPENDS_PATH_RE = re.compile(r"(\w+)\.(\w+)")
# Mirrors specialists/build/specialist.py's own `_GOAL_NAMED_FIELD_RE`
# exactly -- duplicated, not imported, per this codebase's own
# manager/specialists layering (and specialists don't import from each
# other's internals either, per the shared registry-only pattern).
_GOAL_NAMED_FIELD_RE = re.compile(r"^\s*Field(?:\s*name)?:\s*`?(\w+)`?", re.IGNORECASE | re.MULTILINE)
# Historical note (2026-07-26): earlier versions of the filter below
# matched specific keyword patterns here ("ignores", "incomplete",
# "stale data", ...) -- removed after Code-Review kept inventing new
# phrasings that matched none of them, FOUR times in a row on the same
# provably-correct field. Replaced with a structural match (does the
# finding mention the field/subfield at all) gated by a ground-truth
# check against the real generated code -- see that function's own
# docstring for the full history.


def _filter_hallucinated_incomplete_compute_dependency_findings(
    findings: list[ReviewFinding], contract: TaskContract, files: dict[str, str],
) -> list[ReviewFinding]:
    """Downgrades a BLOCKING finding about a compute field's own
    calculation, when the task's own real goal text explicitly states
    EXACTLY ONE `depends on relation.subfield` path for that field --
    the goal's own concrete technical spec IS the authoritative scope
    for what the compute must consider; a finding inventing additional
    fields to factor in (however plausible-sounding, e.g. "quantity",
    "discount") contradicts a stated technical spec, not a vague,
    arguable business judgment call. Never fires when the goal states
    more than one dependency path (a genuinely multi-field formula,
    where "is this complete" is a real, legitimate question) or none at
    all.

    Real, general fix (2026-07-26, same investigation -- task 004's own
    resubmission, 4 TIMES in a row): the original version of this
    function only matched specific keyword patterns ("ignores",
    "incomplete", ...), and Code-Review kept inventing new phrasings
    that matched none of them -- "failing to trigger recomputation...
    leading to stale data", "ignoring 'quantity'" (a gerund the exact-
    word "ignores?" pattern never caught), and finally "Financial
    calculation error: sums price_unit instead of line total (price_unit
    * quantity), violating Financial Safety" -- FOUR independently
    live-confirmed false claims about the SAME provably-correct field,
    never converging on a keyword list. Rather than keep chasing
    Code-Review's own creativity, this now matches STRUCTURALLY: any
    blocking finding whose own text names the field itself OR its real,
    goal-stated dependency subfield -- since a goal this narrowly and
    concretely specified (exactly one dependency path stated) leaves no
    legitimate room for a blocking objection about that field's own
    calculation at all.
    """
    field_name_match = _GOAL_NAMED_FIELD_RE.search(contract.goal or "")
    phrase_match = _COMPUTE_DEPENDS_PHRASE_RE.search(contract.goal or "")
    if not field_name_match or not phrase_match:
        return findings
    field_name = field_name_match.group(1)
    depends_paths = _DEPENDS_PATH_RE.findall(phrase_match.group(1))
    if len(depends_paths) != 1:
        return findings  # a genuinely multi-field formula -- "is this complete" is a real question
    relation, subfield = depends_paths[0]

    # Ground-truth guard, same discipline as every sibling filter in
    # this file: only ever downgrade once the REAL generated code is
    # independently confirmed to already correctly implement exactly
    # the goal's own stated single dependency -- never a blind "the
    # goal only names one dependency" assumption alone, which would
    # risk masking a genuine, different bug that happens to also
    # mention this field's name.
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    if not re.search(
        rf"{re.escape(field_name)}\s*=\s*fields\.\w+\(.*?compute\s*=", models_py, re.DOTALL,
    ) or not re.search(
        rf"@api\.depends\(\s*['\"]{re.escape(relation)}\.{re.escape(subfield)}['\"]\s*\)", models_py,
    ):
        return findings  # can't confirm the real code is correct -- never guess, leave untouched

    # Real, confirmed bug found live (2026-07-26, same task, 6th
    # resubmission): "the @api.depends decorator is missing 'line_ids' to
    # track record creation/deletion" -- names the RELATION, not the
    # field or subfield, which the mention check below didn't cover.
    mention_re = re.compile(
        rf"\b(?:{re.escape(field_name)}|{re.escape(subfield)}|{re.escape(relation)})\b", re.IGNORECASE,
    )
    filtered = []
    for f in findings:
        if f.severity == "blocking" and mention_re.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- the task's own goal explicitly states exactly "
                    f"one dependency ({relation}.{subfield}) for this compute field; this finding "
                    f"objects to that field's own calculation despite the goal never stating "
                    f"anything beyond it] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# Matches both the zero-arg Python 3 form (`super().method(`) and the
# explicit old-style form (`super(ClassName, self).method(`) -- a real
# gap found live (2026-07-24, task 020's context-aware full-rewrite
# fallback): a generated override used the explicit form, which the
# zero-arg-only pattern silently missed, so the downstream hallucinated-
# finding filter never even looked at it.
_SUPER_CALL_RE = re.compile(r"super\([^)]*\)\.([a-zA-Z_][a-zA-Z0-9_]*)\(")
_INHERIT_TARGET_RE = re.compile(r"_inherit\s*=\s*\[?\s*['\"]([a-zA-Z_][\w.]*)['\"]")
# Real bug found live (2026-07-24, task 020's 15th resume attempt):
# Code-Review rephrased the SAME unfounded action_accept uncertainty in
# softer, hedged language across successive rounds -- "is not defined"
# became "without verifying the method exists... if the base module
# does not define it" -- dodging the original phrase list entirely
# while making the exact same ungrounded claim. Broadened to also catch
# "does not define"/"without verifying...exists"-shaped hedges, not
# just flat assertions.
_METHOD_MISSING_CLAIM_RE = re.compile(
    r"\b(is not defined|does not exist|does not define|isn.t defined|isn.t implemented|"
    r"not implemented|has no attribute|missing (?:from|in) the base model|"
    r"without verifying (?:the method|it) exists|if (?:it|the (?:base|parent) (?:model|module)) "
    r"(?:doesn.t|does not) (?:define|exist|have))\b",
    re.IGNORECASE,
)
# The genuinely standard ORM/mixin methods every Odoo model has --
# never worth a grep round-trip, and excluding them keeps the
# extraction focused on the custom action_*/button_*-shaped names an
# LLM actually gets wrong.
_STANDARD_ORM_METHODS = {
    "create", "write", "unlink", "read", "search", "search_read", "search_count",
    "copy", "name_get", "name_search", "browse", "exists", "fields_get",
    "default_get", "message_post", "_message_post_after_hook",
    "action_archive", "action_unarchive", "toggle_active",
}
_FAST_PATH_DB_ENV = "OMA_ODOO_DB_DUPLICATE_FOR_BUILD"


def _filter_hallucinated_method_missing_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-07-24, Group C re-run) --
    sibling to `_filter_hallucinated_scope_findings` above, same
    philosophy: don't trust an LLM's unverified claim when it's cheap
    to check deterministically. Confirmed live on task 020
    (`project.fieldjob`): Code-Review blocked a valid
    `super().action_accept()` override for 3 straight rounds insisting
    "action_accept is not defined in the base model project.fieldjob",
    when a direct grep of the real source
    (`/opt/site/site16/project_fieldjob/models/project_fieldjob.py:121`)
    shows the method plainly exists -- Code-Review has no tool access
    at all, so this was pure, ungrounded guessing, and it happened to
    guess wrong. Unlike the scope-hallucination case, this can ALSO
    happen in the other direction (Build calling a genuinely
    nonexistent method) -- when that's true, `check_real_module_defines_method`
    returns False and the finding is trusted and left exactly as the
    reviewer wrote it.

    Only ever downgrades a finding when the real method DOES exist and
    the finding's own text claims it doesn't -- every other combination
    (method genuinely missing, lookup inconclusive, no `super()` calls
    to check at all) leaves `findings` completely untouched.
    """
    if os.environ.get("OMA_SKIP_METHOD_EXISTENCE_CHECK"):
        return findings  # test-only escape hatch; unset in real operation

    # Real bug found live (2026-07-24, task 020's 11th resume attempt):
    # the loose "/models/" substring check also matches
    # "models/__init__.py" (near-empty, no super() calls at all) -- and
    # since dict order follows read_module_files()'s own `find` output,
    # that file sorted BEFORE the real "models/models.py" and next()
    # silently picked it instead, so this filter had nothing to check
    # against and the hallucinated finding kept recurring even with this
    # filter correctly deployed and independently verified working.
    # endswith("models.py") alone is sufficient and unambiguous (matches
    # "models/models.py", never "models/__init__.py").
    models_py = next(
        (content for path, content in files.items() if path.endswith("models.py")),
        "",
    )
    if not models_py:
        return findings
    super_calls = {m for m in _SUPER_CALL_RE.findall(models_py) if m not in _STANDARD_ORM_METHODS}
    inherit_targets = _INHERIT_TARGET_RE.findall(models_py)
    if not super_calls or not inherit_targets:
        return findings

    db = os.environ.get(_FAST_PATH_DB_ENV, "")
    if not db:
        return findings

    try:
        from tools_odoo.odoo_schema_client import is_fast_path_eligible, resolve_model_owner_module_fast
        from tools_odoo.odoo_source_introspection import check_real_module_defines_method
    except ImportError:
        return findings
    if not is_fast_path_eligible(db):
        return findings

    verified_exists: set[str] = set()
    for target_model in inherit_targets:
        owner_module = resolve_model_owner_module_fast(target_model, db)
        if not owner_module:
            continue
        for method_name in super_calls:
            if method_name in verified_exists:
                continue
            if check_real_module_defines_method(owner_module, method_name) is True:
                verified_exists.add(method_name)

    if not verified_exists:
        return findings

    filtered = []
    for f in findings:
        mentioned = {m for m in verified_exists if m in f.explanation}
        if f.severity == "blocking" and mentioned and _METHOD_MISSING_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified via a real source grep that "
                    f"{'/'.join(sorted(mentioned))} DOES exist on the real base model; "
                    f"this claim was false] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# Matches `ref="module.xmlid"` (and `ref='module.xmlid'`), the standard
# Odoo cross-module external-id reference syntax in XML data/view files.
_XML_REF_RE = re.compile(r"""\bref=["']([\w]+\.[\w]+)["']""")
_XMLID_UNCERTAIN_CLAIM_RE = re.compile(
    r"\b(must exist|does not exist|doesn.t exist|if it doesn.t|"
    r"will fail to load|external id|referenced.{0,40}does not exist)\b",
    re.IGNORECASE,
)


def _extract_csv_model_ids(files: dict[str, str], module_name: str) -> set[str]:
    """Real, general, deterministic guard (2026-08-06, fix-pass task 024) -- sibling gap to
    `_filter_hallucinated_xmlid_missing_findings()`'s own `ref="module.xmlid"` XML-only scan:
    that filter never looked at `security/ir.model.access.csv`'s own `model_id:id` column, a
    completely different real Odoo reference syntax for the exact same underlying concept
    (referencing a model's own external id). Confirmed live on task 024: Code-Review claimed
    "model_id:id='model_waste_container' does not resolve to a real external id in the live
    registry, causing module installation failure" -- directly contradicted by this exact same
    round's own confirmed successful install (a model_id that genuinely doesn't resolve crashes
    Odoo's own registry-build at install time, exactly like the sibling `_inherit`-target-missing
    hallucination class already fixed for task 024 in an earlier session -- this is the same
    task, a different real reference syntax, previously flagged but not yet fixed).

    Returns every `model_id:id` value found in any `.csv` file, normalized to its fully-qualified
    form (`module_name.model_x` when the CSV value is bare, matching standard Odoo external-id
    resolution: a model's own auto-generated `model_<name>` id is addressable both bare within
    its own module and fully-qualified from anywhere).
    """
    model_ids: set[str] = set()
    for path, content in files.items():
        if not path.endswith(".csv"):
            continue
        try:
            rows = list(csv.DictReader(content.splitlines()))
        except csv.Error:
            continue
        for row in rows:
            value = (row.get("model_id:id") or "").strip()
            if not value:
                continue
            model_ids.add(value if "." in value else f"{module_name}.{value}")
    return model_ids


def _filter_hallucinated_xmlid_missing_findings(
    findings: list[ReviewFinding], files: dict[str, str], module_name: str = "",
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-07-24, Group C re-run) --
    third sibling to `_filter_hallucinated_scope_findings` and
    `_filter_hallucinated_method_missing_findings`, same philosophy.
    Confirmed live on task 020: Code-Review blocked a round with a
    HEDGED, unverified claim -- "The model_id ref project_fieldjob.
    model_project_fieldjob must exist in the base module
    project_fieldjob; if it doesn't, this will fail to load, causing
    install failure" -- for TWO real, genuinely-existing xmlids
    (`project_fieldjob.model_project_fieldjob`,
    `project_fieldjob.view_project_fieldjob_form`, both confirmed live
    via a direct grep of the real base module's own source). Code-Review
    has no tool access at all, so a hedged "if it doesn't exist" claim
    is exactly as ungrounded as a flat hallucinated assertion -- it just
    reads more cautious. `resolve_xmlids_exist_fast()` (already proven
    this session, e.g. by `_autofix_xml_ref_wrong_sibling_module`) gives
    a real, deterministic answer via a direct `ir.model.data` lookup.

    Only ever downgrades a finding when a referenced xmlid IS confirmed
    to exist and the finding's own text uses "might not exist"-shaped
    language about it -- a genuinely missing xmlid, or any lookup that
    comes back inconclusive, leaves the finding completely untouched.

    Widened (2026-08-06, fix-pass task 024) to ALSO check security_csv's own `model_id:id`
    column values (see `_extract_csv_model_ids()`'s own docstring for the full incident) --
    same downgrade discipline, just a second reference-syntax source feeding the same
    live-existence check below.
    """
    if os.environ.get("OMA_SKIP_XMLID_EXISTENCE_CHECK"):
        return findings  # test-only escape hatch; unset in real operation

    xml_content = "\n".join(content for path, content in files.items() if path.endswith(".xml"))
    referenced_xmlids = set(_XML_REF_RE.findall(xml_content)) if xml_content else set()
    referenced_xmlids |= _extract_csv_model_ids(files, module_name)
    referenced_xmlids = sorted(referenced_xmlids)
    if not referenced_xmlids:
        return findings

    db = os.environ.get(_FAST_PATH_DB_ENV, "")
    if not db:
        return findings

    try:
        from tools_odoo.odoo_schema_client import is_fast_path_eligible, resolve_xmlids_exist_fast
    except ImportError:
        return findings
    if not is_fast_path_eligible(db):
        return findings

    existence = resolve_xmlids_exist_fast(referenced_xmlids, db)
    if not existence:
        return findings
    verified_exists = {xmlid for xmlid, exists in existence.items() if exists}
    if not verified_exists:
        return findings

    filtered = []
    for f in findings:
        # A finding may quote either the fully-qualified id or (per Odoo's own bare-within-
        # module convention, and matching how a CSV's own model_id:id column is usually
        # written) just the bare suffix after the last '.' -- check both forms.
        mentioned = {
            x for x in verified_exists
            if x in f.explanation or x.rsplit(".", 1)[-1] in f.explanation
        }
        if f.severity == "blocking" and mentioned and _XMLID_UNCERTAIN_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified via a real ir.model.data lookup that "
                    f"{'/'.join(sorted(mentioned))} DOES exist; this uncertainty was unfounded] "
                    f"{f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# Matches `object.<field>` / `record.<field>` -- the standard Odoo
# mail-template Jinja/QWeb convention for referencing the record being
# rendered (`object` in a mail.template's subject/body, `record` in some
# QWeb report contexts). Deliberately narrow to these two specific,
# well-known binding names -- a generic `\.\w+\.\w+` would re-catch
# unrelated dotted access already handled (or deliberately not handled)
# elsewhere.
# Mirrors specialists/build/specialist.py's own _MODEL_FIELD_ASSIGN_RE and specialists/
# testing_qa/specialist.py's own copy exactly -- duplicated here rather than imported, same
# cross-specialist-boundary discipline already established for _NOT_YET_IN_SCOPE_RE elsewhere.
_MODEL_FIELD_ASSIGN_RE = re.compile(r"^\s{4,8}(\w+)\s*=\s*fields\.\w+\(", re.MULTILINE)
_TEMPLATE_FIELD_REF_RE = re.compile(r"\b(?:object|record)\.(\w+)\b")
# `<field name="X">` inside a view's own `<arch>` block -- the OTHER
# real Odoo field-reference syntax, distinct from the Jinja/QWeb
# `object.<field>` convention above. Real bug found live (2026-07-24,
# task 020's 20th resume attempt): once the Jinja-syntax invented field
# was finally fixed, Code-Review immediately hallucinated that the
# REAL fields `date_finish`/`amount_total` "are not defined... nor are
# they part of the base project.fieldjob model" -- referenced via
# `<field name="date_finish">` in views_xml, a syntax this filter never
# looked at (it only ever scanned `object.<field>`/`record.<field>`).
_ARCH_BLOCK_RE = re.compile(r'<field\s+name="arch"[^>]*>(.*?)</field>', re.DOTALL)
_VIEW_FIELD_NAME_RE = re.compile(r'<field\s+name="(\w+)"')
_FIELD_UNCERTAIN_CLAIM_RE = re.compile(
    r"\b(may not have|does not have|doesn.t have|no such field|not a (?:real |valid )?field|"
    r"field .{0,10}(?:does not|doesn.t) exist|not defined|not part of the base|"
    r"nor (?:is|are) (?:it|they) part of|"
    # Real, general fix (2026-07-26, Phase 25D, task 004): a fourth,
    # live-observed wording of the same underlying claim -- "'currency_id'
    # is not declared in this model or inherited from 'project.fieldjob'"
    # -- naming neither "does not exist" nor "not defined" verbatim.
    r"is not declared (?:in|on) (?:this|the) model|"
    # Real, general fix (2026-08-03, full-30-task sweep): a fifth, live-observed family of
    # phrasings, all hit identically on 5 separate real tasks the same day -- "does not define
    # the 'X' field", "defines no fields", "never defined in the model", "field is missing from
    # the ... model definition" -- none matching any alternative above, the exact same
    # "new phrasing dodges the regex" failure this filter family has hit before (see the
    # empty-security-csv filter's own multi-rephrasing history). Root cause this time: the
    # collision-driven case where a field is CORRECTLY, deliberately absent from this round's own
    # diff because it already exists for real on the target model (Build's own
    # `_validate_no_new_field_collides_with_real_target_field` autofix) -- Code-Review has no way
    # to know that, and confidently (not hedgingly) reports the field as missing.
    r"does not define|defines no (?:new )?fields|never defined (?:in|on)|field is missing from|"
    # Real, general fix (2026-08-03, task004): a sixth phrasing, same underlying collision-driven
    # false positive, same day -- "leaving the view's reference to 'X' undefined" -- neither
    # "does not define" nor "defines no fields" (note: the 'new' inserted between "no" and
    # "fields" already required broadening the prior alternative above; this is a genuinely
    # different phrase, not just a variant of it).
    r"reference to .{0,40}undefined|"
    # Real, general fix (2026-08-03, task028): a seventh phrasing, same underlying collision-driven
    # false positive, same day -- "references 'X' which does not exist on the model" -- the
    # existing "field .{0,10}(?:does not|doesn't) exist" alternative above requires the literal
    # word "field" within 10 characters of "does not exist"; this phrasing names the field by its
    # quoted identifier instead ("'project_type_ids' which does not exist"), never using the word
    # "field" at all nearby.
    r"which does not exist on (?:this|the) model)\b",
    re.IGNORECASE,
)
# Real, confirmed bug found live (2026-07-26, Phase 25D, task 004's own
# resubmission): Code-Review flagged "'currency_id' is not declared in
# this model or inherited from 'project.fieldjob'... causing a runtime
# error" -- `currency_id` is a real, live field on the actual base
# model (independently confirmed via a direct fields_get() call). The
# reference here is neither `object.<field>` nor `<field name="X">` --
# it's a KWARG VALUE inside a field declaration itself
# (`currency_field='currency_id'`), a third real Odoo field-reference
# syntax this filter never looked at.
_CURRENCY_FIELD_KWARG_REF_RE = re.compile(r"currency_field\s*=\s*['\"](\w+)['\"]")


_MISSING_IMPORT_CLAIM_RE = re.compile(
    r"missing\s+import\s+(?:for\s+)?['\"`]?(?:from\s+odoo\s+import\s+)?(\w+)|"
    r"name\s+['\"`](\w+)['\"`]\s+is\s+not\s+defined|"
    r"NameError.{0,40}?['\"`](\w+)['\"`]",
    re.IGNORECASE,
)


def _filter_hallucinated_missing_import_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    project_ticket_counts node): Code-Review repeatedly claimed "Missing import 'from odoo
    import fields' causes a NameError when running the test" -- confirmed live, directly against
    the real committed `tests/test_project_ticket_counts.py`, that the import WAS already
    present. This exact stale complaint (and its sibling phrasings, e.g. "NameError: name
    'fields' is not defined") resurfaced repeatedly across many separate resumes even after
    being explicitly retracted via note each time -- a real, confirmed instance of
    `resume_task_after_checkpoint()`'s own round-history replay carrying a resolved finding
    forward indefinitely (see that function's own comments; a genuine architectural gap, not
    something this filter can fix at the source, but one this filter CAN neutralize for this
    specific verifiable-fact claim shape the same way its siblings already do for other claims).

    Deliberately narrow and purely factual (never a judgment call, unlike a style/performance
    preference): a "missing import X" / "NameError: name X is not defined" claim about a
    specific importable name is verified against the real file text -- if that exact name
    appears in any `import X` or `from ... import ... X ...` line anywhere in the SAME file the
    finding's own location references (or, if location can't be resolved, anywhere in the
    module's own files), the claim is factually false and downgraded. A finding whose named
    import genuinely is absent is left completely untouched.
    """
    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        match = _MISSING_IMPORT_CLAIM_RE.search(f.explanation)
        if not match:
            filtered.append(f)
            continue
        name = next((g for g in match.groups() if g), None)
        if not name:
            filtered.append(f)
            continue
        target_file = next(
            (content for path, content in files.items() if path in f.location), None,
        )
        search_space = target_file if target_file is not None else "\n".join(files.values())
        import_re = re.compile(
            rf"^\s*(?:import\s+{re.escape(name)}\b|from\s+[\w.]+\s+import\s+[^#\n]*\b{re.escape(name)}\b)",
            re.MULTILINE,
        )
        if import_re.search(search_space):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified against the real file content that "
                    f"{name!r} IS already imported; this claim was factually false, likely stale "
                    f"round-history being replayed] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _get_graph_verified_field_names(technical_name: str) -> set[str]:
    """Phase 36 §4.4 -- the new, second, faster, offline-snapshot source for
    `_filter_hallucinated_direct_field_missing_findings`, alongside (not replacing) the
    existing live `get_model_fields_fast` RPC call, per §4.4's own "two complementary
    sources" design. Calls `tools_odoo.graph_queries.get_field_types_for_model` and returns
    the model's real field names -- or an empty set on ANY failure (no Neo4j configured, the
    driver/module import fails, the graph is mid-import, the model doesn't resolve, or any
    other exception), following the exact same "no data / any exception -> fall through,
    never raise" fail-open discipline every other graph consumer in this document uses. This
    function never blocks or downgrades anything by itself -- it only ever WIDENS the set of
    field names the caller already independently verifies against the live RPC call.
    """
    try:
        from infra.neo4j_client import get_neo4j_read_driver
        from tools_odoo.graph_queries import get_field_types_for_model
    except Exception:
        return set()

    try:
        driver = get_neo4j_read_driver()
        import_in_progress, fields = get_field_types_for_model(driver, technical_name)
    except Exception:
        return set()

    if import_in_progress:
        return set()  # graph mid-import -- don't trust a possibly-incomplete field list
    return {f["name"] for f in fields if f.get("name")}


def _filter_hallucinated_direct_field_missing_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-07-24, Group C re-run) --
    fourth sibling in this same family. Confirmed live on task 020's
    15th resume attempt: Code-Review flagged "The template body
    references object.name, but the model project.fieldjob may not
    have a name field; verify field existence to prevent rendering
    errors" -- `name` is a real, directly-declared field on the actual
    base model (confirmed live: `name = fields.Char(string='Reference',
    readonly=True, copy=False, default='New')` in the real source).
    Unlike the `related=`-target and inherited-view-field checks in
    specialists/build/specialist.py (which validate Build's own OUTPUT
    before it's ever written), this is the Code-Review side: don't
    trust an unverified "may not have this field" claim about a
    template's `object.<field>`/`record.<field>` reference when the
    real, live target model's field list is one cheap RPC call away.

    Only ever downgrades a finding when the referenced field IS
    confirmed real on the actual `_inherit` target (by the graph, the live RPC call, or
    both) and the finding's own text uses "might not have this field"-shaped language about
    it -- a genuinely missing field, or any lookup that comes back
    inconclusive, leaves the finding completely untouched.

    Phase 36 §4.4 (sixth-audit revision, 2026-08-13): this now also consults the Odoo
    Knowledge Graph via `_get_graph_verified_field_names`, BEFORE falling back to (and
    alongside, not instead of) the existing live `get_model_fields_fast` RPC call below --
    the graph is a second, additive source, unioned with whatever the RPC call separately
    returns, closing incident #4's own "pattern-matching over freeform LLM claims, not a
    queryable structural source of truth" root cause with a real second structural source,
    not a replacement for the RPC path (which stays the authoritative, live check).
    """
    if os.environ.get("OMA_SKIP_FIELD_EXISTENCE_CHECK"):
        return findings  # test-only escape hatch; unset in real operation

    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    inherit_target = _INHERIT_TARGET_RE.search(models_py)
    if not inherit_target:
        return findings
    inherit_target = inherit_target.group(1)

    non_models_content = "\n".join(
        content for path, content in files.items() if not path.endswith(".py")
    )
    referenced_fields: set[str] = set(_TEMPLATE_FIELD_REF_RE.findall(non_models_content))
    for path, content in files.items():
        if path.endswith(".xml"):
            for body in _ARCH_BLOCK_RE.findall(content):
                referenced_fields |= set(_VIEW_FIELD_NAME_RE.findall(body))
    referenced_fields |= set(_CURRENCY_FIELD_KWARG_REF_RE.findall(models_py))
    referenced_fields = sorted(referenced_fields)
    if not referenced_fields:
        return findings

    # §4.4: the graph is consulted first -- cheap, offline, always-attempted, and fail-open
    # on its own (never raises, never requires the RPC path's OMA_ODOO_DB_DUPLICATE_FOR_BUILD
    # env var to be set at all).
    real_fields: set[str] = _get_graph_verified_field_names(inherit_target)

    db = os.environ.get(_FAST_PATH_DB_ENV, "")
    if db:
        try:
            from tools_odoo.odoo_schema_client import get_model_fields_fast, is_fast_path_eligible
        except ImportError:
            is_fast_path_eligible = None  # noqa: N806 -- keeps the union path a no-op below
        if is_fast_path_eligible and is_fast_path_eligible(db):
            rpc_fields = get_model_fields_fast(inherit_target, db)
            if rpc_fields is not None:
                real_fields = real_fields | set(rpc_fields)

    if not real_fields:
        return findings  # neither source had a confident real answer -- skip, never guess
    verified_exists = set(referenced_fields) & real_fields
    if not verified_exists:
        return findings

    filtered = []
    for f in findings:
        mentioned = {x for x in verified_exists if x in f.explanation}
        if f.severity == "blocking" and mentioned and _FIELD_UNCERTAIN_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified via a real field-list lookup against "
                    f"{inherit_target!r} that {'/'.join(sorted(mentioned))} DOES exist; this "
                    f"uncertainty was unfounded] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _extract_touched_model_technical_names(files: dict[str, str]) -> list[str]:
    """Phase 36 §4.5 item 2 support: the diff's touched-model technical-name set, derived
    from the same `models.py` content and the same two regexes this file already uses
    elsewhere for a near-identical purpose -- `_INHERIT_TARGET_RE` (an existing model this
    diff extends, findall'd rather than search'd so a module touching more than one
    `_inherit` class in the same round is fully covered, not just the first match) and
    `_REVIEW_NEW_MODEL_NAME_RE` (a brand-new model this diff declares via `_name`). Returns
    a sorted list (stable order for tests and for the annotation loop below); empty when
    `models.py` declares no model at all (e.g. a views/data-only round).
    """
    models_py = "\n".join(content for path, content in files.items() if path.endswith("models.py"))
    touched = set(_INHERIT_TARGET_RE.findall(models_py)) | set(_REVIEW_NEW_MODEL_NAME_RE.findall(models_py))
    return sorted(touched)


def _flag_ungated_access_model_touched(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Phase 36 §4.5 item 2 (seventh-audit revision, 2026-08-13, graduation trigger added
    eighth-audit revision) -- Code-Review's security-posture consumer for the
    `:AccessGroup`/`RESTRICTED_TO`/`has_ungated_access_rule` schema §2.1's sixth-audit
    revision loaded but which, before this revision, had zero operational consumer.

    Given the diff's touched-model set (`_extract_touched_model_technical_names`), calls the
    new `tools_odoo.graph_queries.get_ungated_models` following the exact same
    fixed-parameterized-query, fail-open, single-round-trip discipline as
    `_get_graph_verified_field_names` (§4.4) above -- and, if the graph is healthy and any
    touched model carries `has_ungated_access_rule = true`, APPENDS one new, clearly-labeled
    `info`-severity `ReviewFinding` per such model to the existing findings list.

    Deliberately additive, never mutating an existing finding: unlike every
    `_filter_hallucinated_*_findings` sibling in this file (which only ever downgrades a
    finding the LLM already produced), this is new information with no corresponding
    incident to calibrate a blocking threshold against, so it stays a pure annotation --
    `severity="info"` never contributes to `_run_diff_review`'s own
    `blocking = [f for f in result.findings if f.severity == "blocking"]` pass/fail gate.
    Per §4.5 item 2's stated graduation trigger (a second logged incident, or the 60-day
    report showing >=5 clean fires), this stays informational-only in this revision -- the
    trigger is not yet met, so this function does not, and must not, downgrade any existing
    finding or itself return anything but `severity="info"`.

    Any failure (no Neo4j configured, import error, connectivity error, mid-import graph) is
    fail-open -- the diff review proceeds with its findings completely unannotated, exactly
    like every other graph consumer in this file degrades on an unhealthy graph.
    """
    if os.environ.get("OMA_SKIP_UNGATED_ACCESS_CHECK"):
        return findings  # test-only escape hatch; unset in real operation

    touched_models = _extract_touched_model_technical_names(files)
    if not touched_models:
        return findings

    try:
        from infra.neo4j_client import get_neo4j_read_driver
        from tools_odoo.graph_queries import get_ungated_models
        driver = get_neo4j_read_driver()
        import_in_progress, ungated = get_ungated_models(driver, touched_models)
    except Exception:
        return findings  # fail-open -- never block/annotate on a guess about graph state

    if import_in_progress or not ungated:
        return findings

    annotations = [
        ReviewFinding(
            location=f"models.py ({model})",
            severity="info",
            explanation=(
                f"This diff touches `{model}`, which has at least one ungated (no "
                f"security-group restriction) access rule per the last knowledge-graph "
                f"extraction -- verify this is intentional."
            ),
        )
        for model in sorted(ungated)
    ]
    return list(findings) + annotations


_DUPLICATE_FIELD_CLAIM_RE = re.compile(
    r"declared\s+twice|declared\s+more\s+than\s+once|duplicate\s+field|"
    r"redeclared\s+in\s+the\s+same\s+class|silent(?:ly)?\s+shadow",
    re.IGNORECASE,
)
_DUPLICATE_FIELD_CLAIM_NAME_RE = re.compile(r"[fF]ield\s+'(\w+)'|['\"](\w+)['\"]\s+is\s+declared")
# Mirrors specialists/build/specialist.py's own _MODEL_FIELD_DEF_RE fix (2026-08-09, task
# 07141af5's flagship run): a genuine field constructor call is always `fields.TypeName(` with
# the opening paren IMMEDIATELY after the type name -- a plain method-body local variable using
# a class-level date/time utility (`fields.Datetime.now()`, `fields.Date.today()`) has a SECOND
# dotted attribute access before any paren and must never be counted as a field declaration.
_CODE_REVIEW_MODEL_FIELD_DEF_RE = re.compile(r"^\s*(\w+)\s*=\s*fields\.\w+\(", re.MULTILINE)


def _filter_hallucinated_duplicate_field_declaration_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    project_ticket_counts node): Code-Review claimed "Field 'cutoff' is declared twice in the
    same class, causing a silent shadowing error" -- the EXACT SAME false-positive shape already
    root-caused and fixed on the Build side (`_MODEL_FIELD_DEF_RE`'s own fix, see that function's
    docstring in specialists/build/specialist.py): `cutoff = fields.Datetime.now() - ...` is a
    completely legitimate, common Odoo idiom for reading the current date/time inside a method
    body, not a field declaration at all -- and here it appeared in TWO DIFFERENT methods
    (`_compute_ticket_counts` and `action_overdue_tickets`), each with its own independent local
    variable, never colliding with anything. Confirmed live via direct inspection of the real
    committed models.py: no field is actually declared twice anywhere in the file. Code-Review is
    an independent LLM judgment, not the same regex Build's own validator uses, so fixing the
    Build-side regex alone (already done) does not stop Code-Review from independently
    hallucinating the identical claim -- this is the Code-Review-side sibling of that same fix,
    same family/philosophy as every other `_filter_hallucinated_*_findings` function in this
    file: verify a specific, checkable factual claim against the real files before trusting it.

    Deliberately narrow and conservative: only ever downgrades a finding that (a) uses
    "declared twice"/"duplicate field"/"silently shadow"-shaped language AND (b) names a
    specific identifier that this file's own field-declaration regex confirms is NOT actually
    declared more than once anywhere in the real models.py. A finding naming an identifier that
    genuinely IS declared twice (a real bug) is left completely untouched.
    """
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    if not models_py:
        return findings

    counts: dict[str, int] = {}
    for name in _CODE_REVIEW_MODEL_FIELD_DEF_RE.findall(models_py):
        counts[name] = counts.get(name, 0) + 1
    genuinely_duplicated = {name for name, n in counts.items() if n > 1}

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _DUPLICATE_FIELD_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        named = {
            g for m in _DUPLICATE_FIELD_CLAIM_NAME_RE.finditer(f.explanation)
            for g in m.groups() if g
        }
        if named and not (named & genuinely_duplicated):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified against the real models.py that "
                    f"{'/'.join(sorted(named))} is never actually declared as a field more than "
                    f"once anywhere in this class; this was a plain method-body local variable, "
                    f"not a field declaration] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_ENSURE_ONE_LIST_VIEW_CLAIM_NAMED_RE = re.compile(
    r"\b(\w+)\s+uses?\s+`?(?:self\.)?(?:ensure_one\(\)|id\b)|"
    r"method\s+`(\w+)`|"
    r"`(\w+)`\s+uses?",
    re.IGNORECASE,
)
_ENSURE_ONE_LIST_VIEW_CLAIM_SHAPE_RE = re.compile(
    r"(?:ensure_one\(\)|self\.id\b).{0,100}?(?:list\s+view|multiple\s+records?|multi-select|bulk)|"
    r"(?:list\s+view|multiple\s+records?|multi-select|bulk).{0,100}?(?:ensure_one\(\)|self\.id\b)",
    re.IGNORECASE,
)


def _filter_hallucinated_ensure_one_list_view_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-10, task 07141af5's flagship run,
    project_ticket_counts node, resumes r189 and r191): Code-Review repeatedly claimed
    "action_open_tickets uses self.ensure_one() which will fail when called from list view with
    multiple records selected" (and the same claim for action_overdue_tickets, and a generic
    "Action methods use ensure_one()..." variant) -- confirmed FALSE both times by directly
    reading the real, currently-committed views/views.xml: both methods are wired ONLY as
    type="object" smart buttons inside project.project's own FORM view button_box
    (view_project_form_inherit, inheriting project.edit_project's <div name="button_box">) --
    there is no <tree>/<list> arch anywhere in this module that references either method name.
    A form view's button_box smart button always operates on exactly the form's own single
    current record; ensure_one() is the correct, standard Odoo idiom there, not a bug. This is a
    DIFFERENT node's concern entirely -- multi-select bulk actions belong to the separate
    'ticket_bulk_close' constraint on oma.service.ticket, never project.project's smart buttons --
    and Code-Review appears to be confusing the two shapes. Retracted via note the first time
    (r190), passed cleanly, then the identical claim resurfaced on the very next round (r191) --
    2+ recurrence of the same claim shape, following this project's own standing rule that
    disproves "one-off" and demands a general pipeline fix, same family as every other
    `_filter_hallucinated_*_findings` function in this file.

    Deliberately verifiable and narrow: only downgrades a finding matching the "ensure_one() +
    list view / multiple records / bulk" claim shape, and only when the specific method name
    named in the claim (if any) is confirmed, via real `xml.etree.ElementTree` parsing of every
    `.xml` file in the module, to NEVER appear as a button's `name=` attribute inside any
    `<tree>`/`<list>` element's own subtree -- i.e. it is only ever invoked from a form view (or
    not referenced by any view button at all). A method that genuinely IS wired to a list/tree
    view button (a real, would-be-broken bulk action) is left completely untouched. When the
    claim is fully generic (no specific method named), the same check is applied to every method
    name mentioned anywhere in the finding's own location/explanation that calls ensure_one() in
    the real models.py -- if none of them are ever list/tree-view-wired, the generic claim is
    downgraded too.
    """
    import xml.etree.ElementTree as ET

    xml_files = {path: content for path, content in files.items() if path.endswith(".xml")}
    list_view_button_names: set[str] = set()
    for content in xml_files.values():
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            continue
        for list_tag in ("tree", "list"):
            for list_el in root.iter(list_tag):
                for button in list_el.iter("button"):
                    name = button.get("name")
                    if name:
                        list_view_button_names.add(name)

    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    # Methods that assume a single record (via self.ensure_one() or a bare self.id read) --
    # the real, checkable population a "fails on multiple records" claim could legitimately be
    # about. Body-scoped (each def's own text up to the next def) so an unrelated method
    # elsewhere in the file that happens to call ensure_one() never gets swept in.
    single_record_assumption_methods: set[str] = set()
    if models_py:
        def_matches = list(_METHOD_DEF_NAME_RE.finditer(models_py))
        for i, m in enumerate(def_matches):
            body_end = def_matches[i + 1].start() if i + 1 < len(def_matches) else len(models_py)
            body = models_py[m.end():body_end]
            if "ensure_one()" in body or "self.id" in body:
                single_record_assumption_methods.add(m.group(1))

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _ENSURE_ONE_LIST_VIEW_CLAIM_SHAPE_RE.search(f.explanation):
            filtered.append(f)
            continue
        raw_named = {
            g for m in _ENSURE_ONE_LIST_VIEW_CLAIM_NAMED_RE.finditer(f.explanation)
            for g in m.groups() if g
        }
        # Only trust a capture that is a REAL single-record-assumption method in this module
        # (drops false captures like "methods" from "Action methods use ensure_one()"); when
        # nothing genuine was named, treat the claim as generic and check every such method.
        named = {n for n in raw_named if n in single_record_assumption_methods} or single_record_assumption_methods
        if named and not (named & list_view_button_names):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified against the real views/*.xml (parsed "
                    f"as XML) that {'/'.join(sorted(named))} is never referenced by a button "
                    f"inside any <tree>/<list> view element anywhere in this module; it is only "
                    f"ever wired as a form-view smart button, which always operates on a single "
                    f"record -- ensure_one() is correct there, not a bug] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_ALREADY_EXISTS_IN_SCHEMA_CLAIM_RE = re.compile(
    r"\balready (?:exists?|defined|declared)\b.{0,60}\b(?:schema|current_schema|base model)\b|"
    r"\b(?:schema|current_schema|base model)\b.{0,60}\balready (?:exists?|defined|declared)\b|"
    r"\bredeclar\w*\b.{0,60}\balready exists\b",
    re.IGNORECASE | re.DOTALL,
)


def _filter_hallucinated_already_exists_in_schema_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-08-06, fix-pass tasks 018/019) -- fifth sibling
    in this same family, the mirror-opposite shape from `_filter_hallucinated_direct_field_
    missing_findings` above: instead of falsely claiming a real field is MISSING, this catches
    Code-Review falsely claiming a field the round's OWN diff just added is a "duplicate"
    already present in the schema.

    Root cause (confirmed live, both incidents): `resolve_current_schema_block()` (this file's
    own `<current_schema>` prompt block, see its call site above) does a LIVE database read,
    called from `_review()` AFTER the round's own module has already been installed --
    `_run_diff_review()`'s own call sequence only ever reaches Code-Review once Build's install
    already succeeded. This means `<current_schema>` ALWAYS reflects the round's own just-added
    field(s) as already being part of the live schema -- correct and expected in the overwhelming
    majority of rounds (Code-Review understands "this is the field I'm reviewing"), but on task
    018 ("Redeclaring 'date_sent' field that already exists in the base model schema...") and
    task 019 ("The field 'customer_grouping_rule' is already defined in the provided
    <current_schema>...") Code-Review misread its own post-install snapshot as proof of a
    PRE-EXISTING duplicate, blocking two genuinely correct rounds. Independently confirmed for
    task 018: the field no longer existed on the live schema at all once the round's own natural
    rollback ran -- proving it was never a real pre-existing collision, purely this round's own
    (correct) addition.

    Deliberately conservative, same "exactly one candidate, no guessing" discipline as
    `_autocorrect_hallucinated_reproduction_target()`'s own newly-defined-field tier
    (specialists/testing_qa/specialist.py): only downgrades when the finding names EXACTLY ONE
    field, and that exact field is genuinely declared via a `fields.<Type>(...)` assignment in
    THIS round's own real models.py -- proving Build itself just added it this round, not a
    genuinely different, real, pre-existing collision the diff coincidentally shares a name with.
    """
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    if not models_py:
        return findings
    newly_declared_fields = set(_MODEL_FIELD_ASSIGN_RE.findall(models_py))
    if not newly_declared_fields:
        return findings

    filtered = []
    for f in findings:
        mentioned = {name for name in newly_declared_fields if name in f.explanation}
        if (
            f.severity == "blocking" and len(mentioned) == 1
            and _ALREADY_EXISTS_IN_SCHEMA_CLAIM_RE.search(f.explanation)
        ):
            field_name = next(iter(mentioned))
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {field_name!r} is genuinely declared as a NEW "
                    f"field in this exact round's own models.py; a post-install schema read always "
                    f"shows this round's own just-added field as 'already in the schema', which is "
                    f"expected, not evidence of a real pre-existing duplicate] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# 50-task deep-dive (docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md, P3 item 7): real,
# confirmed live investigation, task_id db3a160d-cd78-4849-9339-bd66eaf2cdf8 (Task 024, "waste
# container tracking"). The report's own original recommendation assumed this was either a
# validator-wiring gap or a validator registry-lookup bug (`_validate_inherit_target_resolved`,
# specialist.py line 7966) -- pulling the REAL trace data instead shows neither: BOTH of this
# task's rounds show "Scaffolded, wrote, and linted module '...'; install succeeded" and "Round N's
# own install succeeded but the round failed verification." A model that genuinely does not exist
# crashes Odoo's own registry-build at install time -- install cannot succeed twice against a truly
# nonexistent `_inherit` target. `_validate_inherit_target_resolved` was correct to silently pass
# (its own live-registry lookup genuinely resolved `waste.container` as real) -- there is no
# validator gap here at all. The actual, real defect is Code-Review itself hallucinating: "the
# model `waste.container` does not exist in standard Odoo or the provided dependencies, causing a
# crash on module load" is factually contradicted by this exact same round's own confirmed
# successful install. Code-Review has no live tool access (confirmed elsewhere in this same file)
# and reasons only from its own general training knowledge of "standard Odoo," blind to
# SITE-specific or previously-installed custom models genuinely present on this live target.
_INHERIT_TARGET_NONEXISTENT_CLAIM_RE = re.compile(
    r"model\s+`?['\"]?([a-zA-Z_][\w.]*)['\"]?`?\s+does\s+not\s+exist", re.IGNORECASE,
)
# Real, confirmed bug found live (2026-08-06, Phase 30 root-cause pass, task034): a DIFFERENT
# rephrasing of the same false claim, never naming a specific model at all --
# "Models use _inherit instead of _name, inheriting from non-existent standard models instead of
# defining new custom models." Confirmed live: BOTH real `_inherit` targets in this exact
# generation (`payment.term.cust`, `payment.term.cust.line`) genuinely, currently resolve (live
# registry lookup, real fields), and this SAME round's own install already succeeded against them
# -- directly contradicting "non-existent". `_INHERIT_TARGET_NONEXISTENT_CLAIM_RE` above requires
# a specific named model in "model X does not exist" shape and never matches this generic,
# unnamed phrasing. Deliberately narrow: only matches when the finding explicitly pairs
# `_inherit`/`inherit` wording with a "non-existent"/"doesn't exist"/"don't exist" claim about
# what's being inherited -- never a generic, unrelated `_inherit` mention.
_INHERIT_TARGETS_GENERICALLY_NONEXISTENT_CLAIM_RE = re.compile(
    r"(?=.{0,120}\binherit)(?=.{0,120}(?:non-?existent|doesn.t exist|don.t exist|do not exist|does not exist))",
    re.IGNORECASE | re.DOTALL,
)


def _filter_hallucinated_inherit_target_missing_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Downgrades a "`_inherit` target model X does not exist" claim when a real, live
    registry lookup against X (the SAME `get_model_fields_fast` mechanism Build's own
    `_validate_inherit_target_resolved` already uses) confirms it genuinely does resolve.
    Deliberately narrow: only ever fires when the claimed nonexistent model name matches this
    exact generation's own real `_inherit` target (never a different, unrelated model the finding
    might legitimately be naming), and only when the live lookup returns a confident, real
    answer -- any lookup failure/uncertainty leaves the finding completely untouched, never
    guessed at.
    """
    if os.environ.get("OMA_SKIP_FIELD_EXISTENCE_CHECK"):
        return findings  # test-only escape hatch; unset in real operation

    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    inherit_targets = _INHERIT_TARGET_RE.findall(models_py)
    inherit_match = _INHERIT_TARGET_RE.search(models_py)
    if not inherit_match:
        return findings
    inherit_target = inherit_match.group(1)

    db = os.environ.get(_FAST_PATH_DB_ENV, "")
    if not db:
        return findings
    try:
        from tools_odoo.odoo_schema_client import get_model_fields_fast, is_fast_path_eligible
    except ImportError:
        return findings
    if not is_fast_path_eligible(db):
        return findings

    real_fields = get_model_fields_fast(inherit_target, db)
    if real_fields is None:
        return findings  # couldn't get a confident real answer -- skip, never guess

    # Real, confirmed bug found live (2026-08-06, task034): a genuinely correct multi-class
    # generation (this round's own real shape: 2 classes, each `_inherit`ing a DIFFERENT real,
    # currently-existing model) needs EVERY one of its own `_inherit` targets confirmed live, not
    # just the first one this file happens to match -- a false claim naming no specific model at
    # all ("inheriting from non-existent standard models") is only safe to downgrade once all of
    # them are checked. Never guesses: any target whose live lookup fails/is uncertain blocks the
    # whole downgrade for this generic claim shape.
    all_targets_confirmed_real = bool(inherit_targets) and all(
        get_model_fields_fast(t, db) is not None for t in set(inherit_targets)
    )

    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        claim_match = _INHERIT_TARGET_NONEXISTENT_CLAIM_RE.search(f.explanation)
        claimed_model = claim_match.group(1) if claim_match else None
        if claimed_model and claimed_model == inherit_target:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified via a real live-registry lookup that "
                    f"{inherit_target!r} genuinely resolves as a real model (has real fields), "
                    f"directly contradicting this claim that it does not exist; this same round's "
                    f"own install also already succeeded against it] {f.explanation}"
                ),
            }))
        elif all_targets_confirmed_real and _INHERIT_TARGETS_GENERICALLY_NONEXISTENT_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified via real live-registry lookups that "
                    f"every one of this generation's own real `_inherit` targets "
                    f"({sorted(set(inherit_targets))!r}) genuinely resolves (has real fields), "
                    f"directly contradicting this claim they're non-existent; this same round's "
                    f"own install also already succeeded against them] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# Real bug found live (2026-08-06, task042 of the SITE 50-task fix-pass): Code-Review claimed
# "the previous attempt error indicates the base class 'project.fieldjob' itself was missing
# _name/_inherit, causing registry build failure; this diff assumes the base model is fixed, but
# if the base model is still broken, this module will fail to load" -- a genuine hallucination,
# confirmed via direct SSH read of the REAL, live, always-stable project_fieldjob module
# (`_name = 'project.fieldjob'` has been correct there the whole session). Code-Review conflated
# an EARLIER ROUND's own separate, never-installed, failed scaffold (a genuinely different module
# that round 1 itself cleaned up on failure) with the real, live `_inherit` target this round
# extends -- a round-failure's own transient in-memory defect never touches the real base model at
# all. This escalated the whole task to ask_operator on ZERO other blocking findings -- a pure
# hallucination cost a full task. Downgraded the same way every other hallucination filter in this
# file works: only when a live registry lookup against the SAME model this round's own
# `_inherit` targets confirms it genuinely, currently resolves.
_PRIOR_ROUND_BASE_MODEL_BROKEN_CLAIM_RE = re.compile(
    r"\b(?:previous|prior)\s+(?:attempt|round)\b", re.IGNORECASE,
)
_PRIOR_ROUND_BASE_MODEL_BROKEN_SIGNAL_RE = re.compile(
    r"_name|_inherit|registry[\s-]build|registry build", re.IGNORECASE,
)


def _filter_hallucinated_prior_round_base_model_broken_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Downgrades a "the base/inherited model was broken in a previous attempt/round" claim when
    a real, live registry lookup against this round's own `_inherit` target (the same
    `get_model_fields_fast` mechanism the sibling `_filter_hallucinated_inherit_target_missing_findings`
    already uses) confirms it genuinely, currently resolves -- proof the base model was never
    actually broken, only a DIFFERENT, unrelated, already-cleaned-up prior round's own scaffold
    was. Deliberately narrow: only fires on a finding that (a) is blocking, (b) explicitly invokes
    "previous attempt"/"prior round" phrasing, (c) also mentions _name/_inherit/registry-build
    (the specific failure-mode vocabulary this hallucination class uses), and (d) names this
    round's own real `_inherit` target model. Any live-lookup failure/uncertainty leaves the
    finding completely untouched, never guessed at.
    """
    if os.environ.get("OMA_SKIP_FIELD_EXISTENCE_CHECK"):
        return findings  # test-only escape hatch; unset in real operation

    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    inherit_match = _INHERIT_TARGET_RE.search(models_py)
    if not inherit_match:
        return findings
    inherit_target = inherit_match.group(1)

    db = os.environ.get(_FAST_PATH_DB_ENV, "")
    if not db:
        return findings
    try:
        from tools_odoo.odoo_schema_client import get_model_fields_fast, is_fast_path_eligible
    except ImportError:
        return findings
    if not is_fast_path_eligible(db):
        return findings

    real_fields = get_model_fields_fast(inherit_target, db)
    if real_fields is None:
        return findings  # couldn't get a confident real answer -- skip, never guess

    filtered = []
    for f in findings:
        is_candidate = (
            f.severity == "blocking"
            and inherit_target in f.explanation
            and _PRIOR_ROUND_BASE_MODEL_BROKEN_CLAIM_RE.search(f.explanation)
            and _PRIOR_ROUND_BASE_MODEL_BROKEN_SIGNAL_RE.search(f.explanation)
        )
        if is_candidate:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified via a real live-registry lookup that "
                    f"{inherit_target!r} genuinely resolves as a real model (has real fields) "
                    f"right now, directly contradicting this claim that a prior attempt left it "
                    f"broken; a failed round's own transient scaffold never touches the real base "
                    f"model] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_EMPTY_SECURITY_CSV_CLAIM_RE = re.compile(
    r"access control (?:file|list|csv)?.{0,15}(?:is empty|is blank)|"
    r"(?:security|access).{0,20}(?:file|csv).{0,15}empty|"
    r"no access rights|without proper access|missing access rights|"
    # Real, confirmed bug found live (2026-07-28, Phase 28C,
    # school_student task): Code-Review rephrased this same claim
    # avoiding the word "empty" entirely ("contains only a header
    # row"), matching none of the alternatives above -- the same
    # "invents new phrasing that dodges the regex" failure mode this
    # whole filter family has hit before (see the sequence-pattern
    # filter's own historical note). Broadened, not narrowed further.
    r"(?:contains? only|only contains?|nothing but) (?:a |the )?header|"
    # Real, confirmed THIRD rephrasing found live the same session,
    # same task, a later round ("File is empty; Odoo requires at least
    # one access control row for the model to install successfully")
    # -- order-independent proximity match instead of one more
    # exact-phrase alternative, since a fourth rephrasing is entirely
    # plausible and each new alternative only ever covers the ONE
    # wording already observed.
    r"(?=.{0,80}\bempty\b)(?=.{0,80}\baccess (?:control|right))|"
    r"(?=.{0,80}\baccess (?:control|right))(?=.{0,80}\bempty\b)|"
    r"\baccess control row\b",
    re.IGNORECASE,
)
_NEW_MODEL_NAME_LOCAL_RE = re.compile(r"^\s*_name\s*=\s*['\"]([\w.]+)['\"]", re.MULTILINE)


_THIS_ROUNDS_FOCUS_RE = re.compile(r"This round's own NEW focus is ONLY:\s*'([^']+)'")
_REVIEW_SECURITY_RELATED_LABEL_KEYWORDS = ("group", "access", "security", "rule", "permission")


def _review_this_rounds_focus_is_security_related(goal: str) -> bool:
    """Mirrors specialists/build/specialist.py's own
    `_this_rounds_focus_is_security_related()` exactly -- duplicated,
    not imported, per this codebase's own manager/specialists layering
    (specialists don't import from each other's internals either).
    """
    match = _THIS_ROUNDS_FOCUS_RE.search(goal or "")
    if not match:
        return False
    focus = match.group(1).lower()
    return any(keyword in focus for keyword in _REVIEW_SECURITY_RELATED_LABEL_KEYWORDS)


def _filter_hallucinated_empty_security_csv_findings(
    findings: list[ReviewFinding], files: dict[str, str], goal: str = "", scope: RoundScope | None = None,
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-07-25, task 005's 3rd
    fresh submission) -- fifth sibling in this same family. Confirmed
    live: Code-Review blocked a round with "The access control file is
    empty, which will cause a module installation error or leave the
    model without proper access rights" -- for a task that defines NO
    new model at all (a pure `@api.onchange` extension of an existing
    one). A header-only security CSV (zero data rows) for exactly this
    shape is not an omission -- it's the objectively CORRECT, documented
    behavior this project's own `specialists/build/specialist.py::
    build_deterministic_security_csv()` already encodes ("No new models
    at all -- the CSV must be exactly the header row, zero data rows.
    Always correct, always deterministic."): access rights for an
    inherited model already come from whichever module first defined it,
    never re-granted per extension module. Code-Review has no way to
    know this project-specific convention from the diff alone, so it
    reads an empty file as a bug every time -- the same "hedged claim
    with no real access to check itself" shape as its four siblings.

    Only ever downgrades when files show NO new model definition (a
    genuinely new model DOES need real access rows -- never touch that
    case) AND the actual security_csv content really is header-only (not
    a genuinely empty/missing file, and not one with real data rows the
    finding might legitimately be about something else in).

    Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task): the "a genuine new model DOES need real
    access rows" rule above is only true OUTSIDE a decomposed round that
    explicitly defers the access-row constraint to a later round --
    Build's own sibling validator (`_validate_security_csv_covers_new_
    models()`, relaxed 2026-07-20 Phase 20 Area 2 UPDATE 20) already
    correctly relaxes this exact rule for exactly that shape (Odoo does
    NOT require an access row at INSTALL time, only at runtime
    ACL-check time), but this filter never knew about that relaxation
    at all, so Code-Review kept blocking a round scoped to ONLY
    `student_model_fields` (with `security_groups`/`record_rules`
    explicitly named as NOT yet in scope) for having a header-only CSV
    -- correct, deliberate behavior per Build's own already-established
    rule, not a real omission. Now also exempted when `goal` shows a
    decomposed round whose own focus isn't itself security-related,
    mirroring Build's own validator's exact condition so the two never
    fight each other.
    """
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    is_new_model_round = bool(_NEW_MODEL_NAME_LOCAL_RE.search(models_py))
    if is_new_model_round:
        # P12 Tier S/A item 6: reads the structured RoundScope (contracts/scope.py) instead
        # of re-parsing "NOT yet in scope for this round...: [...]" out of goal prose --
        # `scope` is authoritative whenever the caller passes it (every real caller now does,
        # see _run_diff_review()); the `goal` regex remains only as a fallback for any test or
        # caller that still passes `scope=None`, so this function's own behavior is unchanged
        # for anyone not yet updated.
        is_decomposed_with_deferrals = (
            bool(scope.not_yet_in_scope) if scope is not None
            else bool(_REVIEW_NOT_YET_IN_SCOPE_RE.search(goal or ""))
        )
        decomposed_and_not_security_focused = is_decomposed_with_deferrals and not (
            _review_this_rounds_focus_is_security_related(goal)
        )
        if not decomposed_and_not_security_focused:
            return findings  # a genuine new model -- real access rows are actually required, never downgrade

    security_csv = next(
        (content for path, content in files.items() if path.endswith("ir.model.access.csv")), None
    )
    if security_csv is None:
        return findings
    data_lines = [line for line in security_csv.strip().splitlines()[1:] if line.strip()]
    if data_lines:
        # 50-task deep-dive follow-up (2026-08-05): general hardening, justified on its own
        # logical merits, NOT by a specific confirmed task019 case -- an earlier version of this
        # comment cited a "task019 hallucination" that turned out, on the project owner's own independent
        # trace-level check, to be a documentation error in the deep-dive report itself (a quote
        # from two unrelated, days-old tasks conflated into task019's write-up during that
        # report's own analysis pipeline, never actually said by Code-Review about task019). The
        # underlying reasoning still holds regardless of provenance: the pre-existing guard above
        # ("has real data rows -- not the header-only case, leave the finding alone") is too
        # cautious for the SPECIFIC claim shape `_EMPTY_SECURITY_CSV_CLAIM_RE` matches --  that
        # regex only ever fires on wording that unambiguously asserts the file IS empty/header-
        # only/has-no-rows, never a generic "something's wrong with security" complaint. When that
        # specific, unambiguous claim is made and the file demonstrably has real data rows, the
        # claim is definitionally false regardless of what task or context produced it -- "might
        # legitimately be about something else" cannot be true of a claim this specific once its
        # own stated premise (zero rows) is checkably wrong. Downgrade here instead of falling
        # through untouched.
        filtered = []
        for f in findings:
            if f.severity == "blocking" and _EMPTY_SECURITY_CSV_CLAIM_RE.search(f.explanation):
                filtered.append(f.model_copy(update={
                    "severity": "info",
                    "explanation": (
                        f"[Downgraded from blocking -- the real, actually-generated security_csv "
                        f"has {len(data_lines)} genuine data row(s) (not header-only), directly "
                        f"contradicting this claim of emptiness] {f.explanation}"
                    ),
                }))
            else:
                filtered.append(f)
        return filtered

    if is_new_model_round:
        reason = (
            "this round's own explicit scope defers 'security_groups'/'record_rules' to a later "
            "round, and Odoo does not require an access row at INSTALL time (only at runtime "
            "ACL-check time) -- a header-only security CSV is Build's own already-correct, "
            "deliberate behavior for this specific round's scope"
        )
    else:
        reason = (
            "this module defines NO new model, so a header-only security CSV (zero data rows) is "
            "the objectively correct, documented behavior for this shape (access rights come from "
            "whichever module already owns the inherited model, never re-granted per extension)"
        )

    filtered = []
    for f in findings:
        if f.severity == "blocking" and _EMPTY_SECURITY_CSV_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": f"[Downgraded from blocking -- {reason} -- not an omission] {f.explanation}",
            }))
        else:
            filtered.append(f)
    return filtered


_SEQUENCE_PATTERN_NAME_FIELD_RE = re.compile(
    r"\bname\s*=\s*fields\.Char\([^)]*default\s*=\s*['\"]New['\"]", re.DOTALL
)
_SEQUENCE_PATTERN_CREATE_RE = re.compile(r"ir\.sequence.{0,10}\]\.next_by_code")
_SEQUENCE_PATTERN_NEW_CHECK_RE = re.compile(r"==\s*['\"]New['\"]")
_SEQUENCE_PATTERN_FIELD_REDEFINITION_CLAIM_RE = re.compile(
    r"redefin\w*.{0,40}(?:field|name).{0,60}(?:inherit|overrid)|"
    r"overrid\w*.{0,40}(?:field|name).{0,60}(?:inherit|original)",
    re.IGNORECASE,
)
_SEQUENCE_PATTERN_NEW_FALLBACK_CLAIM_RE = re.compile(
    r"'New'.{0,100}(?:contradict|fallback|fails|breaks|does not|doesn.t)|"
    r"(?:contradict|fallback|fails|breaks|does not|doesn.t).{0,100}'New'",
    re.IGNORECASE,
)
# Real, confirmed gap found live (2026-08-08, the "flagship field-service" task's own
# `equipment_registry` node, escalated TWICE across a resume): the three siblings above only
# ever recognize Odoo's ONE classic `name`/`'New'` textbook idiom. A field with any OTHER name
# (here, `tracking_number`), auto-assigned via the equally standard `if not vals.get('X'): ...
# next_by_code(...)` shape, never matched, so `_filter_hallucinated_sequence_pattern_findings()`
# never even considered downgrading anything for it -- and Code-Review kept blocking on "does
# not handle... empty string" against code that, confirmed by direct inspection, genuinely
# already does (`vals.get('tracking_number') == ''` was explicitly present), identically across
# 3 straight rounds (the exact literal phrase "as flagged in previous rounds" appearing in its
# own later findings is itself evidence it was echoing a prior claim rather than re-deriving one
# from the actual current diff). Mirrors the identical widening already made to
# `specialists/testing_qa/specialist.py`'s sibling coverage-exemption function -- same general
# guard/assignment regexes, same discipline of tying them to the SAME field name.
_SEQUENCE_PATTERN_GENERAL_GUARD_RE = re.compile(
    r"(?:not\s+vals\.get\(\s*['\"](\w+)['\"]\s*\)"
    r"|['\"](\w+)['\"]\s*not\s+in\s+vals"
    r"|not\s+vals\[\s*['\"](\w+)['\"]\s*\])"
)
_SEQUENCE_PATTERN_GENERAL_ASSIGN_RE = re.compile(
    r"vals\[\s*['\"](\w+)['\"]\s*\]\s*=\s*self\.env\[\s*['\"]ir\.sequence['\"]\s*\]\.next_by_code"
)
# Distinct from `_SEQUENCE_PATTERN_GENERAL_GUARD_RE` above: THIS one only matches guard forms
# that genuinely treat an empty string as "not provided" (a falsy-value check or an explicit
# `== ''`) -- the bare `'X' not in vals` form alone does NOT cover a caller that explicitly
# passes `vals['X'] = ''`, so it must never be used, by itself, to downgrade a claim SPECIFICALLY
# about empty-string handling (see `_claims_empty_string_not_handled()` below).
_SEQUENCE_PATTERN_EMPTY_STRING_SAFE_GUARD_RE = re.compile(
    r"(?:not\s+vals\.get\(\s*['\"](\w+)['\"]\s*\)"
    r"|not\s+vals\[\s*['\"](\w+)['\"]\s*\]"
    r"|vals\.get\(\s*['\"](\w+)['\"][^)]*\)\s*==\s*['\"]{2}"
    r"|vals\[\s*['\"](\w+)['\"]\s*\]\s*==\s*['\"]{2})"
)
_SEQUENCE_PATTERN_EMPTY_STRING_CLAIM_RE = re.compile(
    r"(?:does\s*not|doesn.t|never|fails?\s+to)\s+handle.{0,60}empty\s*string|"
    r"empty\s*string.{0,60}(?:not\s+handled|unhandled|ignored)",
    re.IGNORECASE,
)


def _general_sequence_idiom_field_names(models_py: str, guard_re: "re.Pattern[str]") -> set[str]:
    """Shared helper: the set of field names for which BOTH a genuine "not already provided"
    guard (per `guard_re`) AND a genuine `next_by_code()` assignment exist, tied together by
    naming the identical field -- never just "a guard exists somewhere" and "an assignment
    exists somewhere," which could accidentally pair up two unrelated fields.
    """
    assign_fields = {m.group(1) for m in _SEQUENCE_PATTERN_GENERAL_ASSIGN_RE.finditer(models_py)}
    guard_fields = {g for m in guard_re.finditer(models_py) for g in m.groups() if g}
    return assign_fields & guard_fields


def _filter_hallucinated_sequence_pattern_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-07-25, task 006's 2nd
    fresh submission) -- sixth sibling in this same family, but a
    different KIND of ground truth than its five siblings (those all
    verify a claim against a live registry lookup; this one verifies a
    claim against a known-correct, standard Odoo CODE PATTERN instead,
    since there's no live-registry fact to check here). Confirmed live:
    Code-Review blocked the SAME two findings identically across 3
    straight rounds -- "Redefining the 'name' field in an inherited
    model overrides the original field definition, potentially breaking
    existing functionality" and "The create() method override uses
    'New' as a fallback which contradicts the requirement that the...
    sequence should always be used" -- for code that, verified directly
    (fetched from Gitea), was the textbook-correct, standard Odoo
    sequence-assignment idiom: redefining an inherited model's own
    `name` field with `default='New'`, checked in a `@api.model_create_
    multi`-decorated `create()` override (`if vals.get('name', 'New')
    == 'New': vals['name'] = self.env['ir.sequence'].next_by_code(...)
    or 'New'`) -- the exact same pattern real Odoo core modules
    (sale.order, account.move, ...) use themselves. Both complaints
    misread a deliberate, idiomatic design as a bug.

    Deliberately narrow: only fires when the actual generated code is
    CONFIRMED to be this exact standard shape (a `name` field genuinely
    defaulting to `'New'`, a genuine `ir.sequence.next_by_code` call, and
    a genuine `== 'New'` check all present together) -- a task using a
    genuinely different, non-standard sequence pattern, or one that
    really does have a broken create() override, is never touched.

    Widened 2026-08-08 (see `_SEQUENCE_PATTERN_GENERAL_GUARD_RE`'s own comment for the full real
    incident): ALSO recognizes the same idiom generalized to any field name, for a genuinely
    different claim shape ("does not handle... empty string") than the two classic-idiom claims
    above -- each idiom only ever downgrades the claim type it can actually verify against real
    code, never the other's.
    """
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    matches_classic_name_idiom = bool(
        _SEQUENCE_PATTERN_NAME_FIELD_RE.search(models_py)
        and _SEQUENCE_PATTERN_CREATE_RE.search(models_py)
        and _SEQUENCE_PATTERN_NEW_CHECK_RE.search(models_py)
    )
    # Fields where a genuine "not already provided" guard and a genuine next_by_code()
    # assignment both name the SAME field -- the general idiom, independent of field name.
    general_idiom_fields = _general_sequence_idiom_field_names(models_py, _SEQUENCE_PATTERN_GENERAL_GUARD_RE)
    # Narrower subset: of those, which ones are guarded specifically against an EMPTY STRING
    # too (not just "key missing from vals entirely") -- only these can safely downgrade an
    # empty-string-specific claim; see the safe-guard regex's own comment for why the two are
    # deliberately kept separate.
    empty_string_safe_fields = _general_sequence_idiom_field_names(
        models_py, _SEQUENCE_PATTERN_EMPTY_STRING_SAFE_GUARD_RE,
    )
    if not (matches_classic_name_idiom or general_idiom_fields):
        return findings

    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        if matches_classic_name_idiom and (
            _SEQUENCE_PATTERN_FIELD_REDEFINITION_CLAIM_RE.search(f.explanation)
            or _SEQUENCE_PATTERN_NEW_FALLBACK_CLAIM_RE.search(f.explanation)
        ):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this is the standard, correct Odoo ir.sequence "
                    f"pattern (redefining an inherited model's own name field with default='New', "
                    f"checked in create() before assigning from ir.sequence.next_by_code -- the "
                    f"same pattern real Odoo core modules use themselves), not a bug] {f.explanation}"
                ),
            }))
        elif empty_string_safe_fields and _SEQUENCE_PATTERN_EMPTY_STRING_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- the create() override's own guard "
                    f"({sorted(empty_string_safe_fields)!r}) already checks for an empty string "
                    f"explicitly (a falsy-value check or an explicit == '' comparison), verified "
                    f"directly against the real generated code, not just a claim taken on faith] "
                    f"{f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_SELECTION_ADD_NOT_ADDED_CLAIM_RE = re.compile(
    r"is not added to (?:the |its )?.{0,20}selection|"
    r"(?:selection|state field).{0,30}(?:does not|doesn.t|never) (?:include|add|contain)|"
    r"cannot store or display the new (?:state|value|option)",
    re.IGNORECASE,
)


def _filter_hallucinated_selection_add_not_added_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-08-03, task011's own real re-test) -- same family
    as the sequence-pattern filters above (verifies against a known-correct standard Odoo CODE
    PATTERN, not a live-registry fact): `fields.Selection(selection_add=[('X', 'Label')], ...)` is
    the real, standard, correct Odoo idiom for extending an INHERITED Selection field with a new
    option -- yet Code-Review confidently claimed the new option "is not added to the state field
    selection, so the model cannot store or display the new state" for code that genuinely DOES
    add it this exact way. A real hallucination about a standard Odoo mechanism, not a bug.

    Deliberately narrow: only fires when the SPECIFIC option name the finding names is genuinely
    present inside a real `selection_add=[...]` call in models_py -- a genuinely missing option,
    or a finding about some other field/value, is never touched.
    """
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    added_options = set(re.findall(r"selection_add\s*=\s*\[([^\]]*)\]", models_py))
    if not added_options:
        return findings
    added_option_keys: set[str] = set()
    for block in added_options:
        added_option_keys |= set(re.findall(r"\(\s*['\"](\w+)['\"]", block))
    if not added_option_keys:
        return findings

    filtered = []
    for f in findings:
        mentioned = {k for k in added_option_keys if k in f.explanation}
        if f.severity == "blocking" and mentioned and _SELECTION_ADD_NOT_ADDED_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified that {'/'.join(sorted(mentioned))} IS "
                    f"genuinely added via the real, standard Odoo `selection_add=[...]` idiom for "
                    f"extending an inherited Selection field; this claim was unfounded] "
                    f"{f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_SEQUENCE_NOT_TRANSACTION_SAFE_CLAIM_RE = re.compile(
    r"not transaction.safe|(?:may|could|might) produce duplicate|"
    r"(?:race condition|concurrent writes?).{0,60}(?:duplicate|sequence)|"
    r"(?:duplicate|sequence).{0,60}(?:race condition|concurrent writes?)",
    re.IGNORECASE,
)


def _filter_hallucinated_sequence_not_transaction_safe_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-08-03, task006's own real re-test) -- same family
    as `_filter_hallucinated_sequence_pattern_findings` above (verifies against a known-correct
    standard Odoo CODE PATTERN, not a live-registry fact), a different claim about the same
    `ir.sequence.next_by_code()` call: "not transaction-safe... may produce duplicate references
    under concurrent writes." This is a hallucination about Odoo's own sequence mechanism, not a
    real bug -- `ir.sequence.next_by_code()` genuinely IS transaction-safe by design (Odoo
    guarantees atomic, non-duplicate increments at the database level for every real sequence
    call); this is the standard, documented, safe way every real Odoo module (sale.order,
    account.move, ...) generates a reference number, not something requiring extra locking code.

    Deliberately narrow: only fires when the flagged code genuinely IS a plain
    `self.env['ir.sequence'].next_by_code(...)` call (the real, safe idiom) -- a task that invented
    its OWN non-atomic, hand-rolled numbering scheme (e.g. `max(existing) + 1`, genuinely racy) is
    never touched.
    """
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    if not _SEQUENCE_PATTERN_CREATE_RE.search(models_py):
        return findings

    filtered = []
    for f in findings:
        if f.severity == "blocking" and _SEQUENCE_NOT_TRANSACTION_SAFE_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- ir.sequence.next_by_code() genuinely IS "
                    f"transaction-safe by design (Odoo guarantees atomic, non-duplicate increments "
                    f"at the database level); this is the standard, safe reference-number idiom "
                    f"real Odoo core modules use themselves, not a concurrency bug] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_FIELD_NOT_INSTANTIATED_CLAIM_RE = re.compile(
    r"\bis a class,?\s*not an instance\b|"
    r"\bmust be\s+fields\.\w+\(\)\s*to (?:define|instantiate)|"
    r"\brequires? parentheses to instantiate\b|"
    r"\bnot (?:properly |correctly )?instantiat\w*\b|"
    # Real, general fix (2026-07-25, same investigation): a second,
    # independently-confirmed live rewording of the EXACT SAME false
    # claim on the SAME task's very next resubmission -- "uses
    # `fields.Text` instead of `fields.Text()`, causing a runtime
    # error" -- proving Code-Review's own prose for this hallucination
    # is not stable across rounds/resubmissions, the same lesson this
    # whole filter family already learned from its five siblings above
    # (each one widened at least once after a live rewording slipped
    # past the first, narrower pattern).
    r"uses\s+`?fields\.\w+`?\s+instead of\s+`?fields\.\w+\(\)`?",
    re.IGNORECASE,
)
_FIELD_TYPE_MENTION_RE = re.compile(r"\bfields\.(\w+)\b")


def _filter_hallucinated_field_not_instantiated_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-07-25, Phase 25B
    regression gate, task 001's resubmission) -- seventh sibling in this
    same family, same shape as the sequence-pattern check above (a
    known-correct CODE PATTERN, not a live-registry lookup, is the
    ground truth here). Confirmed live: Code-Review blocked a round with
    "fields.Text is a class, not an instance; must be fields.Text() to
    define a valid Odoo field" -- the real, actually-generated code
    (independently confirmed via the round's own diff) already read
    `special_instructions = fields.Text(string='Special Instructions')`,
    a completely correct, properly-instantiated field. This misreading
    is general, not specific to Text: any Odoo field type written as
    `= fields.X(...)` (a real, syntactically valid call, with real
    keyword arguments) is never actually the bare-class mistake this
    claim describes.

    Deliberately narrow: only downgrades when a field type this claim
    NAMES (e.g. "fields.Text") is confirmed to appear in the real code
    as a genuine `= fields.Text(` call (an opening parenthesis right
    after the type name -- the actual bug this claim describes would
    have NO opening parenthesis there at all). A genuine instantiation
    bug (a real bare `= fields.Text` with no parentheses anywhere in the
    file) is never touched -- the real pattern simply won't match.
    """
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    if not models_py:
        return findings

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _FIELD_NOT_INSTANTIATED_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        mentioned_types = set(_FIELD_TYPE_MENTION_RE.findall(f.explanation))
        genuinely_instantiated = {
            t for t in mentioned_types if re.search(rf"=\s*fields\.{re.escape(t)}\s*\(", models_py)
        }
        if genuinely_instantiated:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified against the real generated code that "
                    f"{'/'.join(sorted(genuinely_instantiated))} IS genuinely instantiated with a "
                    f"real `(...)` call; this claim of a bare, uninstantiated field class was "
                    f"unfounded] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_MODULE_REGISTRATION_CLAIM_RE = re.compile(
    r"__init__\.py|does not import\w*|doesn.t import\w*|not (?:properly |correctly )?import\w*|"
    r"not (?:properly |correctly )?regist\w*|module (?:is )?not installed|module (?:is )?not activated",
    re.IGNORECASE,
)


def _filter_hallucinated_module_registration_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, general, deterministic guard (2026-07-26, Phase 25D, task
    005's own resubmission) -- eighth sibling in this same family, same
    shape as the sequence-pattern check (a known-correct, standard
    scaffold STRUCTURE, not a live-registry lookup, is the ground truth
    here). Confirmed live: Code-Review blocked a round with "the
    _onchange_project_id method is defined but the module's __init__.py
    does not import the models module correctly or the module is not
    installed/activated, leading to the logic not being applied" -- the
    real, actual `__init__.py` (independently confirmed by reading it
    directly off the real container) correctly read `from . import
    models`, and `models/__init__.py` correctly read `from . import
    models` -- the exact standard scaffold structure `tools_odoo.
    module_dev.toolchain.scaffold_module()` always sets up and this
    project's own generation pipeline never touches afterward. This
    claim doubts a structural, always-correct part of the pipeline's own
    output, not something the model itself ever writes or could break.

    Deliberately narrow: only downgrades when BOTH the top-level
    `__init__.py` genuinely imports the `models` submodule AND
    `models/__init__.py` genuinely imports the real models file -- the
    exact, standard, two-file scaffold chain. A module missing either
    (a genuinely broken registration, however unlikely given this
    project's own deterministic scaffolding) is never touched.
    """
    top_init = next(
        (content for path, content in files.items() if path.endswith("__init__.py") and "/models/" not in path),
        None,
    )
    models_init = next(
        (content for path, content in files.items() if path.endswith("models/__init__.py")), None,
    )
    if not top_init or not models_init:
        return findings
    if not re.search(r"from\s+\.\s+import\s+models\b", top_init):
        return findings
    if not re.search(r"from\s+\.\s+import\s+models\b", models_init):
        return findings

    filtered = []
    for f in findings:
        if f.severity == "blocking" and _MODULE_REGISTRATION_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified against the real __init__.py files "
                    f"that the standard module-registration chain (top-level __init__.py -> "
                    f"models/__init__.py -> models.py) is genuinely intact; this claim of a broken "
                    f"import/registration was unfounded] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_REVIEW_NEW_MODEL_NAME_RE = re.compile(r"_name\s*=\s*['\"]([\w.]+)['\"]")
_MODEL_COLLISION_CLAIM_RE = re.compile(r"collid|silent(?:ly)?\s+overrid|already[\s-]exist", re.IGNORECASE)


_BARE_GROUP_ID_RECORD_RE = re.compile(r'<record\s+id="(\w+)"\s+model="res\.groups"')
_BARE_GROUP_PREFIX_MISMATCH_CLAIM_RE = re.compile(
    r"\bwithout\s+the\s+module\s+prefix\b|"
    r"\bdefined\s+without\s+(?:a\s+|the\s+)?(?:module\s+)?prefix\b|"
    # Real, confirmed rewording found live (2026-07-29, same task, same
    # round, immediately after the first attempt deployed): "causing a
    # module prefix mismatch" -- "module prefix" now comes BEFORE
    # "mismatch", the opposite order the first regex assumed. Matched
    # order-independently from here on (same proximity-match approach
    # already proven necessary for _EMPTY_SECURITY_CSV_CLAIM_RE earlier
    # this session, after an identical single-order regex got dodged by
    # rewording), so a third reordering doesn't reopen this gap again.
    r"(?=.{0,80}\bmismatch\b)(?=.{0,80}\bmodule\s+prefix\b)|"
    r"(?=.{0,80}\bmodule\s+prefix\b)(?=.{0,80}\bmismatch\b)",
    re.IGNORECASE,
)


_WRONG_MODEL_XMLID_FORMAT_CLAIM_RE = re.compile(
    r"model_\w+_\w+.{0,60}\bis\s+(?:incorrect|wrong)\b|"
    r"\bcorrect\s+external\s+id\b.{0,80}\bmodel_\w+_\w+|"
    r"\bincorrect\b.{0,80}\bexternal\s+id\b.{0,80}\bmodel_|"
    # Broadened (2026-07-29, same task, later round, real live
    # recurrence): "the correct auto-generated external ID for the
    # model is model_X_Y" -- extra words ("auto-generated") between
    # "correct" and "external id" dodged the original tight adjacency
    # match, the exact same "rewording defeats a strict regex" failure
    # mode already hit and fixed for several OTHER filters in this same
    # file tonight. Order-independent proximity match instead of one
    # more exact-phrase alternative.
    r"(?=.{0,60}\bcorrect\b)(?=.{0,60}\bexternal\s+id\b)(?=.{0,60}\bmodel_\w+_\w+)",
    re.IGNORECASE,
)
# Broadened (2026-07-29): a bare model_X reference can legitimately
# appear not just in an XML `ref="..."` attribute but also as a raw CSV
# field value (ir.model.access.csv's own `model_id:id` column, e.g.
# `access_x,x,model_school_student,base.group_user,...` with no
# surrounding quotes/ref= syntax at all) -- matched generically as any
# standalone `model_<word>` token, not tied to one specific attribute
# syntax.
_MODEL_XMLID_REF_RE = re.compile(r'ref="(model_\w+)"|"model_id"\s+ref="(model_\w+)"|\b(model_\w+)\b')


def _filter_hallucinated_wrong_model_xmlid_format_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-07-29, Phase 28C,
    school_student task, computed_age_field round): Code-Review claimed
    `ref="model_school_student"` is "incorrect" and the "correct
    external ID... is 'model_school_student_school_student'" (doubling
    the model-name segment) -- a hallucination. Odoo's own, real,
    standard convention for a model's auto-generated `ir.model` xmlid
    is exactly `model_<model_name_with_dots_replaced_by_underscores>`
    -- for `school.student`, that IS `model_school_student`, full stop,
    never a doubled form. Confirmed directly, repeatedly, live tonight:
    this exact bare form installed cleanly via a real Odoo process
    multiple times across this same task's own earlier rounds.

    Deterministic, purely local/textual: downgrades a "wrong xmlid
    format" claim only when it names a `model_X_X` doubled-form
    replacement for a genuinely correctly-formed `model_X` reference
    that already appears in this generation's own content (i.e. the
    claim is contradicting content this generation itself produced
    correctly) -- never touches a claim about a genuinely different,
    real xmlid problem (e.g. referencing a model this generation never
    defined at all, which is a real, different bug this filter must
    not paper over).
    """
    blocking_claims = [
        f for f in findings if f.severity == "blocking" and _WRONG_MODEL_XMLID_FORMAT_CLAIM_RE.search(f.explanation)
    ]
    if not blocking_claims:
        return findings
    all_content = "".join(files.values())
    real_bare_model_refs = {
        m1 or m2 or m3 for m1, m2, m3 in _MODEL_XMLID_REF_RE.findall(all_content)
    }
    if not real_bare_model_refs:
        return findings
    filtered = []
    for f in findings:
        mentioned = {ref for ref in real_bare_model_refs if ref in f.explanation}
        if f in blocking_claims and mentioned:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {'/'.join(sorted(mentioned))} is Odoo's own "
                    f"standard, correct xmlid form for this model (model_<name_with_underscores>, "
                    f"never a doubled form); confirmed live via repeated real installs; this claim "
                    f"was false] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _filter_hallucinated_bare_group_id_prefix_mismatch_findings(
    findings: list[ReviewFinding], files: dict[str, str], module_name: str,
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-07-29, Phase 28C,
    school_student task, security_groups round): Code-Review claimed a
    `<record id="group_school_admin" model="res.groups">` defined
    WITHOUT an explicit module prefix "causes a mismatch" with
    security_csv's own `group_id:id` reference to
    `oma_simple_custom_module_task_595ad7bc.group_school_admin` -- a
    hallucination. This is textbook, standard, correct Odoo xmlid
    resolution: a bare `id="X"` defined inside module M is ALWAYS
    addressable both as the bare `X` (same-file) and as the fully-
    qualified `M.X` (from anywhere else, including this exact CSV
    format) -- there is no "mismatch" here at all, by design, not an
    edge case. Confirmed live by directly, empirically installing this
    EXACT real content via a fresh `odoo-bin -u` process against the
    real target database: it installed cleanly with zero errors,
    proving Code-Review's own claim false, not merely unverified.

    Deterministic, purely local/textual (no DB lookup needed -- this is
    a pure Odoo XML-id-resolution fact, not something that depends on
    live registry state): downgrades a "prefix mismatch" claim only
    when the bare group id it names is genuinely defined via `<record
    id="X" model="res.groups">` somewhere in this generation's own
    security_xml AND the claim's own module-qualified form (`{module_
    name}.X`) is the exact thing the claim itself is complaining about
    -- never touches a claim about a GENUINELY different, real
    mismatch (e.g. two different bare ids that don't actually match).
    """
    security_xml = next((c for p, c in files.items() if p.endswith("security.xml")), "")
    bare_group_ids = set(_BARE_GROUP_ID_RECORD_RE.findall(security_xml))
    if not bare_group_ids:
        return findings
    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _BARE_GROUP_PREFIX_MISMATCH_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        mentioned = {
            group_id for group_id in bare_group_ids
            if group_id in f.explanation or f"{module_name}.{group_id}" in f.explanation
        }
        if mentioned:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- a bare <record id=\"{'/'.join(sorted(mentioned))}\" "
                    f"model=\"res.groups\"> is standard, correct Odoo practice; it resolves as BOTH the "
                    f"bare id and the fully-qualified '{module_name}.<id>' form automatically -- there "
                    f"is no real mismatch with a module-prefixed CSV reference to it, confirmed live by "
                    f"a real, successful install of this exact content; this claim was false] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_MODEL_ID_PREFIX_MISMATCH_CLAIM_RE = re.compile(
    r"(?=.{0,120}\bmismatch\b)(?=.{0,120}\bmodule_?\s*prefix\b)|"
    r"(?=.{0,120}\bmodule_?\s*prefix\b)(?=.{0,120}\bmismatch\b)|"
    r"\bwithout\s+the\s+module\s+prefix\b.{0,80}\bcausing\b|"
    r"(?=.{0,100}\breferences\b)(?=.{0,100}\bbut\s+the\s+model_id\b)(?=.{0,100}\bwithout\s+the\s+module\s+prefix\b)",
    re.IGNORECASE,
)


def _filter_hallucinated_model_id_prefix_mismatch_findings(
    findings: list[ReviewFinding], files: dict[str, str], module_name: str,
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-07-29, Phase 28C,
    school_student task, demo_data round): the SAME false-claim shape as
    `_filter_hallucinated_bare_group_id_prefix_mismatch_findings()`
    above, but for a model's own implicit `model_<name>` xmlid instead
    of a locally-defined `res.groups` record. Code-Review claimed the
    record rule 'rule_school_student_delete' referencing the fully-
    qualified 'oma_simple_custom_module_task_595ad7bc.model_school_
    student' "mismatches" ir.model.access.csv's bare 'model_school_
    student' reference to the SAME model, "causing a mismatch and
    potential loading failure" -- a hallucination. Standard Odoo xmlid
    resolution applies identically here: a model's own auto-generated
    `model_<name>` id is ALWAYS addressable both bare (within its own
    module) and fully-qualified (`module.model_<name>`, from anywhere,
    including a CSV `model_id:id` column) -- there is no real mismatch
    between the two forms referring to the exact same model.

    Deterministic, purely local/textual: downgrades a "module prefix
    mismatch" claim only when it names a `model_X` id that genuinely
    appears (bare or module-qualified) SOMEWHERE in this generation's
    own real content -- never touches a claim about a model id that was
    never actually used at all, which could be a genuinely different,
    real bug.
    """
    all_content = "".join(files.values())
    real_model_ids = {m1 or m2 or m3 for m1, m2, m3 in _MODEL_XMLID_REF_RE.findall(all_content)}
    if not real_model_ids:
        return findings
    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _MODEL_ID_PREFIX_MISMATCH_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        mentioned = {
            model_id for model_id in real_model_ids
            if model_id in f.explanation or f"{module_name}.{model_id}" in f.explanation
        }
        if mentioned:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {'/'.join(sorted(mentioned))} is a model's own "
                    f"standard, auto-generated xmlid; it resolves as BOTH the bare id and the fully-"
                    f"qualified '{module_name}.<id>' form automatically -- there is no real mismatch "
                    f"between a qualified reference and a bare CSV reference to the SAME model; this "
                    f"claim was false] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _filter_hallucinated_own_module_collision_findings(
    findings: list[ReviewFinding], files: dict[str, str], module_name: str,
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task): ninth sibling in this same family. Build's
    own pre-write validator (`_validate_new_model_names_dont_collide()`,
    specialists/build/specialist.py) already correctly exempts THIS
    SAME task's own model continuing to exist across rounds -- a real,
    already-installed model from an earlier round of the identical
    task, not a foreign collision -- via a live-registry ownership
    check comparing the real owner module against the round's own
    `module_name`. Code-Review has no equivalent context at all: its
    own review prompt never tells it "this real model you're seeing
    already exists is genuinely THIS module's own prior-round work,"
    so it independently, plausibly concludes the identical model name
    is a foreign collision ("Model name 'school.student' collides with
    existing model from module 'oma_...', causing silent override")
    every single round, an unrecoverable loop no amount of prompt
    engineering on the BUILD side can fix, since the false claim
    originates entirely on the REVIEW side. Confirmed live: this exact
    finding blocked school_student's own live run even after Build's
    own collision validator was correctly fixed and verified.

    Same deterministic, live-registry-verified downgrade pattern as
    every sibling filter in this file: only downgrades a finding when
    the model it claims collides with is REALLY, verifiably owned by
    this exact round's own `module_name` -- a genuine foreign collision
    (owned by any other real module) is never touched.
    """
    new_model_names = set(_REVIEW_NEW_MODEL_NAME_RE.findall(
        next((c for p, c in files.items() if p.endswith("models.py")), "")
    ))
    if not new_model_names:
        return findings
    blocking_claims = [
        f for f in findings if f.severity == "blocking" and _MODEL_COLLISION_CLAIM_RE.search(f.explanation)
    ]
    if not blocking_claims:
        return findings

    db = os.environ.get(_FAST_PATH_DB_ENV, "")
    if not db:
        return findings
    try:
        from tools_odoo.odoo_schema_client import is_fast_path_eligible, resolve_model_owner_module_fast
    except ImportError:
        return findings
    if not is_fast_path_eligible(db):
        return findings

    verified_own_models = {
        name for name in new_model_names
        if resolve_model_owner_module_fast(name, db) == module_name
    }
    if not verified_own_models:
        return findings

    filtered = []
    for f in findings:
        mentioned = {m for m in verified_own_models if m in f.explanation}
        if f in blocking_claims and mentioned:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- verified via the real, live registry that "
                    f"{'/'.join(sorted(mentioned))} is genuinely owned by this exact module "
                    f"({module_name!r}), i.e. this task's own earlier round, not a foreign "
                    f"collision; this claim was false] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_REVIEW_NOT_YET_IN_SCOPE_RE = re.compile(r"NOT yet in scope for this round[^:]*:\s*\[(.*?)\]")
_REVIEW_ALREADY_SATISFIED_RE = re.compile(r"ALREADY satisfied by earlier work[^:]*:\s*\[(.*?)\]")
_MISSING_FIELD_GATE_RE = re.compile(
    r"\bmissing|\babsent|\bis required\b|\bmust be (?:defined|declared|implemented)\b|\bomit|"
    r"\bno\b.{0,40}\bis present\b|\bnot present\b|\bis empty\b|\blacks?\b|"
    r"\bno such\b.{0,60}\bis implemented\b|\bnot implemented\b|"
    # Real, confirmed bug found live (wave24_rr, hr.employee, 2026-08-16): the real Code-Review
    # finding text "no record rule is defined in the XML" -- a completely natural, common
    # phrasing for a missing artifact -- matched NONE of the existing synonyms above (which
    # cover "present"/"missing"/"absent"/"empty"/"lacks" but not "defined"), so this exact
    # claim shape never reached _filter_hallucinated_out_of_scope_field_required_findings()'s
    # own downgrade logic at all, despite `not_yet_in_scope` genuinely containing a matching
    # label (`hr_employee_record_rule`) and the phrase-to-label-token map already covering
    # "record rule" -> "record_rule" -- confirmed via direct reproduction: the exact real
    # finding text downgrades correctly once this gate recognizes it, and does NOT downgrade
    # without this addition. This is the real, structural root cause behind that direction's
    # repeated `gates_disagree`/`ask_operator` escalations, not a Operator policy question.
    r"\bno\b.{0,40}\bis defined\b|\bnot defined\b",
    re.IGNORECASE,
)
# Real, general bug found live (2026-08-07, task039, 2 consecutive clean relaunches --
# real task_ids 5381852f-8aea-4369-b1a0-6e5c6ea4992e and 1646ddef-f5f9-4fba-9caf-bdbb878d20b7):
# the previous single-match `_MISSING_FIELD_NAME_RE` (matched only "field '...'", never
# "method '...'") only ever extracted ONE name, so a finding naming BOTH a missing field AND a
# missing method ("a computed field 'is_external_service_token_expired' ... and a method
# 'action_mark_refreshed'") never even got the second name checked. Replaced with a broader
# extraction that grabs every quoted identifier in the explanation, regardless of which word
# precedes it.
# Real, confirmed live gap (2026-08-07, task041 v13, real task_id
# 4fcc0ba3-b4e4-470b-9172-cddf37d03eb4): a real finding used backtick-delimited code-style
# quoting ("The required `oe_stat_button` in the form view header is missing.") instead of
# straight quotes -- a common, distinct convention for naming a technical/code term, not caught by
# the original quote-only pattern.
_ALL_QUOTED_IDENTIFIERS_RE = re.compile(r"['\"`](\w+)['\"`]")
# Real, confirmed live gap (2026-08-07, task041 v14, real task_id
# f385f265-af25-4545-9eca-4f5124ec4c3c): the SAME real round's own real not-yet-in-scope labels
# were STILL falsely contradicted a 3rd time, via a 3rd distinct quoting gap -- these two real
# findings used NO quoting convention at all ("The task requires adding an oe_stat_button in the
# form's header/button_box, but the views.xml file is empty."; "The task requires an
# action_view_customer_projects method, but no methods are defined in models.py."). Odoo's own
# naming convention makes bare technical identifiers easy to recognize without quotes: a
# multi-word snake_case token essentially never occurs in ordinary English prose, so scanning for
# bare snake_case tokens too (in addition to quoted ones) is a low-false-positive-risk way to
# catch this shape generally, not just the one real phrasing found live.
_BARE_SNAKE_CASE_IDENTIFIER_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
# Real, confirmed bug found live (2026-08-16, wave24_rr/hr.employee, real task_id
# 4dc517a2-cf36-4afe-a606-84b15eaffe58, escalated ask_operator/gates_disagree): a blocking finding
# phrased as plain English ("the XML only defines the group and no record rule is present")
# never produces a candidate matching the round's own real not-yet-in-scope label
# ('record_rules') -- `_ALL_QUOTED_IDENTIFIERS_RE` only ever captures a SINGLE quoted word, not
# a multi-word quoted requirement sentence, and `_BARE_SNAKE_CASE_IDENTIFIER_RE` requires an
# underscore, which "record rule" (plain, space-separated English) never has. The exact same
# gap class `_SINGLE_TOKEN_ALLOWED_MATCHES` already exists to close for single bare terms
# (button/widget/wizard/menu/decoration) -- this is a two-word sibling of that same shape, not a
# broadening of candidate extraction to arbitrary English phrases: a narrow, explicit,
# known-phrase-to-label mapping, same discipline as every other entry in this family.
_BARE_PHRASE_TO_LABEL_TOKEN = {
    "record rule": "record_rule",
    "row-level security": "record_rule",
    "row level security": "record_rule",
}


def _extract_candidate_identifier_names(text: str) -> list[str]:
    # Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): a
    # finding using pure plain-English description ("show the status field with distinct colors
    # per state using decoration-<state> attributes") never produces a single quoted or
    # snake_case candidate at all, so it can never match ANY not-yet-in-scope label no matter how
    # exactly the label's own name (`ticket_status_decoration`) describes the identical concept.
    # Also scanning for bare, lowercase occurrences of `_SINGLE_TOKEN_ALLOWED_MATCHES`'s own
    # narrow Odoo view-vocabulary allow-list (already trusted as specific enough to avoid the
    # generic-word false-positive risk plain nouns like "student" carry) closes this gap for the
    # exact same class of technical term that allow-list already exists for, without broadening
    # candidate extraction to arbitrary English words.
    bare_technical_terms = [
        term for term in _SINGLE_TOKEN_ALLOWED_MATCHES
        if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE)
    ]
    bare_technical_phrases = [
        label_token for phrase, label_token in _BARE_PHRASE_TO_LABEL_TOKEN.items()
        if re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE)
    ]
    return (
        _ALL_QUOTED_IDENTIFIERS_RE.findall(text)
        + _BARE_SNAKE_CASE_IDENTIFIER_RE.findall(text)
        + bare_technical_terms
        + bare_technical_phrases
    )
# The ORIGINAL goal text's own literal field/method names (e.g. `is_external_service_token_
# expired`, `action_mark_refreshed`) are almost never a lexical SUBSTRING of the round's own
# abstract, separately-generated not-yet-in-scope LABEL (e.g. `token_expired_computed`,
# `mark_refreshed_button`) -- confirmed live: the real name is longer than the label in both
# directions, so plain substring containment can structurally never match. Both names/labels DO
# reliably share multiple significant underscore-separated word tokens (`token`+`expired`,
# `mark`+`refreshed`) even when composed in a different order. Requiring >=2 shared significant
# tokens (not just 1) is the deliberately conservative bar -- confirmed against this same file's
# own existing test fixture (tests/test_code_review_specialist.py): 'student_id' vs the unrelated
# label 'student_views' shares exactly ONE token ('student'), which must NOT match (that finding
# is a genuine, real defect and must stay blocking) -- so a single shared generic token is never
# enough on its own.
_TOKEN_OVERLAP_STOPWORDS = {"is", "a", "an", "the", "of", "to", "and", "or", "for", "on", "in"}
# Real, confirmed live gap (2026-08-07, task041 v13, real task_id
# 4fcc0ba3-b4e4-470b-9172-cddf37d03eb4): a real finding named the technical Odoo widget term
# `oe_stat_button` (tokens: oe/stat/button) against the round's own real not-yet-in-scope label
# `customer_overview_smart_button` (tokens: customer/overview/smart/button) -- these share only
# ONE token ('button'), failing the >=2 bar the student_id/student_views false-positive fixture
# requires generically. Rather than lowering the bar everywhere (reopening that exact false
# positive), a short, explicit allow-list of narrow Odoo UI/view technical terms lets a SINGLE
# shared token count as a match ONLY when that shared token is one of these -- these words are
# specific enough to Odoo view/action vocabulary that they don't carry the same "this is just the
# task's own generic subject noun, appears everywhere" false-positive risk 'student' does.
_SINGLE_TOKEN_ALLOWED_MATCHES = {"button", "widget", "wizard", "menu", "decoration"}


def _significant_tokens(name: str) -> set[str]:
    return {tok for tok in name.lower().split("_") if len(tok) >= 3 and tok not in _TOKEN_OVERLAP_STOPWORDS}


def _name_matches_out_of_scope_label(name: str, label: str) -> bool:
    name = name.lower()
    label = label.lower()
    if name in label or label in name:
        return True
    shared = _significant_tokens(name) & _significant_tokens(label)
    if len(shared) >= 2:
        return True
    return len(shared) == 1 and shared <= _SINGLE_TOKEN_ALLOWED_MATCHES


def _filter_hallucinated_out_of_scope_field_required_findings(
    findings: list[ReviewFinding], goal: str, scope: RoundScope | None = None,
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task): the mirror image of
    `_filter_hallucinated_scope_findings()` above (that one catches
    Code-Review INVENTING a scope exclusion that isn't real; this one
    catches Code-Review CONTRADICTING a scope exclusion that IS real).
    A round explicitly scoped to ONLY `student_model_fields`, with
    `computed_age_field` named as NOT yet in scope, still got
    repeatedly blocked with "Field 'age' is missing from the model
    definition, violating the round constraint 'student_model_fields'
    which requires it" -- Code-Review's own independent judgment
    (reasoning from the ORIGINAL top-level goal's full field list,
    which does list `age` among school_student's overall fields)
    disagreeing with, rather than respecting, the SAME round's own
    explicit, deterministic scope-exclusion instruction it was given in
    the very same prompt. This is not a case Build's own generation can
    ever resolve by itself: whatever it does, either Code-Review flags
    the field as prematurely present (when Build correctly excludes it)
    or as wrongly missing (when Build correctly excludes it and
    Code-Review disagrees) -- an unwinnable, unrecoverable contradiction
    entirely on the review side.

    Deterministic and general, matching every sibling filter's own
    discipline: only downgrades a finding claiming field X is
    missing/required when X is a real, explicit substring of one of
    THIS round's own not-yet-in-scope labels (e.g. `age` inside
    `computed_age_field`) -- a genuine, real missing-field claim for
    any field NOT named by an exclusion label is never touched.
    """
    # P12 Tier S/A item 6: reads RoundScope.not_yet_in_scope (structured, contracts/scope.py)
    # when the caller passes it; falls back to the original goal-prose regex only when it
    # doesn't, for the same backward-compatibility reason as the sibling function above.
    if scope is not None:
        not_yet_in_scope_labels = scope.not_yet_in_scope
    else:
        match = _REVIEW_NOT_YET_IN_SCOPE_RE.search(goal or "")
        if not match:
            return findings
        not_yet_in_scope_labels = [
            label.strip().strip("'\"").lower() for label in match.group(1).split(",") if label.strip()
        ]
    if not not_yet_in_scope_labels:
        return findings

    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        if not _MISSING_FIELD_GATE_RE.search(f.explanation):
            filtered.append(f)
            continue
        candidate_names = _extract_candidate_identifier_names(f.explanation)
        matched_names = sorted({
            name for name in candidate_names
            if any(_name_matches_out_of_scope_label(name, label) for label in not_yet_in_scope_labels)
        })
        if matched_names:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {matched_names!r} is/are explicitly named by "
                    f"this round's own NOT-yet-in-scope constraint list; a field/method this "
                    f"round's own contract explicitly forbids adding cannot simultaneously be a "
                    f"real 'missing field' defect] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# Real, general bug found live (2026-08-07, task039, HUMAN_DECISION deep-push, real task_id
# e447a03a-939a-4061-a0a7-d7483b7f6398): a sibling shape to
# `_filter_hallucinated_out_of_scope_field_required_findings` above, not caught by it --
# `_MISSING_FIELD_GATE_RE` requires phrasing like "is required"/"missing"/"must be defined", but
# this real finding used "the goal text ALSO requires X ... creating a contradiction", which none
# of those patterns match. This is also NOT caught by `_filter_hallucinated_scope_findings`
# (the very first sibling in this family): that filter only fires when the goal DOESN'T genuinely
# contain scope-limiting language at all -- here it genuinely does (the real "NOT yet in scope"
# list, plus the clarifying sentence manager/loop.py now appends explaining the omission is
# correct, see that file's own comment) -- so it correctly bails out and leaves the finding alone,
# with no other filter positioned to catch a reviewer that reads the SAME goal, sees the SAME
# clarifying language, and still concludes "the goal is self-contradictory" anyway. Targets that
# exact residual shape directly: a blocking finding using contradiction/incoherence framing whose
# own quoted names are ALL explainable by this round's real not-yet-in-scope list (via the same
# substring-or-token-overlap match `_name_matches_out_of_scope_label` already uses) is not a real
# defect -- the goal explicitly anticipates and resolves this exact framing; a Code-Review verdict
# that still reaches it is the hallucination, not the code.
_GOAL_CONTRADICTION_CLAIM_RE = re.compile(
    r"\bcontradict\w*\b|\birreconcilable\b|\bincoherent\b|\bimpossible to satisfy\b",
    re.IGNORECASE,
)


def _name_is_exact_match_to_out_of_scope_label(name: str, label: str) -> bool:
    """The EXACT-match slice of `_name_matches_out_of_scope_label()`'s own logic (substring
    containment either direction, case-insensitive) -- split out so a caller can tell "this name
    IS literally the real not-yet-in-scope label, verbatim" apart from a merely fuzzy,
    token-overlap match. See `_filter_hallucinated_goal_self_contradiction_findings()`'s own
    docstring for why this distinction matters.
    """
    name = name.lower()
    label = label.lower()
    return name in label or label in name


def _filter_hallucinated_goal_self_contradiction_findings(
    findings: list[ReviewFinding], goal: str, scope: RoundScope | None = None,
) -> list[ReviewFinding]:
    if scope is not None:
        not_yet_in_scope_labels = scope.not_yet_in_scope
    else:
        match = _REVIEW_NOT_YET_IN_SCOPE_RE.search(goal or "")
        if not match:
            return findings
        not_yet_in_scope_labels = [
            label.strip().strip("'\"").lower() for label in match.group(1).split(",") if label.strip()
        ]
    if not not_yet_in_scope_labels:
        return findings

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _GOAL_CONTRADICTION_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        candidate_names = _extract_candidate_identifier_names(f.explanation)
        matched_names = [
            name for name in candidate_names
            if any(_name_matches_out_of_scope_label(name, label) for label in not_yet_in_scope_labels)
        ]
        # Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): the
        # original >=2-distinct-names bar was calibrated against a real false-positive shape
        # where ONE finding incidentally quoted the round's own CURRENT focus label alongside a
        # genuine not-yet-in-scope one -- but it also silently blocks the single most common real
        # shape this filter exists for: Code-Review naming exactly ONE not-yet-in-scope
        # constraint label VERBATIM per finding (e.g. "the NOT-yet-in-scope list includes
        # 'ticket_access_restriction'..."), one label per finding, two separate findings each
        # individually failing the >=2 bar even though EACH is an exact, unambiguous, zero-doubt
        # quote of a real label. An EXACT substring match (the strong half of `_name_matches_out_
        # of_scope_label`'s own logic, split out as `_name_is_exact_match_to_out_of_scope_label`)
        # is already unambiguous evidence on its own -- unlike a fuzzy token-overlap match (the
        # weak half, which genuinely can coincidentally hit an unrelated focus label), it needs no
        # second corroborating name. Downgrades on EITHER >=1 exact match OR the original >=2
        # (exact-or-fuzzy) bar, preserving the original fuzzy-match conservatism unchanged.
        exact_matches = [
            name for name in candidate_names
            if any(_name_is_exact_match_to_out_of_scope_label(name, label) for label in not_yet_in_scope_labels)
        ]
        if len(set(exact_matches)) >= 1 or len(set(matched_names)) >= 2:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- every name this finding cites resolves to this "
                    f"round's own real NOT-yet-in-scope list; the goal explicitly explains this is "
                    f"the OVERALL multi-round plan's wording, not a genuine self-contradiction, "
                    f"and that this round's own correct omission of them is not a defect] "
                    f"{f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# Real, general bug found live (2026-08-07, task041, HUMAN_DECISION deep-push, real task_id
# a38df0e6-57ee-4e2e-b4ab-c07017f634f0): manager/tools.py's run_code_review_diff() now appends an
# "IMPORTANT: [...] already exist as REAL, LIVE, FUNCTIONING field(s) ... Do NOT flag [...] as
# missing" sentence to the goal text whenever Build's own collision-autofix correctly stripped a
# redundant field declaration -- but confirmed live, this pure textual instruction alone is NOT
# reliably followed (the exact same "explained in the prompt but still not respected" failure mode
# already seen for the not-yet-in-scope sentence, which needed a deterministic backstop filter too,
# see `_filter_hallucinated_out_of_scope_field_required_findings` above). Code-Review still flagged
# "the diff contains no field definition ... for 'project_count'" as blocking despite the sentence
# naming that exact field. Same deterministic backstop discipline as that sibling.
_COLLISION_SATISFIED_MARKER_RE = re.compile(
    r"IMPORTANT:\s*\[(.*?)\]\s*already exist as REAL, LIVE, FUNCTIONING",
)


def _filter_hallucinated_collision_satisfied_field_missing_findings(
    findings: list[ReviewFinding], goal: str,
) -> list[ReviewFinding]:
    match = _COLLISION_SATISFIED_MARKER_RE.search(goal or "")
    if not match:
        return findings
    satisfied_labels = [
        label.strip().strip("'\"").lower() for label in match.group(1).split(",") if label.strip()
    ]
    if not satisfied_labels:
        return findings

    # Deliberately no gate-phrase regex here (unlike the sibling out-of-scope filter above): the
    # collision marker itself only ever exists in goal text for THIS specific real round (parsed
    # fresh from manager/tools.py's own narrow, task-specific sentence), a much rarer and more
    # specific signal than "not yet in scope" prose -- confirmed live, the real task041 finding's
    # own phrasing ("the diff contains no field definition ... implementation") doesn't contain
    # any of "missing/absent/is required/must be defined" at all, so gating on that regex here
    # would silently never fire for the exact real defect this filter exists to catch.
    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        candidate_names = _extract_candidate_identifier_names(f.explanation)
        matched_names = sorted({
            name for name in candidate_names
            if any(_name_matches_out_of_scope_label(name, label) for label in satisfied_labels)
        })
        # Real, confirmed live follow-on gap (2026-08-07, task041, same real task, next
        # relaunch -- real task_id confirmed via redis): once the quoted-name match above fixed
        # the FIRST real finding shape, a second real Code-Review finding for the SAME field
        # recurred with NO quoted field name at all ("no computed method or field definition is
        # present"), so there was nothing for the quoted-identifier match to catch. Deliberately
        # narrow fallback: only when there is EXACTLY ONE collision-satisfied field (no ambiguity
        # about which field an unnamed complaint could mean), the finding's location is a
        # models.py-shaped file (the only file this collision could ever apply to), and the
        # explanation uses field/method-missing-shaped language.
        #
        # Real, confirmed live regression (2026-08-07, task041 v14): once bare snake_case
        # identifiers were ALSO extracted as candidates (to catch unquoted not-yet-in-scope
        # findings elsewhere), this fallback's own original `not candidate_names` guard broke --
        # a genuinely relevant finding mentioning the satisfied field's own real compute pattern
        # by an incidental bare identifier ("no such field or computation method is implemented
        # ... using search_count") now always has non-empty candidate_names, even though
        # 'search_count' isn't itself a genuinely DIFFERENT, unrelated concern. Replaced with a
        # weaker but still real safety check: every extracted candidate must share AT LEAST ONE
        # token (even a generic one) with the single satisfied label -- ruling out a genuinely
        # unrelated field/method mentioned by name (which would share zero tokens), while still
        # tolerating an incidental, related technical term like 'search_count'.
        no_unrelated_candidates = all(
            _significant_tokens(name) & _significant_tokens(satisfied_labels[0])
            for name in candidate_names
        )
        if (
            not matched_names and len(satisfied_labels) == 1 and no_unrelated_candidates
            and f.location.endswith(".py")
            and re.search(r"\bfield\b|\bmethod\b|\bcompute\b", f.explanation, re.IGNORECASE)
            and _MISSING_FIELD_GATE_RE.search(f.explanation)
        ):
            matched_names = satisfied_labels
        if matched_names:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {matched_names!r} is/are confirmed to already "
                    f"exist as real, live, functioning field(s) on the actual target model; this "
                    f"round's own code correctly did not redeclare them to avoid a real collision, "
                    f"which is not a missing-implementation defect] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_STALE_SCOPE_EXCLUSION_CLAIM_RE = re.compile(
    r"['\"]([\w]+)['\"]\s+is\s+(?:explicitly\s+)?not\s+in\s+scope|"
    r"['\"]([\w]+)['\"]\s+is\s+(?:explicitly\s+)?(?:excluded|out\s+of\s+scope)",
    re.IGNORECASE,
)


def _filter_hallucinated_stale_scope_exclusion_findings(
    findings: list[ReviewFinding], goal: str, scope: RoundScope | None = None,
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-07-28, Phase 28C,
    `school_student` task, the `menu_structure` round): a genuinely
    different, more specific failure than
    `_filter_hallucinated_scope_findings()` above -- that filter only
    ever checks WHETHER the goal contains genuine scope language at
    all, trusting every finding as-is once it does; it has no way to
    catch Code-Review naming the WRONG specific constraint label. Here,
    the round's own real, current contract explicitly said only
    `['security_groups', 'record_rules', 'computed_age_field',
    'demo_data', 'automated_tests']` are not yet in scope (`student_
    views` had already, genuinely been satisfied by an EARLIER round
    and its real content -- verified live directly against the real
    committed views.xml -- was completely correct and present) -- yet
    Code-Review still blocked the round claiming "'student_views' is
    explicitly NOT in scope for this round," a stale echo from
    constraint #1's own history still lingering in `contract.rules`
    (which accumulates every prior round's own failure text verbatim --
    see `_filter_hallucinated_scope_findings()`'s own docstring for why
    only `contract.goal` can ever be trusted as the real, current
    source of scope truth). Same root fix, one level more specific:
    extract the EXACT constraint label Code-Review's own finding names
    as excluded, and check it against the round's own real, current
    not-yet-in-scope list -- if the named label genuinely ISN'T there
    (already satisfied, or was never a real constraint at all), the
    claim is stale/hallucinated and downgraded; a label that IS
    genuinely still excluded is left completely untouched.
    """
    # P12 Tier S/A item 6: same RoundScope-first, regex-fallback discipline as the two
    # siblings above.
    if scope is not None:
        if not scope.is_decomposed:
            return findings  # a non-decomposed task never has "not yet in scope" prose to begin with
        real_not_yet_in_scope = set(scope.not_yet_in_scope)
    else:
        match = _REVIEW_NOT_YET_IN_SCOPE_RE.search(goal or "")
        if not match:
            return findings
        real_not_yet_in_scope = {
            label.strip().strip("'\"").lower() for label in match.group(1).split(",") if label.strip()
        }

    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        claim_match = _STALE_SCOPE_EXCLUSION_CLAIM_RE.search(f.explanation)
        claimed_label = (
            (claim_match.group(1) or claim_match.group(2)).lower() if claim_match else None
        )
        if claimed_label and claimed_label not in real_not_yet_in_scope:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {claimed_label!r} is NOT actually in this "
                    f"round's own real, current not-yet-in-scope list (it was either already "
                    f"satisfied by an earlier round, or never a real constraint) -- this claim "
                    f"is a stale echo of an earlier round's own history, not the round's real, "
                    f"current scope] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_FUNCTIONALLY_EMPTY_CLAIM_RE = re.compile(
    r"functionally empty|adds nothing|doesn.t (?:meaningfully )?progress|"
    r"does not (?:meaningfully )?progress|no (?:real |meaningful )?(?:changes?|progress)|"
    r"doesn.t (?:actually )?(?:address|satisfy)|does not (?:actually )?(?:address|satisfy)",
    re.IGNORECASE,
)

_ASSESSMENT_AFFIRMS_NO_ISSUE_RE = re.compile(
    r"correctly (?:focuses|implements|adheres|respects)|adhering to|"
    r"avoiding out-of-scope|without adding (?:the )?out-of-scope|no (?:blocking )?issues?|"
    r"(?:respects|meets|matches) (?:the )?(?:round's own )?scope|"
    # Real, confirmed live rephrasing (2026-08-06, task030's own v10 real attempt): the same
    # "no real issue" conclusion, phrased as "only implementing X as requested/required" without
    # any of the exact wordings above.
    r"only implement\w* .{0,60}(?:as (?:requested|required)|\bfor this round\b)",
    re.IGNORECASE,
)
_ASSESSMENT_HAS_REAL_PROBLEM_RE = re.compile(
    r"\b(?:must|need[s]? to|should) (?:be )?(?:fix|address|correct|resolv)|"
    r"\b(?:critical|blocking|missing|incorrect|broken|fail(?:s|ed)?|invalid|does not exist|"
    r"doesn.t exist|incomplete|empty|violat)",
    re.IGNORECASE,
)


# Mirrors specialists/build/specialist.py's own _METHOD_DEF_NAME_RE/_MODEL_FIELD_DEF_RE/
# _MODEL_CLASS_BLOCK_RE exactly -- duplicated, not imported, per this codebase's own
# manager/specialists layering.
_METHOD_DEF_NAME_RE = re.compile(r"^\s*def\s+(\w+)\s*\(", re.MULTILINE)
_MODEL_FIELD_DEF_RE = re.compile(r"^\s*(\w+)\s*=\s*fields\.", re.MULTILINE)
_MODEL_CLASS_BLOCK_RE = re.compile(
    r"^class\s+\w+\(models\.\w+\):.*?(?=^class\s|\Z)", re.MULTILINE | re.DOTALL,
)
_LABEL_WORD_RE = re.compile(r"[a-z]+")


def _label_words(label: str) -> set[str]:
    return set(_LABEL_WORD_RE.findall(label.lower()))


def _filter_hallucinated_current_round_conflated_with_deferred_labels_findings(
    findings: list[ReviewFinding], files: dict[str, str], scope: RoundScope | None,
) -> list[ReviewFinding]:
    """Real, general, structural fix (2026-08-06, Phase 30 backlog pass, task030): the SAME
    underlying self-contradiction as `_filter_hallucinated_findings_contradicted_by_own_overall_
    assessment` above kept recurring under a NEW prose rephrasing every real re-run (3 confirmed
    distinct wordings across 3 real task_ids in one night: "So it respects the scope", "matches
    the scope constraint... without adding the out-of-scope Y", "matches the technical spec for
    the '<current_focus>' focus. However, the task goal explicitly states..."). Chasing each new
    rephrasing with another regex is the exact "whack-a-mole" risk this file's own sequence-
    pattern filter already documents as inherent to prose-matching. This filter instead checks
    the underlying FACT directly, deterministically, regardless of how Code-Review phrases its
    own confused reasoning: a blocking finding that names this round's own real `current_focus`
    constraint alongside `not_yet_in_scope` labels is only ever legitimate if the round's own
    real generated content actually contains a new method/field whose name shares real,
    substantive words with one of those DEFERRED labels (i.e., Build genuinely did implement
    something from the deferred list). When NONE of the deferred labels correspond to anything
    actually new in the round's own real models.py, the finding's own premise -- that deferred
    work was implemented -- is factually false, independent of its exact wording.

    Deliberately conservative: only fires on a decomposed round (`scope.is_decomposed`) with both
    a known `current_focus` and a non-empty `not_yet_in_scope`; only touches a finding that
    explicitly names the round's own `current_focus` label (proving it's about THIS shape, not
    an unrelated real defect); never touches a finding that doesn't mention `current_focus` at
    all, or a genuinely new method/field that DOES match a deferred label's own words.
    """
    if scope is None or not scope.is_decomposed or not scope.current_focus or not scope.not_yet_in_scope:
        return findings

    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    new_names = set(_METHOD_DEF_NAME_RE.findall(models_py)) | set(_MODEL_FIELD_DEF_RE.findall(models_py))

    # Real, confirmed bug found live while writing THIS filter's own test: comparing a new name's
    # words against a deferred label's words false-positives on a common, generic action verb
    # shared by pure coincidence (e.g. 'send' in both 'action_send' -- this round's own real,
    # legitimate current-focus method -- and the DEFERRED 'send_customer_button'). Requiring >=2
    # shared words instead over-corrects the other direction: a deferred label is a Manager-
    # generated DESCRIPTIVE label, not necessarily sharing more than one real word with the actual
    # Odoo method name that eventually implements it (confirmed live: the real method
    # `_message_post_after_hook` shares only 'hook' with its own real deferred label
    # 'state_change_hook' -- 'message'/'post'/'after' vs 'state'/'change' never overlap at all,
    # even though this genuinely IS the real target). Excluding a small set of generic verbs/nouns
    # that recur across many otherwise-unrelated identifiers closes the false-positive gap
    # directly, without breaking the genuine single-shared-word case.
    _GENERIC_IDENTIFIER_WORDS = {
        "action", "method", "send", "get", "set", "add", "update", "create", "do", "run",
    }

    def _name_substantially_matches_label(name_words: set[str], label_words: set[str]) -> bool:
        shared = (name_words & label_words) - _GENERIC_IDENTIFIER_WORDS
        return bool(shared)

    any_deferred_label_actually_implemented = any(
        _name_substantially_matches_label(_label_words(name), _label_words(label))
        for name in new_names for label in scope.not_yet_in_scope
    )
    if any_deferred_label_actually_implemented:
        return findings  # a deferred label's own words genuinely DO match new content -- real, leave it

    current_focus_lower = scope.current_focus.lower()
    filtered = []
    for f in findings:
        if f.severity == "blocking" and current_focus_lower in f.explanation.lower():
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding names this round's own current "
                    f"focus ({scope.current_focus!r}) alongside deferred constraints "
                    f"({scope.not_yet_in_scope!r}), but NONE of those deferred labels' own words "
                    f"actually appear in any new method/field this round's own real generated "
                    f"content defines -- the claimed premise (deferred work was implemented) is "
                    f"factually false, regardless of this finding's own exact wording] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_CONTEXT_KEY_IMPLIES_DEFERRED_DEPENDENCY_CLAIM_RE = re.compile(
    r"(?:context key ['\"]?(\w+)['\"]?.{0,80}(?:implies?|depends? on|dependency on|premature|"
    r"partial implementation))"
    r"|(?:sets?\s+['\"](\w+)=\w+['\"]\s+in the context.{0,80}(?:trigger for|implies?|depends? on|"
    r"dependency on|premature|partial implementation))",
    re.IGNORECASE,
)
_GOAL_CONTEXT_KEYS_LINE_RE = re.compile(r"context keys?\s*:\s*(.+?)(?:\.\s|$)", re.IGNORECASE | re.DOTALL)


def _filter_hallucinated_goal_spec_context_key_findings(
    findings: list[ReviewFinding], goal: str,
) -> list[ReviewFinding]:
    """Real, general fix (2026-08-06, Phase 30 backlog pass, task030, real task_id
    b2e48922-828d-41d3-8e5c-2df8c7b6ab98): a genuinely different shape from the two filters above
    -- not about implementing a deferred method/field, but Code-Review objecting to a context-dict
    KEY the round's own method sets (e.g. `mark_fieldjob_as_sent=True`), on the theory that setting
    a key some LATER round's hook will eventually read counts as "a partial implementation of a
    future constraint". Confirmed false against the real goal: the task's own explicit "Context
    keys:" specification literally lists that exact key as part of THIS method's own required
    behavior, not deferred at all -- Code-Review's objection contradicts the task's own stated
    spec, not a real defect. Deliberately narrow: only fires when (a) the finding names a specific
    context-key identifier via this exact "implies/depends on/premature/partial implementation"
    claim shape, and (b) that exact key literally appears in the goal's own "Context keys:" line --
    a context key NOT named there is never touched (a genuinely invented/wrong key is still real).
    """
    goal_text = goal or ""
    context_keys_match = _GOAL_CONTEXT_KEYS_LINE_RE.search(goal_text)
    if not context_keys_match:
        return findings
    goal_context_keys_lower = context_keys_match.group(1).lower()

    filtered = []
    for f in findings:
        claim_match = _CONTEXT_KEY_IMPLIES_DEFERRED_DEPENDENCY_CLAIM_RE.search(f.explanation) if f.severity == "blocking" else None
        claimed_key = (claim_match.group(1) or claim_match.group(2)).lower() if claim_match else None
        if claimed_key and claimed_key in goal_context_keys_lower:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {claimed_key!r} is literally listed in the "
                    f"task's own real 'Context keys:' specification as part of THIS method's own "
                    f"required behavior, not deferred -- setting it is following the goal's own "
                    f"explicit spec, not premature scope creep] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


# Real, confirmed gap (2026-08-07, HUMAN_DECISION push, task047, real task_id
# 69108d64-18bf-478a-933b-628bb821a6ab): a fourth, distinct self-contradiction shape -- Code-Review
# cites a PRIOR round's own error/feedback text ("the previous attempt error explicitly flagged
# this pattern...") as justification for a blocking finding on the CURRENT round, while the SAME
# finding's own explanation admits the current code no longer has that exact problem ("which is
# correct", "although the current code passes 'text_input' as an argument"). Confirmed real: the
# round 2 code genuinely fixed the round 1 defect (no longer accesses self.<field> inside an
# @api.model method), but Code-Review kept the finding blocking purely because it recognized the
# vocabulary from the earlier round's own error message, not because the current code is wrong.
_CITES_PREVIOUS_ATTEMPT_ERROR_RE = re.compile(
    r"previous attempt\S* (?:error|explicitly (?:flagged|state))", re.IGNORECASE,
)
_ADMITS_CURRENT_CODE_IS_FINE_RE = re.compile(
    r"which is correct|although (?:the )?current|while this specific implementation",
    re.IGNORECASE,
)


def _filter_hallucinated_findings_citing_stale_previous_attempt_error_text(
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Downgrades a blocking finding that explicitly cites a PRIOR round's own error/feedback text
    as its justification while its own explanation admits the current round's code no longer has
    that exact problem -- a real, confirmed self-contradiction: citing stale historical feedback
    instead of judging the actual, current, already-fixed content. Deliberately narrow: only fires
    when BOTH the "previous attempt error" citation AND a same-finding admission phrase are present
    together -- a finding that cites history without any such admission is left untouched (it may
    be correctly noting a defect that genuinely recurred).
    """
    filtered = []
    for f in findings:
        if (
            f.severity == "blocking"
            and _CITES_PREVIOUS_ATTEMPT_ERROR_RE.search(f.explanation)
            and _ADMITS_CURRENT_CODE_IS_FINE_RE.search(f.explanation)
        ):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding cites a PRIOR round's own error/"
                    f"feedback text as justification while its own explanation admits the current "
                    f"round's code no longer has that exact problem; judging the current, "
                    f"already-fixed content by stale historical feedback is not a real defect] "
                    f"{f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _filter_hallucinated_findings_contradicted_by_own_overall_assessment(
    findings: list[ReviewFinding], overall_assessment: str,
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-06, Phase 30 backlog pass, task030, real task_id
    10cbf536-937e-4477-9317-a03fbd435dae): a single review response's own `overall_assessment`
    directly, affirmatively contradicted its own `findings[0].severity == "blocking"` --
    `overall_assessment` said "The implementation correctly focuses only on the action_send method
    as required by the round's scope, adhering to the specified context keys and avoiding
    out-of-scope elements" (an unambiguous, real self-assessment that nothing is actually wrong),
    while the one finding stayed marked blocking. Reading the finding's own `explanation` field
    confirmed this wasn't a different, real issue the summary just omitted -- it was literal
    chain-of-thought ("So it respects the scope... Is there any issue with the code itself?...")
    that reasoned its way to "no real issue" and then simply never updated the severity field to
    match its own conclusion. Deterministic and general: fires only when `overall_assessment`
    contains clear, affirmative "no issue" language AND contains none of the negative/problem-
    describing vocabulary a genuinely blocking review's own summary would use -- a real review
    that both praises correct work AND names a real separate defect always uses at least one
    problem word somewhere in its summary, so this never touches a genuine mixed-result review.
    """
    if not overall_assessment:
        return findings
    if not _ASSESSMENT_AFFIRMS_NO_ISSUE_RE.search(overall_assessment):
        return findings
    if _ASSESSMENT_HAS_REAL_PROBLEM_RE.search(overall_assessment):
        return findings  # a genuinely mixed review (praise + a real problem) -- never touch it

    filtered = []
    for f in findings:
        if f.severity == "blocking":
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this review's own overall_assessment "
                    f"affirmatively states the implementation is correct and scope-respecting, "
                    f"with no problem language anywhere in it, directly contradicting this "
                    f"finding's own severity -- a self-contradicting review, not a real, "
                    f"independently-confirmed defect] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_FINDING_NAMES_CONTENT_AS_EXCLUDED_RE = re.compile(
    r"explicitly\s+(?:excluded|deferred|out\s+of\s+scope|not\s+in\s+scope)\s+"
    r"(?:from|for)\s+this\s+round|"
    r"which\s+(?:is|are)\s+explicitly\s+excluded",
    re.IGNORECASE,
)
_FINDING_TREATS_ABSENCE_AS_DEFECT_RE = re.compile(
    r"\blacks?\b|\bmissing\b|\bincomplete(?:ness)?\b|\bcontradiction\b|"
    r"does not (?:include|contain|have)|doesn.t (?:include|contain|have)",
    re.IGNORECASE,
)


def _filter_hallucinated_findings_blocking_on_self_named_excluded_scope(
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    service_ticket_model node): "The model 'oma.service.ticket' is defined but the task goal
    requires a 'state' field and workflow buttons, which are explicitly excluded from this
    round's scope, creating a contradiction in the model's completeness." -- a finding that
    names, IN ITS OWN TEXT, that the very thing it's complaining about is "explicitly excluded
    from this round's scope," and then blocks the round for that content's absence anyway. This
    needs no file inspection at all -- the finding is self-refuting on its own words: a round
    cannot simultaneously be required to omit something AND be blocked for omitting it.

    Deliberately narrow to the finding's own self-declaration (never infers scope from the goal
    or contract itself, which this filter doesn't even receive) -- only fires when the SAME
    finding both names something as explicitly excluded/deferred/out of scope AND uses
    absence-is-a-defect language (lacks/missing/incomplete/contradiction/does not include).
    """
    filtered = []
    for f in findings:
        if (
            f.severity == "blocking"
            and _FINDING_NAMES_CONTENT_AS_EXCLUDED_RE.search(f.explanation)
            and _FINDING_TREATS_ABSENCE_AS_DEFECT_RE.search(f.explanation)
        ):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding's own text names the missing "
                    f"content as explicitly excluded/deferred from this round's scope, then "
                    f"blocks the round for that same absence -- self-contradictory on its own "
                    f"words, not a real defect] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_FINDING_NAMES_NOT_YET_OR_SATISFIED_RE = re.compile(
    r"not[- ]yet[- ]in[- ]scope|already\s+satisfied",
    re.IGNORECASE,
)
_FINDING_TREATS_INCLUSION_AS_DEFECT_RE = re.compile(
    r"\bout of scope\b|\btouching\b|\bshould not (?:include|touch|modify|change)\b",
    re.IGNORECASE,
)


def _filter_hallucinated_findings_blocking_on_already_satisfied_carried_forward_content(
    findings: list[ReviewFinding], goal: str,
) -> list[ReviewFinding]:
    """A ground-truth-anchored sibling of `_filter_hallucinated_findings_blocking_on_carried_
    forward_out_of_scope_content()` immediately below -- that one is self-declaration-only (the
    SAME finding must both name an exclusion marker AND use specific inclusion-is-a-defect
    wording), deliberately narrow to avoid over-matching a genuinely blocking finding that
    happens to mention "round constraint" for a real reason. This one instead cross-checks
    against the REAL, structured "ALREADY satisfied" label list parsed directly out of `goal`
    (the exact same real signal `_filter_hallucinated_out_of_scope_field_required_findings`
    already trusts for the not-yet-in-scope side) -- since the anchor is a real, already-
    verified label match, no specific phrasing gate is needed on the "defect" side at all.

    Real, confirmed bug found live (2026-08-10, task e65381cc, parts_consumed_relation node):
    "The diff adds views, menus, and actions for oma.service.ticket and oma.equipment, which
    violates the explicit round constraint to only implement 'parts_consumed_relation' and
    exclude 'ticket_views_menu' and 'equipment_views_menu'" -- both `ticket_views_menu` and
    `equipment_views_menu` are genuinely, verifiably ALREADY SATISFIED constraints from earlier
    rounds of this SAME task; their own real content (views/menus/actions) MUST be carried
    forward unchanged (the goal text's own "MUST continue to hold -- do not remove or weaken
    them" sentence says so explicitly) -- carrying it forward is correct, required behavior,
    never a scope violation, regardless of how the complaint is phrased ("violates the round
    constraint", "exclude", or any future rewording).
    """
    match = _REVIEW_ALREADY_SATISFIED_RE.search(goal or "")
    if not match:
        return findings
    already_satisfied_labels = [
        label.strip().strip("'\"").lower() for label in match.group(1).split(",") if label.strip()
    ]
    if not already_satisfied_labels:
        return findings

    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        candidate_names = _extract_candidate_identifier_names(f.explanation)
        matched_names = sorted({
            name for name in candidate_names
            if any(_name_is_exact_match_to_out_of_scope_label(name, label) for label in already_satisfied_labels)
        })
        if matched_names:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {matched_names!r} is/are explicitly named by "
                    f"this round's own real ALREADY-satisfied constraint list; carrying that "
                    f"earlier, already-completed work forward unchanged is correct, required "
                    f"behavior (the goal text's own words: 'MUST continue to hold'), never a "
                    f"scope violation] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _filter_hallucinated_findings_blocking_on_not_yet_in_scope_carried_forward_content(
    findings: list[ReviewFinding], goal: str, scope: RoundScope | None = None,
) -> list[ReviewFinding]:
    """The NOT-yet-in-scope-side twin of `_filter_hallucinated_findings_blocking_on_already_
    satisfied_carried_forward_content()` immediately above -- same ground-truth-anchored
    approach (cross-checks against the REAL, structured not-yet-in-scope label list parsed
    directly out of `goal`, never self-declaration-only), for the sibling real shape: existing,
    unrelated content (e.g. a pre-existing security row for a model a LATER, not-yet-in-scope
    constraint will eventually modify) gets flagged as "violating" that later constraint's own
    scope merely because it's present in the diff, even though this round never touched it.

    Real, confirmed bug found live (2026-08-10, task e65381cc, parts_consumed_relation node):
    "The diff includes security records for oma.service.ticket, which violates the explicit
    round constraint to exclude 'ticket_access_restriction'" -- `ticket_access_restriction` is a
    real, genuine NOT-yet-in-scope constraint (the one that will eventually REMOVE this exact
    access row), but its own future work being not-yet-done is not the same thing as this
    round's diff being forbidden from containing the row AT ALL -- the row is legitimately
    unchanged, pre-existing content this round never touched.

    Real, confirmed FOLLOW-UP bug found live (2026-08-10, same task, ticket_status_decoration
    node, immediately after the fix above): a second finding on the SAME round -- "The task
    explicitly states that the base.group_user access row for oma.service.ticket must be
    removed, but the diff still retains it" -- named the real, concrete ARTIFACT
    (`base.group_user`) the not-yet-in-scope constraint will eventually touch, never the
    constraint's own internal graph LABEL (`ticket_access_restriction`) -- so the label-only
    match above never caught it. `_compose_focus_goal_text()`'s own goal text (manager/loop.py)
    already spells out this exact mapping one sentence later: "Concretely, this means: do not
    add or reference ANY view, form, tree, menu, action, or field for these real models in this
    round... even though they already exist and their fields are real: [...]" -- the exact same
    real, ground-truth sentence Bug 56's `_GOAL_FORBIDDEN_MODELS_SENTENCE_RE` already parses for
    a different filter. Reused here (never a second, separately-invented parse of the same
    sentence) so a finding naming either the constraint's own label OR the concrete real
    artifact name(s) the goal itself ties to that label is downgraded.
    """
    if scope is not None:
        not_yet_in_scope_labels = list(scope.not_yet_in_scope)
    else:
        match = _REVIEW_NOT_YET_IN_SCOPE_RE.search(goal or "")
        if not match:
            return findings
        not_yet_in_scope_labels = [
            label.strip().strip("'\"").lower() for label in match.group(1).split(",") if label.strip()
        ]
    forbidden_sentence_match = _GOAL_FORBIDDEN_MODELS_SENTENCE_RE.search(goal or "")
    if forbidden_sentence_match:
        not_yet_in_scope_labels.extend(
            name.strip().strip("'\"").lower()
            for name in forbidden_sentence_match.group(1).split(",") if name.strip()
        )
    if not not_yet_in_scope_labels:
        return findings

    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        candidate_names = _extract_candidate_identifier_names(f.explanation)
        matched_names = sorted({
            name for name in candidate_names
            if any(_name_is_exact_match_to_out_of_scope_label(name, label) for label in not_yet_in_scope_labels)
        })
        if matched_names:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {matched_names!r} is/are explicitly named by "
                    f"this round's own real NOT-yet-in-scope constraint list; merely carrying "
                    f"forward existing, unchanged content that a LATER constraint will "
                    f"eventually modify is never itself a scope violation this round] "
                    f"{f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_STRUCTURALLY_MANDATORY_CORE_FILES = (
    "__manifest__.py", "models/models.py", "security/ir.model.access.csv",
    # Real, confirmed gap found live (2026-08-11, task 18fca388, same night, same round):
    # the root __init__.py ('from . import models') and models/__init__.py (imports
    # models.py as the models.models submodule) are EQUALLY structurally mandatory --
    # every module this pipeline scaffolds always has both, deterministically written by
    # tools_odoo/module_dev/toolchain.py, never something Build can omit. Missing from the
    # original list, so a "the models import is unnecessary and should be removed" finding
    # targeting either of these two init files survived the filter untouched while the
    # otherwise-identical complaint against models/models.py itself was correctly caught --
    # confirmed live: 2 of the original 5 findings kept blocking the round for exactly this
    # reason, one round after this filter's own first fix already downgraded the other 3.
    "__init__.py", "models/__init__.py",
)
_FINDING_RECOMMENDS_DELETING_FILE_RE = re.compile(
    r"unnecessary|(?:should|must) be (?:deleted|removed)|delete this file entirely|"
    r"remove(?:d)? (?:the )?(?:import|directory)"
    # Real, confirmed gap found live (2026-08-11, same task, same night): Code-Review kept
    # rephrasing this exact complaint in ways that dodged the original wording match ("violating
    # the minimal module requirement and risking install errors", "must be deleted entirely to
    # prevent...", "requires no new access records" -- no single verb or noun choice was stable
    # across variants). The one CONSISTENT signal across every phrasing variant seen live is that
    # the finding quotes/paraphrases the goal's own "model, field, or view" sentence, or names
    # the goal's "no new X" instruction directly -- the actual root confusion (applying that
    # sentence to files it was never meant to cover), not any particular verb choice, so matching
    # on that is far more robust than trying to enumerate every way an LLM might phrase "get rid
    # of this." Deliberately still narrow/self-declaration-only (never a bare "downgrade by
    # default" for these files) -- a genuine, different problem (a syntax error, a wrong CSV row)
    # is very unlikely to ALSO happen to reference the goal's own minimal-module wording.
    r"|model,\s*field,\s*or\s*view|minimal module requirement|ONLY code this module needs|"
    r"requires no new|unreferenced (?:by the goal|and (?:is|should))",
    re.IGNORECASE,
)


def _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-11, task 18fca388, post_init_hook cleanup):
    `__manifest__.py`, `models/models.py`, and `security/ir.model.access.csv` are structurally
    mandatory on EVERY module this pipeline generates -- `_scoped_edit_missing_required_files()`
    (specialists/build/specialist.py) hard-fails a round that omits any of them, and
    `GeneratedModuleFiles.models_py`/`.security_csv` are non-optional fields with no way to
    represent "this module has none of these" at all. A goal whose own wording says something
    like "the ONLY code this module needs is X" (a genuinely reasonable, human-written
    simplicity instruction, NOT intended to contradict pipeline mechanics the goal's author
    never meant to relitigate) repeatedly caused Code-Review to flag these 3 always-required
    files as "unnecessary complexity" that "should be deleted" -- structurally impossible advice
    to follow (Build has no way to satisfy it without breaking the hard requirement), producing
    an unwinnable loop. Confirmed live: 3 consecutive rounds, byte-identical rejection, even
    after an explicit human resume note directly addressed to Code-Review, placed in the
    prompt's own "CRITICAL, confirmed corrections -- weigh these most heavily" section, failed
    to change the outcome -- the model's own generic "is this unnecessary complexity" review
    heuristic apparently outweighs a rules-list correction for this specific shape of complaint.
    Fixed deterministically instead, mirroring every other hallucinated-finding filter in this
    file: a finding is exempted only when it BOTH targets one of these 3 exact paths AND uses
    deletion/removal-recommending language -- a real, different problem with one of these files
    (e.g. a genuine syntax error, a wrong CSV row) uses neither this specific phrasing pattern
    together with nothing else actionable, so this stays narrow and self-declaration-only, same
    discipline as every sibling filter here.
    """
    filtered = []
    for f in findings:
        # Exact-path match (with an optional ':<line>' suffix), never bare substring
        # containment -- "__init__.py" is a substring of "controllers/__init__.py" too, and a
        # genuine, correct "delete this stale controllers/__init__.py" finding must never be
        # swallowed by a check meant only for the module-ROOT __init__.py.
        location_path = f.location.split(":", 1)[0].strip()
        # Real, confirmed gap found live (2026-08-11, same task, same night, after Bug 82/85's
        # own wording-robustness fixes already landed): the model does not reliably put the
        # mandatory path in `location` at all -- confirmed live, a finding whose own EXPLANATION
        # TEXT literally quotes "security/ir.model.access.csv" and uses exactly the already-
        # matched deletion language still had a `location` value that didn't match any of the
        # 5 exact paths, so this filter never engaged. Fallback: also treat the finding as
        # naming a mandatory file when one of the 5 exact path strings appears anywhere in its
        # own explanation text -- still narrow/self-declaration-only, since the deletion-
        # language requirement below must ALSO independently match.
        names_mandatory_file = location_path in _STRUCTURALLY_MANDATORY_CORE_FILES or any(
            path in f.explanation for path in _STRUCTURALLY_MANDATORY_CORE_FILES
        )
        if (
            f.severity == "blocking"
            and names_mandatory_file
            and _FINDING_RECOMMENDS_DELETING_FILE_RE.search(f.explanation)
        ):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding recommends deleting/emptying a "
                    f"file that is structurally MANDATORY on every module this pipeline "
                    f"generates ({_STRUCTURALLY_MANDATORY_CORE_FILES!r}); the pipeline itself "
                    f"hard-requires it to exist and be referenced regardless of what any task "
                    f"goal's own wording says, so this is never real, actionable advice] "
                    f"{f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_FINDING_CLAIMS_FALSE_CIRCULAR_IMPORT_RE = re.compile(
    r"circular import", re.IGNORECASE,
)


def _filter_hallucinated_findings_claiming_the_standard_module_root_init_is_a_circular_import(
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-11, task 18fca388, one round after the
    structurally-mandatory-core-files filter above): "File imports 'models' from itself ('from .
    import models') which is a circular import error and will crash the module import." --
    `from . import models` in a module's own root `__init__.py`, importing its OWN `models/`
    subpackage, is the single most standard, universal pattern in every Odoo module ever
    generated by this pipeline (see `tools_odoo/module_dev/toolchain.py`'s own deterministic
    scaffold-fix step, unconditionally writing exactly this line) -- it is NEVER a circular
    import (a parent package importing a child submodule it itself defines is the normal,
    correct direction; nothing in `models/models.py` imports back from the package root), so
    this claim is a pure hallucination, a different failure shape from the "unnecessary
    complexity" family the sibling filter above handles (this one asserts a false, specific
    TECHNICAL defect rather than objecting to the file's mere existence). Scoped exactly to
    `__init__.py` (the module root) -- a genuine circular-import finding naming two real,
    different files (e.g. 'models/a.py' and 'models/b.py' importing each other) is a completely
    different shape and must never be touched here.
    """
    filtered = []
    for f in findings:
        location_path = f.location.split(":", 1)[0].strip()
        if (
            f.severity == "blocking"
            and location_path == "__init__.py"
            and _FINDING_CLAIMS_FALSE_CIRCULAR_IMPORT_RE.search(f.explanation)
        ):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding claims the module-root "
                    f"__init__.py's own standard 'from . import models' line is a circular "
                    f"import; a parent package importing its own child submodule is never "
                    f"circular (nothing in models/models.py imports back from the package "
                    f"root), and this exact line is unconditionally, deterministically "
                    f"scaffolded on every module this pipeline generates] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _filter_hallucinated_findings_blocking_on_carried_forward_out_of_scope_content(
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node): the
    OPPOSITE-direction sibling of `_filter_hallucinated_findings_blocking_on_self_named_excluded_
    scope()` immediately above -- that one catches a finding complaining that excluded content is
    MISSING; this one catches a finding complaining that merely CARRYING FORWARD unchanged,
    already-legitimate content (a security CSV row from an already-satisfied constraint, or one
    needed for THIS round's own new views to even be usable) counts as "touching" or "including"
    out-of-scope work. Confirmed live, verbatim: "The diff includes access rows for
    oma.service.ticket and oma.equipment, but 'ticket_access_restriction' is explicitly
    NOT-yet-in-scope and must NOT be implemented, and 'equipment_views_menu' is already
    satisfied, so touching security records is out of scope" -- every round's own diff
    necessarily represents the FULL current state of shared files (security csv, manifest),
    never just this round's own new additions; carrying forward unchanged content is normal,
    required behavior, never a scope violation on its own. This finding shape doesn't use
    absence-is-a-defect language at all (no "missing"/"lacks"/"contradiction"), so the sibling
    filter's own `_FINDING_TREATS_ABSENCE_AS_DEFECT_RE` never matches it -- needs its own,
    presence-is-a-defect counterpart.

    Deliberately narrow, same self-declaration-only discipline as its sibling: only fires when
    the SAME finding both names a real not-yet-in-scope/already-satisfied marker AND uses
    inclusion/touching-is-a-defect language -- never infers scope from the goal/contract itself
    (which this filter doesn't receive), never touches a finding using neither phrasing.
    """
    filtered = []
    for f in findings:
        if (
            f.severity == "blocking"
            and _FINDING_NAMES_NOT_YET_OR_SATISFIED_RE.search(f.explanation)
            and _FINDING_TREATS_INCLUSION_AS_DEFECT_RE.search(f.explanation)
        ):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding treats merely CARRYING FORWARD "
                    f"unchanged, already-legitimate content (from an already-satisfied "
                    f"constraint, or content this round's own new work still needs) as a scope "
                    f"violation -- every round's diff necessarily represents the full current "
                    f"state of shared files, not just this round's own new additions, so mere "
                    f"inclusion is never itself a defect] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_GOAL_FORBIDDEN_MODELS_SENTENCE_RE = re.compile(
    r"do not add or reference any view, form, tree, menu, action, or field for these real "
    r"models in this round[^:]*: \[(.*?)\]",
    re.IGNORECASE,
)
_FINDING_QUOTES_FORBIDDEN_MODELS_TEMPLATE_RE = re.compile(
    r"do not add or reference any view,?\s*form,?\s*tree,?\s*menu,?\s*action,?\s*or field for "
    r"these real models in this round",
    re.IGNORECASE,
)
_DOTTED_MODEL_LIKE_NAME_RE = re.compile(r"\b[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*){1,}\b")


def _filter_hallucinated_fabricated_forbidden_model_quote_findings(
    findings: list[ReviewFinding], goal: str,
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node,
    immediately after Bugs 54/55 correctly fixed the underlying goal-text self-contradiction):
    once the real, current goal text no longer forbade `oma.service.ticket` at all (confirmed
    directly, live, by reading the exact prompt), Code-Review STILL blocked the round quoting
    "the task explicitly states 'do not add or reference ANY view, form, tree, menu, action, or
    field for these real models in this round... oma.service.ticket'" -- a quote that does not
    exist anywhere in the actual, current goal text at all. This is a genuine fabrication, not a
    reading-comprehension gap this session's other filters address: the reviewer invented a
    quote attributing a specific forbidden model to OUR OWN known, exact template sentence
    (`_compose_focus_goal_text()`'s own "Concretely, this means: do not add..." wording,
    manager/loop.py) that the real, live prompt never actually names.

    Deterministic and narrow: only fires when (a) the finding's own text closely paraphrases/
    quotes this pipeline's own known exact template sentence (a very distinctive phrase no other
    source would ever independently produce) AND (b) the finding also names a dotted,
    model-shaped identifier (e.g. `oma.service.ticket`) that is NOT present in the REAL forbidden
    -models list this round's own goal text actually contains (parsed directly from `goal` via
    the exact same template, never guessed at). A model genuinely, currently forbidden and
    correctly cited is never touched -- only a model the real prompt does NOT currently forbid,
    falsely attributed to that exact sentence, is downgraded.
    """
    if not goal:
        return findings
    sentence_match = _GOAL_FORBIDDEN_MODELS_SENTENCE_RE.search(goal)
    real_forbidden_models = set()
    if sentence_match:
        real_forbidden_models = {
            name.strip().strip("'\"").lower() for name in sentence_match.group(1).split(",") if name.strip()
        }
    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _FINDING_QUOTES_FORBIDDEN_MODELS_TEMPLATE_RE.search(f.explanation):
            filtered.append(f)
            continue
        cited_models = {m.lower() for m in _DOTTED_MODEL_LIKE_NAME_RE.findall(f.explanation)}
        fabricated = [m for m in cited_models if m not in real_forbidden_models]
        if fabricated:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding quotes this pipeline's own "
                    f"'do not add or reference...' template sentence as forbidding "
                    f"{sorted(fabricated)!r}, but the REAL, current goal text's own forbidden-"
                    f"models list does not actually name {sorted(fabricated)!r} at all -- a "
                    f"fabricated quote, not a real constraint] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_MISSING_CONTENT_CLAIM_RE = re.compile(
    r"missing|is not (?:present|included|defined)|must (?:also )?contain|"
    r"does not (?:contain|include)|has been (?:dropped|removed|deleted)",
    re.IGNORECASE,
)
_QUOTED_IDENTIFIER_RE = re.compile(r"['\"]([a-zA-Z][\w.]*)['\"]")


def _filter_hallucinated_missing_content_findings_against_real_files(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node): after this same task's own real content-
    preservation bug (an earlier round's `data/sequences.xml` genuinely dropping the equipment
    model's own `seq_equipment` record) was fixed, Code-Review kept repeating the IDENTICAL
    claim -- "Missing the existing 'seq_equipment' record required by the 'equipment_registry'
    constraint" -- against a LATER round whose real, committed `data/sequences.xml` genuinely,
    verifiably contained BOTH records (confirmed live via direct Gitea inspection of the exact
    commit Code-Review was reviewing). This is the identical hallucination SHAPE already fixed
    once for a "SyntaxError" claim (`_filter_hallucinated_syntax_error_findings_against_valid_
    code` immediately below) -- Code-Review's own accumulated round-history context (this exact
    complaint was genuinely true several rounds ago) gets repeated reflexively rather than
    re-verified against THIS round's own real content.

    Deliberately general rather than another narrow, wording-specific regex (matching this
    codebase's own stated policy: "any new Code-Review anti-hallucination need should default to
    a structural ground-truth check ... rather than another regex" -- see
    `_filter_hallucinated_functionally_empty_findings`'s own docstring): a "missing X" claim
    names its own specific identifier(s) in quotes (a record id, a sequence code, a field name)
    -- if the identifier(s) the finding names AS THE MISSING THING are directly, verifiably
    present somewhere in the real generated files, the claim that it's "missing" is
    definitionally false, regardless of phrasing.

    Real, confirmed refinement (2026-08-09, same live incident): the first version of this
    filter required EVERY quoted identifier-shaped string in the finding to be present -- but a
    real finding's own prose often ALSO quotes a constraint/plan label that names WHY something
    matters (e.g. "...required by the 'equipment_registry' constraint"), which never appears
    literally in generated file content at all (it's a planning label, not a real identifier),
    causing a real false-negative on the exact live bug this filter exists to catch. Requires at
    least one identifier-shaped quoted string (contains `_` or `.`, ruling out short/generic
    quoted words that would make this too permissive) and only ANY (not all) of them needs to be
    found -- deliberately favors closing the confirmed real hallucination pattern over the
    rarer, more awkward phrasing of a single finding naming two DIFFERENT missing things at
    once (which no real finding observed so far has ever actually done).
    """
    if not any(
        f.severity == "blocking" and _MISSING_CONTENT_CLAIM_RE.search(f.explanation)
        for f in findings
    ):
        return findings
    combined_content = "\n".join(files.values())

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _MISSING_CONTENT_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        quoted = _QUOTED_IDENTIFIER_RE.findall(f.explanation)
        candidate_ids = [q for q in quoted if len(q) >= 3 and ("_" in q or "." in q)]
        matched_ids = [cid for cid in candidate_ids if cid in combined_content]
        if matched_ids:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- every specific identifier this finding "
                    f"claims is missing ({matched_ids!r}) was directly verified to be "
                    f"genuinely present in the real generated files, not taken on faith] "
                    f"{f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_DUPLICATE_METHOD_CLAIM_RE = re.compile(
    r"defined\s+twice|defined\s+more\s+than\s+once|duplicate\s+(?:method|definition)|"
    r"redefine[sd]?|overwrit(?:e|ing|ten)",
    re.IGNORECASE,
)


def _filter_hallucinated_duplicate_method_findings_against_real_code(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node, the SAME live incident as the sibling
    `_filter_hallucinated_missing_content_findings_against_real_files` immediately above): a
    "defined twice" claim ("The 'create' method in ServiceTicket is defined twice in the
    previous attempt history... current diff shows it defined once but contains redundant list
    comprehension lines...") that EXPLICITLY admits, in its own words, that the current diff no
    longer has the problem -- yet the existing narrow `_filter_hallucinated_findings_citing_
    stale_previous_attempt_error_text` filter requires the specific phrase "previous attempt...
    error" and one of three specific "admits current is fine" phrasings, neither of which this
    real, live rewording matched ("previous attempt history" / "current diff shows it defined
    once"). This closes the same real hallucination class the SAME day
    specialists/build/specialist.py's own `_validate_no_duplicate_method_definitions` was fixed
    for (whole-file duplicate scanning falsely flagging two DIFFERENT classes each defining
    their own method once) -- Code-Review's own equivalent check needs the identical per-class
    ground truth, not another narrow wording-specific regex.

    Deliberately general: extracts the quoted method name the finding names, then directly
    counts its real occurrences WITHIN EACH class block of the real generated models.py (the
    same per-class scoping `_validate_no_duplicate_method_definitions` now uses) -- if no class
    genuinely defines that method more than once, the "defined twice" claim is definitionally
    false, regardless of phrasing. Never downgrades when the claim's own quoted identifier
    isn't a real method name found anywhere, or when a class genuinely does define it twice.
    """
    if not any(
        f.severity == "blocking" and _DUPLICATE_METHOD_CLAIM_RE.search(f.explanation)
        for f in findings
    ):
        return findings
    models_py = next((content for path, content in files.items() if path.endswith("models.py")), "")
    if not models_py:
        return findings

    def _genuinely_duplicated_in_some_class(method_name: str) -> bool:
        for block_match in _MODEL_CLASS_BLOCK_RE.finditer(models_py):
            names = _METHOD_DEF_NAME_RE.findall(block_match.group(0))
            if names.count(method_name) > 1:
                return True
        return False

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _DUPLICATE_METHOD_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        # Real, confirmed follow-up bug found live (2026-08-10, task 07141af5's flagship run,
        # ticket_workflow_and_logging node): the original version here only ever extracted
        # QUOTED method names ("Method 'action_bulk_close' is defined twice...") -- the exact
        # same live claim recurred BARE, unquoted ("Method action_bulk_close is defined twice in
        # the ServiceTicket class (lines 68 and 105)..."), which `_QUOTED_IDENTIFIER_RE` can
        # never match at all, so `quoted_methods` stayed empty and the false claim was never
        # downgraded. Fixed by ALSO scanning every bare word-token in the finding's own text and
        # keeping any that are genuinely real method names in the real models.py -- the same
        # "must be a real, verified name" safety gate the quoted path already used, just no
        # longer requiring quote marks to find the candidate in the first place.
        quoted_methods = [
            q for q in _QUOTED_IDENTIFIER_RE.findall(f.explanation)
            if re.search(rf"^\s*def\s+{re.escape(q)}\s*\(", models_py, re.MULTILINE)
        ]
        bare_methods = [
            w for w in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", f.explanation)
            if re.search(rf"^\s*def\s+{re.escape(w)}\s*\(", models_py, re.MULTILINE)
        ]
        claimed_methods = sorted(set(quoted_methods) | set(bare_methods))
        if claimed_methods and not any(_genuinely_duplicated_in_some_class(m) for m in claimed_methods):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {claimed_methods!r} was directly verified "
                    f"against the real generated models.py to be defined exactly once within "
                    f"every real class, never duplicated within the same class] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_REDUNDANT_LINE_CLAIM_RE = re.compile(
    r"redundant|repeated\s+line|duplicate\s+line|generation\s+artifact|incomplete\s+cleanup",
    re.IGNORECASE,
)
# Broader than _QUOTED_IDENTIFIER_RE -- a redundant-LINE claim quotes an actual code snippet
# (spaces, brackets, operators), not just an identifier, e.g. "vals_list = [dict(v) for v in
# vals_list]".
_QUOTED_CODE_SNIPPET_RE = re.compile(r"['\"]([^'\"]{8,120})['\"]")


def _has_any_consecutive_duplicate_line(content: str) -> bool:
    """Ground truth for an unquoted redundant/duplicate-line claim -- mirrors
    `_autofix_dedupe_consecutive_identical_lines()`'s own build-side detection (specialists/
    build/specialist.py) exactly, duplicated rather than imported per this codebase's own
    manager/specialists layering: a non-blank, non-comment line immediately followed by an
    identical line. If no such line exists anywhere in the reviewed files, a "redundant"/
    "duplicate line" claim has nothing real to point at, regardless of how it's phrased.
    """
    prev_stripped: str | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped == prev_stripped:
            return True
        prev_stripped = stripped if stripped and not stripped.startswith("#") else None
    return False


def _filter_hallucinated_redundant_line_findings_against_real_code(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5-9a4e-41b6-93ea-8b7af04fea9c's
    flagship run, service_ticket_model node, the third real hallucination shape found in this
    same live incident, alongside the sibling missing-content and duplicate-method filters
    immediately above): a genuinely, deterministically-repeated line
    ("vals_list = [dict(v) for v in vals_list]" appearing twice/three times back to back) was
    correctly flagged and, after an explicit correction note, genuinely fixed down to a single
    occurrence -- confirmed live via direct Gitea inspection of the exact commit Code-Review was
    reviewing. Code-Review then repeated the IDENTICAL claim against that now-fixed content,
    this time explicitly self-describing it as recycled history ("...was flagged in previous
    rounds as a generation artifact") -- a real, confirmed instance of the same "citing stale
    round history instead of re-verifying against the current diff" pattern the narrower,
    exact-phrasing `_filter_hallucinated_findings_citing_stale_previous_attempt_error_text`
    filter already targets, but with wording ("previous rounds") that filter's own narrower
    "previous attempt... error" phrase never matches.

    Deliberately general: a "redundant"/"duplicate line"/"generation artifact" claim quotes the
    actual code snippet it considers redundant -- if that exact snippet appears at most ONCE in
    the real generated file (i.e., it is definitionally not "redundant" -- there is nothing left
    to deduplicate), the claim is false regardless of phrasing. Requires the quoted snippet to
    look like a real line of code (contains `=`, `(`, or `[` -- ruling out short, generic quoted
    prose that would make this too permissive) and counts its REAL occurrences directly in the
    combined file content -- a snippet that genuinely still appears 2+ times is never
    downgraded.
    """
    if not any(
        f.severity == "blocking" and _REDUNDANT_LINE_CLAIM_RE.search(f.explanation)
        for f in findings
    ):
        return findings
    combined_content = "\n".join(files.values())

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _REDUNDANT_LINE_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        snippets = [
            q for q in _QUOTED_CODE_SNIPPET_RE.findall(f.explanation)
            if any(ch in q for ch in "=([")
        ]
        # Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
        # service_ticket_model node, round 33): a redundant/duplicate-line claim can describe
        # the alleged duplication in PROSE only ("the previous lines duplicated the list
        # conversion unnecessarily") without quoting the actual code snippet at all -- the
        # `snippets`-based check above then has nothing to verify against and silently falls
        # through to `else: filtered.append(f)`, keeping a genuinely false claim as blocking
        # forever (confirmed live: the real committed models.py had already been fixed down to
        # a single, non-duplicated `vals_list = [dict(v) for v in vals_list]` line, yet this
        # exact unquoted claim kept recurring and escalated the round to a human decision).
        # Ground truth here is "does this file contain ANY consecutive duplicate line at all" --
        # a "redundant"/"duplicate line" claim is categorically false if the file has no such
        # line anywhere, regardless of whether the claim bothered to quote it.
        no_snippets_but_claim_is_unquoted = not snippets and not _has_any_consecutive_duplicate_line(combined_content)
        if (snippets and all(combined_content.count(s) <= 1 for s in snippets)) or no_snippets_but_claim_is_unquoted:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- the quoted code {snippets!r} was directly "
                    f"verified to appear at most once in the real generated files, so it is "
                    f"definitionally not redundant/duplicated, regardless of what an earlier, "
                    f"since-fixed round's own content may have looked like] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_OVERWRITE_GUARD_CLAIM_RE = re.compile(
    r"overwrit(?:e|ing|ten)|does\s+not\s+handle\s+the\s+case\s+where|already\s+provided",
    re.IGNORECASE,
)
_GUARD_ASSIGNMENT_RE = re.compile(
    r"if\s+not\s+[\w.\[\]'\"]*\.get\(\s*['\"](\w+)['\"]\s*\)\s*:", re.MULTILINE,
)


def _filter_hallucinated_overwrite_findings_against_guarded_assignment(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    service_ticket_model node): Code-Review claimed the create() override "does not handle the
    case where name is already provided, potentially overwriting it with a sequence number" --
    but the real generated code already reads `if not vals.get('name'): vals['name'] = ...`,
    the standard Python/Odoo idiom that definitionally never overwrites an already-truthy value
    (the same idiom the equipment model's own create() override already used successfully,
    confirmed via direct Gitea inspection). A second, ungrounded LLM-judge call invented a
    contradiction that isn't in the code.

    Ground truth: if the claim names a field (quoted) and complains about it being overwritten
    despite "already provided"/not handled, and the real file contains an
    `if not <expr>.get('<that field>'):` guard immediately preceding an assignment to that same
    field, the claim is false -- that guard is exactly "don't touch it if already provided."
    """
    if not any(
        f.severity == "blocking" and _OVERWRITE_GUARD_CLAIM_RE.search(f.explanation)
        for f in findings
    ):
        return findings
    combined_content = "\n".join(files.values())
    guarded_fields = set(_GUARD_ASSIGNMENT_RE.findall(combined_content))

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _OVERWRITE_GUARD_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        claimed_fields = [
            q for q in _QUOTED_IDENTIFIER_RE.findall(f.explanation) if q in guarded_fields
        ]
        if claimed_fields:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- field(s) {claimed_fields!r} are directly "
                    f"verified to be guarded by an `if not ...get(...)` check before "
                    f"assignment in the real generated code, so this cannot overwrite an "
                    f"already-provided value] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_DECORATION_FIELD_MISSING_CLAIM_RE = re.compile(
    r"decoration[-\w]*.{0,120}?references?\s+field|"
    r"decoration[-\w]*.{0,200}?does\s+not\s+exist|"
    r"references?\s+field\s+['\"]?\w+['\"]?.{0,80}?decoration",
    re.IGNORECASE | re.DOTALL,
)
_DECORATION_ATTR_VALUE_RE = re.compile(r'decoration-\w+="([^"]+)"')


def _filter_hallucinated_decoration_string_literal_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    ticket_list_view node): the SAME hallucination shape just fixed on the deterministic Build
    side (`_validate_decoration_attr_fields_exist_on_model`'s own fix, specialists/build/
    specialist.py -- a string literal like 'resolved' inside `state == 'resolved'` mistaken for
    a second field reference) recurred independently in Code-Review's own separate LLM call,
    even AFTER the Build-side validator was fixed: "decoration-success=\"state == 'resolved'\"
    references field 'resolved' which does not exist on the model... wait, let me re-read the
    history" -- the visible self-doubt in the finding's own text is itself a strong tell this
    is a confused, ungrounded claim, but the fix here is deterministic and doesn't depend on
    that tell: extract the claimed missing field name, and if it appears in the real views_xml
    ONLY inside a quoted string literal within a decoration-* attribute (never as an actual
    `<field name=...>` reference or a `fields.X(...)` declaration), it's a literal comparison
    value, not a field reference, regardless of which LLM call invents the same confusion.
    """
    if not any(
        f.severity == "blocking" and _DECORATION_FIELD_MISSING_CLAIM_RE.search(f.explanation)
        for f in findings
    ):
        return findings

    views_xml = "\n".join(
        content for path, content in files.items() if path.endswith(".xml") and content
    )
    if not views_xml or "decoration-" not in views_xml:
        return findings
    declared_fields = set(_MODEL_FIELD_DEF_RE.findall(
        next((c for p, c in files.items() if p.endswith("models.py")), "")
    ))
    real_view_field_refs = set(re.findall(r'<field\s+name="(\w+)"', views_xml))

    decoration_literal_values: set[str] = set()
    for expr in _DECORATION_ATTR_VALUE_RE.findall(views_xml):
        clean_expr = re.sub(r"&(?:lt|gt|amp|quot|apos);", " ", expr)
        decoration_literal_values |= set(re.findall(r"'([a-zA-Z_]\w*)'|\"([a-zA-Z_]\w*)\"", clean_expr))
    decoration_literal_values = {v for pair in decoration_literal_values for v in pair if v}

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _DECORATION_FIELD_MISSING_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        claimed_fields = [
            q for q in _QUOTED_IDENTIFIER_RE.findall(f.explanation)
            if q in decoration_literal_values
            and q not in declared_fields
            and q not in real_view_field_refs
        ]
        if claimed_fields:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {claimed_fields!r} appears in the real "
                    f"views_xml only as a quoted STRING LITERAL inside a decoration-* "
                    f"expression (e.g. `state == {claimed_fields[0]!r}`), never as an actual "
                    f"field reference or declaration -- this is a comparison value, not a "
                    f"missing field] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_FINDING_SELF_REPORTS_ALREADY_RESOLVED_RE = re.compile(
    r"(?:is|was)\s+(?:a\s+)?false\s+positive|"
    r"has\s+been\s+resolved|already\s+resolved|"
    r"is\s+actually\s+correct(?:\s+syntax)?|"
    r"which\s+is\s+a\s+false\s+positive",
    re.IGNORECASE,
)


def _filter_hallucinated_findings_that_self_report_as_already_resolved(
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    ticket_list_view node): after the decoration-* string-literal hallucination was explicitly
    corrected via a note, Code-Review's NEXT finding literally said, in its own words:
    "...decoration-success=\"state == 'resolved'\" is actually correct syntax for Odoo 16, but
    the previous review flagged it as referencing a missing field 'resolved' instead of the
    value 'resolved' of field 'state', which is a false positive that has been resolved by
    Operator's arbitration" -- and then kept `severity: "blocking"` anyway. The finding's own text
    affirmatively concludes there is no real issue; only its severity field failed to follow
    that conclusion. This is the same root failure mode as `_filter_hallucinated_findings_
    contradicted_by_own_overall_assessment` above, but manifesting WITHIN a single finding's own
    explanation instead of the separate `overall_assessment` field -- that filter's own
    `_ASSESSMENT_HAS_REAL_PROBLEM_RE` counter-check would false-positive here (this finding's
    text also contains "invalid" en route to reversing itself), so this filter instead keys on
    an explicit, narrow self-declaration of resolution/false-positive status, not the absence of
    problem words.
    """
    filtered = []
    for f in findings:
        if f.severity == "blocking" and _FINDING_SELF_REPORTS_ALREADY_RESOLVED_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding's own text explicitly declares "
                    f"itself a false positive/already resolved, but its severity field was never "
                    f"updated to match that conclusion] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_IR_RULE_CANNOT_GRANT_CLAIM_RE = re.compile(
    r"ir\.?rules?\s+(?:can\s+only|only\s+ever)\s+(?:ever\s+)?narrow|"
    r"(?:can\s+only|only\s+ever)\s+(?:ever\s+)?narrow.{0,60}(?:never|not)\s+grant|"
    r"(?:makes?|make)\s+(?:the|this)\s+rule\s+(?:ineffective|pointless|useless)|"
    r"rule\s+is\s+(?:likely\s+)?(?:pointless|ineffective|useless)\s+for\s+the\s+permission",
    re.IGNORECASE,
)
_IR_RULE_GROUP_QUOTED_RE = re.compile(r"['\"](\w*group\w*)['\"]", re.IGNORECASE)


def _filter_hallucinated_ir_rule_permission_capped_findings(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-09, task 07141af5's flagship run,
    ticket_access_rights node): after fixing the SAME wrong "any ir.rule-referenced group needs
    perm_unlink=1" assumption in FOUR separate deterministic Build-side functions (all sharing
    this exact premise: "an ir.rule can only ever narrow access already granted via access.csv,
    never grant it, so a denied perm_unlink makes the rule pointless"), Code-Review -- a
    SEPARATE LLM judge, never touched by those fixes -- kept independently reaching the exact
    same false conclusion regardless of what the real, already-correct generated content says:
    "The ir.rule 'rule_service_ticket_technician_own' restricts access for
    'group_field_technician', but the access.csv row for this group grants perm_unlink=0; ir.
    rules [can only narrow, never grant]." This premise is only true for the permission
    dimension a rule's own domain_force actually narrows -- a row-restricting rule (domain_force
    references `user.id`/`uid`, e.g. "technicians only see their own tickets") narrows WHICH
    ROWS are visible for read/write/create; it says nothing about delete, so a genuinely correct
    perm_unlink=0 baseline for that role is not "pointless," it's least-privilege working
    exactly as intended.

    Ground truth, not name-guessing: extracts the group xmlid the finding names, finds that
    SAME group's own `<record model="ir.rule">` block in the real security_xml, and only
    downgrades when that rule's own domain_force genuinely references the current user --
    a finding about a genuine elevation-gate rule (domain_force=[], no user-reference) is left
    completely untouched, since a denied perm_unlink there IS a real gap.
    """
    if not any(
        f.severity == "blocking" and _IR_RULE_CANNOT_GRANT_CLAIM_RE.search(f.explanation)
        for f in findings
    ):
        return findings
    security_xml = next((c for p, c in files.items() if p.endswith("security.xml")), "")
    if not security_xml or "ir.rule" not in security_xml:
        return findings
    rule_blocks = re.findall(
        r'<record[^>]*model="ir\.rule"[^>]*>(?:(?!</record>).)*?</record>', security_xml, re.DOTALL,
    )

    def _group_is_row_restricted(group_name: str) -> bool:
        group_bare = group_name.rsplit(".", 1)[-1]
        for block in rule_blocks:
            if group_bare not in block:
                continue
            domain_match = re.search(r'<field\s+name="domain_force">([^<]*)</field>', block)
            rule_domain = domain_match.group(1) if domain_match else ""
            if re.search(r"\buser\.(?:id|partner_id)\b|\buid\b", rule_domain):
                return True
        return False

    filtered = []
    for f in findings:
        if f.severity != "blocking" or not _IR_RULE_CANNOT_GRANT_CLAIM_RE.search(f.explanation):
            filtered.append(f)
            continue
        claimed_groups = [
            q for q in _IR_RULE_GROUP_QUOTED_RE.findall(f.explanation) if _group_is_row_restricted(q)
        ]
        if claimed_groups:
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- {claimed_groups!r} is scoped by an ir.rule "
                    f"whose own domain_force references the current user (a row-restricting "
                    f"rule, narrowing WHICH ROWS are visible for read/write/create, never "
                    f"implying delete capability) -- a perm_unlink=0 baseline for this group is "
                    f"correct least-privilege, not a pointless rule] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_SYNTAX_ERROR_CLAIM_RE = re.compile(
    r"syntax\s*error|invalid\s+syntax", re.IGNORECASE,
)


def _filter_hallucinated_syntax_error_findings_against_valid_code(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """Real, confirmed bug found live (2026-08-08, task 657697fc-f701-4932-a172-b0132da93cfa's
    flagship run, ticket_workflow node): round 1's real generated models.py genuinely had a
    SyntaxError (confirmed via the real sandbox log_tail: a dangling orphaned string fragment
    left over from a botched edit). Round 2 then genuinely, fully fixed it AND correctly added
    the whole ticket_workflow model/state machine on top -- confirmed directly against the real
    Gitea commit content, which compiles cleanly with no syntax error anywhere. Yet Code-Review
    repeated the IDENTICAL claim ("SyntaxError: invalid syntax due to missing opening
    parenthesis and quote in _sql_constraints definition") against this now-valid round 2 code,
    wrongly failing it -- and round 3, misled by that same false, repeated claim (fed back as
    "CRITICAL FIX REQUIRED" round-history text), threw away round 2's real, correct work
    entirely rather than fix a problem that no longer existed. This is exactly the "reactive
    lexical patch" trap this codebase's own filters try to avoid (see
    `_filter_hallucinated_functionally_empty_findings`'s docstring) -- rather than another
    regex trying to recognize this ONE claim's exact stale-context shape, this filter uses the
    general, structural ground truth every other syntax-error claim can also be checked against:
    does the actual generated Python file really fail to compile? `compile()` is authoritative
    here in a way no regex or LLM re-read of the diff can be -- if every real .py file in this
    round's own files compiles cleanly, a "syntax error" finding is definitionally false,
    regardless of how it was phrased or where it came from.
    """
    if not any(_SYNTAX_ERROR_CLAIM_RE.search(f.explanation) for f in findings if f.severity == "blocking"):
        return findings

    genuine_syntax_error = None
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            genuine_syntax_error = f"{path}: {exc}"
            break
    if genuine_syntax_error:
        return findings  # a real syntax error genuinely exists -- never downgrade

    filtered = []
    for f in findings:
        if f.severity == "blocking" and _SYNTAX_ERROR_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding claims a syntax error, but "
                    f"every real .py file in this round's own generated content was directly "
                    f"verified to compile() cleanly with no exception, a structural ground-"
                    f"truth check that overrides the claim rather than another wording-specific "
                    f"regex] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_XML_MALFORMED_CLAIM_RE = re.compile(
    r"not well[- ]formed|mismatched tag|not properly closed|"
    r"malformed\s+xml|invalid\s+xml|xml\s+is\s+(?:invalid|malformed)|"
    r"structure\s+is\s+invalid|hard\s+parse\s+failure|parse\s+error",
    re.IGNORECASE,
)


def _filter_hallucinated_xml_malformed_findings_against_valid_xml(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """The XML-file sibling of `_filter_hallucinated_syntax_error_findings_against_valid_code()`
    immediately above -- same "structural ground truth beats another wording-specific regex"
    philosophy, for XML content (views_xml/security_xml/extra_data_files) instead of `.py` files,
    which that function's own `compile()` check never covers at all.

    Real, confirmed bug found live (2026-08-10, task e65381cc, ticket_views_menu node, 3
    consecutive rounds): Code-Review repeatedly claimed the generated `views/views.xml` was
    malformed ("mismatched tag", "the root <odoo> tag is not properly closed or the structure is
    invalid, causing a hard parse failure") -- but the REAL, actually-committed content (read
    directly via `read_last_validated_commit()`, never trusted from the claim alone) parses
    cleanly with Python's own standard `xml.etree.ElementTree.fromstring()`, confirmed directly.
    Exactly the same "reactive lexical patch" trap the `.py` sibling already guards against:
    rather than adding yet another wording-specific regex, this checks the one real, structural
    ground truth available -- does the actual XML content really fail to parse?
    """
    if not any(_XML_MALFORMED_CLAIM_RE.search(f.explanation) for f in findings if f.severity == "blocking"):
        return findings

    import xml.etree.ElementTree as ET

    genuine_xml_error = None
    for path, content in files.items():
        if not path.endswith(".xml") or not content:
            continue
        try:
            ET.fromstring(content)
        except ET.ParseError as exc:
            genuine_xml_error = f"{path}: {exc}"
            break
    if genuine_xml_error:
        return findings  # a real XML parse error genuinely exists -- never downgrade

    filtered = []
    for f in findings:
        if f.severity == "blocking" and _XML_MALFORMED_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this finding claims the XML is malformed, "
                    f"but every real .xml file in this round's own generated content was "
                    f"directly verified to parse cleanly with xml.etree.ElementTree, a "
                    f"structural ground-truth check that overrides the claim rather than "
                    f"another wording-specific regex] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _filter_hallucinated_functionally_empty_findings(
    findings: list[ReviewFinding], files: dict[str, str], scope: RoundScope | None,
) -> list[ReviewFinding]:
    """P12 Tier S/A item 8 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    §4): the reference implementation of this priority's own stated policy -- "any new
    Code-Review anti-hallucination need should default to a structural ground-truth check
    ... rather than another regex" -- built for Bug #1's own re-grounded live symptom
    (`docs/planning/PHASE30_ARCH_A_FOUNDATION_COMBINATION_AUDIT_2026-07-30.md` Part 0): a
    finding phrased "This round's diff is functionally empty -- it adds nothing that satisfies
    the goal" (or any of its many equally-plausible rewordings) matched none of the 15 existing
    `_filter_hallucinated_*` filters' own narrow claim regexes, and reached
    `fold_code_review_findings()` as a live blocking finding, failing a round that was in fact
    genuinely, deliberately scoped to something small.

    Deliberately does NOT try to enumerate every possible "empty"/"doesn't progress" phrasing
    the way every sibling filter enumerates its own claim shape -- that's exactly the reactive-
    lexical-patch trap B Finding 7 named ("the next hallucination shape is filter #15, not yet
    written"). Instead: `_FUNCTIONALLY_EMPTY_CLAIM_RE` only needs to recognize the general
    SHAPE of an emptiness/no-progress complaint (broad, not exhaustive) -- the actual downgrade
    decision is grounded in real, structural evidence: whether this round's own current focus
    (`scope.current_focus`, e.g. `'computed_age_field'`) genuinely appears, keyword-by-keyword,
    in the real generated content. If it does, the diff structurally DOES address the round's
    own real focus, regardless of how Code-Review chose to phrase its doubt about that -- future
    rewordings of the same complaint are caught for free, with no new regex needed. Conservative
    when scope is unavailable or the focus can't be keyword-matched at all (no decomposed
    round, or a label too short/generic to search for): leaves findings untouched rather than
    guessing.
    """
    if scope is None or not scope.current_focus:
        return findings
    focus_keywords = [w for w in scope.current_focus.split("_") if len(w) > 2]
    if not focus_keywords:
        return findings
    all_content = "\n".join(files.values()).lower()
    matched_keywords = [kw for kw in focus_keywords if kw in all_content]
    if not matched_keywords:
        return findings  # can't structurally confirm -- leave the finding alone, conservative default

    filtered = []
    for f in findings:
        if f.severity == "blocking" and _FUNCTIONALLY_EMPTY_CLAIM_RE.search(f.explanation):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- this round's own real focus "
                    f"({scope.current_focus!r}) genuinely appears in the generated content "
                    f"(matched keyword(s): {matched_keywords}), directly contradicting a "
                    f"'functionally empty'/'doesn't progress' claim] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


_GOAL_EXPLICITLY_ASKS_CLAIM_RE = re.compile(
    r"goal\s+explicitly\s+asks\s+(?:to|for)\s+['\"]?([\w\s]+?)['\"]?[,.]",
    re.IGNORECASE,
)
_PRIMARY_OBJECTIVE_CLAIM_RE = re.compile(
    r"primary\s+objective|failing\s+the\s+(?:primary|main|core)\s+(?:objective|goal|requirement)",
    re.IGNORECASE,
)


def _filter_hallucinated_goal_headline_scope_findings(
    findings: list[ReviewFinding], scope: RoundScope | None,
) -> list[ReviewFinding]:
    """50-task deep-dive (docs/reports/PHASE30_50TASK_DEEP_DIVE_MASTER_2026-08-05.md, P1 item 2):
    the fifth sibling in this same family -- real, confirmed gap on Task 037 ("Create a custom
    security group and apply it to fields and buttons"). The manager's own round-scoping
    correctly narrowed round 1 to ONLY `field_visibility_restriction`, explicitly deferring
    `security_group` to a later round -- but Code-Review evaluated the round's diff against the
    FULL, un-narrowed original goal text and blocked it: "The task goal explicitly asks to
    'Create a custom security group', but the diff contains no security records or group
    definitions, failing the primary objective." None of the four existing siblings above catch
    this shape -- `_filter_hallucinated_out_of_scope_field_required_findings` and `_filter_
    hallucinated_stale_scope_exclusion_findings` both only recognize a finding naming a SPECIFIC
    field/label as missing/excluded, and `_FUNCTIONALLY_EMPTY_CLAIM_RE` (checked directly against
    Task 037's real finding text) does not match "failing the primary objective" at all -- it
    only recognizes "functionally empty"/"doesn't progress"-shaped phrasing.

    Deliberately requires BOTH signals (a captured "goal explicitly asks for X" concept AND
    "primary objective"/"failing the ... objective" language) rather than triggering on either
    alone -- this is the narrowest, most conservative trigger that still catches Task 037's real
    shape, minimizing the chance of downgrading an unrelated finding that happens to loosely use
    "goal explicitly asks" phrasing for some other, legitimate complaint.

    Regression risk (Task 038, explicitly checked, must NOT be touched by this filter): Task
    038's two blocking findings -- (1) "Adds fields ... which are explicitly out-of-scope per the
    task's NOT-yet-in-scope list" and (2) "The model definition is empty of required fields for
    the 'sales_room_model' constraint ... effectively making the module non-functional" -- were
    read directly against `_GOAL_EXPLICITLY_ASKS_CLAIM_RE` and neither contains "goal explicitly
    asks (to|for)" phrasing at all, so neither is even eligible to match; both are genuine, correct
    findings (Build adding out-of-scope fields, and a genuinely-empty in-scope model per Pattern
    G) that must keep blocking. Verified by an explicit regression test below, not just asserted.

    Same "only touch it when the round's own structured scope says this really was deferred"
    discipline as every sibling: only downgrades when the captured concept substring-matches one
    of THIS round's own `not_yet_in_scope` labels (normalized: label underscores treated as
    spaces) -- a genuine, never-deferred headline requirement is never touched.
    """
    if scope is None or not scope.not_yet_in_scope:
        return findings
    normalized_labels = [label.replace("_", " ").strip().lower() for label in scope.not_yet_in_scope]

    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        concept_match = _GOAL_EXPLICITLY_ASKS_CLAIM_RE.search(f.explanation)
        concept = concept_match.group(1).strip().lower() if concept_match else None
        if (
            concept
            and _PRIMARY_OBJECTIVE_CLAIM_RE.search(f.explanation)
            and any(label and label in concept for label in normalized_labels)
        ):
            filtered.append(f.model_copy(update={
                "severity": "info",
                "explanation": (
                    f"[Downgraded from blocking -- the goal's headline requirement this finding "
                    f"names ({concept!r}) is explicitly deferred by this round's own NOT-yet-in-"
                    f"scope constraint list; Code-Review must evaluate the round's own narrowed "
                    f"contract, not the full, un-narrowed original goal] {f.explanation}"
                ),
            }))
        else:
            filtered.append(f)
    return filtered


def _filter_findings_with_unmatched_location(
    findings: list[ReviewFinding], files: dict[str, str],
) -> list[ReviewFinding]:
    """P12 Tier A item 14 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
    §4): real, confirmed gap -- `ReviewFinding.location` was never cross-checked against the
    diffed files at all (`grep -n "location" specialists/code_review/specialist.py` returned
    only the field definition and one prompt-instruction string, no filter, no `files.keys()`
    membership check, anywhere). Code-Review has no tool access at all, so a location naming a
    file that was never actually part of this review is exactly the same class of ungrounded
    claim every sibling `_filter_hallucinated_*` function already exists to catch -- just for
    `location` specifically instead of the finding's own explanation text.

    Matches on basename (not full path) -- `location` is free text the model writes itself
    ("models/models.py:42", "the security CSV", etc.), never guaranteed to reproduce a real
    file's own full relative path verbatim, so basename containment is the same "reasonably
    tolerant, still real" matching discipline `_filter_hallucinated_own_module_collision_
    findings` and its siblings already use elsewhere in this file. Only ever downgrades a
    `blocking` finding; conservative when `location` names nothing recognizable as a path at
    all (no `.` in any token) -- a purely descriptive, non-path location ("the manifest",
    "overall structure") is left untouched, since this check can only ever prove a NAMED file
    is unmatched, never that a vague description is wrong.
    """
    basenames = {Path(p).name for p in files}
    filtered = []
    for f in findings:
        if f.severity != "blocking":
            filtered.append(f)
            continue
        location_tokens = re.split(r"[\s,:]+", f.location)
        named_paths = [t for t in location_tokens if "." in t]
        if not named_paths:
            filtered.append(f)  # purely descriptive location -- nothing concrete to cross-check
            continue
        if any(Path(t).name in basenames for t in named_paths):
            filtered.append(f)
            continue
        filtered.append(f.model_copy(update={
            "severity": "info",
            "explanation": (
                f"[Downgraded from blocking -- location {f.location!r} does not match any file "
                f"actually reviewed this round ({sorted(basenames)}); Code-Review has no tool "
                f"access, so a location naming a file outside the real diff is an ungrounded "
                f"claim] {f.explanation}"
            ),
        }))
    return filtered


def _extract_json_object(text: str) -> str:
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        return brace_match.group(0)
    return text


class CodeReviewSpecialist:
    def __init__(self, client: ModelGatewayClient, model: str | None = None, db: str = ""):
        self.client = client
        self.model = model or os.environ.get(_DEFAULT_MODEL_ENV, _DEFAULT_MODEL_FALLBACK)
        # Phase 30 §26 follow-up (2026-08-04): real, confirmed gap found in a same-night audit --
        # this specialist never had ANY live-schema access at all (unlike Build, which had it but
        # only conditionally until its own §26 item 1 fix). Empty string (the default) means
        # resolve_current_schema_block() gracefully renders nothing, same "" contract Build's own
        # schema-grounding helper already uses -- schema grounding here degrades to the prior,
        # ungrounded behavior rather than ever blocking a review.
        self.db = db

    async def run(self, contract: TaskContract) -> SpecialistOutput:
        if contract.capability_class != CapabilityClass.readonly_investigation:
            return SpecialistOutput(
                task_id=contract.task_id,
                specialist_type=contract.specialist_type,
                summary=(
                    f"CodeReviewSpecialist only handles capability_class=readonly_investigation, "
                    f"got {contract.capability_class!r} -- this specialist never writes anything."
                ),
                detail={},
                claims_complete=False,
            )

        diff_input = next((i for i in contract.inputs if i.startswith(_DIFF_PREFIX)), None)
        audit_input = next((i for i in contract.inputs if i.startswith(_AUDIT_PREFIX)), None)

        try:
            if diff_input:
                module_name = diff_input[len(_DIFF_PREFIX):]
                return await self._run_diff_review(contract, module_name)
            if audit_input:
                relative_path = audit_input[len(_AUDIT_PREFIX):]
                return await self._run_full_audit(contract, relative_path)
            raise NoReviewTargetError(
                f"contract.inputs contains neither a {_DIFF_PREFIX!r} nor a {_AUDIT_PREFIX!r} "
                f"entry -- nothing to review. inputs={contract.inputs!r}"
            )
        except (NoReviewTargetError, CodebaseReadError, ConstitutionNotFoundError, ReviewGenerationError) as exc:
            # P12 Tier S item 2 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
            # §4): real, confirmed gap -- before this marker existed, EVERY one of these four
            # exceptions (Code-Review never actually produced a real review at all -- a
            # structural failure, not a judgment) returned detail={}, and
            # fold_code_review_findings() reads detail.get("findings", []), which silently
            # returns an EMPTY list for an empty dict -- meaning a structural failure was
            # previously INDISTINGUISHABLE from "reviewed cleanly, zero findings" and could let a
            # round be silently approved with no real Code-Review ever having happened.
            # structural_failure=True is the fail-closed marker fold_code_review_findings() now
            # checks explicitly (see manager/tools.py) before ever treating an empty findings
            # list as a clean pass.
            return SpecialistOutput(
                task_id=contract.task_id,
                specialist_type=contract.specialist_type,
                summary=str(exc),
                detail={"structural_failure": True, "structural_failure_kind": type(exc).__name__},
                claims_complete=False,
            )

    async def _run_diff_review(self, contract: TaskContract, module_name: str) -> SpecialistOutput:
        """Reviews a Build specialist's already-written module against
        the Constitution. claims_complete here means "this change is
        APPROVED" (no blocking findings) -- the pass/fail judgment this
        specialist exists to make, never the specialist's own generic
        "did I finish" status.
        """
        files = read_module_files(module_name)
        # P12 Tier S/A item 6: built once, from contract's own already-structured fields,
        # passed to every scope-aware filter below instead of each one independently
        # re-parsing the same "NOT yet in scope..." sentence out of contract.goal.
        scope = derive_round_scope(contract)
        result = await self._review(contract, files, mode="diff")
        result = result.model_copy(update={"findings": _filter_hallucinated_scope_findings(result.findings, contract)})
        result = result.model_copy(update={"findings": _filter_hallucinated_wrong_constraint_attribution_findings(result.findings, contract)})
        result = result.model_copy(update={"findings": _filter_hallucinated_method_missing_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_xmlid_missing_findings(result.findings, files, module_name)})
        result = result.model_copy(update={"findings": _filter_hallucinated_direct_field_missing_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _flag_ungated_access_model_touched(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_missing_import_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_duplicate_field_declaration_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_ensure_one_list_view_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_already_exists_in_schema_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_inherit_target_missing_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_prior_round_base_model_broken_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_empty_security_csv_findings(result.findings, files, contract.goal, scope)})
        result = result.model_copy(update={"findings": _filter_hallucinated_sequence_pattern_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_sequence_not_transaction_safe_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_selection_add_not_added_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_field_not_instantiated_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_module_registration_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_own_module_collision_findings(result.findings, files, module_name)})
        result = result.model_copy(update={"findings": _filter_hallucinated_bare_group_id_prefix_mismatch_findings(result.findings, files, module_name)})
        result = result.model_copy(update={"findings": _filter_hallucinated_model_id_prefix_mismatch_findings(result.findings, files, module_name)})
        result = result.model_copy(update={"findings": _filter_hallucinated_wrong_model_xmlid_format_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_out_of_scope_field_required_findings(result.findings, contract.goal, scope)})
        result = result.model_copy(update={"findings": _filter_hallucinated_goal_self_contradiction_findings(result.findings, contract.goal, scope)})
        result = result.model_copy(update={"findings": _filter_hallucinated_collision_satisfied_field_missing_findings(result.findings, contract.goal)})
        result = result.model_copy(update={"findings": _filter_hallucinated_stale_scope_exclusion_findings(result.findings, contract.goal, scope)})
        result = result.model_copy(update={"findings": _filter_hallucinated_functionally_empty_findings(result.findings, files, scope)})
        result = result.model_copy(update={"findings": _filter_hallucinated_goal_headline_scope_findings(result.findings, scope)})
        result = result.model_copy(update={"findings": _filter_findings_with_unmatched_location(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_incomplete_compute_dependency_findings(result.findings, contract, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_contradicted_by_own_overall_assessment(result.findings, result.overall_assessment)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_blocking_on_self_named_excluded_scope(result.findings)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_blocking_on_already_satisfied_carried_forward_content(result.findings, contract.goal)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_blocking_on_not_yet_in_scope_carried_forward_content(result.findings, contract.goal, scope)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_blocking_on_carried_forward_out_of_scope_content(result.findings)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_blocking_on_structurally_mandatory_core_files(result.findings)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_claiming_the_standard_module_root_init_is_a_circular_import(result.findings)})
        result = result.model_copy(update={"findings": _filter_hallucinated_fabricated_forbidden_model_quote_findings(result.findings, contract.goal)})
        result = result.model_copy(update={"findings": _filter_hallucinated_current_round_conflated_with_deferred_labels_findings(result.findings, files, scope)})
        result = result.model_copy(update={"findings": _filter_hallucinated_goal_spec_context_key_findings(result.findings, contract.goal)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_citing_stale_previous_attempt_error_text(result.findings)})
        result = result.model_copy(update={"findings": _filter_hallucinated_syntax_error_findings_against_valid_code(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_xml_malformed_findings_against_valid_xml(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_duplicate_method_findings_against_real_code(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_redundant_line_findings_against_real_code(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_missing_content_findings_against_real_files(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_overwrite_findings_against_guarded_assignment(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_ir_rule_permission_capped_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_decoration_string_literal_findings(result.findings, files)})
        result = result.model_copy(update={"findings": _filter_hallucinated_findings_that_self_report_as_already_resolved(result.findings)})
        for finding in result.findings:
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "code_review",
                "message": finding.explanation,
                "status": "failed" if finding.severity == "blocking" else "running",
            })
        blocking = [f for f in result.findings if f.severity == "blocking"]
        return SpecialistOutput(
            task_id=contract.task_id,
            specialist_type=contract.specialist_type,
            summary=result.overall_assessment,
            detail={
                "mode": "diff_review",
                "module_name": module_name,
                "findings": [f.model_dump() for f in result.findings],
            },
            claims_complete=(len(blocking) == 0),
            artifacts=[module_name],
        )

    async def _run_full_audit(self, contract: TaskContract, relative_path: str) -> SpecialistOutput:
        """Task 4: a read-only quality audit. claims_complete here means
        "the audit itself completed and produced a real report" -- there
        is no pass/fail concept for an audit, unlike a diff review; this
        is the same underlying specialist and judgment applied to a
        genuinely different question, per the build plan's own framing.
        """
        files = read_codebase_tree(relative_path)
        result = await self._review(contract, files, mode="audit")
        for finding in result.findings:
            publish_trace_event(str(contract.task_id), {
                "level": "specialist", "actor": "code_review",
                "message": finding.explanation,
                "status": "failed" if finding.severity == "blocking" else "running",
            })
        return SpecialistOutput(
            task_id=contract.task_id,
            specialist_type=contract.specialist_type,
            summary=result.overall_assessment,
            detail={
                "mode": "full_audit",
                "audited_path": relative_path,
                "files_read": len(files),
                "findings": [f.model_dump() for f in result.findings],
            },
            claims_complete=True,
            artifacts=[relative_path],
        )

    async def _review(self, contract: TaskContract, files: dict[str, str], mode: str) -> ReviewResult:
        constitution_text = load_odoo_development_constitution()
        skill_text = _AUDIT_SKILL_PATH.read_text()

        files_block = "\n\n".join(f"--- {path} ---\n{content}" for path, content in files.items())
        stripped_schema = _strip_descriptions(ReviewResult.model_json_schema())
        schema_json = json.dumps(stripped_schema, indent=2)
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": ReviewResult.__name__, "schema": stripped_schema},
        }

        # Same fix as BuildSpecialist's own prompt construction (found
        # live: a flat, undifferentiated rules list buries a correct,
        # resolved correction among many older, mostly-superseded
        # entries) -- keep critical corrections in their own heading,
        # cap ordinary history to the most recent entries.
        critical_rules, ordinary_rules = split_critical_rules(contract.rules)
        _MAX_ORDINARY_RULES = 8
        capped_ordinary = ordinary_rules[-_MAX_ORDINARY_RULES:]
        rules_block = ""
        if critical_rules:
            rules_block += (
                "CRITICAL, confirmed corrections -- weigh these most heavily:\n"
                + "\n".join(f"  - {r}" for r in critical_rules) + "\n\n"
            )
        if capped_ordinary:
            rules_block += "General history:\n" + "\n".join(f"  - {r}" for r in capped_ordinary)

        # Phase 30 §26 follow-up (2026-08-04): a same-night audit found this specialist had the
        # IDENTICAL two prompt-assembly gaps Build's own §26 fix already closed -- zero live
        # schema access anywhere in this file, and the same capped-8 paraphrased rules history
        # with no verbatim-failure-text carve-out. Applying the same fix here: a curated,
        # live schema excerpt (reusing tools_odoo.schema_grounding.resolve_current_schema_block(),
        # the SAME real implementation Build's own fix uses -- not a second copy), and the
        # immediately-preceding round's own literal failure text in its own protected block,
        # never subject to the 8-entry cap. "" when self.db wasn't configured or no target model
        # could be resolved -- never blocks a review either way.
        current_schema_block_text = resolve_current_schema_block(contract, self.db)
        current_schema_block = (
            f"<current_schema>\n{current_schema_block_text}\n</current_schema>\n\n---\n\n"
            if current_schema_block_text else ""
        )
        previous_attempt_errors_block = (
            f"<previous_attempt_errors>\n{contract.previous_round_raw_failure_text}\n"
            f"</previous_attempt_errors>\n\n---\n\n"
        ) if contract.previous_round_raw_failure_text else ""

        # Phase 30 §26 item 4/5 analog: same section order as Build's own fixed prompt -- stable
        # constitution/skill first, then the schema excerpt and capped rules history (stable-ish),
        # then the volatile previous-attempt-errors block, then the volatile task/files-to-review
        # block, ending with the output-format contract immediately before generation.
        prompt = (
            f"{constitution_text}\n\n---\n\n{skill_text}\n\n---\n\n"
            f"{current_schema_block}"
            f"Rules:\n{rules_block}\n\n---\n\n"
            f"{previous_attempt_errors_block}"
            f"<task>\n"
            f"Review mode: {'a proposed change (diff review)' if mode == 'diff' else 'a full read-only codebase audit (task 4)'}\n"
            f"Task goal: {contract.goal}\n"
            f"Files to review:\n{files_block}\n"
            f"</task>\n\n---\n\n"
            f"<output_contract>\n"
            "Judge against the four questions from the odoo-codebase-audit skill: is this the "
            "simplest solution, does it respect what's already there, does it introduce "
            "unnecessary complexity, does it touch anything it shouldn't. Respond with ONLY a "
            f"single JSON object matching this schema (no prose, no markdown fence):\n{schema_json}\n"
            "Each finding needs a real location (file path, and line if you can tell), a severity "
            "of exactly one of blocking/major/minor/info, and a one-line explanation -- not a "
            "paragraph. An empty findings list is a valid, honest result if nothing is wrong.\n\n"
            "Do NOT flag the absence of automated test files (e.g. a tests/ directory or "
            "test_*.py) as a finding of any severity for generated Odoo module code. Real, "
            "genuine bug found live (2026-07-11): Build's own generation schema has no field "
            "for test files at all -- it is structurally incapable of producing them in either "
            "full-generation or scoped-edit mode. Repeatedly demanding tests here creates a "
            "permanent deadlock (blocking forever on something that can never be satisfied), "
            "not a real, actionable finding -- this is neither the Constitution's nor the "
            "audit skill's own requirement, only your own general instinct. If the task's own "
            "goal explicitly asks for tests, that's a genuine gap worth a non-blocking (major) "
            "finding; otherwise, do not raise it at all.\n\n"
            "NEVER invent a 'scope constraint' or 'round constraint' that isn't LITERALLY "
            "present in the Task goal or Rules given above -- a real, confirmed, damaging bug "
            "found live (2026-07-19): a task's own goal explicitly asked for 'a new security "
            "group... via its own access rule row', yet this got rejected round after round "
            "with findings like 'violates the explicit round constraint to exclude security "
            "records' -- no such constraint existed anywhere in the real goal or rules; it was "
            "invented, then fed back as 'prior attempt failed' guidance, creating a non-progress "
            "loop where fixing the invented complaint meant removing content the task's own goal "
            "explicitly required. Before flagging ANYTHING as 'out of scope', quote the exact "
            "phrase from the goal or rules above that establishes the scope limit -- if you "
            "cannot point to one, it is not out of scope, and generating what the goal literally "
            "asks for can never be a blocking finding.\n"
            "</output_contract>"
        )

        # This needs BOTH disciplines together, which neither existing
        # helper provides alone: generate_checked()'s </think>-completeness
        # check (required for this model, which always produces a long
        # hidden thinking trace and ignores /no_think entirely -- §0.5.2),
        # and call_structured()'s validate-and-reask retry loop (a real,
        # reproducible failure mode confirmed during Phase 10 testing: on
        # a long, multi-finding response this model occasionally emits
        # JSON with a syntax error, e.g. a missing comma between array
        # elements -- a one-shot parse has no way to recover from that).
        messages = [{"role": "user", "content": prompt}]
        max_tokens_for_this_prompt = _max_tokens_for_prompt(prompt)
        last_error: Exception | None = None
        for attempt in range(1, contract.retry_sub_budget + 1):
            try:
                cleaned = await generate_checked(
                    self.client, self.model, messages, max_tokens=max_tokens_for_this_prompt, timeout_sec=_TIMEOUT_SEC,
                    task_id=str(contract.task_id), actor="code_review",
                    node_id=contract.current_constraint_label,
                    call_label=f"Reviewing the {'diff' if mode == 'diff' else 'full codebase'}",
                    response_format=response_format,
                )
            except IncompleteResponseError as exc:
                last_error = exc
                continue  # a truncated response can't be usefully fed back; just retry

            candidate = _extract_json_object(cleaned)
            try:
                parsed = json.loads(candidate)
                return ReviewResult.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if attempt < contract.retry_sub_budget:
                    messages.append({"role": "assistant", "content": cleaned})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"That response didn't parse as valid JSON: {exc}\n"
                                "Respond again with ONLY a corrected, complete JSON object matching "
                                "the schema above -- check for missing commas between array elements."
                            ),
                        }
                    )

        raise ReviewGenerationError(
            f"review response failed to produce valid JSON after {contract.retry_sub_budget} attempts: {last_error}"
        ) from last_error
