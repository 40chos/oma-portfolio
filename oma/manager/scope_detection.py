"""Phase 22 follow-up (2026-07-23): detects when a task's plain-text
goal targets a model that ALREADY belongs to a genuinely pre-existing
CUSTOM module (e.g. SITE's own `project_fieldjob`, `mis_base_extend`)
-- the real, structural gap found live running the SITE 50-task list:
a task like "add a field to the fieldjob record" has no way, as plain
text submitted through the ordinary chat entry point, to tell Build
that "fieldjob" means the real, already-installed `project.fieldjob`
model (48 real fields already there) rather than something to
scaffold from scratch. Confirmed live: Build scaffolded a brand-new
module with 6 of its own fresh fields (`line_ids`, `name`, `price`,
`product_name`, `record_id`, `total_price`) for a task whose real goal
only needed ONE new field on the real model -- hit the
single-constraint-per-round decomposition cap trying to declare all 6
as if the model didn't exist yet.

`edit_existing_module:<name>` already existed as a real mechanism
Build honors (see `_extract_edit_existing_module()` in
`specialists/build/specialist.py`) -- it was just never POPULATED
automatically from plain free text before this. This module is the
missing piece, not a new mechanism.

Deliberately conservative at every step: a wrong guess here is worse
than no guess at all (it would incorrectly redirect a genuinely
NEW-model task into editing an unrelated existing module). First real
design attempt asked the LLM to invent the target's dotted Odoo
technical model name cold from the goal text alone (e.g. "fieldjob" ->
"project.fieldjob") -- confirmed live to be unreliable: correct only
inconsistently across repeated identical calls, because there is
nothing grounding that guess in what modules actually exist. Redesigned
to multiple-choice instead: give the LLM the REAL, current list of
custom SITE module names (`list_custom_site_module_names()`) and ask it
to pick one or return none -- a fundamentally easier, more reliable
task than open-ended name generation. Even a confident pick is then
independently re-verified against the live registry (the picked
module must genuinely own a real model with no core-Odoo co-owner)
before ever being trusted.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from infra.gateway_client import ModelGatewayClient
from infra.structured_output import StructuredOutputError, call_structured
from tools_odoo.module_dev.toolchain import (
    is_custom_site_module,
    is_own_scaffolded_module,
    list_custom_site_module_names,
    list_own_scaffolded_module_names,
)
from tools_odoo.odoo_schema_client import get_module_state_fast, is_fast_path_eligible

_GOAL_WORD_RE = re.compile(r"[a-z0-9]+")


def _fuzzy_match_existing_custom_module(goal: str, candidates: list[str]) -> str | None:
    """Phase 30 §26 follow-up (2026-08-04): real, confirmed gap -- the LLM pick above is a
    single call with no retry or fallback, and was confirmed live to return null for a goal
    ("...the fieldjob form...") that obviously matches a real candidate ("project_fieldjob")
    already in the list it was shown -- non-deterministically, since a second, differently-
    worded goal targeting the exact same real module correctly picked it on the first try. A
    plain retry at the same call's default temperature (0.0) would very likely just reproduce
    the same null, so this is a deterministic fallback instead, tried only when the LLM path
    found nothing.

    Conservative by construction, same "a wrong guess is worse than no guess" philosophy as the
    LLM path: for each candidate module name, take its single MOST DISTINCTIVE token (the
    longest underscore-separated segment -- "project_fieldjob" -> "fieldjob", never the whole
    name, since requiring the whole name verbatim in plain-English prose would almost never
    match anything real). A match requires that exact token to appear as a whole word in the
    goal text (word-boundary, never a partial substring) -- and only if EXACTLY ONE candidate
    matches is anything returned; 0 or 2+ matches both mean "not confident enough," same as the
    LLM path's own null-on-uncertainty behavior.
    """
    goal_lower = (goal or "").lower()
    goal_words = set(_GOAL_WORD_RE.findall(goal_lower))
    if not goal_words:
        return None
    # Real, confirmed gap found live (2026-08-10, task 4724a61f): a goal naming a module's own
    # FULL technical name verbatim (e.g. "...module oma_build_a_complete_field_ab52b7f8...")
    # still fell through to the single-distinctive-token logic below, which for this real name
    # picked 'complete' as the longest token (tied with the random suffix) -- a word that never
    # actually appears standalone in the goal text, so the whole match silently failed despite
    # the exact name being right there. An exact full-name match is strictly stronger signal than
    # any token-decomposition heuristic, so it's checked first and wins outright. Uses a raw
    # substring check (word-boundary-guarded, not the `_GOAL_WORD_RE`-tokenized set) since that
    # regex itself splits on underscores and would never reconstruct the full dotted/underscored
    # name as one token in the first place.
    exact_matches = [
        c for c in candidates
        if re.search(r"(?<![a-z0-9_])" + re.escape(c.lower()) + r"(?![a-z0-9_])", goal_lower)
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    matches = []
    for candidate in candidates:
        tokens = [t for t in candidate.lower().split("_") if len(t) >= 4]
        if not tokens:
            continue
        distinctive_token = max(tokens, key=len)
        if distinctive_token in goal_words:
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    return None


class _ModulePick(BaseModel):
    module_name: str | None


_PICK_PROMPT = """A user submitted this request to an Odoo development assistant:

