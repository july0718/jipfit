import math
from pathlib import Path

import joblib
import pandas as pd

from districts import DISTRICTS


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
            for months, rate in zip(HORIZONS, growth, strict=True)
        ],
    }

