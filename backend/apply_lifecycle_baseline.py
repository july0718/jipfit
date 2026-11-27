"""Apply verified existence results from jipfit_lifecycle to PostgreSQL (plan-existence.md 3-1 to 3-3).

  python apply_lifecycle_baseline.py --results results.jsonl [--dry-run]
  python apply_lifecycle_baseline.py --correct <complex_id> --existence present --evidence "..." [--dry-run]

Judgments go through lifecycle_results.apply_lifecycle_results, one transaction per record. The
complexes.existence_status copy and complex_training_exclusions are then rebuilt from the confirmed state,
so an interrupted run is finished by running the same file again. --dry-run rolls every data change back;
preparing the idempotent schema is still committed. Back up the database with pg_dump before a real run.
"""
import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

import psycopg

from lifecycle_results import (
    LifecycleResult, apply_lifecycle_results, effective_date_conflicts, sync_derived, training_exclusion_date,
)
from refresh import KST, ensure_schema

SUCCESSOR_NAME = re.compile(r"; successor_name=([^;]*); note=")
SUMMARY_KEYS = ("applied", "noop", "pending", "quarantined")
# A dry run holds one advisory lock per record until rollback, so it rolls back in small batches.
DRY_RUN_BATCH = 500


def read_results(path):
    """Load JSON Lines into LifecycleResult; a malformed file stops before anything is applied."""
    results, ids, keys = [], set(), set()
    with Path(path).open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                result = LifecycleResult(**json.loads(line))
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError(f"{path} {number}행: 결과 형식 오류 ({type(exc).__name__})") from None
            if result.complex_id in ids or result.result_key in keys:
                raise ValueError(f"{path} {number}행: 단지 ID 또는 result_key 중복 ({result.complex_id})")
            ids.add(result.complex_id)
            keys.add(result.result_key)
            results.append(result)
    if not results:
        raise ValueError(f"{path}: 결과가 없습니다")
    return results


def apply_each(db, results, today):
    """Apply records one by one so each quarantined record can be reported with its reasons."""
    summary = Counter({key: 0 for key in SUMMARY_KEYS})
    quarantined = []
    for result in results:
        outcome = apply_lifecycle_results(db, [result], allowed_ids=None, cutoff=today)
        summary.update(outcome)
        if outcome["quarantined"]:
            key, cid = str(result.result_key or "").strip(), str(result.complex_id or "").strip()
            rows = db.execute("""
                SELECT reason FROM complex_lifecycle_quarantine
                WHERE result_key = %s AND complex_id = %s ORDER BY quarantine_id
            """, (key, cid)).fetchall()
            quarantined.append((key, cid, ", ".join(dict.fromkeys(row[0] for row in rows))))
    return dict(summary), quarantined


def dry_run(db, results, today):
    summary = Counter({key: 0 for key in SUMMARY_KEYS})
    quarantined = []
    for start in range(0, len(results), DRY_RUN_BATCH):
        with db.transaction(force_rollback=True):
            part, held = apply_each(db, results[start:start + DRY_RUN_BATCH], today)
        summary.update(part)
        quarantined.extend(held)
    return dict(summary), quarantined


def marker_ids(db):
    """Complexes the map can draw: not confirmed demolished, valid coordinates and an active priced trade."""
    return {row[0] for row in db.execute("""
        SELECT c.complex_id FROM complexes c
        LEFT JOIN complex_lifecycle_state s USING (complex_id)
        WHERE NOT COALESCE(s.existence = 'demolished', FALSE)
          AND c.latitude BETWEEN 33 AND 39 AND c.longitude BETWEEN 124 AND 132
          AND EXISTS (SELECT 1 FROM trades t WHERE t.complex_id = c.complex_id
                      AND NOT t.is_cancelled AND t.price_manwon > 0)
    """).fetchall()}


def pending_revisions(db):
    return db.execute("SELECT count(*) FROM refresh_revision_pending").fetchone()[0]


def successor_report(db, results):
    """Renewed complexes whose successor has no marker yet, and successors known only by name (3-5)."""
    markers = marker_ids(db)
    linked = sorted({(r.complex_id, r.successor_id) for r in results if r.successor_id})
    print(f"후속 단지 연결 {len(linked)}곳, 마커 없는 후속 단지:")
    missing = [(old, new) for old, new in linked if new not in markers]
    for old, new in missing:
        print(f"  {old} -> {new}")
    print(f"  합계 {len(missing)}곳")
    named = [r for r in results if r.successor_status == "name_only"]
    print(f"이름만 있는 후속 단지 {len(named)}곳:")
    for result in named:
        matched = SUCCESSOR_NAME.search(result.evidence)
        print(f"  {result.complex_id} -> {matched.group(1) if matched else '(근거 확인)'}")


def print_summary(summary, quarantined):
    print("집계: " + " ".join(f"{key}={summary[key]}" for key in SUMMARY_KEYS))
    for reason, count in sorted(Counter(item[2] for item in quarantined).items()):
        print(f"격리 {reason}: {count}건")
        for key, cid, item_reason in quarantined:
            if item_reason == reason:
                print(f"  {cid} ({key})")


def metadata_revision_text():
    """The API's own digest, printed to confirm public data will notice the new state."""
    try:
        from main import metadata_revision
        return metadata_revision()["revision"]
    except Exception as exc:
        return f"확인 불가({type(exc).__name__}); GET /api/metadata-revision 으로 확인"


