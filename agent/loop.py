"""
Main agent loop — orchestrates all components on a fixed-interval tick.

Per tick:
  1. Fetch FeatureSnapshot from Prometheus
  2. Append RPS to per-(region,tier) history deques
  3. Fit all 9 predictors; compute forecasts
  4. Every DETECTOR_REFIT_TICKS: refit IsolationForest on 60-min history
  5. Score current obs for anomalies
  6. Read current policies from Redis
  7. Run decision engine (pure logic)
  8. Write decisions to Redis
  9. Append log entry to decisions.jsonl

Two history deques per (region,tier):
  pred_hist — 30 min (120 ticks) for predictor warmup
  det_hist  — 60 min (240 ticks) for IsolationForest training set
"""

import asyncio
import logging
import signal
import time
from collections import deque

from agent.config import Config, REGIONS, TIERS
from agent.decision_log import DecisionLog
from agent.decider import Decider
from agent.detector import IsolationForestDetector
from agent.metrics_client import MetricsClient
from agent.policy_writer import apply_decisions, build_redis_pool, read_current_policies
from agent.predictor import Predictor, build_predictor

log = logging.getLogger(__name__)

_PRED_HIST_MAXLEN = 120   # 30 min at 15s ticks
_DET_HIST_MAXLEN = 240    # 60 min at 15s ticks
DETECTOR_REFIT_TICKS = 20 # refit every 5 min


async def run(cfg: Config) -> None:
    redis_pool = build_redis_pool(cfg)
    prom = MetricsClient(cfg.prom_url)

    # 9 independent predictors — one per (region, tier)
    predictors: dict[tuple[str, str], Predictor] = {
        (r, t): build_predictor(cfg.predictor)
        for r in REGIONS for t in TIERS
    }
    pred_hist: dict[tuple[str, str], deque] = {
        (r, t): deque(maxlen=_PRED_HIST_MAXLEN)
        for r in REGIONS for t in TIERS
    }
    det_hist: dict[tuple[str, str], deque] = {
        (r, t): deque(maxlen=_DET_HIST_MAXLEN)
        for r in REGIONS for t in TIERS
    }

    detector = IsolationForestDetector()
    decider = Decider()
    dlog = DecisionLog(cfg.log_path)

    shutdown = False

    def _on_shutdown(signum, frame):  # noqa: ANN001
        nonlocal shutdown
        log.info("Signal %d received — draining and stopping", signum)
        shutdown = True

    signal.signal(signal.SIGINT, _on_shutdown)
    signal.signal(signal.SIGTERM, _on_shutdown)

    tick = 0
    log.info(
        "Agent started  predictor=%s  interval=%ds  log=%s",
        cfg.predictor, cfg.interval_seconds, cfg.log_path,
    )

    while not shutdown:
        t0 = time.time()
        tick += 1

        try:
            # ── 1. Fetch features ────────────────────────────────────────────
            obs = await prom.fetch_features()

            # ── 2. Update history deques ─────────────────────────────────────
            for r in REGIONS:
                for t in TIERS:
                    rps = obs.regions[r][t].rps
                    entry = (obs.timestamp, rps)
                    pred_hist[(r, t)].append(entry)
                    det_hist[(r, t)].append(entry)

            # ── 3. Fit predictors; compute forecasts ─────────────────────────
            forecasts = {}
            for r in REGIONS:
                for t in TIERS:
                    key = (r, t)
                    predictors[key].fit(list(pred_hist[key]))
                    forecasts[key] = predictors[key].forecast(120)

            # ── 4. Refit anomaly detector every N ticks ──────────────────────
            if tick % DETECTOR_REFIT_TICKS == 0:
                detector.refit(det_hist)
                log.debug("Detector refitted at tick %d", tick)

            # ── 5. Score current obs ─────────────────────────────────────────
            anomalies = detector.score(obs)

            # ── 6. Read current policies ─────────────────────────────────────
            cur_policies = await read_current_policies(redis_pool)

            # ── 7. Run decision engine ───────────────────────────────────────
            decisions = decider.decide(obs, forecasts, anomalies, cur_policies)

            # ── 8. Write to Redis ────────────────────────────────────────────
            written = await apply_decisions(decisions, redis_pool)

            # ── 9. Log tick ──────────────────────────────────────────────────
            dlog.append({
                "tick": tick,
                "timestamp": obs.timestamp,
                "observed_features": {
                    f"{r}/{t}": {
                        "rps": obs.regions[r][t].rps,
                        "rejection_rate": obs.regions[r][t].rejection_rate,
                        "top_user_count": len(obs.regions[r][t].top_users),
                    }
                    for r in REGIONS for t in TIERS
                },
                "predictions": {
                    f"{r}/{t}": (
                        {"point": forecasts[(r, t)].point,
                         "horizon_seconds": forecasts[(r, t)].horizon_seconds}
                        if forecasts.get((r, t)) is not None else None
                    )
                    for r in REGIONS for t in TIERS
                },
                "anomalies": {
                    f"{r}/{t}": anomalies.get((r, t), False)
                    for r in REGIONS for t in TIERS
                },
                "decisions": [
                    {
                        "type": d.type,
                        "region": d.region,
                        "tier": d.tier,
                        "user_id": d.user_id,
                        "limit_per_minute": d.limit_per_minute,
                        "reason": d.reason,
                    }
                    for d in decisions
                ],
                "written_count": len(written),
            })

            if decisions:
                log.info(
                    "tick=%-4d decisions=%d  %s",
                    tick, len(decisions),
                    [d.reason for d in decisions],
                )
            else:
                log.debug("tick=%-4d no decisions", tick)

        except Exception:
            log.exception("tick=%d failed", tick)

        elapsed = time.time() - t0
        await asyncio.sleep(max(0.0, cfg.interval_seconds - elapsed))

    dlog.close()
    log.info("Agent stopped after %d ticks", tick)
