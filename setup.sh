#!/usr/bin/env bash
# One-command setup: clone, run this, open the URL it prints. No other manual
# steps for the default (local LLM) path. Safe to re-run -- every step here is
# idempotent (checked before done, not blindly repeated).
set -euo pipefail
cd "$(dirname "$0")"

say() { echo "==> $*"; }

# ---------------------------------------------------------------------------
# 0. Docker socket (only matters for non-Docker-Desktop runtimes, e.g. Colima)
# ---------------------------------------------------------------------------
if [ -n "${OMA_DOCKER_HOST_OVERRIDE:-}" ]; then
  export DOCKER_HOST="$OMA_DOCKER_HOST_OVERRIDE"
elif [ -f "$HOME/.colima/default/docker.sock" ] && ! docker ps >/dev/null 2>&1; then
  export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
  say "Detected Colima; using its Docker socket."
fi

if ! docker ps >/dev/null 2>&1; then
  echo "Docker isn't reachable. Start Docker Desktop (or 'colima start' if you use Colima) and re-run this script." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. .env -- the root one (host-port overrides, read by docker-compose.yml's
#    own ${VAR} interpolation) AND oma/.env (everything else). Both get
#    exported here so the helper scripts below (which talk to the containers
#    over their published host ports, same as docker-compose.yml does) agree
#    with docker-compose on which ports those actually are -- a port override
#    in the root .env with nothing here would otherwise make these scripts
#    silently fall back to the *default* port instead, which may belong to a
#    completely different stack on the same machine.
# ---------------------------------------------------------------------------
if [ -f .env ]; then
  set -a; source .env; set +a
fi
if [ ! -f oma/.env ]; then
  say "Creating oma/.env from oma/.env.example (local LLM mode, no API key needed)."
  cp oma/.env.example oma/.env
else
  say "oma/.env already exists -- leaving it as-is."
fi
set -a; source oma/.env; set +a
LLM_MODE="${OMA_LLM_MODE:-local}"

# ---------------------------------------------------------------------------
# 2. Core infra
# ---------------------------------------------------------------------------
say "Starting Postgres, Redis, Neo4j, Odoo CE, Gitea..."
docker compose up -d postgres redis neo4j odoo-init odoo gitea

say "Waiting for everything to report healthy (can take a minute on first run)..."
for svc in postgres redis neo4j odoo gitea; do
  for i in $(seq 1 60); do
    status=$(docker compose ps "$svc" --format '{{.Health}}' 2>/dev/null || echo "")
    [ "$status" = "healthy" ] && break
    sleep 3
  done
  echo "  $svc: $(docker compose ps "$svc" --format '{{.Status}}' 2>/dev/null)"
done

# ---------------------------------------------------------------------------
# 3. Python env (needed by every script below this point)
# ---------------------------------------------------------------------------
if [ ! -d .venv ]; then
  say "Creating a Python venv and installing dependencies..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r oma/requirements.txt
fi
export OMA_VENV_PYTHON="$(pwd)/.venv/bin/python3"

# ---------------------------------------------------------------------------
# 4. Gitea provisioning (idempotent -- see the script itself)
# ---------------------------------------------------------------------------
if ! grep -q "^DEV_AGENT_GITEA_TOKEN=." oma/.env 2>/dev/null; then
  say "Provisioning Gitea (admin user, org, repo, token)..."
  ./docker/gitea-bootstrap.sh
else
  say "Gitea already provisioned (oma/.env has a token) -- skipping."
fi

# ---------------------------------------------------------------------------
# 5. Odoo database provisioning (the sandbox-isolation target + the golden
#    template the sandbox pre-flight clones from -- idempotent)
# ---------------------------------------------------------------------------
say "Provisioning Odoo databases (sandbox target + golden template)..."
./docker/provision-odoo-databases.sh
set -a; source oma/.env; set +a

# ---------------------------------------------------------------------------
# 6. LLM backend
# ---------------------------------------------------------------------------
if [ "$LLM_MODE" = "cloud" ]; then
  if [ -z "${OMA_CLOUD_ESCALATION_API_KEY:-}" ]; then
    echo "OMA_LLM_MODE=cloud but OMA_CLOUD_ESCALATION_API_KEY is empty in oma/.env -- set it and re-run." >&2
    exit 1
  fi
  say "Starting LiteLLM (cloud mode)..."
  docker compose --profile cloud up -d litellm
else
  say "Starting Ollama (local mode) and pulling models -- first run downloads ~11GB, can take a while..."
  docker compose up -d ollama
  docker compose up -d ollama-init
  say "Waiting for model pull to finish..."
  docker wait "$(docker compose ps -q ollama-init)" >/dev/null
fi

# ---------------------------------------------------------------------------
# 7. Knowledge graph seed (idempotent: re-running just re-imports the same data)
# ---------------------------------------------------------------------------
if [ ! -f oma/var/knowledge_graph/final_module_graph.jsonl ]; then
  say "Seeding the knowledge graph from Odoo CE's own demo modules..."
  ./docker/seed-knowledge-graph.sh
else
  say "Knowledge graph already seeded -- skipping (delete oma/var/knowledge_graph to redo)."
fi

echo
say "Everything's up. Opening the app on http://localhost:${APP_HOST_PORT:-8000}"
echo "    (Ctrl-C stops the app; the containers keep running -- 'docker compose down' to stop those too)"
echo
cd oma
set -a; source .env; set +a
exec ../.venv/bin/uvicorn ui.chat.server:app --host 0.0.0.0 --port "${APP_HOST_PORT:-8000}"
