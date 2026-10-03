"""Fast, XML-RPC-based replacements for toolchain.py's read-only
registry-lookup functions (Phase 21, 2026-07-22).

Real, measured root cause this replaces: get_model_fields(),
list_custom_models(), and get_primary_form_view_xmlid() each boot a
FRESH `odoo-bin shell` process (full ORM registry + every addon
loading from scratch) just to answer one simple metadata question --
confirmed live at 8.83s, 9.66s, and 20.30s for three sequential calls,
the single largest cost anywhere in the pipeline (bigger than every
LLM call combined). The Odoo web server for this exact database is
already running, with its registry already warm -- XML-RPC against it
answers the same questions in well under a second (confirmed live:
0.74s cold, 0.34s once the connection is warm).

Uses the SAME JIT-credential mechanism already built and tested for
this exact purpose (infra.odoo_jit_apikey) and the SAME production-
safety allow-list (infra.odoo_settings) every other Odoo-facing tool
in this codebase already goes through -- but caches the (uid, key)
pair per DATABASE, not per task, deliberately different from
OdooToolClient's own per-task scoping.

Real, confirmed regression found live (2026-07-22), the same night
this module was built: caching per (db, task_id) instead means EVERY
distinct task pays a full ~8-26s JIT-key mint (create_task_api_key()
itself goes through the odoo-bin shell channel -- infra.odoo_jit_apikey's
own documented reason: generating a key on someone else's behalf isn't
a plain XML-RPC operation in stock Odoo) on its FIRST metadata query,
with zero reuse across tasks. tests/test_build_specialist.py -- 8
tests, each constructing its own fresh contract/task_id -- went from
passing to 3 genuine timeouts under that design (confirmed: the
slowest, test_fencing_rejects_stale_caller_after_real_specialist_reacquires,
passed cleanly in isolation at 113.45s once given enough time, proving
this was pure added latency, not a logic bug). For a single real task
making several registry queries the per-task design is still a net
win over the original odoo-bin-shell cost, but it reintroduces almost
the SAME "boot a fresh, expensive credential path" tax at task
granularity instead of call granularity -- not the fix this was meant
to be.

Fixed: ONE shared JIT key per database, minted once per process and
reused for the rest of that process's lifetime (module-level dict,
keyed by db only) -- every metadata query across every task now pays
the ~8-26s mint cost AT MOST ONCE per running oma-chat-ui.service
process, not once per task. This is a deliberate, justified trade-off:
these are exclusively READ-ONLY metadata queries (fields_get,
search_read/read/search_count against ir.model.data/ir.model) with NO
data-write capability at all, so a longer-lived shared key carries
much lower risk than OdooToolClient's per-task scoping (which fronts
real data writes and correctly stays narrowly scoped). Named
`oma-schema-client-shared` (stable across restarts) rather than
`oma-task-{task_id}` -- identifiable in Odoo's own UI as this one
long-lived purpose, not confusable with a stray per-task leak.

Known gap, deliberately not closed tonight (flagged, not hidden): this
shared key is never revoked -- it is meant to live for the service
process's lifetime, but there's no explicit cleanup on process
shutdown either. On a non-production, allow-list-guarded dev instance
this is a low, accepted risk, not a security cliff; a proper fix would
mint it once at service startup and revoke it in a shutdown hook.
"""

from __future__ import annotations

import re
import threading
import xmlrpc.client

from infra.odoo_jit_apikey import create_task_api_key
from infra.odoo_settings import load_odoo_settings

# Real, confirmed bug found live (2026-08-13 overnight Phase 35 Tier-0/Context-row bake-in, 2
# real occurrences the same night -- once during a manual backfill run, once from the live
# service itself while processing an unrelated real task): every `xmlrpc.client.ServerProxy` in
# this file was constructed with NO timeout at all -- confirmed directly via `/proc/<pid>/wchan`
# showing a real process stuck in `do_poll` (a genuine, indefinite socket wait, not CPU work) for
# 10+ minutes on what should be a sub-second metadata call. Since this service is single-worker
# and blocking (see manager/loop.py's own documented "/api/message blocks until the round batch
# resolves" design), one hung XML-RPC call here can freeze the ENTIRE service for every other
# concurrent request, not just the one task that triggered it. `xmlrpc.client.ServerProxy` has no
# `timeout=` constructor parameter of its own -- the standard, correct fix is a custom
# `Transport` subclass that sets a socket-level timeout on the underlying HTTP connection.
_XMLRPC_TIMEOUT_SECONDS = 30


class _TimeoutTransport(xmlrpc.client.Transport):
    """A plain `xmlrpc.client.Transport` with a real socket timeout -- the standard-library
    idiom for this, since `ServerProxy` itself has no `timeout=` kwarg. Used for every
    `ServerProxy` in this file so a slow/unresponsive Odoo instance degrades to a clear,
    catchable `TimeoutError` instead of hanging the single-worker service indefinitely.
    """

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = _XMLRPC_TIMEOUT_SECONDS
        return conn


def _timeout_transport() -> _TimeoutTransport:
    return _TimeoutTransport()
from tools_odoo.codebase_read import CodebaseReadError, read_module_files

_SHARED_KEY_NAME = "oma-schema-client-shared"

_shared_key_cache: dict[str, tuple[int, str]] = {}
# Real, live-reproduced bug (Phase 26A follow-up, 2026-07-27, task 004):
# every caller here runs via `asyncio.to_thread()` (real OS threads, not
# coroutines cooperatively yielding), and Phase 18's best-of-N runs 2-3
# candidates concurrently for any computed/onchange field -- so 2-3
# threads can all see `_shared_key_cache.get(db)` return None at once on
# a cold cache and each independently kick off `create_task_api_key()`,
# an 8-20s odoo-bin-shell registry boot. Confirmed live: a real
# `_inherit = 'project.fieldjob'` target that DOES genuinely exist
# (independently confirmed via a direct, isolated `get_model_fields_fast()`
# call returning 49 real fields) was reported as "does not exist as a
# real model anywhere" on round 1 of a best-of-N round -- consistent
# with one of the racing threads' RPC calls failing/timing out under
# the thundering-herd load and `_read_real_field_rows()`'s own broad
# `except Exception: return None` silently swallowing that as a
# nonexistent-model result. A per-db lock serializes the cold-cache
# path so only the first thread ever pays the creation cost; every
# other thread blocks briefly then reuses the now-cached key instead
# of racing its own redundant (and here, apparently failure-prone)
# shell-script creation.
_shared_key_lock = threading.Lock()

# Real, confirmed false-negative bug found live (2026-07-22), BEFORE
# this whole module was ever deployed to production: every function
# here defaults to login="Admin" (confirmed to exist on odoo16_dev,
# the one real dev target this was benchmarked against) -- but a
# fresh/duplicate/sandbox test database is NOT guaranteed to have any
# user named "Admin" at all (confirmed live:
# `RuntimeError("user 'Admin' not found in db 'odoo16_dev_fresh_...'")`
# from the JIT-key mint itself), unlike the original odoo-bin-shell
# functions this replaces, which run in a superuser context needing
# NO login and work identically on any database. Every caller
# throughout the codebase that gates on `task_id` to pick this fast
# path MUST also gate on `is_fast_path_eligible(db)` -- a single,
# explicit allow-list (same philosophy as infra.odoo_settings' own
# production-safety guard: allow-list, never a deny-list) rather than
# trying to make every individual call site robust to an auth failure
# it may not even know how to detect. Only odoo16_dev is listed --
# the one database confirmed, live, to have the Admin account these
# functions authenticate as; a fresh/duplicate/sandbox target always
# falls back to the original, universal, login-free mechanism.
_FAST_PATH_ELIGIBLE_DBS = frozenset({"odoo16_dev"})

