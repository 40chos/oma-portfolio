# Stage A — Odoo Static Structure Parser

**Design source:** `docs/architecture/ODOO_KNOWLEDGE_PIPELINE_PILOT_RESULTS_2026-07-17.md` §11.4/§11.5
**Orchestration:** `docs/planning/ODOO_FULL_GRAPH_BUILD_ORCHESTRATION_2026-07-18.md`, Step 1a.

Reads one Odoo 16 module's real source with Python's `ast`/`csv`/`xml.etree` and
produces a structured JSON record — no LLM call, no network access, near-zero cost.
This is the deterministic half of the two-stage pipeline (§11.4): Stage A produces
`deps`/`models`/`extends`/`views_extend`/`security` directly from the code's own
declarative syntax; Stage B (built separately, in parallel, by Agent W) is reserved
for exactly the two things a parser can't do — the one-line business-purpose `NOTES`
summary, and resolving whatever Stage A flags under `needs_llm_review`.

**This package never touches real Odoo source.** Everything here is designed and
tested against small synthetic fixture modules written for this purpose (see
`fixtures/`, every file headed `SYNTHETIC TEST FIXTURE -- not real Odoo source`).
The project owner runs `driver.py` against real module checkouts themselves, over
their own SSH connection, per the data-exfiltration boundary already established
in the project's own handoff documentation.

## What's in this package

| File | Purpose |
|---|---|
| `parser.py` | Core: `parse_module(module_dir) -> ModuleRecord`. Parses `__manifest__.py` (depends), `models/*.py` (models/fields/EXTENDS/computed-`depends`), `views/*.xml` (`inherit_id`), `security/*.csv` (access rules), `security/*.xml` (`ir.rule` — always flagged, see below). |
| `driver.py` | CLI: walks a directory of module subdirectories, runs the parser over each, writes one `<module>.json` per module plus a combined `store.jsonl`. |
| `lookup.py` | `KnowledgeStore` — the small query layer over `store.jsonl`: `get_module(name)`, `get_dependents(model, field)`, `get_hub_modules(min_indegree)`. Ready to wrap as MCP tools later (§11.5) without redesign. |
| `fixtures/` | Four small synthetic modules (`fixture_base`, `fixture_mail`, `fixture_note`, `fixture_dynamic`), shaped like the real pilot's `base`→`mail`→`note` dependency chain, used only for testing this parser. |
| `test_parser.py` | Runs the parser, driver, and lookup layer against the fixtures and asserts real output values — this is how correctness was validated without needing real Odoo source. |

## Output schema

One JSON record per module, matching §11.5:

```json
{
  "module": "crm",
  "deps": ["base_setup", "sales_team", "mail"],
  "models": [
    {"name": "crm.lead", "inherits": ["mail.thread"], "fields": [
      {"name": "name", "type": "Char", "required": true},
      {"name": "partner_id", "type": "Many2one", "comodel": "res.partner"},
      {"name": "expected_revenue", "type": "Monetary", "computed": true, "depends": ["stage_id", "team_id"]}
    ]}
  ],
  "extends": [
    {"model": "res.partner", "name": "opportunity_count", "type": "Integer", "computed": true, "depends": ["crm.lead.partner_id"]}
  ],
  "views_extend": [{"view": "crm_lead_view_form", "inherit_id": "base.view_partner_form"}],
  "security": [{"model": "crm.lead", "group": "sales_team.group_sale_salesman", "perms": "rw"}],
  "needs_llm_review": [
    {"reason": "dynamic _inherit in class X, cannot resolve statically", "snippet": "..."}
  ]
}
```

Notes on two deliberate deviations from the prompt's example, both harmless supersets:
- `models[].inherits`: an added, optional list of mixin/parent models from a
  `_name` + `_inherit=[...]` class (e.g. `note.note` inheriting `mail.thread` in the
  real pilot's own `call_5_output.txt`). Not in the original example payload, but
  needed to represent that real, common Odoo pattern without losing information.
- `extends[]` entries use the field's own key (`"name"`) rather than repeating it as
  a top-level `"field"` key alongside a nested duplicate — the entry is just
  `{"model": ..., **field_info}`, so it carries the same `type`/`required`/`comodel`/
  `computed`/`depends` shape as a normal field, not a stripped-down version.

## What gets flagged under `needs_llm_review`, and why

Per the explicit instruction not to guess, the parser flags rather than resolves:

- **Dynamic `_inherit`** — built from a variable, `getattr()`, list comprehension,
  or anything else that isn't a literal string or a literal list of strings.
- **Dynamic `getattr()`/`setattr()` calls** anywhere in a model class body whose
  attribute-name argument isn't a string literal — a parser cannot know which
  field/attribute is actually being touched.
- **`compute=` referencing a method the parser can't find in the same class**, or
  whose `@api.depends(...)` decorator isn't a literal list of strings (or is
  missing entirely) — the dependency list is the single highest-value fact in the
  whole schema (see `ODOO_MODULE_CHAINING_SUMMARY_DESIGN_2026-07-16.md` §1), so this
  is flagged rather than left silently empty.
