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

from automatic_metadata import refresh_automatic_metadata
from districts import DISTRICTS, GWANGJIN
from features import build_inputs
from molit import fetch_month
from refresh_metadata import apply_metadata

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
FIELDS = "complex_id,area_group,contract_date,area,floor,price_manwon,is_cancelled"


def reconcile(db, month, complexes, wanted, district, *, history=False, refresh_day=None):
    first = month.start_time.date()
    end = (month + 1).start_time.date()
    with db.cursor() as cur:
        cur.execute("""
            SELECT complex_id, name, address, built FROM complexes
            WHERE split_part(complex_id,' ',2)=%s
        """, (DISTRICTS[district],))
        known_ids = set()
        current_names = defaultdict(list)
        for cid, name, address, built in cur:
            known_ids.add(cid)
            current_names[name, address, built].append(cid)
        # The trade key includes the reported name. Reuse an existing ID when a
        # verified name change now matches the source's name, parcel and year.
        aliases = {}
        for cid, name, address, built in complexes.values():
            matches = current_names[name, address, built]
            if cid not in known_ids and len(matches) == 1:
                aliases[cid] = matches[0]
        if aliases:
            complexes = {cid: row for cid, row in complexes.items() if cid not in aliases}
            remapped = Counter()
            for (cid, *trade), count in wanted.items():
                remapped[(aliases.get(cid, cid), *trade)] += count
            wanted = remapped
        insert_complex = """
            INSERT INTO complexes(complex_id,name,address,built) VALUES (%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """
        if refresh_day is None:
            cur.executemany(insert_complex, complexes.values())
        else:
            cur.executemany(f"""
                WITH inserted AS ({insert_complex} RETURNING complex_id)
                INSERT INTO complex_registrations(complex_id,refresh_day,district)
                SELECT complex_id,%s,%s FROM inserted
            """, ((*row, refresh_day, district) for row in complexes.values()))
        areas = defaultdict(list)
        for cid, group, _, area, _, _, _ in wanted:
            areas[cid, group].append(float(area))
        cur.executemany("""
            INSERT INTO area_groups VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
        """, [(cid, group, pd.Series(values).median()) for (cid, group), values in areas.items()])
        existing = defaultdict(list)
        cur.execute(f"SELECT trade_id,{FIELDS} FROM trades WHERE contract_date >= %s AND contract_date < %s AND split_part(complex_id,' ',2)=%s ORDER BY trade_id", (first, end, DISTRICTS[district]))
        for trade_id, *row in cur:
            existing[tuple(row)].append(trade_id)
        if existing and not wanted:
            raise ValueError("Empty API month would remove existing trades")
        counts = Counter({k: len(ids) for k, ids in existing.items()})
        if history and (counts - wanted):
            raise ValueError(f"{district} {month}: existing trades differ from API; month rolled back")
        removed = [id_ for k, ids in existing.items() if not k[-1] for id_ in ids[wanted[k]:]]
        if removed:
            # Preserve the prior reported row before replacing the active version.
            cur.execute(f"""
                WITH revised AS (
                    DELETE FROM trades WHERE trade_id = ANY(%s)
                    RETURNING trade_id,{FIELDS}
                )
                INSERT INTO trade_revisions(trade_id,{FIELDS})
                SELECT trade_id,{FIELDS} FROM revised
            """, (removed,))
        additions = wanted - counts
        cur.executemany(f"INSERT INTO trades({FIELDS}) VALUES (%s,%s,%s,%s,%s,%s,%s)", additions.elements())
        return sum(additions.values()), len(removed)


def publish(db, anchor, macro_path, district):
    cur = db.execute("""
        SELECT t.complex_id,t.area_group,t.contract_date,t.area,t.floor,t.price_manwon,c.built
        FROM trades t JOIN complexes c USING(complex_id)
        WHERE split_part(t.complex_id,' ',2)=%s AND NOT t.is_cancelled AND t.contract_date >= %s AND t.contract_date < %s
    """, (DISTRICTS[district], (anchor-19).start_time.date(), (anchor-1).start_time.date()))
    frame = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    inputs = build_inputs(frame, anchor, macro_path)
    with db.cursor() as cur:
        cur.executemany("UPDATE area_groups SET representative_area=%s WHERE complex_id=%s AND area_group=%s",
                        inputs[["representative_area","complex_id","area_group"]].itertuples(index=False,name=None))
        columns = inputs.drop(columns="representative_area")
        clean = columns.astype(object).where(columns.notna(), None)
        cur.execute("DELETE FROM model_inputs WHERE month=%s AND split_part(complex_id,' ',2)=%s", (str(anchor),DISTRICTS[district]))
        cur.executemany("INSERT INTO model_inputs ("+",".join(columns.columns)+") VALUES ("+
                        ",".join(["%s"]*len(columns.columns))+")", clean.itertuples(index=False,name=None))
    return len(inputs)


