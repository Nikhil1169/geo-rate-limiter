"""
Redis policy and override writer.

Two schemas:
  policy:{region}:{tier}  → Contract 4 JSON  (fields include ttl_seconds)
  override:{user_id}      → {limit_per_minute, ttl, reason}  (NOT ttl_seconds)

The override schema deliberately differs — see gateway/internal/override/cache.go:17-23.

Sequence counter _seq is module-level and starts at 1 each agent run.
The unix timestamp in policy_id disambiguates across restarts.
"""

import json
import logging
import time
from datetime import datetime, timezone

import redis.asyncio as aioredis

from agent.config import Config, REGIONS, TIERS, STATIC_FALLBACK
from agent.decider import Decision

log = logging.getLogger(__name__)

_seq: int = 0


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def _parse_addr(addr: str) -> tuple[str, int]:
    host, port = addr.rsplit(":", 1)
    return host, int(port)


def build_redis_pool(cfg: Config) -> dict[str, aioredis.Redis]:
    def _redis(addr: str) -> aioredis.Redis:
        host, port = _parse_addr(addr)
        return aioredis.Redis(host=host, port=port, decode_responses=True)

    return {
        "us":   _redis(cfg.redis_us),
        "eu":   _redis(cfg.redis_eu),
        "asia": _redis(cfg.redis_asia),
    }


async def read_current_policies(
    redis_pool: dict[str, aioredis.Redis],
) -> dict[tuple[str, str], dict]:
    """
    Read all 9 policy:{region}:{tier} keys and return a flat dict.
    Falls back to {limit_per_minute: STATIC_FALLBACK[tier]} on missing/error.
    """
    policies: dict[tuple[str, str], dict] = {}
    for region in REGIONS:
        r = redis_pool[region]
        for tier in TIERS:
            key = f"policy:{region}:{tier}"
            try:
                raw = await r.get(key)
                if raw:
                    policies[(region, tier)] = json.loads(raw)
                else:
                    policies[(region, tier)] = {"limit_per_minute": STATIC_FALLBACK[tier]}
            except Exception as exc:
                log.warning("read %s: %s — using static fallback", key, exc)
                policies[(region, tier)] = {"limit_per_minute": STATIC_FALLBACK[tier]}
    return policies


async def apply_decisions(
    decisions: list[Decision],
    redis_pool: dict[str, aioredis.Redis],
) -> list[dict]:
    """
    Write each Decision to the appropriate regional Redis.
    Returns a list of the payloads actually written (for logging).
    """
    written: list[dict] = []
    for d in decisions:
        try:
            if d.type == "policy":
                payload = _make_policy(d)
                key = f"policy:{d.region}:{d.tier}"
                await redis_pool[d.region].set(key, json.dumps(payload), ex=d.ttl)
                log.info("SET %-24s limit_per_minute=%-6d  %s", key, d.limit_per_minute, d.reason)
                written.append({"key": key, **payload})

            elif d.type == "override":
                payload = _make_override(d)
                key = f"override:{d.user_id}"
                await redis_pool[d.region].set(key, json.dumps(payload), ex=d.ttl)
                log.info("SET %-40s limit_per_minute=%-6d  %s", key, d.limit_per_minute, d.reason)
                written.append({"key": key, **payload})

        except Exception as exc:
            log.error("apply %s: %s", d.reason, exc)

    return written


# ── payload builders ─────────────────────────────────────────────────────────

def _make_policy(d: Decision) -> dict:
    seq = _next_seq()
    return {
        "policy_id":        f"pol_{int(time.time())}_{seq}",
        "region":           d.region,
        "tier":             d.tier,
        "limit_per_minute": d.limit_per_minute,
        "burst":            max(1, d.limit_per_minute // 5),
        "algorithm":        "token_bucket",
        "ttl_seconds":      d.ttl,
        "reason":           d.reason,
        "created_at":       datetime.now(timezone.utc).isoformat(),
    }


def _make_override(d: Decision) -> dict:
    # Field is "ttl" (int seconds), not "ttl_seconds" — gateway parses .TTL
    return {
        "limit_per_minute": d.limit_per_minute,
        "ttl":              d.ttl,
        "reason":           d.reason,
    }
