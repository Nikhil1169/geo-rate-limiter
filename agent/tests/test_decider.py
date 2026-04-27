"""
Table-driven tests for the Decider rules engine.

Each test class covers one rule. All tests use synthetic FeatureSnapshots
and Forecast objects — no I/O, no Redis.

Unit convention (matches decider.py): comparisons happen in rpm internally.
RPS values in the snapshot are multiplied by 60 at the decider boundary.
"""

import time

import pytest

from agent.config import DEMO_BASELINE, STATIC_FALLBACK, STATIC_FLOOR
from agent.decider import Decider, Decision
from agent.metrics_client import FeatureSnapshot, TierFeatures, UserStat, REGIONS, TIERS
from agent.predictor import Forecast


# ── helpers ───────────────────────────────────────────────────────────────────

def _snap(
    rps: float = 0.0,
    rejection_rate: float = 0.0,
    top_users: list[UserStat] | None = None,
) -> TierFeatures:
    return TierFeatures(rps=rps, rejection_rate=rejection_rate, top_users=top_users or [])


def _obs(overrides: dict[tuple[str, str], TierFeatures] | None = None) -> FeatureSnapshot:
    """Build a FeatureSnapshot; default TierFeatures for unspecified (region, tier)."""
    regions = {r: {t: TierFeatures() for t in TIERS} for r in REGIONS}
    if overrides:
        for (r, t), tf in overrides.items():
            regions[r][t] = tf
    return FeatureSnapshot(timestamp=int(time.time() * 1000), regions=regions)


def _forecasts(**overrides: float) -> dict[tuple[str, str], Forecast | None]:
    """
    Build the full 9-key forecast dict.  Pass keyword args as  region_tier=rps.
    Unprovided keys map to None (decider falls back to observed_rpm).
    Example: _forecasts(us_free=4.5)
    """
    fc: dict[tuple[str, str], Forecast | None] = {(r, t): None for r in REGIONS for t in TIERS}
    for key, rps in overrides.items():
        region, tier = key.split("_", 1)
        fc[(region, tier)] = Forecast(point=rps, lower=None, upper=None, horizon_seconds=120)
    return fc


def _no_anomalies() -> dict[tuple[str, str], bool]:
    return {(r, t): False for r in REGIONS for t in TIERS}


def _policies(**overrides: int) -> dict[tuple[str, str], dict]:
    """
    Build a policy dict.  Keyword args: region_tier=limit_per_minute.
    Example: _policies(us_free=300, us_premium=3000)
    """
    result: dict[tuple[str, str], dict] = {}
    for key, limit in overrides.items():
        region, tier = key.split("_", 1)
        result[(region, tier)] = {"limit_per_minute": limit}
    return result


# ── Rule 1 — Predicted spike mitigation (free tier only) ─────────────────────

