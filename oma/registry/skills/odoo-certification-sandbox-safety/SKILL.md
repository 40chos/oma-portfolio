---
name: odoo-certification-sandbox-safety
version: 1.0.0
last_validated: null
description: How to safely use the certification_tooling sandbox
  primitives (create/drop sandbox databases, fetch/delete module files,
  run ETL/lint against a sandbox container) without leaking state or
  running a destructive filesystem/process call against the wrong
  target. Use whenever a task calls anything under
  registry/tools/primitives/certification_tooling/.
---

# Odoo Certification Sandbox Safety

## When to use this
Any task that touches `registry/tools/primitives/certification_tooling/`
— this is the largest real primitive domain in the registry (136 files)
and, by real, computed classifier evidence (`behavior_classifier.py`,
run 2026-08-29), also the domain with the most `high_risk` findings in
the whole system (33 of the registry's 68 total high-risk primitives).
The real signals driving that tier are concrete, not speculative:
`shutil.rmtree()` calls in `_fetch_module_to_local_temp.py` and
`_reparse_and_update_source_jsonl.py`, and `subprocess.run()` calls in
`create_sandbox_database.py`, `drop_sandbox_database.py`,
`delete_module_file.py`, `fetch_module.py`, `check_field_exists_on_model.py`,
and `_run_etl_incremental.py` — real raw process/filesystem primitives,
not a false positive from the classifier's pattern list.

## Rules specific to this domain
- **`create_sandbox_database()` and `drop_sandbox_database()`
  (`services/oma/tools_odoo/module_dev/toolchain.py:411-480` and
  `:483-505`) must always be called as a pair.** `drop_sandbox_database()`
  exists specifically because a real, confirmed bug (2026-07-22) let
  746+ leftover sandbox filestore directories accumulate from dropping
  the database alone without also removing its filestore directory —
  always call `drop_sandbox_database()` in a `finally` block around any
  round that calls `create_sandbox_database()`, never only on the
  success path.
- Both functions validate `db_name` against `^[a-z0-9_]+$` before ever
  reaching the container — this is a real SQL/shell-injection guard on
  an identifier that gets interpolated into a shell command, not
  decorative. Never bypass it by constructing the shell command
  yourself instead of calling the function.
- `create_sandbox_database(template=...)` clones Postgres rows via
  `createdb -T` but does **not** clone the on-disk filestore directory
  automatically — the function copies
  `/var/lib/odoo/filestore/<template>/` itself as a real, separate
  step, best-effort (a missing source directory is a silent no-op).
  If you ever reimplement or wrap this call, preserve that second copy
  step — skipping it reproduces the exact FileNotFoundError class
  (e.g. hr's default employee photo) this function was written to
  avoid.
- `fetch_module()` (`fetch_module.py`) is filesystem-only — it never
  touches the Odoo database (no XML-RPC, no ORM call). Do not assume a
  successful fetch means the module is installed or valid against a
  live database; it only means the source tree now exists locally
  under the caller-chosen `output_dir`.
- All of these functions require real SSH + `sudo docker exec` access
  to the sandbox container via `OMA_ODOO_SSH_HOST`/`OMA_ODOO_SSH_USER`
  (optionally `OMA_ODOO_SSH_KEY_PATH`) — never hardcode a host or
  credential inline; if these env vars are absent, the correct
  response is to report the real blocker, not to guess a fallback
  value.
- These primitives operate against sandbox containers/databases only —
  never the production Odoo instance. Nothing in this domain's real
  code path performs its own production/sandbox check; that discipline
  has to come from the caller. Before invoking any
  `create_sandbox_database`/`drop_sandbox_database`/`delete_module_file`
  call, confirm the `container`/`db_name` argument actually names a
  sandbox target, not a value passed through unexamined from elsewhere
  in a task.

## How
1. Before writing/testing a module change, `create_sandbox_database()`
   against a real sandbox container, optionally cloning from a known
   template to skip re-bootstrapping Odoo's base modules.
2. Run the real work (fetch/lint/ETL/field-check primitives in this
   same domain) against that sandbox database — never the production
   instance.
3. Always `drop_sandbox_database()` in a `finally` block, regardless of
   whether the round succeeded, so no leftover database or filestore
   directory survives the round.
4. If a `fetch_module()`/`delete_module_file()` call is needed, confirm
   the target `output_dir`/module name explicitly rather than trusting
   a value threaded through from an earlier, unrelated step.

## Revision Log
(empty at creation — entries get added here as real incidents are
found, matching the convention already used across the other 6 skill
docs in this repo.)
