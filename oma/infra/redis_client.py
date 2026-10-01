"""Redis connection helper. One shared client factory, credentials only
via infra.settings (never a literal address/password anywhere else).
"""

from __future__ import annotations

import redis

from infra.settings import load_redis_settings


def get_redis_client() -> redis.Redis:
    s = load_redis_settings()
    return redis.Redis(
        host=s.host,
        port=s.port,
        username=s.username,
        password=s.password,
        db=s.db,
        decode_responses=True,
    )
