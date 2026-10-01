# Skill Registry

Per `docs/planning/OMA_SYSTEM_BLUEPRINT_DIAGRAM.html`'s "Skill
Registry" panel — knowledge an agent loads, never executes. Distinct
from `../tools/`: a skill informs how a tool is used, it is not itself
an action.

## What's actually here today

Five real, Odoo-domain skills, mined from real dev-agent work on the
`odoo16_dev` codebase: `odoo-codebase-audit`, `odoo-module-scaffolding`,
`odoo-safe-financial-queries`, `odoo-verification-and-reproduction`,
`odoo-xmlrpc-operations`. Each is real content used by `agents/services/oma`
today, not a placeholder.

## What this is not yet

The diagram also specifies a general **Knowledge graph** — facts as
nodes, relationships between facts as edges, built on the system's
hybrid graph+vector retrieval engine, with deterministic, trust-gated
query templates for known question shapes (see the diagram's
"Knowledge graph" glossary entry and its "Skill Registry" panel prose).
That general fact/relationship layer does not exist yet — this folder
holds domain skill documents an agent reads, not a queryable fact
store. Building it is retrieval-engine work, out of scope for this
pass; noted here honestly so nobody mistakes five Odoo skill files for
the general Knowledge layer.
