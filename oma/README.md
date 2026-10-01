# Odoo Manager Agent

One Manager agent + three specialists (Build, Code-Review, Testing/QA)
built to actually do Operator's four real Odoo test tasks. Full spec:
`/home/andrew/projects/docs/planning/ODOO_MANAGER_AGENT_BUILD_PLAN_2026-07-06.md`
(the step-by-step build plan this code follows, phase by phase) plus the
technical and vision documents in the same folder for the reasoning
behind it.

**Isolation, explicit:** this is new, standalone code. It does not import
from or depend on the production agents in `agents/src/` (NEXUS, Router,
Memory, Sanitizer) and it does not touch Operator's sandbox
(`10.1.24.0/24`) at all until Phase 14 of the build plan explicitly says
to. It runs against this company's own real dev-tier infrastructure
(`postgres-dev.int` / `redis-dev.int`), in a database and Redis DB index
dedicated to this project alone -- never the same namespace other dev
agents already use, and never Docker/local throwaway containers (an
earlier revision of this did stand up local Docker containers; that was
wrong and was torn down).

## Secrets: one file, `.env`

`.env` (git-ignored, `chmod 600`) is **the single place** every
credential, address, and port this project needs lives. Every module
reads it dynamically via `infra/settings.py` -> `os.environ`. Nothing is
ever pasted into any other file. Copy `.env.example` to `.env` and fill
in real values (ask Andrew/Operator for anything you don't already have from
existing platform config).

## Real infra this project uses

- **Postgres** — `postgres-dev.int` (`10.1.13.144:5432`), user `andrew`.
  Own, separate database: **`odoo_manager_dev`** (created 2026-07-07,
  owned by `andrew`) -- distinct from `dev`, `devbox_scratch`,
  `synthetic_mock_sales`, and the `odoo_scrub_temp_*` databases that
  already live on this same instance for other work.
- **Redis** — `redis-dev.int` (`10.1.13.143:6379`). DB0/1/2 are already
  occupied by other agents (confirmed via a read-only `DBSIZE` check per
  index on 2026-07-07 -- 41/5/2 keys respectively). **This project uses
  DB3, and only DB3.** Before ever picking a DB index for a future
  project, check current usage the same way rather than assuming.
  Authenticates as a dedicated ACL user, `odoo-manager-agent`, scoped to
  the `oma:*` key prefix only (provisioned via the existing
  `nexus-governor` admin credential already in this codebase, the same
  mechanism that provisions the `control-plane` ACL user, with Andrew's
  explicit go-ahead — the `default` user credential he initially gave
  returned `WRONGPASS` consistently and was never resolved). Confirmed:
  can read/write within `oma:*`, correctly blocked outside it.
- **Model gateway** — `10.1.19.195:9090/v1`, an OpenAI-compatible
  `llama-swap` proxy in front of vLLM, serving four real models:
  `qwen3.6-27b` (Manager), `qwen3-coder-30b-a3b` (Build specialist),
  `qwen3-14b` (all cheap classification calls), and
  `deepseek-r1-distill-qwen-32b` (Testing/QA escalation). Confirmed live
  via `/v1/models`. The older `10.1.19.194` Ollama target is
  decommissioned — never referenced. Only one model is loaded in VRAM
  at a time (llama-swap), so switching models costs a real ~30s cold-swap
  latency that the gateway client's timeouts are sized around. Repointed
  to the sandbox's LiteLLM (`10.1.24.244:8000/v1`) only at Phase 14.

## Setup

1. `cp .env.example .env`, fill in real values.
2. Apply the schema to `odoo_manager_dev` (idempotent-safe to rerun --
   `psql -v ON_ERROR_STOP=1` will just fail loudly if objects already
   exist rather than silently double-creating anything):
   ```bash
   set -a && source .env && set +a
   PGPASSWORD="$OMA_PG_PASSWORD" psql -h "$OMA_PG_HOST" -p "$OMA_PG_PORT" \
     -U "$OMA_PG_USER" -d "$OMA_PG_DB" -v ON_ERROR_STOP=1 \
     -f scripts/001_agent_memory_events.sql
   ```
3. **Dedicated virtualenv, not the shared base venv:**
   ```bash
   python3 -m venv .venv
   ./.venv/bin/pip install -r requirements.txt
   ```
   (An earlier revision of Phase 2 briefly installed `instructor` into
   the shared `venv-base` used by other agents on this devbox, which
   downgraded `rich`/`jiter` there — caught and reverted immediately, and
   fixed properly by giving this project its own venv so it can never
   touch shared dependency versions again. Always use `./.venv/bin/python3`
   / `./.venv/bin/pip` for this project, never the bare `python3`/`pip`
   that resolve to the shared base venv.)

## Status: Phase 1 (foundations)

What's built and tested, per the build plan's Phase 1:

- `agent_memory_events` table, partitioned by month, with the
  `verified`/`stale_after`/`root_cause` columns already in place
  (`scripts/001_agent_memory_events.sql`), live in `odoo_manager_dev` on
  `postgres-dev.int`.
  - **Adapted from the build plan's literal DDL in two ways, both forced
    by actually running it against Postgres 16** (documented inline in
    the SQL file): the primary key had to become `(id, created_at)`
    since a partitioned table's PK must include the partition column;
    the `supersedes_id` self-referential foreign key had to be dropped
    since a single-column FK can no longer target a composite PK -- the
    `mark_superseded` trigger already enforces that relationship
    procedurally, so nothing is actually less safe.
  - Verified with `scripts/test_memory_schema.py` (self-cleaning,
    rerunnable): issue→attempt→outcome chaining by `task_id`, the
    supersede trigger, `active_agent_rules` filtering, and the
    `stale_after` expiry policy all pass against the real dev database.
- The fencing-token lock primitive (`infra/fencing.py`), tested in
  `tests/test_fencing.py` against the exact race it exists to prevent:
  a lock expires mid-write, a second caller acquires a new lock and
  commits, and the first caller's now-stale token is correctly rejected
  by `check_fence()` even though it never learned its lock had expired.
  Passing against real `redis-dev.int`, DB3, via the `odoo-manager-agent`
  ACL user.
- The Postgres-side JIT credential pair (`infra/jit_postgres_role.py`:
  `create_scoped_role`/`drop_scoped_role`), tested in
  `tests/test_jit_postgres_role.py` against the real `andrew` credential
  on `postgres-dev.int` (confirmed to hold `CREATEROLE` there): a scoped
  role can do exactly what it was granted and nothing else, `VALID
  UNTIL` blocks a *new* login after expiry, and `drop_scoped_role`
  actually removes the role rather than relying on expiry alone (which,
  per the technical document's §8, does not end an already-open
  session).

## Status: Phase 2 (LLM gateway client)

- `infra/gateway_client.py`: `ModelGatewayClient` -- pooled
  `httpx.AsyncClient`, hard per-call timeout (90s default, sized above
  the ~30s cold model-swap latency), jittered exponential-backoff retry
  (3 attempts), a circuit breaker (opens after 5 failures in 60s, 30s
  cooldown), and a bulkhead semaphore **partitioned by backend**
  (`local_llama_swap` vs `external`), not by model tier -- matching
  Nexo's real, corrected partitioning (§2.4). Accepts a `model` parameter
  per call.
- `infra/structured_output.py`: `call_structured()`, a thin wrapper on
  top of the client using an instructor-style validate-and-reask loop
  against a Pydantic schema, with its own `retry_sub_budget` tracked
  completely separately from the client's network-level retries.
  `strip_think_block()` handles `deepseek-r1-distill-qwen-32b`'s visible
  `<think>...</think>` trace.
- **A real quirk found and fixed, not just designed on paper:**
  qwen3-14b/qwen3.6-27b, even with the model's own `/no_think`
  convention, can return a response containing only an **unmatched
  closing** `</think>` tag with no visible opening one (the chat
  template opens it as part of the prompt itself, invisible to the
  client) — a real captured example: a `/no_think` "reply with exactly:
  pong" request came back as literally `": pong\n\n</think>\n\npong"`.
  A regex requiring both tags would silently leave that artifact in the
  "cleaned" text. `strip_think_block()` now handles both the full-pair
  case and this unmatched-closing-tag case, and there's a dedicated unit
  test (`test_strip_think_block`) pinned to this exact real string so it
  can't regress silently.

## Status: Phase 3 (Manager's memory and context assembly)

Built by directly reading Nexo's real source
(`agents/src/nexo/context/assembler.py`, `context/formatter.py`,
`pipeline/session_memory.py`, `context/summarizer.py`) rather than
relying only on the build plan's summary of it.

- `manager/memory.py`: `ContextSelector`/`ContextFormatter`, mirroring
  Nexo's real split -- selector decides what fits in a token budget
  (greedy fill, history trimmed backward from the most recent turn);
  formatter turns the result into system-prompt text. `budget_exhausted`
  and `memory_query_ran` are genuine additions beyond what Nexo tracks
  (per §2.1's correction, Nexo's own `is_partial` reports upstream
  retrieval completeness, not whether the selector itself dropped a
  candidate) -- built so "no memory found" and "memory wasn't queried"
  can never render identically. `read_project_memory()` is a plain
  parameterized query against `agent_memory_events`/`active_agent_rules`,
  no LLM call involved.
