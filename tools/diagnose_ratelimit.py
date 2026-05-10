#!/usr/bin/env python3
"""
Rate-limit enforcement diagnostic.

Shows exactly what the gateway sees per-user: local bucket limit, global cap,
G-Counter value, and when the global cap fires for the current send rate.

Usage (from repo root):
    python3 tools/diagnose_ratelimit.py [--user free_00001] [--region us]
"""

import argparse
import json
import sys
import time

import redis
import requests


REDIS_PORTS = {"us": 6379, "eu": 6380, "asia": 6381}
PROM_URL    = "http://localhost:9090"


# ── helpers ───────────────────────────────────────────────────────────────────

def rc(region: str) -> redis.Redis:
    return redis.Redis(host="localhost", port=REDIS_PORTS[region],
                       decode_responses=True, socket_connect_timeout=2)


def prom(q: str) -> list:
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": q}, timeout=4)
        r.raise_for_status()
        body = r.json()
        return body["data"]["result"] if body["status"] == "success" else []
    except Exception as e:
        return []


def prom_scalar(q: str) -> float:
    res = prom(q)
    try:
        return float(res[0]["value"][1])
    except (IndexError, KeyError, ValueError):
        return 0.0


def sep(title=""):
    width = 60
    if title:
        pad = (width - len(title) - 2) // 2
        print(f"\n{'─'*pad} {title} {'─'*pad}")
    else:
        print("─" * width)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user",   default="free_00001")
    ap.add_argument("--tier",   default="free")
    ap.add_argument("--region", default="us")
    args = ap.parse_args()

    user, tier, region = args.user, args.tier, args.region

    print(f"\n{'═'*60}")
    print(f"  Rate-Limit Enforcement Diagnostic")
    print(f"  user={user}  tier={tier}  region={region}")
    print(f"{'═'*60}")

    r = rc(region)

    # ── 1. Policy ─────────────────────────────────────────────────────────────
    sep("1. Active tier policy")
    pol_key = f"policy:{region}:{tier}"
    pol_raw = r.get(pol_key)
    pol_ttl = r.ttl(pol_key)

    if pol_raw:
        pol = json.loads(pol_raw)
        lim = pol.get("limit_per_minute", 0)
        print(f"  {pol_key}")
        print(f"    limit_per_minute : {lim}")
        print(f"    burst            : {pol.get('burst')}")
        print(f"    algorithm        : {pol.get('algorithm')}")
        print(f"    reason           : {pol.get('reason')}")
        print(f"    Redis TTL        : {pol_ttl}s")
        print()
        # Gateway sets GlobalLimit = limit_per_minute (store.go:115)
        global_limit = lim
        print(f"  ⚠  Gateway GlobalLimit = limit_per_minute = {global_limit}")
        print(f"     (store.go sets GlobalLimit = c4.LimitPerMinute, same value)")
    else:
        print(f"  {pol_key}  ← NOT SET (gateway uses static fallback: free=10)")
        global_limit = 10

    # ── 2. Override ───────────────────────────────────────────────────────────
    sep("2. User override")
    ov_key = f"override:{user}"
    ov_raw = r.get(ov_key)
    ov_ttl = r.ttl(ov_key)

    if ov_raw:
        ov = json.loads(ov_raw)
        ov_lim = ov.get("limit_per_minute", 0)
        print(f"  {ov_key}")
        print(f"    limit_per_minute : {ov_lim}  ← effectiveLimit for this user")
        print(f"    reason           : {ov.get('reason')}")
        print(f"    Redis TTL        : {ov_ttl}s")
    else:
        print(f"  {ov_key}  ← NOT SET (using tier policy limit: {global_limit})")
        ov_lim = None

    effective_limit = ov_lim if ov_lim else global_limit
    print(f"\n  effectiveLimit = {effective_limit}/min  ({effective_limit/60:.2f} req/s allowed)")

    # ── 3. Local token bucket ─────────────────────────────────────────────────
    sep("3. Local token bucket (rl:local)")
    local_key = f"rl:local:{region}:{tier}:{user}"
    bucket = r.hgetall(local_key)
    bucket_ttl = r.ttl(local_key)
    if bucket:
        tokens      = bucket.get("tokens", "?")
        last_refill = bucket.get("last_refill_ms", "?")
        age_ms = int(time.time() * 1000) - int(last_refill) if last_refill != "?" else 0
        print(f"  {local_key}")
        print(f"    tokens        : {tokens}")
        print(f"    last_refill   : {age_ms}ms ago")
        print(f"    Redis TTL     : {bucket_ttl}s")
    else:
        print(f"  {local_key}  ← NOT FOUND (user hasn't sent a request yet)")

    # ── 4. G-Counter ──────────────────────────────────────────────────────────
    sep("4. G-Counter (cross-region detection + global cap)")
    window_id   = int(time.time()) // 60
    g_key_cur   = f"rl:global:{tier}:{user}:{window_id}"
    g_key_prev  = f"rl:global:{tier}:{user}:{window_id - 1}"

    slots_cur  = r.hgetall(g_key_cur)
    slots_prev = r.hgetall(g_key_prev)
    g_ttl_cur  = r.ttl(g_key_cur)

    def sum_slots(slots):
        return sum(int(v) for v in slots.values() if v.isdigit())

    g_sum_cur  = sum_slots(slots_cur)
    g_sum_prev = sum_slots(slots_prev)

    print(f"  Current window  ({g_key_cur})")
    print(f"    slots : {dict(slots_cur) or 'empty'}")
    print(f"    sum   : {g_sum_cur}")
    print(f"    TTL   : {g_ttl_cur}s")
    print(f"  Previous window : sum = {g_sum_prev}")

    # ── 5. Global cap analysis ────────────────────────────────────────────────
    sep("5. Global cap analysis")
    send_rps_free  = 8.0   # abuser send rate in scenario
    allowed_rps    = effective_limit / 60.0
    send_rps_total = send_rps_free   # only 1 user being diagnosed

    # G-counter grows at SEND rate (handler.go increments regardless of allowed/denied)
    # NOTE: this is the bug — it should grow at ALLOWED rate
    g_counter_rate = send_rps_total   # sends/sec → G-counter increments/sec
    cap_fires_at   = global_limit / g_counter_rate if g_counter_rate > 0 else float("inf")

    print(f"  GlobalLimit (from policy)    : {global_limit}/min")
    print(f"  Send rate ({user})  : {send_rps_free} req/s  ({send_rps_free*60:.0f}/min)")
    print(f"  G-Counter growth rate        : {g_counter_rate} increments/s  ← grows at SEND rate")
    print(f"  Global cap fires at          : t = {cap_fires_at:.1f}s into window")
    print()

    if cap_fires_at < 60:
        remaining_window = 60 - cap_fires_at
        allowed_before_cap = allowed_rps * cap_fires_at
        print(f"  ⚠  Cap fires at {cap_fires_at:.1f}s — user is denied for remaining {remaining_window:.1f}s!")
        print(f"     Allowed before cap : {allowed_before_cap:.0f} req  (instead of {effective_limit} target)")
        print(f"     Allow efficiency   : {allowed_before_cap/effective_limit*100:.0f}%  of override target")
    else:
        print(f"  ✓  Cap fires at {cap_fires_at:.1f}s — well after 60s window, override fully effective")

    # ── 6. Expected vs actual allow rate ─────────────────────────────────────
    sep("6. Predicted allow rates  (20 RPS total, 8 abuser + 12 legit)")
    legit_rps      = 12.0
    legit_req_min  = legit_rps * 60  # 720/min, all under 300/min each → all allowed
    abuser_req_min = 8.0 * 60        # 480/min

    if cap_fires_at < 60:
        abuser_allowed = allowed_rps * cap_fires_at
        note = f"cap fires at {cap_fires_at:.1f}s, stops {effective_limit}-{abuser_allowed:.0f}={effective_limit-abuser_allowed:.0f} req"
    else:
        abuser_allowed = float(effective_limit)
        note = "cap never fires, full override in effect"

    total_req     = abuser_req_min + legit_req_min
    total_allowed = abuser_allowed + legit_req_min
    allow_rate    = total_allowed / total_req * 100

    print(f"  Abuser  ({user}) : {abuser_req_min:.0f} sent, {abuser_allowed:.0f} allowed  [{note}]")
    print(f"  Legit (40 users × 0.3 RPS)   : {legit_req_min:.0f} sent, {legit_req_min:.0f} allowed")
    print(f"  ──────────────────────────────────────────")
    print(f"  Total                         : {total_req:.0f} sent, {total_allowed:.0f} allowed")
    print(f"  Predicted allow rate          : {allow_rate:.1f}%")

    # Also show WITHOUT override for comparison
    no_ov_effective = global_limit
    no_ov_rate_allowed = no_ov_effective / 60.0
    no_ov_cap_fires = global_limit / (8.0 * 60 / 60)  # grow at send rate
    no_ov_allowed = no_ov_rate_allowed * min(no_ov_cap_fires, 60)
    no_ov_total_allowed = no_ov_allowed + legit_req_min
    no_ov_allow_rate = no_ov_total_allowed / total_req * 100
    print(f"\n  WITHOUT override (tier limit {global_limit}/min): {no_ov_allow_rate:.1f}%")
    print(f"  WITH    override ({effective_limit}/min)          : {allow_rate:.1f}%")
    if allow_rate < no_ov_allow_rate:
        diff = no_ov_allow_rate - allow_rate
        print(f"\n  ⚠  Override makes allow rate WORSE by {diff:.1f}%")
        print(f"     Cause: G-Counter still grows at 8 req/s, capping at {cap_fires_at:.1f}s regardless.")
        print(f"     Fix A: Only increment G-Counter for ALLOWED requests (handler.go:140)")
        print(f"     Fix B: Set GlobalLimit = limit_per_minute * 3 (store.go:115)")
        print(f"     Both fixes together: override becomes fully effective → abuser gets 30 allowed.")
    else:
        print(f"\n  ✓  Override improves allow rate by {allow_rate - no_ov_allow_rate:.1f}%")

    # ── 7. Prometheus live data ───────────────────────────────────────────────
    sep("7. Live Prometheus metrics")
    total_rps_live  = prom_scalar('sum(rate(rl_requests_total[30s]))')
    allow_rps_live  = prom_scalar('sum(rate(rl_requests_total{decision="allowed"}[30s]))')
    deny_rps_live   = prom_scalar('sum(rate(rl_requests_total{decision="denied"}[30s]))')
    allow_rate_live = (allow_rps_live / total_rps_live * 100) if total_rps_live > 0 else 0

    print(f"  Total RPS  : {total_rps_live:.2f}")
    print(f"  Allow RPS  : {allow_rps_live:.2f}")
    print(f"  Deny  RPS  : {deny_rps_live:.2f}")
    print(f"  Allow rate : {allow_rate_live:.1f}%")

    # Per-user counter (only visible when sum*2 >= GlobalLimit)
    counter_res = prom(f'rl_counter_value{{tier="{tier}",region="{region}",user_id="{user}"}}')
    if counter_res:
        cv = float(counter_res[0]["value"][1])
        print(f"\n  rl_counter_value[{user}] = {cv:.0f}")
        print(f"  (reported when G-Counter sum ≥ GlobalLimit/2 = {global_limit//2})")
    else:
        print(f"\n  rl_counter_value[{user}] = not visible in Prometheus yet")
        print(f"  (only reported when G-Counter sum ≥ {global_limit//2})")

    print(f"\n{'═'*60}\n")


if __name__ == "__main__":
    main()
