---
name: odoo-xmlrpc-operations
version: 1.0.0
last_validated: null
description: How to read and write Odoo data safely through
  OdooToolClient (search_read/read/write/create/unlink only) for tasks
  classified capability_class=data_change. Use whenever the goal is a
  pure record edit that does not require writing or installing a new
  module.
---

> **Phase 30, P6 (§11, 2026-07-30) — real, confirmed finding, not fixed
> here (out of this priority's own narrower versioning/staleness
> scope):** exhaustive grep across every `specialists/*.py` and
> `manager/*.py` file found **no live code path that ever reads this
> file**. Unlike `odoo-module-scaffolding`
> (`specialists/build/specialist.py`), `odoo-codebase-audit`
> (`specialists/code_review/specialist.py`), and
> `odoo-verification-and-reproduction`
> (`specialists/testing_qa/specialist.py`), all confirmed read
> unconditionally/by capability_class, `capability_class=data_change`
> tasks currently have **no skill file wired into their prompt at
> all** — this document is real, well-written, and genuinely orphaned.
> `last_validated: null` above is the honest value (never validated
> against a real task, because it is never actually read by one) — do
> not backfill a plausible-sounding date.

# Odoo XML-RPC Operations

## When to use this
Any task whose `capability_class` is `data_change` — a record read or
edit against existing Odoo models, with no module authoring or
deployment step involved at all. Do not reach for
`odoo-module-scaffolding` for these tasks: giving a data-only task
access to deployment tools hands out more capability than the task's
own classification says it needs (build plan, Phase 9 step 2).

## Rules specific to this domain
- **Foundational, non-negotiable (Operator, 2026-07-09): never a direct
  Postgres connection to the Odoo database, ever, for any reason,
  even a real, working credential.** `OdooToolClient` goes through
  Odoo's own XML-RPC API specifically so this is structurally true, not
  just a convention -- a data edit always goes through Odoo's own
  application layer, the same as any real Odoo user's own action would,
  never underneath it.
- Use `tools_odoo.odoo_tool_client.OdooToolClient` exclusively. It
  exposes exactly `search_read`, `read`, `create`, `write`, `unlink` —
  never a generic `execute_kw` passthrough. If a task seems to need
  something outside these five operations, that's a signal the task is
  misclassified as `data_change`, not a reason to reach for a broader
  API.
- Every `(model, operation)` pair is checked against
  `odoo_tool_allowlist.yaml` before it reaches Odoo. A refusal
  (`AllowlistViolationError`) means the model/operation genuinely isn't
  approved yet — surface this back to the Manager rather than trying a
  different call shape to work around it. Extending the allowlist is a
  deliberate, reviewed edit, never something a specialist does for
  itself mid-task.
- **Every write requires a fresh `read()` immediately beforehand** to
  obtain the current `__last_update` value, and must echo that exact
  value back into `write()`. A `ConcurrencyConflictError` means someone
  else changed the record since it was read — re-read and reassess,
  never blindly retry with the same stale value.
- **Call `infra.fencing.check_fence()` immediately before every real
  write** — no exceptions, not just when convenient. This is the one
  place the fencing-token lock actually protects anything; skipping it
  in what looks like a safe case is exactly how it stops protecting
  anything at all.
- A fresh, task-scoped API key (via `infra.odoo_jit_apikey`) is
  required for every task — never a long-lived credential reused across
  tasks. Revoke it when the task ends, success or failure.

## How
1. Confirm the task's `capability_class` really is `data_change` before
   touching anything.
2. `acquire_module_lock()` on the model/module this task touches;
   `create_task_api_key()` for a fresh, scoped credential.
3. Construct `OdooToolClient(uid=..., api_key=..., db_override=...)`.
4. For a write: `read()` first to get `__last_update`, then
   `check_fence()`, then `write()` with the echoed value.
5. On task completion (success or failure): `revoke_task_api_key()`,
   then `release_module_lock()`.
6. If anything doesn't reconcile — an allowlist refusal, a concurrency
   conflict, an unexpected empty result — stop and report it plainly
   rather than working around it silently.

## Revision Log
(empty at creation)
