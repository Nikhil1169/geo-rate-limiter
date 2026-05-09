# Phase 3 — Cross-Region Sync Design

## Problem

Three regional gateways (us, eu, asia) enforced rate limits in isolation.
Each wrote only to `rl:local:{region}:{tier}:{user_id}` in its own Redis.
A user capped at 10 req/min could reach 30 req/min by spreading load across
all three regions.

## Solution: G-Counter CRDT

A G-Counter is a vector of per-region counts.  Each region only increments its
own slot.  The global count is the sum of all slots.  Merging two G-Counters
takes the per-slot maximum — this is associative, commutative, and idempotent,
so updates can be applied in any order without locks or a leader.

### Why CRDT instead of a shared counter?

| Approach | Trade-off |
|---|---|
| Central shared Redis | Single point of failure; global locking on every request |
| Cross-region read on every /check | 50–200 ms added to the hot path |
| G-Counter CRDT (chosen) | Local read on hot path; eventual consistency; no coordinator |

The G-Counter gives each region full autonomy.  Under partition, each region
continues to enforce its local bucket and a slightly stale global view.
When the partition heals, state converges automatically.

## Data Model

```
Key:    rl:global:{tier}:{user_id}:{window_id}   (Redis hash, TTL 120 s)
Fields: us, eu, asia                              (absolute counts, monotonic)

window_id = floor(unix_time_seconds / 60)         (minute-of-epoch)
```

`window_id` is embedded in the key, not as a hash field, so each minute gets
its own independent G-Counter.  Slots stay monotonically increasing within
a window; old windows expire via TTL.  No reset logic needed.

**Example — user `u42`, free tier, window 30412345, fully synced:**

```
redis-us    rl:global:free:u42:30412345  →  {us: 6, eu: 2, asia: 0}
redis-eu    rl:global:free:u42:30412345  →  {us: 6, eu: 2, asia: 0}
redis-asia  rl:global:free:u42:30412345  →  {us: 6, eu: 2, asia: 0}
```

Global sum = 8.  Limit = 10.  Next request allowed.

## Request Flow

```
client  ──POST /check──►  gateway-us
                           │
                           │ 1. Token bucket (Lua, local Redis)
                           │    → allowed / denied + remaining
                           │
                           │ 2. If allowed: HGETALL rl:global:... (local Redis)
                           │    → sum slots; if sum ≥ GlobalLimit → override denied
                           │
                           │ 3. If still allowed: HINCRBY us slot + EXPIRE
                           │    → PUBLISH rl:sync:counter (fire-and-forget)
                           │
                           │ 4. Respond 200 {allowed, remaining, limit, ...}
                           │
                           ▼
                  rl:sync:counter on redis-us
                           │
               ────────────┴────────────
               ▼                        ▼
         sync-eu                   sync-asia
    (subscribed to redis-us)  (subscribed to redis-us)
         │                              │
         │ merge.lua (max-per-slot)     │ merge.lua (max-per-slot)
         ▼                              ▼
       redis-eu updated            redis-asia updated
```

**Hot path cost:** one extra HGETALL + HINCRBY against local Redis (~5–15 µs).
No cross-region call during normal operation.

## Merge Function

`sync/merge.lua` — applied by sync service subscribers:

```lua
local current  = tonumber(redis.call('HGET', KEYS[1], ARGV[1])) or 0
local incoming = tonumber(ARGV[2])
if incoming > current then
  redis.call('HSET',   KEYS[1], ARGV[1], incoming)
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
  return incoming
end
return current
```

**Why max-per-slot is conflict-free:**
Each region writes only its own slot (via HINCRBY).  All other regions touch
that slot only via the merge, which is `max(a, b)`.  `max` is associative,
commutative, and idempotent — applying messages out-of-order, duplicated, or
in any combination always converges to the highest observed value.

Sync messages carry **absolute values**, not deltas.  A lost delta would
permanently undercount; a lost absolute value is fully corrected by the next
publish or by periodic reconciliation.

## Pub/Sub Transport

- Channel: `rl:sync:counter` (single channel on every Redis instance)
- Each sync service subscribes to **all three** Redises.
- Own-region messages are skipped (`region == my_region`).
- Messages from partitioned origins are dropped before merge (see below).

Message format:
```json
{
  "tier": "free",
  "user_id": "u42",
  "window_id": 30412345,
  "region": "us",
  "value": 6,
  "ts_ms": 1745654400000
}
```

## Periodic Reconciliation

Every 30 seconds each sync service:
1. SCANs local Redis for `rl:global:*:*:{current_window}`.
2. For each key, re-PUBLISHes its own region's current slot value.

This is idempotent (receivers apply max-merge) and recovers from:
- Missed pub/sub messages (Redis pub/sub is fire-and-forget, no persistence)
- Subscribers that restarted or were briefly partitioned
- Any lag between gateway increment and peer convergence

## Eventual Consistency Guarantee

After all writes stop and at least one reconciliation cycle completes
(≤ 30 s), all three regions hold identical G-Counter state.  The test
`test_heal_convergence` asserts this within a 5 s SLA for in-process
message delivery; in production the bound is network RTT + one
reconciliation interval.

Formally: for any two replicas R₁, R₂ and any slot s,
`R₁[s] = R₂[s]` after quiescence, because `max` is the unique idempotent
join operation on a total order.

## Partition Behavior

Simulated via the admin API on each sync service node:

```
POST /admin/partition {"from": "us", "to": "eu"}
  → sync-eu drops all messages originating from us (before applying merge)

POST /admin/heal      {"from": "us", "to": "eu"}
  → messages flow again; next reconciliation tick converges eu

GET  /admin/status
  → lists active (from, to) pairs on this node
```

Partitions are **asymmetric and per-node**.  Simulating a full network split
requires two calls — one in each direction.  After heal, convergence happens
within one reconciliation interval (30 s) without any manual intervention.

**During a partition** each region continues to enforce its local token bucket
and a stale global view.  A user could overshoot the global cap by at most:

```
overshoot ≤ (num_isolated_regions) × (requests_in_partition_window)
```

For steady-state pub/sub (no partition), staleness is bounded by network
propagation latency: ~1–10 ms intra-cluster.

## Staleness Window Analysis

| Tier | Limit | Max RPS per region | Overshoot per 500 ms | % error |
|---|---|---|---|---|
| free | 10/min | 0.17 | < 1 req | < 10% |
| premium | 100/min | 1.67 | ~1 req | ~1% |
| internal | 1000/min | 16.7 | ~8 req | ~0.8% |

*500 ms represents a conservative WAN propagation + processing bound.*

Acceptable for this project.  A stricter bound would require moving the
global check into the Lua script (one atomic transaction), at the cost of
including the global key in every Lua KEYS[] — a deliberate design trade-off.

## Files

| File | Role |
|---|---|
| `sync/merge.lua` | Atomic max-merge Lua script |
| `sync/counter.py` | `RegionalCounter` — async CRDT wrapper |
| `sync/sync_service.py` | Subscriber tasks, reconciliation loop, metrics |
| `sync/admin.py` | Partition simulation endpoints |
| `sync/tests/test_counter.py` | CRDT property proofs (10 unit tests) |
| `sync/tests/test_integration.py` | Pub/sub + partition/heal tests (6 tests) |
| `gateway/internal/handler/handler.go` | Global cap check + HINCRBY + PUBLISH |
| `gateway/internal/policy/policy.go` | `GlobalLimit` field (Contract 4 extension) |
