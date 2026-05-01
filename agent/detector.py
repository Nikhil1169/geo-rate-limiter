"""
Anomaly detector for RPS time series using IsolationForest.

One model is maintained per (region, tier) so each series is scored against
its own historical distribution — a single shared model would conflate
free-tier (hundreds rpm) with internal-tier (tens of thousands rpm).

The caller is responsible for refit cadence (every 20 ticks = 5 min).
Before the first refit, score() returns False for every key.
"""

import logging
from collections import deque

import numpy as np
from sklearn.ensemble import IsolationForest

from agent.metrics_client import FeatureSnapshot, REGIONS, TIERS

log = logging.getLogger(__name__)

_MIN_FIT_SAMPLES = 10  # require at least 10 points before fitting


class IsolationForestDetector:
    def __init__(
        self,
        contamination: float = 0.05,
        random_state: int = 42,
    ) -> None:
        self._contamination = contamination
        self._random_state = random_state
        self._models: dict[tuple[str, str], IsolationForest] = {}

    def refit(self, histories: dict[tuple[str, str], deque]) -> None:
        """
        Retrain all 9 models on current rolling history.

        histories: dict keyed by (region, tier), values are deques of
        (unix_ms, rps) tuples. None rps values are skipped.
        """
        n_fitted = 0
        for key, hist in histories.items():
            values = [v for _, v in hist if v is not None]
            if len(values) < _MIN_FIT_SAMPLES:
                continue
            X = np.array(values, dtype=float).reshape(-1, 1)
            m = IsolationForest(
                contamination=self._contamination,
                n_estimators=100,
                random_state=self._random_state,
            )
            m.fit(X)
            self._models[key] = m
            n_fitted += 1
        log.debug("IsolationForest: refitted %d/%d models", n_fitted, len(histories))

    def score(self, obs: FeatureSnapshot) -> dict[tuple[str, str], bool]:
        """
        Score the current observation for each (region, tier).

        Returns True where IsolationForest predicts an outlier (-1).
        Returns False if no model has been fitted yet for that key.
        """
        result: dict[tuple[str, str], bool] = {}
        for region in REGIONS:
            for tier in TIERS:
                key = (region, tier)
                model = self._models.get(key)
                if model is None:
                    result[key] = False
                    continue
                rps = obs.regions[region][tier].rps
                pred = model.predict(np.array([[rps]]))
                result[key] = bool(pred[0] == -1)
        return result
