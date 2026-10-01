"""Neo4j connection helper. One process-wide singleton driver, credentials
only via infra.settings (never a literal address/password anywhere else).
Every query call site (tools_odoo/graph_queries.py) is responsible for
wrapping its Cypher text in a neo4j.Query(..., timeout=settings.query_timeout_s)
before passing it to session.run() -- this module only bounds connection
setup, not per-query time. Phase 36 §3.2.
"""

from __future__ import annotations

from functools import lru_cache

from neo4j import Driver, GraphDatabase

from infra.settings import load_neo4j_settings


@lru_cache(maxsize=1)
def get_neo4j_driver() -> Driver:
    """Process-wide singleton Neo4j driver.

    The neo4j Python driver is documented as expensive to create (it owns
    a connection pool) and cheap to reuse, so this is constructed once per
    process and reused for the process's lifetime -- matching this repo's
    existing get_redis_client()/get_gateway_client() singleton pattern.
    connection_timeout/max_transaction_retry_time bound how long a
    genuinely unreachable instance can hang connection setup, per §3.2.
    """
    s = load_neo4j_settings()
    return GraphDatabase.driver(
        s.uri,
        auth=(s.user, s.password),
        connection_timeout=s.connection_timeout_s,
        max_transaction_retry_time=s.max_transaction_retry_time_s,
    )


def get_neo4j_read_driver() -> Driver:
    """Same singleton driver as get_neo4j_driver(); callers on Build's (and
    Code-Review's) runtime read path use this name and open sessions with
    default_access_mode=neo4j.READ_ACCESS (see tools_odoo/graph_queries.py
    and §3.3). This does not construct a second driver/connection pool --
    it is a naming/call-site convention so a reviewer (and, per §3.3, a
    future AST-based lint check) can identify any use of
    get_neo4j_driver() outside graph_queries.py/schema_grounding.py and
    treat any hit as a potential write-path violation to scrutinize.
    """
    return get_neo4j_driver()


def reset_neo4j_driver_cache_for_tests() -> None:
    """Test-only helper: clears the lru_cache singleton so tests can swap
    in a fresh mocked/fake driver between cases without leaking state
    across test functions (mirrors the same need any lru_cache-backed
    singleton factory has in tests).
    """
    get_neo4j_driver.cache_clear()
