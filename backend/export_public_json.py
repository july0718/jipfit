"""Build versioned, public read-only JSON snapshots from PostgreSQL.

Usage (DB must be available):
  python export_public_json.py --out /export
The manifest is published last. No trade rows are modified or deleted.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from math import exp, expm1, isfinite
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from fastapi.encoders import jsonable_encoder

from districts import DISTRICTS
from inference import HORIZONS, load_model, predict_growth


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable_encoder(payload), ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False), encoding="utf-8")


def trade_filename(complex_id):
    return sha256(complex_id.encode("utf-8")).hexdigest() + ".json"


def load_home_rows(db):
    # Same public response fields as GET /api/complexes?include_sort=true.
    columns = {r["column_name"] for r in db.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'complexes'
    """)}
    status_column = next((name for name in ("existence_status", "survival_status") if name in columns), None)
    # Null means unknown; never infer demolition from absent coordinates.
    status_expr = f'c."{status_column}"' if status_column else "NULL::text"
    homes = db.execute(f"""
        SELECT c.complex_id,c.name,c.address,c.built,c.latitude,c.longitude,
               {status_expr} AS existence_status,
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
    units = {(r["complex_id"], r["area_group"]): exp(float(r["log_current"]))
             for r in inputs}
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
        reference = units.get(key)
        marker_price = (reference * float(home["representative_area"])
                        if reference is not None else prices.get(key))
        if marker_price is not None and not isfinite(float(marker_price)):
            marker_price = None
        home["marker_price_manwon"] = marker_price
        home["trade_count"] = stats.get(cid, {}).get("trade_count", 0)
        home["last_trade"] = stats.get(cid, {}).get("last_trade")
        home["sort_returns"] = returns.get(cid, {})
    return homes


def export(out):
    generated = datetime.now(timezone.utc)
    release = "releases/" + generated.strftime("%Y%m%dT%H%M%S%fZ")
    target = out / release
    target.mkdir(parents=True, exist_ok=False)
    districts = [{"code": code, "name": name} for code, name in DISTRICTS.items()]
    totals = {"area_groups": 0, "complexes": 0, "transactions": 0}
    with psycopg.connect(row_factory=dict_row, connect_timeout=10) as db:
        # All exported files represent one consistent committed DB snapshot.
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        homes = load_home_rows(db)
        group_rows = defaultdict(list)
        recent_rows = defaultdict(list)
        all_ids = set()
        by_name = {entry["name"]: entry["code"] for entry in districts}
        for home in homes:
            district = home["address"].split(" ")[1]
            code = by_name.get(district)
            if code is None:
                raise ValueError(f"Unknown district in complexes: {district}")
            all_ids.add(home["complex_id"])
            group_rows[code].append({key: value for key, value in home.items()
                                     if key not in {"marker_price_manwon", "trade_count", "last_trade", "sort_returns"}})
            recent_rows[code].append({key: home[key] for key in
                                     ("complex_id", "area_group", "marker_price_manwon",
                                      "trade_count", "last_trade", "sort_returns")})
        totals["area_groups"] = len(homes)
        totals["complexes"] = len(all_ids)
        for entry in districts:
            code = entry["code"]
            write_json(target / "complexes" / f"{code}.json", group_rows[code])
            write_json(target / "latest-trades" / f"{code}.json", recent_rows[code])

        expected = db.execute("SELECT count(*) AS total FROM trades").fetchone()["total"]
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
                if cid != current_id:
                    flush()
                    current_id = cid
                    area_groups = {}
                area_groups.setdefault(str(row["area_group"]), []).append({
                    key: row[key] for key in
                    ("contract_date", "area", "floor", "price_manwon", "is_cancelled")
                })
                totals["transactions"] += 1
        flush()
        if totals["transactions"] != expected:
            raise RuntimeError(f"Transaction mismatch: JSON={totals['transactions']}, DB={expected}")
        if not seen_ids.issubset(all_ids):
            raise RuntimeError("Orphan trade complex_id; export not published")
        for cid in all_ids - seen_ids:
            code = by_name[cid.split(" ")[1]]
            write_json(target / "transactions" / code / trade_filename(cid),
                       {"complex_id": cid, "area_groups": {}})
        for entry in districts:
            code = entry["code"]
            if len(group_rows[code]) != len(recent_rows[code]):
                raise RuntimeError(f"District group mismatch: {code}")

    manifest = {
        "schema_version": 1, "generated_at": generated.isoformat(), "release": release,
        "districts": districts, "totals": totals,
        "notes": "Public read-only snapshot. User data and live HGB forecasts are not exported."
    }
    # Publish manifest only after every file is written and row counts match.
    temp_manifest = out / "manifest.json.tmp"
    write_json(temp_manifest, manifest)
    temp_manifest.replace(out / "manifest.json")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    export(args.out.resolve())
