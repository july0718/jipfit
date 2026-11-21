"""Publish verified Gwangjin historical residual quantiles as static Pages JSON.

This is a descriptive reference for the historical Gwangjin model, never a
prediction interval calibrated for the production Seoul-wide HGB.
"""
import csv
import gzip
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sys

HORIZONS = (3, 6, 12)
MIN_SAMPLES = 30
ROOT = Path(__file__).resolve().parents[1]


def quantile(values, fraction):
    """Linear interpolation, matching numpy.quantile's default method."""
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    left = int(index)
    weight = index - left
    return ordered[left] * (1 - weight) + ordered[min(left + 1, len(ordered) - 1)] * weight


def export_reference(source, output):
    source = Path(source)
    output = Path(output)
    data = source.read_bytes()
    errors = {months: [] for months in HORIZONS}
    with gzip.open(source, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        needed = {"horizon_months", "predicted_return_percent", "actual_return_percent"}
        if not reader.fieldnames or not needed.issubset(reader.fieldnames):
            raise ValueError("Historical audit is missing required columns")
        for row in reader:
            try:
                months = int(row["horizon_months"])
                if months not in errors:
                    continue
                predicted = float(row["predicted_return_percent"])
                actual = float(row["actual_return_percent"])
            except (ValueError, TypeError, KeyError):
                continue
            if isfinite(predicted) and isfinite(actual):
                errors[months].append(actual - predicted)
    horizons = []
    for months in HORIZONS:
        values = errors[months]
        if len(values) < MIN_SAMPLES:
            raise ValueError(f"Insufficient historical samples at {months} months")
        horizons.append({
            "months": months,
            "sample_count": len(values),
            "p10_pp": quantile(values, .1),
            "p90_pp": quantile(values, .9),
        })
    result = {
        "kind": "gwangjin_historical_model",
        "scope": "광진구 과거 모델 평가",
        "method": "실제 상승률 - 당시 예측 상승률의 10·90백분위수",
        "calibrated_for_current_model": False,
        "audit_sha256": sha256(data).hexdigest(),
        "horizons": horizons,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "frontend/dist/metadata/forecast-reference.json"
    result = export_reference(ROOT / "backend/audit.csv.gz", out)
    print("Historical reference:", {row["months"]: row["sample_count"] for row in result["horizons"]})
