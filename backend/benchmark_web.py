"""Compare old API and snapshot startup/history requests against the same DB."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
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


def snapshot_homes(origin, pool):
    manifest, size = fetch(origin, "/data/manifest.json")
    if manifest["schema_version"] != 1 or len(manifest["districts"]) != 25:
        raise ValueError("Expected a Seoul snapshot")
    paths = [snapshot_path(manifest, f"{kind}/{d['code']}.json")
             for d in manifest["districts"] for kind in ("complexes", "latest-trades")]
    files = list(pool.map(lambda path: fetch(origin, path), paths))
    groups = sum(len(payload) for payload, _ in files[::2])
    if groups != manifest["totals"]["area_groups"]:
        raise ValueError("Unexpected snapshot group count")
    return groups, size + sum(length for _, length in files), 1 + len(paths)


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
    code = next(entry["code"] for entry in manifest["districts"] if entry["name"] == district)
    digest = sha256(args.complex_id.encode("utf-8")).hexdigest()
    trade_path = snapshot_path(manifest, f"transactions/{code}/{digest}.json")

    def snapshot_trades():
        payload, size, count = single(candidate, trade_path)
        if payload["complex_id"] != args.complex_id:
            raise ValueError("Unexpected trade complex ID")
        return payload["area_groups"].get(str(args.area_group), []), size, count

    def baseline_homes():
        homes, size, count = single(baseline, "/api/complexes?include_sort=true")
        return len(homes), size, count

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        pairs = {
            "cold_homes": (baseline_homes, lambda: snapshot_homes(candidate, pool)),
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

