"""Apply already verified metadata evidence in one transaction per batch."""
from hashlib import sha256
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COLUMNS = {"name", "address", "built", "latitude", "longitude"}


def validate(document, batch, year):
    if document.get("batch") != batch or not isinstance(document.get("complexes"), list):
        raise ValueError("Batch and metadata list must match the verified input file")
    ids = set()
    for record in document["complexes"]:
        cid, address, evidence, changes = (record[k] for k in ("complex_id", "expected_address", "evidence", "changes"))
        if not isinstance(cid, str) or not cid.strip() or cid in ids or not isinstance(address, str) or not address.strip():
            raise ValueError("Missing or duplicate complex ID/address")
        ids.add(cid)
        if not isinstance(evidence, str) or not evidence.strip() or not changes or not changes.keys() <= COLUMNS:
            raise ValueError("Each update requires evidence and allowed fields")
        for field in ("name", "address"):
            if field in changes and (not isinstance(changes[field], str) or not changes[field].strip()):
                raise ValueError("Invalid name or address")
        if "address" in changes and changes["address"].split()[1:2] != address.split()[1:2]:
            raise ValueError("A complex ID cannot change district")
        if "built" in changes and (type(changes["built"]) is not int or not 1900 <= changes["built"] <= year + 2):
            raise ValueError("Invalid construction year")
        if {"latitude", "longitude"} & changes.keys():
            lat, lng = changes.get("latitude"), changes.get("longitude")
            if not (type(lat) in (int, float) and type(lng) in (int, float) and 33 <= lat <= 39 and 124 <= lng <= 132):
                raise ValueError("Coordinates must be a verified numeric pair")


def apply_file(path, year):
    import psycopg
    from psycopg.types.json import Jsonb

    batch = path.stem
    payload = path.read_bytes()
    checksum = sha256(payload).hexdigest()
    document = json.loads(payload.decode("utf-8-sig"))
    validate(document, batch, year)
    with psycopg.connect(connect_timeout=8) as db:
        db.execute("SELECT pg_advisory_xact_lock(1121506)")
        db.execute((ROOT / "db/metadata_refresh.sql").read_text(encoding="utf-8"))
        prior = db.execute("SELECT checksum FROM complex_metadata_runs WHERE batch=%s", (batch,)).fetchone()
        if prior:
            if prior[0] != checksum:
                raise ValueError(f"Verified batch changed after application: {batch}")
            return False
        db.execute("INSERT INTO complex_metadata_runs(batch,checksum) VALUES (%s,%s)", (batch, checksum))
        for record in document["complexes"]:
            cid, address, evidence, changes = (record[k] for k in ("complex_id", "expected_address", "evidence", "changes"))
            # Field names have been checked against the fixed allowlist.
            assignments = ", ".join(f"{key}=%s" for key in changes)
            updated = db.execute(f"UPDATE complexes SET {assignments} WHERE complex_id=%s AND address=%s",
                                 (*changes.values(), cid, address))
            if updated.rowcount != 1:
                raise ValueError(f"Address/complex mismatch in {batch}: {cid}")
            db.execute("INSERT INTO complex_metadata_evidence VALUES (%s,%s,%s,%s)",
                       (batch, cid, evidence, Jsonb(changes)))
    return True


def apply_metadata(inbox: Path, today):
    # A new-complex verification batch may arrive independently of the monthly batch.
    month = today.strftime("%Y-%m")
    monthly = sorted(path for path in inbox.glob("*.json")
                     if re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", path.stem) and path.stem <= month)
    if not any(path.stem == month for path in monthly):
        print(f"Monthly verified metadata pending: {month}", flush=True)
    return sum(apply_file(path, today.year) for path in monthly + sorted(inbox.glob("new-*.json")))