"{goal}"

Here is the REAL, current list of custom modules already installed in this Odoo
system:
{module_list}

Does this request describe modifying, extending, or adding something to a
record type that belongs to ONE of the modules in this exact list? If so,
return that module's exact name, copied character-for-character from the list
above.

If the request is clearly about building something genuinely NEW that doesn't
belong to any of these modules, or you're not sure, return null.
"""


async def detect_existing_custom_module_target(
    goal: str, db: str, client: ModelGatewayClient, model: str, task_id: str | None = None,
) -> str | None:
    """Returns the real module name to inject as `edit_existing_module:`
    if, and only if, ALL of the following hold:
    1. `list_custom_site_module_names()` returns a non-empty candidate
       list (a live SSH lookup, not cached/assumed) -- the LLM only
       ever picks from this REAL list, never invents a name (checked
       again below regardless, since a structured-output model can
       still occasionally return something outside its own schema's
       intended range).
    2. The LLM picks one of those exact names for this goal, OR (Phase 30 §26 follow-up,
       2026-08-04: confirmed live -- the LLM path is a single call with no retry, and returned
       null for a goal with an obvious real candidate already in its own list) the deterministic
       fallback `_fuzzy_match_existing_custom_module()` finds exactly one unambiguous match when
       the LLM found none.
    3. The picked name genuinely lives under the custom SITE addons
       root (`is_custom_site_module()` -- redundant with #1's own
       source but re-checked independently rather than trusted
       transitively) AND is actually in `state='installed'` on the
       real target right now (`get_module_state_fast()`) -- a module
       directory existing on disk doesn't mean it's live.

    Returns None on any failure or uncertainty at any step -- never
    guessed at, always independently verified against the real, live
    system before ever being trusted enough to redirect a task.

    Real, confirmed gap found live (2026-08-10, task 4724a61f): candidates originally came ONLY
    from `list_custom_site_module_names()` (`/opt/site/site16`, genuine external customer
    modules), so a plain-text goal explicitly naming one of THIS PIPELINE'S OWN previously
    scaffolded modules under `/mnt/extra-addons` (e.g. `oma_build_a_complete_field_ab52b7f8`,
    already real and installed) was never even offered to the LLM/fuzzy-match as a candidate --
    `existing_module_dependency` silently resolved to None and Build scaffolded a brand-new,
    disconnected module instead. Candidates now merge both roots; step 3's own independent
    re-verification below accepts either `is_custom_site_module()` (SITE customer root) OR
    `is_own_scaffolded_module()` (this pipeline's own root) -- still never trusting a bare name
    match without confirming the picked name really lives somewhere real.
    """
    candidates = list_custom_site_module_names() + list_own_scaffolded_module_names()
    if not candidates:
        return None

    # Real, confirmed bug found live (2026-08-13 overnight Phase 35 single_new_field bake-in,
    # 4+ real occurrences the same night -- sf01/sf03/sf04/sf06b/sf07b/sf08b): the LLM pick below
    # used to run FIRST and unconditionally, even though `_fuzzy_match_existing_custom_module()`
    # (below) already includes a cheap, deterministic, zero-ambiguity exact-substring check that
    # would answer this correctly and instantly whenever the goal names the real module verbatim
    # (confirmed live, directly: called in isolation against one of these exact real failing
    # goals, it correctly returned the right module name every time). The real, live root cause:
    # `candidates` here is `list_custom_site_module_names() + list_own_scaffolded_module_names()`
    # -- confirmed live tonight at 1,816 real entries (38 + 1,778) -- and the LLM pick prompt
    # (`_PICK_PROMPT`) embeds the ENTIRE candidate list as one giant multiple-choice block, one
    # module per line, in a single call. A goal that named its target module's exact real name
    # verbatim still got `module_name: null` back from that call on 4+ separate real occurrences
    # tonight -- a small/fast extraction model given an ~1,800-line haystack is a fundamentally
    # different, harder task than the deterministic substring check already sitting right below
    # it, unconditionally, unreached. Reordered: the cheap, reliable, zero-network-cost
    # deterministic check now runs FIRST -- the expensive, unreliable-at-this-scale LLM call is
    # now only ever reached as the fallback for a genuinely non-obvious goal (the case it was
    # actually designed for), not as the first, blocking gate in front of an easy exact match.
    picked_name = _fuzzy_match_existing_custom_module(goal, candidates)

    if not picked_name:
        try:
            pick = await call_structured(
                client, model, _PICK_PROMPT.format(goal=goal, module_list="\n".join(f"- {c}" for c in candidates)),
                _ModulePick, task_id=task_id, actor="manager", call_label="Detecting existing-module target",
            )
            if pick.module_name and pick.module_name in candidates:
                picked_name = pick.module_name
        except (ValueError, StructuredOutputError):
            picked_name = None

    if not picked_name:
        return None
    if not is_fast_path_eligible(db):
        return None
    if not (is_custom_site_module(picked_name) or is_own_scaffolded_module(picked_name)):
        return None
    if get_module_state_fast(picked_name, db) != "installed":
        return None
    return picked_name
