
# Claude Code working notes

## Project
Geo-distributed rate limiter with AI traffic shaping. Solo build.
A distributed rate limiting platform with three regional API gateways 
(US, EU, Asia), CRDT-based cross-region counter sync, a traffic simulator, 
and an AI agent that predicts spikes and dynamically adjusts rate limits 
per tier and region. The agent is part of the control plane — it actually 
changes enforced limits, not just dashboards them.

## Stack (do not deviate without asking)
- Gateway: Go 1.22+, Gin, go-redis/v9, prometheus/client_golang
- Sync service, simulator, agent: Python 3.11+
- Counter store: Redis 7 (three instances, one per region)
- Sync transport: Redis pub/sub + periodic reconciliation
- Metrics: Prometheus + Grafana
- Orchestration: docker-compose
- Repo: monorepo

## The four contracts (every component must conform)

### Contract 1 — Gateway HTTP API
POST /check
Request:  { "user_id": str, "tier": "free"|"premium"|"internal",
            "region": "us"|"eu"|"asia", "endpoint": str }
Response: { "allowed": bool, "remaining": int, "limit": int,
            "retry_after_ms": int, "policy_id": str }
Always returns HTTP 200; allowed=false means rate-limited.
Plus: GET /health, GET /metrics (Prometheus format)

### Contract 2 — Redis key schema
rl:local:{region}:{tier}:{user_id}      hash {tokens, last_refill_ms}, TTL=120s
rl:global:{tier}:{user_id}              hash {us, eu, asia} (G-Counter slots)
policy:{region}:{tier}                  JSON string (Contract 4)
override:{user_id}                      JSON string {limit_per_minute, ttl, reason}

### Contract 3 — Prometheus metrics
rl_requests_total{region, tier, endpoint, decision}    counter
rl_decision_duration_seconds{region}                   histogram
rl_counter_value{region, tier, user_id}                gauge (sampled, top-N)
rl_sync_lag_seconds{from_region, to_region}            gauge
rl_policy_version{region, tier}                        gauge

### Contract 4 — Policy JSON
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

## Working rules
1. After every meaningful change, run the relevant test or smoke check 
   (curl, redis-cli, etc.) and show me the output.
2. Before writing more than ~50 lines in a new file, show me the structure 
   first and wait for confirmation.
3. If you hit an ambiguous design choice not covered by the contracts, STOP 
   and ask. Do not guess.
4. Use docker-compose for everything.
5. Write tests as you go — at minimum, unit tests for the rate limiter 
   algorithms and the CRDT merge logic.
6. Commit after each phase with conventional commits (feat:, fix:, test:, docs:).
7. Don't refactor untouched code unless I explicitly ask.

## Completed phases (do not re-do these)
- Phase 1 ✓ — Infrastructure scaffolding
- Phase 2 ✓ — Gateway with atomic token bucket
- Phase 3 ✓ — G-Counter CRDT cross-region sync and global enforcement
- Phase 4 ✓ — Sliding window algorithm, dynamic policy plane, sync NOSCRIPT fix
- Phase 5 ✓ — Traffic simulator with Poisson patterns and predefined scenarios

## Phase
Currently on: Phase 6 — Grafana dashboard provisioning. Don't jump ahead.

## Environment note
Host machine has a local Homebrew Redis on port 6379.
ALWAYS use `docker exec redis-us redis-cli` to target Docker Redis.
ALWAYS run cross-region tests without sleep between US and EU requests
— the rate limit window is 60 seconds and tests that pause mid-way
will hit a new window and produce false positives.
