# OMA — application internals

See the [repo root README](../README.md) for what this is, the real-run
replay, and how to run the whole stack. This file is just a map of the
application code itself.

## Layout

| Path | What it is |
|---|---|
| `manager/` | The Manager: classification, decomposition, the constraint-graph scheduler, replanning/retry, certification, memory. |
| `specialists/` | Build, Code-Review, Testing/QA — each independent, each importable only via `specialists/registry.py` (enforced by `pyproject.toml`'s import-linter contract). |
| `contracts/` | Shared schemas and pure-logic helpers both `manager/` and `specialists/` depend on. |
| `infra/` | Everything that talks to the outside world: Postgres, Redis, Neo4j, the Odoo connection + safety guard, the fencing lock, the LLM gateway. |
| `tools_odoo/` | Odoo-specific tooling: the knowledge-graph pipeline, schema introspection, the Gitea-backed version-control client, the module-dev toolchain (install/scaffold/test). |
| `ui/chat/` | The FastAPI server + single-page chat UI this whole thing runs behind. |
| `registry/` | The skills (`odoo-module-scaffolding`, `odoo-codebase-audit`, `odoo-verification-and-reproduction`, ...) and the constitution every specialist's generated code is checked against. |
| `scripts/` | Operational scripts kept as working examples: schema migrations, the knowledge-graph ETL, the Gitea branch-cleanup job, the CLI test harness. |
| `state/` | Small persisted JSON state: scope certification, the gate-tamper-protection manifest. |
| `var/` | Generated/working data (knowledge-graph intermediate files, backups) — gitignored. |

## Configuration

Every credential, address, and port is read from the environment via
`infra/settings.py` — `.env.example` at the repo root documents every
variable with inline comments on what it's for and what to set it to.
Nothing is ever hardcoded as a fallback that matters (see `paths.py` for
the filesystem-path equivalent: constitution path, skills path, coverage
evidence path, all overridable, all defaulting to a location inside this
repo).

## The autonomy model

`MANAGER_CONSTITUTION.md` defines the four autonomy tiers (read-only →
notify-after → sign-off-before-deploy → sign-off-before-delegating) and how
a task's tier gets decided mechanically from `sensitive_paths.yaml`, not a
judgment call made in the moment.

## Tests

`tests/` has real tests for the mechanisms above — the scheduler, the
fencing lock, the certification state machine, each specialist's own
validators. Most require the real stack running (Postgres/Redis/Neo4j/Odoo)
rather than mocks — this codebase's own stated discipline is "never mock
infrastructure," per several test files' own module docstrings.
