#!/usr/bin/env python3
"""
seed_policies.py — write default Contract-4 policies to all three regional
Redis instances so the gateway policy store has a known starting state.

Run from the repo root after `docker-compose up -d`:
    python3 tools/seed_policies.py

Each policy key is: policy:{region}:{tier}
TTL: 86400 s (24 h) so the keys survive a test session but don't accumulate.
"""

import json
import sys
import time
from datetime import datetime, timezone

try:
    import redis
except ImportError:
    sys.exit("redis-py not installed — run: pip install redis")

# ── static defaults (must match gateway/internal/policy/policy.go) ──────────
DEFAULTS = {
    "free":     {"limit_per_minute": 10,   "burst": 5,   "algorithm": "token_bucket"},
    "premium":  {"limit_per_minute": 100,  "burst": 20,  "algorithm": "token_bucket"},
    "internal": {"limit_per_minute": 1000, "burst": 100, "algorithm": "token_bucket"},
}

REGIONS = {
    "us":   6379,
    "eu":   6380,
    "asia": 6381,
}

SEQ = 1  # sequence number — agent-written policies will use higher values


def make_policy(region: str, tier: str, seq: int) -> dict:
    d = DEFAULTS[tier]
    return {
        "policy_id":       f"pol_{int(time.time())}_{seq}",
        "region":          region,
        "tier":            tier,
        "limit_per_minute": d["limit_per_minute"],
        "burst":           d["burst"],
        "algorithm":       d["algorithm"],
        "ttl_seconds":     86400,
        "reason":          "seed — default baseline",
        "created_at":      datetime.now(timezone.utc).isoformat(),
    }


def seed_region(region: str, port: int) -> None:
    try:
        r = redis.Redis(host="localhost", port=port, decode_responses=True)
        r.ping()
    except Exception as e:
        print(f"  [SKIP] {region} (localhost:{port}): {e}")
        return

    for tier in DEFAULTS:
        key = f"policy:{region}:{tier}"
        payload = make_policy(region, tier, SEQ)
        r.set(key, json.dumps(payload), ex=86400)
        print(f"  SET {key}  limit={payload['limit_per_minute']} algo={payload['algorithm']}")


def main() -> None:
    print("Seeding policies to all regional Redis instances...")
    for region, port in REGIONS.items():
        print(f"\n[{region}] localhost:{port}")
        seed_region(region, port)
    print("\nDone.")


if __name__ == "__main__":
    main()