# Phase 26A audit (2026-07-27): every function in this file was
# individually checked for the same "privileged-identity ACL
# postprocessing" risk class that caused the fields_get()-omission bug
# fixed above (_read_real_field_rows / check_field_group_restricted_fast)
# and the earlier button-groups bug (Phase 25F, check_button_group_
# restricted_fast). Confirmed NOT at risk, and why: list_custom_models_
# fast, list_module_models_fast, resolve_model_owner_module_fast,
# resolve_xmlids_exist_fast, resolve_owning_modules_fast, resolve_model_
# names_from_xmlids_fast all read ONLY ir.model / ir.model.data rows --
# these describe MODEL/xmlid metadata, not a specific model's own
# field-level data-access surface, and are not subject to the same
# per-field groups= runtime filtering. check_group_exists_fast and
# check_group_model_access_fast read res.groups / ir.model.access --
# security-configuration rows themselves, always readable at the
# permission level these JIT keys operate at. get_module_state_fast
# reads ir.module.module -- module install-state, not gated by any
# model's own field ACL. check_button_group_restricted_fast was already
# fixed in Phase 25F (reads ir.ui.view.read_combined(), not the
# ACL-postprocessed get_views() arch). The one function with a
# plausible but NOT YET LIVE-CONFIRMED variant risk is
# _get_primary_view_xmlid_fast -- see its own comment, below, for why
# it was flagged but deliberately not "fixed" without live reproduction
# first.


def is_fast_path_eligible(db: str) -> bool:
    return db in _FAST_PATH_ELIGIBLE_DBS


_REDIS_SHARED_KEY_TEMPLATE = "oma:schema_client:shared_key:{db}"
# Real, honest 30-day figure, not an active expiry mechanism -- res.users.apikeys rows never
# expire on Odoo's own side (this whole module's sibling, infra/odoo_jit_apikey.py, says so
# directly in its own header). Purely a staleness safety net (e.g. if the underlying Admin user
# were ever recreated) so a genuinely dead cached credential can't wedge this path forever;
# refreshed on every real read below, so an actively-used key never actually expires in practice.
_REDIS_SHARED_KEY_TTL_SECONDS = 30 * 24 * 3600


def _get_or_create_shared_key(db: str, login: str) -> tuple[int, str]:
    """Real fix, 2026-08-08 (the project owner's own explicit, emphatic request to optimize Build's real
    non-LLM overhead "to the maximum... one second, not ten"). Root cause, confirmed live via
    direct trace-history inspection: `_shared_key_cache` (in-memory only, defined above) is
    wiped on every process restart, and the ONLY way to refill it is `create_task_api_key()` --
    a real, cold `odoo-bin shell` registry boot (8-20s, matching the exact 9.6-14.0s this
    session's own several restarts each triggered live). `_SHARED_KEY_NAME` is a single, fixed,
    intentionally-reused name (see its own comment above) -- Odoo API keys never expire on their
    own (infra/odoo_jit_apikey.py's own header) -- so there was never a real reason this had to
    be re-minted on every restart; it just never had anywhere durable to live. Persisted to
    Redis now (the same store already used throughout this codebase for cross-process state),
    checked BEFORE paying the cold-mint cost -- a restart no longer pays this cost at all once a
    key has been minted once, ever, matching the "genuinely once per credential's real lifetime,
    not once per process" semantics `create_task_api_key()`'s own real, no-native-expiry Odoo
    behavior already implies. Bonus, real fix to a separate, related leak found while reading
    this code: `_generate()` (infra/odoo_jit_apikey.py) has no idempotency check of its own --
    every previous restart-triggered re-mint left the PRIOR "oma-schema-client-shared" key row
    orphaned in `res.users.apikeys` forever (no revoke path exists for this specific shared key,
    unlike the per-task keys elsewhere that ARE explicitly revoked at task end) -- reusing one
    persisted key across restarts stops this accumulation too, not just the speed cost.
    """
    cached = _shared_key_cache.get(db)
    if cached is not None:
        return cached
    with _shared_key_lock:
        # Re-check now that we hold the lock -- another thread may have
        # already populated the cache while we were waiting.
        cached = _shared_key_cache.get(db)
        if cached is not None:
            return cached
        redis_key = _REDIS_SHARED_KEY_TEMPLATE.format(db=db)
        try:
            from infra.redis_client import get_redis_client
            r = get_redis_client()
            raw = r.get(redis_key)
        except Exception:
            r = None
            raw = None
        if raw:
            import json
            try:
                uid, plaintext_key = json.loads(raw)
                # Real credential still needs a live check -- a persisted key could be stale if
                # the underlying Admin user/db was ever rebuilt out from under it; never trust
                # a cached credential blindly just because Redis still has it.
                settings = load_odoo_settings(db_override=db)
                common = xmlrpc.client.ServerProxy(f"{settings.url}/xmlrpc/2/common", transport=_timeout_transport())
                if common.authenticate(db, login, plaintext_key, {}):
                    _shared_key_cache[db] = (uid, plaintext_key)
                    if r is not None:
                        r.expire(redis_key, _REDIS_SHARED_KEY_TTL_SECONDS)  # refresh TTL on real use
                    return uid, plaintext_key
            except Exception:
                pass  # fall through to a fresh cold mint below -- same conservative posture as every other cache-miss path in this file
        _row_id, plaintext_key = create_task_api_key(db, login, _SHARED_KEY_NAME)
        settings = load_odoo_settings(db_override=db)
        common = xmlrpc.client.ServerProxy(f"{settings.url}/xmlrpc/2/common", transport=_timeout_transport())
        uid = common.authenticate(db, login, plaintext_key, {})
        if not uid:
            raise RuntimeError(f"JIT key created for {login!r} but XML-RPC authentication failed")
        _shared_key_cache[db] = (uid, plaintext_key)
        if r is not None:
            import json
            try:
                r.set(redis_key, json.dumps([uid, plaintext_key]), ex=_REDIS_SHARED_KEY_TTL_SECONDS)
            except Exception:
                pass  # durable persistence is a real speed optimization, never a correctness requirement -- the in-memory cache above already makes this call correct on its own
        return uid, plaintext_key


def _models_proxy(db: str) -> xmlrpc.client.ServerProxy:
    settings = load_odoo_settings(db_override=db)
    return xmlrpc.client.ServerProxy(f"{settings.url}/xmlrpc/2/object", transport=_timeout_transport())


