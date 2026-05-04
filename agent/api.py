"""
Dashboard API — Flask backend for the monitoring dashboard.

Endpoints:
  GET /api/metrics   — Prometheus summary (RPS, allow rate, active users, history)
  GET /api/decisions — Last 20 agent decisions from decisions.jsonl
  GET /api/overrides — All override:* keys from all 3 Redis instances
  GET /api/counters  — rl:global:* slot distribution from all 3 Redis instances
"""

import json
import os
import time
from collections import deque
from pathlib import Path

import redis
import requests
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Configuration ────────────────────────────────────────────────────────────

PROM_URL = os.getenv("PROM_URL", "http://localhost:9090")
DECISIONS_PATH = Path(os.getenv("LOG_PATH", "agent/decisions.jsonl"))

REDIS_CONFIGS = {
    "us":   {"host": os.getenv("REDIS_US_HOST", "localhost"),   "port": int(os.getenv("REDIS_US_PORT", "6379"))},
    "eu":   {"host": os.getenv("REDIS_EU_HOST", "localhost"),   "port": int(os.getenv("REDIS_EU_PORT", "6380"))},
    "asia": {"host": os.getenv("REDIS_ASIA_HOST", "localhost"), "port": int(os.getenv("REDIS_ASIA_PORT", "6381"))},
}

# In-memory RPS history (last 20 samples)
_rps_history: deque = deque(maxlen=20)
_allow_history: deque = deque(maxlen=20)
_users_history: deque = deque(maxlen=20)

# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis_client(region: str) -> redis.Redis:
    cfg = REDIS_CONFIGS[region]
    return redis.Redis(host=cfg["host"], port=cfg["port"], decode_responses=True, socket_connect_timeout=2)


def _safe_redis(region: str, fn):
    """Execute fn(client) against a region's Redis; return None on any error."""
    try:
        client = _redis_client(region)
        return fn(client)
    except Exception:
        return None

# ── Prometheus helpers ────────────────────────────────────────────────────────

