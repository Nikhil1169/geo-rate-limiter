# Demo Prep — Geo-Distributed Rate Limiter

Source of truth for the Phase 9 live demo. Captures what's shipped, what's
still broken, the recommended presentation flow, and the order in which the
remaining issues should be fixed.

Last updated: 2026-05-10 (post round-1 fixes). Reflects PR #1 commits, the
post-merge re-run audit, and Round 1 follow-up fixes (B + C).

---

## 1. Purpose

Get from "stack runs" to "stack tells a clear story in 8 minutes" without the
presenter having to apologize for visible bugs, mistimed scenarios, or stale
panel content.

Audience for the demo is mixed engineering / non-engineering — they need to see
*the agent doing something useful in real time*, not raw Prometheus numbers.

---

## 2. What's already shipped (PR #1 — `fix/demo-stability`)

7 commits + 1 docs commit, all pushed to `yashashav-dk:fix/demo-stability`,
PR open against `Nikhil1169:main`.

| Commit | What it fixed |
|---|---|
| `b47ce3f` | Production compose connectivity: hardcoded `localhost` in sim/agent/seed; api container had wrong Redis ports for EU/Asia; missing `REDIS_*_ADDR` + `PROM_URL` env aliases needed by the agent subprocess. |
| `023c9fe` | `.gitignore` for local Playwright screenshots + `*.png`. |
| `60be356` | Decisions table flex-overflow at narrow viewports; replaced broken `docker ps` shell-out container-health probe with in-network Redis PING + gateway `/health` + Prometheus `/-/ready`. |
| `3518610` | `allow_rate` clipped to ≤100 (transient Prom rate-window glitch was producing 206.5%); `override:*` keys scrubbed at scenario start so abuser throttles from one scenario don't bleed into the next. |
| `e228c05` | Rule 3 (noisy-neighbor override) now requires absolute per-user ≥60 rpm in addition to >30% share — kills sparse-region false positives in Asia. `global_steady` recalibrated from 10/6/4 rps to 3/2/1.5 rps so per-tier RPS stays under seeded limits. Corrected `region_failover` description to use the real `gateway-us` container name. |
| `c388049` | This spec. |
| `b5be53e` | **Round 1 — Issue B**: Rule 3 (noisy_neighbor override) moved above Rule 4's hysteresis gate so per-user throttles fire even when (region, tier) is locked out of tier-policy changes. Demo storyline now plays out by t≈75 s instead of waiting ~130 s. New unit test covers the tick-1 → tick-2 sequence. |
| `89fd850` | **Round 1 — Issue C**: `/api/decisions` filters to current-scenario timestamps by default so the Live Agent Decisions panel doesn't show prior-scenario reasons. Tracks `_scenario_started_ms` set when `/api/control/scenario` fires. `?since=<ms>` overrides; `?since=0` returns full history. |

---

## 3. Architecture quick map (for the presenter)

```
            ┌──────────────────────────────────────────────┐
            │   dashboard (nginx :8080) ── proxies /api/   │
            └────────────────────┬─────────────────────────┘
                                 │
                       ┌─────────┴──────────┐
                       │   api (Flask :5001)│
                       │   • read endpoints │
                       │   • spawns sim     │
                       │   • spawns agent   │
                       └──┬──────┬──────┬───┘
                          │      │      │
                          ▼      ▼      ▼
                ┌─────────────┐  ┌────────────┐  ┌───────────┐
                │ gateway-us  │  │ gateway-eu │  │ gtwy-asia │   (Go, Gin)
                │   :8081     │  │   :8082    │  │   :8083   │
                └──────┬──────┘  └─────┬──────┘  └─────┬─────┘
                       │               │                │
                  ┌────▼───┐       ┌───▼────┐      ┌────▼────┐
                  │redis-us│       │redis-eu│      │redis-as │
                  │ :6379  │       │ :6380  │      │ :6381   │
                  └────────┘       └────────┘      └─────────┘
                       ▲               ▲                ▲
                       └────── sync-{us,eu,asia} ───────┘
                            (CRDT G-counter merge)

                   prometheus :9090  (scrapes gateways + sync)
                   grafana    :3000  (admin/admin)
```

**Three planes**:

- *Data plane* — gateway processes `/check` requests, Redis stores `rl:local:*`
  token buckets and `rl:global:*` G-counters.
- *Sync plane* — `sync-{region}` merges G-counter slots across regions every
  30 s + on pub/sub.
- *Control plane* — `agent/loop.py` ticks every 15 s. Per tick: read
  Prometheus features, fit predictors (EWMA / Holt-Winters), score anomalies
  (Isolation Forest), apply 4-rule decider, write `policy:*` and `override:*`
  back to Redis.

The data plane is unaware of the agent — gateway just reads whatever
`policy:{region}:{tier}` and `override:{user_id}` are currently in Redis.

