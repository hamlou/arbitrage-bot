"""
Calibrated momentum -> probability mapping, fit from real historical price
data instead of the hand-picked `sensitivity=8.0` constant that shipped in
the original scaffold. See scripts/calibrate_momentum_model.py for fitting.

Design: bin historical (momentum magnitude, did the direction hold at
horizon?) samples into quantile buckets, take the empirical hit rate per
bucket, enforce monotonicity (more momentum should never imply a *lower*
continuation probability — real markets can violate this in small samples,
but a monotonic calibration is the more defensible assumption to bake in),
then linearly interpolate between bucket midpoints at inference time.

This intentionally avoids adding a hard dependency on scikit-learn/scipy for
what is fundamentally a small, offline curve-fit — numpy is enough.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np

DEFAULT_CALIBRATION_PATH = Path("config/calibration.json")


@dataclass
class CalibrationModel:
    """A monotonic momentum-magnitude -> continuation-probability curve for one contract horizon."""

    horizon_minutes: int
    magnitude_breakpoints: list[float]      # sorted ascending
    continuation_probability: list[float]   # same length, monotonic non-decreasing, in [0.5, 1.0]
    sample_count: int
    fitted_at: str

    def probability_direction_holds(self, momentum_magnitude: float) -> float:
        """
        P(the observed momentum's direction is still the outcome at horizon),
        interpolated from the fitted curve. Clamped to the fitted range at the
        edges (no extrapolation past the most extreme magnitude observed).
        """
        if not self.magnitude_breakpoints:
            return 0.5
        return float(
            np.interp(
                momentum_magnitude,
                self.magnitude_breakpoints,
                self.continuation_probability,
            )
        )

    def implied_probability(self, momentum_pct: float, direction: str) -> float:
        """Drop-in replacement for the old _implied_prob_from_momentum() heuristic."""
        p_continues = self.probability_direction_holds(abs(momentum_pct))
        p_up = p_continues if direction == "UP" else (1 - p_continues)
        return max(0.01, min(0.99, p_up))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CalibrationModel":
        return cls(**d)


def quantile_bin_stats(
    values: list[float],
    outcomes: list[float],
    n_bins: int,
) -> tuple[list[float], list[float], list[int]]:
    """
    The single binning routine every calibration computation uses: sort by the
    feature, split into `n_bins` equal-count quantile buckets (np.array_split),
    and return (breakpoint, empirical-rate, count) per bucket. Breakpoints and
    rates are bucket MEANS — this is the exact binning fit_calibration() fits
    its curve on, and it is reused verbatim by scripts/check_calibration_drift.py
    so the drift check compares apples to apples with the fitted model.
    """
    values = np.asarray(values, dtype=float)
    outcomes = np.asarray(outcomes, dtype=float)

    order = np.argsort(values)
    values_sorted = values[order]
    outcomes_sorted = outcomes[order]

    breakpoints: list[float] = []
    rates: list[float] = []
    counts: list[int] = []
    for idx in np.array_split(np.arange(len(values_sorted)), n_bins):
        if len(idx) == 0:
            continue
        breakpoints.append(float(values_sorted[idx].mean()))
        rates.append(float(outcomes_sorted[idx].mean()))
        counts.append(int(len(idx)))
    return breakpoints, rates, counts


def fit_calibration(
    samples: list[tuple[float, bool]],
    horizon_minutes: int,
    n_bins: int = 8,
) -> Optional[CalibrationModel]:
    """
    samples: list of (momentum_magnitude, direction_held_at_horizon) pairs,
    typically produced by build_samples_from_price_series() below.
    Returns None if there isn't enough data to fit a meaningful curve.
    """
    if len(samples) < n_bins * 5:
        return None

    magnitudes = [s[0] for s in samples]
    held = [1.0 if s[1] else 0.0 for s in samples]
    breakpoints, probs, _counts = quantile_bin_stats(magnitudes, held, n_bins)

    if not breakpoints:
        return None

    # Enforce monotonic non-decreasing continuation probability with magnitude.
    probs = list(np.maximum.accumulate(probs))
    # Never let the curve claim *worse* than a coin flip — if the raw data
    # says otherwise, that's a sign of too little data, not a real edge to bake in.
    probs = [max(0.5, p) for p in probs]

    return CalibrationModel(
        horizon_minutes=horizon_minutes,
        magnitude_breakpoints=breakpoints,
        continuation_probability=probs,
        sample_count=len(samples),
        fitted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def build_samples_from_price_series(
    timestamps: list[float],
    prices: list[float],
    lookback_s: float,
    horizon_s: float,
) -> list[tuple[float, bool]]:
    """
    Slides through a (timestamp, price) series and, for each point, computes:
      - momentum over the preceding `lookback_s` window
      - whether that momentum's direction "held" (price kept moving the same
        way) by `horizon_s` later

    This is intentionally symmetric with SymbolMomentumTracker's own
    lookback/direction logic in engine/signal.py, so the calibration reflects
    the same feature the live signal engine actually computes.
    """
    n = len(prices)
    samples: list[tuple[float, bool]] = []

    j = 0  # left pointer for the lookback window
    k = 0  # right pointer for the horizon window

    for i in range(n):
        t0 = timestamps[i]

        while j < i and timestamps[j] < t0 - lookback_s:
            j += 1
        if j >= i or prices[j] == 0:
            continue

        momentum = (prices[i] - prices[j]) / prices[j]
        if momentum == 0:
            continue
        direction_up = momentum > 0

        target_t = t0 + horizon_s
        if k < i:
            k = i
        while k < n - 1 and timestamps[k] < target_t:
            k += 1
        if timestamps[k] < target_t:
            continue  # ran off the end of the series before reaching the horizon

        outcome_up = prices[k] > prices[i]
        held = outcome_up == direction_up
        samples.append((abs(momentum), held))

    return samples


def save_calibration(models: dict[int, CalibrationModel], path: Path = DEFAULT_CALIBRATION_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {str(horizon): model.to_dict() for horizon, model in models.items()}
    path.write_text(json.dumps(payload, indent=2))


def load_calibration(path: Path = DEFAULT_CALIBRATION_PATH) -> dict[int, CalibrationModel]:
    """Returns an empty dict (not an error) if no calibration file exists yet —
    callers should fall back to the uncalibrated heuristic in that case."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return {int(horizon): CalibrationModel.from_dict(d) for horizon, d in payload.items()}
