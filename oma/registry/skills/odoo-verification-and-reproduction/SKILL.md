---
name: odoo-verification-and-reproduction
version: 1.0.0
last_validated: 2026-07-30
description: How to independently verify a Build specialist's claimed
  fix or module -- running the real reproduction case from the task
  contract, never trusting the builder's own word. Use for the
  Testing/QA specialist's own verification of tasks 1, 2, and 3.
---

# Odoo Verification and Reproduction

## When to use this
Any time a task's `validation_by` is `testing_qa` -- verifying whether
a Build specialist's claimed change actually holds up, independently.
This is authored from scratch, not adapted from prior art: there is no
existing AI-agent pattern for this specific job (per the technical
document's own honest flag).

## Rules specific to this domain
- **A specialist's own `claims_complete` is never sufficient.** This
  skill's entire purpose is producing an independent, real check --
  never rubber-stamping a self-report.
- **Reproduction first, coverage second.** Confirm the concrete thing
  the task actually asked for really exists/works (a field, a model, a
  behavior) via a real ORM-level check -- never an assumption based on
  "the install didn't error."
- **The coverage/spot-check step is deliberately NOT something this
  skill's own judgment performs.** It is a separate, deterministic,
  non-LLM piece of code (`tools_odoo/spot_check.py`) that this
  specialist's own self-report gets run through automatically. This
  specialist's job is to produce an honest self-report of what it
  believes is tested/untested; the deterministic check is what actually
  decides `spot_check_mismatch`, using real `coverage.py` data, never
  this specialist's own guess alone.
- **Escalate to the deeper reasoning model only when reproduction
  fails and the reason isn't obvious from the test output alone** --
  routine "field exists / doesn't exist" checks stay on the cheap,
  fast model; genuine debugging judgment about *why* something failed
  is what the escalation model is for.
- For anything touching Odoo's own test framework in a future task,
  invoke Odoo's real `unittest`-based test runner
  (`odoo-bin ... --test-tags`) rather than inventing a parallel
  mechanism -- Odoo already has one.

## How
1. Read the task contract's goal/deliverables and determine the
   concrete reproduction case (which model, which field, which
   behavior) -- ask the fast model (`qwen3-14b`) to extract this as a
   structured claim if it isn't already explicit.
2. Run the real, deterministic reproduction check
   (`tools_odoo.spot_check.check_field_exists_on_model()`, or the
   equivalent for the concrete case) -- never an XML-RPC read alone,
   which can't distinguish "doesn't exist" from "exists but empty."
3. If reproduction fails, escalate to `deepseek-r1-distill-qwen-32b`
   for a genuine debugging analysis of why -- strip its `<think>` block
   before using the response (this model doesn't get the same
   completeness-check treatment as `qwen3-14b`/`qwen3.6-27b`; use
   `strip_think_block()` directly, not `generate_checked()`).
4. Produce an honest self-report of what you believe is
   covered/uncovered -- never claim "fully tested" just to look
   complete.
5. Run the deterministic spot-check (`tools_odoo.spot_check.run_coverage_and_diff()`
   plus `compute_spot_check_mismatch()`) against that self-report. The
   real, measured result is what becomes `spot_check_mismatch` -- never
   this specialist's own belief about it.
6. Report `passed = reproduction_confirmed and not spot_check_mismatch`
   -- a task is never marked passed on this specialist's word alone if
   the deterministic check disagrees.

## Revision Log
(empty at creation)
