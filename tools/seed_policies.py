#!/usr/bin/env python3
"""
seed_policies.py — write Contract-4 policies to all three regional Redis
instances so the gateway policy store has a known starting state.

Run from the repo root after `docker-compose up -d`:

    python3 tools/seed_policies.py          # static defaults (10/100/1000 rpm)
    python3 tools/seed_policies.py --demo   # demo baselines (300/3000/30000 rpm)

The --demo baselines are sized so the traffic simulator's product_launch
(50 RPS peak) and noisy_neighbor scenarios produce visible agent decisions.
Static defaults (10/100/1000) are kept as the gateway's crash-safety fallback
in gateway/internal/policy/policy.go and are not changed here.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import redis
except ImportError:
    sys.exit("redis-py not installed — run: pip install redis")

# ── static defaults (match gateway/internal/policy/policy.go) ───────────────
_STATIC = {
    "free":     {"limit_per_minute": 10,     "burst": 5,    "algorithm": "token_bucket"},
    "premium":  {"limit_per_minute": 100,    "burst": 20,   "algorithm": "token_bucket"},
    "internal": {"limit_per_minute": 1_000,  "burst": 100,  "algorithm": "token_bucket"},
}

# ── demo baselines (sized for simulator scenarios + agent Rule 1/2 cycles) ──
_DEMO = {
    "free":     {"limit_per_minute": 300,    "burst": 60,   "algorithm": "token_bucket"},
    "premium":  {"limit_per_minute": 3_000,  "burst": 600,  "algorithm": "token_bucket"},
    "internal": {"limit_per_minute": 30_000, "burst": 6000, "algorithm": "token_bucket"},
}

REGIONS = {
    "us":   (os.getenv("REDIS_US_HOST",   "localhost"), int(os.getenv("REDIS_US_PORT",   "6379"))),
    "eu":   (os.getenv("REDIS_EU_HOST",   "localhost"), int(os.getenv("REDIS_EU_PORT",   "6380"))),
    "asia": (os.getenv("REDIS_ASIA_HOST", "localhost"), int(os.getenv("REDIS_ASIA_PORT", "6381"))),
}

# Seed uses seq=1 (static) or seq=2 (demo) so agent-written policies
# (seq ≥ 3) are visibly distinct in the rl_policy_version Grafana panel.
_SEQ = {"static": 1, "demo": 2}


def make_policy(region: str, tier: str, defaults: dict, seq: int) -> dict:
    d = defaults[tier]
    return {
        "policy_id":        f"pol_{int(time.time())}_{seq}",
        "region":           region,
        "tier":             tier,
        "limit_per_minute": d["limit_per_minute"],
        "burst":            d["burst"],
        "algorithm":        d["algorithm"],
        "ttl_seconds":      86400,
        "reason":           "seed — demo baseline" if defaults is _DEMO else "seed — default baseline",
        "created_at":       datetime.now(timezone.utc).isoformat(),
    }


def seed_region(region: str, host: str, port: int, defaults: dict, seq: int) -> None:
    try:
        r = redis.Redis(host=host, port=port, decode_responses=True)
        r.ping()
    except Exception as e:
        print(f"  [SKIP] {region} ({host}:{port}): {e}")
        return

    for tier in defaults:
        key = f"policy:{region}:{tier}"
        payload = make_policy(region, tier, defaults, seq)
        r.set(key, json.dumps(payload), ex=86400)
        print(f"  SET {key:<24}  limit={payload['limit_per_minute']:<7}  burst={payload['burst']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Write demo baselines (300/3000/30000 rpm) instead of static defaults.",
    )
    args = parser.parse_args()

    defaults = _DEMO if args.demo else _STATIC
    seq = _SEQ["demo"] if args.demo else _SEQ["static"]
    label = "demo baselines" if args.demo else "static defaults"

    print(f"Seeding policies [{label}] to all regional Redis instances...")
    for region, (host, port) in REGIONS.items():
        print(f"\n[{region}] {host}:{port}")
        seed_region(region, host, port, defaults, seq)
    print("\nDone.")


if __name__ == "__main__":
    main()
