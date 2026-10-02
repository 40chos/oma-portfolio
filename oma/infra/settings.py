"""Central settings module. Every credential/address is read from the
environment (populated from .env, git-ignored) here and only here.
Nothing else in this codebase should read os.environ directly for a
secret -- import from this module instead, so there is exactly one
place to change when the target environment changes (per Phase 1's own
requirement and the project's zero-plain-text-secrets rule).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    db: str
    user: str
    password: str

    @property
    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.db} "
            f"user={self.user} password={self.password}"
        )


@dataclass(frozen=True)
class RedisSettings:
    host: str
    port: int
    password: str
    username: str
    db: int


@dataclass(frozen=True)
class GatewaySettings:
    # Real infra change, 2026-07-13: two dedicated, permanently-resident
    # GPU hosts replace the old single-host llama-swap hot-swap pattern
    # -- see infra/gateway_client.py's module docstring for the full
    # rationale and the coder/reasoning role split.
    coder_base_url: str
    reasoning_base_url: str
    # Real infra change, 2026-07-22: GPU Worker 03 (10.1.19.200:9090) --
    # see infra/gateway_client.py's own BACKEND_FAST_EXTRACTION comment
    # for the full rationale. Defaults to the real, confirmed address so
    # existing deployments don't need a .env change to pick this up;
    # still overridable per-environment like every other gateway URL.
    fast_extraction_base_url: str
    api_key: str | None
    # Real infra change, 2026-07-22: GPU Worker 01 (coder) crashed under
    # concurrent load from our own agents pushing system RAM past what
    # was available -- see infra/gpu_capacity_guard.py's own module
    # docstring for the full rationale, including why the ceiling is a
    # FRACTION of the host's own live-reported total (self-adapts to a
    # VM resize) rather than only a fixed byte count. Optional overrides
    # (sane defaults, never required) so the ceiling/wait behavior is
    # tunable per-environment without a code change.
    ram_ceiling_fraction: float
    ram_ceiling_bytes: float
    ram_check_poll_interval_sec: float
    ram_check_max_wait_sec: float


@dataclass(frozen=True)
class Neo4jSettings:
    """Odoo Knowledge Graph Neo4j connection settings -- Phase 36 §3.2/§4.2.

    Env var naming deliberately does NOT follow this module's own
    OMA_<SERVICE>_<FIELD> convention (see PostgresSettings/RedisSettings/
    GatewaySettings above). Phase 36 §3.2 originally specified
    OMA_NEO4J_URI/OMA_NEO4J_USER/OMA_NEO4J_PASSWORD, but the credentials
    that actually exist on disk (agents/.env-graph-neo4j, the one real
    source of truth for this instance, mode 600) use unprefixed
    NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD, matching the neo4j Python
    driver's own conventional names, not this module's prefix scheme.
    This was a deliberate, explicit instruction for this implementation
    task (matching real deployed credentials over the plan doc's
    speculative naming) -- flagged here, and in the implementation
    report, as a real divergence from Phase 36 §3.2's literal text so a
    future reader of the plan document isn't misled by it.
    """

    uri: str
    user: str
    password: str
    connection_timeout_s: float = 5.0
    max_transaction_retry_time_s: float = 15.0
    # Real fix 2026-08-13: 8.0s was set when the live graph had ~6.8k :Field
    # nodes; grounding/blast-radius queries (get_module_grounding, the
    # Nexo-isolation sweep) now confirmed timing out against the real,
    # grown graph (9.2k+ :Field nodes after the EXTENDS_FIELD fix). Raised
    # to keep these usable for real safety checks rather than failing open
    # or blocking a round on a timeout that has nothing to do with the
    # query's own correctness.
    query_timeout_s: float = 30.0
    max_graph_calls_per_generation_round: int = 2
    grounding_alert_pct: float = 5.0
    database: str = "neo4j"
    graph_staleness_warn_days: int = 14
    graph_staleness_hard_days: int = 30


def load_postgres_settings() -> PostgresSettings:
    return PostgresSettings(
        host=_require("OMA_PG_HOST"),
        port=int(_require("OMA_PG_PORT")),
        db=_require("OMA_PG_DB"),
        user=_require("OMA_PG_USER"),
        password=_require("OMA_PG_PASSWORD"),
    )


def load_redis_settings() -> RedisSettings:
    return RedisSettings(
        host=_require("OMA_REDIS_HOST"),
        port=int(_require("OMA_REDIS_PORT")),
        password=_require("OMA_REDIS_PASSWORD"),
        username=_require("OMA_REDIS_USER"),
        db=int(_require("OMA_REDIS_DB")),
    )


def _default_gateway_base_url() -> str:
    """Stage 3 port: the original three GPU-host base URLs are replaced by a
    single OMA_LLM_MODE switch -- `local` (default) points every backend pool
    at Ollama; `cloud` points them at LiteLLM instead, which proxies to the
    real OpenAI/Anthropic API using a key the operator supplies. Either way,
    all three backend pools in infra/gateway_client.py still exist and still
    speak the same OpenAI-compatible wire protocol -- only which real server
    sits behind each pool changes. Explicit OMA_MODEL_GATEWAY_URL_* env vars
    always override this, same as before.

    Defaults to the host-published ports (127.0.0.1), matching this repo's
    default setup (`setup.sh` runs the app on the host, not in its own
    container -- see README's "Why no app container by default"). The
    opt-in containerized `app` compose service overrides these to the
    container-network service names (`ollama`/`litellm`) explicitly in
    docker-compose.yml instead of relying on this default.
    """
    mode = os.environ.get("OMA_LLM_MODE", "local").strip().lower()
    if mode == "cloud":
        return "http://127.0.0.1:4000/v1"
    return "http://127.0.0.1:11434/v1"


def load_gateway_settings() -> GatewaySettings:
    from infra.gpu_capacity_guard import (
        DEFAULT_MAX_WAIT_SEC,
        DEFAULT_POLL_INTERVAL_SEC,
        DEFAULT_RAM_CEILING_BYTES,
        DEFAULT_RAM_CEILING_FRACTION,
    )
    default_url = _default_gateway_base_url()
    return GatewaySettings(
        coder_base_url=os.environ.get("OMA_MODEL_GATEWAY_URL_CODER") or default_url,
        reasoning_base_url=os.environ.get("OMA_MODEL_GATEWAY_URL_REASONING") or default_url,
        fast_extraction_base_url=(
            os.environ.get("OMA_MODEL_GATEWAY_URL_FAST_EXTRACTION") or default_url
        ),
        api_key=os.environ.get("OMA_MODEL_GATEWAY_API_KEY") or None,
        ram_ceiling_fraction=float(
            os.environ.get("OMA_GPU_RAM_CEILING_FRACTION") or DEFAULT_RAM_CEILING_FRACTION
        ),
        ram_ceiling_bytes=float(os.environ.get("OMA_GPU_RAM_CEILING_BYTES") or DEFAULT_RAM_CEILING_BYTES),
        ram_check_poll_interval_sec=float(
            os.environ.get("OMA_GPU_RAM_CHECK_POLL_INTERVAL_SEC") or DEFAULT_POLL_INTERVAL_SEC
        ),
        ram_check_max_wait_sec=float(
            os.environ.get("OMA_GPU_RAM_CHECK_MAX_WAIT_SEC") or DEFAULT_MAX_WAIT_SEC
        ),
    )


def load_neo4j_settings() -> Neo4jSettings:
    return Neo4jSettings(
        uri=_require("NEO4J_URI"),
        user=_require("NEO4J_USER"),
        password=_require("NEO4J_PASSWORD"),
        connection_timeout_s=float(os.environ.get("NEO4J_CONNECTION_TIMEOUT_S", "5.0")),
        max_transaction_retry_time_s=float(os.environ.get("NEO4J_MAX_RETRY_TIME_S", "15.0")),
        query_timeout_s=float(os.environ.get("NEO4J_QUERY_TIMEOUT_S", "30.0")),
        max_graph_calls_per_generation_round=int(
            os.environ.get("NEO4J_MAX_CALLS_PER_ROUND", "2")
        ),
        grounding_alert_pct=float(os.environ.get("NEO4J_GROUNDING_ALERT_PCT", "5.0")),
        database=os.environ.get("NEO4J_DATABASE", "neo4j"),
        graph_staleness_warn_days=int(os.environ.get("NEO4J_STALENESS_WARN_DAYS", "14")),
        graph_staleness_hard_days=int(os.environ.get("NEO4J_STALENESS_HARD_DAYS", "30")),
    )
