"""Odoo Database Manager operations (duplicate, list, drop) via the
`db` XML-RPC service -- NOT a manual pg_dump, which misses the
filestore (uploaded attachments/report files). Uses the master
password, never a regular user login. Every call here goes through
infra.odoo_settings.load_odoo_settings(), which hardcodes the
Production-instance safety guard -- this module never bypasses that.
"""

from __future__ import annotations

import xmlrpc.client

from infra.odoo_settings import load_odoo_settings


def _db_proxy() -> xmlrpc.client.ServerProxy:
    s = load_odoo_settings()  # runs the safety guard as a side effect
    return xmlrpc.client.ServerProxy(f"{s.url}/xmlrpc/2/db")


def list_databases() -> list[str]:
    """NOTE: this container has `list_db` disabled (a sensible security
    setting) -- this method's own Fault 3 "Access Denied" is how that
    surfaces. Kept here for a deployment where it IS enabled; callers
    in this project should not depend on it succeeding.
    """
    proxy = _db_proxy()
    return proxy.list()


def duplicate_database(new_db_name: str) -> None:
    """Duplicates OMA_ODOO_DB (odoo16_dev) into new_db_name using
    Odoo's own Database Manager function -- correctly includes the
    filestore, unlike a raw pg_dump/createdb.

    Does not pre-check via list_databases() (disabled on this
    deployment, see above) -- relies on duplicate_database's own error
    if new_db_name already exists.
    """
    s = load_odoo_settings()
    proxy = _db_proxy()
    proxy.duplicate_database(s.master_password, s.db, new_db_name)


def create_database(new_db_name: str, login: str = "admin", user_password: str = "admin") -> None:
    """Creates a genuinely FRESH, empty database via Odoo's own Database
    Manager `create_database` call -- distinct from duplicate_database(),
    which copies odoo16_dev's existing data. Used specifically to
    isolate whether Phase 8's InsufficientPrivilege install blocker is a
    property of duplicate_database()'s own Postgres-level table-ownership
    handling, or systemic to this Postgres instance as a whole -- if a
    module install succeeds cleanly against a database Odoo itself
    created from scratch, the duplicate path is the actual culprit; if
    it fails identically, the problem is instance-wide.

    Odoo initializes base modules itself as part of this call, so this
    genuinely takes a a few seconds -- much slower than duplicate_database(),
    which is a near-instant filesystem-level copy.
    """
    s = load_odoo_settings()
    proxy = _db_proxy()
    proxy.create_database(s.master_password, new_db_name, False, "en_US", user_password, login)


def drop_database(db_name: str) -> None:
    """Only ever call this on a duplicate this project itself created
    (a name this module generated), never on OMA_ODOO_DB itself.
    """
    if db_name == "odoo16_dev" or db_name == "16_202012":
        raise ValueError(f"Refusing to drop {db_name!r} -- this is a protected instance, not a duplicate.")
    s = load_odoo_settings()
    proxy = _db_proxy()
    proxy.drop(s.master_password, db_name)