class TestRule1Spike:
    """forecast_rpm > 0.8 * cur_limit AND premium_rej < 10%  →  free cut + premium boost."""

    _CUR_FREE = 300
    _CUR_PREM = 3_000

    def _spike_obs(self, prem_rej: float = 0.05) -> FeatureSnapshot:
        # rps=1.0 for free (safe below Rule 1 threshold when no forecast).
        # Actual spike comes from the explicit Forecast.
        return _obs({
            ("us", "free"):    _snap(rps=1.0, rejection_rate=0.0),
            ("us", "premium"): _snap(rps=0.5, rejection_rate=prem_rej),
        })

    def _spike_fc(self, free_rps: float = 4.5) -> dict:
        # 4.5 RPS * 60 = 270 rpm > 0.8 * 300 = 240 rpm → triggers
        return _forecasts(us_free=free_rps)

    # table: (scenario, free_rps, prem_rej, expect_fire)
    @pytest.mark.parametrize("label,free_rps,prem_rej,expect_fire", [
        ("spike_low_premium_rej",  4.5, 0.05, True),   # 270 > 240, rej=5%  → fires
        ("spike_high_premium_rej", 4.5, 0.15, False),  # 270 > 240, rej=15% → blocked
        ("no_spike",               3.0, 0.05, False),  # 180 < 240           → no fire
    ])
    def test_rule1_table(self, label, free_rps, prem_rej, expect_fire):
        decider = Decider()
        obs = self._spike_obs(prem_rej=prem_rej)
        fc = self._spike_fc(free_rps=free_rps)
        policies = _policies(us_free=self._CUR_FREE)

        decisions = decider.decide(obs, fc, _no_anomalies(), policies)
        free_cuts = [d for d in decisions if d.type == "policy" and d.tier == "free" and d.region == "us"]
        fired = len(free_cuts) == 1

        assert fired == expect_fire, f"[{label}] expected fire={expect_fire}, got decisions={decisions}"
        if expect_fire:
            assert free_cuts[0].limit_per_minute == max(STATIC_FLOOR["free"], int(self._CUR_FREE * 0.70))
            assert "predicted_spike" in free_cuts[0].reason

    def test_premium_compensation_fires_with_spike(self):
        """When free tier is cut, premium gets a 10% boost."""
        decider = Decider()
        obs = self._spike_obs(prem_rej=0.05)
        fc = self._spike_fc(free_rps=4.5)
        policies = _policies(us_free=self._CUR_FREE, us_premium=self._CUR_PREM)

        decisions = decider.decide(obs, fc, _no_anomalies(), policies)
        prem = [d for d in decisions if d.type == "policy" and d.tier == "premium" and d.region == "us"]
        assert len(prem) == 1
        assert prem[0].limit_per_minute == int(self._CUR_PREM * 1.10)
        assert "compensation" in prem[0].reason


# ── Rule 2 — Capacity restoration ────────────────────────────────────────────

class TestRule2Restoration:
    """forecast_rpm < 0.5 * cur_limit AND rej > 0 AND cur_limit < baseline → restore."""

    _CUR_LIMIT = 200  # below DEMO_BASELINE["free"] = 300

    # table: (label, forecast_rps, rej_rate, cur_limit, expect_restore)
    @pytest.mark.parametrize("label,forecast_rps,rej_rate,cur_limit,expect_restore", [
        ("all_conditions_met",        0.3, 0.05, 200, True),  # 18<100, rej>0, 200<300
        ("no_rejection",              0.3, 0.00, 200, False), # rej=0 → blocked
        ("forecast_too_high",         2.0, 0.05, 200, False), # 120 > 100 → blocked
        ("already_at_baseline",       0.3, 0.05, 300, False), # 300 = baseline → blocked
        ("above_baseline",            0.3, 0.05, 400, False), # 400 > baseline → blocked
    ])
    def test_rule2_table(self, label, forecast_rps, rej_rate, cur_limit, expect_restore):
        decider = Decider()
        obs = _obs({("us", "free"): _snap(rps=0.1, rejection_rate=rej_rate)})
        fc = _forecasts(us_free=forecast_rps)
        policies = _policies(us_free=cur_limit)

        decisions = decider.decide(obs, fc, _no_anomalies(), policies)
        restores = [d for d in decisions if d.type == "policy" and "restore" in d.reason and d.region == "us"]
        fired = len(restores) > 0

        assert fired == expect_restore, f"[{label}] expected={expect_restore}, decisions={decisions}"
        if expect_restore:
            expected_limit = min(DEMO_BASELINE["free"], int(cur_limit * 1.20))
            assert restores[0].limit_per_minute == expected_limit

    def test_restoration_does_not_exceed_baseline(self):
        """step_rpm is capped at DEMO_BASELINE even if 1.20× exceeds it."""
        decider = Decider()
        cur_limit = 280  # 1.20 × 280 = 336 > baseline 300
        obs = _obs({("us", "free"): _snap(rps=0.1, rejection_rate=0.05)})
        fc = _forecasts(us_free=0.3)  # 18 < 0.5 * 280 = 140
        policies = _policies(us_free=cur_limit)

        decisions = decider.decide(obs, fc, _no_anomalies(), policies)
        restores = [d for d in decisions if "restore" in d.reason and d.region == "us"]
        assert len(restores) == 1
        assert restores[0].limit_per_minute == DEMO_BASELINE["free"]  # capped at 300


