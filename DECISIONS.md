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

## Target shape: on-demand, not always-on

Mid-build, the brief changed: this does not run 24/7 anywhere paid. The finish
line for the Docker Compose work is one real, clean, fully-passing end-to-end
run on demand (`docker compose up`, laptop or screen-share), not a hosted URL.
After that run is captured (see below), the deliverable becomes a recorded
demo + a free static GitHub Pages replay page (driven by the real captured
trace JSON) + clone-and-run instructions — no paid hosting anywhere.

## Stage 1 evidence: Postgres + Redis + Neo4j + Odoo CE, real and healthy

Ran via `docker compose up -d postgres redis neo4j odoo-init odoo`, verified
through the actual app code paths (`infra/settings.py`, `infra/neo4j_client.py`,
`infra/odoo_settings.py`), not just `docker exec`:

- Postgres 16, Redis 7, Neo4j 5 Community, Odoo 16 CE all report `healthy`.
- `odoo-init` (one-shot `odoo -i base --stop-after-init`) exited 0, loaded 8
  modules with demo data (40 `res.partner` demo records confirmed in the db).
- `scripts/001_agent_memory_events.sql` applied for real against the `oma`
  database; `scripts/010_ensure_agent_memory_events_partitions.py` created 4
  real monthly partitions (`agent_memory_events_2026_10` through `2027_01`).
- A real Python smoke test (`infra.settings.load_postgres_settings()` +
  `psycopg2`, `infra.settings.load_redis_settings()` + `redis`,
  `infra.neo4j_client.get_neo4j_driver()`, and `infra.odoo_settings.load_odoo_settings()`
  + `xmlrpc.client` against `/xmlrpc/2/common`) connected to all four and got
  real responses, including the Odoo server-version handshake.
- `infra/odoo_settings.py`'s production-guard assertion (`_assert_safe_odoo_target`)
  fired and passed against the new topology without any change to its actual
  logic — confirming the "never touch anything resembling production" safety
  mechanism survived the port intact, pointed at the new single-container target.
- **Colima, not Docker Desktop**, is the local Docker runtime on this machine.
  Found and fixed one real port collision during this: a native Homebrew
  Postgres was already bound to `127.0.0.1:5432`, shadowing the container's
  published port — remapped the host side to `55432` via `OMA_PG_HOST_PORT`
  (container-internal port/networking unaffected).
- Fixed two Dockerfile/health-check bugs found by actually running this
  (not just reading it): the Odoo CE image has `curl` but not `wget`
  (health check rewritten); `neo4j` wasn't in `requirements.txt` at all
  despite `infra/neo4j_client.py` depending on it directly — added it for
  real, not worked around.

## Stage 3 evidence: LLM gateway two-mode switch

- `infra/gateway_client.py` kept its real mechanism (named backend pools per
  role, each with its own circuit breaker/bulkhead/pooled httpx client,
  dispatched per call by `backend_for_model()`) completely unchanged; only
  `infra/settings.py`'s `_default_gateway_base_url()` changed what server
  sits behind each pool, switched by `OMA_LLM_MODE`.
- `local` (default): all three pools point at the `ollama` compose service.
  Chose `qwen2.5-coder:7b` (coder role), `qwen2.5:7b-instruct` (reasoning
  role: manager/classifier/code-review/testing-qa), `qwen2.5:3b-instruct`
  (fast-extraction role) -- real, pullable Ollama models sized to run on a
  laptop (CPU/Metal), not a GPU server, while still preserving the original's
  three-tier role split (coder vs. reasoning vs. fast-extraction) rather than
  collapsing it to one model.
- `cloud` (`docker compose --profile cloud up -d`, `OMA_LLM_MODE=cloud`):
  all three pools instead point at a self-hosted `litellm` container
  (`docker/litellm-config.yaml`), which proxies to the real Anthropic API
  using a key the operator supplies. Chosen over OpenAI/OpenRouter because
  (a) the codebase already has a real, working Anthropic call path
  (`infra/cloud_escalation.py`, kept as-is -- see below) and (b) LiteLLM as
  an OpenAI-compatible proxy in front of a cloud model was *already* a real
  part of this system's own architecture (`BACKEND_EXTERNAL`'s docstring:
  "the sandbox's LiteLLM, from Phase 14 onward") -- reusing that pattern for
  the primary gateway's cloud mode, rather than inventing a second,
  unrelated cloud-integration mechanism.
