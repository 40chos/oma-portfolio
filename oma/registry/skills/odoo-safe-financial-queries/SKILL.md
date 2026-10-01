---
name: odoo-safe-financial-queries
version: 1.0.0
last_validated: null
description: How to safely read and reason about financial data in Odoo
  (invoices, stock valuation, tax) without risking an incorrect
  conclusion or an accidental write. Use whenever a task touches
  account.move, sale.order pricing, or stock valuation.
---

> **Phase 30, P6 (§11, 2026-07-30) — same real, confirmed finding as
> `odoo-xmlrpc-operations`:** no live code path was found that reads
> this file either. `last_validated: null` is the honest value.

# Odoo Safe Financial Queries

## When to use this
Any task where the goal mentions invoices, payments, tax, discounts,
currency, or stock valuation — even if it looks like a simple bug fix.

## Rules specific to this domain
- Never trust a single field in isolation. Odoo computes totals through
  a chain — line subtotal, then tax, then currency conversion, then
  rounding. Read the whole chain rather than hand-recomputing the
  arithmetic yourself.
- This skill covers investigation only. Any write to account.move or
  stock.valuation.layer is on the sensitive_paths list — see
  MANAGER_CONSTITUTION.md — and needs a pause, not a confident guess.

## How
1. Search for the relevant record(s).
2. Always request the computed fields explicitly — Odoo won't return
   amount_total or amount_tax unless you ask for them by name.
3. Cross-check against the originating sale order if the invoice came
   from one — discrepancies here are the most common real bug class.
4. If anything doesn't reconcile, stop. Don't guess. This is exactly
   the situation the Constitution's financial-safety section exists for.

## Revision Log
(empty at creation — entries get added here per §2.3/§2.7's
skill-revision mechanism, each one dated, describing what went wrong
and what changed as a result.)