- `manager/session.py`: `SessionFactsStore` (in-process dict, 24h TTL,
  matching Nexo's real shape at this project's much smaller scale) +
  `compress_session()` (token-triggered, 5-section summary, last 8 turn
  pairs kept verbatim). Compression threshold set to 20,000 tokens,
  chosen fresh for the Manager's own model (qwen3.6-27b, verified 65,536
  context window) rather than inherited from either of Nexo's two
  disagreeing internal values (20K config default vs. 27K live, per
  §2.2's correction).
- **Two real, confirmed model-behavior findings from testing this phase
  against the real gateway, not just designing on paper:**
  1. `qwen3.6-27b` (the Manager's own model) does **not** honor `/no_think`
     at all -- confirmed directly, identical verbose output with or
     without it. It always produces a long hidden chain-of-thought
     (same structural quirk as Phase 2's finding: only the closing
     `</think>` appears in visible content, the opening tag is part of
     the invisible prompt template).
  2. A first pass of the compression call used `max_tokens=1024`, too
     small for this model's real thinking length -- the response got
     truncated *before* reaching `</think>`, so the raw, unfinished
     reasoning monologue leaked into the "summary" instead of the actual
     5-section output. Fixed two ways: raised the budget to 6000, and
     added an explicit check (`"</think>" not in raw` → treat as a
     failure, not a degraded-but-usable summary) so this can't silently
     recur.
  3. Per the plan's own requirement, tested specifically for **context
     drift**, not just "does a summary get produced": a constraint
     stated at the very start of a conversation ("don't touch the
     accounting module without asking") survived two full rounds of
     compression through many turns of deliberately unrelated filler --
     confirmed present, correctly worded, in both intermediate and final
     summaries.

## Status: Phase 4 (Manager's charter and classification cascade)

- `MANAGER_CONSTITUTION.md` and `sensitive_paths.yaml` written verbatim
  per §0.5.3/§0.5.4, live at the project root.
- `manager/charter.py`: `check_sensitive_paths()` -- the single most
  important function in the whole Manager, per the build plan's own
  words. Purely mechanical: a set-intersection/glob check against
  `sensitive_paths.yaml`, no LLM call anywhere in this file. Reads both
  files fresh on every call (no import-time caching), so editing
  `sensitive_paths.yaml` over the life of the project takes effect
  immediately. Handles: no-hit (tier 1/2 by read/write), a single hit
  (that rule's tier), multiple simultaneous hits (highest tier wins),
  and `touches_schema_or_permissions` (forces tier 4 regardless of
  anything else).
- `manager/classify.py`: `classify_capability_class()` -- the
  capability-class cascade (`data_change` / `module_dev` / `readonly`),
  shaped after Nexo's real `complexity_classifier.py` (§2.5): a
  zero-model-call keyword fast-path first, a cached, whitelisted,
  temperature-zero `qwen3-14b` call only when genuinely ambiguous,
  defaulting to `readonly` (the most tool-restricted class) whenever
  unsure or on any failure. **Never conflated with `tier`** -- tier stays
  entirely separate and mechanical, exactly per §2.5's instruction.
- Reused Phase 2/3's discipline deliberately, not re-derived: LLM
  responses run through `strip_think_block()`, plus an explicit
  completeness check (`"</think>" not in raw` → treat as incomplete,
  don't trust it) before parsing -- the same pattern
  `compress_session()` established in Phase 3, now proven to generalize
  to a second, different thinking-mode model call site.
- **Tested against a genuine labeled test set, written down in advance**
  (per the build plan's explicit requirement, not an exploratory pass):
  all 4 of Operator's real tasks (each correctly caught by the zero-model
  keyword fast path *and* independently re-verified through the full
  cascade), one deliberately ambiguous request (confirmed it actually
  falls through past the fast path to the LLM stage), one
  harmless-sounding request that concretely touches `account.move`
  (confirmed tier 3 fires via the mechanical check regardless of
  capability-class label), and one nonsense/garbage input (confirmed it
  fails safe to `readonly` rather than erroring out). Also a dedicated
  test proving the completeness-check safety net itself works: a
  forced-truncation call (`max_tokens=1`) is caught internally and
  degrades to the conservative default rather than propagating an error.

## Status: Phase 5 (specialist registry and task-contract schema)

- `contracts/schema.py`: `SpecialistType`, `CapabilityClass`,
  `AutonomyTier`, `CompensatingAction`, `TaskContract`,
  `VerificationResult` -- pulled directly from the build plan's §8/§9,
  verbatim. Plus `SpecialistOutput` (named in the plan's step 2 as the
  return shape a specialist actually hands back, but not given verbatim
  fields there -- authored here, deliberately minimal): a specialist
  reports what it *believes* it did; turning that into a trusted
  `VerificationResult` is the Manager's job later (Phase 6), combining
  it with a deterministic spot-check and, for Build-specialist work, the
  Testing/QA specialist's own independent report. Per the technical
  document's own words: "never the specialist's own word, and never
  only the QA specialist's self-report either."
- `specialists/base.py`: `Specialist` -- a `Protocol` with exactly one
  method, `run(contract) -> SpecialistOutput`. Kept as small as possible
  on purpose.
- `specialists/registry.py`: `register()`/`get()`/`registered_types()`,
  intentionally boring. `get()` on an unregistered `SpecialistType`
  raises a clear `SpecialistNotAvailableError` rather than silently
  returning a hollow stub -- verified directly (no specialist is
  registered yet; none will be until Phases 9-11).
- **The registry boundary is now mechanically enforced, not just a
  convention:** `import-linter` (`pyproject.toml`'s `[tool.importlinter]`
  section) forbids `manager` from importing anything under `specialists`
  at all. Proven to actually catch a violation, not just configured and
  assumed to work: a throwaway `from specialists.base import Specialist`
  line was added to `manager/charter.py`, `lint-imports` correctly
  failed with the exact file/line, then the line was reverted and
  `lint-imports` passed clean again. As of Phase 5, `manager/` doesn't
  import anything from `specialists/` yet (no `delegate_to_specialist`
  exists until Phase 6), so there's deliberately no `ignore_imports`
  allowlist entry yet either -- `import-linter` itself errors on an
  allowlist rule matching zero real imports. **Must re-run
  `lint-imports` after Phases 6, 9, 10, and 11** -- each is exactly the
  point where a "quick" direct import becomes tempting, per the plan's
  own warning.
- **The concrete "add a fourth specialist later" proof, built now
  rather than assumed:** a trivial fake `EchoFakeSpecialist` was
  registered under `SpecialistType.bug_fix` and successfully driven
  through `registry.get(contract.specialist_type).run(contract)` --
  with a *realistic* `TaskContract` built using Phase 4's own
  already-hardened `classify_capability_class()` (real gateway call,
  `strip_think_block()` + completeness check already applied
  internally -- reused, not re-derived, satisfying the "make it three
  call sites" instruction by composition rather than inventing a
  redundant new one) and `check_sensitive_paths()`. Zero changes to
  `manager/charter.py`, `manager/classify.py`, `contracts/schema.py`, or
  `specialists/registry.py` were needed to make this work.

## Status: Phase 6 (Manager's own tools and decision loop)

This is the phase that ties everything together -- components from
Phases 1-5 become one working conversational agent.

- `manager/tools.py` -- the six tools: `delegate_to_specialist`,
  `ask_operator`, `check_sensitive_paths`/`read_project_memory` (re-exported
  from Phases 3/4), `append_project_memory` (**the only code path
  anywhere in the project allowed to INSERT into
  `agent_memory_events`** -- grep-verified across all of
  `manager`/`specialists`/`infra`/`contracts`, not just documented),
  and `await_verification` (never trusts a specialist's own claim --
  always delegates to whichever specialist `contract.validation_by`
  names and builds the result from *that* independent report; honestly
  flagged limitation: the deterministic diff-vs-coverage spot-check that
  should make `spot_check_mismatch` a real third signal is Phase 11's
  job, so it's always `False` here, not because a mismatch was ruled
  out but because the real Testing/QA specialist doesn't exist yet).
  Also `fold_specialist_result()` -- the 5,000-char / first-30%-last-30%
  rule (§2.6).
- `manager/correction.py` -- the correction-detection mechanism, built
  for real: a cheap classifier call, below-threshold results logged as
  a `possible_correction` note, at-or-above-threshold results proposed
  as `event_type='rule'`/`status='proposed'` (**not active**), only
  `confirm_proposed_rule()` (an explicit, narrow, documented exception
  to the insert-only rule -- a status flip on an existing row, not new
  information) flips it active. **A real schema gap found and fixed
  while wiring this**: the `active_agent_rules` view built in Phase 1
  had no concept of a proposed-vs-active status at all -- a just-proposed,
  unconfirmed rule would have been mechanically enforced immediately.
  Fixed with a migration (`scripts/002_active_rules_status_filter.sql`)
  excluding any `rule` row whose `detail->>'status'` is `'proposed'`,
  backward-compatible with rows that have no status field at all.
- `manager/learning.py` -- the error-learning mechanism (§2.7): root-cause
  classification (`one_off`/`skill_gap`/`pattern_worth_a_rule`/`unclear`)
  on every failed verification, routing `skill_gap` to a Code-Review note
  and `pattern_worth_a_rule` into the **exact same** shadow/confirm-first
  pipeline as a Operator-originated correction (just tagged
  `origin=self_detected_pattern`); plus `check_repeated_failures()` --
  2+ prior failed outcomes on the same module trips a plain-language
  pause instead of a silent third attempt.
- `manager/gateway_orchestration.py` + `manager/task_state.py` -- real
  Manager-loop gateway-outage handling, distinct from the client's own
  retry/circuit-breaker (Phase 2): flips Redis task state to
  `paused:gateway_unavailable`, releases the task's module lock, raises
  `GatewayOutagePause` with wording distinct from ambiguity/sign-off
  pauses. **Tested by actually killing the gateway mid-task** (pointed a
  client at a dead, non-routable address) and confirming all three
  things happen, not just that the client gives up cleanly.
- `manager/loop.py` -- `run_turn()`, the plain six-phase async function
  (§2.3's real Nexo shape, no graph library), loading
  `MANAGER_CONSTITUTION.md` and `sensitive_paths.yaml` **in full, every
  turn** (verified: both load substantial real content, not summaries).
  Honest, flagged limitation: there's no real scope-extraction NLP yet
  to turn free text into concrete Odoo model names, so `anticipated_scope`
  is an explicit parameter a caller supplies rather than guessed --
  `check_sensitive_paths()`/`check_repeated_failures()` are both fully
  built and tested against whatever scope they're given; only
  "auto-derive scope from a sentence" is deferred, honestly, to when
  real Odoo model names exist to extract (Phase 7+).
- `scripts/cli_harness.py` -- the bare CLI harness (step 7), registering
  a clearly-labeled `DemoFakeSpecialist` at startup (no real specialist
  exists until Phases 9-11) so the full loop is actually runnable by a
  human right now, not just provable in a test file. Smoke-tested via
  piped stdin, exit code 0.
- **Step 8's explicit requirement** -- re-running Phase 5's
  fake-specialist round-trip through the FULL loop, not in isolation --
  done: both the success path and the failure path (root-cause
  classification + error-learning routing firing correctly) are proven
  end to end through `run_turn()`, with the outcome verified actually
  written to `agent_memory_events`.
- **Extracted a shared helper**, closing an item flagged in the last two
  reports: `infra/structured_output.py` now has `generate_checked()` --
  the `strip_think_block()` + `</think>`-completeness-check pattern,
  previously hand-copied at three separate call sites (Phase 3's
  `compress_session()`, Phase 4's `classify.py`, and this phase's
  `correction.py`/`learning.py`), factored into one place.
  `correction.py`/`learning.py` use it; `classify.py`'s already-tested
  Phase 4 code was deliberately left as its own inline copy to avoid
  re-risking already-passing, signed-off tests for a purely cosmetic
  consistency pass.
- **Import-linter re-armed**, as flagged in the Phase 5 report:
  `manager/tools.py` now has the one real
  `from specialists import registry` import, so
  `pyproject.toml`'s `ignore_imports` allowlist now has exactly
  `"manager.* -> specialists.registry"` -- confirmed `lint-imports`
  still passes (`1 kept, 0 broken`) with this real import in place.

## Status: Phase 7 (Odoo tool layer: data operations)

Real Odoo dev-copy access landed 2026-07-07 (`odoo-dev.int`, container
`odoo16-dev`, port 8071 → `odoo16_dev`) — worked directly against it
rather than a throwaway local install.

**Redone mid-phase, same day, per Andrew's direction**: `mcp-server-odoo`
is dropped entirely. Its Standard mode (the one with a model-whitelist
layer) has no Odoo 16 build and needs an Odoo.com login; its YOLO-mode
fallback drops that whitelist layer entirely. Rather than accept that
reduction or run unfamiliar third-party code against an
already migration-history-uncertain instance, this phase now builds
`OdooToolClient` directly on Python's stdlib `xmlrpc.client` against
Odoo's own standard External API — nothing installed inside Odoo, no
extra dependency at all. `mcp-server-odoo` was uninstalled from the
venv and removed from `requirements.txt`. Everything below describes
the current, rebuilt design; the network/duplication/user/JIT-key
findings from the original build are unchanged and still apply.

- **Network:** port 8071 is bound to `127.0.0.1` on odoo-dev.int itself
  -- unreachable directly from this devbox. Tunneled via
  `ssh -f -N -L 18071:127.0.0.1:8071 andrew@odoo-dev.int` (Andrew's
  explicit approval). `OMA_ODOO_URL` points at the local tunnel end.
  **The tunnel must be running** for anything Odoo-related to work.
- **`infra/odoo_settings.py`** -- the hardcoded, non-negotiable
  Production-instance safety guard Andrew asked for. An **allow-list**
  (not a deny-list): only the canonical `odoo16_dev` or a name matching
  `^odoo16_dev_dup_\d{8}(_\d{6})?$` on port 8071 is accepted; port
  8070 / db `16_202012` (confirmed via `docker ps` to be a genuinely
  different container, `odoo16`) is refused unconditionally, and so is
  anything else unrecognized. `tests/test_odoo_settings_guard.py` (6
  tests) proves this for real, including that an unrecognized target is
  refused even though it isn't the specific known-bad value.
- **Database duplicated via Odoo's own Database Manager function** (not
  pg_dump), per step 2 -- `odoo16_dev_dup_20260707`, confirmed with its
  own filestore directory (`infra/odoo_admin.py`). **A real
  infrastructure surprise, not assumed away**: this container has
  `list_db=False` (database-management functions fully disabled),
  which blocks `duplicate_database` entirely regardless of the master
  password -- confirmed via the container's own log line
  ("Database management functions blocked, admin disabled database
  listing"). With Andrew's explicit approval: temporarily flipped
  `list_db=True` on the host-side config (`/etc/odoo16-dev/odoo.conf`,
  the container's own config directory is a read-only bind-mount),
  restarted the container, ran the duplicate, reverted `list_db=False`,
  restarted again -- confirmed functionally reverted (duplicate calls
  correctly blocked again afterward, not just the config text checked).
- **Dedicated non-admin user** (`oma_agent`, uid 450) created via
  `odoo-bin shell` (real ORM access, not raw SQL) against the
  *duplicate* database, per step 3 -- base Internal User group only at
  first. **Two real, custom business rules on this migrated instance
  required explicit, Andrew-approved scope grants** to make the account
  actually usable, discovered by testing rather than assumed: (1) a
  rule restricting Internal Users to contacts they own or that are
  flagged `display_in_po` unless granted a separate "See all contacts"
  group (granted, after asking); (2) no write access to `res.partner`
  at all without one of several specific groups (granted the narrowest
  fit, "Extra Rights/Contact Creation", after asking). Both grants
  required a container restart to take effect -- the running server's
  permission cache didn't pick up a group change made via a separate
  `odoo-bin shell` process until restarted; confirmed this diagnosis
  directly (0 visible records via ORM shell matched 16,111 before
  restart, matched correctly after).
- **`infra/odoo_jit_apikey.py`** -- the Odoo-side JIT credential
  wrapper (step 4, the other half of Phase 1 step 6's Postgres-side
  mechanism). Create-then-delete: a fresh `res.users.apikeys` record
  per task, deleted at task end -- Odoo has no native key expiry, so
  this wrapper IS the whole mechanism. **A real bug caught by testing,
  not assumed correct**: `_generate(False, name)` stores a literal
  non-NULL scope that `_check_credentials`'s `scope IS NULL OR scope =
  'rpc'` check never matches, so the key silently never authenticates
  -- confirmed via a live login failure, fixed by passing `None`
  instead (matching the method's own docstring, which was easy to
  misread quickly). `tests/test_odoo_jit_apikey.py` proves the full
  lifecycle: the fresh key genuinely authenticates, and the exact same
  key genuinely stops authenticating immediately after revocation.
- **`tools_odoo/odoo_tool_client.py`** -- `OdooToolClient`, wrapping
  stdlib `xmlrpc.client` directly against Odoo's `/xmlrpc/2/object`
  endpoint. Exposes exactly five methods -- `search_read`, `read`,
  `create`, `write`, `unlink` -- **and nothing else**. No generic
  `execute_kw` passthrough, ever; that's a permanent property of the
  class, not a missing feature, closing off the exact class of risk the
  old design's `call_model_method` escape hatch represented by simply
  never building that capability at all.
- **`odoo_tool_allowlist.yaml`** -- the whitelist layer the dropped
  companion module would have provided, done instead as code this
  project owns and can read line-by-line. Same shape and spirit as
  `sensitive_paths.yaml`: a living file, read fresh from disk every
  call (no import-time caching), naming exactly which
  (model, operation) pairs `OdooToolClient` will even attempt.
  Currently: `res.partner` for `read`/`search_read`/`write` only --
  deliberately not `create`/`unlink`, since no real task needs them yet.
  `_check_allowlist()` runs before every single call reaches Odoo.
- **`__last_update` read-and-echo**, required and non-optional on every
  `read()`/`write()` call, unchanged in behavior from the original
  design. **The concurrency test the plan requires**, re-proven against
  the rebuilt client: our client reads a record (captures
  `__last_update`), a second, independent XML-RPC connection (simulating
  a human editing the record in the Odoo web UI at that same moment)
  modifies it, and our client's write -- using the now-stale
  `__last_update` -- is confirmed rejected (`ConcurrencyConflictError`),
  with the record confirmed to still hold the concurrent client's value,
  not silently overwritten.
- **New: the allowlist-rejection tests**, proving the replacement
  whitelist layer actually works, not just exists as a YAML file: a
  call for an unlisted *operation* on an otherwise-allowlisted model
  (`unlink`/`create` on `res.partner`) is refused before reaching Odoo;
  a call for a model **not on the allowlist at all** (`res.users`) is
  refused regardless of operation.
- **Confirmed for Phase 8**: this is a bespoke `odoo:16.0-site` image,
  **not Doodba** -- no Doodba env vars, directory conventions
  (`odoo/custom/src/`), or tooling found; a standard `addons_path`
  pointing at `/opt/site/site16` (custom modules) plus various OCA repos
  under `/opt/site/extra_addons/*`. Plain `click-odoo-contrib` is the
  right tool, as originally planned.

## Status: Phase 8 (module-authoring/deployment toolchain)

Scaffold → lint → install, wrapped as one callable tool interface
(`tools_odoo/module_dev/toolchain.py`, `ModuleDevToolchain`) for the
Build specialist (Phase 9). All three steps run inside `odoo16-dev` via
the same SSH + `docker exec` channel `infra/odoo_jit_apikey.py` already
established, over the existing tunnel.

- **`scaffold_module()`** — runs `odoo-bin scaffold <name> .` inside
  `/mnt/extra-addons`, a writable Docker volume on `odoo16-dev` that is
  deliberately **not** on the container's persistent `addons_path`
  (confirmed via `docker inspect`: it's an RW volume; `/opt/site`, by
  contrast, is a **read-only** bind mount shared with the host image —
  not writable even if we wanted to generate code there). Refuses to
  scaffold over an existing module directory of the same name.
- **`lint_module()`** — installed `pylint-odoo` (and `click-odoo-contrib`)
  via `pip3 install --user`, which lands in `/var/lib/odoo/.local`, a
  *persistent* volume (`odoo16-dev-data`) — survives container restarts,
  confirmed. Runs with `--output-format=json` and returns a structured
  `LintResult` (a list of `LintFinding`s: message-id, symbol, message,
  path, line, column) — never a raw log dump the Build specialist has
  to parse itself. Confirmed it runs sensibly against a fresh scaffold
  (which legitimately fails several checks: missing license, missing
  README, deprecated manifest keys — the point was confirming the tool
  runs and reports structurally, not that an empty scaffold passes).
- **`install_module()`** — first-time install via `odoo-bin -i
  <module> --stop-after-init`, **not** `click-odoo-contrib`'s
  `click-odoo-update`: reading that tool's own source
  (`click_odoo_contrib/update.py`) during this phase showed it only
  ever updates addons already in an `installed`/`to upgrade` state — it
  cannot perform an initial install of a brand-new module. `click-odoo-update`
  remains the right tool for *subsequent* upgrades once a module is
  installed; `odoo-bin -i` is the correct, official tool for the first
  install. Every call re-runs the Production-instance safety guard
  (`_assert_safe_odoo_target`) on the target `db` before anything
  touches SSH — confirmed refused for the forbidden `16_202012` target
  before any command runs.
- **All three steps use an explicit `--addons-path` CLI override**
  (the container's real, existing addons path plus `/mnt/extra-addons`
  appended) rather than ever editing the container's persistent
  `odoo.conf` — the only `odoo.conf` edit this phase needed was the
  same narrow, revert-verified `list_db` toggle from Phase 7, done
  twice more (see blocker below), each time following the identical
  flip → restart → do the work → flip back → restart → verify-the-revert-
  actually-took-effect cycle.
- **A real infrastructure blocker was found, and Phase 9.5 narrowed its
  actual scope:** installing any module that adds a column to an
  existing model (e.g. `res.partner`) fails with
  `psycopg2.errors.InsufficientPrivilege: must be owner of table
  res_partner` on **every duplicate database tested** — confirmed on
  two independent duplicates (`odoo16_dev_dup_20260707`,
  `odoo16_dev_dup2_20260707`) created via Odoo's own Database Manager.
  Originally reported (Phase 8) as possibly instance-wide. **Settled,
  Phase 9.5 (2026-07-07):** installing the same kind of module against
  a genuinely **fresh** database — created via `create_database()`
  (new this pass, `infra/odoo_admin.py`, using Odoo's own
  `create_database` Database Manager call, never a raw
  `createdb`/`pg_dump`) — **succeeded cleanly**, independently confirmed
  both at the Postgres column level (`information_schema.columns`) and
  the Odoo ORM field level (`env['res.partner']._fields`). This is a
  Postgres-level table-ownership issue in **`duplicate_database()`'s
  own copy mechanism specifically**, not systemic to this Postgres
  instance as a whole — still outside this project's access boundary to
  fix directly (zero Postgres access, by design), but now a narrower,
  better-understood problem than originally reported. `install_module()`
  still surfaces the duplicate-specific failure as a structured
  `error_kind="postgres_ownership_blocked"` result rather than a raw
  traceback. **Practical path forward, no fix required to keep
  progressing:** use a fresh database (`create_database()`) for
  module-install testing going forward; duplicates remain the right
  choice for data-only (`OdooToolClient`) testing, which never performs
  a schema `ALTER` and so never hits this issue at all. The one-time
  ownership fix on `duplicate_database()`'s own output remains a real,
  worthwhile fix for whoever administers Postgres behind
  `odoo-dev.int`, but is no longer a hard blocker to install-testing
  overall.
- Tests (`tests/test_module_dev_toolchain.py`, 6 functions, all against
  the real container): scaffold produces a real skeleton; scaffold
  refuses to overwrite an existing module; lint returns structured
  findings on a fresh scaffold; install surfaces the real ownership
  blocker as a structured result (using the actual known-bad case as
  the test, not a synthetic one); install refuses the forbidden
  Production target before any SSH command runs; `ModuleDevToolchain`
  exposes all three steps as one interface. All 6 pass.

## Status: Phase 9 (the Build specialist)

`specialists/build/specialist.py` — `BuildSpecialist`, registered for
`SpecialistType.bug_fix` in `scripts/cli_harness.py` (real, replacing
the demo fake for this one role; Testing/QA still uses the demo fake
until Phase 11). Branches on `capability_class`:

- **`module_dev`** — the real, tested path: decide module name (derived
  from the goal), scaffold via Phase 8's toolchain, generate real
  manifest/model code with `qwen3-coder-30b-a3b` via `call_structured()`
  (a `GeneratedModuleFiles` schema: manifest, models, optional views, and
  an honest `notes` self-report), write the files into the scaffolded
  module (`write_module_file()`, new this phase, base64-transported over
  SSH to avoid any shell-quoting corruption of real source code), lint,
  `check_fence()` immediately before install (mandatory, no exceptions),
  then install.
- **`data_change`** — deliberately minimal this phase: routes toward
  `OdooToolClient` directly per the `odoo-xmlrpc-operations` skill, never
  touching module-dev tools at all (giving deployment tools to a
  data-only task would hand out more capability than its own
  classification says it needs). Not exercised end-to-end this phase —
  no concrete `data_change` task was part of Phase 9's required test
  (task 1 is `module_dev`); real scope-extraction is still the same
  documented Phase 6 gap.
- **`readonly_investigation`** — explicitly refused; that's Code-Review's
  job (Phase 10), not Build's.

**Resolved, same day:** the Odoo Development Agent Constitution
(`/home/andrew/projects/scratch/odoo/# Odoo Development Agent
Constitution.md`, per the build plan's §0.5.5) was missing when this
phase was first built — confirmed via a direct filesystem check, not
assumed — and `specialists/constitution.py` raised a clear, loud
`ConstitutionNotFoundError` rather than fabricating placeholder content
for Operator's own governing document. Andrew has since created the real
file at that exact path. Confirmed it loads correctly
(`load_odoo_development_constitution()` returns real, substantial
content, not empty/placeholder) and re-ran every Constitution-dependent
test in `tests/test_build_specialist.py` against it for real — all
still pass. The test-only stand-in files each test created when the
Constitution was missing have been removed from the test code entirely
(no longer needed now that the real file is a permanent fixture).

**Four skill files now exist** (`skills/odoo-safe-financial-queries`,
`odoo-xmlrpc-operations`, `odoo-module-scaffolding`,
`odoo-codebase-audit`), each following §0.5.6's required shape
(frontmatter with `name`/`version`/`description`, `## When to use
this`, domain-specific rules, `## How`, empty `## Revision Log`).
`odoo-safe-financial-queries` is the plan's own verbatim example;
`odoo-codebase-audit` is Code-Review's own skill (Phase 10's job to
actually use) but written now per the plan's explicit "once all four
exist" instruction. A deliberate overlap-check pass: they split cleanly
by capability class (data vs. module-dev) and by role (build vs.
review), with `odoo-safe-financial-queries` scoped narrowly to
financial-data investigation rather than overlapping either
mechanism-focused skill.

**Fencing (`check_fence()`) is now wired in for real, not just tested
synthetically** — mandatory, immediately before every install.
Confirmed with a real end-to-end test: a stale caller's fence token
(standing in for a specialist whose lock silently expired) is
genuinely rejected once a **real `BuildSpecialist.run()`** has since
reacquired the same module's lock fresh and moved forward — the actual
specialist code path, not a second synthetic caller.

**`manager/compensations.py`** — `run_compensations()` walks backward
through whichever `CompensatingAction`s a specialist actually recorded
before a cutoff, executing each `undo_action` for real via a small,
explicit `"<kind>:<args>"` convention (`remove_scaffolded_module`,
`uninstall_module` — more kinds get added only as later tasks actually
need them). `contracts/exceptions.py` holds `PartialTaskFailure`
specifically so a specialist can raise it and `manager/tools.py` can
catch it **without** `manager/` ever importing a specialist module
directly (only `specialists.registry` is allowed — the import-linter
contract stays `1 kept, 0 broken`). `delegate_to_specialist()` catches
it, runs compensations, and raises `TaskCutOffPause` (worded distinctly
from a gateway-outage or ambiguity pause, same pattern as Phase 6);
`manager/loop.py`'s Phase 4 now handles this alongside
`GatewayOutagePause`. Tested with a forced mid-task cutoff (a test-only
subclass that raises immediately after the real scaffold step
succeeds, standing in for `turn_budget` exhaustion): confirmed the
scaffolded module directory is **actually** gone from
`/mnt/extra-addons` afterward — checked independently via SSH, not just
a claimed cleanup.

**Task 1 (the field add) run end to end, twice, against two different
databases** — both through `BuildSpecialist.run()`'s own real code
path, never the generic toolchain checked in isolation:

- Against the real **duplicate** database: real scaffold, real
  LLM-generated manifest/model code, real lint all confirmed. Install
  correctly surfaces the duplicate-specific Postgres-ownership block
  (Phase 8, narrowed Phase 9.5) as an honest, structured self-report
  (`claims_complete=False`) — proving the specialist doesn't crash or
  falsely claim success.
- Against a **fresh** database (Phase 9.5's `create_database()`): a
  genuinely complete, successful run — `claims_complete=True`, no
  `error_kind` — with the field's actual existence confirmed
  **independently at the Odoo ORM level** (`env['res.partner']._fields`),
  not just trusted from the specialist's own report. This is the real
  proof the build plan's Phase 9 step 6 asks for; the earlier
  fresh-database result (Phase 9.5) had only checked the generic
  scaffold/lint/install mechanism directly, not the specialist itself.
- **A real bug was caught and fixed getting here**: the LLM's generated
  manifest referenced a view file (`views/res_partner_views.xml`) that
  didn't match the literal path `write_module_file()` actually writes
  generated view content to (`views/views.xml`) — Odoo's install
  crashed with `FileNotFoundError` trying to load a file that was never
  created. Fixed two ways: the generation prompt now states the exact,
  non-negotiable path requirement, and `_validate_manifest_view_references()`
  catches any mismatch structurally, before anything is written, rather
  than letting Odoo's own install process crash on it.

Tasks 2 and 3 (the harder, relational-model tasks) are explicitly
deferred, per the plan's own step 7 ("only once task 1 is genuinely
working... progressively harder").

Tests (`tests/test_build_specialist.py`, all against the real container
and real model gateway): fencing rejects a stale caller after a real
specialist reacquires; compensations actually clean up a forced
cutoff; task 1 runs end to end against a duplicate (documenting the
known limitation) and against a fresh database (the actual, complete
success case, field independently verified). All pass.

## Status: Phase 10 (the Code-Review specialist)

`specialists/code_review/specialist.py` — `CodeReviewSpecialist`, runs
on `qwen3.6-27b` (the Manager's own model tier — judgment, not code
generation), using the shared `generate_checked()` completeness
discipline (this model always produces a long hidden `<think>` trace
and ignores `/no_think` entirely, per §0.5.2's own finding). Two
distinct `TaskContract` shapes route through the same `run()`, via a
small, explicit `inputs` convention (no new schema field needed):
`"diff_module:<name>"` (review a Build specialist's real, already-
written module) and `"full_codebase_audit:<relative_path>"` (task 4's
read-only audit of the real custom Odoo codebase under
`/opt/site/site16`).

- **`tools_odoo/codebase_read.py`** (new) — strictly read-only file
  access (`read_module_files()`, `read_codebase_tree()`), plain
  `find`/`cat` over SSH. Task 4's read-only property is enforced
  **structurally**: neither this module nor `code_review/specialist.py`
  imports anything write-capable at all (`OdooToolClient`,
  `ModuleDevToolchain`, `write_module_file`, `install_module`,
  `scaffold_module`) — confirmed by a real test that parses both files'
  own `import` statements via `ast` and checks for these bindings, not
  a promise the specialist keeps.
- **Structured findings**: each a `location`, `severity`
  (`blocking`/`major`/`minor`/`info`), and a one-line `explanation` —
  never a prose paragraph to parse. `claims_complete` has two honest,
  documented meanings depending on mode: for a diff review, "this
  change is approved" (no blocking findings); for a full audit, "the
  audit itself completed" (there's no pass/fail concept for an audit).
- **Skill reused, not duplicated**: Phase 9's `odoo-codebase-audit`
  skill already covers exactly what the build plan's Phase 10 step 1
  calls "the `review-against-constitution` skill" (load the actual
  Constitution in full, judge against the same four questions) — a
  second, near-identical skill file would have violated the very
  overlap-check principle Phase 9 asked for. Confirmed no meaningful
  overlap across all four skill files (split cleanly by capability
  class and by role — build vs. review).
- Tested against a **deliberately introduced bad module** (a hardcoded
  API credential, an overly broad `sudo()` bulk-delete): both flagged
  as `blocking` findings, `claims_complete=False`, plus two genuine
  additional findings the model caught on its own (unjustified core-model
  inheritance, a stale `ir.model.access.csv` reference) — confirmed
  before trusting this specialist against any real Build output or
  task 4's real codebase, per the build plan's own step 5.
- Tested against a **real, existing custom module**
  (`garazd_product_label`, task 4's actual shape) — a genuinely
  substantive, real audit (11 real files read, 7 real findings citing
  specific lines and patterns: unguarded `sudo()` use, deprecated
  `self.update()`, undeclared custom fields, invalid CSS in a QWeb
  template), `claims_complete=True`.

**Two real bugs found and fixed this phase, not routed around:**
1. The initial per-call timeout (90s default) wasn't enough for this
   specialist's unusually large prompt (full Constitution + full skill
   text + real file contents) — confirmed via a real `GatewayUnavailableError`
   after 3 timed-out retries. Fixed with an explicit, generous
   `timeout_sec=240` for this specialist's calls specifically.
2. **A genuine, reproducible model-output bug**: on a longer,
   multi-finding response, `qwen3.6-27b` occasionally emitted JSON with
   a syntax error (a missing comma between array elements). The
   specialist's original one-shot `generate_checked()` + `json.loads()`
   had no way to recover from this — reproduced twice in a row before
   being root-caused. Fixed by building a proper retry-with-feedback
   loop combining **both** disciplines that neither existing helper
   provided alone: `generate_checked()`'s `</think>`-completeness check,
   and `call_structured()`'s validate-and-reask pattern (the parse
   error is fed back to the model, up to `contract.retry_sub_budget`
   attempts) — confirmed fixed by two clean full test-suite runs in a
   row afterward.

Tests (`tests/test_code_review_specialist.py`, 5 functions, all against
the real container, real custom codebase, and real model gateway): the
deliberately-bad-module test, the real-module full-audit test, a wrong-
capability-class refusal, a no-review-target refusal, and the
structural read-only-import proof. All pass.

## Status: Phase 11 (the Testing/QA specialist)

`specialists/testing_qa/specialist.py` — `TestingQASpecialist`, the
independent verification specialist that actually closes the loop on
tasks 1-3: a task is never marked done just because the Build
specialist says so. Model cascade per §0.5.2: defaults to `qwen3-14b`
for routine reproduction/self-reporting, escalates to
`deepseek-r1-distill-qwen-32b` (raw `client.generate()` +
`strip_think_block()`, **never** `generate_checked()` — that
completeness check is tuned for `qwen3-14b`/`qwen3.6-27b`'s own
behavior and is meaningless for this model, per
`infra/structured_output.py`'s own docstring) only when a reproduction
check has already failed and the reason isn't obvious from output
alone.

- **`tools_odoo/spot_check.py`** (new) — the deterministic, non-LLM
  spot-check the build plan explicitly asks for: "a separate,
  non-LLM piece of code this specialist's output gets run through
  automatically, not something the specialist does itself."
  - `check_field_exists_on_model()` — a real ORM-level proof (via
    `odoo-bin shell`, full `--addons-path`), never an XML-RPC read
    alone (which can't distinguish "doesn't exist" from "exists but
    empty").
  - `run_coverage_and_diff()` — installs a module under Python's real
    `coverage` tool (one-time `pip3 install --user coverage`, same
    persistent-volume pattern as `pylint-odoo`/`click-odoo-contrib`)
    and reports genuine per-line coverage. **Confirmed working via a
    real experiment during this phase**: a method body never actually
    called during install shows up as genuinely uncovered lines
    (`models/models.py:10-13`, exactly the untested `if`/`else`
    branches) — a real, non-fabricated signal.
  - `compute_spot_check_mismatch()` — the actual diff-vs-claim
    comparison: true only when reality found a real gap the self-report
    didn't already own up to.
- **`manager/tools.py`'s `await_verification()` — the Phase 6 flagged
  limitation is now genuinely fixed**, not just closed on paper:
  `reproduction_confirmed` and `spot_check_mismatch` are read directly
  from the real `TestingQASpecialist`'s own `detail` dict (falling back
  to deriving them from `claims_complete` for any older/other validator
  that doesn't populate these keys, so this never breaks a validator
  that hasn't been updated). Confirmed with a dedicated end-to-end test
  using a genuinely correct module — `reproduction_confirmed=True`,
  `spot_check_mismatch=False`, `passed=True`, no hardcoding anywhere in
  the path.
- **A 5th skill file**, `odoo-verification-and-reproduction` — a
  deliberate, explained departure from Phase 9's "four total" framing:
  Phase 9's own enumeration only covered skills relevant to the
  specialists that existed as of Phase 9's writing (Build, Code-Review);
  Phase 11's own step 1 explicitly asks for "this specialist's own
  system prompt and skill," and none of the existing four cover
  verification/reproduction methodology at all. Judged safe against the
  overlap-check principle since this skill's domain (independent
  verification) is genuinely distinct from build, data-ops, or review.
- Registered for real in `scripts/cli_harness.py` (`testing_qa`,
  conditional on `OMA_ODOO_DB_DUPLICATE_FOR_BUILD`, same pattern as
  Phase 9's `bug_fix` registration).

**Both required tests from the build plan's own step 3, run for real:**

- **A deliberately broken fix** (a module claiming to add
  `should_not_exist_field`, but actually defining a different field
  entirely — install itself succeeds cleanly, so "it installed without
  error" would be the wrong signal to trust): correctly caught —
  `reproduction_confirmed=False`, `claims_complete=False`.
- **A deliberately incomplete self-report** (a real, un-called method
  body with actual branching logic; the specialist's own self-report
  step force-overridden, via a test-only subclass, to falsely claim
  full coverage): the deterministic spot-check catches the discrepancy
  on its own — `spot_check_mismatch=True`, `claims_complete=False` —
  regardless of what the self-report claimed.

Tests (`tests/test_testing_qa_specialist.py`, 3 functions, all against
the real container, a real fresh database, and the real model gateway):
the two required failure-mode tests above, plus the `await_verification()`
end-to-end wiring proof. All pass.

## Status: Phase 12 (the chat UI) + a loose end closed from Phase 10

Before starting the UI itself, closed a real loose end flagged after
Phase 10/11: `TaskContract.validation_by` was still hardcoded to
`"testing_qa"` on every task, and `specialist_type` was hardcoded to
`bug_fix` — meaning a `capability_class=readonly` task (task 4's audit
shape) would have been delegated to `BuildSpecialist`, which explicitly
refuses that capability class, and then "verified" by `TestingQASpecialist`,
which has nothing meaningful to check against a pure report. Fixed with
`manager/loop.py`'s new `select_specialist_and_validator()`: `readonly`
routes to `CodeReviewSpecialist` with `validation_by=None` (no second
specialist independently verifies a pure audit — Code-Review's own
`claims_complete` is the terminal signal for that shape); every other
capability class is unchanged (`bug_fix` + `testing_qa`).
`TaskContract.validation_by` is now `str | None`, a deliberate, narrow
widening — `None` means "this specialist's own result is authoritative
for this shape," never a stand-in for "verify against myself."

**A second, previously-masked bug surfaced and fixed while building
this**: `run_turn()` computed a failure's `root_cause` locally but
never wrote it back onto the `VerificationResult` it returned — this
only ever worked before because `await_verification()` did that
assignment internally on its own returned object. Exposed by the new
`readonly` pass-through path (which has no such internal assignment),
fixed generally for both paths.

**A third, real gap found and fixed while building Phase 12's own
plan/act UI pattern**: `PAUSE_SIGN_OFF_REQUIRED` existed as a task-state
constant since Phase 6 but was **never actually wired into the loop** —
a tier-3/4 task would proceed straight through delegation without ever
pausing for Operator's sign-off, directly contradicting
`MANAGER_CONSTITUTION.md`'s own tier definitions. Fixed:
`manager/sign_off.py` (new) stores a pending contract in Redis (not
just held on the async call stack — it must survive a real chat UI's
separate, later approve/reject request); `run_turn()` now pauses before
delegating for tier ≥ 3, returning the contract itself for Operator to
review (the plan/act pattern: show the plan before any action, not just
the result after); `resume_after_sign_off()` runs the exact same
back-half execution path (`_execute_contract()`, extracted so there's
no second, drifting copy of delegate→verify→memory-write) once
approved, or writes a real `"decision"`/`"rejected"` memory row and
cancels cleanly if not.

**The actual chat UI** (`ui/chat/server.py` + `ui/chat/index.html`) —
a minimal FastAPI backend and a single vanilla-JS page, per the build
plan's own "simplest possible... nothing fancier yet" instruction:

- `POST /api/message` — runs the real Manager loop, returns the reply
  and full result.
- `GET /api/pending` — the one queue the build plan asks for: every
  tier-3/4 sign-off (from Redis) and every proposed-rule confirmation
  (a real Postgres read, `manager.correction.list_pending_proposed_rules()`,
  new this phase) together, each rendered as a confidence-card (action,
  reasoning, confidence together, per the build plan's third UX
  pattern) — not a wall of text.
- `POST /api/sign_off/{task_id}` and `POST /api/proposed_rule/{row_id}`
  — the approve/reject actions, wired to the real
  `resume_after_sign_off()`/`confirm_proposed_rule()`/`reject_proposed_rule()`
  functions, no new logic duplicated in the UI layer.
- Specialist registration is shared between the CLI harness and the
  chat server via **`scripts/bootstrap_specialists.py`** (new) —
  deliberately kept under `scripts/`, not `manager/`, since the
  import-linter contract forbids anything under `manager/` from
  importing a specialist module directly; `code_review` is always
  registered for real (it needs no target database), `bug_fix`/`testing_qa`
  fall back to a demo fake only if `OMA_ODOO_DB_DUPLICATE_FOR_BUILD`
  isn't set.

Tested for real: started the server and drove the full sign-off
round-trip over real HTTP with `curl` first (propose → appears in the
pending queue → approve → actually executes → clears), then wrote it
as a repeatable automated test using starlette's real `TestClient` (a
genuine ASGI call through the actual app, not a mock of the HTTP
layer) against the real Manager loop, real Postgres, real Redis, and
the real model gateway.

Per the plan's own step 4, held off on anything more elaborate (diff
rendering, a full dashboard) — this is deliberately minimal, proven
against the real loop first.

## Status: Phase 13 (all four of Operator's tasks, end to end)

All four tasks run for real, through the actual Manager loop / real
specialists / real Odoo application layer — no mocks anywhere. Per the
plan's own sequencing (lowest-risk first):

- **Task 4 (audit)** — 168s. Routed to `CodeReviewSpecialist` via the
  real loop; audited `sale_room_management` (a real, different custom
  module than Phase 10's test target). Found genuine, substantive
  issues: an `eval()` RCE vulnerability, a raw-SQL ORM bypass, hardcoded
  developer-machine paths, and a real bug (a security rule referencing
  a nonexistent model). Read-only enforcement is structural (Phase 10's
  own `ast`-parsed import check), not re-verified per audit target.
- **Task 1 (field add)** — 94.5s, against a fresh database
  (`create_database()`). Full chain: Build → Testing/QA, genuinely
  chained via a real plumbing fix (see below). Field independently
  confirmed at the ORM level.
- **Task 2 (service module)** — Build succeeded (a real new
  `oma.service` model with `project_id`/`partner_id`/`employee_id`).
  Code-Review, genuinely engaged (not skipped): caught a real,
  independently-confirmed bug — `security/ir.model.access.csv`
  referenced the wrong model, leaving `oma.service` with **zero** real
  access rules (confirmed via a live query). This exact pattern
  recurred on task 3 — see the error-learning section below.
- **Task 3 (advanced service module)** — 198s first attempt,
  **failed exactly as the plan predicted**: a real `views.xml` bug (a
  `<menuitem>` forward-referencing an `<record>` action defined later
  in the same file — a classic, real Odoo XML-loading mistake). Fixed
  as a genuine second pass; the same `ir.model.access.csv` bug recurred
  here too. After both fixes, the **required cross-customer negative
  test** ran for real: two customers, two projects, two products each
  scoped to one project via a new `project_ids` field, two service
  records — confirmed **zero cross-visibility** (`visible_to_a` showed
  only Product A, `visible_to_b` showed only Product B).

**A real plumbing gap found and fixed**: `TestingQASpecialist` needs a
`verify_module:<name>:<db>` entry in `contract.inputs`, but nothing
populated it automatically — `contract.inputs` and `db` only become
known once the Build specialist actually runs. Fixed in
`manager/tools.py`'s `await_verification()`: it now enriches a *copy*
of the contract passed to the validator, using the real module
name/db from `build_output.detail` (the Build specialist now also
reports its own target `db`). The original contract is never mutated.

**The error-learning mechanism, demonstrated with a real, recurring
failure — not a manufactured one**: the same `ir.model.access.csv`
bug hit independently on tasks 2 and 3. Classified for real:
`root_cause="skill_gap"` (not `"unclear"` — the richer, accurate
failure description made a real classification possible).
`handle_failed_verification()` correctly routed it to
`skill_revision` (a real memory row). **The actual skill gap was then
fixed**: `BuildSpecialist`'s `GeneratedModuleFiles` schema gained a
required `security_csv` field (previously nonexistent — the specialist
never touched `ir.model.access.csv` at all, leaving `odoo-bin
scaffold`'s own stale placeholder in place), plus a structural
pre-write validation (`_validate_security_csv_covers_new_models()`)
catching any future mismatch before anything is written. **A third,
real task afterward confirmed the fix**: a new `oma.equipment.log`
model got a correct, matching access row — confirmed via a live query
(`access_rules_found=1`, versus `0` both prior times).

**A second, related nuance found and fixed the same day**: an earlier
version of the fix for the `verify_module` plumbing gap (reading a
module's real generated code to resolve ambiguous field names) had a
side effect — it let the reproduction check report whatever field
*actually* exists rather than checking the field the task *claims*
exists, which trivially "passes" a deliberately-broken-fix test and
defeats the entire point of independent verification. Fixed by making
the priority explicit: a literally-named field in the goal/deliverables
is the claim being checked (even if it doesn't match the code); the
real code is consulted only to resolve a field the goal describes
relationally, without ever naming it. Confirmed both directions work
correctly afterward.

**The tier-3/4 sign-off gate (Phase 12) did not fire on any of these
four tasks** — correctly: none of Operator's four real tasks touch a
`sensitive_paths.yaml` entry or schema/permissions, so tier 1/2 is the
mechanically correct classification. This isn't a gap: the gate's own
mechanism is already proven firing and behaving correctly by Phase 12's
dedicated tests (`tests/test_manager_sign_off.py`); these four tasks
simply don't happen to be tier-3/4 shaped.

**On the Postgres boundary**: everything in this phase was tested fully
through the Odoo application layer — the API, `odoo-bin` installs
against a fresh database, real ORM queries, real concurrency behavior
— exactly as rigorously as every phase before it. Nothing was skipped
or under-tested because of the Postgres boundary; the only thing that
boundary has ever excluded (since Phase 7) is direct table-level
access to the Postgres instance backing this Odoo deployment, which
nothing in this phase needed. The one genuinely Postgres-shaped item —
`duplicate_database()`'s own table-ownership quirk (Phase 8/9.5) —
remains open, unrelated to anything tested here (this phase used fresh
databases throughout, which don't have that issue), and will simply
work once the sandbox's own, unshared Postgres access is available —
that's a reconnect, not a rebuild, for every mechanism proven here.

Tests: all four tasks driven through real `run_turn()`/specialist
calls (not a dedicated pytest file — this phase's "test" is the real
task execution itself, independently verified at each step via live
ORM/SQL queries, per the build plan's own instruction to record real,
honest verification results). Full regression re-run afterward:
`import-linter` clean, all prior test files pass.

## Status: Phase 16 (live trace bus, real-time UI, cancel, replay, and the cross-task dashboard)

Built on top of Phase 15, on this same DevBox. Full report:
`docs/reports/ODOO_MANAGER_AGENT_PHASE16_2026-07-07.md`.

**The trace bus** (`manager/trace.py`, §20.3): `publish_trace_event()`
PUBLISHes to `oma:trace:{task_id}` on the same Redis instance/DB
already provisioned — fire-and-forget, wrapped so a publish failure
never breaks the Manager loop (`agent_memory_events` stays the durable
record; this is purely ephemeral live visibility). Wired into all six
of the Manager's phase transitions, every round boundary, and each
specialist's own real sub-steps (Build: scaffold/generate/lint/install;
Code-Review: each finding as produced; Testing/QA: reproduction-check
then spot-check).

**`GET /api/stream/{task_id}`** (§20.4): a real SSE endpoint
subscribing to that channel, closing cleanly on a real terminal event
or an idle backstop. Degrades honestly (one real failure event, not a
crash) if the subscribe itself fails.

**A real, live gap found and reported, then closed and confirmed live**:
the `odoo-manager-agent` Redis ACL user initially had no
`PUBLISH`/`SUBSCRIBE` grant on `oma:*` channels — confirmed directly
against the real instance, flagged to Andrew, and every trace-publishing
call site was built to degrade gracefully in the meantime. Andrew
granted the ACL; a full live-confirmation pass followed (see
`docs/reports/ODOO_MANAGER_AGENT_PHASE16_LIVE_CONFIRMATION_2026-07-07.md`)
— real delivery proven at the Redis level, the HTTP/SSE level, and in
the actual browser watching a real, unforced multi-round task render
live. That pass found and fixed two further real bugs, both consequences
of Redis Pub/Sub's own lack of message history meeting a client that
joins mid-task: specialist events for a round whose own start event was
missed were silently dropped (fixed with round self-healing), and
round-less specialist-internal events could misattach to a stale,
already-failed round (fixed to only attach to a genuinely still-running
round). Both found by actually watching the real browser, not by
inspection.

**Real cancel** (`manager/task_state.py`'s `request_cancel()`/
`is_cancel_requested()`, §20.6): a genuine cooperative check at the top
of every round in `manager/loop.py`'s round loop — never a hard kill
mid-write. `POST /api/tasks/{task_id}/cancel`. A real, engineered
mid-flight cancel test (`tests/test_cancel.py`) confirms it stops at
the next safe boundary, not immediately and not never, with a real
`cancelled_by_operator` memory row written.

**The cross-task dashboard** (`manager/dashboard.py`,
`GET /api/tasks`): every recent task — standalone and outer-plan items
alike — grouped into Needs-attention/In-progress/Completed, a real
query over `agent_memory_events` plus live Redis pending state. Closed
a real, necessary gap along the way: standalone tasks had nowhere
durable storing their own original goal text (only outer-plan items
did) — a new `task_created` event (`scripts/004_task_created_event.sql`)
closes this, and its own `inputs` field turned out to matter for
**replay** too (`POST /api/tasks/{task_id}/replay` — resubmits the
real original goal *and* its real `contract_inputs` as a brand-new
task; a real replay test caught this the hard way when the first
attempt at replay silently dropped `contract_inputs` and the replayed
task failed for a reason the original never had).

**Real diff computation** (`specialists/build/specialist.py`,
`difflib`): empty-vs-new for a brand-new file, real prior content for
a genuine same-task retry — never odoo-bin's own scaffold placeholder
leaking in as fake "old" content. Findings/checks serialization
(`manager/ui_serialize.py`) maps Code-Review's and Testing/QA's
already-real structured output into the UI's shape, no redesign.

**The frontend** (`ui/chat/index.html`, fully rewritten, still plain
JS/DOM, no framework, no build step): a faithful port of the approved
design's CSS custom properties, layout, animation keyframes, and
rail/nesting rendering logic — wired to real endpoints throughout,
zero mock/scripted data shipped. Verified with real, automated browser
testing (Playwright + real Chromium, installed for this purpose): real
chat messages, a real historical escalation with its real 4-round
trace, a real round-1 success (honestly showing limited detail given
the live-stream ACL gap — not fabricated), zero console errors across
every real interaction tested.

## Status: Phase 15 (reflect-and-retry, the outer task-list layer, and Code-Review genuinely wired in)

Built on the **same DevBox, against the real Odoo dev-copy**, per
Andrew's explicit sequencing decision: Phase 14 (sandbox transfer) is
deliberately held back until this phase is fully done and Andrew has
run his own end-to-end check, since some of what Phase 14 needs is a
manual, one-time sandbox action not worth doing twice.

**The actual gap this phase closes**: through Phase 13, a failed task
was reported to Operator on its very first attempt — real, valuable error-
learning (proposed rules, skill-revision notes) for *next* time, but
nothing for *this* task, failing once, right now, for a reason that
might genuinely be fixable on a second attempt. Two related, flagged
Open Items closed in the same pass: Code-Review (fully built since
Phase 10) was never actually consulted mid-loop for a real `module_dev`
task; `ask_operator()` existed as one of the Manager's six tools with no
real call site.

**The inner loop — bounded reflect, revise, retry** (`manager/loop.py`'s
`_execute_contract()`, `manager/replanning.py`): a failed round revises
the contract from the SPECIFIC evidence in its own `VerificationResult`
(root_cause, uncovered_paths, notes) and any Code-Review finding —
deliberately mechanical/templated, not a fresh LLM re-plan each round,
avoiding AgentOrchestra's own published failure mode. Bounded by
`planning_round_budget` (default 5) and `round_wall_clock_cap_seconds`
(default 45 minutes) — both deliberately generous, per this phase's own
stated philosophy: **this system optimizes for a correct result over a
fast one, on purpose.** A genuinely unfixable task still stops:
`should_escalate_to_operator()` raises `PauseForOperator`, which calls the real
`ask_operator()` and stores the escalation (Redis, `manager/escalations.py`)
so it's visible in the chat UI's pending queue later, not just in the
one reply that triggered it.

**Code-Review genuinely engaged mid-loop for `module_dev` tasks**
(`manager/tools.py`'s new `run_code_review_diff()`, wired into
`await_verification()`): any `blocking` finding now fails the round —
even if Testing/QA's own reproduction independently passed. This is
exactly Phase 13's own real finding (Testing/QA's field-existence check
had no way to catch the `ir.model.access.csv` bug Code-Review caught)
now closed as a structural, automatic part of every `module_dev` task's
closing flow, not something that has to be invoked by hand.

**The outer plan — a real, visible task list** (`manager/task_plan.py`,
`scripts/003_task_plan_view.sql`'s `active_task_plan_items` view): for
a request that genuinely decomposes into dependent pieces (task 2/3's
shape). `create_task_plan()`/`mark_plan_item_status()` are the only
functions anywhere allowed to write a `plan_created`/`plan_item_status`
event — grep-verified. `next_runnable_item()` reconstructs a real
`TaskContract` from persisted state alone (the full contract travels
forward in each status row's own `detail`), so it needs nothing held
externally in memory. `run_plan()` (new, `manager/loop.py`) drives an
outer plan's items through the same inner round loop each. A
single-item request skips all of this entirely and behaves exactly as
it always has — `plan_id`/`plan_item_id`/`blocked_by` all default to
nothing.

**A real, honest gap closed, not glossed over**: `check_repeated_failures()`
is scoped per-module-name — Phase 13 found a real bug recurring
identically across two *different* modules, which that function alone
would never catch. `check_repeated_failures_across_modules()` (new,
`manager/learning.py`) is the companion signal, matching on a caller-
supplied failure signature rather than a module string. Reproduced
Phase 13's exact real scenario directly in a test: the per-module check
genuinely misses it, the new cross-module check genuinely catches it.

**A real, deliberate behavior change, not a regression** (flagged
plainly, not silently absorbed): a task that used to come back as
`{"status": "completed", "passed": False}` on its very first failure
now retries first. Existing tests written against the old one-shot-
failure shape needed updating to expect this — exactly the intended
point of this phase.

**UI additions** (`ui/chat/server.py`, `ui/chat/index.html`): `GET
/api/plan/{plan_id}` (a literal pending/in-progress/passed/failed/
blocked checklist), `GET /api/rounds/{task_id}` (the round trace, in
order), and the pending-items queue gains a third card type — a
`PauseForOperator` escalation, same confidence-card shape, with a
collapsed-by-default round trace so Operator can see exactly what was
already tried before being asked.

**`MANAGER_CONSTITUTION.md`** gained one new paragraph, per this
phase's own required constraint: the mechanical gates
(`check_sensitive_paths()`, the autonomy tiers, the tier-3/4 sign-off
pause) are completely untouched by this entire phase. Replanning
changes *how* the Manager pursues an already-approved goal; it never
loosens *whether* a task needed sign-off in the first place.

All 7 tests §19.9 specifies were written and pass, against real
Postgres/Redis and (where a real LLM call is genuinely involved) the
real model gateway — `tests/test_replanning.py` and
`tests/test_task_plan.py`. Full regression re-run afterward across all
prior test files: clean, with one real, necessary test update
(`test_chat_ui_server.py`'s readonly-routing test needed a real audit
target supplied, or the new round loop retries the "nothing to review"
refusal 5 times before escalating — a real behavior difference, not a
mistake to route around).

## Open items

- **Found during Phase 15's real-task verification pass (real
  specialists, real Odoo, no forced failures), all now fixed and
  re-verified — see
  `docs/reports/ODOO_MANAGER_AGENT_PHASE15_REAL_VERIFICATION_2026-07-07.md`
  and `docs/reports/ODOO_MANAGER_AGENT_PHASE15_STRUCTURAL_FIXES_2026-07-07.md`:**
  - A real `ScaffoldError` crash (round 2 of any module_dev retry
    colliding with round 1's own, never-cleaned-up scaffold) — fixed
    (`specialists/build/specialist.py` reuses an existing scaffold on a
    genuine same-task retry, same `task_id`).
  - **Fixed, structurally, not just made less likely:**
    `slugify_module_name()` now suffixes every module name with a short
    hash of the task's own `task_id` — two different tasks (or two
    outer-plan items) can never derive the same module name again,
    regardless of how similar their goal text is. A genuine retry of
    the *same* task (same `task_id` across rounds) still derives the
    identical name, so same-task retry-reuse is unaffected.
  - **Fixed:** a blocked_by-dependent outer-plan item now extends its
    blocking item's real module the way Odoo is actually meant to be
    extended — a new, separate module with `depends=[..., "<name>"]`
    and `_inherit = "<model>"` — instead of ever regenerating another
    item's files. `manager/loop.py`'s `run_plan()` looks up the
    blocking item's real, recorded `module_name` and injects
    `depends_on_module:<name>` into the dependent item's `inputs`
    (same `<marker>:<value>` convention as `diff_module:`/`verify_module:`);
    `BuildSpecialist` reads the target module's real source and
    generates a genuine extension, structurally validated before write
    (`_validate_manifest_declares_dependency()`,
    `_validate_module_name_not_reused()` as a second, independent
    backstop). Re-verified live: two plan items with intentionally
    similar goal text got distinct module names, item 2's manifest
    genuinely declared a dependency on item 1's module, and item 1's
    own fields (`project_id`, `partner_id`, `description`) were
    confirmed still present at the ORM level after item 2 ran.
  - **Fixed:** `select_specialist_for_retry()`'s "same finding recurs"
    check now uses `classify_findings_same_theme()` — the same
    two-stage cascade shape as `classify_capability_class()` (cheap
    keyword-overlap fast path, escalating to a cheap `qwen3-14b`
    classification call only when genuinely ambiguous) — instead of
    exact string equality, which real Code-Review output's own
    round-to-round rephrasing never actually matched. Re-verified with
    REPHRASED (not identical) findings describing the same real
    underlying problem — correctly triggers the switch to `code_review`.
- **Minor, non-security cleanup:** the `odoo-manager-agent` Redis ACL
  user briefly ended up with two valid password hashes (one from a
  first provisioning attempt that failed partway through before
  `+select`/`+eval` permissions were added, one from the working retry).
  Only the current one is written anywhere (`.env`) — the stale one was
  never recorded in any file — but a follow-up `ACL SETUSER
  odoo-manager-agent resetpass >current-password` on `redis-dev.int`
  would tidy it up. Left alone for now since a further ACL write wasn't
  re-authorized in this session.
- **Carried into Phase 7+:** real scope-extraction (turning "fix the
  invoice total" into `models=["account.move"]`) doesn't exist yet --
  `anticipated_scope` is an explicit parameter until real Odoo model
  names exist to extract against. `await_verification()`'s
  `spot_check_mismatch` is always `False` until Phase 11 builds the real
  deterministic diff-vs-coverage check -- both are documented,
  intentional gaps, not oversights.
- **Re-run `lint-imports` again after Phases 9, 10, and 11** build the
  three real specialists -- per the plan's own warning, exactly when a
  "quick" direct import becomes tempting.
- **New from Phase 7**: the SSH tunnel (`ssh -f -N -L
  18071:127.0.0.1:8071 andrew@odoo-dev.int`) is a background process on
  this devbox, not something that survives a reboot -- re-establish it
  before running anything Odoo-related in a fresh session.
- `oma_agent`'s scope so far is: base Internal User + "See all
  contacts" + "Extra Rights/Contact Creation" -- sufficient for
  Phase 7's testing, but Phase 9+'s real Build specialist will likely
  need additional groups for other models (products, projects,
  employees) as those tasks actually require them; grant narrowly and
  ask first, same pattern as this phase.
- `odoo_tool_allowlist.yaml` currently has one entry (`res.partner`,
  read/search_read/write). Extend it with an explicit, reviewed entry
  the moment a real task genuinely needs a new model/operation
  (products, projects, employees, calendar events for tasks 2/3) --
  never widen it speculatively ahead of an actual need.
- `scripts/cli_harness.py` now registers both real specialists
  (`bug_fix` → `BuildSpecialist`, `testing_qa` → `TestingQASpecialist`,
  both conditional on `OMA_ODOO_DB_DUPLICATE_FOR_BUILD` being set) —
  `DemoFakeSpecialist` is only a fallback now, not the default.
  `CodeReviewSpecialist` (Phase 10) is still not registered in
  `cli_harness.py` at all — nothing in the Manager's own loop calls
  `SpecialistType.code_review` yet (`TaskContract.validation_by` is
  always `"testing_qa"` in every example so far); wiring Code-Review
  into the actual per-task verification flow (reviewing a Build
  specialist's diff as part of closing a task, not just as a
  standalone-tested specialist) is real, not-yet-done integration work,
  worth doing before Phase 13's full end-to-end task testing.
- Phase 11's coverage-based spot-check treats a plain install as the
  only "test" a module gets — genuinely correct for task 1's shape (a
  field declaration has no method body to miss), but tasks 2/3 will
  have real business logic in compute methods and the like. Once a real
  reproduction test (not just an install) exists for those tasks, the
  spot-check should run against that test's own coverage, not just the
  install — worth revisiting when task 2 is actually attempted, not
  before.
- `generate_checked()`'s `</think>`-completeness check plus a
  validate-and-reask retry loop (Phase 10's fix for a real,
  reproducible malformed-JSON output bug on long responses) currently
  lives only in `CodeReviewSpecialist._review()`. If another specialist
  ever needs both disciplines together against a large-prompt call,
  worth factoring this into `infra/structured_output.py` as a shared
  helper rather than a second copy — not done now since this is the
  only current caller that needs both at once.
- **No longer blocking, narrowed in scope (Phase 9.5):** the Postgres
  table-ownership issue is specific to `duplicate_database()`'s own
  copy mechanism, not the Postgres instance as a whole — confirmed by a
  clean install against a genuinely fresh database
  (`create_database()`, new this pass). Practical habit going forward:
  use a fresh database for module-install testing (task 1's shape),
  duplicates for data-only testing (Phase 7's shape, which never hits
  this issue at all). A one-time ownership fix on `duplicate_database()`'s
  own output is still worth doing by whoever administers Postgres
  behind `odoo-dev.int`, but is no longer required to keep progressing.
- **Resolved (Phase 9.5):** the Odoo Development Agent Constitution now
  exists at the documented path, confirmed loading real, substantial
  content. All Constitution-dependent tests re-run against the real
  file; the temporary test-only stand-in logic has been removed from
  the test code.
- `BuildSpecialist`'s `data_change` branch is intentionally minimal
  this phase (routes toward `OdooToolClient` conceptually, per the
  `odoo-xmlrpc-operations` skill, but isn't exercised end-to-end) —
  real scope-extraction still doesn't exist (the same documented Phase
  6 gap), and no concrete `data_change` task was part of Phase 9's
  required test. Worth a real test once a concrete data-only task shows
  up.
- Tasks 2 and 3 (the relational-model service tasks) are explicitly
  deferred to a later pass, per the build plan's own sequencing —
  task 1 alone was this phase's required test.
- `tests/test_manager_session.py` still hangs on a real live-gateway
  call in this environment — re-confirmed this phase with the gateway
  itself confirmed healthy (`curl .../v1/models` succeeds), so it isn't
  a simple reachability issue. Unrelated to any Phase 9 change (nothing
  this phase touches `manager/session.py`); worth its own dedicated
  investigation before Phase 10/11, not something to keep re-deferring
  silently.
- Housekeeping, all harmless/isolated: `odoo16_dev_dup2_20260707` (a
  second duplicate from Phase 8's own systemic-check), the throwaway
  module `oma_throwaway_test` left inside `/mnt/extra-addons`, and
  `odoo16_dev_fresh_20260707_123141` (the Phase 9.5 fresh test database
  — its own `list_db` toggle window closed before it could be dropped
  the same way it was created; needs one more brief `list_db` window,
  or a manual drop, to actually remove it). None of these are on any
  code path or config value anything else depends on.
- **Chat UI session state is in-memory, single-process** — fine for
  the "simplest possible" first version this phase asks for, but won't
  survive a server restart or scale past one process. Worth revisiting
  if/when the UI needs to be genuinely multi-user or production-hosted,
  not before.
- The chat UI's `anticipated_scope` field is exposed on `POST
  /api/message` specifically so a sensitive-scope task can be tested
  deliberately (used by this phase's own sign-off round-trip test) —
  it is **not** real scope-extraction from free text, which still
  doesn't exist (the same documented Phase 6 gap). An ordinary chat
  message alone still cannot trigger a tier-3/4 pause; it needs this
  field supplied explicitly.
- `starlette.testclient.TestClient` prints a `StarletteDeprecationWarning`
  about `httpx` vs. a future `httpx2` package — harmless today (tests
  pass cleanly), worth a quick look whenever `httpx2` actually ships,
  not urgent.
- The Manager's own `six tools` (per the technical document's §3) still
  don't include an explicit `ask_operator()` call site wired into
  `run_turn()`'s own ambiguity-detection path — `PAUSE_AMBIGUITY` is
  defined in `manager/task_state.py` alongside `PAUSE_SIGN_OFF_REQUIRED`
  but, unlike sign-off, was not part of this phase's fix (no ambiguity-
  detection step currently exists in the loop at all — a separate,
  real gap from the sign-off one, not yet scoped into any phase).
- **Phase 13 additions carried forward:**
  `check_repeated_failures()` is scoped per-module-name — the real,
  recurring `ir.model.access.csv` bug this phase found and fixed
  happened across two *different* modules (`oma.service`,
  `oma.svc.record`/`oma.svc.group`), which the current mechanism
  would **not** have caught as "the same issue repeating," since it
  isn't designed to detect a systemic, cross-module specialist skill
  gap — only a repeated failure on one specific module. Worth a real
  look at whether repeated-failure detection should also consider
  failure *pattern* (e.g. the same `root_cause` + similar
  `failure_summary`), not just the same module string, though this
  wasn't in this phase's own required scope.
  `slugify_module_name()`'s first-4-words scheme produced a genuine
  collision this phase (two different goals both starting "Build a
  new 'oma...'" derived the identical module name) — harmless here
  (caught immediately by `scaffold_module()`'s own overwrite refusal),
  but worth a more collision-resistant naming scheme (e.g. appending a
  short hash of the full goal) if this keeps happening in practice.
  Two extra real modules now exist from this phase's testing
  (`oma_build_a_new_oma`, `oma_build_an_advanced_oma`,
  `oma_create_an_equipment_usage`) plus a new fresh database
  (`odoo16_dev_fresh_20260707_154658`) — all harmless, isolated,
  intentionally left as real evidence of this phase's work rather than
  cleaned up.
- **Phase 15 additions carried forward:** `run_plan()` exists and is
  tested at the `manager/task_plan.py` level (chained items, blocked_by,
  supersession), but nothing yet decides automatically WHEN a real Operator
  request should go through `run_plan()` instead of plain `run_turn()`
  — the same documented Phase 6 scope-extraction gap, now also covering
  "does this decompose into multiple items." The pending-escalation
  store (`manager/escalations.py`) has no dedicated "resolve/dismiss"
  endpoint yet — an escalation just sits in the queue until its 24h TTL
  expires or Operator starts a new, unrelated message; acceptable for this
  phase's own minimal-UI instruction, worth a real resolve action later.
  `ask_operator()` itself is still the same plain marker function from
  Phase 6 (no persistence of its own) — Phase 15 only added a real call
  site for it, not any new capability to the function itself.

Per §0.4/Phase 1 of the build plan, phases 1-6 don't need Odoo at all,
and the actual sandbox (Phase 14) isn't touched by any of this. Andrew
will provide real Odoo access when Phase 7 starts.

## Running the tests

```bash
set -a && source .env && set +a
./.venv/bin/python3 scripts/test_memory_schema.py              # PASS -- odoo_manager_dev
./.venv/bin/python3 tests/test_jit_postgres_role.py             # PASS -- odoo_manager_dev
./.venv/bin/python3 tests/test_fencing.py                        # PASS -- redis-dev.int DB3
./.venv/bin/python3 tests/test_gateway_client.py                 # PASS -- real 10.1.19.195:9090 gateway
./.venv/bin/python3 tests/test_manager_memory.py                 # PASS -- odoo_manager_dev
./.venv/bin/python3 tests/test_manager_session.py                # HANGS as of Phase 9 -- see Open Items, unrelated to Phase 9's own changes
./.venv/bin/python3 tests/test_manager_charter.py                # PASS -- purely mechanical, no LLM
./.venv/bin/python3 tests/test_manager_classify.py               # PASS -- real gateway, labeled test set
./.venv/bin/python3 tests/test_specialists_registry.py           # PASS -- schema + registry + fake-specialist delegation
./.venv/bin/python3 tests/test_manager_correction.py             # PASS -- real gateway + real Postgres
./.venv/bin/python3 tests/test_manager_learning.py               # PASS -- real gateway + real Postgres
./.venv/bin/python3 tests/test_manager_gateway_orchestration.py  # PASS -- gateway actually killed mid-task
./.venv/bin/python3 tests/test_manager_loop.py                   # PASS -- full loop, success + failure paths
./.venv/bin/python3 tests/test_manager_tools.py                  # PASS -- incl. grep-verified single-write-path
./.venv/bin/python3 tests/test_odoo_settings_guard.py            # PASS -- Production-instance allow-list guard
./.venv/bin/python3 tests/test_odoo_jit_apikey.py                # PASS -- real Odoo dev-copy (duplicate db)
./.venv/bin/python3 tests/test_odoo_tool_client.py               # PASS -- real Odoo, concurrency + allowlist tests
./.venv/bin/python3 tests/test_module_dev_toolchain.py           # PASS -- real container, scaffold/lint/install(blocked, structured)
./.venv/bin/python3 tests/test_build_specialist.py               # PASS -- real container + real LLM gateway, fencing/compensations/task1/outer-plan-extension
./.venv/bin/python3 tests/test_code_review_specialist.py         # PASS -- real container + real custom codebase + real LLM gateway
./.venv/bin/python3 tests/test_testing_qa_specialist.py          # PASS -- real container + real coverage.py + real LLM gateway
./.venv/bin/python3 tests/test_manager_sign_off.py               # PASS -- real Redis + real Postgres, tier-4 pause/approve/reject
./.venv/bin/python3 tests/test_chat_ui_server.py                 # PASS -- real starlette TestClient, real full stack
./.venv/bin/python3 tests/test_task_plan.py                      # PASS -- real Postgres, chained plan items + grep proof
./.venv/bin/python3 tests/test_replanning.py                     # PASS -- real loop + real gateway, all §19.9 scenarios
./.venv/bin/lint-imports                                          # PASS -- 1 kept, 0 broken
```

**Requires the SSH tunnel to odoo-dev.int running** (see Phase 7 section
above) for the `test_odoo_*`, `test_module_dev_toolchain.py`,
`test_build_specialist.py`, `test_code_review_specialist.py`,
`test_testing_qa_specialist.py`, and `test_chat_ui_server.py` (its
readonly-routing test now needs a real audit target) files.
Twenty-five of twenty-six test files pass clean against real infra as
of this revision (`test_manager_session.py` hangs, unrelated to
Phase 9 — see Open Items). Phases 1 through 13, plus 15, are all
closed on this DevBox, against the real Odoo dev-copy. Phase 14
(sandbox transfer) is deliberately deferred until Andrew has run his
own end-to-end check of the whole system, per his explicit sequencing
decision — not because anything here blocks it.

**Running the chat UI directly:**

```bash
set -a && source .env && set +a
./.venv/bin/python3 -m uvicorn ui.chat.server:app --host 127.0.0.1 --port 8199
# then open http://127.0.0.1:8199/ in a browser
```