---

## 4. The four scenarios

All four are defined in `simulator/scenarios.py` and dispatched via
`POST /api/control/scenario`.

| Scenario | Length | Story | What dashboard should show |
|---|---|---|---|
| `global_steady` | 300 s | Calm baseline + diurnal sine | RPS ≈ 6.5, allow 100%, no decisions, no overrides — *just observation* |
| `noisy_neighbor` | 120 s | Two abusers (`free_00001` 35%, `free_00002` 25%) saturate US free | Latency mountain → agent overrides both abusers @30 rpm → latency recovers |
| `product_launch` | 150 s | US baseline 30 s @5 rps, then spike 120 s @50 rps | Tier-level POLICY decision drops free limit, premium gets +10% compensation |
| `region_failover` | 120 s | All three regions, manual `docker stop gateway-us` mid-run | Container Health pill flips → US RPS drops cleanly → recover on `docker start` |

---

## 5. Open issues (post-PR-#1)

### 5a. Demo-blocking

| ID | Status | Issue | Where |
|---|---|---|---|
| ~~**A**~~ | **Retracted (round 1)** | Sim throughput was a false alarm. Re-measured at 53-56 rps total during product_launch spike phase (96% of 58 rps target); US alone hits 47 rps consistently. Earlier 7.4 rps reading was a one-off, likely from a stale low policy still in Redis from a prior failed run. No code change needed. | n/a |
| ~~**B**~~ | **Fixed (`b5be53e`)** | Rule 3 moved above Rule 4 hysteresis gate. noisy_neighbor now overrides both bias users by t≈75 s on a warm agent. Verified live + unit-tested. | `agent/decider.py` |
| ~~**C**~~ | **Fixed (`89fd850`)** | `/api/decisions` defaults to filtering by `_scenario_started_ms`. Switching from noisy_neighbor → product_launch now shows count=0 immediately and contains only current-scenario reasons once the agent ticks. | `agent/api.py` |
| **H** *(new)* | High | **Prometheus `rl_counter_value` gauge staleness leaks across scenarios.** Surfaced during Issue C verification. After noisy_neighbor ends, the gateway stops emitting `rl_counter_value` for `free_00001/00002`, but Prometheus retains the last-seen value for ~5 min via the default stale-marker. The agent reads these as live top users on every tick of the *next* scenario and fires Rule 3 overrides for users the new sim isn't even biasing. Visible symptom: product_launch shows `noisy_neighbor_free_00001` overrides with timestamps after the scenario switch. | gateway emission policy + Prometheus retention |

### 5b. UX rough edges

| ID | Severity | Issue | Where |
|---|---|---|---|
| **D** | Low | "Active overrides — Agent not running or warming up" empty-state shows even when agent IS running with zero overrides. Should distinguish *no agent* vs *agent up, no overrides*. | `dashboard.html` Active Overrides panel |
| **E** | Low | Tier Latency tile reads 0 ms after spike passes — moment-only, no peak preserved. Demo viewer can miss the storyline if they look up a few seconds late. | `dashboard.html` MetricCard for tier latency |
| **F** | Low | Dashboard container reports `unhealthy` via `docker ps` even though the page works fine. `wget --spider` quirk on nginx. Cosmetic but distracting if the presenter runs `docker ps` on screen. | `docker-compose.production.yml` healthcheck |
| **G** | Low | `holtwinters` predictor selectable in the dropdown but never tested end-to-end in this audit cycle. May or may not work. | `agent/predictor.py`, control sidebar |

### 5c. Verified non-issues (audit retractions)

| Was flagged | Verdict |
|---|---|
| `rl_decision_duration_seconds` missing from Prom | **False finding** — histogram exposes `_bucket`/`_sum`/`_count`, not bare metric. Audit query was wrong. Contract 3 is satisfied. |
| `active_users` reads 0 | Prometheus query timing — recovers within a few ticks. Not a real bug. |
| Model panel empty `{}` after agent restart | Already shows "warming up (n/8)" once the predictor has a few samples. UX is correct. |
| Sim throughput cap (was Issue A) | **Retracted** — re-measured at 53-56 rps total / 47 rps US during spike (96% of 58 rps target). Original 7.4 rps reading was a one-off from a stale low policy in Redis. |

### 5d. Cross-cutting backlog (not demo-critical)

- No automated end-to-end test for the dashboard control endpoints (`/api/control/*`).
- `_clear_overrides()` scrubs only `override:*`; doesn't reset `_policy_ts` /
  `_override_ts` hysteresis state inside the running agent. Killing + restarting
  the agent between scenarios is the workaround.
- `decisions.jsonl` grows unbounded — fine for a demo but needs rotation for
  any real run.

---

## 6. Recommended demo flow

