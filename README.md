# OMA — Odoo Manager Agent

A multi-agent system that takes a plain-English request ("add a field to
this model," "restrict this view to a security group") and turns it into a
real, installed Odoo ERP module change — writing the code, reviewing it,
installing it in an isolated sandbox, verifying the result independently,
and only then promoting it. A human-approval gate sits in front of anything
sensitive.

This is a port of a system I built and ran in production, stripped of
everything specific to that environment and rebuilt against a fully
self-hosted stack (Odoo Community Edition, Postgres, Redis, Neo4j, Gitea,
and either a local model via Ollama or a cloud model via LiteLLM) so anyone
can run the real thing. Every mechanism below — the scheduler, the fencing
lock, the knowledge graph, the three-specialist review pipeline, the
certification system, the failure-recovery logic — is the real,
unmodified code, not a simplified rewrite. Only the environment it talks to
changed. [`DECISIONS.md`](DECISIONS.md) is the honest log of every judgment
call made during that port, including the real bugs found by actually
running it.

**[→ Watch a real run replay](https://claude.ai/code/artifact/2aa5e7f5-bf07-4066-bdb5-af4ea9fcea0f)** —
a scrubbable flight-recorder timeline built from the actual trace of one
real execution: the request going in, classification, Build writing code,
Code-Review and Testing/QA independently checking it, and the real result —
including the real generated `models.py`/`views.xml` and the real commit.
No video, no slides — the real captured data, replayable in your browser,
free, forever, no server required.

## What's actually interesting here

- **A constraint-graph scheduler** (`manager/graph_scheduler.py`) that
  dispatches independent sub-tasks in parallel under a concurrency cap,
  respecting real dependency edges, with Tarjan's-algorithm cycle
  detection on the constraint graph.
- **A fencing lock** (`infra/fencing.py`) implementing the Kleppmann
  distributed-lock fix — a monotonic fence token, not just a TTL'd lock,
  so a stale writer can never commit after losing its lock.
- **A real structural knowledge graph** of the target Odoo instance
  (modules/models/fields/views and their real relationships) in Neo4j,
  used to ground generation in real facts and catch cross-module
  "blast radius" collisions before a write happens.
- **Three independent specialists** — Build writes code, Code-Review runs
  an independent pass with dozens of hallucination-filtering checks
  against the live schema and knowledge graph, Testing/QA installs the
  result in a sandbox and verifies it behaviorally. Testing/QA never
  trusts Build's own claim of success.
- **A certification system** that tracks real reliability per
  request-category over time and gates whether a category is trusted
  enough to hand to a deterministic, non-AI generator instead of an LLM.
- **Automatic failure recovery** — detects LLM repetition loops,
  infrastructure outages, and gateway failures, and resumes paused work
  with exponential backoff, no human needed for routine transient
  failures.
- **A sandboxed, allow-listed execution model** — the system can only
  ever write to an explicitly allow-listed target, enforced in code
  (`infra/odoo_settings.py`'s `ProductionOdooGuardError`), never just
  convention.

## Run it yourself

Needs Docker (Desktop, or Colima/similar) and ~10GB free for images/models.
Everything runs on demand — nothing stays running permanently; this is
designed to come up for a session (a demo, an interview screen-share) and
tear down after.

```bash
git clone <this-repo>
cd oma-portfolio
./setup.sh
```

That's it. `setup.sh` brings up Postgres/Redis/Neo4j/Odoo CE/Gitea, waits
for everything to report healthy, provisions Gitea (org/repo/token) and
the two Odoo databases Build/Testing-QA actually write into, pulls and
starts a local LLM via Ollama (the default — free, fully self-hosted; set
`OMA_LLM_MODE=cloud` and `OMA_CLOUD_ESCALATION_API_KEY` in `oma/.env`
first if you'd rather use a cloud model), seeds the knowledge graph from
Odoo's own demo modules, and starts the app — printing the URL to open
when it's ready. Safe to re-run; every step checks whether it's already
done before doing it again.

Open `http://localhost:8000`, type a request (e.g. *"On res.partner, add a
field for a LinkedIn profile URL"*), and watch it classify, build, review,
test, and install — against a real Odoo CE instance with real demo data.

Verified end to end from a genuinely clean clone (fresh volumes, no prior
state) as part of this port — see `DECISIONS.md`, Stage 8.

### Why no `app` container by default

The Build/Testing-QA specialists run `docker exec` against the sibling
`odoo` container (the direct replacement for the original's SSH-based
remote exec — see `DECISIONS.md`, Stage 5). Doing that from *inside* a
container means mounting the host's Docker socket into it, which grants
that container full control over the whole Docker daemon — a real
sandbox-escape-adjacent risk, and in tension with this project's own
"sandboxed, allow-listed execution" design. Running the app on the host
instead means `docker exec` is just the normal Docker CLI a developer
already has. A containerized `app` service is still in `docker-compose.yml`
for anyone who wants full containerization and is comfortable with that
tradeoff (`docker compose up -d app` — see its comment in that file).

## How it's organized

```
oma/                   the actual application (unchanged internal structure:
                        manager/, specialists/, contracts/, infra/, tools_odoo/)
oma/registry/          skills (odoo-module-scaffolding, odoo-codebase-audit, ...)
                        and the constitution every specialist checks its own work against
docker/                Compose support: Dockerfiles, entrypoints, Gitea/knowledge-graph
                        bootstrap scripts, the LiteLLM cloud-mode config
docs/                  the real-run replay page (GitHub Pages, served from here)
demo-capture/          the real captured trace the replay page is built from
DECISIONS.md           every judgment call made during the port, and why
```

`oma/README.md` has the deeper per-module documentation (env vars,
specialist internals, the manager's autonomy-tier model in
`oma/MANAGER_CONSTITUTION.md`).

## Status

All 7 stages — infra, knowledge graph, LLM gateway, Git versioning, SSH
removal, full end-to-end wiring, and final secret-scan + clean-clone
verification — are done. `DECISIONS.md` has real evidence for each one:
command output, real database/Gitea state, and the real bugs found by
actually running this rather than just reading it. Gitleaks and
TruffleHog both report zero findings across the full git history.
