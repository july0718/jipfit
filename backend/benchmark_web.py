"""Compare old API and snapshot startup/history requests against the same DB."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import re
import json
import statistics
import time
from urllib.parse import urlencode
from urllib.request import urlopen


def fetch(origin, path):
    with urlopen(origin.rstrip("/") + path, timeout=120) as response:
        body = response.read()
        if response.headers.get_content_type() != "application/json":
            raise ValueError(f"Expected JSON at {path}")
    return json.loads(body), len(body)


def snapshot_path(manifest, path):
    return f"/data/{manifest['release']}/{path}"


def object_path(digest):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid snapshot object digest")
    return f"/data/objects/{digest}.json"


def snapshot_homes(origin, pool, district_name):
    manifest, size = fetch(origin, "/data/manifest.json")
    if manifest["schema_version"] not in (1, 2) or len(manifest["districts"]) != 25:
        raise ValueError("Expected a Seoul snapshot")
    district = next(d for d in manifest["districts"] if d["name"] == district_name)
    if manifest["schema_version"] == 2:
        paths = [object_path(district[field]) for field in ("complexes", "latest")]
    else:
        paths = [snapshot_path(manifest, f"{kind}/{district['code']}.json")
                 for kind in ("complexes", "latest-trades")]
    (homes, homes_size), (prices, prices_size) = pool.map(lambda path: fetch(origin, path), paths)
    keys = {(row["complex_id"], row["area_group"]) for row in homes}
    if len(keys) != len(homes) or keys != {(row["complex_id"], row["area_group"]) for row in prices}:
        raise ValueError("Snapshot area groups differ from latest prices")
    return keys, size + homes_size + prices_size, 3


def measure(request, repeat):
    timings = []
    for _ in range(repeat):
        start = time.perf_counter()
        result, size, count = request()
        timings.append((time.perf_counter() - start) * 1000)
    return {"p50_ms": round(statistics.median(timings), 2),
            "max_ms": round(max(timings), 2), "body_bytes": size, "requests": count}, result


def single(origin, path):
    result, size = fetch(origin, path)
    return result, size, 1


def compare(args):
    baseline, candidate = args.baseline_origin, args.snapshot_origin
    params = urlencode({"complex_id": args.complex_id, "area_group": args.area_group})
    predict_path = "/api/predict?" + params
    manifest, _ = fetch(candidate, "/data/manifest.json")
    district = args.complex_id.split()[1]
    entry = next(entry for entry in manifest["districts"] if entry["name"] == district)
    digest = sha256(args.complex_id.encode("utf-8")).hexdigest()

    def snapshot_trades():
        index_size = 0
        if manifest["schema_version"] == 2:
            index, index_size = fetch(candidate, object_path(entry["history"]))
            trade_path = object_path(index[digest])
        else:
            trade_path = snapshot_path(manifest, f"transactions/{entry['code']}/{digest}.json")
        payload, size, count = single(candidate, trade_path)
        if payload["complex_id"] != args.complex_id:
            raise ValueError("Unexpected trade complex ID")
        return payload["area_groups"].get(str(args.area_group), []), size + index_size, count + bool(index_size)

    def baseline_homes():
        homes, size, count = single(baseline, "/api/complexes?include_sort=true")
        keys = {(row["complex_id"], row["area_group"]) for row in homes
                if row["complex_id"].split()[1] == district}
        return keys, size, count

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        pairs = {
            "cold_district_homes": (baseline_homes, lambda: snapshot_homes(candidate, pool, district)),
            "selected_trade_history": (lambda: single(baseline, "/api/trades?" + params), snapshot_trades),
            "live_prediction": (lambda: single(baseline, predict_path), lambda: single(candidate, predict_path)),
        }
        report = {}
        for label, (old, new) in pairs.items():
            # One warm-up on each origin before repeated full requests.
            old()
            new()
            left, old_result = measure(old, args.repeat)
            right, new_result = measure(new, args.repeat)
            if label == "live_prediction":
                for result in (old_result, new_result):
                    result.pop("calculated_at")
                    if [f["months"] for f in result["forecasts"]] != [3, 6, 12]:
                        raise ValueError("Missing live inference horizons")
            if old_result != new_result:
                raise ValueError(f"Baseline and candidate return different data: {label}")
            report[label] = {"baseline": left, "snapshot": right}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-origin", required=True)
    parser.add_argument("--snapshot-origin", required=True)
    parser.add_argument("--complex-id", required=True)
    parser.add_argument("--area-group", type=int, required=True)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--parallel", type=int, default=6)
    args = parser.parse_args()
    if min(args.repeat, args.parallel, args.area_group) < 1:
        parser.error("repeat, parallel and area-group must be positive")
    print(json.dumps(compare(args), ensure_ascii=False, indent=2))

