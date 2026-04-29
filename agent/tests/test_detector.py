"""
Tests for IsolationForestDetector.

Synthetic training data only — no I/O, no Redis, no Prometheus.
"""

from collections import deque

import numpy as np
import pytest

from agent.detector import IsolationForestDetector
from agent.metrics_client import FeatureSnapshot, TierFeatures, REGIONS, TIERS

BASELINE_RPS = 20.0
N_TRAIN = 60


def _make_deque(values: list[float], start_ms: int = 1_000_000) -> deque:
    d: deque = deque()
    for i, v in enumerate(values):
        d.append((start_ms + i * 15_000, v))
    return d


def _make_snapshot(overrides: dict[tuple[str, str], float] | None = None) -> FeatureSnapshot:
    """Build a FeatureSnapshot; overrides maps (region, tier) → rps."""
    regions = {r: {t: TierFeatures() for t in TIERS} for r in REGIONS}
    if overrides:
        for (region, tier), rps in overrides.items():
            regions[region][tier].rps = rps
    return FeatureSnapshot(timestamp=1_000_000, regions=regions)


def _trained_detector(
    baseline_rps: float = BASELINE_RPS,
    n: int = N_TRAIN,
    contamination: float = 0.05,
) -> IsolationForestDetector:
    """Return an IsolationForestDetector trained on Poisson-distributed baseline data."""
    rng = np.random.default_rng(0)
    detector = IsolationForestDetector(contamination=contamination, random_state=42)
    histories: dict[tuple[str, str], deque] = {}
    for region in REGIONS:
        for tier in TIERS:
            counts = rng.poisson(baseline_rps, n).astype(float)
            histories[(region, tier)] = _make_deque(counts.tolist())
    detector.refit(histories)
    return detector


class TestIsolationForestDetector:
    def test_spike_flagged_as_anomaly(self):
        """A 10× spike above the trained baseline is flagged as an anomaly."""
        detector = _trained_detector()
        spike_rps = BASELINE_RPS * 10  # 200 vs trained ~20
        obs = _make_snapshot({("us", "free"): spike_rps})
        result = detector.score(obs)
        assert result[("us", "free")] is True, (
            f"10× spike ({spike_rps}) should be flagged; trained on ~{BASELINE_RPS}"
        )

    def test_normal_variance_not_flagged(self):
        """Poisson samples around the baseline are flagged at most ~contamination rate."""
        detector = _trained_detector(contamination=0.05)
        rng = np.random.default_rng(99)
        n_trials = 100
        flagged = 0
        for _ in range(n_trials):
            rps = float(rng.poisson(BASELINE_RPS))
            obs = _make_snapshot({("us", "free"): rps})
            if detector.score(obs)[("us", "free")]:
                flagged += 1
        # Allow up to 3× contamination rate as a generous false-positive budget
        assert flagged / n_trials <= 0.15, (
            f"Too many false positives: {flagged}/{n_trials} = {flagged/n_trials:.0%}"
        )

    def test_no_model_before_refit(self):
        """score() returns False for all keys before any refit call."""
        detector = IsolationForestDetector()
        obs = _make_snapshot({("us", "free"): 200.0})
        result = detector.score(obs)
        assert result[("us", "free")] is False

    def test_all_regions_scored(self):
        """score() returns an entry for every (region, tier) pair."""
        detector = _trained_detector()
        obs = _make_snapshot()
        result = detector.score(obs)
        for region in REGIONS:
            for tier in TIERS:
                assert (region, tier) in result

    def test_insufficient_samples_skipped_in_refit(self):
        """Keys with fewer than 10 samples are skipped; no model fitted → False."""
        detector = IsolationForestDetector()
        histories = {("us", "free"): _make_deque([20.0] * 5)}  # < _MIN_FIT_SAMPLES
        detector.refit(histories)
        obs = _make_snapshot({("us", "free"): 200.0})
        result = detector.score(obs)
        assert result[("us", "free")] is False

    def test_moderate_spike_flagged(self):
        """A 5× spike should also be flagged (well outside the Poisson distribution)."""
        detector = _trained_detector()
        spike_rps = BASELINE_RPS * 5  # 100 vs ~20
        obs = _make_snapshot({("us", "free"): spike_rps})
        result = detector.score(obs)
        assert result[("us", "free")] is True

    def test_models_fitted_per_region_tier(self):
        """After refit, each (region, tier) has an independent model."""
        detector = _trained_detector()
        # Score with (us, free) spiked and other regions at the normal baseline.
        # 0 RPS would itself be an outlier for a Poisson(20) model, so use baseline.
        normal_snapshot = {(r, t): BASELINE_RPS for r in REGIONS for t in TIERS}
        normal_snapshot[("us", "free")] = BASELINE_RPS * 10  # only us/free is spiked
        result = detector.score(_make_snapshot(normal_snapshot))
        assert result[("us", "free")] is True
        # Regions running at baseline should not be flagged
        assert result[("eu", "free")] is False
        assert result[("asia", "free")] is False
