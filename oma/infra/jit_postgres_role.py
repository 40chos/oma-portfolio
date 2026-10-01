"""Just-in-time, scoped Postgres credentials -- Phase 1 step 6, the
Postgres-side half of the technical document's §8 JIT-credential
requirement (motivated by the April 2026 "PocketOS" incident: a coding
agent with a standing broad credential deleted a production database and
all its backups in 9 seconds).

Small-team pattern, not a full Vault deployment: CREATE ROLE ... VALID
UNTIL for a hard expiry, but -- and this is the part that actually
matters -- VALID UNTIL alone only blocks *new* logins. It does not end
an already-open session. So a task's role gets explicitly dropped
(REASSIGN OWNED BY + DROP OWNED BY + DROP ROLE) at the end of the task
rather than trusting expiry alone to clean anything up.

The Odoo-side half (a scoped, short-lived API key rather than a
permanent one) is built in Phase 7, once a real Odoo instance exists to
issue keys against.
"""

from __future__ import annotations

import re
import secrets
import string

import psycopg2
import psycopg2.extensions

from infra.settings import load_postgres_settings

_ROLE_PREFIX = "oma_task_"
_VALID_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")


def _role_name(task_id: str) -> str:
    if not _VALID_TASK_ID_RE.match(task_id):
        raise ValueError(
            f"task_id {task_id!r} isn't safe to embed in a role name "
            f"(must match {_VALID_TASK_ID_RE.pattern})"
        )
    # Postgres identifiers are case-folded and length-limited; keep this
    # short and predictable rather than relying on quoting to be perfect.
    return f"{_ROLE_PREFIX}{task_id}".lower()[:63]


def _generate_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _admin_connection():
    """A connection using the app role configured in .env (currently
    `oma_admin` on the Postgres host, confirmed to hold CREATEROLE there).
    Once this points at Operator's sandbox Postgres (Phase 14), this must be
    re-verified against whatever credential is used there -- a scoped,
    non-superuser credential on a different instance may not have
    CREATEROLE, and that's a real prerequisite to check, not something
    this script can paper over.
    """
    s = load_postgres_settings()
    return psycopg2.connect(
        host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password
    )


def create_scoped_role(task_id: str, ttl_interval: str = "1 hour") -> dict:
    """Create a role scoped to exactly one task, expiring after
    ttl_interval. Returns the connection info a caller needs to actually
    use it -- never logs or returns anything that gets written to a
    shared/durable store as plain text.
    """
    role = _role_name(task_id)
    password = _generate_password()

    conn = _admin_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        # DROP first in case a previous run for this exact task_id was
        # never cleaned up -- idempotent by design, not just by luck.
        cur.execute(f'DROP ROLE IF EXISTS "{role}"')
        # VALID UNTIL needs a literal timestamp, not an expression -- so
        # compute the real expiry timestamp first, then pass it as a
        # plain string constant into CREATE ROLE.
        cur.execute("SELECT (now() + %s::interval)::text", (ttl_interval,))
        expires_at = cur.fetchone()[0]
        cur.execute(
            f'CREATE ROLE "{role}" LOGIN PASSWORD %s VALID UNTIL %s',
            (password, expires_at),
        )
        s = load_postgres_settings()
        cur.execute(f'GRANT CONNECT ON DATABASE "{s.db}" TO "{role}"')
        cur.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        # Scoped narrowly: read/write on agent_memory_events only, not
        # blanket schema privileges. Extend per-task as real specialist
        # write needs grow -- never widen this default.
        cur.execute(f'GRANT SELECT, INSERT ON agent_memory_events TO "{role}"')
        cur.execute(
            f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"'
        )
    finally:
        cur.close()
        conn.close()

    s = load_postgres_settings()
    return {
        "role": role,
        "password": password,
        "host": s.host,
        "port": s.port,
        "dbname": s.db,
        "expires_in": ttl_interval,
    }


def drop_scoped_role(task_id: str) -> None:
    """End-of-task cleanup. Explicit, not relied-upon-via-expiry: VALID
    UNTIL only stops *new* logins, so a still-open session from this role
    keeps working right up until this function actually runs.
    """
    role = _role_name(task_id)
    conn = _admin_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        s = load_postgres_settings()
        # REASSIGN/DROP OWNED must run before DROP ROLE, or the DROP ROLE
        # fails if this role owns anything at all (it shouldn't, given
        # the narrow grants above, but this makes that an invariant
        # rather than an assumption).
        cur.execute(f'REASSIGN OWNED BY "{role}" TO "{s.user}"')
        cur.execute(f'DROP OWNED BY "{role}"')
        cur.execute(f'DROP ROLE IF EXISTS "{role}"')
    finally:
        cur.close()
        conn.close()
