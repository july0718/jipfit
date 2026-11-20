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


def shipped_reference():
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
    return month, expected, snapshot


def live_reference():
    from database import connect
    from inference import forecast, load_model

    with connect() as db:
        rows = db.execute("""
            SELECT m.*, a.representative_area
            FROM model_inputs m JOIN area_groups a USING (complex_id, area_group)
            JOIN district_input_months d ON d.district=split_part(m.complex_id,' ',2) AND m.month=d.month
        """).fetchall()
    if not rows:
        raise ValueError("No live model inputs")
    model = load_model()
    expected, inputs = {}, {}
    for row in rows:
        key = row["complex_id"], str(row["area_group"])
        inputs[key] = row
        expected[key] = {f["months"]: f["price_manwon"] for f in forecast(model, row)["forecasts"]}
    return "district latest", expected, inputs


def validate(base_url, live=False):
    month, expected, snapshot = live_reference() if live else shipped_reference()
    for (complex_id, area_group), prices in expected.items():
        as_of = snapshot[complex_id, area_group]["month"]
        params = urlencode(dict(complex_id=complex_id, area_group=area_group, as_of=as_of))
        with urlopen(f"{base_url.rstrip('/')}/api/predict?{params}", timeout=20) as response:
            result = json.load(response)
        forecasts = result["forecasts"]
        if (result["complex_id"], str(result["area_group"]), result["as_of"]) != (complex_id, area_group, as_of):
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
    cli.add_argument("--live", action="store_true", help="Compare against the current database inputs")
    args = cli.parse_args()
    validate(args.base_url, args.live)
