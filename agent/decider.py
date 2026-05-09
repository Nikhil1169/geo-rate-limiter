"""
Rules-based decision engine.

Pure logic — no I/O, no Redis. The loop reads cur_policies from Redis and
passes them in; policy_writer applies the returned decisions.

All threshold comparisons are in requests-per-minute (rpm). Prometheus
gives us RPS; multiply by 60 at the entry point to this module.
"""

import logging
import time
from dataclasses import dataclass
from typing import Literal

from agent.config import DEMO_BASELINE, STATIC_FALLBACK, STATIC_FLOOR, REGIONS, TIERS
from agent.metrics_client import FeatureSnapshot
from agent.predictor import Forecast

log = logging.getLogger(__name__)


@dataclass
class Decision:
    type: Literal["policy", "override"]
    region: str
    tier: str | None        # populated for "policy" decisions
    user_id: str | None     # populated for "override" decisions
    limit_per_minute: int
    ttl: int                # seconds; policy TTL=300 so stale agent doesn't linger
    reason: str             # mandatory audit trail (Contract 4)


class Decider:
    """
    Applies four rules each tick and returns a list of Decisions.

    Hysteresis state is in-memory; a restart resets all timers (the
    60-second window is short enough that this is an acceptable gap).
    """

    _HYSTERESIS_S = 60.0
    _POLICY_TTL = 300       # policies expire if agent crashes
    _OVERRIDE_TTL = 300

    def __init__(self) -> None:
        # last wall-clock time a policy was emitted for (region, tier)
        self._policy_ts: dict[tuple[str, str], float] = {}
        # last wall-clock time an override was emitted for user_id
        self._override_ts: dict[str, float] = {}

    def decide(
        self,
        obs: FeatureSnapshot,
        forecasts: dict[tuple[str, str], Forecast | None],
        anomalies: dict[tuple[str, str], bool],
        cur_policies: dict[tuple[str, str], dict],
    ) -> list[Decision]:
        now = time.monotonic()
        decisions: list[Decision] = []

        for region in REGIONS:
            for tier in TIERS:
                key = (region, tier)
                cur = cur_policies.get(key, {})
                cur_limit_rpm = int(cur.get("limit_per_minute", STATIC_FALLBACK[tier]))

                # — convert all RPS observations to rpm at the boundary —
                observed_rpm = obs.regions[region][tier].rps * 60.0
                fc = forecasts.get(key)
                forecast_rpm = fc.point * 60.0 if fc is not None else observed_rpm
                rejection_rate = obs.regions[region][tier].rejection_rate
                is_anomaly = anomalies.get(key, False)

                # Detect noisy-neighbor pattern for this tick.
                # When any single user holds > 30% of tier traffic the pattern
                # is concentration, not a tier-wide spike — Rule 3 (per-user
                # override) is the right tool, not Rule 1 (tier-wide policy).
                # Computed here so it can gate Rule 1 before hysteresis check.
                has_noisy_neighbor = (
                    observed_rpm >= 30
                    and any(
                        u.share_of_tier > 0.30
                        for u in obs.regions[region][tier].top_users
                    )
                )

                # Rule 4 — hysteresis gate (Rules 1 & 2 only; Rule 3 has its own)
                last_pol = self._policy_ts.get(key)
                if last_pol is not None and (now - last_pol) < self._HYSTERESIS_S:
                    continue

                # ── Rule 1 — predicted spike mitigation (free tier only) ────
                # Skipped when a noisy neighbor is present: firing a tier-wide
                # policy would set _policy_ts and block Rule 3 for 60s, and the
                # reduced cur_limit_rpm would cause the override throttle value
                # to shrink each cycle (30 → 21 → 14 → …).
                # With no noisy neighbor this fires normally for product_launch.
                if tier == "free" and not has_noisy_neighbor:
                    premium_key = (region, "premium")
                    premium_rej = obs.regions[region]["premium"].rejection_rate
                    spike = (forecast_rpm > 0.8 * cur_limit_rpm) or is_anomaly
                    if spike and premium_rej < 0.10:
                        new_free_rpm = max(STATIC_FLOOR["free"], int(cur_limit_rpm * 0.70))
                        decisions.append(Decision(
                            type="policy", region=region, tier="free", user_id=None,
                            limit_per_minute=new_free_rpm,
                            ttl=self._POLICY_TTL,
                            reason=f"predicted_spike_{region}_free",
                        ))
                        self._policy_ts[key] = now

                        # Premium compensation — gated by its own hysteresis
                        prem_last = self._policy_ts.get(premium_key)
                        if prem_last is None or (now - prem_last) >= self._HYSTERESIS_S:
                            prem_cur = cur_policies.get(premium_key, {})
                            prem_limit = int(prem_cur.get("limit_per_minute", STATIC_FALLBACK["premium"]))
                            decisions.append(Decision(
                                type="policy", region=region, tier="premium", user_id=None,
                                limit_per_minute=int(prem_limit * 1.10),
                                ttl=self._POLICY_TTL,
                                reason=f"predicted_spike_{region}_free_compensation",
                            ))
                            self._policy_ts[premium_key] = now

                        continue

                # ── Rule 2 — capacity restoration ────────────────────────────
                baseline_rpm = DEMO_BASELINE[tier]
                if (
                    forecast_rpm < 0.5 * cur_limit_rpm
                    and rejection_rate > 0
                    and cur_limit_rpm < baseline_rpm
                ):
                    step_rpm = min(baseline_rpm, int(cur_limit_rpm * 1.20))
                    decisions.append(Decision(
                        type="policy", region=region, tier=tier, user_id=None,
                        limit_per_minute=step_rpm,
                        ttl=self._POLICY_TTL,
                        reason=f"restore_capacity_{region}_{tier}",
                    ))
                    self._policy_ts[key] = now
                    continue

                # ── Rule 3 — noisy neighbor (per-user, own hysteresis) ───────
                # Guard: skip when traffic is idle or the agent is still warming
                # up — observed_rpm < 30 means < 0.5 RPS, which produces noisy
                # counter fractions that falsely elevate share_of_tier to 1.0.
                if observed_rpm < 30:
                    continue

                for user in obs.regions[region][tier].top_users:
                    if user.share_of_tier <= 0.30:
                        continue
                    u_last = self._override_ts.get(user.user_id)
                    if u_last is not None and (now - u_last) < self._HYSTERESIS_S:
                        continue
                    # Throttle against the fixed baseline (not cur_limit_rpm).
                    # cur_limit_rpm can be reduced by Rule 1, which would make
                    # the override value shrink each cycle — 30 → 21 → 14 → …
                    # Using DEMO_BASELINE keeps it stable: 30/min for free tier.
                    throttle = max(10, DEMO_BASELINE[tier] // 10)
                    decisions.append(Decision(
                        type="override", region=region, tier=tier,
                        user_id=user.user_id,
                        limit_per_minute=throttle,
                        ttl=self._OVERRIDE_TTL,
                        reason=f"noisy_neighbor_{user.user_id}",
                    ))
                    self._override_ts[user.user_id] = now

        return decisions
