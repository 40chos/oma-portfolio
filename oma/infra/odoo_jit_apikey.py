"""The Odoo-side half of the JIT-credential requirement (Phase 1 step 6
was the Postgres side; this is Phase 7 step 4's Odoo side). Odoo has no
native expiry on API keys -- `res.users.apikeys` records live forever
until explicitly removed -- so the create-then-delete wrapper here IS
the whole mechanism: generate a fresh key at the start of a task,
delete that exact `res.users.apikeys` record at the end, never hold a
permanent key.

Generating an API key on someone's behalf isn't a plain XML-RPC
`execute_kw` operation in stock Odoo (self-service, tied to the
calling user's own session) -- so this runs via the same `odoo-bin
shell` channel used to create the dedicated user itself (legitimate ORM
access, real business logic, not raw SQL), which this project's access
boundary already treats as the right channel for credential/container
management, distinct from the XML-RPC API used for actual data
operations.

Stage 5 port: the original reached this channel over SSH + a remote
`docker exec` (two real hosts: this devbox, and a remote dev host running the
actual Odoo container). In this single-host Docker Compose port, the Odoo
container is a direct sibling on the same compose network, so this is a
plain local `docker exec` -- same real `odoo-bin shell` invocation, same
script templates below, no SSH hop needed because there's no second host
to hop to.
"""

from __future__ import annotations

import subprocess

from infra.odoo_settings import _assert_safe_odoo_target  # the guard, always run first

_CONTAINER_ENV = "OMA_ODOO_CONTAINER"
_ODOO_BIN_PATH = "/usr/bin/odoo"
_ODOO_CONF_PATH = "/etc/odoo/odoo.conf"


def _run_shell_script(db: str, script: str, remote_port: int = 8071) -> str:
    """Runs a Python script through `odoo-bin shell` inside the `odoo`
    compose service via `docker exec`, targeting `db`. Always re-validates
    the safety guard here too (not just at the call site) -- this function
    can execute real ORM writes, so it must never trust a caller blindly.
    """
    import os

    _assert_safe_odoo_target(db, remote_port)  # redundant with the caller's own check, deliberately

    container = os.environ.get(_CONTAINER_ENV, "oma-odoo-1")

    # docker-compose.yml writes a real odoo.conf (db_host/db_port/db_user/
    # db_password) into this container at its own startup -- a fresh
    # `odoo shell` invocation here reads the same file, no separate
    # connection args needed.
    proc = subprocess.run(
        ["docker", "exec", "-i", container, _ODOO_BIN_PATH, "shell",
         "-c", _ODOO_CONF_PATH, "-d", db, "--no-http"],
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"odoo-bin shell failed (rc={proc.returncode}): {proc.stderr[-2000:]}")
    return proc.stdout


_GENERATE_SCRIPT_TEMPLATE = """\
user = env['res.users'].search([('login', '=', {login!r})], limit=1)
if not user:
    print("ERROR_USER_NOT_FOUND")
else:
    key_env = env(user=user.id)
    # scope=None (NOT False) -- per res.users.apikeys._generate's own
    # docstring, "If None, the key will give access to any rpc." A real
    # bug caught during testing: passing False stores a literal
    # non-NULL scope value that _check_credentials's `scope IS NULL OR
    # scope = 'rpc'` check never matches, so the key silently never
    # authenticates -- confirmed via a live login-failure test before
    # this fix, not assumed.
    key = key_env['res.users.apikeys']._generate(None, {key_name!r})
    env.cr.commit()
    print(f"APIKEY_CREATED id={{key_env['res.users.apikeys'].search([('name','=',{key_name!r}),('user_id','=',user.id)], limit=1, order='id desc').id}} key={{key}}")
"""

_REVOKE_SCRIPT_TEMPLATE = """\
key_row = env['res.users.apikeys'].browse({key_row_id})
if key_row.exists():
    key_row.sudo().unlink()
    env.cr.commit()
    print(f"APIKEY_REVOKED id={key_row_id}")
else:
    print(f"APIKEY_ALREADY_GONE id={key_row_id}")
"""


def create_task_api_key(db: str, login: str, task_id: str) -> tuple[int, str]:
    """Creates a fresh API key for `login`, named after this task so
    it's identifiable in Odoo's own UI. Returns (apikeys_row_id, plaintext_key)
    -- the plaintext is only ever available at creation time (Odoo's own
    design, matching a real password/secret generation pattern), so the
    caller must use it immediately and never expect to retrieve it again.
    """
    key_name = f"oma-task-{task_id}"
    script = _GENERATE_SCRIPT_TEMPLATE.format(login=login, key_name=key_name)
    output = _run_shell_script(db, script)
    for line in output.splitlines():
        if line.startswith("APIKEY_CREATED"):
            parts = dict(p.split("=", 1) for p in line[len("APIKEY_CREATED "):].split(" ", 1))
            return int(parts["id"]), parts["key"]
        if "ERROR_USER_NOT_FOUND" in line:
            raise RuntimeError(f"user {login!r} not found in db {db!r}")
    raise RuntimeError(f"could not parse APIKEY_CREATED from shell output: {output[-500:]!r}")


def revoke_task_api_key(db: str, apikeys_row_id: int) -> None:
    """Deletes the exact res.users.apikeys record created for this task --
    the required end-of-task cleanup half of the JIT mechanism. Never
    holds a permanent key: this must be called when a task ends,
    success or failure.
    """
    script = _REVOKE_SCRIPT_TEMPLATE.format(key_row_id=apikeys_row_id)
    _run_shell_script(db, script)
