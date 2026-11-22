"""Historical comparison for the two-district pilot; never writes production data."""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from districts import GWANGJIN
from evaluate_districts import PILOT, audit_panel, evaluate as evaluate_panel, historical_trades, train
from joint_model import HORIZONS

ROOT = Path(__file__).resolve().parent


def score(district, test, predictions, asof):
    metrics = []
    predictions["flat"] = np.zeros((len(test), len(HORIZONS)))
    for j, h in enumerate(HORIZONS):
        observed = test[f"y{h}"].notna() & (pd.PeriodIndex(test.month, freq="M") + int(h) + 2 <= asof)
        if not observed.any():
            continue
        actual = 100 * np.expm1(test.loc[observed, f"y{h}"].to_numpy())
        for name, rates in predictions.items():
            error = 100 * np.expm1(rates[observed.to_numpy(), j]) - actual
            if not np.isfinite(error).all():
                raise ValueError(f"Non-finite predictions: {name}")
            metrics.append(dict(district=district, months=int(h), model=name, eligible=len(test),
                                scored=len(error), mae_pp=np.abs(error).mean(), bias_pp=error.mean()))
    return metrics


def evaluate(panel, cutoff, tests, asof, features, spec):
    return evaluate_panel(panel, cutoff, tests, asof, features, spec, score_fn=score)


def evaluate_seoul(cache, tests, asof, artifact):
    from districts import DISTRICTS
    origins = pd.period_range("2021-01", tests[-1], freq="M")
    history = historical_trades(GWANGJIN, origins[0] - 19, asof - 2, cache)
    reference = audit_panel(history, ROOT / "macro_raw.csv", origins)
    model = train("gwangjin", reference, tests[0], artifact.spec, artifact.features)
    indexes = pd.read_csv(ROOT / "seoul_audit_indexes.csv", index_col="reference_month")
    template = pd.read_csv(ROOT / "macro_raw.csv").set_index("reference_month")
    for code in DISTRICTS:
        trades = historical_trades(code, tests[0] - 19, asof - 2, cache)
        macro = template.copy()
        macro["gwangjin_apt_index"] = indexes[code]
        path = cache / f"macro-{code}.csv"
        macro.to_csv(path)
        test = audit_panel(trades, path, tests)
        scores = pd.DataFrame(score(code, test, {"gwangjin": model.predict(test)}, asof))
        scores.to_csv(cache / f"scores-{code}.csv", index=False)
        coverage = []
        for anchor in tests:
            known = trades[trades.period <= anchor - 2]
            total = len(known[["complex_id", "area_group"]].drop_duplicates())
            coverage.append(dict(month=str(anchor), known_groups=total, eligible=(test.month == str(anchor)).sum()))
        pd.DataFrame(coverage).to_csv(cache / f"coverage-{code}.csv", index=False)
        print(scores.round(2).to_string(index=False), flush=True)


def main():
    import joblib
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--seoul", action="store_true", help="Evaluate Gwangjin model transfer across all districts")
    args = parser.parse_args()
    args.cache.mkdir(exist_ok=True, parents=True)
    # Both districts use the November 2026 pilot's publication cutoff.
    asof = pd.Period("2026-11", freq="M")
    tests = pd.period_range("2025-10", "2026-03", freq="M")
    artifact = joblib.load(ROOT / "joint_model.joblib")["model"]
    if args.seoul:
        return evaluate_seoul(args.cache, tests, asof, artifact)
    origins = pd.period_range("2021-01", tests[-1], freq="M")
    panels = []
    coverage = []
    for district in (PILOT, GWANGJIN):
        trades = historical_trades(district, origins[0] - 19, asof - 2, args.cache)
        macro = ROOT / ("macro_raw.csv" if district == GWANGJIN else "seongdong_macro_audit.csv")
        print(f"Building historical inputs for {district} ({len(trades)} trades)", flush=True)
        panels.append(audit_panel(trades, macro, origins).assign(district=district))
        for anchor in tests:
            known = trades[trades.period <= anchor - 2]
            groups = len(known[["complex_id", "area_group"]].drop_duplicates())
            eligible = int((panels[-1].month == str(anchor)).sum())
            coverage.append(dict(district=district, month=str(anchor), known_groups=groups,
                                 eligible=eligible, coverage_percent=100 * eligible / groups))
    pd.DataFrame(coverage).to_csv(args.cache / "district_coverage.csv", index=False)
    scores = evaluate(pd.concat(panels, ignore_index=True), pd.Period("2025-04", freq="M"), tests, asof, artifact.features, artifact.spec)
    scores.to_csv(args.cache / "district_scores.csv", index=False)
    print(scores.round(2).to_string(index=False))
    maes = scores.pivot(index=["district", "months"], columns="model", values="mae_pp")
    pooled_wins = (maes.pooled < maes.gwangjin) & (maes.pooled < maes.flat)
    pilot = maes.loc[PILOT]
    if pooled_wins.all() and (pilot.pooled <= pilot.seongdong).all():
        decision = "Pooled model is the candidate for a subsequent independent test."
    elif ((pilot.seongdong < pilot.pooled) & (pilot.seongdong < pilot.gwangjin)
          & (pilot.seongdong < pilot.flat)).all():
        decision = "District-specific Seongdong model is the candidate for an independent test."
    else:
        decision = "No consistent MAE winner. Retain Gwangjin production and Seongdong pilot status."
    print(decision)
    print("scored/eligible: observable targets among eligible group-months; errors in percentage points.")


if __name__ == "__main__":
    main()
