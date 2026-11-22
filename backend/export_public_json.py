"""Build versioned, public read-only JSON snapshots from PostgreSQL.

Usage (DB must be available):
  python export_public_json.py --out /export
The manifest is published last. No trade rows are modified or deleted.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from math import expm1, isfinite
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from fastapi.encoders import jsonable_encoder

from check_public_json import check, trade_filename
from districts import DISTRICTS
from inference import HORIZONS, load_model, predict_growth


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable_encoder(payload), ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False), encoding="utf-8")


def load_home_rows(db):
    # Same public response fields as GET /api/complexes?include_sort=true.
    homes = db.execute("""
        SELECT c.complex_id,c.name,c.address,c.built,c.latitude,c.longitude,
               a.area_group,a.representative_area,
               (m.complex_id IS NOT NULL) AS predictable
        FROM complexes c JOIN area_groups a USING(complex_id)
        LEFT JOIN district_input_months d ON d.district=split_part(c.complex_id,' ',2)
        LEFT JOIN model_inputs m ON m.complex_id=c.complex_id
             AND m.area_group=a.area_group AND m.month=d.month
        ORDER BY c.name,c.complex_id,a.area_group
    """).fetchall()
    stats = {r["complex_id"]: r for r in db.execute("""
        SELECT complex_id,count(*) AS trade_count,max(contract_date) AS last_trade
        FROM trades WHERE NOT is_cancelled GROUP BY complex_id
    """)}
    prices = {(r["complex_id"], r["area_group"]): r["price_manwon"]
              for r in db.execute("""
                  SELECT DISTINCT ON (complex_id,area_group)
                      complex_id,area_group,price_manwon
                  FROM trades WHERE NOT is_cancelled
                  ORDER BY complex_id,area_group,contract_date DESC,trade_id DESC
              """)}
    inputs = db.execute("""
        SELECT DISTINCT ON (complex_id) m.*
        FROM model_inputs m JOIN district_input_months d
          ON d.district=split_part(m.complex_id,' ',2) AND m.month=d.month
        ORDER BY complex_id,area_group
    """).fetchall()
    returns = {}
    if inputs:
        model = load_model()
        for row, rates in zip(inputs, predict_growth(model, inputs), strict=True):
            returns[row["complex_id"]] = {
                horizon: 100 * expm1(float(rate)) for horizon, rate in zip(HORIZONS, rates, strict=True)
                if isfinite(float(rate))
            }
    for home in homes:
        cid = home["complex_id"]
        key = (cid, home["area_group"])
        marker_price = prices.get(key)
        if marker_price is not None and not isfinite(float(marker_price)):
            marker_price = None
        home["marker_price_manwon"] = marker_price
        home["trade_count"] = stats.get(cid, {}).get("trade_count", 0)
        home["last_trade"] = stats.get(cid, {}).get("last_trade")
        home["sort_returns"] = returns.get(cid, {})
    return homes


def build_release(staging):
    generated = datetime.now(timezone.utc)
    release = "releases/" + generated.strftime("%Y%m%dT%H%M%S%fZ")
    target = staging / release
    target.mkdir(parents=True, exist_ok=False)
    districts = [{"code": code, "name": name} for code, name in DISTRICTS.items()]
    totals = {"area_groups": 0, "complexes": 0, "transactions": 0, "cancelled_transactions": 0}
    manifest = {
        "schema_version": 1, "generated_at": generated.isoformat(), "release": release,
        "districts": districts, "totals": totals,
        "notes": "Public read-only snapshot with sorting values. Full forecasts use the live API."
    }
    with psycopg.connect(row_factory=dict_row, connect_timeout=10) as db:
        # All exported files represent one consistent committed DB snapshot.
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        homes = load_home_rows(db)
        group_rows = defaultdict(list)
        recent_rows = defaultdict(list)
        home_keys = set()
        all_ids = set()
        by_name = {entry["name"]: entry["code"] for entry in districts}
        for home in homes:
            district = home["complex_id"].split()[1]
            if district != home["address"].split()[1]:
                raise ValueError(f"District differs between ID and address: {home['complex_id']}")
            code = by_name.get(district)
            if code is None:
                raise ValueError(f"Unknown district in complexes: {district}")
            all_ids.add(home["complex_id"])
            home_keys.add((home["complex_id"], home["area_group"]))
            group_rows[code].append({key: value for key, value in home.items()
                                     if key not in {"marker_price_manwon", "trade_count", "last_trade", "sort_returns"}})
            recent_rows[code].append({key: home[key] for key in
                                     ("complex_id", "area_group", "marker_price_manwon",
                                      "trade_count", "last_trade", "sort_returns")})
        totals["area_groups"] = len(homes)
        totals["complexes"] = len(all_ids)
        for entry in districts:
            code = entry["code"]
            rows = group_rows[code]
            points = [(float(h["latitude"]), float(h["longitude"])) for h in rows
                      if h["latitude"] is not None and h["longitude"] is not None
                      and isfinite(float(h["latitude"])) and isfinite(float(h["longitude"]))]
            if points:
                lat, lng = zip(*points)
                entry["bounds"] = [min(lat), min(lng), max(lat), max(lng)]
            write_json(target / "complexes" / f"{code}.json", rows)
            write_json(target / "latest-trades" / f"{code}.json", recent_rows[code])

        expected = db.execute("""
            SELECT count(*) AS total, count(*) FILTER (WHERE is_cancelled) AS cancelled FROM trades
        """).fetchone()
        current_id = None
        area_groups = {}
        seen_ids = set()
        def flush():
            if current_id is None:
                return
            district_name = current_id.split(" ")[1]
            code = by_name.get(district_name)
            if code is None:
                raise ValueError(f"Unknown trade district: {district_name}")
            write_json(target / "transactions" / code / trade_filename(current_id),
                       {"complex_id": current_id, "area_groups": area_groups})
            seen_ids.add(current_id)

        with db.cursor(name="public_json_export") as cursor:
            cursor.itersize = 5000
            cursor.execute("""
                SELECT complex_id,area_group,contract_date,area,floor,price_manwon,is_cancelled
                FROM trades ORDER BY complex_id,area_group,contract_date DESC,trade_id DESC
            """)
            for row in cursor:
                cid = row["complex_id"]
                if (cid, row["area_group"]) not in home_keys:
                    raise ValueError(f"Trade references an absent group: {cid}, {row['area_group']}")
                if cid != current_id:
                    flush()
                    current_id = cid
                    area_groups = {}
                area_groups.setdefault(str(row["area_group"]), []).append({
                    key: row[key] for key in
                    ("contract_date", "area", "floor", "price_manwon", "is_cancelled")
                })
                totals["transactions"] += 1
                totals["cancelled_transactions"] += int(row["is_cancelled"])
        flush()
        if (totals["transactions"], totals["cancelled_transactions"]) != (expected["total"], expected["cancelled"]):
            raise RuntimeError(f"PostgreSQL trade counts differ from the export: {expected}")
        for cid in all_ids - seen_ids:
            code = by_name[cid.split(" ")[1]]
            write_json(target / "transactions" / code / trade_filename(cid),
                       {"complex_id": cid, "area_groups": {}})
        check(staging, manifest=manifest, db=db)
    return manifest


def export(out):
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out, prefix=".snapshot-") as temporary:
        staging = Path(temporary)
        manifest = build_release(staging)
        target = out / manifest["release"]
        target.parent.mkdir(exist_ok=True)
        (staging / manifest["release"]).rename(target)

    # Each writer has its own temporary file. Verified files become visible via one manifest swap.
    temp_manifest = out / f".manifest-{uuid4().hex}.tmp"
    try:
        write_json(temp_manifest, manifest)
        temp_manifest.replace(out / "manifest.json")
    finally:
        temp_manifest.unlink(missing_ok=True)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    export(args.out.resolve())
