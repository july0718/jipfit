"""Compare public Pages responses from Windows with the verified local web service."""
import json
import subprocess
from urllib.parse import urlencode

LOCAL = "http://127.0.0.1:5173"
PUBLIC = "https://jipfit-48v.pages.dev"


def read(base, path, raw=False):
    data = subprocess.check_output(["curl.exe", "--fail", "--silent", "--show-error",
                                    "--max-time", "30", base + path])
    return data if raw else json.loads(data)


def compare(path, ignore=()):
    local = read(LOCAL, path)
    public = read(PUBLIC, path)
    for field in ignore:
        local.pop(field)
        public.pop(field)
    if public != local:
        raise ValueError(f"Public and local data differ: {path.split('?')[0]}")
    return public


def verify():
    for path in ("/", "/app.js", "/config.js", "/style.css"):
        content = read(PUBLIC, path, raw=True).strip()
        valid = b'id="root"' in content if path == "/" else content and not content.startswith(b"<")
        if not valid:
            raise ValueError(f"Unexpected page or asset: {path}")
    print("PASS public page and assets")
    if compare("/api/account")["username"] is not None:
        raise ValueError("Expected guest account")
    homes = compare("/api/complexes?include_sort=true")
    print("PASS guest account and", len(homes), "area groups")
    for ready in (True, False):
        item = next(r for r in homes if r["predictable"] == ready)
        key = {"complex_id": item["complex_id"], "area_group": item["area_group"]}
        query = urlencode(key)
        rows = compare("/api/trades?" + query)
        if not rows:
            raise ValueError("Expected trade history")
        if ready:
            forecast = compare("/api/predict?" + query, ignore=("calculated_at",))
            if [f["months"] for f in forecast["forecasts"]] != [3, 6, 12]:
                raise ValueError("Unexpected forecast horizons")
        detail = compare("/api/complex?" + urlencode({"complex_id": key["complex_id"]}))
        if not detail["last_sync_at"]:
            raise ValueError("No successful sync timestamp")
        print("PASS", item["name"], key["area_group"], len(rows), "trades; sync:", detail["last_sync_at"])


if __name__ == "__main__":
    verify()
