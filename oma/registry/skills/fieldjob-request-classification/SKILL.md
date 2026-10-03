---
name: fieldjob-request-classification
version: 1.0.0
last_validated: 2026-08-28
description: How to classify an employee request about "fieldjob" (extra,
  out-of-scope work billed separately from the original quote/order) versus
  the neighboring concepts it's easily confused with — plain logged hours,
  originally purchased materials, and regular invoices. Use whenever a task
  touches project billing classification, chat-assistant intent routing, or
  any function that decides whether a request is "fieldjob" or something else.
---

# Fieldjob Request Classification

## Source
Derived from the real, live-tuned routing logic in
`agents/services/chat_assistant/function_router.py` (`_CATEGORY_DESCRIPTIONS`,
`_CATEGORY_DESCRIPTIONS_DETAILED`) and
`agents/services/chat_assistant/config/function_descriptions.json`
(`get_project_fieldjob` entry), as of the 2026-08-18/19 category-split
fix documented in that module's own comments. This skill summarizes that
already-shipped, already-tested classification logic for reuse outside the
chat assistant — it does not introduce new judgment calls.

## What "fieldjob" means
Fieldjob is EXTRA or OUT-OF-SCOPE work added on top of the original
quote/order, billed SEPARATELY from it. It is a billing/scope concept, not
a time-tracking or delivery concept: the defining question is "did this
work come up AFTER the original scope was agreed, and does it need its own
billing record?"

Real example phrasings (from the chat assistant's own utterance corpus):
- "is there any extra work logged"
- "fieldjob for this project"
- "out of scope work billed"
- "has extra work been accepted"
- "additional work on this job"
- "extra billable work"
- "additional work logged"

## Why this is its own category (real, confirmed history)
`fieldjob` was split out of a generic "financial" bucket after
`get_project_fieldjob` failed to route correctly on every real test across
two separate sessions — three independent, live-confirmed failures, not
noise. Root cause: "extra billable work" reads as equally plausible under
"time" (hours), "deliveries" (extra materials), or "financial" (billing)
depending on phrasing, so it kept losing to whichever neighboring category
the rest of the message's wording leaned toward. A generic "financial"
description couldn't out-compete that; a category that exists for nothing
else, with a description built from the real failure phrasings themselves,
could.

## The boundary rules
Apply these in order — each one names the specific neighboring concept
fieldjob is NOT, and why:

1. **vs. plain logged hours ("time"):** A project's normal logged hours are
   NOT fieldjob. BUT — even a phrase that mentions "hours" or "logged" is
   still fieldjob if it *also* signals extra/additional/out-of-scope/
   beyond-scope/separately-billed (e.g. "out-of-scope hours", "extra work
   logged"). The presence of "hours"/"logged" alone does not settle it;
   the extra/separate-billing signal overrides it.

2. **vs. originally purchased materials ("deliveries"):** Fieldjob is never
   the original quoted/purchased materials for the job. Those stay
   "deliveries" even if the message uses billing-adjacent language.

3. **vs. a regular invoice ("financial"):** Fieldjob is never a normal,
   already-scoped invoice. It becomes financial-relevant only once it has
   its own separate billing record for work that came up after the
   original scope — the classification itself still routes to fieldjob;
   only the resulting billing document is a financial artifact.

## How to classify a request
1. Check for an explicit "fieldjob" mention — always fieldjob if present.
2. If the request mentions hours/logged time, check for an
   extra/additional/out-of-scope/separately-billed signal alongside it.
   Present → fieldjob. Absent → plain "time".
3. If the request is about materials/products that were part of the
   original quote/order → "deliveries", never fieldjob.
4. If the request is about an already-scoped invoice with no
   extra-work signal → "financial", never fieldjob.
5. When genuinely ambiguous between two of the above, prefer the
   explanation that requires the fewest new billing artifacts — i.e.
   default away from fieldjob unless an extra/out-of-scope/separate-billing
   signal is actually present in the message.

## Real backing function
`get_project_fieldjob` (`arg_kind: project`, `category: fieldjob`) —
"extra/out-of-scope work logged against a project." This is the one
function fieldjob-classified requests should resolve to in the chat
assistant's own catalog.

## Revision Log
- 2026-08-28: Created from the chat_assistant router's existing,
  live-tested fieldjob category logic. No new classification judgment
  introduced — this documents what was already shipped and tuned there.
