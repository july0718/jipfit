"""Non-mutating HTTP response-size and latency baseline; run against local web.

python benchmark_web.py --origin http://127.0.0.1:5173 --complex-id '...'
"""
import argparse
import json
import statistics
import time
from urllib.parse import urlencode
from urllib.request import urlopen


def measure(url, repeat):
    timings, lengths = [], []
    for _ in range(repeat):
        start = time.perf_counter()
        with urlopen(url, timeout=120) as response:
            body = response.read()
        timings.append(round((time.perf_counter() - start) * 1000, 1))
        lengths.append(len(body))
    return {"p50_ms": statistics.median(timings),
            "max_ms": max(timings), "response_bytes": max(lengths)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--origin", default="http://127.0.0.1:5173")
    p.add_argument("--complex-id")
    p.add_argument("--area-group", type=int, default=60)
    p.add_argument("--repeat", type=int, default=3)
    a = p.parse_args()
    origin = a.origin.rstrip("/")
    routes = ["/api/complexes?include_sort=true", "/data/manifest.json"]
    if a.complex_id:
        routes.extend([
            "/api/trades?" + urlencode({"complex_id": a.complex_id, "area_group": a.area_group}),
            "/api/predict?" + urlencode({"complex_id": a.complex_id, "area_group": a.area_group}),
        ])
    result = {}
    for route in routes:
        try:
            result[route.split("?")[0]] = measure(origin + route, a.repeat)
        except Exception as error:
            result[route.split("?")[0]] = {"error": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
