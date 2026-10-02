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

## Stage 6 evidence: one real, clean, fully-passing end-to-end run

Submitted through the real chat API (`POST /api/message`), running `app`
on the host (not in Docker -- see the Docker-socket tradeoff note below),
against the full stack (Postgres, Redis, Neo4j, Odoo CE, Gitea, LiteLLM
in cloud mode): **"On res.partner, please add a simple text field for
storing a LinkedIn profile URL."**

Real, verified result -- `status: completed`, task_id `228a280d-a26e-45c9-bc98-c3661368ef40`:
- Full real pipeline, in order: correction-check → risk classification
  (tier 1, module_dev) → intake grounding against the real Neo4j graph →
  memory read (50 relevant rows) → decomposition/blast-radius check →
  delegate → Build (round 1) → safety-net filestore backup → Code-Review
  → Testing/QA, independently reproducing the result → post-install graph
  diff → branch summary.
- Real commit on the self-hosted Gitea instance: branch
  `task/228a280d-a26e-45c9-bc98-c3661368ef40`, commit `7d7181edab`.
- Real module `oma_on_res_partner_please_ca9bb3eb` genuinely `installed`
  (confirmed via `ir_module_module`) in the real duplicate Odoo database,
  with the real `linkedin_profile_url` column physically present on
  `res_partner` (confirmed via `information_schema.columns`) -- not a
  claim, an actual schema change.
- Full 240-event trace saved to `demo-capture/trace_228a280d.json` for
  the static replay page.

Real bugs found and fixed by actually running this (not by reading the
code), each a config/path-config landmine of the same class as Stage 1-5's,
not a logic bug in the preserved mechanisms themselves:
- `toolchain.py`'s `_run_in_container()` required `OMA_ODOO_CONTAINER`
  unconditionally with no default -- added to `docker-compose.yml`'s `app`
  service environment (and `.env` for the host-run path).
- `toolchain.py`'s `_BASE_ADDONS_PATH` still hardcoded the original
  deployment's real OCA/custom addon repo paths (`/opt/site/16/addons`,
  `/opt/site/extra_addons/*`, etc.) -- replaced with the real addons path
  confirmed via `docker-compose.yml`'s own `odoo.conf` generation
  (`/usr/lib/python3/dist-packages/odoo/addons`). This was the actual
  root cause of "`res.partner` does not exist" -- the schema-check tool's
  `odoo shell` invocation was failing at the `--addons-path` CLI arg
  itself, not finding a real schema mismatch.
- `specialists/build/specialist.py`'s `_SANDBOX_CONTAINER` hardcoded the
  original deployment's separate physical sandbox container
  (`odoo16-dev2`) -- made env-configurable
  (`OMA_ODOO_SANDBOX_CONTAINER`), defaulting to the single `oma-odoo-1`
  container this port actually has. Real, disclosed tradeoff: the
  original used a SEPARATE container for sandbox isolation; this port
  isolates sandbox installs by database name only, sharing the same
  Odoo process/container as the main demo -- acceptable for a
  single-operator portfolio demo, not something to silently claim is
  equivalent.
- `SANDBOX_TEMPLATE_DB` (`odoo16_sandbox_golden_template`, a Postgres
  `-T` clone-template speed optimization with `base` pre-installed)
  didn't exist yet in a fresh environment -- created for real via
  `infra.odoo_admin.create_database()`, same mechanism Build itself uses.
- `tools_odoo/odoo_schema_client.py`'s ~20 functions default to
  `login="Admin"` (confirmed to exist on the original `odoo16_dev` only);
  correctly gated behind `is_fast_path_eligible(db)` / `_FAST_PATH_ELIGIBLE_DBS`
  (a real allow-list already built for exactly this), so the universal
  fallback path in `toolchain.py` is what actually runs against this
  port's duplicate/sandbox databases -- no code change needed here, just
  confirmed the existing gate does its job.
- `manager/replanning.py`'s `MODEL_CONTEXT_WINDOWS` dict only had entries
  for the two retired GPU-era model names; added real entries for the
  Stage 3 local/cloud model names so the context-pressure safety check
  uses their real windows instead of the conservative 32k fallback.
- `manager/gate_tamper_protection.py`'s detect-only manifest flagged 2
  files touched by the sanitization pass (comment-only changes, verified
  via diff) -- re-pinned after explicit confirmation (see git history;
  this needed its own sign-off, not something to do silently given what
  the mechanism is for).
