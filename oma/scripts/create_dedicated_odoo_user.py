"""Run via `odoo-bin shell` (piped over SSH) against a target database --
creates the dedicated, narrowly-scoped, non-admin Odoo user this project
authenticates mcp-server-odoo as. Idempotent: safe to re-run.

Internal User group ONLY (base.group_user) -- no admin, no settings
access, nothing beyond a plain internal user's default Odoo permissions.
This is deliberately the least-privilege starting point; per the
technical document's own words, the whole point of testing against a
real non-admin account is proving the least-privilege behavior actually
works, not assuming it does.
"""

LOGIN = "oma_agent"
NAME = "OMA Agent (Odoo Manager Agent -- dedicated, non-admin)"

existing = env["res.users"].search([("login", "=", LOGIN)], limit=1)
if existing:
    print(f"USER_ALREADY_EXISTS id={existing.id} login={existing.login}")
else:
    group_user = env.ref("base.group_user")
    user = env["res.users"].create({
        "name": NAME,
        "login": LOGIN,
        "groups_id": [(6, 0, [group_user.id])],
        "active": True,
        "share": False,
    })
    env.cr.commit()
    print(f"USER_CREATED id={user.id} login={user.login}")
