"""Train one Seoul-wide candidate from the operational DB and checked-in macro history."""
import argparse
from pathlib import Path
import tempfile

import joblib
import numpy as np
import pandas as pd
import psycopg

from districts import DISTRICTS, GWANGJIN
from evaluate_districts import audit_panel, train
from joint_model import HORIZONS
from macro_features import load_monthly_macro

ROOT = Path(__file__).resolve().parent
TRADE_START = pd.Period("2006-01", freq="M")
EXPECTED_BACKFILL_MONTHS = 231


def read_trades(db, code, district, cutoff):
    cursor = db.execute("""
        SELECT t.complex_id, t.area_group, t.contract_date, t.area::float8,
               t.floor, t.price_manwon::float8, c.built
        FROM trades t JOIN complexes c USING (complex_id)
        WHERE split_part(t.complex_id, ' ', 2) = %s AND NOT t.is_cancelled
          AND t.contract_date >= %s AND t.contract_date < %s
        """, (district, TRADE_START.start_time.date(), (cutoff - 1).start_time.date()))
    rows = pd.DataFrame(cursor.fetchall(), columns=[column.name for column in cursor.description])
    dates = pd.to_datetime(rows.contract_date)
    valid = rows.built.between(1, dates.dt.year) & rows.area.gt(0) & rows.price_manwon.gt(0)
    print(f"{code}: {len(rows):,} source trades, {(~valid).sum():,} excluded (year/area/price)", flush=True)
    rows = rows.loc[valid].copy()
    rows["period"] = dates.loc[valid].dt.to_period("M")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", required=True, type=lambda value: pd.Period(value, freq="M"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT / "joint_model.joblib" or output.exists():
        parser.error("Choose a new candidate artifact path.")
    output.parent.mkdir(parents=True, exist_ok=True)

    # All districts start with the same full 18-month history window.
    origins = pd.period_range(TRADE_START + 19, args.cutoff - (int(HORIZONS.min()) + 2), freq="M")
    if not len(origins):
        parser.error("The cutoff is too early for a mature target.")
    artifact = joblib.load(ROOT / "joint_model.joblib")["model"]
    rates = pd.read_csv(ROOT / "macro_raw.csv", index_col="reference_month")
    indexes = pd.read_csv(ROOT / "seoul_audit_indexes.csv", index_col="reference_month")
    panels = []
    with tempfile.TemporaryDirectory() as scratch, psycopg.connect(connect_timeout=8) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        backfill = dict(db.execute("""
            SELECT district, count(*) FROM trade_history_imports
            WHERE month >= '2006-01-01' AND month < '2025-04-01'
            GROUP BY district
            """).fetchall())
        incomplete = [code for code in DISTRICTS
                      if code != GWANGJIN and backfill.get(code) != EXPECTED_BACKFILL_MONTHS]
        if incomplete:
            raise ValueError(f"Incomplete historical import: {', '.join(incomplete)}")
        for code, district in DISTRICTS.items():
            print(f"{code}: building inputs for {origins[0]} to {origins[-1]}", flush=True)
            macro = rates.copy()
            macro["gwangjin_apt_index"] = indexes[code]
            macro_path = Path(scratch) / f"{code}.csv"
            macro.to_csv(macro_path)
            coverage, _ = load_monthly_macro(macro_path, origins)
            if not coverage.macro_complete.all():
                missing = coverage.loc[~coverage.macro_complete, "month"].tolist()
                raise ValueError(f"{code}: incomplete macro data for {', '.join(missing)}")
            rows = read_trades(db, code, district, args.cutoff)
            panel = audit_panel(rows, macro_path, origins)
            target_availability = pd.PeriodIndex(panel.month, freq="M").asi8[:, None] + HORIZONS + 2
            targets = panel[[f"y{horizon}" for horizon in HORIZONS]].to_numpy()
            mature = np.isfinite(targets) & (target_availability <= args.cutoff.ordinal)
            if not mature.any(axis=0).all():
                raise ValueError(f"{code}: missing mature targets for one or more horizons")
            panels.append(panel.assign(district=code))
            print(f"{code}: {len(panel):,} input rows; mature 3/6/12m targets {mature.sum(axis=0).tolist()}", flush=True)

    frame = pd.concat(panels, ignore_index=True)
    print(f"Fitting pooled Seoul model on {len(frame):,} input rows", flush=True)
    model = train("seoul", frame, args.cutoff, artifact.spec, artifact.features)
    if not np.isfinite(model.predict(frame.iloc[:5])).all():
        raise ValueError("Candidate produced non-finite predictions")
    temporary = output.with_suffix(output.suffix + ".tmp")
    joblib.dump({"model": model, "scope": list(DISTRICTS), "cutoff": str(args.cutoff),
                 "trade_start": str(TRADE_START), "first_origin": str(origins[0])}, temporary)
    temporary.replace(output)
    print(f"Saved {output}: {model.training_rows_:,} fitted rows, {model.training_tasks_}", flush=True)


if __name__ == "__main__":
    main()