def _read_real_field_rows(model_name: str, db: str, login: str) -> list[dict] | None:
    """Phase 26A (2026-07-27): the shared, identity-independent field
    reader every existence/metadata check in this file should route
    through -- reads directly from `ir.model.fields` (real Postgres
    rows describing field METADATA) instead of calling `fields_get()`
    against the target model itself (which returns the model's own
    DATA-ACCESS surface, and is silently ACL-filtered for it).

    Real, confirmed bug this replaces (Phase 26A audit + live
    reproduction, 2026-07-27): Odoo's real `fields_get()` OMITS a field
    ENTIRELY -- not just its `groups` attribute, the whole field -- the
    moment the calling user (every function in this file defaults to
    `login="Admin"`) lacks the group that field's own `groups=` kwarg
    requires. Confirmed live with a real, disposable test module
    (`oma_phase26a_test_fixture`, installed then removed): a field
    declared `fields.Char(groups="mis_base_extend.group_user_developer_
    access_fields")`, a real group Admin genuinely does not hold,
    vanished completely from `fields_get()`'s own returned dict -- even
    with the warm worker freshly restarted to rule out any caching
    explanation. `ir.model.fields.search_read()` against the SAME field,
    same call, correctly returned the row regardless -- `ir.model.fields`
    rows are field DEFINITIONS (Postgres data), not filtered by the
    TARGET model's own runtime field-level ACL the way `fields_get()`'s
    RPC call is.

    Returns None only when the model itself doesn't exist / nothing
    could be read at all -- an empty list (a model that genuinely has
    zero matching rows for some other filtered query) is a different,
    valid, non-None result for callers to handle themselves.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        rows = models.execute_kw(
            db, uid, key, "ir.model.fields", "search_read",
            [[("model", "=", model_name)]],
            # "selection" added 2026-08-06 (Phase 30, category-wide info-gap sweep): confirmed
            # live this was the SAME gap class as TransientModel/mail.template exposure -- a real,
            # already-queryable fact (a Selection field's real, valid option keys, e.g.
            # project.fieldjob.state -> draft/sent/accepted/rejected/invoiced/done) that no caller
            # of this shared reader had ever requested, so no schema-grounding block could ever
            # show it, no matter how many other callers routed through here.
            {"fields": ["name", "ttype", "relation", "required", "readonly", "selection", "modules"]},
        )
        return rows
    except Exception:
        return None


def get_model_fields_fast(model_name: str, db: str, login: str = "Admin") -> list[str] | None:
    """Drop-in replacement for toolchain.get_model_fields() -- same
    return contract (sorted field-name list, or None if the model
    doesn't exist or anything went wrong).

    Phase 26A (2026-07-27): rewritten to read via `_read_real_field_rows()`
    (ir.model.fields) instead of `fields_get()` directly -- see that
    function's own docstring for the real, live-confirmed omission bug
    this closes. A model with genuinely zero fields is not a real
    Odoo shape (every model has at least `id`), so an empty row list
    here is treated the same as None, matching this function's own
    original contract.
    """
    rows = _read_real_field_rows(model_name, db, login)
    if not rows:
        return None
    return sorted(row["name"] for row in rows)


def list_custom_models_fast(db: str, login: str = "Admin") -> list[tuple[str, str]] | None:
    """Drop-in replacement for toolchain.list_custom_models() -- same
    filtering semantics (a model is "ours" only if no non-oma_ module
    also owns an ir.model.data row for it), expressed as standard
    search_read/read calls instead of a hand-written ORM script.

    Real, confirmed N+1 performance bug found live (2026-07-30): the
    original version issued 1 + 2*N sequential XML-RPC round-trips (a
    search_count AND a separate ir.model.read call PER oma_*-owned
    model row) -- fine when this shared dev database had only a
    handful of custom models, but this function's own cost scales
    directly with how many real oma_* models have EVER been created
    across this system's whole history (hundreds, after 748+ real
    historical tasks), not with anything about the CURRENT call.
    Confirmed live: a single real call to this function, via
    _validate_inherit_target_resolved(), measured 196.9s -- the actual,
    dominant cause of Build's own real slowdown, not a GPU/model issue
    at all. Fixed to 3 total round-trips regardless of N: one batched
    search_read for every candidate row's own res_id, one batched
    search_read for ALL non-oma_ owners of any of those res_ids at
    once (grouped in Python, replacing N separate search_count calls),
    and one batched ir.model.read for every res_id that survives the
    filter (replacing N separate single-row read calls) -- identical
    filtering semantics, verified to return the exact same real result
    shape, just without the N+1 pattern.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        recs = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("module", "like", "oma_"), ("model", "=", "ir.model")]],
            {"fields": ["res_id", "module"]},
        )
        if not recs:
            return None
        res_ids = [rec["res_id"] for rec in recs]
        other_owner_rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("model", "=", "ir.model"), ("res_id", "in", res_ids), ("module", "not like", "oma_")]],
            {"fields": ["res_id"]},
        )
        res_ids_with_other_owner = {row["res_id"] for row in other_owner_rows}
        eligible_res_ids = [rid for rid in res_ids if rid not in res_ids_with_other_owner]
        if not eligible_res_ids:
            return None
        model_rows = models.execute_kw(db, uid, key, "ir.model", "read", [eligible_res_ids, ["model"]])
        model_name_by_res_id = {row["id"]: row["model"] for row in model_rows}
        out: list[tuple[str, str]] = []
        for rec in recs:
            model_name = model_name_by_res_id.get(rec["res_id"])
            if model_name:
                out.append((model_name, rec["module"]))
        return out or None
    except Exception:
        return None


def list_module_models_fast(module_name: str, db: str, login: str = "Admin") -> list[str] | None:
    """Real gap found live (2026-07-23): _validate_inherit_target_resolved()
    in specialists/build/specialist.py only ever suggests models owned by
    OUR OWN oma_*-prefixed scaffolds (via list_custom_models_fast()'s hard
    `module LIKE 'oma_'` filter) when Build invents a nonexistent `_inherit`
    target. That's blind to real, pre-existing SITE customer modules (e.g.
    `project_fieldjob`, defining `project.fieldjob`) even when a task is
    KNOWN to be extending one via a `depends_on_module:` marker -- Build
    still has to guess the real model's dotted name from the module name
    alone, and got it wrong live (`project_fieldjob.project_fieldjob`
    instead of the real `project.fieldjob`). This gives the validation
    check a way to list a SPECIFIC named module's own real models, so that
    guess can be corrected with a concrete suggestion instead of a bare
    rejection.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        recs = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("module", "=", module_name), ("model", "=", "ir.model")]],
            {"fields": ["res_id"]},
        )
        if not recs:
            return None
        res_ids = [rec["res_id"] for rec in recs]
        model_rows = models.execute_kw(db, uid, key, "ir.model", "read", [res_ids, ["model"]])
        return sorted(row["model"] for row in model_rows) or None
    except Exception:
        return None


def resolve_model_owner_module_fast(model_name: str, db: str, login: str = "Admin") -> str | None:
    """Real gap found live (2026-07-24, task 020): the naive heuristic
    used elsewhere (a model's own dotted prefix == its real addon name,
    e.g. `project.project` -> `project`) is WRONG for the very common
    shape of a genuinely custom model whose module name doesn't match
    its own dotted prefix at all (`project.fieldjob` is owned by
    `project_fieldjob`, not `project`) -- and unlike
    `list_module_models_fast`, this doesn't require already knowing
    which module to ask about. Does the reverse lookup instead: given a
    model NAME, find its own `ir.model` row, then any `ir.model.data`
    row recording which module(s) own that `ir.model` record. Several
    modules can have such a row for the same model (every `_inherit`-
    only extension gets one too, not just the original `_name`
    definer) -- deliberately picks any non-`oma_*` owner over an
    `oma_*` one when both exist (a real, deployed module is a far more
    useful "add this to your depends" answer than one of our own
    scaffolds), and alphabetically-first among ties for determinism.
    None on any uncertainty, same conservative posture as every sibling
    lookup here.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        model_rows = models.execute_kw(
            db, uid, key, "ir.model", "search_read", [[("model", "=", model_name)]], {"fields": ["id"]},
        )
        if not model_rows:
            return None
        res_id = model_rows[0]["id"]
        data_rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("model", "=", "ir.model"), ("res_id", "=", res_id)]], {"fields": ["module"]},
        )
        owner_modules = sorted({r["module"] for r in data_rows})
        if not owner_modules:
            return None
        non_oma = [m for m in owner_modules if not m.startswith("oma_")]
        return non_oma[0] if non_oma else owner_modules[0]
    except Exception:
        return None


def get_primary_form_view_xmlid_fast(model_name: str, db: str, login: str = "Admin") -> str | None:
    """Drop-in replacement for toolchain.get_primary_form_view_xmlid()
    -- get_views() to resolve the model's real primary form, exactly
    like the shell version, then a direct ir.model.data search_read
    for its xmlid rather than calling get_external_id() directly.

    Real, confirmed bug found live (2026-07-22): Odoo's own base
    controller (addons/base/controllers/rpc.py) cannot serialize
    get_external_id()'s return value over XML-RPC at all --
    it returns an int-keyed dict ({view_id: 'module.xmlid'}), and
    Python's xmlrpc.client server-side dumper raises `TypeError:
    dictionary key must be string` unconditionally, a genuine
    server-side limitation, not a client-side mistake. ir.model.data's
    own search_read (string-keyed rows, exactly what get_external_id()
    reads from internally) sidesteps this entirely.
    """
    return _get_primary_view_xmlid_fast(model_name, db, "form", login)


