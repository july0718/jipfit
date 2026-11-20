"""Daily KST district refresh, also executable once with --once."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time as clock

import pandas as pd
import psycopg

from features import build_inputs
from molit import fetch_month

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
FIELDS = "complex_id,area_group,contract_date,area,floor,price_manwon,is_cancelled"


def reconcile(db, month, complexes, wanted):
    first = month.start_time.date()
    end = (month + 1).start_time.date()
    with db.cursor() as cur:
        cur.executemany("""
            INSERT INTO complexes(complex_id,name,address,built) VALUES (%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """, complexes.values())
        areas = defaultdict(list)
        for cid, group, _, area, _, _, _ in wanted:
            areas[cid, group].append(float(area))
        cur.executemany("""
            INSERT INTO area_groups VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
        """, [(cid, group, pd.Series(values).median()) for (cid, group), values in areas.items()])
        existing = defaultdict(list)
        cur.execute(f"SELECT trade_id,{FIELDS} FROM trades WHERE contract_date >= %s AND contract_date < %s ORDER BY trade_id", (first, end))
        for trade_id, *row in cur:
            existing[tuple(row)].append(trade_id)
        if existing and not wanted:
            raise ValueError("Empty API month would remove existing trades")
        counts = Counter({k: len(ids) for k, ids in existing.items()})
        removed = [id_ for k, ids in existing.items() if not k[-1] for id_ in ids[wanted[k]:]]
        if removed:
            cur.execute("DELETE FROM trades WHERE trade_id = ANY(%s)", (removed,))
        additions = wanted - counts
        cur.executemany(f"INSERT INTO trades({FIELDS}) VALUES (%s,%s,%s,%s,%s,%s,%s)", additions.elements())
        return sum(additions.values()), len(removed)


def publish(db, anchor, macro_path):
    cur = db.execute("""
        SELECT t.complex_id,t.area_group,t.contract_date,t.area,t.floor,t.price_manwon,c.built
        FROM trades t JOIN complexes c USING(complex_id)
        WHERE NOT t.is_cancelled AND t.contract_date >= %s AND t.contract_date < %s
    """, ((anchor-19).start_time.date(), (anchor-1).start_time.date()))
    frame = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    inputs = build_inputs(frame, anchor, macro_path)
    with db.cursor() as cur:
        cur.executemany("UPDATE area_groups SET representative_area=%s WHERE complex_id=%s AND area_group=%s",
                        inputs[["representative_area","complex_id","area_group"]].itertuples(index=False,name=None))
        columns = inputs.drop(columns="representative_area")
        clean = columns.astype(object).where(columns.notna(), None)
        cur.execute("DELETE FROM model_inputs WHERE month=%s", (str(anchor),))
        cur.executemany("INSERT INTO model_inputs ("+",".join(columns.columns)+") VALUES ("+
                        ",".join(["%s"]*len(columns.columns))+")", clean.itertuples(index=False,name=None))
    return len(inputs)


def refresh():
    if not os.environ.get("MOLIT_SERVICE_KEY"):
        raise ValueError("MOLIT_SERVICE_KEY is required")
    today = datetime.now(KST).date()
    anchor = pd.Period(today, freq="M")
    with psycopg.connect(connect_timeout=8) as db, tempfile.TemporaryDirectory() as temp:
        with db.transaction():
            db.execute((ROOT/"db/trade_status.sql").read_text(encoding="utf-8"))
        db.execute("SELECT pg_advisory_xact_lock(1121505)")
        db.execute("""CREATE TABLE IF NOT EXISTS refresh_runs (
            day date PRIMARY KEY, finished_at timestamptz NOT NULL, added integer NOT NULL,
            removed integer NOT NULL, forecast_groups integer NOT NULL)""")
        previous = db.execute("SELECT max(day) FROM refresh_runs").fetchone()[0]
        if previous == today:
            return
        # Revisit the reporting window and catch up after downtime.
        start = min(anchor-3, pd.Period(previous, freq="M")-3) if previous else min(anchor-3, pd.Period("2026-10", freq="M"))
        added = removed = 0
        for month in pd.period_range(start, anchor):
            complexes, rows = fetch_month(month.strftime("%Y%m"))
            a, r = reconcile(db, month, complexes, rows)
            added += a
            removed += r
        subprocess.run([sys.executable, str(ROOT/"fetch_macro.py"), "--refresh", "--end-month", str(anchor-1)],
                       cwd=temp, check=True)
        n = publish(db, anchor, Path(temp)/"macro_raw.csv")
        db.execute("INSERT INTO refresh_runs VALUES (%s,now(),%s,%s,%s)", (today,added,removed,n))
    print(f"{today}: added={added} removed={removed} forecast_groups={n}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        refresh()
        return
    while True:
        try:
            refresh()
        except Exception as exc:
            # Network errors can contain the credentialed request URL.
            print(f"Refresh failed ({type(exc).__name__}); retry in one hour.", flush=True)
            clock.sleep(3600)
        else:
            now = datetime.now(KST)
            tomorrow = datetime.combine(now.date()+timedelta(days=1), time(6), KST)
            clock.sleep((tomorrow-now).total_seconds())


if __name__ == "__main__":
    main()
