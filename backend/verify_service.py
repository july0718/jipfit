"""Read-only integration checks, run inside the API container on the server PC."""
import argparse
import json
from datetime import datetime
from math import isclose
from urllib.parse import urlencode
from urllib.request import urlopen

from database import connect
from inference import forecast, load_model


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(base):
    def fetch(path, params=None, raw=False):
        url = base.rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        with urlopen(url, timeout=30) as response:
            return response.read() if raw else json.load(response)

    require(b'id="root"' in fetch("/", raw=True), "React page missing")
    for asset in ("/app.js", "/config.js", "/style.css"):
        content = fetch(asset, raw=True)
        require(content.strip() and not content.lstrip().startswith(b"<"), f"Missing asset {asset}")
    require(fetch("/api/account")["username"] is None, "Expected guest account")
    homes = fetch("/api/complexes", {"include_sort": "true"})
    with connect() as db:
        total = db.execute("SELECT count(*) AS n FROM area_groups").fetchone()["n"]
        require(len({(r["complex_id"], r["area_group"]) for r in homes}) == len(homes) == total, "List mismatch")
        require(any(r["latitude"] is not None and r["longitude"] is not None for r in homes), "Map coordinates missing")
        model = load_model()
        for predictable in (True, False):
            item = next(r for r in homes if r["predictable"] == predictable)
            key = {k: item[k] for k in ("complex_id", "area_group")}
            rows = fetch("/api/trades", key)
            stored = db.execute("""
                SELECT contract_date, area, floor, price_manwon, is_cancelled FROM trades
                WHERE complex_id=%s AND area_group=%s ORDER BY contract_date DESC,trade_id DESC
            """, tuple(key.values())).fetchall()
            require(rows and len(rows) == len(stored), "Trade count mismatch")
            for row, ref in zip(rows, stored, strict=True):
                require(row["contract_date"] == ref["contract_date"].isoformat()
                        and row["floor"] == ref["floor"] and row["is_cancelled"] == ref["is_cancelled"]
                        and float(row["area"]) == float(ref["area"])
                        and float(row["price_manwon"]) == float(ref["price_manwon"]), "Trade mismatch")
            if predictable:
                result = fetch("/api/predict", key)
                ref = db.execute("""
                    SELECT m.*,a.representative_area FROM model_inputs m JOIN area_groups a USING(complex_id,area_group)
                    WHERE m.complex_id=%s AND m.area_group=%s AND m.month=(SELECT max(month) FROM model_inputs)
                """, tuple(key.values())).fetchone()
                expected = forecast(model, ref)
                require(result["as_of"] == expected["as_of"], "Forecast month mismatch")
                for a,b in zip(result["forecasts"], expected["forecasts"], strict=True):
                    require(a["months"] == b["months"] and all(isclose(a[k],b[k],abs_tol=1e-8)
                            for k in ("price_manwon","return_percent")), "Forecast mismatch")
            print("PASS", item["name"], item["area_group"], len(rows), "trades; predictable =", predictable)
        sync = db.execute("SELECT max(finished_at) AS ts FROM refresh_runs").fetchone()["ts"]
        detail = fetch("/api/complex", {"complex_id": item["complex_id"]})
        require(sync is not None and detail["last_sync_at"] and datetime.fromisoformat(detail["last_sync_at"]) == sync,
                "Latest successful sync timestamp mismatch")
        print("PASS", total, "groups; latest successful refresh:", sync.isoformat())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?", default="https://jipfit-48v.pages.dev")
    verify(parser.parse_args().base)
