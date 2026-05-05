import math
import pathlib
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from engine import diurnal_factor, poisson_stream
from population import load_population


# ---------------------------------------------------------------------------
# diurnal_factor
# ---------------------------------------------------------------------------

def test_diurnal_factor_range():
    """Factor must stay within [0.5, 1.5] for any elapsed time."""
    elapsed_values = [i * 0.5 for i in range(700)]  # covers > 2 full 300s cycles
    factors = [diurnal_factor(t) for t in elapsed_values]
    assert all(0.5 <= f <= 1.5 for f in factors)


def test_diurnal_factor_known_points():
    assert math.isclose(diurnal_factor(0),   1.0, abs_tol=1e-9)   # sin(0) = 0
    assert math.isclose(diurnal_factor(75),  1.5, abs_tol=1e-6)   # sin(π/2) = 1, peak
    assert math.isclose(diurnal_factor(225), 0.5, abs_tol=1e-6)   # sin(3π/2) = -1, trough
    assert math.isclose(diurnal_factor(300), 1.0, abs_tol=1e-6)   # one full period


# ---------------------------------------------------------------------------
# poisson_stream inter-arrival distribution
# ---------------------------------------------------------------------------

async def test_poisson_mean_interarrival():
    """Mean inter-arrival time should be close to 1/rps."""
    rps = 30.0
    duration = 4.0
    population = load_population()

    timestamps: list[float] = []
    async for _ in poisson_stream(rps, duration, population, "us"):
        timestamps.append(time.monotonic())

    assert len(timestamps) >= 10, "too few samples to test distribution"
    intervals = np.diff(timestamps)
    measured_mean = float(intervals.mean())
    expected_mean = 1.0 / rps
    # Allow ±30% — Poisson has high variance; we just confirm order of magnitude
    assert abs(measured_mean - expected_mean) < expected_mean * 0.30, (
        f"mean inter-arrival {measured_mean:.4f}s, expected ~{expected_mean:.4f}s"
    )


async def test_poisson_stream_respects_duration():
    """Stream must stop yielding within a short margin after duration elapses."""
    duration = 1.0
    population = load_population()
    t0 = time.monotonic()
    async for _ in poisson_stream(50.0, duration, population, "us"):
        pass
    elapsed = time.monotonic() - t0
    # Allow up to 0.5s overshoot (one extra sleep interval at most)
    assert elapsed < duration + 0.5, f"stream ran too long: {elapsed:.2f}s"


async def test_poisson_stream_yields_correct_region():
    population = load_population()
    regions_seen: set[str] = set()
    async for spec in poisson_stream(20.0, 0.5, population, "eu"):
        regions_seen.add(spec.region)
    assert regions_seen == {"eu"}


async def test_poisson_stream_bias_applied():
    """With bias_share=1.0, all specs should have the culprit user."""
    population = load_population()
    culprit = "free_00042"
    specs = []
    async for spec in poisson_stream(
        20.0, 0.5, population, "us",
        bias_user=culprit, bias_share=1.0,
    ):
        specs.append(spec)
    assert len(specs) > 0
    assert all(s.user_id == culprit for s in specs)
