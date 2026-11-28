"""Remove complexes judged not to be apartments, keeping a restorable archive (plan-existence.md 3-4).

  python delete_complexes.py --targets delete_targets.json --archive archive.json [--dry-run]
  python delete_complexes.py --restore archive.json [--dry-run]

Every related row is written to the archive before anything is deleted, in one transaction. The removed IDs
go into complex_exclusions so daily registration cannot create them again. Aliases and rules of other
source IDs that point at a removed complex stay, so their trades are not registered under a new ID.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path

import psycopg

from districts import DISTRICTS, GWANGJIN
from refresh import KST, ensure_schema

ARCHIVE_VERSION = 1
# Deletion order follows the foreign keys: user data, trades and areas, metadata, then the complex.
# Restore inserts in the reverse order. {name: condition}; each %s receives the list of removed IDs.
TARGET = "complex_id = ANY(%s::text[])"
TABLES = (
    ("favorites", TARGET),
    ("saved_comparisons", "left_id = ANY(%s::text[]) OR right_id = ANY(%s::text[])"),
    ("complex_notes", TARGET),
    ("refresh_revision_pending", "complex_id = ANY(%s::text[]) OR trade_id IN "
                                 "(SELECT trade_id FROM trades WHERE complex_id = ANY(%s::text[]))"),
    ("refresh_missing_trades", "trade_id IN (SELECT trade_id FROM trades WHERE complex_id = ANY(%s::text[]))"),
    ("trades", TARGET),
    ("trade_revisions", TARGET),
    ("model_inputs", TARGET),
    ("area_groups", TARGET),
    ("complex_training_exclusions", TARGET),
    ("complex_facilities", TARGET),
    ("complex_lifecycle_state", TARGET),
    ("complex_lifecycle_results", TARGET),
    ("complex_lifecycle_quarantine", TARGET),
    ("complex_metadata_candidate_observations", "candidate_id IN "
     "(SELECT candidate_id FROM complex_metadata_candidates WHERE complex_id = ANY(%s::text[]))"),
    ("complex_metadata_candidates", TARGET),
    ("complex_metadata_evidence", TARGET),
    ("complex_geocode_attempts", TARGET),
    ("complex_geocode_pending", TARGET),
    ("complex_registrations", TARGET),
    ("kapt_verified_links", TARGET),
    ("refresh_identity_pending", "source_id = ANY(%s::text[])"),
    ("refresh_identity_decisions", "source_id = ANY(%s::text[])"),
    ("refresh_identity_period_decisions", "source_id = ANY(%s::text[])"),
    ("complexes", TARGET),
)
IDENTITY_TABLES = {"trades", "complex_lifecycle_quarantine", "refresh_identity_period_decisions"}
# Archives written before these tables were listed stay restorable. complex_facilities holds derived rows
# that the next facility sync rebuilds.
ADDED_TABLES = {"complex_facilities"}


def read_targets(path):
    items = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(items, list) or not items:
        raise ValueError("삭제 대상 목록이 비었습니다")
    targets = {}
    for item in items:
        cid, reason = (item.get("complex_id"), item.get("reason")) if isinstance(item, dict) else (None, None)
        if not isinstance(cid, str) or not cid.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError("삭제 대상마다 complex_id와 reason이 필요합니다")
        if cid in targets or district_code(cid) is None:
            raise ValueError(f"중복이거나 자치구를 알 수 없는 단지 ID: {cid}")
        targets[cid] = reason.strip()
    return targets


def district_code(complex_id):
    parts = complex_id.split(" ")
    return next((code for code, name in DISTRICTS.items() if len(parts) > 1 and parts[1] == name), None)


def lock_districts(db, ids):
    """The refresh workers' district locks, so no reconcile runs while rows move."""
    for code in sorted({district_code(cid) for cid in ids}):
        db.execute("SELECT pg_advisory_xact_lock(%s)", (1121505 if code == GWANGJIN else int(code),))


def parameters(condition, ids):
    return (ids,) * condition.count("%s")


def refuse_unlisted_tables(db, ids):
    """Stop when a table this script does not archive still refers to a target."""
    listed = {name for name, _ in TABLES} | {"complex_exclusions"}
    columns = db.execute("""
        SELECT c.table_name, c.column_name FROM information_schema.columns c
        JOIN information_schema.tables t USING (table_schema, table_name)
        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
          AND c.column_name IN ('complex_id', 'left_id', 'right_id')
        ORDER BY 1, 2
    """).fetchall()
    for table, column in columns:
        if table in listed:
            continue
        # Names come from the catalog and are quoted.
        query = f'SELECT 1 FROM "{table}" WHERE "{column}" = ANY(%s::text[]) LIMIT 1'
        if db.execute(query, (ids,)).fetchone():
            raise ValueError(f"보관 목록에 없는 표가 삭제 대상을 참조합니다: {table}.{column}")


def collect(db, ids):
    rows = {}
    for table, condition in TABLES:
        query = f"SELECT to_jsonb(t)::text FROM {table} AS t WHERE {condition}"
        rows[table] = [row[0] for row in db.execute(query, parameters(condition, ids)).fetchall()]
    return rows