Total: 8 minutes. Assumes the stack is already up, agent already running for
≥1 minute (so predictor isn't cold).

### Pre-demo checklist (run 5 minutes before going live)

```bash
# 1. Bring stack up
cd geo-rate-limiter
docker compose -f docker-compose.production.yml up -d --build

# 2. Wait for all services
sleep 10
docker compose -f docker-compose.production.yml ps

# 3. Seed policies + start agent
curl -sX POST http://localhost:5001/api/control/policies/seed
curl -sX POST http://localhost:5001/api/control/agent/start

# 4. Warm the predictor — 90 s of global_steady
curl -sX POST http://localhost:5001/api/control/scenario \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"global_steady"}'
sleep 90

# 5. Confirm Container Health green, agent running, predictor has samples
open http://localhost:8080
```

If any of step 5 looks wrong, run `Flush All Redis` from the dashboard,
re-seed, restart agent, redo step 4.

### Live narration (8 min)

| Minute | Scenario | What to say while it runs |
|---|---|---|
| 0:00 — 0:30 | (already on `global_steady`) | "Three regions — US, EU, Asia. Each gateway is independent. The agent watches Prometheus and decides if it needs to step in. Right now: 6 RPS, 100% allow, no agent decisions. Steady state." |
| 0:30 — 2:30 | `noisy_neighbor` | "Now I drop two abusers into US free tier — `free_00001` and `free_00002` between them take 60% of the tier traffic. Watch the latency tile climb. Around 30 seconds in the agent's predictor sees the spike — fires a tier-level policy. About a minute later — once the per-tier hysteresis clears — the agent identifies the *individual* abusers and pins them at 30/min. Latency drops back to baseline. The legitimate users `free_00003`–`00008` are unaffected." |
| 2:30 — 4:30 | `product_launch` | "Different pattern — not abuse, just a real spike. 30 second baseline, then a 10× burst on US for two minutes. Predictor catches it, lowers free-tier limit, *raises* premium-tier limit by 10% to absorb the overflow. Different rule, different remediation." (*Caveat: spike doesn't reach 50 rps target due to issue A — narrate the policy decisions instead of the raw RPS.*) |
| 4:30 — 6:30 | `region_failover` | "Regions are independent. I'll kill US gateway." (*`docker stop gateway-us` from a side terminal*) "Container Health pill flips. EU and Asia keep serving. Now I bring it back." (*`docker start gateway-us`*) "All-green. The agent never had to do anything — the gateway-region isolation just works." |
| 6:30 — 8:00 | Q&A + show Grafana :3000 | "Same metrics, prebuilt dashboard. 25 panels covering CRDT sync lag, policy versions, decision latency histograms." |

### Backup talk-track

If sim stalls or a scenario stops emitting, switch to:

- *Show the policy seed*: `redis-cli -p 6379 KEYS 'policy:*'` then `GET policy:us:free` — show the JSON contract.
- *Show the agent decision log*: `tail -f agent/decisions.jsonl | jq` from a terminal.
- *Show the CRDT G-counter*: `redis-cli KEYS 'rl:global:*' | head` then HGETALL on one — three slots, one per region.

---

## 7. Implementation roadmap (in suggested order)

Each row is a single PR-sized chunk. Acceptance criteria shown in *italics*.

### Round 1 — demo-blocking ✅ done

1. ~~**Issue B — Rule ordering / hysteresis interaction.**~~ Shipped in
   `b5be53e`. Rule 3 moved above Rule 4 hysteresis gate; per-user overrides
   fire independent of tier-policy lockout. Verified live at t≈75 s.
2. ~~**Issue A — Sim throughput.**~~ Retracted, see §5c.
3. ~~**Issue C — Scenario-scoped decisions feed.**~~ Shipped in `89fd850`.
   `/api/decisions` defaults to `ts >= _scenario_started_ms`.

### Round 2 — Prom staleness + UX polish

4. **Issue H — Prometheus `rl_counter_value` cross-scenario leak** (high impact)
   - Surfaced during Round 1 verification. Two practical options:
     - H1: Set Prometheus `--query.lookback-delta=30s` in
       `docker-compose.production.yml` (default is 5 min; tighter delta makes
       Prom mark gauges stale much sooner once gateway stops emitting).
     - H2: Have the gateway proactively `Delete` `rl_counter_value` for users
       whose Redis G-counter falls below the 50% sampling threshold, so the
       gauge series simply ends instead of going stale.
     - H1 is one line, H2 is more correct. Start with H1 for the demo.
   - *Acceptance: switching scenarios with no shared bias users no longer
     produces phantom Rule 3 overrides for users from the prior scenario.*

5. **Issue E — Latency tile peak preservation**
   - Add a `peak60s` sub-stat to the Tier Latency `MetricCard`.
   - *Acceptance: tile shows current value AND "peak 161ms 30s ago" sub-stat.*

6. **Issue D — "Agent not running or warming up" misleading text**
   - Pass `agentRunning` prop into Active Overrides panel; show
     "No active overrides" when `agentRunning && !overrides.length`.
   - *Acceptance: panel reads "No active overrides" during global_steady.*

7. **Issue F — Dashboard healthcheck false negative**
   - Replace `wget --spider` with `wget -q -O /dev/null http://localhost/`
     in `docker-compose.production.yml`.
   - *Acceptance: `docker compose ps` shows dashboard `(healthy)`.*

### Round 3 — completeness ✅ done

8. ~~**Issue G — Holt-Winters predictor end-to-end test.**~~ Tested. See
   §10 below.

---

## 8. Risks and recovery during the demo

| If this happens | Do this |
|---|---|
| Dashboard shows "Stopped" agent and you didn't stop it | Click `Start Agent` again. The subprocess died — usually OOM or import error. Check `docker logs api`. |
| Scenario button does nothing visible after click | Check `Running · pid N` text under the scenario card; if missing, the previous sim is still alive. Kill via `docker exec api sh -c "kill <pid>"` or restart the api container. |
| Container Health pill goes red unexpectedly | One of the gateways crashed. `docker compose restart gateway-us` (or eu/asia). Probably won't recover the in-flight sim — start the scenario fresh. |
| `allow_rate` reads weird value (e.g. negative) | Should not happen post-PR-#1 (`min(100, max(0, ...))`). If it does: agent is writing bad policy IDs that make Prom labels diverge. Stop the agent, re-seed policies. |
| Stack hangs entirely | `docker compose -f docker-compose.production.yml restart`. Total downtime ~15 s. Re-warm the agent for 60 s before resuming the demo. |

---

## 9. Open questions for the team

- **Throughput target for product_launch.** 50 rps is currently aspirational.
  Should we lower the scenario to a sustainable 25 rps (still a 5× spike), or
  invest in fixing the sim throughput properly?
- **Agent warm-up time.** Predictor needs 8 samples = 2 minutes at 15 s tick.
  Is it worth pre-loading a synthetic warm history at agent start so demo
  scenarios don't depend on a warm-up phase?
- **Doc location for failure modes.** `docs/failure-modes.md` covers what
  happens when subsystems break; this `demo-prep.md` covers presentation.
  Eventually consolidate into one Runbook?

---

## 10. Predictor comparison (Issue G test results)

End-to-end run through all four scenarios with `predictor=holtwinters`,
agent warmed for 10+ minutes (HW requires 40 samples at 15 s tick before
`fit()` returns a fitted model).

| Scenario | EWMA | Holt-Winters |
|---|---|---|
| `global_steady` warm-up | 0 decisions, calm | **6 false predicted_spike** decisions on near-zero idle data — seasonal/trend components amplify noise |
| `noisy_neighbor` | Both abusers throttled @ 30/min by t≈75 s, no collateral | Both abusers throttled ✓ BUT collateral **eu/free → 5/min** false-spike (EU is at baseline 5 rps with no anomaly) |
| `product_launch` | Single Rule 1 cascade (free → 210/min, premium +10%); MAE 0.5414 | Triple Rule 1 cascade (free → 147 → 102 → 71/min) PLUS false-spikes for EU and Asia; MAE 2.7371 (5× worse); HW overpredicts 3× (last_pred 116.8 RPS vs last_actual 41.0) |
| `region_failover` | Clean US-out / US-in handling | Identical — failover behaviour is not predictor-dependent |
| Confidence interval bounds | n/a (EWMA doesn't produce CI) | **None** in practice — `simulate()` is silently catching an exception inside `predictor.py`, so the dashboard CI bands never light up |

**Verdict**: keep EWMA as the default for the live demo. Holt-Winters
catches the noisy_neighbor signal correctly but is far too aggressive
everywhere else — every region/tier with non-trivial baseline traffic
triggers false `predicted_spike_*` decisions, the per-tier policy
cascades 3-deep before the 60 s tier hysteresis takes effect, and the
CI bands that justify the model's complexity never actually appear in
the UI. The HW dropdown option should either be hidden until the CI
exception is fixed or relabelled `holtwinters (experimental)`.

Recommended follow-ups if HW is kept on the menu:
- Catch and surface the `simulate()` exception so CI bands work.
- Add a `min_signal_rpm` floor (similar to Rule 3's `_NOISY_USER_MIN_RPM`)
  so HW doesn't fire predicted_spike on regions whose RPS is comfortably
  under their seeded limit even with the trend component included.
- Reduce `seasonal_periods` from 20 to 4 (1 minute) so the model doesn't
  imprint a 5-minute pattern on traffic that has no such cycle.
