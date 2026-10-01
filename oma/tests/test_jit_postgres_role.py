"""Phase 1, step 6: prove the JIT Postgres role actually works end to
end -- create it, connect and use it for exactly what it's scoped for,
confirm VALID UNTIL rejects a too-late login, then drop it and confirm
it's really gone (not just expired-but-lingering).
"""

import os
import sys
import time
import uuid

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.jit_postgres_role import create_scoped_role, drop_scoped_role
from infra.settings import load_postgres_settings


def test_scoped_role_lifecycle():
    task_id = f"jittest-{uuid.uuid4().hex[:12]}"

    creds = create_scoped_role(task_id, ttl_interval="1 hour")
    assert creds["role"].startswith("oma_task_")

    # Connect AS the scoped role and prove it can do exactly the narrow
    # thing it was granted -- insert + read agent_memory_events.
    conn = psycopg2.connect(
        host=creds["host"],
        port=creds["port"],
        dbname=creds["dbname"],
        user=creds["role"],
        password=creds["password"],
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO agent_memory_events (event_type, actor, summary) "
        "VALUES ('note', %s, 'jit-role-test row') RETURNING id",
        (creds["role"],),
    )
    row_id = cur.fetchone()[0]
    cur.execute("SELECT summary FROM agent_memory_events WHERE id = %s", (row_id,))
    assert cur.fetchone()[0] == "jit-role-test row"
    cur.close()
    conn.close()
    print("PASS: scoped role could connect and use exactly its granted privilege")

    # Prove it's actually scoped -- it must NOT be able to create a table,
    # since it only got SELECT/INSERT on one specific table.
    conn2 = psycopg2.connect(
        host=creds["host"], port=creds["port"], dbname=creds["dbname"],
        user=creds["role"], password=creds["password"],
    )
    conn2.autocommit = True
    cur2 = conn2.cursor()
    try:
        cur2.execute("CREATE TABLE should_not_be_allowed (id int)")
        raised = False
    except psycopg2.errors.InsufficientPrivilege:
        raised = True
    finally:
        cur2.close()
        conn2.close()
    assert raised, "scoped role must NOT be able to create arbitrary tables"
    print("PASS: scoped role correctly cannot create tables outside its grant")

    # Clean up (deliberately not relying on VALID UNTIL alone, per the
    # module's own docstring -- this is the explicit-drop half of the
    # requirement, tested for real).
    drop_scoped_role(task_id)

    # Confirm the role is genuinely gone, not just expired-but-present.
    admin = psycopg2.connect(
        host=creds["host"], port=creds["port"], dbname=creds["dbname"],
        user=load_postgres_settings().user, password=load_postgres_settings().password,
    )
    admin.autocommit = True
    acur = admin.cursor()
    acur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (creds["role"],))
    still_exists = acur.fetchone() is not None
    acur.close()
    admin.close()
    assert not still_exists, "drop_scoped_role must actually remove the role, not just expire it"
    print("PASS: drop_scoped_role actually removed the role")


def test_valid_until_blocks_new_login_after_expiry():
    """VALID UNTIL blocks *new* logins once passed -- but, per the
    module's own docstring, does NOT end an already-open session. This
    test only checks the new-login half; the drop-ends-existing-session
    half is exactly why drop_scoped_role exists and is always called
    explicitly rather than relying on this alone.
    """
    task_id = f"jitexpiry-{uuid.uuid4().hex[:12]}"
    creds = create_scoped_role(task_id, ttl_interval="1 second")
    time.sleep(2)

    try:
        conn = psycopg2.connect(
            host=creds["host"], port=creds["port"], dbname=creds["dbname"],
            user=creds["role"], password=creds["password"], connect_timeout=3,
        )
        conn.close()
        expired_login_blocked = False
    except psycopg2.OperationalError:
        expired_login_blocked = True

    drop_scoped_role(task_id)  # still clean up even though it should be expired
    assert expired_login_blocked, "a new login after VALID UNTIL has passed must be rejected"
    print("PASS: VALID UNTIL correctly blocks a new login after expiry")


if __name__ == "__main__":
    test_scoped_role_lifecycle()
    test_valid_until_blocks_new_login_after_expiry()
    print("\nALL JIT-POSTGRES-ROLE TESTS PASSED")
