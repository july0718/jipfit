"""Compare the running prediction API with every shipped reference price."""
import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def explain_difference(reference, result, prices, forecast):
    area = float(result["representative_area"])
    live_unit = result["reference_price_manwon"] / area
    reference_unit = math.exp(float(reference["log_current"]))
    print(f"Reference unit price: {reference_unit:.8f}, API unit price: {live_unit:.8f} manwon/m2")
    print(f"API representative area: {area} m2")
    print(f"Expected: {prices[forecast['months']]:.8f}, API: {forecast['price_manwon']:.8f} manwon")


def validate(base_url):
    inputs = read_rows(ROOT / "db/gwangjin/inputs.csv")
    months = {row["month"] for row in inputs}
    if len(months) != 1:
        raise ValueError("Expected one reference month")
    month = months.pop()
    expected = defaultdict(dict)
    for row in read_rows(ROOT / "reference_prices.csv"):
        expected[row["complex_id"], row["area_group"]][int(row["months"])] = float(row["future_price_manwon"])
    if expected.keys() != {(row["complex_id"], row["area_group"]) for row in inputs}:
        raise ValueError("Reference group mismatch")
    snapshot = {(r["complex_id"], r["area_group"]): r for r in inputs}
    for (complex_id, area_group), prices in expected.items():
        params = urlencode(dict(complex_id=complex_id, area_group=area_group, as_of=month))
        with urlopen(f"{base_url.rstrip('/')}/api/predict?{params}", timeout=20) as response:
            result = json.load(response)
        forecasts = result["forecasts"]
        if (result["complex_id"], str(result["area_group"]), result["as_of"]) != (complex_id, area_group, month):
            raise ValueError(f"Unexpected snapshot for {complex_id} {area_group}")
        if [f["months"] for f in forecasts] != [3, 6, 12]:
            raise ValueError("Unexpected forecast horizons")
        for f in forecasts:
            price = prices[f["months"]]
            if not math.isclose(f["price_manwon"], price, rel_tol=1e-10, abs_tol=1e-8):
                explain_difference(snapshot[complex_id, area_group], result, prices, f)
                raise ValueError(f"Price mismatch: {complex_id} {area_group} {f}")
    print(f"{month}: {len(expected)} groups, {3 * len(expected)} API prices matched")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--base-url", default="http://127.0.0.1:8000")
    validate(cli.parse_args().base_url)