- Real debug-noise cleanup: my own iterative bug-fixing tripped
  `check_repeated_failures()`'s real 2-strikes guard on `res.partner`
  (honest behavior -- it doesn't know the difference between "bug in my
  port" and "bug in the generated code" until a human says so). Retired
  those specific `agent_memory_events` outcome rows (`active=false`, the
  same supersession mechanism the schema already provides) after explicit
  confirmation -- not deleted, not hidden, just marked with a real
  `root_cause` note distinguishing port-infra bugs from capability gaps.

**Docker-socket tradeoff, decided explicitly**: running `app` fully
containerized requires mounting the host's Docker socket into it (so
`docker exec`-based specialist tools can reach the sibling `odoo`
container) -- that grants the container full control over the whole
Docker daemon, a materially bigger attack surface than this app actually
needs, in real tension with its own "sandboxed, allow-listed execution"
design. Decided to run `app` on the host instead (plain `uvicorn`, same
venv as every other smoke test in this log) for both this capture and as
the documented default -- `docker exec` from the host is just the normal
Docker CLI a developer already has, zero socket-mounting, zero added
attack surface. The `app` Dockerfile/compose service stay in the repo,
clearly marked opt-in, for anyone who wants full containerization and is
comfortable with that specific tradeoff.

## Stage 7 evidence: secret scan + real clean-clone verification

- `gitleaks detect --log-opts="--all"` and `trufflehog git file://.` both
  run against the full history (9 commits): zero findings in either,
  every run. The two gitleaks hits that ever appeared were always the
  same untracked, gitignored `oma/.env` on disk (confirmed via
  `git status --short` before every commit) -- never staged, never
  committed.
- One real credential materialization incident during this build, not a
  repo issue: the operator's real Anthropic and OpenAI API keys appeared
  in plaintext in this session's own chat transcript (the `!`-prefixed
  shell-injection mechanism didn't fully suppress the echoed command).
  Neither key was written to any tracked file. Flagged to the operator
  live, with a recommendation to rotate both afterward.
- Final sweep for the class of bug Stage 1-6 kept finding (hardcoded
  values from the original deployment that are live code, not just
  comments) found two more: a raw `httpx` call in
  `scripts/draft_validator_from_cluster.py` bypassing
  `infra.gateway_client` entirely with the old GPU host baked in, and a
  live user-facing error string in `toolchain.py` naming a host that
  doesn't exist in this topology. Both fixed.
- Real clean-clone test: cloned the repo fresh into `/tmp`, under a
  separate Compose project name (`-p oma-clonetest`) so it couldn't
  touch the real demo stack's volumes, and brought up Postgres/Redis/
  Neo4j/Odoo CE from nothing but `cp .env.example .env` + `docker compose
  up`. Fresh volumes, zero prior state: Postgres auto-created
  `odoo16_dev` with real demo data (40 `res.partner` rows, same as
  Stage 1), Odoo answered `HTTP 200`. No absolute paths from the
  development machine found anywhere in the cloned tree. Confirmed the
  real demo stack (and its captured evidence -- the installed module,
  the Gitea commits, the seeded knowledge graph) was untouched
  throughout and afterward.

## Stage 8: one-command setup, tested to a genuinely clean run

