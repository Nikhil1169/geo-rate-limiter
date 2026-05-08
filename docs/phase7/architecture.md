# Phase 7 — AI Traffic Agent

## Context

Phases 1–6 built the data plane (3 Redis + 3 Go gateways + sync service +
Prometheus + Grafana) and a control plane stub: gateways already poll
`policy:{region}:{tier}` every 5s and read `override:{user_id}` per-request.
Phase 7 is the **brain**: an autonomous Python service that reads
Prometheus, predicts the next 2 minutes of traffic per (region, tier),
detects anomalies, applies a small rules engine, and writes new policies
to Redis. The gateway picks them up within 5s without restart.

This is the deliverable that makes the project "AI-driven" rather than
"a rate limiter with a dashboard." The decision log (`agent/decisions.jsonl`)
is also the demo script and the writeup's empirical backbone (predictor MAE
comparison in the eval notebook).

## Reality checks from exploration (must be reflected in the code)

These differ from the verbal spec — flagging them so we don't waste an
implementation cycle:

1. **Decision label values are `"allowed"`, `"denied"`, `"error"`** — *not* `"deny"`.
   PromQL filter must be `decision="denied"`. Source:
   [gateway/internal/handler/handler.go](gateway/internal/handler/handler.go) lines 191–198.
2. **Override JSON schema is `{limit_per_minute, ttl, reason}`** — the field
   is `ttl` (seconds), not `ttl_seconds`. Source:
   [gateway/internal/override/cache.go:19-23](gateway/internal/override/cache.go#L19-L23).
   Contract 4 (`ttl_seconds`) applies only to the `policy:` key.
3. **Static baselines are very low**: free=10/min, premium=100/min,
   internal=1000/min. RPS-to-limit comparison must do `rps_per_min = rps * 60`
   *or* convert limit to RPS (`limit / 60`). Either way, the units must match.
   Source: [gateway/internal/policy/policy.go:16-20](gateway/internal/policy/policy.go#L16-L20).
4. **policy_id sequence**: gateway parses last `_<n>` segment via `seqFromID`.
   We must use `pol_<unix_ms>_<seq>` and increment `seq` per write so the
   `rl_policy_version` gauge advances visibly.
5. **Network**: all services live on `georl_net` (docker-compose). Inside
   Docker, agent uses `redis-us:6379`, `prometheus:9090`. From host, use
   `localhost:{6379,6380,6381}` and `localhost:9090`.

## File structure

Host-only (mirrors `simulator/`); no Dockerfile, no docker-compose entry.
Connects to localhost:9090 (Prometheus), localhost:{6379,6380,6381} (Redis).

```
agent/
├── requirements.txt                 (new — populated)
├── pytest.ini                       (new — asyncio_mode = auto)
├── __init__.py                      (existing? verify; create if not)
├── main.py                          (replace stub — Click CLI entrypoint)
├── config.py                        (new — defaults + env-var overrides)
├── metrics_client.py                (new — Prometheus HTTP API wrapper)
├── predictor.py                     (new — Predictor base + EWMA + HoltWinters)
├── detector.py                      (new — IsolationForest spike detector)
├── decider.py                       (new — rules engine + hysteresis state)
├── policy_writer.py                 (new — Contract 4 JSON, multi-Redis writes)
├── decision_log.py                  (new — JSONL appender, flush on shutdown)
├── loop.py                          (new — orchestration, 15s tick)
├── agent_metrics.py                 (new — self-observability via prometheus-client; exposed on :9114 for local curl/debug)
├── notebooks/
│   └── predictor_eval.ipynb         (new — EWMA vs Holt-Winters MAE)
├── decisions.jsonl                  (runtime artifact, in .gitignore)
└── tests/
    ├── test_predictor.py            (synthetic series → known forecasts)
    ├── test_detector.py             (anomaly detection on injected spikes)
    ├── test_decider.py              (each of 4 rules + hysteresis)
    └── test_policy_writer.py        (fakeredis, Contract 4 round-trip)
```

Also touch:
- `tools/seed_policies.py` — extend (or add a sibling `seed_policies_demo.py`)
  to write higher demo baselines: free=300/min (5 RPS), premium=3000/min (50 RPS),
  internal=30000/min (500 RPS). The static fallbacks in
  [gateway/internal/policy/policy.go:16-20](gateway/internal/policy/policy.go#L16-L20)
  stay at 10/100/1000 — those are crash-safety defaults, not demo defaults.
- `.gitignore` — add `agent/decisions.jsonl`.

Splitting `main.py` (CLI) from `loop.py` (orchestration) keeps the loop
testable without spinning up Click.

## Predictor interface (`agent/predictor.py`)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class Forecast:
    point: float                    # predicted RPS at horizon
    lower: float | None             # 95% CI lower (None if predictor doesn't support)
    upper: float | None             # 95% CI upper
    horizon_seconds: int            # how far ahead this forecast applies

class Predictor(ABC):
    name: str

    @abstractmethod
    def fit(self, history: list[tuple[int, float]]) -> None:
        """history: list of (unix_ms, rps). May be empty or have gaps."""

    @abstractmethod
    def forecast(self, horizon_seconds: int = 120) -> Forecast | None:
        """Return None if insufficient data — caller handles gracefully."""

class EWMAPredictor(Predictor):
    name = "ewma"
    # alpha=0.3, point estimate is the smoothed value; no CI.

class HoltWintersPredictor(Predictor):
    name = "holtwinters"
    # statsmodels.tsa.holtwinters.ExponentialSmoothing
    # trend='add', seasonal='add', seasonal_periods=20  (= 5 min cycle at 15s tick)
    # CI from .get_prediction(...).conf_int()
```

**Holt-Winters minimum-samples rule:** `2 * seasonal_periods = 40` samples =
10 minutes of history before HW will fit. Below that threshold,
`HoltWintersPredictor.forecast()` returns `None` and the loop transparently
falls back to the latest observation. The 30-min history buffer
(`window_s=1800` = 120 samples) gives HW 3× its minimum once warmed up,
which is on the lower end but acceptable for Phase 7. (For a longer demo,
consider extending `window_s` to 3600.)

**Predictor instancing — 9 separate instances, one per (region, tier).**
Each (region, tier) pair is a fully independent forecaster: independent
EWMA smoothed state, independent Holt-Winters level/trend/seasonal
components, independent history buffer. Built once at startup:

```python
predictors: dict[tuple[str, str], Predictor] = {
    (r, t): build_predictor(predictor_name)
    for r in REGIONS for t in TIERS
}
histories:  dict[tuple[str, str], deque] = {
    (r, t): deque(maxlen=120)        # 30 min @ 15s ticks
    for r in REGIONS for t in TIERS
}
```

Rationale:
- The three tiers differ by 10× in magnitude (demo baselines: 300 / 3000 /
  30000 rpm). A shared smoother would have a single `level` parameter that
  can't simultaneously fit all three.
- Tier traffic shapes are qualitatively different: free is bursty/social,
  internal is more uniform/scheduled. Trend and seasonal components should
  be free to differ.
- Region-level differences (timezone offsets, scenario targeting) mean even
  same-tier series across regions evolve independently.
- Cost is trivial: 9 × (a single deque + a few floats for EWMA, or a
  statsmodels `ExponentialSmoothing` fit object for HW). Refit happens
  every tick in <1ms for EWMA and a few ms for HW; negligible at 15s ticks.

`HistoryBuffer` in the loop is therefore a `dict[(region, tier) → deque]`,
not a single deque. Each `predictor[r,t].fit(history[r,t])` call gets its
own series.

**Missing-data handling:** `metrics_client` returns `None` for gaps;
predictor's `fit` skips them; if fewer than `min_samples` (8 for EWMA,
2 × seasonal_periods for HW) remain, `forecast()` returns `None` and the
decider falls back to using the latest observation directly.

## Decision rules (pseudocode, from `agent/decider.py`)

Inputs per tick: `obs[region][tier] = {rps, rejection_rate, top_users}`,
`pred[region][tier] = Forecast | None`, `current_policy[region][tier]` (read
from Redis or static fallback), `last_decision_ts[(region, tier)]`.

**Unit convention** (load-bearing — see "reality check #3" above): all
internal comparisons happen in **requests-per-minute (rpm)**. RPS values
from Prometheus get multiplied by 60 at the boundary in `decider.decide()`.
Limits in policies are already rpm. There is no implicit conversion deeper
in the code; if you see `rps` and `rpm` mixed, that's a bug.

`DEMO_BASELINE = {"free": 300, "premium": 3000, "internal": 30000}` (rpm).
Loaded from `agent/config.py`; matches what `tools/seed_policies.py` writes
on first boot.

```
for region in [us, eu, asia]:
    for tier in [free, premium, internal]:
        cur_limit_rpm   = current_policy[region][tier].limit_per_minute
        observed_rpm    = obs[region][tier].rps * 60.0
        forecast_rpm    = (pred[region][tier].point * 60.0) if pred[region][tier] else observed_rpm
        rej             = obs[region][tier].rejection_rate
        last_change     = last_decision_ts.get((region, tier))

        # Rule 4 — hysteresis gate (applies to Rules 1 and 2 only; Rule 3 has its own)
        if last_change and (now - last_change) < 60:
            continue

        # Rule 1 — predicted spike mitigation (free tier only)
        if tier == "free":
            premium_rej = obs[region]["premium"].rejection_rate
            if forecast_rpm > 0.8 * cur_limit_rpm and premium_rej < 0.10:
                new_free_rpm    = max(STATIC_FLOOR["free"], int(cur_limit_rpm * 0.70))
                new_premium_rpm = int(current_policy[region]["premium"].limit_per_minute * 1.10)
                emit_policy(region, "free",    new_free_rpm,    reason=f"predicted_spike_{region}_free")
                emit_policy(region, "premium", new_premium_rpm, reason=f"predicted_spike_{region}_free_compensation")
                continue

        # Rule 2 — capacity restoration
        baseline_rpm = DEMO_BASELINE[tier]
        if forecast_rpm < 0.5 * cur_limit_rpm and rej > 0 and cur_limit_rpm < baseline_rpm:
            step_rpm = min(baseline_rpm, int(cur_limit_rpm * 1.20))
            emit_policy(region, tier, step_rpm, reason=f"restore_capacity_{region}_{tier}")
            continue

        # Rule 3 — noisy neighbor (per top user, independent of policy hysteresis)
        for user in obs[region][tier].top_users:
            if user.share_of_tier > 0.30:
                emit_override(user.user_id, limit=cur_limit_rpm // 10,
                              ttl=300, reason=f"noisy_neighbor_{user.user_id}")
```

`STATIC_FLOOR = {"free": 5, "premium": 50, "internal": 500}` — Rule 1 must
not collapse a tier to zero even if cur_limit is already very small.

**Notes on edge cases I will handle in code:**
- `STATIC_FREE_FLOOR = 5` (hardcoded constant) — Rule 1 must not collapse free to 0.
- Rule 1's premium boost is also gated by hysteresis on `(region, "premium")`.
- Rule 3 fires per-user, has its own per-user hysteresis dict separate from
  `last_decision_ts` so a noisy-neighbor override won't block a tier-policy update.
- `current_policy` is read fresh from Redis each tick (gateway has 5s lag, so
  successive ticks may see our own previous write — that's expected).

## Main loop orchestration (`agent/loop.py`)

```
async def run(predictor_name: str, interval_s: int):
    cfg          = load_config()                      # env vars
    redis_pool   = {region: Redis(...) for region in [us, eu, asia]}
    prom         = MetricsClient(cfg.prom_url)
    predictor    = build_predictor(predictor_name)    # one instance per (region, tier)
    detector     = IsolationForestDetector()
    decider      = Decider(redis_pool, hysteresis_window_s=60)
    log          = DecisionLog(cfg.log_path)
    history      = HistoryBuffer(window_s=1800)       # 30 min, in-memory deque

    start_self_metrics_server(cfg.metrics_port)       # /metrics on :9101
    register_signal_handlers(log)                     # SIGINT/SIGTERM → log.flush()

    while not shutdown:
        t0 = time.time()
        try:
            obs        = await prom.fetch_features()  # PART A
            history.append(obs)
            forecasts  = {(r, t): predictor[r,t].forecast(120)
                          for r in REGIONS for t in TIERS}
            anomalies  = detector.score(obs)          # current vs trained model
            decisions  = decider.decide(obs, forecasts, anomalies)
            await decider.apply(decisions)            # writes to Redis
            log.append({...})                         # one record per tick
            agent_metrics.tick_ok.inc()
        except Exception as e:
            agent_metrics.tick_error.inc()
            logger.exception("tick failed")
        await asyncio.sleep(max(0, interval_s - (time.time() - t0)))
```

**Detector retraining cadence:** every 20 ticks (5 min) IsolationForest
refits on the rolling 60 min of RPS data. Avoids per-tick fit cost and gives
the model enough samples (240 points × 9 series = 2160).

## Deploy mode: host-only (no Docker)

The agent does *not* go in `docker-compose.yml`. It runs on the host like
the simulator:

```
cd agent
pip install -r requirements.txt
python -m agent.main run --predictor ewma --interval 15
```

Defaults in `agent/config.py`:
- `PROM_URL=http://localhost:9090`
- `REDIS_US_ADDR=localhost:6379`
- `REDIS_EU_ADDR=localhost:6380`
- `REDIS_ASIA_ADDR=localhost:6381`
- `METRICS_PORT=9114`
- `LOG_PATH=agent/decisions.jsonl`

All overrideable via env vars (mirroring the sync service's convention).

**Self-observability metrics** are still exposed on `localhost:9114/metrics`
for `curl` and notebook inspection. **They are not scraped by Prometheus**
in this phase — Prometheus runs inside `georl_net` and would need a
`host.docker.internal:9114` target which is platform-specific. If we want
the agent on Grafana later, we add the scrape config then; for now,
agent telemetry lives in `decisions.jsonl` and the metrics endpoint is a
debug surface.

Metrics exposed (for the curious / for the eval notebook):
- `agent_ticks_total{outcome="ok"|"error"}` counter
- `agent_decisions_total{rule, region, tier}` counter
- `agent_prediction_error{predictor, region, tier}` gauge (last actual − prior prediction)
- `agent_policy_writes_total{region, tier}` counter

## Tests (pytest, fakeredis where applicable)

- `test_predictor.py`: feed sine wave + noise → EWMA tracks within tolerance;
  HW recovers period; both return `None` on <8 samples.
- `test_detector.py`: train on uniform stream, inject 10× spike, assert flagged.
- `test_decider.py`: one test per rule (1, 2, 3, hysteresis); table-driven.
- `test_policy_writer.py`: fakeredis, write policy, parse back, assert
  Contract 4 fields all present and `seqFromID` would return the expected seq.

## Verification (end-to-end)

1. `docker-compose up -d` — all data-plane services healthy.
2. `python tools/seed_policies.py --demo` — write rebased baselines
   (free=300, premium=3000, internal=30000 rpm) to all three regions.
   Verify with `docker exec redis-us redis-cli GET policy:us:free`.
3. In a separate terminal: `cd agent && python -m agent.main run --predictor ewma`.
   Should tick every 15s with "decision=..." log lines.
4. In another terminal: `python -m simulator.main scenario noisy_neighbor`.
5. Within 60s of scenario start, expect:
   - `agent/decisions.jsonl` contains a `noisy_neighbor_free_00001` entry
   - `docker exec redis-us redis-cli GET override:free_00001` returns JSON
     `{"limit_per_minute": 30, "ttl": 300, "reason": "noisy_neighbor_free_00001"}`
   - `rl_policy_version{region="us",tier="free"}` *unchanged* (Rule 3 writes
     overrides, not policies — confirms unit separation works)
6. Run `python -m simulator.main scenario product_launch`. Expect:
   - `predicted_spike_us_free` decision within ~30s of spike onset
   - Contract 4 policy at `policy:us:free` with `limit_per_minute=210`
     (70% of demo baseline 300)
   - `policy:us:premium` raised to ~3300
   - `rl_policy_version{region="us",tier="free"}` advances visibly in Prometheus
7. After scenario ends, wait ~90s with no traffic. Expect `restore_capacity_us_free`
   decision (`forecast_rpm < 0.5 * 210 = 105`, `rej > 0` from the spike's tail).
8. Open `agent/notebooks/predictor_eval.ipynb`, point it at
   `agent/decisions.jsonl`, run all cells; produces MAE comparison plot
   between EWMA and Holt-Winters.

Tests: `cd agent && pytest -q` — all four test files pass without docker
(uses fakeredis + synthetic series).

## What I am NOT doing in Phase 7

- ML training pipeline (the predictors fit online; no offline training step).
- Persisting hysteresis state across agent restarts (in-memory is fine; restart
  resets the 60s clocks — acceptable failure mode).
- Multi-agent coordination (single agent instance; if it crashes, policies
  expire via TTL=300 and gateway falls back to static baselines).
- Modifying gateway or sync code.
