"""
Traffic predictors.

Both predictors share the same Predictor interface:
  fit(history)       — update internal state from a list of (unix_ms, rps) pairs
  forecast(horizon)  — return a Forecast or None if insufficient history

Callers must handle None gracefully (fall back to last observed RPS).

HoltWintersPredictor is defined here but not yet implemented; it will be
added in the next implementation step after EWMA is reviewed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Forecast:
    point: float            # predicted RPS at the given horizon
    lower: float | None     # 95 % CI lower bound (None if unsupported)
    upper: float | None     # 95 % CI upper bound (None if unsupported)
    horizon_seconds: int    # how far ahead this forecast applies


class Predictor(ABC):
    name: str

    @abstractmethod
    def fit(self, history: list[tuple[int, float]]) -> None:
        """
        Ingest a fresh window of observations.

        history: list of (unix_ms, rps) ordered oldest-first.
        None values inside the list are skipped (Prometheus gap handling).
        Called every tick before forecast(); implementations should be fast
        (no I/O, no heavy allocation).
        """

    @abstractmethod
    def forecast(self, horizon_seconds: int = 120) -> Forecast | None:
        """
        Return a point forecast for `horizon_seconds` in the future, or None
        when there is not enough history to produce a reliable estimate.
        """


class EWMAPredictor(Predictor):
    """
    Exponentially Weighted Moving Average predictor.

    Point estimate = last smoothed value (the EWMA is stationary, so the
    'forecast' at any future horizon is the current level — appropriate for
    slowly-varying traffic within a 2-minute window).

    No confidence interval is produced; lower/upper are None.
    """

    name = "ewma"
    MIN_SAMPLES = 8  # require 8 valid observations (~2 min) before forecasting

    def __init__(self, alpha: float = 0.3) -> None:
        self._alpha = alpha
        self._level: float | None = None
        self._n: int = 0

    def fit(self, history: list[tuple[int, float]]) -> None:
        values = [v for _, v in history if v is not None]
        self._n = len(values)
        if not values:
            self._level = None
            return
        level = values[0]
        for v in values[1:]:
            level = self._alpha * v + (1.0 - self._alpha) * level
        self._level = level

    def forecast(self, horizon_seconds: int = 120) -> Forecast | None:
        if self._level is None or self._n < self.MIN_SAMPLES:
            return None
        return Forecast(
            point=self._level,
            lower=None,
            upper=None,
            horizon_seconds=horizon_seconds,
        )


class HoltWintersPredictor(Predictor):
    """
    Holt-Winters double-exponential smoothing with additive trend and
    additive seasonality (seasonal_periods=20, i.e. a 5-minute cycle at
    15-second ticks).

    Requires statsmodels. Implementation pending review of EWMAPredictor.
    """

    name = "holtwinters"
    MIN_SAMPLES = 40  # 2 × seasonal_periods

    def fit(self, history: list[tuple[int, float]]) -> None:
        raise NotImplementedError("HoltWintersPredictor not yet implemented")

    def forecast(self, horizon_seconds: int = 120) -> Forecast | None:
        raise NotImplementedError("HoltWintersPredictor not yet implemented")


def build_predictor(name: str) -> Predictor:
    """Factory used by the main loop to construct predictors by name."""
    match name:
        case "ewma":
            return EWMAPredictor()
        case "holtwinters":
            return HoltWintersPredictor()
        case _:
            raise ValueError(f"Unknown predictor: {name!r}  (choices: ewma, holtwinters)")
