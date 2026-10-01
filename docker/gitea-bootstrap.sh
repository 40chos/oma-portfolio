#!/usr/bin/env bash
# One-time Gitea provisioning: run after `docker compose up -d gitea` (or just
# `docker compose up -d`, which brings gitea up as a dependency anyway).
# Idempotent -- safe to re-run. Replaces what the original system did by hand
# against a pre-existing internal Gitea instance: create the admin user, the
# org, and the dedicated generated-modules repo, then mint an access token and
# write it into .env so docker compose (and a host-side venv run of the app)
# both pick it up.
set -euo pipefail

cd "$(dirname "$0")/.."

GITEA_URL="${GITEA_URL:-http://127.0.0.1:${GITEA_HOST_PORT:-3000}}"
ADMIN_USER="${GITEA_ADMIN_USER:-oma_admin}"
ADMIN_PASSWORD="${GITEA_ADMIN_PASSWORD:-oma_dev_password}"
ORG="${DEV_AGENT_GITEA_ORG:-oma}"
REPO="${DEV_AGENT_GITEA_GENERATED_MODULES_REPO:-oma-generated-modules}"
ENV_FILE="${OMA_ENV_FILE:-oma/.env}"

echo "[gitea-bootstrap] waiting for gitea at ${GITEA_URL}..."
until curl -sf "${GITEA_URL}/api/healthz" >/dev/null 2>&1; do sleep 1; done

if ! docker exec -u git oma-gitea-1 gitea admin user list 2>/dev/null | grep -q "${ADMIN_USER}"; then
  echo "[gitea-bootstrap] creating admin user ${ADMIN_USER}..."
  docker exec -u git oma-gitea-1 gitea admin user create \
    --username "${ADMIN_USER}" --password "${ADMIN_PASSWORD}" \
    --email "${ADMIN_USER}@oma.local" --admin --must-change-password=false
else
  echo "[gitea-bootstrap] admin user ${ADMIN_USER} already exists."
fi

AUTH="${ADMIN_USER}:${ADMIN_PASSWORD}"

if ! curl -sf -u "${AUTH}" "${GITEA_URL}/api/v1/orgs/${ORG}" >/dev/null 2>&1; then
  echo "[gitea-bootstrap] creating org ${ORG}..."
  curl -sf -u "${AUTH}" -X POST "${GITEA_URL}/api/v1/orgs" \
    -H "Content-Type: application/json" -d "{\"username\": \"${ORG}\"}" >/dev/null
else
  echo "[gitea-bootstrap] org ${ORG} already exists."
fi

if ! curl -sf -u "${AUTH}" "${GITEA_URL}/api/v1/repos/${ORG}/${REPO}" >/dev/null 2>&1; then
  echo "[gitea-bootstrap] creating repo ${ORG}/${REPO}..."
  curl -sf -u "${AUTH}" -X POST "${GITEA_URL}/api/v1/orgs/${ORG}/repos" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${REPO}\", \"auto_init\": true, \"default_branch\": \"main\"}" >/dev/null
else
  echo "[gitea-bootstrap] repo ${ORG}/${REPO} already exists."
fi

echo "[gitea-bootstrap] minting access token..."
TOKEN_NAME="oma-dev-agent-$(date +%s)"
TOKEN=$(curl -sf -u "${AUTH}" -X POST "${GITEA_URL}/api/v1/users/${ADMIN_USER}/tokens" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"${TOKEN_NAME}\", \"scopes\": [\"write:repository\", \"write:organization\"]}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha1"])')

if [ -z "${TOKEN}" ]; then
  echo "[gitea-bootstrap] FAILED to mint a token." >&2
  exit 1
fi

echo "[gitea-bootstrap] writing DEV_AGENT_GITEA_* into ${ENV_FILE}..."
for key in DEV_AGENT_GITEA_URL DEV_AGENT_GITEA_TOKEN DEV_AGENT_GITEA_ORG DEV_AGENT_GITEA_GENERATED_MODULES_REPO; do
  grep -v "^${key}=" "${ENV_FILE}" 2>/dev/null > "${ENV_FILE}.tmp" || touch "${ENV_FILE}.tmp"
  mv "${ENV_FILE}.tmp" "${ENV_FILE}"
done
{
  echo "DEV_AGENT_GITEA_URL=http://gitea:3000"
  echo "DEV_AGENT_GITEA_TOKEN=${TOKEN}"
  echo "DEV_AGENT_GITEA_ORG=${ORG}"
  echo "DEV_AGENT_GITEA_GENERATED_MODULES_REPO=${REPO}"
} >> "${ENV_FILE}"

echo "[gitea-bootstrap] done. Gitea ready at ${GITEA_URL} (${ORG}/${REPO})."
