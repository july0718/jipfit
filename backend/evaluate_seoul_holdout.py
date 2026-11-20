
"""Evaluate the April 2025 Seoul HGB model without changing production data."""
import argparse
import tempfile
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import psycopg

from districts import DISTRICTS, GWANGJIN
from evaluate_districts import audit_panel, train
from joint_model import HORIZONS
from macro_features import load_monthly_macro
from train_seoul import TRADE_START, read_trades

ROOT = Path(__file__).resolve().parent
CUTOFF = pd.Period("2025-04", freq="M")
ASOF = pd.Period("2026-11", freq="M")
TESTS = pd.period_range("2025-04", "2025-09", freq="M")


def aggregate(frame, keys):
    result = frame.groupby(keys, as_index=False).agg(
        eligible=("eligible", "sum"),
        scored=("scored", "sum"),
        abs_error_sum=("abs_error_sum", "sum"),
        error_sum=("error_sum", "sum"),
        direction_hits=("direction_hits", "sum"),
    )
    result["mae_pp"] = result.abs_error_sum / result.scored
    result["bias_pp"] = result.error_sum / result.scored
    result["direction_pct"] = (
        100 * result.direction_hits / result.scored
    )
    result["coverage_pct"] = (
        100 * result.scored / result.eligible
    )
    return result.drop(
        columns=["abs_error_sum", "error_sum", "direction_hits"]
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()

    artifact = joblib.load(args.model)

    if artifact.get("cutoff") != str(CUTOFF):
        raise ValueError("Model cutoff must be 2025-04")

    if set(artifact.get("scope", [])) != set(DISTRICTS):
        raise ValueError("Model scope is not Seoul's 25 districts")

    seoul = artifact["model"]

    rates = pd.read_csv(
        ROOT / "macro_raw.csv", index_col="reference_month"
    )
    indexes = pd.read_csv(
        ROOT / "seoul_audit_indexes.csv",
        index_col="reference_month",
    )

    records = []

    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)

        def macro_path(code):
            macro = rates.copy()
            macro["gwangjin_apt_index"] = indexes[code]
            path = temp / f"macro-{code}.csv"
            macro.to_csv(path)
            return path

        with psycopg.connect(connect_timeout=8) as db:
            db.execute(
                "SET TRANSACTION ISOLATION LEVEL "
                "REPEATABLE READ READ ONLY"
            )

            # Refit only the Gwangjin comparison model
            # using data available at the same cutoff.
            print("Training historical Gwangjin baseline", flush=True)

            gw_macro = macro_path(GWANGJIN)
            gw_trades = read_trades(
                db, GWANGJIN, DISTRICTS[GWANGJIN], CUTOFF
            )

            origins = pd.period_range(
                TRADE_START + 19,
                CUTOFF - (int(HORIZONS.min()) + 2),
                freq="M",
            )

            gw_panel = audit_panel(
                gw_trades, gw_macro, origins
            ).assign(district=GWANGJIN)

            gw_model = train(
                "gwangjin_2025",
                gw_panel,
                CUTOFF,
                seoul.spec,
                seoul.features,
            )

            print("Evaluating Seoul's 25 districts", flush=True)

            for code, district in DISTRICTS.items():
                path = macro_path(code)

                coverage, _ = load_monthly_macro(path, TESTS)
                if not coverage.macro_complete.all():
                    raise ValueError(
                        f"Incomplete macro inputs for {code}"
                    )

                # ASOF=2026-11 reads transactions
                # through September 2026.
                trades = read_trades(db, code, district, ASOF)
                panel = audit_panel(trades, path, TESTS)

                for month in TESTS:
                    test = panel[
                        panel.month.astype(str) == str(month)
                    ].copy()

                    if test.empty:
                        continue

                    predictions = {
                        "seoul": seoul.predict(test),
                        "gwangjin_2025": gw_model.predict(test),
                        "flat": np.zeros(
                            (len(test), len(HORIZONS))
                        ),
                    }

                    for name, prediction in predictions.items():
                        if not np.isfinite(prediction).all():
                            raise ValueError(
                                f"Non-finite prediction: {code}, {name}"
                            )

                    for j, h in enumerate(HORIZONS):
                        if month + int(h) + 2 > ASOF:
                            continue

                        y = pd.to_numeric(
                            test[f"y{h}"], errors="coerce"
                        ).to_numpy(dtype=float)

                        observed = np.isfinite(y)
                        if not observed.any():
                            continue

                        actual = 100 * np.expm1(y[observed])

                        for name, prediction in predictions.items():
                            predicted = (
                                100 * np.expm1(
                                    prediction[observed, j]
                                )
                            )

                            error = predicted - actual

                            records.append({
                                "district": code,
                                "month": str(month),
                                "horizon": int(h),
                                "model": name,
                                "eligible": len(test),
                                "scored": len(actual),
                                "abs_error_sum": float(
                                    np.abs(error).sum()
                                ),
                                "error_sum": float(error.sum()),
                                "direction_hits": int(
                                    (
                                        np.sign(predicted)
                                        == np.sign(actual)
                                    ).sum()
                                ),
                            })

                print(f"Evaluated {code}", flush=True)

    if not records:
        raise ValueError("No eligible holdout observations")

    detail = pd.DataFrame(records)

    missing = set(DISTRICTS) - set(detail.district)
    if missing:
        raise ValueError(f"Unscored districts: {missing}")

    summary = aggregate(detail, ["horizon", "model"])
    by_district = aggregate(
        detail, ["district", "horizon", "model"]
    )
    by_month = aggregate(
        detail, ["month", "horizon", "model"]
    )

    prefix = args.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)

    summary.to_csv(
        prefix.parent / f"{prefix.name}-summary.csv",
        index=False,
    )
    by_district.to_csv(
        prefix.parent / f"{prefix.name}-district.csv",
        index=False,
    )
    by_month.to_csv(
        prefix.parent / f"{prefix.name}-month.csv",
        index=False,
    )
    detail.to_csv(
        prefix.parent / f"{prefix.name}-detail.csv",
        index=False,
    )

    print("\n=== SEOUL HOLDOUT SUMMARY ===")
    print(summary.round(3).to_string(index=False))
    print("\nSaved four CSV reports.", flush=True)


if __name__ == "__main__":
    main()
