"""Verify a generated public JSON release without modifying PostgreSQL."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(root):
    manifest = read(root / "manifest.json")
    if manifest["schema_version"] != 1 or not manifest["release"].startswith("releases/"):
        raise ValueError("Unsupported or unsafe manifest")
    release = root / manifest["release"]
    if release.resolve().is_relative_to(root.resolve()) is False:
        raise ValueError("Invalid release path")
    district_counts = Counter()
    complex_ids = set()
    for district in manifest["districts"]:
        code = district["code"]
        homes = read(release / "complexes" / (code + ".json"))
        recent = read(release / "latest-trades" / (code + ".json"))
        assert len(homes) == len(recent), (code, "area-group count mismatch")
        keys = {(h["complex_id"], h["area_group"]) for h in homes}
        assert keys == {(h["complex_id"], h["area_group"]) for h in recent}
        district_counts["area_groups"] += len(homes)
        complex_ids.update(h["complex_id"] for h in homes)
    assert district_counts["area_groups"] == manifest["totals"]["area_groups"]
    assert len(complex_ids) == manifest["totals"]["complexes"]
    transactions = 0
    for cid in complex_ids:
        district = cid.split(" ")[1]
        code = next(entry["code"] for entry in manifest["districts"] if entry["name"] == district)
        digest = sha256(cid.encode("utf-8")).hexdigest() + ".json"
        payload = read(release / "transactions" / code / digest)
        assert payload["complex_id"] == cid
        for rows in payload["area_groups"].values():
            transactions += len(rows)
            for row in rows:
                assert set(row) == {"contract_date", "area", "floor", "price_manwon", "is_cancelled"}
    assert transactions == manifest["totals"]["transactions"], (transactions, manifest["totals"])
    print(json.dumps({"verified": True, **manifest["totals"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    check(parser.parse_args().root.resolve())