def _prom_query(promql: str) -> list:
    try:
        resp = requests.get(
            f"{PROM_URL}/api/v1/query",
            params={"query": promql},
            timeout=4,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "success":
            return body["data"]["result"]
    except Exception:
        pass
    return []


def _prom_scalar(promql: str) -> float:
    results = _prom_query(promql)
    if results:
        try:
            return float(results[0]["value"][1])
        except (IndexError, KeyError, ValueError, TypeError):
            pass
    return 0.0


def _prom_range(promql: str, duration: str = "2m", step: str = "6s") -> list[dict]:
    """Fetch a range query and return [{t, v}] pairs for the first series."""
    try:
        now = time.time()
        resp = requests.get(
            f"{PROM_URL}/api/v1/query_range",
            params={
                "query": promql,
                "start": now - 120,
                "end": now,
                "step": step,
            },
            timeout=5,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "success":
            results = body["data"]["result"]
            if results:
                return [
                    {"t": int(pair[0] * 1000), "v": round(float(pair[1]) if float(pair[1]) == float(pair[1]) else 0.0, 3)}
                    for pair in results[0]["values"]
                ]
    except Exception:
        pass
    return []

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/api/metrics")
def api_metrics():
    # Current values
    total_rps = _prom_scalar('sum(rate(rl_requests_total[1m]))')
    allowed_rps = _prom_scalar('sum(rate(rl_requests_total{decision="allowed"}[1m]))')
    active_users = _prom_scalar('count(rl_counter_value)')

    allow_rate = round((allowed_rps / total_rps * 100) if total_rps > 0 else 0.0, 1)
    total_rps_r = round(total_rps, 2)

    # Range history for sparklines
    rps_spark = _prom_range('sum(rate(rl_requests_total[1m]))')
    allow_spark = _prom_range(
        'sum(rate(rl_requests_total{decision="allowed"}[1m])) / sum(rate(rl_requests_total[1m])) * 100'
    )
    users_spark = _prom_range('count(rl_counter_value)')

    # Decision count (last 24h decisions total)
    recent_decisions = _count_recent_decisions()

    return jsonify({
        "total_rps": total_rps_r,
        "allow_rate": allow_rate,
        "active_users": int(active_users),
        "recent_decisions": recent_decisions,
        "sparklines": {
            "rps": rps_spark[-20:] if rps_spark else [],
            "allow_rate": allow_spark[-20:] if allow_spark else [],
            "users": users_spark[-20:] if users_spark else [],
        },
        "ts": int(time.time() * 1000),
    })


@app.route("/api/decisions")
def api_decisions():
    entries = []
    try:
        path = DECISIONS_PATH if DECISIONS_PATH.is_absolute() else Path.cwd() / DECISIONS_PATH
        if not path.exists():
            # Try relative to this file
            path = Path(__file__).parent / "decisions.jsonl"
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            # Parse last 100 lines, extract individual decisions
            for line in lines[-100:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    tick = json.loads(line)
                    ts = tick.get("timestamp", 0)
                    for dec in tick.get("decisions", []):
                        entries.append({
                            "ts": ts,
                            "tick": tick.get("tick", 0),
                            "type": dec.get("type", ""),
                            "region": dec.get("region", ""),
                            "tier": dec.get("tier", ""),
                            "user_id": dec.get("user_id"),
                            "limit_per_minute": dec.get("limit_per_minute"),
                            "reason": dec.get("reason", ""),
                        })
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception as e:
        return jsonify({"error": str(e), "decisions": []}), 200

    # Return newest first, last 20
    entries.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify(entries[:20])


@app.route("/api/overrides")
def api_overrides():
    overrides = []
    for region in ("us", "eu", "asia"):
        def fetch(client, r=region):
            keys = client.keys("override:*")
            result = []
            for key in keys:
                try:
                    raw = client.get(key)
                    if raw:
                        data = json.loads(raw)
                        user_id = key.split(":", 1)[1] if ":" in key else key
                        ttl = client.ttl(key)
                        result.append({
                            "user_id": user_id,
                            "limit": data.get("limit_per_minute"),
                            "reason": data.get("reason", ""),
                            "ttl": ttl if ttl >= 0 else None,
                            "region": r,
                        })
                except (json.JSONDecodeError, Exception):
                    continue
            return result

        region_overrides = _safe_redis(region, fetch) or []
        for ov in region_overrides:
            # Deduplicate by user_id (same override may be in multiple regions)
            if not any(x["user_id"] == ov["user_id"] for x in overrides):
                overrides.append(ov)

    return jsonify(overrides)


@app.route("/api/counters")
def api_counters():
    """
    Returns top users by global counter value.
    rl:global:{tier}:{user_id} is a hash with slots: us, eu, asia.
    We scan US Redis (authoritative for global keys) and aggregate.
    """
    counters = []

    def fetch_global(client):
        keys = client.keys("rl:global:*")
        result = []
        for key in keys:
            try:
                slots = client.hgetall(key)
                parts = key.split(":")  # rl:global:{tier}:{user_id}
                if len(parts) < 4:
                    continue
                tier = parts[2]
                user_id = ":".join(parts[3:])
                us_val = int(slots.get("us", 0))
                eu_val = int(slots.get("eu", 0))
                asia_val = int(slots.get("asia", 0))
                total = us_val + eu_val + asia_val
                if total > 0:
                    result.append({
                        "user_id": user_id,
                        "tier": tier,
                        "us": us_val,
                        "eu": eu_val,
                        "asia": asia_val,
                        "total": total,
                    })
            except Exception:
                continue
        return result

    # Query all three Redis instances and merge
    seen = set()
    for region in ("us", "eu", "asia"):
        entries = _safe_redis(region, fetch_global) or []
        for entry in entries:
            key = (entry["user_id"], entry["tier"])
            if key not in seen:
                seen.add(key)
                counters.append(entry)

    # Sort by total desc, return top 20
    counters.sort(key=lambda x: -x["total"])
    return jsonify(counters[:20])


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "ts": int(time.time() * 1000)})

# ── Internal helpers ──────────────────────────────────────────────────────────

def _count_recent_decisions() -> int:
    """Count decisions written in the last 24 hours."""
    cutoff = (time.time() - 86400) * 1000
    total = 0
    try:
        path = DECISIONS_PATH if DECISIONS_PATH.is_absolute() else Path.cwd() / DECISIONS_PATH
        if not path.exists():
            path = Path(__file__).parent / "decisions.jsonl"
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        tick = json.loads(line)
                        if tick.get("timestamp", 0) >= cutoff:
                            total += len(tick.get("decisions", []))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass
    return total


if __name__ == "__main__":
    port = int(os.getenv("API_PORT", "5001"))
    print(f"Dashboard API starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