def run_apply(db, results, today, *, dry, allow_conflicts=False):
    gone = {r.complex_id for r in results if r.existence == "demolished"}
    before = marker_ids(db)
    revisions_before = pending_revisions(db)
    conflicts = effective_date_conflicts(db, results)
    print(f"결과 {len(results)}건: " + " ".join(f"{k}={v}" for k, v in sorted(
        Counter(r.existence for r in results).items())))
    print(f"효력일 있는 결과 {sum(r.effective_date is not None for r in results)}건, "
          f"효력일 이후 기존 거래가 있는 단지 {len(conflicts)}곳")
    for cid, effective, count, last in conflicts:
        print(f"  충돌 {cid}: 효력일 {effective}, 이후 거래 {count}건(마지막 {last})")
    excluded = [r for r in results if training_exclusion_date(r.existence, r.effective_date, r.evidence)]
    print(f"학습 입력 제외 대상 {len(excluded)}곳")
    print(f"마커 대상 단지 {len(before)}곳, 이 중 멸실·전면갱신 {len(before & gone)}곳")

    if dry:
        summary, quarantined = dry_run(db, results, today)
        print_summary(summary, quarantined)
        print(f"dry-run: 데이터 변경 없음. 반영 시 예상 마커 대상 {len(before - gone)}곳")
        return summary

    if conflicts and not allow_conflicts:
        raise SystemExit("효력일 이후 거래가 있어 반영하지 않았습니다. 결과 파일의 효력일을 확인하세요"
                         " (확인 후 강행: --allow-trade-conflicts).")
    revision_before = metadata_revision_text()
    summary, quarantined = apply_each(db, results, today)
    print_summary(summary, quarantined)
    derived = sync_derived(db, [r.complex_id for r in results], today)
    print(f"existence_status 갱신 {derived['existence_status']}곳, "
          f"학습 제외 {derived['training_exclusions']}곳(이번 변경 {derived['changed']}), "
          f"예측 재계산 예약 {', '.join(derived['republish']) or '없음'}")
    after = marker_ids(db)
    hidden = db.execute("""
        SELECT count(*) FROM complex_lifecycle_state WHERE existence = 'demolished'
    """).fetchone()[0]
    print(f"demolition_confirmed 단지 {hidden}곳, 마커 대상 {len(before)} -> {len(after)} ({len(after) - len(before):+d})")
    print(f"refresh_revision_pending {revisions_before} -> {pending_revisions(db)}")
    print(f"metadata-revision {revision_before} -> {metadata_revision_text()}")
    print("정적 데이터는 export_public_json.py(배포 절차)를 다시 실행해야 갱신됩니다.")
    successor_report(db, results)
    return summary


def correct(db, complex_id, existence, evidence, today, *, dry):
    """Replace one complex's confirmed judgment with a new result that names the one it corrects (3-2)."""
    row = db.execute("""
        SELECT result_key, existence, evidence FROM complex_lifecycle_state
        WHERE complex_id = %s AND existence IS NOT NULL
    """, (complex_id,)).fetchone()
    if row is None:
        raise SystemExit(f"정정할 확정 판정이 없습니다: {complex_id}")
    current_key, current_existence, current_evidence = row
    if (current_existence, current_evidence) == (existence, evidence.strip()):
        print(f"이미 같은 판정입니다: {complex_id} ({existence})")
        if not dry:
            sync_derived(db, [complex_id], today)
        return None
    digest = sha256("|".join((current_key, existence, evidence)).encode("utf-8")).hexdigest()[:16]
    result = LifecycleResult(
        result_key=f"correction:{digest}:{complex_id}", complex_id=complex_id, existence=existence,
        evidence=evidence, as_of=today.isoformat(), verifier_version="apply_lifecycle_baseline@manual",
        correction_of=current_key,
    )
    print(f"{complex_id}: {current_existence} ({current_key}) -> {existence}")
    if dry:
        summary, quarantined = dry_run(db, [result], today)
        print_summary(summary, quarantined)
        print("dry-run: 데이터 변경 없음")
        return summary
    summary, quarantined = apply_each(db, [result], today)
    print_summary(summary, quarantined)
    if quarantined:
        raise SystemExit("정정이 격리되었습니다.")
    derived = sync_derived(db, [complex_id], today)
    print(f"학습 제외 변경 {derived['changed']}건, 예측 재계산 예약 {', '.join(derived['republish']) or '없음'}")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--results", type=Path, help="export_results.py가 만든 results.jsonl")
    action.add_argument("--correct", metavar="COMPLEX_ID", help="단지 하나의 확정 판정 정정")
    parser.add_argument("--existence", choices=("present", "demolished"))
    parser.add_argument("--evidence")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-trade-conflicts", action="store_true")
    args = parser.parse_args(argv)
    if args.correct and not (args.existence and (args.evidence or "").strip()):
        parser.error("--correct에는 --existence와 --evidence가 필요합니다")

    results = read_results(args.results) if args.results else None
    today = datetime.now(KST).date()
    ensure_schema()
    # Autocommit keeps each transaction() block a real transaction instead of a savepoint.
    with psycopg.connect(autocommit=True, connect_timeout=8) as db:
        if args.correct:
            correct(db, args.correct, args.existence, args.evidence, today, dry=args.dry_run)
            return 0
        summary = run_apply(db, results, today, dry=args.dry_run, allow_conflicts=args.allow_trade_conflicts)
    return 1 if summary["quarantined"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
