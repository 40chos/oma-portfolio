---
name: odoo-codebase-audit
version: 1.0.0
last_validated: 2026-07-30
description: How to perform a read-only review of Odoo module code
  against the Odoo Development Agent Constitution's standards. Use for
  task 4 (the full codebase audit) and for reviewing a Build
  specialist's proposed diff before it's treated as done.
---

# Odoo Codebase Audit

## When to use this
Task 4's full, read-only codebase audit, and any review of a Build
specialist's proposed change (a diff, not a whole-codebase read) before
that change is treated as done. This is the Code-Review specialist's
own skill (Phase 10) — a Build specialist never uses this skill to
review its own work; generation and verification must never be the
same agent instance.

## Rules specific to this domain
- **This capability is read-only at the tool-scoping level, not just by
  instruction.** An invocation using this skill must never be handed
  any module-development or write-capable data tools at all — enforce
  this structurally when constructing the task, not by trusting the
  specialist's own restraint.
- Judge against four concrete questions, in this order: is this the
  simplest solution to the actual problem; does it respect what's
  already there (existing patterns, naming, module boundaries) rather
  than introducing a parallel way of doing the same thing; does it
  introduce unnecessary complexity; does it touch anything it shouldn't
  (a file, model, or permission outside the task's own stated scope).
- Every finding needs a location, a severity, and a one-line
  explanation — a structured claim that can be spot-checked, never a
  paragraph of prose someone has to parse to extract the actual issue.
- A hardcoded value, an overly broad `sudo()` call, a write to a
  sensitive_paths model without the tier the Manager assigned it, or a
  change that silently widens an existing security rule are all worth
  flagging at whatever severity actually matches the risk — don't
  soften a real finding into a vague, hedged note.

## How
1. Load the actual Constitution document in full (never summarized) as
   the standard being checked against.
2. For a diff review: read the diff plus enough surrounding context
   (the full file(s) touched, not just the changed hunks) to judge it
   fairly.
3. For task 4's whole-codebase audit: read broadly, model by model or
   module by module, rather than sampling — a read-only pass has time
   to be thorough that a write-capable task doesn't.
4. Produce a structured list of findings (location, severity, one-line
   explanation) — never a prose summary alone.
5. If nothing is wrong, say so plainly and briefly — an empty findings
   list is a valid, useful result, not something to pad out to look
   thorough.

## Revision Log
(empty at creation)