- `infra/cloud_escalation.py`'s own narrow, off-by-default, cost-governed
  escalation path (three named Classifier/Planner/Judge call sites only,
  separate cost ceiling, separate enable flag) is untouched and independent
  of `OMA_LLM_MODE` -- both mechanisms coexist, exactly as before.
- The old GPU-host wire-level model-alias rewrite (`_wire_model_id()`) is
  kept as a real mechanism (not deleted), now empty-by-default and
  env-configurable (`OMA_WIRE_MODEL_ALIASES`), since neither Ollama nor
  LiteLLM need alias rewriting the way the old GPU Worker 02 did.

## Stage 4 evidence: self-hosted Gitea, real commit verified

- `docker/gitea-bootstrap.sh` (idempotent, re-runnable) provisions a real
  admin user, org, repo, and access token against the `gitea` compose
  service on first boot, and writes the token into `oma/.env` for both
  the `app` container and a host-side run to pick up.
- `tools_odoo/module_dev/vcs.py`'s separate `SECRETS/dev-agent.env` file-read
  replaced with plain `os.environ` reads (the container's own environment
  *is* the credential boundary now) -- its actual Gitea REST API mechanism
  (branch-per-task, one real commit per round via the "change multiple
  files" contents API) is otherwise unchanged.
- Verified for real: `vcs.commit_validated_round()` against the live
  container returned a real commit SHA
  (`694261a739b0a0765de33e7f5746d31e593a2fb6`), confirmed via the Gitea API
  that branch `task/smoke-test-0001` exists with that commit, authored as
  `dev-agent`; `vcs.read_last_validated_commit()` read the same files back
  with the expected `<module_name>/<path>` keys.

## Stage 2 evidence: real knowledge graph, seeded from Odoo CE's own addons

- Copied Odoo CE's own bundled addons directory straight out of the running
  `odoo` container (`docker cp`, no SSH) -- 71 real module directories, not a
  synthetic fixture.
- Ran Stage A unmodified (`tools_odoo/knowledge_graph/driver.py`): parsed 75
  modules, 333 real `needs_llm_review` flags.
- The original pipeline's Stage B (LLM-written architectural notes,
  `build_model_cards.py`) wasn't included in this handoff. Rather than block
  on it or fabricate narrative content, wrote
  `tools_odoo/knowledge_graph/build_stage2_derived_artifacts.py`: derives
  `model_cards.jsonl` (FIELDS:/INHERITS: grammar) and `odoo_full_module_graph.json`
  (author/topo_order/external_deps) mechanically from Stage A's own real
  structural facts and each module's real `__manifest__.py` -- zero LLM calls,
  zero fabricated content. Confirmed by direct inspection of
  `scripts/odoo_kg_to_neo4j.py` that its live ETL path only ever regex-parses
  FIELDS:/INHERITS: out of model_cards.jsonl and reads `extends` straight off
  final_module_graph.jsonl (the EXTENDED_BY: grammar parsers are dead code,
  per that script's own 2026-08-13 fix note) -- so this was sufficient, not
  a shortcut around something still load-bearing.
- `merge_final_graph.py` ran unmodified with an empty notes file / empty
  review-output dir, which it already handles correctly: every Stage A
  review flag stays honestly `not_reviewed` rather than being marked
  resolved. 333 unresolved items across 33 modules, accurately reported.
- Ran the real, unmodified `odoo_kg_to_neo4j.py --mode full` against this
  local Neo4j. Verified with real Cypher: 98 `Module`, 395 `Model`, 3102
  `Field`, 758 `View` nodes, 693 `DEPENDS_ON` edges. Confirmed the exact
  `infra.neo4j_client` module Code-Review's hallucination filters import
  returns correct data (`res.partner`'s real fields, `account`'s real
  module dependencies).

## Stage 3 evidence, continued: real hardware constraint, cloud mode verified

- Pulled all three local models successfully (qwen2.5-coder:7b, qwen2.5:7b-instruct,
  qwen2.5:3b-instruct), but the actual demo/build machine turned out to have
  only 8GB total RAM, with Colima's VM capped at its 2GB default -- nowhere
  near enough to load a 7B model (`ggml_aligned_malloc: insufficient memory
  (attempted to allocate 4166.82 MB)`, a real error from the real container,
  not a code bug). `ModelGatewayClient`'s own retry/circuit-breaker mechanism
  handled the failure exactly as designed (retried, then raised a clear
  `GatewayUnavailableError`), which is itself evidence the wiring is correct
  even though the local generation didn't complete.
- Rather than resize Colima and fight an 8GB ceiling for a stack that only
  needs to run once for a capture (not stay up permanently -- see "Target
  shape" above), switched the verification run to `OMA_LLM_MODE=cloud` via
  the already-built LiteLLM path, re-pointed at OpenAI (`gpt-4o-mini`, the
  operator's available credit) instead of Anthropic for this run --
  `docker/litellm-config.yaml`'s `model_list` is just config, swapping
  providers needed no code change.
- Verified for real, twice: a raw HTTP call against the `litellm` service
  got a real OpenAI completion back, and a full `ModelGatewayClient.generate()`
  call (the actual mechanism every specialist calls through, circuit
  breaker/bulkhead/retry included) got a real, non-canned model response.
- Stopped the `ollama`/`ollama-init` containers after this to free RAM for
  the Stage 6 capture run -- local mode stays fully wired and documented in
  `.env.example` for anyone cloning this on a machine with more RAM/a GPU.

## Stage 5 evidence: SSH removed, replaced with local `docker exec`

- `infra/odoo_jit_apikey.py`, `tools_odoo/module_dev/toolchain.py`'s
  `_run_in_container()` (the shared utility ~40 call sites go through),
  `tools_odoo/codebase_read.py`, `tools_odoo/odoo_source_introspection.py`,
  and `tools_odoo/spot_check.py` all replaced their SSH-to-a-second-host
  transport with a direct `docker exec` into the sibling `odoo` compose
  service -- same real commands (`odoo-bin shell`, `find`/`cat`, `grep`),
  same timeout/error handling, just no second host to hop to.
- Real, found-by-running-it bug: a fresh `odoo shell`/`odoo -i` invocation
  via `docker exec` doesn't inherit the main process's CLI-arg-based db
  connection config, because the official image's entrypoint translates
  `HOST`/`PORT`/`USER`/`PASSWORD` env vars into CLI args for ONE process,
  not into `odoo.conf` itself. Fixed at the source: `docker-compose.yml`'s
  `odoo`/`odoo-init` commands now write a real `odoo.conf` with db
  credentials before starting, so every subsequent `docker exec` against
  that same container (shell, install, scaffold, coverage) reads the same
  config consistently.
- `tools_odoo/knowledge_graph/fetch_modules_via_ssh.py` deleted outright --
  superseded by Stage 2's `docker cp` approach, no remaining callers.
- Verified for real: a JIT Odoo API key created and revoked via
  `infra.odoo_jit_apikey` (through `docker exec`, no SSH), and
  `toolchain._run_in_container()` returning real container output.

## Still open (tracked, not forgotten)

- The two `oma-backlog-triage.service`/`.timer` systemd units were already
  dropped during the initial scaffold pass (Stage 0); `recurring_backlog_triage.py`
  itself is kept as a plain script anyone can run manually or wire to any
  scheduler (cron, a CI job) -- no systemd-specific code remains.
- Stage 6: full app wiring + one real end-to-end UI run (classify → decompose
  → Build → Code-Review → Testing/QA → install), captured as the recorded demo.
- Record the demo, build the static GitHub Pages replay page from its real
  trace JSON, rewrite README as a case study (demo → replay page → clone
  instructions).