# ── Rule 3 — Noisy neighbor ───────────────────────────────────────────────────

class TestRule3NoisyNeighbor:
    """User with share_of_tier > 30% gets an override at cur_limit // 10."""

    _CUR_LIMIT = 300

    def _make_user(self, share: float, uid: str = "free_00001") -> UserStat:
        return UserStat(user_id=uid, rps=share * 5.0, share_of_tier=share)

    # table: (label, share, expect_override)
    @pytest.mark.parametrize("label,share,expect_override", [
        ("above_threshold",  0.45, True),
        ("at_threshold",     0.30, False),  # > 0.30, not >=
        ("below_threshold",  0.20, False),
    ])
    def test_rule3_table(self, label, share, expect_override):
        decider = Decider()
        user = self._make_user(share=share)
        # Low rps to avoid triggering Rule 1 (rps*60 must not exceed 0.8*300=240)
        obs = _obs({("us", "free"): _snap(rps=2.0, rejection_rate=0.0, top_users=[user])})
        policies = _policies(us_free=self._CUR_LIMIT)

        decisions = decider.decide(obs, _forecasts(), _no_anomalies(), policies)
        overrides = [d for d in decisions if d.type == "override" and d.user_id == user.user_id]
        fired = len(overrides) == 1

        assert fired == expect_override, f"[{label}] share={share}, expected={expect_override}"
        if expect_override:
            assert overrides[0].limit_per_minute == max(1, self._CUR_LIMIT // 10)
            assert "noisy_neighbor" in overrides[0].reason

    def test_multiple_noisy_users(self):
        """Each user above the threshold gets an independent override."""
        decider = Decider()
        users = [
            UserStat(user_id="free_00001", rps=3.0, share_of_tier=0.45),
            UserStat(user_id="free_00002", rps=2.5, share_of_tier=0.35),
            UserStat(user_id="free_00003", rps=0.5, share_of_tier=0.10),  # below threshold
        ]
        obs = _obs({("us", "free"): _snap(rps=2.0, rejection_rate=0.0, top_users=users)})
        policies = _policies(us_free=self._CUR_LIMIT)

        decisions = decider.decide(obs, _forecasts(), _no_anomalies(), policies)
        override_ids = {d.user_id for d in decisions if d.type == "override"}
        assert "free_00001" in override_ids
        assert "free_00002" in override_ids
        assert "free_00003" not in override_ids


# ── Rule 4 — Hysteresis ──────────────────────────────────────────────────────

class TestRule4Hysteresis:
    """Policy decisions for (region, tier) are blocked for 60s after a write."""

    def _spike_setup(self):
        obs = _obs({
            ("us", "free"):    _snap(rps=1.0, rejection_rate=0.0),
            ("us", "premium"): _snap(rps=0.5, rejection_rate=0.05),
        })
        fc = _forecasts(us_free=4.5)  # 270 > 0.8*300 → triggers Rule 1
        policies = _policies(us_free=300, us_premium=3_000)
        return obs, fc, policies

    def test_second_call_is_blocked(self):
        """A second policy decision for the same (region, tier) within 60s is suppressed."""
        decider = Decider()
        obs, fc, policies = self._spike_setup()

        d1 = decider.decide(obs, fc, _no_anomalies(), policies)
        free1 = [d for d in d1 if d.type == "policy" and d.tier == "free" and d.region == "us"]
        assert len(free1) == 1, "First call should fire Rule 1"

        d2 = decider.decide(obs, fc, _no_anomalies(), policies)
        free2 = [d for d in d2 if d.type == "policy" and d.tier == "free" and d.region == "us"]
        assert len(free2) == 0, "Second call within 60s should be blocked by hysteresis"

    def test_different_regions_not_blocked(self):
        """Hysteresis on (us, free) does NOT block (eu, free) decisions."""
        decider = Decider()
        obs_us = _obs({
            ("us", "free"):    _snap(rps=1.0, rejection_rate=0.0),
            ("us", "premium"): _snap(rps=0.5, rejection_rate=0.05),
            ("eu", "free"):    _snap(rps=1.0, rejection_rate=0.0),
            ("eu", "premium"): _snap(rps=0.5, rejection_rate=0.05),
        })
        fc = _forecasts(us_free=4.5, eu_free=4.5)
        policies = _policies(us_free=300, us_premium=3_000, eu_free=300, eu_premium=3_000)

        d1 = decider.decide(obs_us, fc, _no_anomalies(), policies)
        us_free_d1 = [d for d in d1 if d.type == "policy" and d.tier == "free" and d.region == "us"]
        eu_free_d1 = [d for d in d1 if d.type == "policy" and d.tier == "free" and d.region == "eu"]
        assert len(us_free_d1) == 1
        assert len(eu_free_d1) == 1  # eu fires independently

        d2 = decider.decide(obs_us, fc, _no_anomalies(), policies)
        us_free_d2 = [d for d in d2 if d.type == "policy" and d.tier == "free" and d.region == "us"]
        eu_free_d2 = [d for d in d2 if d.type == "policy" and d.tier == "free" and d.region == "eu"]
        assert len(us_free_d2) == 0  # hysteresis blocks us
        assert len(eu_free_d2) == 0  # hysteresis blocks eu too (its own hysteresis entry)

    def test_rule3_fires_during_rule1_hysteresis(self):
        """Per-user overrides must still land while (region, tier) is locked
        out of tier-policy changes. The original code applied Rule 4 as a single
        gate for all rules, which caused noisy_neighbor's per-user throttle to
        be delayed by 60 s after Rule 1 fired in the prior tick — exactly the
        demo-blocking timing issue described in docs/demo-prep.md §5a-B.
        """
        decider = Decider()

        # Tick 1: rising tier RPS but no concentrated top user yet (Prom gauge
        # sample lags). Rule 1 fires, locks (us, free) for 60 s.
        tick1_obs = _obs({
            ("us", "free"):    _snap(rps=1.0, rejection_rate=0.0, top_users=[]),
            ("us", "premium"): _snap(rps=0.5, rejection_rate=0.05),
        })
        fc = _forecasts(us_free=4.5)  # 270 > 0.8 * 300 → Rule 1
        policies = _policies(us_free=300, us_premium=3_000)
        d1 = decider.decide(tick1_obs, fc, _no_anomalies(), policies)
        assert any(
            d.type == "policy" and d.tier == "free" and "predicted_spike" in d.reason
            for d in d1
        ), "Tick 1 must fire Rule 1 to set up the hysteresis lockout"

        # Tick 2: top-N gauge sample now reveals a real abuser at 50% share +
        # 1 rps (= 60 rpm, above the per-user floor). Rule 3 MUST fire even
        # though (us, free) is locked out of tier-policy changes.
        tick2_obs = _obs({
            ("us", "free"): _snap(
                rps=2.0, rejection_rate=0.0,
                top_users=[UserStat(user_id="free_00001", rps=1.0, share_of_tier=0.50)],
            ),
            ("us", "premium"): _snap(rps=0.5, rejection_rate=0.05),
        })
        d2 = decider.decide(tick2_obs, fc, _no_anomalies(), policies)
        overrides = [d for d in d2 if d.type == "override" and d.user_id == "free_00001"]
        assert len(overrides) == 1, (
            f"Rule 3 must fire even when (us, free) is in Rule 1 hysteresis. "
            f"Got: {d2}"
        )
