#!/usr/bin/env bash
# One-time Odoo database provisioning: the duplicate database Build/Code-Review
# actually write into (never the main demo db directly), and the "golden
# template" database the sandbox pre-flight clones from for a fast, isolated
# per-round install. Idempotent: skips whatever already exists.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${OMA_VENV_PYTHON:-.venv/bin/python3}"
ENV_FILE="${OMA_ENV_FILE:-oma/.env}"

cd oma
set -a; source .env; set +a
cd ..

if [ -z "${OMA_ODOO_DB_DUPLICATE_FOR_BUILD:-}" ]; then
  echo "[provision-odoo-db] creating the Build/Code-Review target database..."
  NEW_DB=$("$PY" -c "
import sys, datetime
sys.path.insert(0, 'oma')
from infra.odoo_admin import duplicate_database
name = f'odoo16_dev_dup_{datetime.datetime.now():%Y%m%d_%H%M%S}'
duplicate_database(name)
print(name)
")
  echo "[provision-odoo-db] created: $NEW_DB"
  grep -v "^OMA_ODOO_DB_DUPLICATE_FOR_BUILD=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  echo "OMA_ODOO_DB_DUPLICATE_FOR_BUILD=$NEW_DB" >> "$ENV_FILE"
else
  echo "[provision-odoo-db] OMA_ODOO_DB_DUPLICATE_FOR_BUILD already set ($OMA_ODOO_DB_DUPLICATE_FOR_BUILD) -- skipping."
fi

cd oma
set -a; source .env; set +a
cd ..

echo "[provision-odoo-db] ensuring the sandbox golden-template database exists..."
"$PY" -c "
import sys
sys.path.insert(0, 'oma')
from infra.odoo_admin import create_database
from infra.odoo_settings import SANDBOX_TEMPLATE_DB
import xmlrpc.client
from infra.odoo_settings import load_odoo_settings

s = load_odoo_settings()
proxy = xmlrpc.client.ServerProxy(f'{s.url}/xmlrpc/2/db')
try:
    exists = proxy.db_exist(SANDBOX_TEMPLATE_DB)
except Exception:
    exists = False
if exists:
    print(f'[provision-odoo-db] {SANDBOX_TEMPLATE_DB} already exists -- skipping.')
else:
    create_database(SANDBOX_TEMPLATE_DB)
    print(f'[provision-odoo-db] created: {SANDBOX_TEMPLATE_DB}')
"

echo "[provision-odoo-db] done."
