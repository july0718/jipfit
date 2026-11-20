"""Read-only integration checks, run inside the API container on the server PC."""
import argparse
import json
from datetime import datetime
from math import isclose
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from database import connect
from districts import DISTRICTS, GWANGJIN
from inference import forecast, load_model


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(base, district=GWANGJIN):
    def fetch(path, params=None, raw=False):
        url = base.rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        try:
            with urlopen(url, timeout=30) as response:
                return response.read() if raw else json.load(response)
        except HTTPError as exc:
            with exc:
                headers = " ".join(f"{k}={exc.headers[k]}" for k in
                                   ("Server", "Content-Type", "cf-mitigated", "cf-ray") if k in exc.headers)
                raise ValueError(f"{path} HTTP {exc.code} {headers}") from None
        except URLError as exc:
            raise ValueError(f"{path} connection failed: {exc.reason}") from None

    failures = []
    for path in ("/", "/app.js", "/config.js", "/style.css", "/api/account"):
        try:
            if path == "/api/account":
                require(fetch(path)["username"] is None, "Expected guest account")
            else:
                content = fetch(path, raw=True)
                if path == "/":
                    require(b'id="root"' in content, "React page missing")
                else:
                    require(content.strip() and not content.lstrip().startswith(b"<"), "Missing asset")
            print("PASS", path, flush=True)
        except (ValueError, KeyError, OSError) as exc:
            failures.append(path)
            print("FAIL", path, exc, flush=True)
    homes = fetch("/api/complexes", {"include_sort": "true"})
    with connect() as db:
        total = db.execute("SELECT count(*) AS n FROM area_groups").fetchone()["n"]
        require(len({(r["complex_id"], r["area_group"]) for r in homes}) == len(homes) == total, "List mismatch")
        homes = [r for r in homes if r["address"].split()[1] == DISTRICTS[district]]
        require(homes, f"No groups for district {district}")
        require(any(r["latitude"] is not None and r["longitude"] is not None for r in homes), "Map coordinates missing")
        model = load_model()
        for predictable in (True, False):
            eligible = [r for r in homes if r["predictable"] == predictable]
            require(eligible, f"No test group with predictable={predictable}")
            item = next((r for r in eligible if r["latitude"] is not None), eligible[0])
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
                    WHERE m.complex_id=%s AND m.area_group=%s AND m.month=(SELECT month FROM district_input_months WHERE district=split_part(m.complex_id,' ',2))
                """, tuple(key.values())).fetchone()
                expected = forecast(model, ref)
                require(result["as_of"] == expected["as_of"], "Forecast month mismatch")
                for a,b in zip(result["forecasts"], expected["forecasts"], strict=True):
                    require(a["months"] == b["months"] and all(isclose(a[k],b[k],abs_tol=1e-8)
                            for k in ("price_manwon","return_percent")), "Forecast mismatch")
            print("PASS", item["name"], item["area_group"], len(rows), "trades; predictable =", predictable)
        sync = db.execute("SELECT max(finished_at) AS ts FROM refresh_runs").fetchone()["ts"]
        if district != GWANGJIN:
            sync = db.execute("SELECT max(finished_at) AS ts FROM district_refresh_runs WHERE district=%s", (district,)).fetchone()["ts"]
        detail = fetch("/api/complex", {"complex_id": item["complex_id"]})
        require(sync is not None and detail["last_sync_at"] and datetime.fromisoformat(detail["last_sync_at"]) == sync,
                "Latest successful sync timestamp mismatch")
        print("PASS", district, len(homes), "groups; latest successful refresh:", sync.isoformat())

    require(not failures, "Failed routes: " + ", ".join(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?", default="https://jipfit-48v.pages.dev")
    parser.add_argument("--district", choices=DISTRICTS, default=GWANGJIN)
    args = parser.parse_args()
    try:
        verify(args.base, args.district)
    except ValueError as exc:
        parser.exit(1, f"FAIL {exc}\n")