The documented multi-command setup worked, but didn't match how real OSS
projects onboard (confirmed via research: the convention is a single
`./setup.sh`/`make up`, not a sequence of manual steps a stranger has to
get right unaided). Built `setup.sh` as that single entrypoint, and
found four more real gaps by actually running it against a from-scratch
clone under an isolated Compose project name (so it couldn't touch the
real demo stack's volumes/evidence):

- **The sandbox-isolation database and the golden-template database were
  never actually automated** -- I'd created both by hand while debugging
  Stage 6 and never wrote that down as a setup step. A fresh clone would
  have hit the exact same `createdb` failures I hit. Wrote
  `docker/provision-odoo-databases.sh` (idempotent, called by `setup.sh`)
  to create both for real via the same `infra.odoo_admin` mechanism Build
  itself uses.
- **`.env.example` never documented `OMA_ODOO_CONTAINER`** -- `toolchain.py`'s
  `_run_in_container()` requires it with no default, so a fresh clone
  would crash with the exact `KeyError('OMA_ODOO_CONTAINER')` I hit
  during Stage 6. Added it, plus `OMA_ODOO_SANDBOX_CONTAINER` and
  `OMA_GITEA_CONTAINER` for the same reason.
- **`docker/gitea-bootstrap.sh` and `docker/seed-knowledge-graph.sh` both
  hardcoded their target container names** (`oma-gitea-1`, `oma-odoo-1`)
  instead of reading them from env like every other docker-exec call site
  in this codebase. Harmless under the single documented default (the
  compose file pins `name: oma`, so these always happen to be right) --
  but caught two real, silent cross-talk bugs during testing: the test
  run's Gitea bootstrap actually talked to the *real* demo stack's Gitea
  instance (wrong port fell back to the default, which belonged to a
  different running stack on the same machine) before this was fixed.
  Fixed for real env-driven consistency, not just to unblock the test.
- **The default `.env.example` addresses assumed the app runs inside a
  container** (`http://odoo:8069`, `http://ollama:11434`) but `setup.sh`'s
  whole point is running the app on the *host* by default (see the
  Docker-socket tradeoff above) -- host-side code can't resolve a
  compose-network service name. Flipped the defaults to host-published
  ports (`127.0.0.1:*`) and moved the container-network overrides onto
  the opt-in containerized `app` compose service instead (inverting what
  Stage 6 had), including `infra/settings.py`'s own gateway-URL default.

Verified for real, end to end, zero manual steps beyond pasting a cloud
API key: full teardown (`docker compose down -v`, deleting `oma/.env`
and `.venv`), one `./setup.sh` invocation, and a fresh, genuinely
isolated stack came up with real demo data (40 `res.partner` rows) and a
freshly-seeded 544-module knowledge graph -- not a copy, confirmed via
direct Postgres/Neo4j queries against the isolated instance. Two
unrelated flakes hit during testing and are *not* repo bugs: a transient
Colima networking error on a container recreated immediately after
`down -v` (resolved on retry), and a leftover `uvicorn` process from an
earlier manual test run squatting on a port (killed, unrelated to
`setup.sh` itself).

## Stage 9: a real sanitization miss, found by the user's own question

The user asked a sharp, simple question: a fresh clone shouldn't show any
historical tasks at all, so why did theirs? Checking it surfaced something
this whole port had missed: `oma/state/scope_certification.json` (committed
since the very first commit) was carried over from the original handoff
**unscrubbed** -- 95 real historical entries, dates from August 2026, up to
31 consecutive real runs per scope, from the actual pre-port system.

Reviewed the actual content before deciding what to do: every entry turned
out to be a synthetic, auto-generated combinatorial-test goal (the pairwise
test-generation harness's own output -- standard Odoo models like
`sale.order`/`purchase.order`/`res.partner`, no real company names, no real
people, no client-specific business logic). Not a secret-scanner miss --
gitleaks/TruffleHog correctly didn't flag it, because none of it is a
credential. But it's real pre-port operational history, and shipping it
would make a fresh checkout look like it already had a real usage track
record it never actually earned in this environment.

Fixed by resetting the file to `{}` -- confirmed via
`manager/scope_certification.py`'s own `_load_state()` that an empty/absent
state file is the genuinely correct "no certification history yet" case,
not a workaround. Also used this as a live opportunity to clean the
*running* instance's `agent_memory_events` task history (10 tasks, all
from this session's own testing/debugging, none from the actual operator)
back to a real clean slate via the same `active=false` mechanism used
throughout this log -- done only after explicit confirmation, including a
second, more carefully-scoped pass after an appropriately-cautious
unbounded-predicate rejection the first time.

Open question worth tracking: this class of "real historical state file
carried over unscrubbed" might have other instances beyond this one --
worth a dedicated pass over `oma/state/` and any other persisted JSON if
this repo accumulates more such files later.

## Stage 10: real bugs found by actually using the running system

The operator ran the live instance themselves and reported four real things.
Each was investigated and fixed at the root, not patched around:

**Frontend sanitization gap.** The Stage-whatever Python-only sanitization
pass (`--include=*.py`) never touched `oma/ui/chat/index.html` -- it's HTML,
not Python. 12 "Jack"/"jack" and 84 "Andrew" references shipped in the one
file every visitor actually looks at first. Fixed: `jack` (the CSS
class/role) -> `operator`, "Jack" (the displayed name) -> "Operator",
every `Andrew`-attributed comment -> `the project owner`, matching the
exact phrasing already used throughout the `.py` files. Verified: zero
matches anywhere in the repo.

**Three real findings from one real task run.** A single real task
(`f185ca1f-...`, "add a fax number field") surfaced three independent,
genuine problems, not one:
- `pg_dump` (host, Homebrew, 14.20) refused to dump the `postgres:16`
  container's database -- Postgres pg_dump cannot dump a server newer
  than itself. Fixed by running `pg_dump` (and the filestore `tar`) via
  `docker exec` *inside* the container that actually matches its own
  server version, copying the result out with `docker cp` -- removes the
  host/container version coupling entirely rather than pinning a host
  tool version that will drift again.
- `bandit` (the security linter `tools_odoo/module_dev/toolchain.py`
  already shells out to) was never installed anywhere -- the `odoo:16`
  image is stock, no custom build step ever ran `pip install`. Installed,
  and baked into the `odoo` service's own compose startup command so a
  fresh clone gets it for free.
- "REGRESSION SIGNAL: 2/3 core smoke checks failed" after every install --
  a false positive. `tools_odoo/smoke_suite.py`'s own checks create a
  `sale.order` and a `project.task`, but the demo database only had
  `base` + 7 deps installed -- those models genuinely didn't exist.
  Installed `sale` and `project` (baked into `odoo-init` for fresh
  clones). All 3 checks now pass for real.

**No live progress during a run.** The operator could see a task's full
result once it finished, but nothing while it ran -- despite the real
backend publishing real trace events (`node_state_changed`, token deltas)
from the moment a turn starts. Root cause: `POST /api/message`
(`ui/chat/server.py`) `await`ed the entire round loop before returning
anything, so the frontend only learned a task's `task_id` after there was
nothing left to watch. Fixed: `run_turn()` (`manager/loop.py`) now accepts
an optional pre-generated `task_id` (every existing caller unaffected,
still generates its own if not given); `post_message()` mints the
`task_id` up front, launches the real round loop as a background task, and
returns immediately. The frontend opens this task's SSE stream
(`/api/stream/{task_id}`) the instant it gets the `task_id` back, and
separately polls a new `GET /api/message_result/{task_id}` for the real
rendered reply once the background turn finishes. Verified live: the POST
now returns in under a second (previously 10-70s+), and the opened stream
showed real token-by-token Build output arriving as it was generated.

**Proof the constraint-graph splitting is real, not a single-node
illustration.** Submitted a genuinely multi-part goal (a new model, a
computed field, a related field, a security rule, and a server action,
spanning two existing models) through the now-fixed live path. It
decomposed into 9 real sub-contracts with independently tracked nodes
(`equipment_loan_model`, `active_loans_count_field`,
`partner_chatter_log`, `batch_return_action`, ...) showing genuine
`blocked` -> `running` -> `failing` transitions as later pieces waited on
earlier ones. It correctly, honestly paused on the one piece needing
Odoo server-action automation this system has no real codegen support
for yet, rather than faking it -- exactly the "disclosed, not silently
incomplete" behavior the rest of this codebase holds itself to.

**One more sanitization-class bug, found while cleaning up this session's
own test data.** `manager/correction.py`'s `list_pending_proposed_rules()`
was the same bug class as Stage 9's `read_main_chat_history()` fix:
no `active = true` filter, so an archived proposed-rule row kept
reappearing in the pending-items queue. Fixed and verified (18 leftover
test-session rows correctly disappeared from `/api/pending` after the
fix, where archiving alone hadn't been enough).

All of this session's own test task_ids, chat rows, and proposed-rule
rows were archived (`active=false`, exact row IDs, never deleted) after
explicit confirmation, same discipline as Stage 9. Checked but
deliberately NOT changed: a broader sweep found ~25 more call sites
across `manager/replanning.py`, `manager/dashboard.py`, `manager/tools.py`,
and `manager/learning.py` that also read `agent_memory_events` without an
`active` filter -- left alone because, unlike the two confirmed cases
above, these may intentionally read full history for learning/analytics
rather than display, and changing them without verifying each one
individually risks a real behavior change to core production logic. Noted
here as a real, open question, not silently fixed or silently ignored.

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
