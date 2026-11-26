"""Apply already verified metadata evidence in one transaction per batch."""
from datetime import date
from hashlib import sha256
import json
import re
from pathlib import Path

from districts import DISTRICTS

ROOT = Path(__file__).resolve().parent
COLUMNS = {"name", "address", "built", "latitude", "longitude"}
DISTRICT_CODES_BY_NAME = {name: code for code, name in DISTRICTS.items()}


class MetadataApprovalReview(ValueError):
    """The approval needs correction or new evidence before it can be applied."""


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
        if "expected_name" in record and record["expected_name"] is not None:
            if not isinstance(record["expected_name"], str) or not record["expected_name"].strip():
                raise ValueError("Invalid expected_name")
        if "expected_built" in record and record["expected_built"] is not None:
            if type(record["expected_built"]) is not int or not 1900 <= record["expected_built"] <= year + 2:
                raise ValueError("Invalid expected_built")
        if "candidate_id" in record and record["candidate_id"] is not None:
            if not isinstance(record["candidate_id"], str) or not record["candidate_id"].strip():
                raise ValueError("Invalid candidate_id")
        if "candidate_ids" in record and record["candidate_ids"] is not None:
            if not isinstance(record["candidate_ids"], list) or not all(
                isinstance(x, str) and x.strip() for x in record["candidate_ids"]
            ):
                raise ValueError("Invalid candidate_ids")
        if "field_evidence" in record and record["field_evidence"] is not None:
            fe = record["field_evidence"]
            if not isinstance(fe, dict) or not fe.keys() <= changes.keys() or not all(
                isinstance(v, str) and v.strip() for v in fe.values()
            ):
                raise ValueError("Invalid field_evidence")
        for field in ("name", "address"):
            if field in changes and (not isinstance(changes[field], str) or not changes[field].strip()):
                raise ValueError("Invalid name or address")
        if "address" in changes:
            new_address = changes["address"]
            if " ".join(new_address.split()) != new_address:
                raise ValueError("Address must use normalized single spaces")
            parts = new_address.split()
            if (len(parts) < 3 or parts[0] not in ("서울특별시", "서울시")
                    or parts[1:2] != address.split()[1:2] or parts[1:2] != cid.split()[1:2]):
                raise ValueError("A complex ID cannot change district")
        if "built" in changes and (type(changes["built"]) is not int or not 1900 <= changes["built"] <= year + 2):
            raise ValueError("Invalid construction year")
        if {"latitude", "longitude"} & changes.keys():
            lat, lng = changes.get("latitude"), changes.get("longitude")
            if not (type(lat) in (int, float) and type(lng) in (int, float) and 33 <= lat <= 39 and 124 <= lng <= 132):
                raise ValueError("Coordinates must be a verified numeric pair")


def update_complex(db, cid, address, changes, *, expected_name=None, expected_built=None):
    if not changes or not changes.keys() <= COLUMNS:
        raise ValueError("Unknown metadata fields")
    assignments = ", ".join(f"{key}=%s" for key in changes)
    if "address" in changes and changes["address"] != address and "latitude" not in changes:
        assignments += ", latitude=NULL, longitude=NULL"
    query = f"UPDATE complexes SET {assignments} WHERE complex_id=%s AND address=%s"
    values = (*changes.values(), cid, address)
    if expected_name is not None:
        query += " AND name=%s"
        values += (expected_name,)
    if expected_built is not None:
        query += " AND built=%s"
        values += (expected_built,)
    if db.execute(query, values).rowcount != 1:
        return False
    if "latitude" in changes and "longitude" in changes:
        db.execute("DELETE FROM complex_geocode_pending WHERE complex_id=%s", (cid,))
        if "address" in changes and changes["address"] != address:
            db.execute("DELETE FROM complex_geocode_attempts WHERE complex_id=%s", (cid,))
    elif "address" in changes and changes["address"] != address:
        db.execute("INSERT INTO complex_geocode_pending VALUES (%s) ON CONFLICT DO NOTHING", (cid,))
        db.execute("DELETE FROM complex_geocode_attempts WHERE complex_id=%s", (cid,))
    return True


