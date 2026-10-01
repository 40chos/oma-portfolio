# Decisions

Judgment calls made while porting OMA out of its original employer environment into
this self-hosted portfolio repo, per the instruction to decide and document rather
than ask approval for each one.

## Scaffolding

- **New repo, not an in-place edit of the handoff.** The handoff lives at
  `oma-portfolio-src/`; this repo is a fresh `git init` at `oma-portfolio/` built by
  copying and transforming that source, so the original handoff stays untouched as a
  reference.
- **`dev_runs/` (316 one-off `submit_wave*.py`/`backfill_*.py` scripts) dropped
  entirely.** Pure result logs and one-shot batch-submission scripts tied to specific
  historical task batches, not reusable or demo-relevant.
- **Employer-specific one-off scripts dropped**: `attach_accounting_governance_predicate.py`,
  `diag_companies.py`, `diag_recheck.py`, `diag_rules.py`, `grant_contact_creation.py`,
  `grant_see_all_contacts.py`, the `oma-backlog-triage.service`/`.timer` systemd units,
  `docs/reports/` (phase evidence reports with real company/ticket data). The
  remaining ~20 scripts in `scripts/` (bootstrap, partition migration, coverage-evidence
  mining, knowledge-graph maintenance, the CLI harness) are generic enough to keep as
  working examples. Note: the "~359 scripts" figure in the original brief didn't match
  this snapshot — only 27 existed in `scripts/` to begin with.
- **`tools_odoo/chat_assistant_queries.py` (19,445 lines), `chat_assistant_identity.py`,
  and `supplier_confirmation_bridge.py` deleted**, along with their ~14 test files.
  This is a separate internal tool (a project-hours/billing chat assistant) bundled
  into the handoff but outside OMA's own classify → decompose → Build → Review → QA
  pipeline — nothing in `manager/`, `specialists/`, or `contracts/` imports it (only
  `infra/fencing.py`'s comments referenced it, now reworded). It also contained real
  customer PII in its docstrings (real email addresses, a real company domain) that
  wasn't worth scrubbing line-by-line in a 19k-line file whose actual mechanism isn't
  part of what this portfolio is meant to showcase.
- **UI redesign drafts dropped**: `ui/redesign_*`, `ui/new_ui`, and the `.backup-*`
  file. `ui/chat/server.py` only ever serves `ui/chat/index.html` — the rest were dead
  experiments.

## Path landmines (constitution.py, skill lookups, coverage evidence)

- Added `oma/paths.py` as the single place every one of these lives, all overridable
  via env var, all defaulting to a location inside this repo:
  - `OMA_CONSTITUTION_PATH` → `registry/constitution/constitution.md`
  - `OMA_SKILLS_BASE_PATH` → `registry/skills/`
  - `OMA_COVERAGE_BASE_PATH` → `registry/coverage/` (combinatorial-coverage evidence;
    every reader already treats a missing file as "no evidence yet", not an error)
  - `OMA_KNOWLEDGE_GRAPH_DATA_PATH` → `var/knowledge_graph/` (generated, gitignored)
- The three `specialists/*/specialist.py` `parents[5]` skill lookups, `constitution.py`'s
  hardcoded absolute path, `contracts/coverage_signal.py`'s `parents[4]` walk, and four
  `scripts/*.py` evidence-mining tools' `parents[3]`/`parents[4]` walks all now resolve
  through `paths.py` instead. Verified live (not just compiled) against the real
  `registry/skills/*/SKILL.md` and `registry/constitution/constitution.md` files.
- `odoo_kg_to_neo4j.py` existed in the handoff already (it was mistakenly reported
  missing during initial exploration) — 1926 lines, fully real. Its only landmine was
  a cross-repo import bootstrap (`parents[4]` hunting for a sibling `agents/odoo`
  checkout) that's now unnecessary since `infra/` is a direct sibling in this repo;
  simplified to a plain repo-root `sys.path` insert. `LOCK_DIR`/`BACKUP_DIR` defaults
  moved off the original NAS path (`/volume1/khash-platform/...`) onto
  `KNOWLEDGE_GRAPH_DATA_PATH`.
- `tools_odoo/module_dev/safety_net.py`'s pre-install backup shelled out to an
  external `automated_backup_orchestrator.py` that wasn't part of the handoff at all
  (lived in the original engineer's own scratch space). Wrote a real replacement,
  `scripts/odoo_filestore_backup.py` (pg_dump + filestore tar, same `--odoo-filestore
  --tag <task_id>` CLI contract), rather than leaving a dead reference — this backup
  path is best-effort/non-blocking by design, but it should still do something real.

## Sanitization

- **"Jack" → "Operator"**, consistently, across 87 files including live identifiers
  (`ask_jack` → `ask_operator`, `PauseForJack` → `PauseForOperator`, etc.), not just
  prose — this was the human-approval-gate persona's name baked throughout the
  codebase as a role name, not a generic placeholder.
- **"Andrew" → "the project owner"** across ~49 files (comments quoting specific past
  decisions in the third person); lowercase `andrew` as a literal username/role value
  in test fixtures and a Postgres role-name comment → generic `test-user`/`oma_admin`.
- **"xerp" → "site"**, case-preserving, across 32 files — this was the employer's own
  product name embedded directly in function/variable identifiers
  (`is_custom_xerp_module`, `/opt/xerp/16/...`), not just comments.
- **"vgroep" (employer's internal domain/org name) → `oma`/`oma.local`**, including the
  Gitea org path and committer email.
- Remaining IP/hostname literals (`10.1.19.195/200/203`, `10.1.13.143/144`,
  `odoo-dev.int`, `gitea.int`) in `infra/gateway_client.py`, `infra/settings.py`,
  `infra/gpu_capacity_guard.py`, `infra/odoo_settings.py`, `infra/odoo_jit_apikey.py`,
  and the SSH-dependent tools are **deliberately left for Stages 3 and 5** rather than
  patched twice — those files get a real architectural rewrite there (GPU-gateway →
  Ollama/cloud switch; SSH → compose networking), not a find-and-replace.
- `README.md`, `ARCHITECTURE.md`, `ENGINEERING_LOG.md` are being rewritten/re-sanitized
  wholesale rather than patched in place — the originals are an internal phase-by-phase
  build log addressed to the original team, not portfolio-facing documentation.

## Still open (tracked, not forgotten)

- Stage 3: replace `infra/gateway_client.py`'s 3-GPU-host gateway with an
  `OMA_LLM_MODE=local|cloud` switch (Ollama by default; Anthropic via the existing
  `infra/cloud_escalation.py` path for cloud mode — chosen over OpenAI/OpenRouter
  because the code already has a real, working call path for Anthropic specifically).
- Stage 4: Gitea container, replacing `SECRETS/dev-agent.env` file-based credentials
  with plain env vars.
- Stage 5: remove SSH (`odoo_jit_apikey.py`, `toolchain.py`'s `ssh_cmd`,
  `fetch_modules_via_ssh.py`) and the two systemd units, replacing with compose
  networking and direct in-container execution.
