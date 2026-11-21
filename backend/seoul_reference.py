"""Aggregate row-level *Seoul pooled HGB* holdout residuals for feature #19.

The inputs are individual out-of-sample predictions, not MAE/bias summary rows.
Never create quantiles from aggregate metrics. The 2025-04 model is a historical
validation artifact; these bands are not calibrated for the current live model.
This module uses the standard library so it can also validate published JSON.
"""
from collections import defaultdict
from hashlib import sha256
from math import isfinite
import json
from pathlib import Path

from districts import DISTRICTS

HORIZONS = (3, 6, 12)
TEST_MONTHS = tuple(f"2025-{m:02d}" for m in range(4, 10))
CUTOFF = "2025-04"
ASOF = "2026-11"
MIN_SAMPLES = 30
KIND = "seoul_hgb_holdout"


def percentile(values, p):
    values = sorted(values)
    if not values:
        raise ValueError("Cannot calculate percentile with no observations")
    index = (len(values) - 1) * p
    left = int(index)
    weight = index - left
    return values[left] * (1 - weight) + values[min(left + 1, len(values) - 1)] * weight


def build_reference(records, model_sha256):
    if not isinstance(model_sha256, str) or len(model_sha256) != 64 or any(c not in "0123456789abcdef" for c in model_sha256):
        raise ValueError("Expected verified SHA-256 of historical Seoul model")
    errors = defaultdict(list)
    unique = set()
    districts, months = set(), set()
    for record in records:
        if record.get("model") != "seoul":
            raise ValueError("Only the Seoul pooled historical model is eligible")
        district, month, horizon = record["district"], record["month"], int(record["horizon"])
        if district not in DISTRICTS or month not in TEST_MONTHS or horizon not in HORIZONS:
            raise ValueError("Outside the declared Seoul holdout set")
        key = (district, month, record["complex_id"], record["area_group"], horizon)
        if key in unique:
            raise ValueError("Duplicate district/complex/area/month/horizon observation")
        unique.add(key)
        predicted = float(record["predicted_return_percent"])
        actual = float(record["actual_return_percent"])
        residual = float(record["residual_pp"])
        if not all(map(isfinite, (predicted, actual, residual))) or abs((actual-predicted)-residual) > 1e-6:
            raise ValueError("Invalid individual holdout residual")
        errors[horizon].append(residual)
        districts.add(district)
        months.add(month)
    if districts != set(DISTRICTS):
        raise ValueError(f"Expected all 25 districts, got {len(districts)}")
    if months != set(TEST_MONTHS):
        raise ValueError("Missing one or more holdout origin months")
    horizons = []
    for horizon in HORIZONS:
        values = errors[horizon]
        if len(values) < MIN_SAMPLES:
            raise ValueError(f"Insufficient {horizon}-month holdout observations")
        # The evaluation's bias is prediction - actual; residual is actual - prediction.
        horizons.append({
            "months": horizon,
            "sample_count": len(values),
            "p10_pp": percentile(values, .1),
            "p90_pp": percentile(values, .9),
            "mae_pp": sum(abs(e) for e in values) / len(values),
            "bias_pp": -sum(values) / len(values),
        })
    return {
        "schema_version": 1,
        "kind": KIND,
        "scope": "서울 25개 자치구 통합 HGB 과거 홀드아웃",
        "training_cutoff": CUTOFF,
        "holdout_start": TEST_MONTHS[0],
        "holdout_end": TEST_MONTHS[-1],
        "as_of": ASOF,
        "model_sha256": model_sha256,
        "calibrated_for_current_model": False,
        "method": "실제 수익률 - 과거 서울 통합 HGB 예측 수익률의 10·90백분위(%p)",
        "horizons": horizons,
    }


def validate_reference(document):
    if not isinstance(document, dict) or document.get("schema_version") != 1 or document.get("kind") != KIND:
        raise ValueError("Not a Seoul HGB holdout distribution")
    for k, v in [("training_cutoff", CUTOFF), ("holdout_start", TEST_MONTHS[0]),
                 ("holdout_end", TEST_MONTHS[-1]), ("as_of", ASOF),
                 ("calibrated_for_current_model", False)]:
        if document.get(k) != v or type(document.get(k)) is not type(v):
            raise ValueError(f"Reference provenance mismatch: {k}")
    digest = document.get("model_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Missing validated model SHA")
    horizons = document.get("horizons")
    if not isinstance(horizons, list) or [r.get("months") for r in horizons] != list(HORIZONS):
        raise ValueError("Incomplete 3/6/12-month reference")
    for row in horizons:
        if type(row.get("sample_count")) is not int or row["sample_count"] < MIN_SAMPLES:
            raise ValueError("Insufficient holdout sample count")
        for key in ("p10_pp", "p90_pp", "mae_pp", "bias_pp"):
            value = row.get(key)
            if type(value) not in (float, int) or not isfinite(value):
                raise ValueError(f"Non-finite or missing {key}")
        if row["p10_pp"] > row["p90_pp"] or row["mae_pp"] < 0:
            raise ValueError("Invalid residual distribution")
    return document


def write_reference(document, destination):
    validate_reference(document)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # No complex identifiers, member data, or raw trades enter the public JSON.
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")
    return destination


def check_file(path):
    return validate_reference(json.loads(Path(path).read_text(encoding="utf8")))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Validate published Seoul holdout reference")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    doc = check_file(args.path)
    print("Verified Seoul holdout distribution:", {r["months"]: r["sample_count"] for r in doc["horizons"]})
