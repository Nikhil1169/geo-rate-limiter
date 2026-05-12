# Contracts

Every component in this repo must conform to all four contracts below.

---

## Contract 1 — Gateway HTTP API

POST /check
Request:  { "user_id": str, "tier": "free"|"premium"|"internal",
            "region": "us"|"eu"|"asia", "endpoint": str }
Response: { "allowed": bool, "remaining": int, "limit": int,
            "retry_after_ms": int, "policy_id": str }
Always returns HTTP 200; allowed=false means rate-limited.
Plus: GET /health, GET /metrics (Prometheus format)

---

## Contract 2 — Redis key schema

rl:local:{region}:{tier}:{user_id}      hash {tokens, last_refill_ms}, TTL=120s
rl:global:{tier}:{user_id}              hash {us, eu, asia} (G-Counter slots)
policy:{region}:{tier}                  JSON string (Contract 4)
override:{user_id}                      JSON string {limit_per_minute, ttl, reason}

---

## Contract 3 — Prometheus metrics

rl_requests_total{region, tier, endpoint, decision}    counter
rl_decision_duration_seconds{region}                   histogram
rl_counter_value{region, tier, user_id}                gauge (sampled, top-N)
rl_sync_lag_seconds{from_region, to_region}            gauge
rl_policy_version{region, tier}                        gauge

---

## Contract 4 — Policy JSON

{
  "policy_id": "pol_<timestamp>_<seq>",
  "region": "us"|"eu"|"asia",
  "tier": "free"|"premium"|"internal",
  "limit_per_minute": int,
  "burst": int,
  "algorithm": "token_bucket"|"sliding_window",
  "ttl_seconds": int,
  "reason": str,
  "created_at": ISO8601 string
}