- **Non-literal relational comodel** (`fields.Many2one(some_variable)` instead of a
  string literal).
- **Every `ir.rule` domain**, unconditionally — `domain_force` is a Python
  expression evaluated at runtime against `user`/`company_id`/etc., never a static
  fact a parser can resolve, so every `ir.rule` record is flagged with its raw
  domain string as the snippet rather than partially interpreted.
- **Any class with neither a resolvable `_name` nor a resolvable `_inherit`** —
  can't be classified as a new model or an extension.
- **Malformed XML/manifest files** — a parse failure is surfaced as a flag with the
  real exception message, not silently skipped (a real bug caught during testing:
  an early fixture's XML comment contained `--`, which is illegal inside an XML
  comment and broke parsing silently until this was added).

None of these are guessed at or partially filled in — the field/class/record is
either fully captured or fully flagged, never a mix.

## How to run against real source

```
cd oma
python3 -m tools_odoo.knowledge_graph.driver <path-to-modules-root> <output-dir>
```

`<path-to-modules-root>` is a local directory containing one subdirectory per
module (each with its own `__manifest__.py`, `models/`, `views/`, `security/`).
This script never opens a network connection of any kind — it only ever reads
a local directory, so it works identically whether that directory came from
`docker cp` out of a running container (see `docker/seed-knowledge-graph.sh`,
the actual mechanism this repo uses against the `odoo` compose service), a git
clone, or anywhere else.

Output: `<output-dir>/<module>.json` per module, plus `<output-dir>/store.jsonl`
(one line per module, same records) — the file `lookup.py`'s `KnowledgeStore` reads.

```python
from tools_odoo.knowledge_graph.lookup import KnowledgeStore

store = KnowledgeStore.from_jsonl("output-dir/store.jsonl")
store.get_module("crm")
store.get_dependents("res.partner", "opportunity_count")
store.get_hub_modules(min_indegree=3)
```

## Testing

```
cd oma
python3 tools_odoo/knowledge_graph/test_parser.py
```

33 assertions, all against the synthetic fixtures in `fixtures/`, covering: new-model
extraction with required/relational/computed fields, cross-module `EXTENDS` (both the
single-`_inherit`-string case and the cross-model dotted `depends` reference), the
`_name` + `_inherit=[mixin]` case (a new model that also inherits a mixin — distinct
from a pure extension), `views/*.xml` `inherit_id` extraction, `security/*.csv`
parsing and perms-letter decoding, the driver's per-module + JSONL output, and all
three `KnowledgeStore` lookup functions. A fourth fixture (`fixture_dynamic`)
contains only patterns the parser must **not** guess at, and every check there
asserts the correct flag appears under `needs_llm_review` — proving the "flag,
don't fabricate" contract holds, not just that the happy path works.

Current result: **all 33 checks pass.**

## Known limitations (real, not hidden)

- **Only `models/*.py` is scanned for model classes.** Real Odoo modules
  occasionally define models outside `models/` (rare, but not impossible) — such a
  file would be silently invisible to this parser rather than flagged, since the
  parser doesn't currently walk the whole module tree looking for stray model
  classes. Worth a real-source spot-check once this runs against actual modules
  (§11.8 step 3 of the orchestration plan already calls for reviewing real output).
- **Business logic embedded in method bodies that affects model shape at runtime**
  (e.g. a method that conditionally adds fields via `self._fields[...] = ...` or
  similar metaprogramming) is not specifically detected beyond the `getattr`/
  `setattr` scan — this is inherently open-ended and was scoped down to the concrete
  patterns named in the task (dynamic `_inherit`, `getattr`-based access,
  non-literal `ir.rule` domains) rather than an exhaustive dynamic-code detector.
  Anything genuinely unusual here would currently pass through unflagged rather than
  being caught — a real gap, not claimed to be solved.
- **`selection_add=` (Selection-field extension, real, common Odoo pattern — see
  the actual pilot's `note` output, `call_5_output.txt`: `mail.activity.type +=
  category~[selection_add:reminder]`) is not specifically extracted.** The field
  itself is still captured with its base info, but the specific selection-option
  addition isn't broken out as its own fact. Not required by the schema in the
  task prompt, but worth flagging as a real gap versus the pilot's LLM-derived
  output, which did capture it.
- **Only `security/*.csv` files are treated as access rules; only `ir.rule` records
  in `security/*.xml` are scanned.** Other XML-defined security constructs (e.g.
  `ir.model.access` records written directly in XML instead of CSV, which is rare
  but valid Odoo) would not be captured or flagged at all.