def get_primary_search_view_xmlid_fast(model_name: str, db: str, login: str = "Admin") -> str | None:
    """Fast-path sibling of get_primary_form_view_xmlid_fast()/get_primary_tree_view_xmlid_fast()
    above, for the search view -- Phase 34 (2026-08-11): the quick-filter deterministic builder's
    own base-view lookup. Same XML-RPC mechanism, same safety contract (None on any uncertainty).
    """
    return _get_primary_view_xmlid_fast(model_name, db, "search", login)


def get_primary_tree_view_xmlid_fast(model_name: str, db: str, login: str = "Admin") -> str | None:
    """Fast-path sibling of get_primary_form_view_xmlid_fast() above,
    for the tree/list view (2026-07-23, SITE fix-pass audit) -- see
    toolchain.get_primary_tree_view_xmlid()'s own docstring for why
    this generalization exists. Same XML-RPC mechanism, same safety
    contract (None on any uncertainty).
    """
    return _get_primary_view_xmlid_fast(model_name, db, "tree", login)


def _get_primary_view_xmlid_fast(model_name: str, db: str, view_type: str, login: str) -> str | None:
    # Phase 26A audit (2026-07-27): this calls get_views(), the SAME RPC
    # method already confirmed (Phase 25F) to postprocess its own result
    # for the calling user's real permissions -- but the risk here is a
    # DIFFERENT variant, not yet confirmed live: a view's own `groups_id`
    # (view-level visibility, distinct from a field's `groups=` kwarg)
    # could in principle cause get_views() to skip the intended primary
    # view for a caller lacking that group and resolve a different one
    # instead. Deliberately NOT fixed in this pass -- unlike the
    # button-groups case (Phase 25F) and the field-existence/field-
    # groups case (this same phase, above), this has no live
    # reproduction yet, and this project's own discipline is to never
    # guess a fix without first confirming the failure live. Flagged
    # here so the next person auditing this file doesn't have to
    # re-derive it from scratch; a real fix (if this ever reproduces
    # live) would likely mean resolving the view id a different way,
    # e.g. reading `ir.ui.view` directly rather than trusting
    # get_views()'s own view-selection logic.
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        res = models.execute_kw(db, uid, key, model_name, "get_views", [[[False, view_type]], {}])
        view_id = (res.get("views") or {}).get(view_type, {}).get("id")
        if not view_id:
            return None
        data_rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("model", "=", "ir.ui.view"), ("res_id", "=", view_id)]],
            {"fields": ["module", "name"], "limit": 1},
        )
        if not data_rows:
            return None
        return f"{data_rows[0]['module']}.{data_rows[0]['name']}"
    except Exception:
        return None


def list_model_view_xmlids_fast(model_name: str, db: str, login: str = "Admin") -> list[tuple[str, str]] | None:
    """Real gap found live (2026-08-06, task038 of the SITE 50-task fix-pass, following up on
    Category 2 of the same night's fix-pass): `external_id_exists_fast()` below can only ever
    confirm/deny ONE specific guessed xmlid AFTER Build has already committed to it -- there was
    no equivalent to `resolve_current_schema_block()` (tools_odoo/schema_grounding.py) for VIEWS,
    so when a scoped edit needs to `inherit_id`/xpath against an existing module's own real view,
    Build has nothing to draw a correct guess from and just invents a plausible-sounding-but-
    nonexistent id (confirmed live: task038 guessed `sale_room_management.view_sale_room_line_
    form` identically across 2 rounds even after `_validate_references_resolve_against_real_target`
    correctly rejected it both times -- the validator worked exactly as designed, but had nothing
    better to offer as a correction). This is the missing "what real views exist for this model"
    listing that fix was flagged as needing.

    Returns every real view for `model_name` as `[(xmlid, view_type), ...]`, sorted by xmlid.
    Unlike `_get_primary_view_xmlid_fast()` (single primary form/tree view only), this returns
    ALL of them -- search views, kanban views, secondary inherited views, everything a real xpath/
    inherit_id target could plausibly need. None on any failure/uncertainty, same conservative
    contract as every other `_fast` function in this file.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        view_rows = models.execute_kw(
            db, uid, key, "ir.ui.view", "search_read",
            [[("model", "=", model_name)]], {"fields": ["id", "type"]},
        )
        if not view_rows:
            return None
        view_type_by_id = {row["id"]: row["type"] for row in view_rows}
        data_rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("model", "=", "ir.ui.view"), ("res_id", "in", list(view_type_by_id.keys()))]],
            {"fields": ["module", "name", "res_id"]},
        )
        out = [
            (f"{row['module']}.{row['name']}", view_type_by_id.get(row["res_id"], "?"))
            for row in data_rows
        ]
        return sorted(out) or None
    except Exception:
        return None


def get_model_form_view_arch_fast(model_name: str, db: str, login: str = "Admin") -> str | None:
    """Real, confirmed gap found live (2026-08-17, overnight `workflow_with_custom_buttons_or_
    cron` certification run): `list_model_view_xmlids_fast()` above tells Build WHICH real
    views exist for a model, but nothing tells it what's actually INSIDE the one it's about to
    inherit -- so a goal asking to add a button ends up guessing whether a `<header>` element
    exists, or whether a specific existing button name (used as an xpath anchor) is really
    there. Confirmed live: 4 of 5 real button-adding attempts that night guessed wrong about
    the target parent view's own structure (`<header>` assumed present when it wasn't; existing
    button names like `toggle_active`/`action_done` assumed present when they weren't),
    correctly caught by Code-Review's own xpath-resolution check every time, but only after a
    wasted round.

    Uses the exact same technique already proven live and safe in
    `specialists/testing_qa/ui_action_presence_check.py`/`manager/loop.py`'s post-completion
    checks: `fields_view_get(view_type="form")` returns Odoo's own FINAL, already-inheritance-
    resolved arch for the model's real form view -- the authoritative answer to "what does this
    view actually contain right now," not a guess. Returns the raw arch XML string, or None on
    any failure/uncertainty (model doesn't exist, no form view, RPC error) -- same conservative
    contract as every other `_fast` function in this file. Deliberately does NOT parse/summarize
    the XML here -- that's `schema_grounding.py`'s job, matching this file's existing division of
    labor (this module speaks live Odoo RPC; schema_grounding.py speaks prompt-ready text).
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        # `args[0]` must be a (possibly empty) record-ids list, never a bare `[]` -- see the
        # `execute_kw` dispatch bug this exact call shape already had to fix once, live,
        # documented in manager/loop.py's own `run_phase32_post_completion_checks()`.
        view_data = models.execute_kw(db, uid, key, model_name, "fields_view_get", [[]], {"view_type": "form"})
        arch = view_data.get("arch") if isinstance(view_data, dict) else None
        return arch or None
    except Exception:
        return None


def get_model_is_transient_fast(model_name: str, db: str, login: str = "Admin") -> bool | None:
    """Real, confirmed gap found live (2026-08-06, Phase 30 Step 0 capability test, task049):
    neither the local model nor Sonnet-5 has ever had access to whether a target model is a
    `models.TransientModel` (a wizard) versus a regular persisted `models.Model` -- `grep -n
    "TransientModel" tools_odoo/odoo_schema_client.py` returned zero matches before this function
    was added. Confirmed live: `payment.export.wizard` (task049) and a real existing wizard-shaped
    model targeted by task034 (`payment.term.cust`'s own `_inherit` guess) are both genuinely
    TransientModel, and both tasks crashed install with `TypeError: ... transforms the transient
    model '<x>' into a non-transient model` after Build correctly resolved the right model NAME
    but had no way to know its real persistence kind. `ir.model.transient` is real, live Postgres
    metadata (confirmed: `payment.export.wizard` -> True, `project.project`/`mail.activity` ->
    False) -- this simply exposes it, mirroring every other `_fast` lookup's conservative
    None-on-any-failure contract.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        rows = models.execute_kw(
            db, uid, key, "ir.model", "search_read",
            [[("model", "=", model_name)]], {"fields": ["transient"]},
        )
        if not rows:
            return None
        return bool(rows[0]["transient"])
    except Exception:
        return None


def list_model_mail_templates_fast(model_name: str, db: str, login: str = "Admin") -> list[tuple[str, str]] | None:
    """Real, confirmed gap found live (2026-08-06, Phase 30 root-cause pass on task030): the
    mail.template sibling of `list_model_view_xmlids_fast()` above -- Build had no listing of a
    model's real, existing mail.template records, so when a goal names one by its human-readable
    label (task030's own real manager request: "pre-loaded with our 09.A template"), Build
    fabricates a plausible-sounding-but-nonexistent external id (confirmed live:
    `self.env.ref('your_module.email_template_customer')`, which does not exist -- the real
    template is `project_fieldjob`-owned, id 157, named "09.A — Fieldjob versturen", genuinely
    installed and queryable). Same missing-grounding shape as the view-xmlid gap, just for
    templates instead of views.

    Returns every real mail.template for `model_name` as `[(xmlid, name), ...]`, sorted by xmlid.
    None on any failure/no rows, same conservative contract as every other `_fast` lookup here.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        template_rows = models.execute_kw(
            db, uid, key, "mail.template", "search_read",
            [[("model", "=", model_name)]], {"fields": ["id", "name"]},
        )
        if not template_rows:
            return None
        name_by_id = {row["id"]: row["name"] for row in template_rows}
        data_rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("model", "=", "mail.template"), ("res_id", "in", list(name_by_id.keys()))]],
            {"fields": ["module", "name", "res_id"]},
        )
        out = [
            (f"{row['module']}.{row['name']}", name_by_id.get(row["res_id"], "?"))
            for row in data_rows
        ]
        return sorted(out) or None
    except Exception:
        return None


