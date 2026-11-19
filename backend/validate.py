"""Replay the shipped reference inputs and report historical return errors."""
from pathlib import Path

import numpy as np
import pandas as pd

from features import build_inputs
from inference import forecast, load_model

ROOT = Path(__file__).resolve().parent


def validate():
    district = ROOT / "db/gwangjin"
    trades = pd.read_csv(district/"trades.csv").merge(
        pd.read_csv(district/"complexes.csv")[["complex_id","built"]], validate="many_to_one")
    reference = pd.read_csv(district/"inputs.csv")
    month = reference.month.unique().item()
    rebuilt = build_inputs(trades, pd.Period(month, freq="M"), ROOT/"macro_raw.csv")
    index = ["complex_id","area_group","month"]
    left = reference.set_index(index).sort_index()
    right = rebuilt.set_index(index).sort_index()
    pd.testing.assert_index_equal(left.index, right.index)
    np.testing.assert_allclose(left, right[left.columns], rtol=1e-10, atol=1e-10, equal_nan=True)
    model = load_model()
    prices = []
    for row in rebuilt.to_dict("records"):
        prices.extend(f["price_manwon"] for f in forecast(model, row)["forecasts"])
    expected = pd.read_csv(ROOT/"reference_prices.csv")
    keys = rebuilt[index[:2]].merge(pd.DataFrame({"months":[3,6,12]}),how="cross")
    keys["price"] = prices
    paired = keys.merge(expected,how="outer",validate="one_to_one",on=index[:2]+["months"])
    np.testing.assert_allclose(paired.price, paired.future_price_manwon, rtol=1e-10, atol=1e-8)
    print(f"Snapshot {month}: {len(rebuilt)} groups, {len(prices)} predictions matched.")
    audit = pd.read_csv(ROOT/"audit.csv.gz")
    if not (audit.training_cutoff <= audit.month).all():
        raise ValueError("Forecast precedes its training cutoff")
    for months, group in audit.groupby("horizon_months"):
        error = group.predicted_return_percent - group.actual_return_percent
        print(f"{months}m | n={len(group)} | MAE={error.abs().mean():.2f} pp | "
              f"bias={error.mean():+.2f} pp | flat-price MAE={group.actual_return_percent.abs().mean():.2f} pp")


if __name__ == "__main__":
    validate()
