"""Cross-region G-Counter sync service.

One instance runs per region.  Responsibilities:
  1. Subscribe to rl:sync:counter on all three Redis instances
     and apply max-merge when a remote region's slot update arrives.
  2. Reconcile every RECONCILE_INTERVAL_SEC seconds by re-publishing
     this region's current slot values so late-joining subscribers
     catch up after missed pub/sub frames or partitions.
  3. Expose Prometheus metrics on METRICS_PORT.
  4. Expose partition-simulation admin endpoints on ADMIN_PORT
     (wired in by admin.py, which imports PARTITIONS from here).

Environment variables
  REGION                    required  "us" | "eu" | "asia"
  LOCAL_REDIS_ADDR          required  host:port
  PEER_REDIS_US_ADDR        set the two that do not match LOCAL_REDIS_ADDR
  PEER_REDIS_EU_ADDR
  PEER_REDIS_ASIA_ADDR
  ADMIN_PORT                optional  default 8001
  METRICS_PORT              optional  default 9101
  RECONCILE_INTERVAL_SEC    optional  default 30
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import redis.asyncio as aioredis
from prometheus_client import Gauge, start_http_server

from sync.counter import REGIONS, RegionalCounter
from sync.state import PARTITIONS

log = logging.getLogger(__name__)

CHANNEL = "rl:sync:counter"

# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

_lag_gauge = Gauge(
    "rl_sync_lag_seconds",
    "Replication lag: time between source publish and this region's apply",
    ["from_region", "to_region"],
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_redis(addr: str) -> aioredis.Redis:
    host, port = addr.rsplit(":", 1)
    return aioredis.Redis(host=host, port=int(port), decode_responses=False)


async def _subscribe(
    sub_redis: aioredis.Redis,
    counter: RegionalCounter,
    my_region: str,
    label: str,
) -> None:
    """Listen on CHANNEL of sub_redis; apply merges to counter's local Redis."""
    ps = sub_redis.pubsub()
    await ps.subscribe(CHANNEL)
    log.info("subscribed  channel=%s  source=%s", CHANNEL, label)

    async for raw in ps.listen():
        if raw["type"] != "message":
            continue
        try:
            m = json.loads(raw["data"])
        except Exception:
            log.warning("unparseable message: %r", raw["data"][:120])
            continue

        src = m.get("region")
        if not src:
            continue
        if src == my_region:
            continue  # own publish — already applied locally by the gateway

        if (src, my_region) in PARTITIONS:
            log.debug("partition drop  %s → %s", src, my_region)
            continue

        try:
            await counter.merge_remote(
                m["tier"],
                m["user_id"],
                int(m["window_id"]),
                src,
                int(m["value"]),
            )
        except Exception as exc:
            log.error("merge_remote failed: %s", exc)
            continue

        ts_s = m.get("ts_ms", 0) / 1000.0
        lag = max(0.0, time.time() - ts_s)
        _lag_gauge.labels(from_region=src, to_region=my_region).set(lag)
        log.debug("merged  %s→%s  lag=%.3fs", src, my_region, lag)


async def _reconcile(
    local_redis: aioredis.Redis,
    my_region: str,
    interval: int,
) -> None:
    """Periodically re-publish this region's slot values for active windows.

    This recovers any peer that missed live pub/sub messages (e.g. after a
    partition heals or a subscriber restarts).  Sending absolute slot values
    makes every publish idempotent — the max-merge on the receiving end is
    a no-op if the receiver already has an equal-or-greater value.
    """
    while True:
        await asyncio.sleep(interval)
        win = int(time.time()) // 60
        pattern = f"rl:global:*:*:{win}"
        published = 0

        async for key in local_redis.scan_iter(pattern):
            ks = key.decode() if isinstance(key, bytes) else key
            # Key format: rl:global:{tier}:{user_id}:{window_id}
            try:
                _, _, tier, *uid_parts, win_str = ks.split(":")
                uid = ":".join(uid_parts)
                w = int(win_str)
            except ValueError:
                continue

            raw = await local_redis.hget(ks, my_region)
            val = int(raw) if raw is not None else 0
            if val == 0:
                continue

            payload = json.dumps({
                "tier": tier,
                "user_id": uid,
                "window_id": w,
                "region": my_region,
                "value": val,
                "ts_ms": int(time.time() * 1000),
            })
            await local_redis.publish(CHANNEL, payload)
            published += 1

        if published:
            log.info("reconciliation  window=%d  published=%d keys", win, published)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run() -> None:
    region = os.environ["REGION"]
    local_addr = os.environ["LOCAL_REDIS_ADDR"]
    metrics_port = int(os.environ.get("METRICS_PORT", "9101"))
    admin_port = int(os.environ.get("ADMIN_PORT", "8001"))
    interval = int(os.environ.get("RECONCILE_INTERVAL_SEC", "30"))

    local_redis = _build_redis(local_addr)
    counter = await RegionalCounter.create(local_redis, region)

    # Collect all Redis clients to subscribe to (own + peers).
    all_redis: dict[str, aioredis.Redis] = {region: local_redis}
    for r in REGIONS:
        if r == region:
            continue
        env_key = f"PEER_REDIS_{r.upper()}_ADDR"
        addr = os.environ.get(env_key)
        if addr:
            all_redis[r] = _build_redis(addr)
        else:
            log.warning("peer region %s: no address configured, subscription skipped", r)

    start_http_server(metrics_port)
    log.info("metrics server  port=%d", metrics_port)

    # Start admin HTTP server.
    try:
        import uvicorn
        from sync.admin import build_app

        admin_app = build_app()
        config = uvicorn.Config(admin_app, host="0.0.0.0", port=admin_port, log_level="warning")
        server = uvicorn.Server(config)
        admin_task = asyncio.create_task(server.serve())
        log.info("admin server  port=%d", admin_port)
    except ImportError:
        admin_task = None
        log.warning("uvicorn/admin not available, skipping admin server")

    tasks: list[asyncio.Task] = [
        asyncio.create_task(_reconcile(local_redis, region, interval)),
    ]
    for r_name, r_client in all_redis.items():
        tasks.append(asyncio.create_task(
            _subscribe(r_client, counter, region, r_name)
        ))
    if admin_task:
        tasks.append(admin_task)

    log.info(
        "sync-service started  region=%s  peers=%s",
        region,
        [r for r in all_redis if r != region],
    )
    await asyncio.gather(*tasks)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