def external_id_exists_fast(xmlid: str, db: str, login: str = "Admin") -> bool | None:
    """P11 addition (2026-07-31, found via P7 Tier 3, PHASE30_P11_ADDITIONS_FROM_P7_TIER3
    _2026-07-31.md item #1): does a real `ref="module.name"` value this
    project's own generated XML uses actually exist as a real
    `ir.model.data` row? Confirmed live, 5 separate real Tier 3 tasks:
    the builder repeatedly hallucinates plausible-sounding external ids
    (`base.view_res_partner_tree` instead of the real `base.view_
    partner_tree`) with no existing check catching it before a real
    sandbox install crash. Returns None (never guesses) if `xmlid`
    isn't in `module.name` form or the real answer can't be confidently
    fetched -- same conservative contract as every other `_fast`
    function in this file.
    """
    if "." not in xmlid:
        return None
    module, name = xmlid.split(".", 1)
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("module", "=", module), ("name", "=", name)]],
            {"fields": ["id"], "limit": 1},
        )
        return bool(rows)
    except Exception:
        return None


def get_view_arch_by_xmlid_fast(xmlid: str, db: str, login: str = "Admin") -> str | None:
    """P11 addition (2026-07-31, found via P7 Tier 3, same document as
    external_id_exists_fast() above): the real, live `arch` XML text for
    a view referenced by external id (e.g. the `inherit_id` a generated
    view inherits from) -- lets a caller check whether a generated
    `<xpath expr="...">` could ever resolve against the REAL parent
    view, not just a plausible-sounding guess. Confirmed live: the same
    exact hallucinated xpath (`//group[@name='contact']` against `base.
    view_partner_form`) was independently generated on two separate
    real tasks, neither one caught before a real sandbox install crash.
    Returns None (never guesses) if the xmlid can't be resolved to a
    real view or its arch can't be confidently fetched.
    """
    if "." not in xmlid:
        return None
    module, name = xmlid.split(".", 1)
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        data_rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("module", "=", module), ("name", "=", name), ("model", "=", "ir.ui.view")]],
            {"fields": ["res_id"], "limit": 1},
        )
        if not data_rows:
            return None
        view_rows = models.execute_kw(
            db, uid, key, "ir.ui.view", "read",
            [[data_rows[0]["res_id"]], ["arch"]],
        )
        if not view_rows:
            return None
        return view_rows[0].get("arch") or None
    except Exception:
        return None


def check_field_exists_on_model_fast(db: str, model: str, field_name: str, login: str = "Admin") -> bool | None:
    """Fast-path attempt for spot_check.check_field_exists_on_model() --
    the SINGLE highest-impact remaining conversion, found live
    2026-07-22: this runs on EVERY task's own reproduction-check step
    (no condition, no opt-out), yet was never converted in the original
    pass. Measured live as the direct cause of the "Reproducing X"
    step's own unexplained ~15.7s gap. Same fields_get()-based check as
    get_model_fields_fast(), just returning membership directly.

    Real, confirmed false-negative bug found live (2026-07-22) BEFORE
    this was ever deployed: every fast-path function here defaults to
    `login="Admin"`, an account confirmed to exist on odoo16_dev (the
    one real dev target these were all benchmarked against) -- but a
    genuinely fresh/duplicate/sandbox test database may have NO user
    named "Admin" at all (confirmed live: `RuntimeError("user 'Admin'
    not found in db 'odoo16_dev_fresh_...'")` from the JIT-key mint
    itself), which the original shell version never hit at all (it
    runs via `env[...]` inside a superuser `odoo-bin shell` session,
    no login required, works identically on ANY database).

    Returns `bool | None` -- unlike the original shell version's own
    always-bool contract, this can genuinely NOT KNOW (None) when the
    fast path can't even authenticate against `db`. This is
    DELIBERATE and different from every sibling _fast() function in
    this module: a caller must fall back to the real, universal shell
    version on None, never treat it as "confirmed does not exist" --
    doing so caused a real, live test failure (a genuinely-installed
    field reported as missing) before this fix.

    Phase 26A (2026-07-27): as of `get_model_fields_fast()`'s own
    rewrite to read via `ir.model.fields` instead of `fields_get()`,
    this function is now ALSO immune (transitively, via that call) to
    the group-restricted-field omission bug documented on
    `_read_real_field_rows()` -- a field correctly restricted to a
    group `login` doesn't hold is no longer reported as "does not
    exist."
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
    except Exception:
        return None
    fields = get_model_fields_fast(model, db, login)
    if fields is None:
        return None
    return field_name in fields


def check_group_exists_fast(db: str, group_name: str, login: str = "Admin") -> bool | None:
    """Drop-in replacement for spot_check.check_group_exists() -- a
    plain search on res.groups' own (translated) name field via
    XML-RPC resolves this directly; no need to know about the
    underlying jsonb column shape the raw-SQL shell version had to
    handle manually.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        count = models.execute_kw(db, uid, key, "res.groups", "search_count", [[("name", "=", group_name)]])
        return count > 0
    except Exception:
        return None


