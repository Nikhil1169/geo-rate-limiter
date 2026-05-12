# Failure Modes and Recovery

This document describes how the system behaves when components fail and how it recovers.

## Test 1: Redis Instance Failure

**Scenario:** Redis instance (redis-us) killed mid-traffic

**Observed behavior:**
- Gateway immediately fell back to static default policies (free=10/min instead of 300/min)
- All override lookups failed gracefully with "skipping override" 
- Traffic continued with 0 connection errors
- Latency increased significantly (p50: 154ms, p95: 1143ms) due to Redis retry attempts
- 33.5% of requests allowed vs 66.5% denied (static limits much stricter than demo baselines)

**Recovery:**
- Gateway did NOT auto-reconnect after `docker start redis-us`
- Required gateway restart to re-establish Redis connection
- Issue: Docker DNS cache doesn't refresh automatically

**Impact:** Service degraded but not unavailable. Users experience stricter rate limits.

---

## Test 2: Gateway Instance Failure

**Scenario:** US gateway killed while distributing traffic 50% US / 30% EU / 20% Asia

**Observed behavior:**
- First 25 seconds: 100% success (603 requests)
- After killing US gateway: 398 errors (requests to dead gateway)
- EU and Asia gateways continued serving traffic
- Overall success rate: 76% despite losing 50% of capacity

**Recovery:**
- Simulator crashed with `httpx.ReadError` (not graceful degradation)
- In production, client would need circuit breaker logic to detect dead gateway and reroute

**Impact:** Partial outage. Traffic to failed region lost, other regions unaffected.

---

## Test 3: Policy Expiry (Agent Crash Simulation)

**Scenario:** Policy key deleted from Redis (simulating TTL expiry or agent crash)

**Observed behavior:**
- Gateway detected missing policy
- Fell back to hardcoded static defaults (free=10/min, premium=100/min, internal=1000/min)
- Requests succeeded with `"policy_id": "pol_static_free"`
- No errors returned to clients

**Recovery:**
- Automatic: Gateway uses static fallback until policy restored
- Manual: Agent restart or manual policy write to Redis

**Impact:** Service continues with conservative static limits. No downtime.

---

## Summary

| Component | Failure Mode | Auto-Recovery | Impact |
|-----------|--------------|---------------|---------|
| Redis | Connection lost | No (needs gateway restart) | Degraded (static limits) |
| Gateway | Process crash | No (manual restart) | Partial outage (region-specific) |
| Agent | Process crash | Yes (policies expire, fallback activates) | Degraded (static limits) |
| Policy | Expired/deleted | Yes (static fallback) | Degraded (static limits) |

**Key insight:** The system prioritizes availability over strict rate limiting. When control plane components fail, the gateway falls back to permissive static limits rather than denying all traffic.
