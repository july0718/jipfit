"""PostgreSQL reads and the run and fingerprint tables of the monthly check.

The reads replace jipfit_lifecycle@d86dcdf:monthly.py load_dump: COPY TO STDOUT prints every value as
pg_dump did, so the judgement gets the same strings and None for NULL. The tables are new in Jipfit
(plan-lifecycle-auto.md 4-4) and the fingerprint documents are those of 4-1.
"""
from datetime import date
import json

import psycopg
from psycopg.types.json import Jsonb

LOCK_KEY = 2011280100  # Session advisory lock held while one monthly check runs.
FINGERPRINT_PARTS = ("closed", "demolition", "current")
READS = {
    "complexes": (
        ("complex_id", "name", "address", "built"),
        "COPY (SELECT complex_id, name, address, built FROM complexes ORDER BY complex_id) TO STDOUT"),
    "trades": (
        ("complex_id", "contract_date", "is_cancelled"),
        "COPY (SELECT complex_id, contract_date, is_cancelled FROM trades) TO STDOUT"),
    "complex_lifecycle_state": (
        ("complex_id", "result_key", "existence", "as_of", "evidence", "effective_date", "effective_precision",
         "pending_hold_code", "pending_as_of"),
        "COPY (SELECT complex_id, result_key, existence, as_of, evidence, effective_date, effective_precision, "
        "pending_hold_code, pending_as_of FROM complex_lifecycle_state ORDER BY complex_id) TO STDOUT"),
    "complex_lifecycle_results": (
        ("complex_id", "existence", "successor_id", "applied_status"),
        "COPY (SELECT complex_id, existence, successor_id, applied_status FROM complex_lifecycle_results "
        "ORDER BY complex_id) TO STDOUT"),
}


def connect():
    # Autocommit keeps each transaction() block a real transaction instead of a savepoint.
    return psycopg.connect(autocommit=True, connect_timeout=8)


def load_inputs(db):
    """(tables, fingerprint document) as of one moment, without writing anything."""
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        db.execute("SET LOCAL datestyle = 'ISO, YMD'")
        tables, shared = {}, {}
        for table, (columns, query) in READS.items():
            with db.cursor() as cursor, cursor.copy(query) as copy:
                # Over a million trade rows repeat the same IDs and days, so equal strings are kept once.
                tables[table] = [{name: shared.setdefault(value, value) for name, value in zip(columns, row, strict=True)}
                                 for row in copy.rows()]
        return tables, load_fingerprints(db)


# Runs: one row per month (plan-lifecycle-auto.md 5-2).

def try_lock(db):
    return db.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]


def read_run(db, month):
    row = db.execute("""
        SELECT status, attempts, batch FROM complex_lifecycle_runs WHERE month = %s
    """, (month,)).fetchone()
    return None if row is None else dict(zip(("status", "attempts", "batch"), row))


def start_attempt(db, month):
    return db.execute("""
        INSERT INTO complex_lifecycle_runs(month, status, attempts) VALUES (%s, 'collecting', 1)
        ON CONFLICT (month) DO UPDATE
        SET attempts = complex_lifecycle_runs.attempts + 1, last_error = NULL, updated_at = clock_timestamp()
        RETURNING attempts
    """, (month,)).fetchone()[0]


def note_run(db, month, remaining, error):
    db.execute("""
        UPDATE complex_lifecycle_runs
        SET remaining_calls = COALESCE(%s, remaining_calls), last_error = %s, updated_at = clock_timestamp()
        WHERE month = %s AND status <> 'applied'
    """, (remaining, error, month))


def name_batch(db, month, batch, as_of):
    db.execute("""
        UPDATE complex_lifecycle_runs SET batch = %s, as_of = %s, updated_at = clock_timestamp()
        WHERE month = %s
    """, (batch, as_of, month))


def mark_judged(db, month, remaining=None, summary=None):
    # The daily job may already have applied the bundle, so an applied run stays applied.
    db.execute("""
        UPDATE complex_lifecycle_runs
        SET status = 'judged', remaining_calls = COALESCE(%s, remaining_calls),
            summary = COALESCE(%s::jsonb, summary), last_error = NULL, updated_at = clock_timestamp()
        WHERE month = %s AND status = 'collecting'
    """, (remaining, None if summary is None else Jsonb(summary), month))


def mark_applied(db, batch):
    db.execute("""
        UPDATE complex_lifecycle_runs SET status = 'applied', last_error = NULL, updated_at = clock_timestamp()
        WHERE batch = %s AND status <> 'applied'
    """, (batch,))


# Fingerprints

def parse_fingerprints(body):
    """The fingerprint document of a bundle, else ValueError."""
    try:
        document = json.loads(body.decode("utf-8"))
        date.fromisoformat(document["as_of"])
        for found in document["parcels"].values():
            if not isinstance(found["sha256"], str) or any(
                    not isinstance(found[part], list) for part in FINGERPRINT_PARTS):
                raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("fingerprints.json 형식 오류") from None
    return document


def load_fingerprints(db):
    """Every stored parcel; the document's day is the latest one, None while nothing is seeded."""
    rows = db.execute("""
        SELECT parcel_key, as_of, digest, records FROM complex_lifecycle_fingerprints
    """).fetchall()
    return {"as_of": max(row[1] for row in rows).isoformat() if rows else None,
            "parcels": {key: {"sha256": digest, **records} for key, _, digest, records in rows}}


def count_fingerprints(db):
    return db.execute("SELECT count(*) FROM complex_lifecycle_fingerprints").fetchone()[0]


def save_fingerprints(db, document, batch):
    """Take over the parcels whose records changed; an older document never replaces a newer record."""
    db.execute("""
        INSERT INTO complex_lifecycle_fingerprints(parcel_key, as_of, digest, records, batch)
        SELECT key, %s::date, value->>'sha256', value - 'sha256', %s::text FROM jsonb_each(%s::jsonb)
        ON CONFLICT (parcel_key) DO UPDATE
        SET as_of = EXCLUDED.as_of, digest = EXCLUDED.digest, records = EXCLUDED.records, batch = EXCLUDED.batch
        WHERE complex_lifecycle_fingerprints.digest <> EXCLUDED.digest
          AND complex_lifecycle_fingerprints.as_of <= EXCLUDED.as_of
    """, (document["as_of"], batch, Jsonb(document["parcels"])))
