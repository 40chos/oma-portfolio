"""Odoo connection settings, with a hardcoded, non-negotiable safety
guard -- per the project owner's explicit instruction, worth the extra half hour.

This devbox has SSH/docker access to FOUR containers on odoo-dev.int:
  odoo16-dev   -> 127.0.0.1:8071->8069  -- our dev target, ALWAYS
  odoo16-dev2  -> 127.0.0.1:8072->8069  -- a second dev/test container,
                                            not used by this project
  odoo16       -> 127.0.0.1:8070->8069  -- LIVE CORPORATE PRODUCTION.
                                            NEVER touch this, ever.
  odoo19       -> 127.0.0.1:8069->8069  -- unrelated, different version

Port 8071 is bound to 127.0.0.1 on odoo-dev.int itself -- unreachable
directly from this devbox, so an SSH local port-forward tunnels it here:
    ssh -f -N -L 18071:127.0.0.1:8071 dev-agent@odoo
OMA_ODOO_URL points at that LOCAL tunnel endpoint. The guard below does
NOT check the tunnel's local port (that's an implementation detail of
how the bytes get here) -- it checks the two things that actually
identify which real Odoo instance and database code is about to talk
to: OMA_ODOO_DB must be exactly "odoo16_dev", and the tunnel's target
remote port (OMA_ODOO_REMOTE_PORT, separate from whatever local port a
future tunnel might use) must be exactly 8071.

Deliberately an ALLOW-LIST, not a deny-list: this only lets the one
known-good target through, rather than trying to blocklist the one
known-bad value (8070 / 16_202012) and hoping nothing else bad ever
shows up. A deny-list only catches what you already thought to name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Hardcoded. Never read from .env, never overridable -- the entire
# point of this guard is to survive a future .env mistake, not to
# trust .env to get it right.
_CANONICAL_DB = "odoo16_dev"
_ALLOWED_REMOTE_PORT = 8071

# Per the build plan's own migration-risk mitigation (technical
# document §4), Phase 7 steps 3-6 (and Phase 8's install/upgrade
# testing) are required to run against a DUPLICATE of odoo16_dev, never
# the instance directly. A duplicate created via
# infra.odoo_admin.duplicate_database() is exactly as safe as
# odoo16_dev itself (it's a full copy, including filestore, of the same
# dev data) -- so the allow-list accepts it too, but ONLY by this exact,
# narrow naming pattern, never a bare prefix match that could
# accidentally admit something unintended.
_DUPLICATE_DB_RE = re.compile(r"^odoo16_dev_dup_\d{8}(_\d{6})?$")

# Phase 9.5 addition: a genuinely FRESH, empty test database created
# via Odoo's own Database Manager `create_database()` (never a raw
# createdb/pg_dump) -- used specifically to isolate whether Phase 8's
# InsufficientPrivilege install blocker is a property of duplicate_database()'s
# own table-ownership handling, or systemic to this Postgres instance as
# a whole. Same narrow, exact-pattern reasoning as the duplicate regex
# above -- never a bare prefix match.
_FRESH_TEST_DB_RE = re.compile(r"^odoo16_dev_fresh_\d{8}_\d{6}$")

_FORBIDDEN_DB = "16_202012"
_FORBIDDEN_REMOTE_PORT = 8070

# Phase 20 (§24.13) addition, 2026-07-13: the Build sandbox target --
# odoo16-dev2, port 8072, confirmed unused by this project until this
# phase, now network- and account-isolated (see the build plan's own
# §24.13.3a/§24.13.3b/§24.13.3c for the real infra hardening this target
# depends on). Deliberately a SEPARATE constant and a SEPARATE naming
# pattern, not a loosening of _ALLOWED_REMOTE_PORT/_is_allowed_db above --
# a sandbox-target call must explicitly opt in via
# assert_safe_sandbox_target() below, never fall through the main dev
# guard by accident.
_SANDBOX_REMOTE_PORT = 8072
_SANDBOX_DB_RE = re.compile(r"^odoo16_sandbox_\d{8}_\d{6}$")

# Real speed fix, 2026-07-14 (the project owner, direct request under real time
# pressure -- 100-200s/round was making the per-area rollout impractical):
# a one-time database on the sandbox container (odoo16-dev2) with Odoo's
# base module already installed. tools_odoo.module_dev.toolchain.
# create_sandbox_database()'s `template` param clones this via Postgres'
# own `createdb -T`, which measured near-instant (~1.3s) against the real
# container -- skipping the ~9s of base-module bootstrap (2985 queries)
# every fresh empty database would otherwise repeat every single round.
# Deliberately does NOT match _SANDBOX_DB_RE (no date/time suffix) -- it
# is never itself a valid per-round install target, only ever a clone
# SOURCE; assert_safe_sandbox_target() below still validates the actual
# per-round db name exactly as before. If this database is ever dropped
# or goes missing, callers should treat that as "no template available"
# and fall back to `template=None` (the original, always-correct, just
# slower, truly-empty-database path) rather than fail the round.
SANDBOX_TEMPLATE_DB = "odoo16_sandbox_golden_template"


class ProductionOdooGuardError(RuntimeError):
    """Raised when configured Odoo target values don't match an allowed
    dev target (the canonical odoo16_dev, or one of its own duplicates).
    This must never be caught and worked around -- if this fires, stop
    and fix the configuration, don't silently retry against something
    else.
    """


@dataclass(frozen=True)
class OdooSettings:
    url: str  # the LOCAL tunnel endpoint actually connected to
    db: str
    user: str
    api_key: str | None
    master_password: str


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name!r}.")
    return value


def _is_allowed_db(db: str) -> bool:
    return (
        db == _CANONICAL_DB
        or bool(_DUPLICATE_DB_RE.match(db))
        or bool(_FRESH_TEST_DB_RE.match(db))
    )


def _assert_safe_odoo_target(db: str, remote_port: int) -> None:
    if db == _FORBIDDEN_DB or remote_port == _FORBIDDEN_REMOTE_PORT:
        raise ProductionOdooGuardError(
            f"REFUSING TO PROCEED: configured Odoo target (db={db!r}, "
            f"remote_port={remote_port}) matches the FORBIDDEN Production "
            f"instance (db={_FORBIDDEN_DB!r}, port={_FORBIDDEN_REMOTE_PORT}). "
            f"This system must never touch Production, under any circumstance."
        )
    if not _is_allowed_db(db) or remote_port != _ALLOWED_REMOTE_PORT:
        raise ProductionOdooGuardError(
            f"REFUSING TO PROCEED: configured Odoo target (db={db!r}, "
            f"remote_port={remote_port}) does not match the canonical dev "
            f"target ({_CANONICAL_DB!r}), a recognized duplicate of it "
            f"(pattern: {_DUPLICATE_DB_RE.pattern}), or a recognized fresh test db "
            f"(pattern: {_FRESH_TEST_DB_RE.pattern}), on port {_ALLOWED_REMOTE_PORT}. "
            f"This is an allow-list, not a deny-list -- an unrecognized target "
            f"is refused even if it isn't the known Production value."
        )


def assert_safe_sandbox_target(db: str, remote_port: int) -> None:
    """The sandbox-target counterpart to _assert_safe_odoo_target() --
    same allow-list philosophy, deliberately separate function and
    separate pattern. Only ever accepts port 8072 (odoo16-dev2) paired
    with a genuinely fresh, this-round-only sandbox db name matching
    _SANDBOX_DB_RE -- never the canonical dev db, never a dev duplicate/
    fresh-test pattern (those are §24.13's main-dev identity, not the
    sandbox's), and never anything on the main dev port (8071) or the
    forbidden production port (8070). A caller that means to reach the
    real dev target must go on calling _assert_safe_odoo_target()/
    load_odoo_settings() as before -- this function is exclusively for
    the Build sandbox pre-flight path (specialists/build/specialist.py).
    """
    if db == _FORBIDDEN_DB or remote_port == _FORBIDDEN_REMOTE_PORT:
        raise ProductionOdooGuardError(
            f"REFUSING TO PROCEED: configured sandbox target (db={db!r}, "
            f"remote_port={remote_port}) matches the FORBIDDEN Production "
            f"instance. This system must never touch Production, under any circumstance."
        )
    if remote_port != _SANDBOX_REMOTE_PORT or not _SANDBOX_DB_RE.match(db):
        raise ProductionOdooGuardError(
            f"REFUSING TO PROCEED: configured sandbox target (db={db!r}, "
            f"remote_port={remote_port}) does not match the recognized sandbox "
            f"target (port {_SANDBOX_REMOTE_PORT}, db pattern {_SANDBOX_DB_RE.pattern}). "
            f"This is an allow-list, not a deny-list -- an unrecognized target is "
            f"refused even if it isn't the known Production value."
        )


def new_sandbox_db_name() -> str:
    """Generates a fresh sandbox db name matching _SANDBOX_DB_RE, in the
    same style as Phase 9.5's own fresh-test-db naming
    (odoo16_dev_fresh_<date>_<time>) -- one per Build sandbox pre-flight
    attempt, never reused across rounds (§24.13.4b: the sandbox is fully
    stateless/disposable every round).
    """
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    return f"odoo16_sandbox_{now:%Y%m%d}_{now:%H%M%S}"


def load_odoo_settings(remote_port: int = _ALLOWED_REMOTE_PORT, db_override: str | None = None) -> OdooSettings:
    """remote_port defaults to the one allowed value -- a caller would
    have to go out of their way to override it, and doing so still runs
    through the same assertion. db_override lets Phase 7 steps 3-6 (and
    Phase 8) point at a duplicate database instead of OMA_ODOO_DB
    itself -- still runs through the exact same guard, which only
    accepts the canonical name or a recognized duplicate-naming pattern.
    """
    db = db_override or _require("OMA_ODOO_DB")
    _assert_safe_odoo_target(db, remote_port)

    return OdooSettings(
        url=_require("OMA_ODOO_URL"),
        db=db,
        user=os.environ.get("OMA_ODOO_USER") or "",
        api_key=os.environ.get("OMA_ODOO_API_KEY") or None,
        master_password=_require("OMA_ODOO_MASTER_PASSWORD"),
    )
