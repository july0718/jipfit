"""Reference-only historical error distribution for feature 19.

The bundled audit.csv.gz describes a historical GWANGJIN evaluation, NOT
calibration of the current Seoul-wide production model. Never call these
bounds forecast confidence or prediction intervals.
"""
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from inference import HORIZONS

ROOT = Path(__file__).resolve().parent
MIN_SAMPLES = 30


def summarize_reference(frame):
    required = {"horizon_months", "predicted_return_percent", "actual_return_percent"}
    if not required.issubset(frame.columns):
        raise ValueError("Historical audit is missing required return columns")
    rows = []
    for months in HORIZONS:
        part = frame.loc[pd.to_numeric(frame.horizon_months, errors="coerce") == months]
        actual = pd.to_numeric(part.actual_return_percent, errors="coerce")
        predicted = pd.to_numeric(part.predicted_return_percent, errors="coerce")
        residual = (actual - predicted).to_numpy(dtype=float)
        residual = residual[np.isfinite(residual)]
        if len(residual) < MIN_SAMPLES:
            raise ValueError(f"Historical reference has insufficient samples for {months} months")
        low, high = np.quantile(residual, [0.1, 0.9])
        rows.append({
            "months": int(months),
            "sample_count": len(residual),
            "p10_pp": float(low),
            "p90_pp": float(high),
        })
    return {
        "kind": "gwangjin_historical_model",
        "scope": "광진구 과거 모델 평가",
        "method": "실제 상승률 - 당시 예측 상승률의 10·90백분위수",
        "calibrated_for_current_model": False,
        "horizons": rows,
    }


@lru_cache(maxsize=1)
def historical_reference():
    """Read an immutable historical audit once; leave live inference working if absent."""
    try:
        audit = pd.read_csv(ROOT / "audit.csv.gz")
        return summarize_reference(audit)
    except (OSError, ValueError, TypeError, KeyError):
        return None
