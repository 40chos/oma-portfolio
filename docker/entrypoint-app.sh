#!/usr/bin/env bash
# Entrypoint for the OMA app container -- replaces the original systemd-unit
# startup with a plain, idempotent script: apply the Postgres schema (safe to
# re-run, every statement is CREATE ... IF NOT EXISTS), ensure this month's
# partition exists, then start the FastAPI server.
set -euo pipefail

echo "[entrypoint] waiting for postgres at ${OMA_PG_HOST}:${OMA_PG_PORT}..."
until PGPASSWORD="$OMA_PG_PASSWORD" psql -h "$OMA_PG_HOST" -p "$OMA_PG_PORT" -U "$OMA_PG_USER" -d "$OMA_PG_DB" -c '\q' 2>/dev/null; do
  sleep 1
done
echo "[entrypoint] postgres is up."

echo "[entrypoint] applying agent_memory_events schema (idempotent)..."
PGPASSWORD="$OMA_PG_PASSWORD" psql -h "$OMA_PG_HOST" -p "$OMA_PG_PORT" -U "$OMA_PG_USER" -d "$OMA_PG_DB" \
  -f /app/scripts/001_agent_memory_events.sql

echo "[entrypoint] ensuring current partitions exist..."
python3 /app/scripts/010_ensure_agent_memory_events_partitions.py

echo "[entrypoint] starting uvicorn..."
exec uvicorn ui.chat.server:app --host 0.0.0.0 --port 8000
