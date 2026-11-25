import math
import os
from pathlib import Path

import joblib
import pandas as pd

from districts import DISTRICTS
from indexed_reference import uses_indexed_reference


def load_model():
    artifact = joblib.load(Path(__file__).with_name("joint_model.joblib"))
    if set(artifact.get("scope", ())) != set(DISTRICTS):
        raise ValueError("The production HGB artifact must cover all 25 Seoul districts")
    return artifact["model"]


HORIZONS = (3, 6, 12)


def predict_growth(model, rows):
    features = pd.DataFrame(rows, columns=model.features, dtype=float)
    return model.predict(features, months=HORIZONS)


def forecast(model, row):
    growth = predict_growth(model, [row])[0]
    indexed = row.get("indexed_log_current")
    log_current = row["log_current"]
    if os.getenv("JIPFIT_INDEXED_REFERENCE") == "1" and indexed is not None and math.isfinite(indexed):
        log_current = indexed
    area = float(row["representative_area"])
    reference = math.exp(log_current) * area
    original_log = row["log_current"]
    gangnam_prefix = f"서울특별시 {DISTRICTS['11680']} "
    district = "11680" if row["complex_id"].startswith(gangnam_prefix) else ""
    forecasts = []
    for months, rate in zip(HORIZONS, growth, strict=True):
        forecast_log = log_current if uses_indexed_reference(district, months) else original_log
        forecasts.append({
            "months": months,
            "price_manwon": math.exp(forecast_log + rate) * area,
            "return_percent": 100 * math.expm1(forecast_log - log_current + rate),
        })
    return {
        "complex_id": row["complex_id"],
        "area_group": row["area_group"],
        "representative_area": float(row["representative_area"]),
        "as_of": row["month"],
        "reference_price_manwon": reference,
        "forecasts": forecasts,
    }