def _hold_stale_approval(record, batch, today):
    """Record a stale approval hold on matching pending candidates in an independent transaction."""
    import psycopg

    cid = record["complex_id"]
    cand_ids = list(record.get("candidate_ids") or ())
    if record.get("candidate_id"):
        cand_ids.append(record["candidate_id"])
    try:
        with psycopg.connect(connect_timeout=8) as hold_db:
            if cand_ids:
                hold_db.execute("""
                    UPDATE complex_metadata_candidates
                    SET status = 'held', reason = 'STALE_APPROVAL', last_observed_on = %s
                    WHERE candidate_id = ANY(%s::text[]) AND complex_id = %s AND status = 'pending'
                """, (today, cand_ids, cid))
            else:
                hold_db.execute("""
                    UPDATE complex_metadata_candidates
                    SET status = 'held', reason = 'STALE_APPROVAL', last_observed_on = %s
                    WHERE complex_id = %s AND status = 'pending'
                      AND field = ANY(%s::text[])
                """, (today, cid, list(record["changes"].keys())))
    except Exception:
        pass


def apply_verified_complex_records(db, batch, checksum, records, *, today=None):
    """Apply a validated list of verified metadata records inside the caller's transaction."""
    from psycopg.types.json import Jsonb

    effective_day = today or date.today()
    prior = db.execute("SELECT checksum FROM complex_metadata_runs WHERE batch=%s", (batch,)).fetchone()
    if prior:
        if prior[0] != checksum:
            raise ValueError(f"Verified batch changed after application: {batch}")
        return False
    db.execute("INSERT INTO complex_metadata_runs(batch,checksum) VALUES (%s,%s)", (batch, checksum))

    locked_districts = set()
    for record in records:
        cid = record["complex_id"]
        parts = cid.split(" ")
        if len(parts) >= 2 and parts[1] in DISTRICT_CODES_BY_NAME:
            d_code = DISTRICT_CODES_BY_NAME[parts[1]]
            if d_code not in locked_districts:
                lock_id = 1121505 if d_code == "11215" else int(d_code)
                db.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
                locked_districts.add(d_code)

    for record in records:
        cid, address, evidence, changes = (
            record[k] for k in ("complex_id", "expected_address", "evidence", "changes")
        )
        expected_name = record.get("expected_name")
        expected_built = record.get("expected_built")
        cand_id = record.get("candidate_id")
        cand_ids = sorted(set(record.get("candidate_ids") or ()) | ({cand_id} if cand_id else set()))

        cur_row = db.execute("""
            SELECT name, address, built, latitude, longitude
            FROM complexes
            WHERE complex_id=%s
            FOR UPDATE
        """, (cid,)).fetchone()
        before_values = None
        built_actually_changed = "built" in changes
        if cur_row is not None and len(cur_row) == 5:
            cur_map = {
                "name": cur_row[0],
                "address": cur_row[1],
                "built": cur_row[2],
                "latitude": cur_row[3],
                "longitude": cur_row[4],
            }
            before_values = {k: cur_map[k] for k in changes if k in cur_map}
            if "built" in changes:
                built_actually_changed = cur_row[2] != changes["built"]

        if cand_ids:
            if expected_name is None or expected_built is None:
                raise MetadataApprovalReview(f"Missing expected name or build year in {batch}: {cid}")
            candidates = db.execute("""
                SELECT candidate_id, field, expected_value, proposed_value
                FROM complex_metadata_candidates
                WHERE candidate_id = ANY(%s::text[]) AND complex_id = %s AND status = 'pending'
                ORDER BY candidate_id FOR UPDATE
            """, (cand_ids, cid)).fetchall()
            if len(candidates) != len(cand_ids) or before_values is None:
                raise MetadataApprovalReview(f"Invalid metadata candidates in {batch}: {cid}")
            for _, field, old_value, new_value in candidates:
                fields = field.split("+")
                if any(key not in before_values for key in fields) or (
                    "|".join(str(before_values[key]) for key in fields) != old_value
                    or "|".join(str(changes[key]) for key in fields) != new_value
                ):
                    raise MetadataApprovalReview(f"Stale or mismatched metadata candidate in {batch}: {cid}")

        if not update_complex(
            db, cid, address, changes,
            expected_name=expected_name, expected_built=expected_built,
        ):
            raise MetadataApprovalReview(f"Address/complex mismatch in {batch}: {cid}")

        db.execute(
            "INSERT INTO complex_metadata_evidence(batch,complex_id,evidence,changes) VALUES (%s,%s,%s,%s)",
            (batch, cid, evidence, Jsonb(changes)),
        )
        if cand_id is not None or before_values is not None:
            db.execute("""
                UPDATE complex_metadata_evidence
                SET candidate_id = COALESCE(%s, candidate_id),
                    before_values = COALESCE(%s, before_values)
                WHERE batch = %s AND complex_id = %s
            """, (cand_id, Jsonb(before_values) if before_values is not None else None, batch, cid))

        if cand_ids or record.get("field_evidence"):
            db.execute("""
                UPDATE complex_metadata_evidence SET candidate_ids=%s, field_evidence=%s
                WHERE batch=%s AND complex_id=%s
            """, (cand_ids, Jsonb(record.get("field_evidence") or {}), batch, cid))
        if cand_ids:
            db.execute("""
                UPDATE complex_metadata_candidates
                SET status = 'approved', evidence = %s, last_observed_on = %s
                WHERE candidate_id = ANY(%s::text[])
            """, (evidence, effective_day, cand_ids))
        for field, new_val in changes.items():
            if field in ("name", "address", "built"):
                db.execute("""
                    UPDATE complex_metadata_candidates
                    SET status = 'approved', evidence = %s, last_observed_on = %s
                    WHERE complex_id = %s AND field = %s AND proposed_value = %s AND status = 'pending'
                      AND expected_value = %s
                """, (evidence, effective_day, cid, field, str(new_val),
                      str(before_values[field]) if before_values is not None else None))

        if built_actually_changed:
            parts = cid.split(" ")
            d_code = DISTRICT_CODES_BY_NAME.get(parts[1]) if len(parts) >= 2 else None
            if d_code:
                db.execute("""
                    INSERT INTO refresh_publish_pending(district, day, added, removed)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (district, day) DO NOTHING
                """, (d_code, effective_day, 0, 0))
    return True


