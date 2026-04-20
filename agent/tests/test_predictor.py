"""
Tests for EWMAPredictor and HoltWintersPredictor.

Synthetic data only — no I/O, no Redis, no Prometheus.
"""

import math

import pytest

from agent.predictor import EWMAPredictor, Forecast, HoltWintersPredictor


def _history(values: list, start_ms: int = 1_000_000) -> list[tuple[int, float]]:
    """Build a (unix_ms, rps) list with 15-second spacing."""
    return [(start_ms + i * 15_000, v) for i, v in enumerate(values)]


def _sine_wave(n: int, mean: float = 20.0, amp: float = 8.0, period: int = 20) -> list[float]:
    return [mean + amp * math.sin(i * 2 * math.pi / period) for i in range(n)]


# ── EWMAPredictor ─────────────────────────────────────────────────────────────

class TestEWMAPredictor:
    def test_returns_none_below_min_samples(self):
        """Fewer than 8 valid samples → forecast() is None."""
        p = EWMAPredictor()
        p.fit(_history([5.0] * 7))
        assert p.forecast() is None

    def test_returns_forecast_at_min_samples(self):
        """Exactly 8 valid samples → forecast() returns a Forecast."""
        p = EWMAPredictor()
        p.fit(_history([5.0] * 8))
        result = p.forecast()
        assert isinstance(result, Forecast)
        assert result.lower is None
        assert result.upper is None
        assert result.horizon_seconds == 120

    def test_constant_series_gives_exact_level(self):
        """EWMA of a constant series converges to that constant."""
        p = EWMAPredictor(alpha=0.3)
        p.fit(_history([10.0] * 20))
        result = p.forecast()
        assert result is not None
        assert abs(result.point - 10.0) < 1e-9

    def test_smoothing_on_sine_wave(self):
        """EWMA level on a sine wave should stay within the wave's range."""
        p = EWMAPredictor(alpha=0.3)
        p.fit(_history(_sine_wave(40, mean=20.0, amp=8.0)))
        result = p.forecast(horizon_seconds=120)
        assert result is not None
        # EWMA should not overshoot the min/max of the sine wave
        assert 12.0 <= result.point <= 28.0

    def test_skips_none_values_insufficient(self):
        """None entries are skipped; fewer than 8 valid → None."""
        p = EWMAPredictor()
        p.fit(_history([None] * 5 + [5.0] * 3))
        assert p.forecast() is None

    def test_skips_none_values_sufficient(self):
        """None entries are skipped; 8+ valid samples → forecast returned."""
        p = EWMAPredictor()
        p.fit(_history([None, None] + [10.0] * 8))
        result = p.forecast()
        assert result is not None
        assert abs(result.point - 10.0) < 1e-9

    def test_empty_history(self):
        p = EWMAPredictor()
        p.fit([])
        assert p.forecast() is None

    def test_all_none_history(self):
        p = EWMAPredictor()
        p.fit(_history([None] * 10))
        assert p.forecast() is None

    def test_horizon_seconds_preserved(self):
        p = EWMAPredictor()
        p.fit(_history([5.0] * 8))
        result = p.forecast(horizon_seconds=60)
        assert result is not None
        assert result.horizon_seconds == 60


# ── HoltWintersPredictor ──────────────────────────────────────────────────────

class TestHoltWintersPredictor:
    def test_returns_none_below_40_samples(self):
        """Fewer than 40 valid samples → forecast() is None."""
        p = HoltWintersPredictor()
        p.fit(_history([5.0] * 39))
        assert p.forecast() is None

    def test_returns_forecast_at_40_samples(self):
        """Exactly 40 valid samples → forecast() returns a Forecast."""
        data = _sine_wave(40, mean=20.0, amp=5.0)
        p = HoltWintersPredictor()
        p.fit(_history(data))
        result = p.forecast(horizon_seconds=120)
        assert isinstance(result, Forecast)
        assert result.point >= 0.0

    def test_returns_none_with_fewer_than_min(self):
        """30 samples (< 40) → None even though > EWMA minimum."""
        p = HoltWintersPredictor()
        p.fit(_history([10.0] * 30))
        assert p.forecast() is None

    def test_skips_none_values_insufficient(self):
        """None entries skipped; fewer than 40 valid → None."""
        p = HoltWintersPredictor()
        p.fit(_history([None] * 10 + [5.0] * 30))
        assert p.forecast() is None

    def test_skips_none_values_sufficient(self):
        """40 valid samples among Nones → returns a Forecast."""
        data = _sine_wave(40, mean=20.0, amp=5.0)
        p = HoltWintersPredictor()
        p.fit(_history([None, None] + data))
        result = p.forecast()
        assert result is not None

    def test_confidence_intervals_with_ample_data(self):
        """With 60 samples, CI bounds should be consistent when present."""
        data = _sine_wave(60, mean=20.0, amp=5.0)
        p = HoltWintersPredictor()
        p.fit(_history(data))
        result = p.forecast(horizon_seconds=120)
        assert result is not None
        if result.lower is not None:
            assert result.lower <= result.point + 1e-6
        if result.upper is not None:
            assert result.upper >= result.point - 1e-6

    def test_horizon_seconds_preserved(self):
        data = _sine_wave(40, mean=20.0, amp=5.0)
        p = HoltWintersPredictor()
        p.fit(_history(data))
        result = p.forecast(horizon_seconds=60)
        assert result is not None
        assert result.horizon_seconds == 60

    def test_empty_history(self):
        p = HoltWintersPredictor()
        p.fit([])
        assert p.forecast() is None
