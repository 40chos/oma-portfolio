#!/usr/bin/env bash
# The official postgres image only auto-creates the single database named by
# POSTGRES_DB. Odoo needs its own database (odoo16_dev); OMA's own durable
# event log needs a separate one (oma) so the two schemas never collide.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE DATABASE "${ODOO_DB_NAME:-odoo16_dev}";
EOSQL