def apply_file(path, year, *, today=None):
    import psycopg

    batch = path.stem
    payload = path.read_bytes()
    checksum = sha256(payload).hexdigest()
    document = json.loads(payload.decode("utf-8-sig"))
    validate(document, batch, year)
    effective_day = today or date.today()
    try:
        with psycopg.connect(connect_timeout=8) as db:
            db.execute("SELECT pg_advisory_xact_lock(1121506)")
            db.execute((ROOT / "db/metadata_refresh.sql").read_text(encoding="utf-8"))
        with psycopg.connect(connect_timeout=8) as db:
            db.execute("SELECT pg_advisory_xact_lock(1121506)")
            return apply_verified_complex_records(
                db, batch, checksum, document["complexes"], today=effective_day
            )
    except ValueError as exc:
        if str(exc).startswith(f"Address/complex mismatch in {batch}: "):
            failed_cid = str(exc).split(": ", 1)[1]
            for rec in document["complexes"]:
                if rec.get("complex_id") == failed_cid:
                    _hold_stale_approval(rec, batch, effective_day)
                    break
        raise


def apply_metadata(inbox: Path, today, *, daily_only=False):
    # A new-complex verification batch may arrive independently of the monthly batch.
    month = today.strftime("%Y-%m")
    monthly = [] if daily_only else sorted(
        path for path in inbox.glob("*.json")
        if re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", path.stem) and path.stem <= month
    )
    if not daily_only and not any(path.stem == month for path in monthly):
        print(f"Monthly verified metadata pending: {month}", flush=True)
    applied = 0
    failed = []
    for path in monthly + sorted(inbox.glob("new-*.json")):
        try:
            applied += apply_file(path, today.year, today=today)
        except MetadataApprovalReview as exc:
            print(f"Verified metadata awaiting review: {path.name} ({exc})", flush=True)
        except Exception as exc:
            failed.append(path.name)
            print(f"Verified metadata deferred: {path.name} ({type(exc).__name__})", flush=True)
    if failed:
        raise ValueError("Verified metadata files deferred: " + ", ".join(failed))
    return applied
