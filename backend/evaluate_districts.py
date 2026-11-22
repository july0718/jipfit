"""Historical comparison for the two-district pilot; never writes production data."""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from districts import DISTRICTS, GWANGJIN
from features import build_inputs
from joint_model import HORIZONS, JointCurve
from molit import fetch_month

ROOT = Path(__file__).resolve().parent
PILOT = "11200"


def historical_trades(code, first, last, cache):
    if code == GWANGJIN:
        base = ROOT / "db/gwangjin"
        trades = pd.read_csv(base / "trades.csv").merge(
            pd.read_csv(base / "complexes.csv")[["complex_id", "built"]], validate="many_to_one")
    else:
        parts = []
        for month in pd.period_range(first, last, freq="M"):
            path = cache / f"{code}-{month}.csv"
            if not path.exists():
                try:
                    complexes, counts = fetch_month(month.strftime("%Y%m"), code)
                except Exception as exc:
                    raise RuntimeError(f"{code} {month}: collection failed ({type(exc).__name__}); "
                                       "check the updater API key/network and retry with the same cache") from None
                records = []
                for (cid, area_group, day, area, floor, price, cancelled), count in counts.items():
                    if not cancelled:
                        records.extend([(cid, area_group, day, area, floor, price, complexes[cid][3])] * count)
                frame = pd.DataFrame(records, columns=["complex_id", "area_group", "contract_date", "area",
                                                       "floor", "price_manwon", "built"])
                temp = path.with_suffix(".tmp")
                frame.to_csv(temp, index=False)
                temp.replace(path)
                print(f"Collected {code} {month}: {len(frame)} trades", flush=True)
            parts.append(pd.read_csv(path))
        trades = pd.concat(parts, ignore_index=True)
    trades["period"] = pd.to_datetime(trades.contract_date).dt.to_period("M")
    return trades.loc[trades.period.between(first, last)].copy()


def audit_panel(trades, macro, origins):
    trades["ppm"] = trades.price_manwon / trades.area
    keys = ["complex_id", "area_group"]
    monthly = trades.groupby(keys + ["period"]).ppm.median().unstack("period")
    frames = []
    for origin in origins:
        history = trades.loc[trades.period.between(origin - 19, origin - 2)]
        rows = build_inputs(history, origin, macro).set_index(keys)
        for horizon in HORIZONS:
            end = origin + int(horizon)
            target = monthly.reindex(columns=pd.period_range(end - 2, end, freq="M")).median(axis=1)
            rows[f"y{horizon}"] = np.log(target.reindex(rows.index)) - rows.log_current
        frames.append(rows.reset_index())
    return pd.concat(frames, ignore_index=True)


def train(name, frame, cutoff, spec, features):
    available = pd.PeriodIndex(frame.month, freq="M").asi8[:, None] + HORIZONS + 2
    y = frame[[f"y{h}" for h in HORIZONS]].to_numpy()
    if not np.all((np.isfinite(y) & (available <= cutoff.ordinal)).any(axis=0)):
        raise ValueError(f"{name}: missing mature training targets")
    return JointCurve(spec, features).fit(frame, y, available, cutoff)


def score(district, test, predictions, asof):
    metrics = []
    predictions["flat"] = np.zeros((len(test), len(HORIZONS)))
    for j, h in enumerate(HORIZONS):
        observed = test[f"y{h}"].notna() & (pd.PeriodIndex(test.month, freq="M") + int(h) + 2 <= asof)
        if not observed.any():
            raise ValueError(f"No mature observations: {district} {h}m")
        actual = 100 * np.expm1(test.loc[observed, f"y{h}"].to_numpy())
        for name, rates in predictions.items():
            error = 100 * np.expm1(rates[observed.to_numpy(), j]) - actual
            if not np.isfinite(error).all():
                raise ValueError(f"Non-finite predictions: {name}")
            metrics.append(dict(district=district, months=int(h), model=name, eligible=len(test),
                                scored=len(error), mae_pp=np.abs(error).mean(), bias_pp=error.mean()))
    return metrics


def evaluate(panel, cutoff, tests, asof, features, spec, *, score_fn=score):
    models = {}
    for name, districts in [("pooled", [GWANGJIN, PILOT]), ("gwangjin", [GWANGJIN]), ("seongdong", [PILOT])]:
        frame = panel[panel.district.isin(districts) & (panel.month < str(cutoff))]
        if frame.empty:
            continue
        models[name] = train(name, frame, cutoff, spec, features)
    metrics = []
    for district in (GWANGJIN, PILOT):
        test = panel[(panel.district == district) & panel.month.isin(tests.astype(str))]
        if test.empty:
            continue
        names = ["gwangjin", "pooled", "seongdong"] if district == PILOT else ["gwangjin", "pooled"]
        predictions = {name: models[name].predict(test) for name in names}
        metrics.extend(score_fn(district, test, predictions, asof))
    return pd.DataFrame(metrics)


def evaluate_seoul(cache, tests, asof, artifact, districts):
    origins = pd.period_range("2021-01", tests[-1], freq="M")
    history = historical_trades(GWANGJIN, origins[0] - 19, asof - 2, cache)
    reference = audit_panel(history, ROOT / "macro_raw.csv", origins)
    model = train("gwangjin", reference, tests[0], artifact.spec, artifact.features)
    indexes = pd.read_csv(ROOT / "seoul_audit_indexes.csv", index_col="reference_month")
    template = pd.read_csv(ROOT / "macro_raw.csv").set_index("reference_month")
    for code in districts:
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
    parser.add_argument("--district", choices=DISTRICTS, action="append", help="Limit --seoul to this district (repeatable)")
    args = parser.parse_args()
    if args.district and not args.seoul:
        parser.error("--district requires --seoul")
    args.cache.mkdir(exist_ok=True, parents=True)
    # Both districts use the November 2026 pilot's publication cutoff.
    asof = pd.Period("2026-11", freq="M")
    tests = pd.period_range("2025-04", "2025-09", freq="M")
    artifact = joblib.load(ROOT / "joint_model.joblib")["model"]
    if args.seoul:
        return evaluate_seoul(args.cache, tests, asof, artifact, args.district or DISTRICTS)
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
    scores = evaluate(pd.concat(panels, ignore_index=True), tests[0], tests, asof, artifact.features, artifact.spec)
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