def kept_references(db, ids):
    """Other source IDs that point at a removed complex; they are reported and left in place."""
    found = []
    for table in ("refresh_identity_decisions", "refresh_identity_period_decisions"):
        query = (f"SELECT source_id, target_id FROM {table} "
                 "WHERE target_id = ANY(%s::text[]) AND NOT (source_id = ANY(%s::text[]))")
        found.extend((table, source, target) for source, target in db.execute(query, (ids, ids)).fetchall())
    return found


def write_archive(path, document):
    """Write the archive completely before any row is deleted; an existing file is never replaced."""
    with open(path, "x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=1)
        stream.flush()
        os.fsync(stream.fileno())
    if json.loads(Path(path).read_text(encoding="utf-8")) != document:
        raise ValueError("보관 파일을 다시 읽은 내용이 다릅니다")


def delete(db, targets, archive, today, *, dry=False):
    requested = sorted(targets)
    with db.transaction(force_rollback=dry):
        lock_districts(db, requested)
        present = {row[0] for row in db.execute(
            "SELECT complex_id FROM complexes WHERE complex_id = ANY(%s::text[]) FOR UPDATE", (requested,)
        ).fetchall()}
        excluded = {row[0] for row in db.execute(
            "SELECT complex_id FROM complex_exclusions WHERE complex_id = ANY(%s::text[])", (requested,)
        ).fetchall()}
        unknown = sorted(set(requested) - present - excluded)
        if unknown:
            raise ValueError("DB에 없는 삭제 대상: " + ", ".join(unknown))
        ids = sorted(present)
        if not ids:
            print("이미 모두 삭제되었습니다. 변경 없음")
            return {}
        # Block new user data for these area groups until the rows are gone.
        db.execute("SELECT 1 FROM area_groups WHERE complex_id = ANY(%s::text[]) FOR UPDATE", (ids,))
        refuse_unlisted_tables(db, ids)
        rows = collect(db, ids)
        for table, source, target in kept_references(db, ids):
            print(f"남겨 둠 {table}: {source} -> {target}")
        counts = {table: len(found) for table, found in rows.items() if found}
        print("대상 " + str(len(ids)) + "곳, 삭제 행: " + json.dumps(counts, ensure_ascii=False))
        if dry:
            print("dry-run: 보관 파일을 만들지 않았고 데이터도 바꾸지 않았습니다")
            return counts
        write_archive(archive, {
            "version": ARCHIVE_VERSION, "created_at": datetime.now(KST).isoformat(),
            "targets": {cid: targets[cid] for cid in ids}, "tables": rows,
        })
        for table, condition in TABLES:
            done = db.execute(f"DELETE FROM {table} WHERE {condition}", parameters(condition, ids)).rowcount
            # Cascades may already have removed child rows; more rows than archived means an unsaved change.
            if done > len(rows[table]):
                raise ValueError(f"{table}: 보관한 행보다 많이 삭제되어 중단합니다")
        for cid in ids:
            db.execute("""
                INSERT INTO complex_exclusions(complex_id, reason, excluded_on) VALUES (%s, %s, %s)
                ON CONFLICT (complex_id) DO UPDATE SET reason = EXCLUDED.reason, excluded_on = EXCLUDED.excluded_on
            """, (cid, targets[cid], today))
    print(f"삭제 완료. 보관 파일: {archive}")
    return counts


def restore(db, archive, *, dry=False):
    document = json.loads(Path(archive).read_text(encoding="utf-8"))
    listed = {name for name, _ in TABLES}
    if document.get("version") != ARCHIVE_VERSION or set(document.get("tables", ())) not in (
            listed, listed - ADDED_TABLES):
        raise ValueError("지원하지 않는 보관 파일입니다")
    ids = sorted(document["targets"])
    with db.transaction(force_rollback=dry):
        lock_districts(db, ids)
        found = db.execute("SELECT complex_id FROM complexes WHERE complex_id = ANY(%s::text[])", (ids,)).fetchall()
        if found:
            raise ValueError("이미 있는 단지는 복구하지 않습니다: " + ", ".join(row[0] for row in found))
        for table, _ in reversed(TABLES):
            override = "OVERRIDING SYSTEM VALUE " if table in IDENTITY_TABLES else ""
            query = f"INSERT INTO {table} {override}SELECT * FROM jsonb_populate_record(NULL::{table}, %s::jsonb)"
            for row in document["tables"].get(table, []):
                db.execute(query, (row,))
        db.execute("DELETE FROM complex_exclusions WHERE complex_id = ANY(%s::text[])", (ids,))
    counts = {table: len(rows) for table, rows in document["tables"].items() if rows}
    print(("dry-run 복구 시험 통과: " if dry else "복구 완료: ") + json.dumps(counts, ensure_ascii=False))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--targets", type=Path, help="export_results.py가 만든 delete_targets.json")
    action.add_argument("--restore", type=Path, help="삭제 때 만든 보관 파일")
    parser.add_argument("--archive", type=Path, help="새 보관 파일 경로(삭제할 때 필수)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.targets and not args.dry_run:
        if args.archive is None:
            parser.error("삭제에는 --archive가 필요합니다")
        if args.archive.exists():
            parser.error("보관 파일이 이미 있습니다. 새 경로를 지정하세요")
    targets = read_targets(args.targets) if args.targets else None
    ensure_schema()
    with psycopg.connect(autocommit=True, connect_timeout=8) as db:
        if args.restore:
            restore(db, args.restore, dry=args.dry_run)
        else:
            delete(db, targets, args.archive, datetime.now(KST).date(), dry=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
