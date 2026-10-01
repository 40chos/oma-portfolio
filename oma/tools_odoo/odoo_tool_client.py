"""OdooToolClient -- Phase 7, rebuilt per the technical document's
revised §4 and the build plan's revised Phase 7. Replaces the earlier
mcp-server-odoo-based design entirely.

*(Why: mcp-server-odoo's Standard mode -- the one with a model-whitelist
layer -- has no Odoo 16 build and requires an Odoo.com login to obtain
one; its YOLO-mode fallback drops that whitelist layer entirely. Rather
than accept that reduction, or install unfamiliar third-party code
inside an already migration-history-uncertain Odoo instance, this
builds our own thin wrapper directly on Odoo's own standard,
long-stable External API -- xmlrpc.client, stdlib only, nothing
installed inside Odoo at all.)*

Exposes exactly five methods -- search_read, read, create, write,
unlink -- and NOTHING else. No generic execute_kw passthrough, ever;
that's a deliberate, permanent property of this class, not a
missing feature. Every call is checked against odoo_tool_allowlist.yaml
before it reaches Odoo at all -- the whitelist layer the third-party
companion module would have provided, done instead as code we own and
can read line-by-line.

The __last_update read-and-echo concurrency behavior is required and
non-optional on every read/write -- never something a caller has to
remember to ask for, per the technical document's own repeated warning
that a raw API client does not get Odoo's optimistic-concurrency
protection for free.
"""

from __future__ import annotations

import xmlrpc.client
from pathlib import Path

import yaml

from infra.odoo_settings import load_odoo_settings

_ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "odoo_tool_allowlist.yaml"

_VALID_OPERATIONS = {"read", "search_read", "write", "create", "unlink"}


class AllowlistViolationError(RuntimeError):
    """Raised when a call's (model, operation) pair isn't explicitly
    listed in odoo_tool_allowlist.yaml. Must never be caught and
    silently retried with a different model/operation -- if this
    fires, the allowlist needs a deliberate, reviewed edit, not a
    workaround.
    """


class ConcurrencyConflictError(RuntimeError):
    """Raised when a write's echoed __last_update no longer matches the
    record's current value -- someone else changed this record between
    our read and our write. This must be treated as a real, expected
    outcome the caller handles, never suppressed.
    """


def load_allowlist() -> list[dict]:
    """Read fresh from disk every call, not cached at import time --
    same living-document treatment as sensitive_paths.yaml, so an edit
    takes effect without a restart.
    """
    data = yaml.safe_load(_ALLOWLIST_PATH.read_text())
    return data.get("odoo_tool_allowlist", []) if data else []


def _check_allowlist(model: str, operation: str) -> None:
    assert operation in _VALID_OPERATIONS, f"invalid operation {operation!r}"
    rules = load_allowlist()
    for rule in rules:
        if rule.get("model") == model and operation in (rule.get("operations") or []):
            return
    raise AllowlistViolationError(
        f"REFUSED: ({model!r}, {operation!r}) is not on odoo_tool_allowlist.yaml. "
        f"This is an allow-list, not a deny-list -- add an explicit, reviewed entry "
        f"if this model/operation is genuinely needed, rather than working around this."
    )


class OdooToolClient:
    """One instance per task, typically -- construct with the task's
    own JIT-created api_key (infra.odoo_jit_apikey) and the dedicated
    user's uid, never a long-lived admin credential.
    """

    def __init__(self, uid: int, api_key: str, db_override: str | None = None):
        self._settings = load_odoo_settings(db_override=db_override)
        self._uid = uid
        self._api_key = api_key
        self._models = xmlrpc.client.ServerProxy(f"{self._settings.url}/xmlrpc/2/object")

    def _execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None):
        return self._models.execute_kw(
            self._settings.db, self._uid, self._api_key, model, method, args, kwargs or {}
        )

    def search_read(self, model: str, domain: list, fields: list[str], limit: int | None = None) -> list[dict]:
        _check_allowlist(model, "search_read")
        kwargs = {"fields": fields}
        if limit is not None:
            kwargs["limit"] = limit
        return self._execute_kw(model, "search_read", [domain], kwargs)

    def read(self, model: str, record_id: int, fields: list[str]) -> dict:
        """Every read includes __last_update in the requested fields --
        required, not optional, so a caller can never forget it before
        a subsequent write.
        """
        _check_allowlist(model, "read")
        fields_with_marker = list(fields)
        if "__last_update" not in fields_with_marker:
            fields_with_marker.append("__last_update")
        results = self._execute_kw(model, "read", [[record_id], fields_with_marker])
        if not results:
            raise ValueError(f"{model} record {record_id} not found")
        return results[0]

    def write(self, model: str, record_id: int, values: dict, expected_last_update: str) -> None:
        """Every write echoes back the __last_update value obtained from
        a prior read() call. Re-reads the record immediately before
        writing and compares -- if it's changed, raises
        ConcurrencyConflictError instead of writing, exactly the
        protection a normal Odoo web form gets automatically and a raw
        XML-RPC client does not get for free.
        """
        _check_allowlist(model, "write")
        current = self._execute_kw(model, "read", [[record_id], ["__last_update"]])
        if not current:
            raise ValueError(f"{model} record {record_id} not found")
        current_last_update = current[0]["__last_update"]
        if current_last_update != expected_last_update:
            raise ConcurrencyConflictError(
                f"{model} record {record_id} was modified since it was last read "
                f"(expected __last_update={expected_last_update!r}, "
                f"found {current_last_update!r}) -- refusing to write."
            )
        self._execute_kw(model, "write", [[record_id], values])

    def create(self, model: str, values: dict) -> int:
        _check_allowlist(model, "create")
        return self._execute_kw(model, "create", [values])

    def unlink(self, model: str, record_id: int) -> None:
        _check_allowlist(model, "unlink")
        self._execute_kw(model, "unlink", [[record_id]])
