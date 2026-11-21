"""Validate all district and transaction files before publishing a manifest."""
import argparse
from collections import Counter
from hashlib import sha256
from itertools import groupby
import json
from pathlib import Path
import re

from districts import DISTRICTS

TRADE_FIELDS = {"contract_date", "area", "floor", "price_manwon", "is_cancelled"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def trade_filename(complex_id):
    return sha256(complex_id.encode("utf-8")).hexdigest() + ".json"


def trade_digest(rows):
    digest = sha256()
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.digest()


def verify_database(db, home_keys, digests):
    from fastapi.encoders import jsonable_encoder

    db_keys = {(row["complex_id"], row["area_group"])
               for row in db.execute("SELECT complex_id, area_group FROM area_groups")}
    require(db_keys == home_keys, "JSON area groups differ from PostgreSQL")
    with db.cursor(name="public_json_verify") as cursor:
        cursor.itersize = 5000
        cursor.execute("""
            SELECT complex_id, area_group, contract_date, area, floor, price_manwon, is_cancelled
            FROM trades ORDER BY complex_id, area_group, contract_date DESC, trade_id DESC
        """)
        for key, rows in groupby(cursor, lambda row: (row["complex_id"], row["area_group"])):
            expected = trade_digest(jsonable_encoder({field: row[field] for field in TRADE_FIELDS})
                                    for row in rows)
            require(digests.pop(key, None) == expected, f"JSON trades differ from PostgreSQL: {key}")
    require(not digests, "JSON contains trade groups missing from PostgreSQL")


def check(root, *, manifest=None, db=None):
    manifest = read(root / "manifest.json") if manifest is None else manifest
    require(manifest["schema_version"] == 1 and
            re.fullmatch(r"releases/[0-9]{8}T[0-9]{12}Z", manifest["release"]),
            "Unsupported or invalid release path")
    release = root / manifest["release"]
    require(release.resolve().is_relative_to(root.resolve()), "Invalid release location")
    districts = manifest["districts"]
    require(len(districts) == len(DISTRICTS) and
            {item["code"]: item["name"] for item in districts} == DISTRICTS,
            "Expected all 25 Seoul districts")

    totals = Counter(area_groups=0, complexes=0, transactions=0, cancelled_transactions=0)
    home_keys = set()
    digests = {}
    expected_files = set()
    for code, name in DISTRICTS.items():
        homes = read(release / "complexes" / f"{code}.json")
        recent = read(release / "latest-trades" / f"{code}.json")
        keys = {(home["complex_id"], home["area_group"]) for home in homes}
        recent_keys = {(row["complex_id"], row["area_group"]) for row in recent}
        require(keys and len(homes) == len(keys) == len(recent) and keys == recent_keys,
                f"{code}: empty district, duplicate keys, or differing list and price groups")
        require(not (keys & home_keys), f"{code}: groups repeated across districts")
        home_keys.update(keys)
        totals["area_groups"] += len(keys)
        areas = {}
        for cid, group in keys:
            require(cid.split()[1] == name, f"{cid}: incorrect district {code}")
            areas.setdefault(cid, set()).add(str(group))
        totals["complexes"] += len(areas)
        for cid, groups in areas.items():
            path = release / "transactions" / code / trade_filename(cid)
            expected_files.add(path)
            payload = read(path)
            require(payload["complex_id"] == cid, f"{cid}: incorrect transaction file")
            require(set(payload["area_groups"]) <= groups, f"{cid}: unknown trade group")
            for group, rows in payload["area_groups"].items():
                require(rows, f"{cid}: empty trade group {group}")
                previous_date = None
                for row in rows:
                    require(set(row) == TRADE_FIELDS and type(row["is_cancelled"]) is bool,
                            f"{cid}: invalid trade fields")
                    date = row["contract_date"]
                    require(previous_date is None or date <= previous_date,
                            f"{cid}: trade history is out of order")
                    previous_date = date
                    totals["cancelled_transactions"] += int(row["is_cancelled"])
                totals["transactions"] += len(rows)
                if db is not None:
                    digests[(cid, int(group))] = trade_digest(rows)

    require(set((release / "transactions").rglob("*.json")) == expected_files,
            "Unexpected transaction files")
    require(dict(totals) == manifest["totals"], f"File totals differ from manifest: {totals}")
    if db is not None:
        verify_database(db, home_keys, digests)
    return dict(totals)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    totals = check(parser.parse_args().root.resolve())
    print(json.dumps({"verified": True, **totals}, ensure_ascii=False))