def resolve_group_external_id_fast(db: str, group_name: str, login: str = "Admin") -> str | None:
    """Phase 33 (2026-08-11): real, confirmed gap found live -- `check_group_exists_fast()`
    confirms a res.groups row exists by its human-readable, translated display NAME (e.g.
    "Settings"), but Odoo's real `groups=` kwarg on a field/button requires the group's
    external ID (e.g. "base.group_system"), never the display name. Handing Build a display
    name to put directly in `groups=` produces syntactically-plausible-looking but wrong code --
    confirmed live: Build, given "Settings" as guidance, ignored it and invented its OWN
    (also wrong) external-ID-shaped guesses ("project.group_admin", "base.group_user") instead,
    since a bare display name doesn't look like valid `groups=` syntax at all.

    Returns "module.name" (the real external ID via ir.model.data), or None if the group exists
    but has no external ID (a small number of legacy/DB-only groups) -- never guesses at a
    plausible-looking ID when one can't be resolved.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        group_rows = models.execute_kw(
            db, uid, key, "res.groups", "search_read", [[("name", "=", group_name)]], {"fields": ["id"]},
        )
        if not group_rows:
            return None
        group_id = group_rows[0]["id"]
        data_rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [[("model", "=", "res.groups"), ("res_id", "=", group_id)]],
            {"fields": ["module", "name"]},
        )
        if not data_rows:
            return None
        return f"{data_rows[0]['module']}.{data_rows[0]['name']}"
    except Exception:
        return None


def check_group_model_access_fast(
    db: str, group_name: str, model: str,
    expects_read: bool | None, expects_write: bool | None,
    expects_create: bool | None, expects_unlink: bool | None,
    login: str = "Admin",
) -> tuple[bool, str] | None:
    """Drop-in replacement for spot_check.check_group_model_access() --
    same OR-aggregation semantics across multiple access rows for the
    same group+model, same "only compare claims actually made" rule,
    via a direct ir.model.access search_read instead of a raw SQL join.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        rows = models.execute_kw(
            db, uid, key, "ir.model.access", "search_read",
            [[("group_id.name", "=", group_name), ("model_id.model", "=", model)]],
            {"fields": ["perm_read", "perm_write", "perm_create", "perm_unlink"]},
        )
        if not rows:
            return False, f"group {group_name!r} has NO access rows at all for model {model!r}"

        actual_read = any(r["perm_read"] for r in rows)
        actual_write = any(r["perm_write"] for r in rows)
        actual_create = any(r["perm_create"] for r in rows)
        actual_unlink = any(r["perm_unlink"] for r in rows)

        mismatches = []
        for label, expected, actual in (
            ("read", expects_read, actual_read),
            ("write", expects_write, actual_write),
            ("create", expects_create, actual_create),
            ("delete", expects_unlink, actual_unlink),
        ):
            if expected is not None and expected != actual:
                mismatches.append(f"{label}: expected {expected}, actual {actual}")
        if mismatches:
            return False, f"group {group_name!r} on model {model!r} permission mismatch: {'; '.join(mismatches)}"
        return True, f"group {group_name!r} on model {model!r} matches all claimed permissions"
    except Exception:
        return None


_BUTTON_TAG_RE_TEMPLATE = r'<button\b[^>]*\bname="{name}"[^>]*/?>'
_GROUPS_ATTR_RE = re.compile(r'\bgroups="([^"]*)"')


