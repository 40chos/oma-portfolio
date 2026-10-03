# Architecture

This is a technical companion to the [README](README.md) — more detail on how the
pieces actually fit together, for anyone deciding whether to clone and run it rather
than just skim it.

> [!NOTE]
> **Scope note:** As highlighted in the [README](README.md), this repository represents a functional, sanitized, and self-hosted **architectural slice** of a commercial system — not the final enterprise production state. It is scoped to what demonstrates the core agent engineering (the constraint-graph scheduler, the review/verification pipeline, the knowledge graph, and safety mechanisms) running against an open-source stack. Enterprise production layers — such as distributed asynchronous message queues (e.g. RabbitMQ), centralized observability and monitoring (e.g. Prometheus, Grafana), and proprietary multi-service backup topologies — are commercial operational concerns that cannot be shared under NDA and are omitted here. Where this document says a mechanism is "real," it refers to fully verifiable execution within this repository; see [DECISIONS.md](DECISIONS.md) for the complete engineering and verification log.

## Request lifecycle

Every operator message is handled as one **turn**:

1. **Governance gates.** A small set of deterministic, non-AI rules run first —
   before any model is called — to hard-block a narrow category of requests outright
   (e.g. "never touch the accounting module without asking"). These are plain
   pattern checks, not judgment calls, and they can never be talked around by clever
   prompt phrasing because no model sees the request until after they pass.
2. **Classification.** A fast-extraction model call determines the risk tier and
   work-type (capability class) of the request. Tier drives what happens next —
   routine work proceeds autonomously; anything touching a sensitive scope pauses
   for an explicit human sign-off before a single line of code is written.
3. **Decomposition.** The Manager decides whether the goal is small enough to build
   in one pass, or needs splitting into a constraint graph of smaller pieces (see
   below). This is itself a real decision point, not automatic — a single clean field
   addition stays one node; a request spanning multiple models, views, and security
   rules really does split.
4. **Dispatch.** Each graph node is handed to the specialist pipeline. Independent
   nodes (no dependency edge between them) can run concurrently, under a concurrency
   cap.
5. **Promotion or pause.** A node that passes review and verification is promoted to
   the real target database. A node that can't resolve within its round budget
   escalates back to a human with a concrete, logged reason — never a silent retry
   loop, never a generic "something went wrong."

## The constraint-graph scheduler

`manager/graph_scheduler.py` is where a goal becomes a graph. The Manager drafts
candidate splits, a second pass critiques and merges them into one real plan (not a
verbatim pick of a single draft), and the result is a set of nodes with explicit
`blocked_by` dependency edges — not a flat list, not a simple linear chain.

- **Cycle detection** uses Tarjan's strongly-connected-components algorithm against
  the real dependency edges before any node is dispatched, so a genuinely circular
  plan is caught structurally rather than discovered at runtime as a deadlock.
- **Dispatch order** respects dependencies: a node only starts once everything it
  `blocked_by` has actually passed. Independent nodes with no edge between them
  genuinely run in parallel, under a configurable concurrency cap — not simulated
  concurrency, real concurrent specialist calls.
- **Node state** is a small, honest vocabulary: `pending` → `running` →
  `passed`/`failed`, plus `blocked` for a node whose dependency hasn't cleared yet.
  The live chat UI's graph view renders this directly from the real backend state,
  not a client-side approximation.
- **Splitting can go more than one level deep.** A node that turns out to still be
  too large for one round can itself be split further in a later round — the graph
  isn't fixed at intake, it's revised as real information (what actually failed, what
  actually turned out to be in scope) comes in.

## The three-specialist pipeline

Every graph node goes through three independent passes, not one model asked to
self-certify:

1. **Build** writes the actual module code — models, views, security records,
   whatever the node's own scope requires.
2. **Code-Review** runs a separate pass with its own set of hallucination-filtering
   checks against the live schema and knowledge graph: does the field it's reviewing
   actually exist, does the method it's calling actually exist on the model being
   extended, is a `.sudo()` call flagged for human attention. Code-Review never
   trusts Build's own description of what it did — it re-derives the real diff and
   checks it independently.
3. **Testing/QA** installs the result in an isolated sandbox and verifies it
   behaviorally — not just "does it install," but for certain goal shapes (a computed
   field, a sequence-assigned field), an actual create/read round-trip against the
   live Odoo instance to confirm the field doesn't just exist but actually does the
   thing it claims to.

A node that fails any stage gets a structured failure record back, not a generic
error, and the Manager decides whether to retry with that specific feedback, split
the node further, or escalate — governed by a round budget so nothing retries
forever.

## Knowledge graph grounding

A real structural graph of the target Odoo instance — modules, models, fields, views,
and their real relationships — lives in Neo4j, built by parsing actual Odoo module
source (not a hand-maintained schema description that can drift from reality).

This grounds generation in facts rather than model memory: before Build writes a
field, before Code-Review approves a diff, the system can check what's actually true
of the live schema. It's also the mechanism behind cross-module "blast radius"
checks — before a change lands, the graph can surface what else references the thing
being touched, catching a collision a model working from the request text alone would
never see.

## Safety mechanisms

- **A fencing lock** (`infra/fencing.py`), implementing the Kleppmann distributed-lock
  pattern — a monotonic fence token accompanies every write claim, not just a TTL'd
  lock. A writer that's lost its lock (e.g. after a long GC pause or network partition)
  can never have a stale write accepted, because the fence token it would present is
  provably out of date.
- **Sandbox pre-flight.** Nothing writes to the real target database directly. Build
  and Testing/QA work against an isolated sandbox clone first; only a clean pass gets
  promoted.
- **Allow-listed execution.** The system can only ever write to an explicitly
  allow-listed target, enforced in code, never left as a convention someone could
  forget to follow.
- **A certification system** tracks real reliability per request-category over time
  and gates whether a category is trusted enough to eventually hand to a
  deterministic, non-AI generator instead of an LLM call — reliability has to be
  earned per category, not assumed globally.
- **Automatic failure recovery** detects LLM repetition loops, infrastructure
  outages, and gateway failures, and resumes paused work with exponential backoff —
  routine transient failures don't need a human to notice and manually retry.

## Live streaming

The chat UI opens a task's live event stream (Server-Sent Events over Redis pub/sub)
the instant the Manager mints a task_id — before the round loop has done any real
work yet — so classification, node creation, and token-by-token code generation are
visible as they actually happen, not summarized after the fact once everything's
already finished.

## What this repo intentionally doesn't include

Stated directly, not buried: this is a demonstration of the agent architecture, not a
24/7 production deployment. Left out on purpose, not because they're hard:

- A dedicated observability stack (structured tracing, metrics dashboards, alerting
  on drift) — the live SSE stream and the Postgres-backed event log already make
  every real decision inspectable for a demo session; a standing metrics pipeline is
  infrastructure for a service that runs continuously, which this isn't meant to.
- Message-queue-based job orchestration for background/scheduled work — the one
  background job this repo ships (`scripts/recurring_backlog_triage.py`) is a plain
  script runnable by hand or any scheduler, not wired to a queue.
- Multi-destination, NAS-backed backup topology — `scripts/odoo_filestore_backup.py`
  does a real, working pg_dump + filestore backup per task; a production-grade
  retention/rotation policy across multiple storage backends is a separate, larger
  concern this demo doesn't need to solve.

See [DECISIONS.md](DECISIONS.md) for the full, dated log of every judgment call made
porting this out of its original environment, including the real bugs found by
actually running it.