def refresh(district, temp):
    if not os.environ.get("MOLIT_SERVICE_KEY"):
        raise ValueError("MOLIT_SERVICE_KEY is required")
    today = datetime.now(KST).date()
    anchor = pd.Period(today, freq="M")
    with psycopg.connect(connect_timeout=8) as db:
        with db.transaction():
            db.execute((ROOT/"db/trade_status.sql").read_text(encoding="utf-8"))
            db.execute((ROOT/"db/regions.sql").read_text(encoding="utf-8"))
            db.execute((ROOT/"db/trade_revisions.sql").read_text(encoding="utf-8"))
            db.execute((ROOT/"db/metadata_refresh.sql").read_text(encoding="utf-8"))
        db.execute("SELECT pg_advisory_xact_lock(%s)", (1121505 if district == GWANGJIN else int(district),))
        db.execute("""CREATE TABLE IF NOT EXISTS refresh_runs (
            day date PRIMARY KEY, finished_at timestamptz NOT NULL, added integer NOT NULL,
            removed integer NOT NULL, forecast_groups integer NOT NULL)""")
        previous = db.execute("SELECT max(day) FROM district_refresh_runs WHERE district=%s", (district,)).fetchone()[0]
        if district == GWANGJIN and previous is None:
            previous = db.execute("SELECT max(day) FROM refresh_runs").fetchone()[0]
        if previous == today:
            return
        # Revisit the reporting window and catch up after downtime.
        start = min(anchor-3, pd.Period(previous, freq="M")-3) if previous else min(anchor-3, pd.Period("2026-10", freq="M"))
        if district != GWANGJIN and previous is None:
            start = anchor - 19
        added = removed = 0
        for month in pd.period_range(start, anchor):
            complexes, rows = fetch_month(month.strftime("%Y%m"), district)
            a, r = reconcile(db, month, complexes, rows, district, refresh_day=today)
            added += a
            removed += r
        subprocess.run([sys.executable, str(ROOT/"fetch_macro.py"), "--district", district, "--end-month", str(anchor-1)],
                       cwd=temp, check=True)
        n = publish(db, anchor, Path(temp)/"macro_raw.csv", district)
        if district == "11200":
            db.execute("""UPDATE complexes SET latitude=37.55605003075,longitude=127.03913424878
                WHERE address='서울특별시 성동구 행당동 380' AND name='서울숲리버뷰자이' AND latitude IS NULL""")
        db.execute("INSERT INTO district_refresh_runs VALUES (%s,%s,clock_timestamp(),%s,%s,%s)", (district,today,added,removed,n))
        if district == GWANGJIN:
            db.execute("INSERT INTO refresh_runs VALUES (%s,now(),%s,%s,%s)", (today,added,removed,n))
    print(f"{district} {today}: added={added} removed={removed} forecast_groups={n}", flush=True)


def refresh_many(districts):
    failed = []
    with tempfile.TemporaryDirectory() as temp:
        for district in districts:
            try:
                refresh(district, temp)
            except Exception as exc:
                failed.append(district)
                print(f"{district} update failed ({type(exc).__name__}): check source availability and credentials.", flush=True)
    return failed


def seconds_until_refresh():
    now = datetime.now(KST)
    next_run = datetime.combine(now.date(), time(6), KST)
    if now >= next_run:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--district", choices=DISTRICTS)
    parser.add_argument("--metadata-dir", type=Path)
    args = parser.parse_args()
    districts = [args.district] if args.district else list(DISTRICTS)
    while True:
        failed = refresh_many(districts)
        if not failed:
            try:
                today = datetime.now(KST).date()
                refresh_automatic_metadata(today, districts)
                if args.metadata_dir:
                    apply_metadata(args.metadata_dir, today)
            except Exception as exc:
                failed.append("verified metadata")
                print(f"Metadata update failed ({type(exc).__name__}); check the verified input.", flush=True)
        if args.once:
            if failed:
                raise SystemExit("Failed districts: " + ", ".join(failed))
            return
        if failed:
            clock.sleep(3600)
        else:
            clock.sleep(seconds_until_refresh())


if __name__ == "__main__":
    main()