def check_button_group_restricted_fast(
    db: str, model: str, button_name: str, group_xmlid: str | None = None,
    group_name: str | None = None, login: str = "Admin",
) -> tuple[bool, str] | None:
    """Real, general fix (2026-07-25, task 008): the field-existence
    reproduction check and the field-visibility security-claim check
    (both above) have NO mechanism to verify a BUTTON's own visibility
    was restricted to a group -- a button is a `<button groups="...">`
    XML attribute, not an ORM field with a `.groups` attribute (what
    `check_field_group_restricted_fast` reads), and it will never appear
    in `fields_get()`'s own field list either (what `_check_field_exists_
    on_model` checks). Confirmed live: a task restricting the real
    "Send to customer" button to `base.group_system` genuinely,
    correctly implemented the fix (confirmed directly via Code-Review
    AND a live registry read), yet every existing verification path was
    checking the wrong thing entirely and reported failure identically
    across 2+ rounds.

    Reads the model's own RESOLVED form-view arch via `get_views()` (the
    same live, fully-inherited XML any real Odoo client actually
    renders -- not a single view record's own raw, un-merged XML), locates
    the named button's own opening tag, and reads its `groups` attribute
    the same way `check_field_group_restricted_fast()` reads a field's --
    resolving each comma-separated xmlid to its group's real display name.

    Matches on EITHER the raw xmlid (when `group_xmlid` is given -- this
    project's own goal convention states standard/base groups like
    `base.group_system` literally, and matching by xmlid is strictly more
    reliable than by display name for a well-known base group whose
    display name may not resemble how the goal describes it, e.g.
    `base.group_system`'s real name is 'Settings', not 'System
    Administrators') OR the resolved display name (`group_name`, for a
    custom group the goal only ever names descriptively). Returns None
    (never a guessed False) if the button element itself can't be found
    in the resolved arch at all -- a genuinely missing button is a
    different check's job, not this one's.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        res = models.execute_kw(db, uid, key, model, "get_views", [[[False, "form"]], {}])
        view_id = (res.get("views") or {}).get("form", {}).get("id")
        if not view_id:
            return None
        # Real, confirmed false-negative bug found live (2026-07-26,
        # Phase 25F, task 008's own first run under the new architecture):
        # `get_views()`'s own returned `arch` is POST-PROCESSED for the
        # calling user's own real permissions -- Odoo strips a `groups=`
        # attribute from any element the CALLING user already has access
        # to, since from that user's own perspective the element is
        # simply visible, full stop. Every function here defaults to
        # `login="Admin"`, and Admin belongs to `base.group_system` (the
        # exact group this whole check exists to verify) on this real dev
        # target -- so `get_views()`'s own arch NEVER contains the
        # `groups=` attribute being checked, regardless of whether the
        # real view correctly declares it. Confirmed directly: reading
        # the view's raw `arch_db` via `ir.ui.view.search_read` showed
        # `groups="base.group_system"` genuinely present; `get_views()`'s
        # own resolved arch for the identical view, moments later, did
        # not -- ruling out any install/caching gap (also independently
        # ruled out live: reinstalling the module and even relaunching
        # the warm worker process made no difference). `read_combined()`
        # (`ir.ui.view`'s own method for exactly this -- the fully
        # inherited/merged arch WITHOUT the per-viewer ACL postprocessing
        # `get_views()`/`fields_view_get()` both apply) is the correct
        # read for a check that must see the view's own DECLARATION, not
        # what a privileged caller happens to be shown.
        combined = models.execute_kw(db, uid, key, "ir.ui.view", "read_combined", [view_id])
        arch = combined.get("arch") or ""
        tag_match = re.search(_BUTTON_TAG_RE_TEMPLATE.format(name=re.escape(button_name)), arch)
        if not tag_match:
            return None  # button itself not found in the resolved arch -- a different check's job
        groups_match = _GROUPS_ATTR_RE.search(tag_match.group(0))
        groups_str = groups_match.group(1) if groups_match else ""
        xmlids = [g.strip() for g in groups_str.split(",") if g.strip()]
        if not xmlids:
            return False, f"button {button_name!r} on {model!r} has NO group restriction at all -- visible to everyone"
        if group_xmlid and group_xmlid in xmlids:
            return True, f"button {button_name!r} on {model!r} is correctly restricted to {group_xmlid!r}"
        names = []
        for xmlid in xmlids:
            if "." not in xmlid:
                names.append(xmlid)
                continue
            module, name = xmlid.split(".", 1)
            data_rows = models.execute_kw(
                db, uid, key, "ir.model.data", "search_read",
                [[("module", "=", module), ("name", "=", name), ("model", "=", "res.groups")]],
                {"fields": ["res_id"], "limit": 1},
            )
            if not data_rows:
                names.append(xmlid)
                continue
            group_rows = models.execute_kw(db, uid, key, "res.groups", "read", [[data_rows[0]["res_id"]], ["name"]])
            names.append(group_rows[0]["name"] if group_rows else xmlid)
        if group_name and group_name in names:
            return True, f"button {button_name!r} on {model!r} is correctly restricted to {group_name!r}"
        if group_xmlid or group_name:
            return False, (
                f"button {button_name!r} on {model!r} is restricted to {xmlids!r} ({names!r}), not the "
                f"claimed group {group_xmlid or group_name!r}"
            )
        return False, f"button {button_name!r} on {model!r} has a group restriction, but no claimed group to compare against"
    except Exception:
        return None


_PY_FIELD_DECL_START_RE_TEMPLATE = r"^\s*{name}\s*=\s*fields\.\w+\("
_PY_FIELD_GROUPS_KWARG_RE = re.compile(r"groups\s*=\s*['\"]([^'\"]+)['\"]")


def _extract_field_declaration_source(source: str, field_name: str) -> str | None:
    """Paren-balanced extraction of one field's own real declaration
    statement from real Python source -- same discipline already used
    elsewhere in this codebase for exactly this kind of "can't rely on
    a single-line regex because real kwargs may contain their own
    parens" problem (e.g. specialists/build/specialist.py's own
    `_strip_field_and_method`). A naive non-greedy regex ending in a
    literal close-paren
    would break the moment a field declares e.g. `selection=[(...)]`
    before `groups=`. Returns None (never a wrong guess) if the field's
    own declaration can't be confidently located.
    """
    match = re.search(_PY_FIELD_DECL_START_RE_TEMPLATE.format(name=re.escape(field_name)), source, re.MULTILINE)
    if not match:
        return None
    depth = 0
    for i in range(match.end() - 1, len(source)):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                return source[match.start(): i + 1]
    return None


def check_field_group_restricted_fast(
    db: str, model: str, field_name: str, group_name: str | None = None,
    group_xmlid: str | None = None, login: str = "Admin",
) -> tuple[bool, str] | None:
    """Drop-in replacement for spot_check.check_field_group_restricted().

    Phase 26A follow-up (2026-07-27), found live during this same fix's
    own end-to-end verification: task testing revealed a REAL, general
    bug distinct from the ACL-omission fix above -- `group_xmlid`
    matching (preferred over resolved display-name matching, for
    exactly the reason `check_button_group_restricted_fast()`'s own
    docstring already explains: a well-known base group's real display
    name may not resemble how the goal describes it, e.g.
    `base.group_system`'s real name is 'Settings') was added to the
    BUTTON-restriction check in Phase 25F (fix 40) but never propagated
    to this, its sibling -- confirmed live: a goal stating "Restrict to
    group: mis_base_extend.group_user_developer_access_fields" (this
    project's own xmlid-literal goal convention) correctly resolved the
    real restriction ('Access: Developer only') but reported it as NOT
    matching, because the caller only ever had a raw xmlid string to
    compare against a list of resolved DISPLAY names. Exactly the same
    "propagate the fix to its siblings" gap Phase 26 (see Finding #3,
    `_resolve_current_round_named_field`) already diagnosed as a
    recurring pattern in this codebase -- found here for a third time.

    Phase 26A (2026-07-27) rewrite -- real, live-confirmed finding: a
    Python-declared field's own `groups="module.xmlid"` kwarg is a
    PURE RUNTIME attribute, checked entirely inside Odoo's own
    `fields_get()` execution against the calling user's real
    permissions -- it is NEVER mirrored into `ir.model.fields.groups`
    (that many2many column stays genuinely empty; confirmed live
    against a real, disposable test field with a real, confirmed-active
    `groups=` restriction: `ir.model.fields.search_read()` correctly
    found the row, but its own `groups` column was `[]`). This means
    there is NO way to read the real, declared group restriction for a
    Python-declared field via plain XML-RPC introspection at all --
    `ir.model.fields.groups` only reflects a Studio-style, DB-authored
    field, and this pipeline has never generated one of those (confirmed:
    zero rows repo-wide with `groups` set, this whole database's entire
    history). The OLD version of this function (calling `fields_get()`
    directly) inherited the exact omission bug documented on
    `_read_real_field_rows()`'s own docstring -- it silently returned
    None ("field doesn't exist") for ANY field genuinely restricted to
    a group `login` (always "Admin") doesn't hold, indistinguishable
    from a genuinely missing field.

    Fixed by reading the REAL GENERATED SOURCE directly -- the same
    "when RPC-level introspection can't answer the question reliably,
    read the real file" discipline this project's own Code-Review
    hallucination filters and `check_button_group_restricted_fast()`'s
    sibling (view XML, not Python) already established.

    Real, confirmed follow-up finding during this same fix's own live
    verification: resolving the field's owning module via
    `find_module_defining_model(model)` is the WRONG mechanism here --
    that function answers "which module DEFINES this MODEL" (its own
    `_name`), which is undefined or ambiguous for the extremely common
    case of a field added via `_inherit` on a BASE Odoo model (e.g.
    `res.partner`, owned by no single custom module at all). The field
    row already read via `_read_real_field_rows()` carries its own
    `ir.model.fields.modules` column instead -- a comma-separated list
    of the module(s) that specifically declared THIS field (confirmed
    live: correctly resolved to the real disposable test module,
    `oma_phase26a_test_fixture`, unlike `find_module_defining_model()`
    which returned None for the same case since `res.partner` itself
    has no single defining module). This is a strictly more precise
    per-FIELD lookup, not a per-MODEL heuristic, and is used
    exclusively now, for both custom-model and `_inherit`-only shapes.

    Existence is still confirmed via `_read_real_field_rows()` first
    (fast, ACL-independent, per Phase 26A's own core fix) before ever
    reaching the filesystem, so a genuinely nonexistent field still
    correctly returns None without needing an SSH round-trip at all.
    """
    try:
        rows = _read_real_field_rows(model, db, login)
        if rows is None:
            return None
        field_row = next((row for row in rows if row["name"] == field_name), None)
        if field_row is None:
            return None  # field itself doesn't exist -- a different check's job

        owning_modules = [m.strip() for m in (field_row.get("modules") or "").split(",") if m.strip()]
        if not owning_modules:
            return None  # can't locate the real source -- never guess

        py_source = None
        for module_name in owning_modules:
            try:
                files = read_module_files(module_name)
            except CodebaseReadError:
                continue
            py_source = "\n".join(content for path, content in files.items() if path.endswith(".py"))
            decl = _extract_field_declaration_source(py_source, field_name)
            if decl is not None:
                break
        else:
            decl = None
        if decl is None:
            return None  # couldn't confidently locate the real declaration -- never guess

        groups_match = _PY_FIELD_GROUPS_KWARG_RE.search(decl)
        groups_str = groups_match.group(1) if groups_match else ""
        xmlids = [g.strip() for g in groups_str.split(",") if g.strip()]

        if not xmlids:
            return False, f"{model}.{field_name} has NO group restriction at all -- visible to everyone"
        if group_xmlid and group_xmlid in xmlids:
            return True, f"{model}.{field_name} is correctly restricted to {group_xmlid!r}"

        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        names = []
        for xmlid in xmlids:
            if "." not in xmlid:
                names.append(xmlid)
                continue
            module, name = xmlid.split(".", 1)
            data_rows = models.execute_kw(
                db, uid, key, "ir.model.data", "search_read",
                [[("module", "=", module), ("name", "=", name), ("model", "=", "res.groups")]],
                {"fields": ["res_id"], "limit": 1},
            )
            if not data_rows:
                names.append(xmlid)
                continue
            group_rows = models.execute_kw(db, uid, key, "res.groups", "read", [[data_rows[0]["res_id"]], ["name"]])
            names.append(group_rows[0]["name"] if group_rows else xmlid)
        if group_name and group_name in names:
            return True, f"{model}.{field_name} is correctly restricted to {group_name!r}"
        claimed = group_xmlid or group_name
        return False, f"{model}.{field_name} is restricted to {names!r}, not the claimed group {claimed!r}"
    except Exception:
        return None


def get_module_state_fast(module_name: str, db: str, login: str = "Admin") -> str | None:
    """Drop-in replacement for the post-install state-verification
    check inline inside toolchain.install_module() -- a SEPARATE
    odoo-bin-shell boot found live (2026-07-22) after this module's
    first pass, running on EVERY single install (both the sandbox
    pre-flight AND the real dev-target install), not just the 8
    functions originally identified. Same fields_get-style direct
    read against ir.module.module instead.
    """
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        rows = models.execute_kw(
            db, uid, key, "ir.module.module", "search_read",
            [[("name", "=", module_name)]], {"fields": ["state"], "limit": 1},
        )
        return rows[0]["state"] if rows else "NOT_FOUND"
    except Exception:
        return None


def get_relation_fields_fast(model_name: str, db: str, login: str = "Admin") -> dict[str, str] | None:
    """Drop-in replacement for toolchain.get_relation_fields() -- same
    return contract ({field_name: target_model} for every real
    Many2one/One2many/Many2many field, including inherited ones).

    Phase 26A (2026-07-27): rewritten to read via `_read_real_field_rows()`
    (ir.model.fields) instead of `fields_get()` directly -- a
    group-restricted relational field would previously have vanished
    from this function's own output exactly like the plain-field case
    `_read_real_field_rows()`'s own docstring documents (the omission
    bug applies identically regardless of field type).
    """
    rows = _read_real_field_rows(model_name, db, login)
    if not rows:
        return None
    out = {
        row["name"]: row["relation"]
        for row in rows
        if row.get("ttype") in ("many2one", "one2many", "many2many") and row.get("relation")
    }
    return out or None


# Real investigation dead-end, kept as a comment so it isn't re-attempted (2026-08-06, Phase 30
# root-cause pass critic round 2): a structural, module-ownership-based way to tell mixin/ORM-
# automatic relations (create_uid/write_uid, message_ids/activity_ids/etc.) apart from genuine
# business relations was proposed as a self-maintaining alternative to a hardcoded model-name
# blocklist. Tested live against the real odoo16_dev database before building it: `ir.model.
# fields.modules` for `project.project.create_uid`/`.message_ids`/`.activity_ids` all report
# `{'project'}` as the owning module -- Odoo attributes a mixin-added field's `ir.model.fields`
# row to whichever module extends THAT model with the mixin, not to `mail`/`base` where the
# mixin itself lives. The signal this needed doesn't exist at this granularity. Confirmed no
# usable structural shortcut here; the hardcoded noise list in schema_grounding.py stays
# hardcoded, just kept as complete as real testing can make it.


def resolve_xmlids_exist_fast(xmlids: list[str], db: str, login: str = "Admin") -> dict[str, bool] | None:
    """Drop-in replacement for toolchain.resolve_xmlids_exist() --
    same {xmlid: True/False} contract, via a direct ir.model.data
    search_read (module+name pair, split from each dotted xmlid) rather
    than env.ref() inside a fresh shell process. A dotted xmlid with no
    '.' at all is never valid Odoo external-id syntax -- treated as
    False, matching what env.ref() would raise on too.
    """
    if not xmlids:
        return {}
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        pairs = []
        for xmlid in xmlids:
            if "." not in xmlid:
                continue
            module, name = xmlid.split(".", 1)
            pairs.append((xmlid, module, name))
        result = {xmlid: False for xmlid in xmlids}
        if not pairs:
            return result
        domain = []
        for i, (_xmlid, module, name) in enumerate(pairs):
            if i > 0:
                domain.insert(0, "|")
            domain += ["&", ("module", "=", module), ("name", "=", name)]
        rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read", [domain], {"fields": ["module", "name"]},
        )
        found = {(r["module"], r["name"]) for r in rows}
        for xmlid, module, name in pairs:
            result[xmlid] = (module, name) in found
        return result
    except Exception:
        return None


def resolve_owning_modules_fast(
    bare_xmlids: list[str], db: str, own_module_name: str | None = None, login: str = "Admin",
    depends_on_module: str | None = None,
) -> dict[str, str | None] | None:
    """Drop-in replacement for toolchain.resolve_owning_modules() --
    same {bare_xmlid: owning_module_or_None} contract and the same
    contamination-safety rule (excludes oma_*-owned rows except the
    caller's own module, orders by id ascending so the genuine
    original definer always wins over a later, self-poisoned row),
    via ir.model.data search_read instead of a raw SQL cursor script.

    Real, confirmed bug found live (2026-08-10, task e65381cc, equipment_views_menu node): the
    `oma_*` exclusion exists to avoid resolving to some OTHER task's own unrelated scaffolded
    module that happens to define a same-named model -- but it had no exception for a task's own
    REAL, explicitly-declared `depends_on_module:` target, which (since Bug 40's fix made
    `depends_on_module:` correctly reachable for the pipeline's own previously-scaffolded
    modules, not just genuine external customer modules) is now routinely ALSO `oma_*`-prefixed.
    Confirmed live: `oma.equipment`'s real owning module (`oma_build_a_complete_field_ab52b7f8`)
    was completely unresolvable for a module extending it, so its security_csv's bare
    `model_oma_equipment` reference could never be qualified, and Odoo's own CSV loader
    (which only resolves bare ids against the installing module's OWN definitions) failed
    installation every time. `depends_on_module`, when given, is an EXPLICIT, already-verified
    real dependency (never a guess) -- exactly as trustworthy as `own_module_name` already was.
    """
    if not bare_xmlids:
        return {}
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        domain = [("model", "=", "ir.model"), ("name", "in", bare_xmlids)]
        trusted_oma_modules = [m for m in (own_module_name, depends_on_module) if m]
        if trusted_oma_modules:
            domain = ["|", ("module", "not like", "oma_"), ("module", "in", trusted_oma_modules)] + domain
        else:
            domain = [("module", "not like", "oma_")] + domain
        rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read", [domain],
            {"fields": ["name", "module"], "order": "id asc"},
        )
        found: dict[str, str] = {}
        for row in rows:
            found.setdefault(row["name"], row["module"])
        return {name: found.get(name) for name in bare_xmlids}
    except Exception:
        return None


def resolve_model_names_from_xmlids_fast(
    bare_names: list[str], db: str, login: str = "Admin",
    own_module_name: str | None = None, depends_on_module: str | None = None,
) -> dict[str, str | None] | None:
    """Drop-in replacement for toolchain.resolve_model_names_from_xmlids()
    -- same {bare_name: real_dotted_model_or_None} contract and the
    same contamination-safety rule (excludes oma_*-owned rows, orders
    by id ascending), via an ir.model.data search_read then an
    ir.model read for the resolved res_ids, instead of a raw SQL join.

    Real, confirmed bug found live (2026-08-10, task e65381cc): same
    `depends_on_module`/`own_module_name` exemption as `resolve_owning_modules_fast()`'s own fix
    -- see that function's docstring for the full incident this closes.
    """
    if not bare_names:
        return {}
    try:
        uid, key = _get_or_create_shared_key(db, login)
        models = _models_proxy(db)
        domain = [("model", "=", "ir.model"), ("name", "in", bare_names)]
        trusted_oma_modules = [m for m in (own_module_name, depends_on_module) if m]
        if trusted_oma_modules:
            domain = ["|", ("module", "not like", "oma_"), ("module", "in", trusted_oma_modules)] + domain
        else:
            domain = [("module", "not like", "oma_")] + domain
        rows = models.execute_kw(
            db, uid, key, "ir.model.data", "search_read",
            [domain],
            {"fields": ["name", "res_id"], "order": "id asc"},
        )
        found_res_id: dict[str, int] = {}
        for row in rows:
            found_res_id.setdefault(row["name"], row["res_id"])
        if not found_res_id:
            return {name: None for name in bare_names}
        model_rows = models.execute_kw(
            db, uid, key, "ir.model", "read", [list(set(found_res_id.values())), ["model"]],
        )
        model_by_res_id = {row["id"]: row["model"] for row in model_rows}
        return {
            name: model_by_res_id.get(found_res_id[name]) if name in found_res_id else None
            for name in bare_names
        }
    except Exception:
        return None
