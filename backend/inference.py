import math
from pathlib import Path

import joblib
import pandas as pd


def load_model():
    artifact = joblib.load(Path(__file__).with_name("joint_model.joblib"))
    return artifact["model"]


def forecast(model, row):
    features = pd.DataFrame([{key: row[key] for key in model.features}], dtype=float)
    horizons = (3, 6, 12)
    growth = model.predict(features, months=horizons)[0]
    reference = math.exp(row["log_current"]) * float(row["representative_area"])
    return {
        "complex_id": row["complex_id"],
        "area_group": row["area_group"],
        "representative_area": float(row["representative_area"]),
        "as_of": row["month"],
        "reference_price_manwon": reference,
        "forecasts": [
            {
                "months": months,
                "price_manwon": reference * math.exp(rate),
                "return_percent": 100 * math.expm1(rate),
            }
            for months, rate in zip(horizons, growth, strict=True)
        ],
    }

