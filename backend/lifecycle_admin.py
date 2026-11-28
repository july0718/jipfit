"""Operator commands of the monthly existence check (plan-lifecycle-auto.md 7, 8 and 10).

  python lifecycle_admin.py holds [--all]
  python lifecycle_admin.py resolve <complex_id> --existence present|demolished --evidence "<확인한 근거>"
         [--effective-date YYYY-MM-DD [--precision day|month|year]] [--trade-caution-from YYYY-MM-DD]
         [--successor-id <complex_id>] [--dry-run]
  python lifecycle_admin.py accept --batch monthly-<YYYYMMDD> (--all | <complex_id> ...) [--dry-run]
  python lifecycle_admin.py revert --batch monthly-<YYYYMMDD> [--complex <complex_id> ...] [--dry-run]
  python lifecycle_admin.py run [--dry-run] [--offline]
  python lifecycle_admin.py import-reference --from <jipfit_lifecycle>/source_cache --to lifecycle-data/reference
  python lifecycle_admin.py seed-fingerprints --as-of 2026-11-26

import-reference needs only the standard library, so it runs on the host. The other commands run in the updater
container. --dry-run rolls every data change back; preparing the idempotent schema is still committed.
"""
import argparse
from collections import Counter
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil

from lifecycle_check import batches_dir, data_dir, run_month
from lifecycle_check.common import csv_output
from lifecycle_check.monthly import (
    INCOMPLETE_HOLD, MASS_HOLD, active_hold, baseline_fingerprints, evidence_text,
)
from lifecycle_check.sources import REFERENCE_FOLDERS, today as kst_today

BASIS_OPERATOR = "운영자 확인"
VERSION = "lifecycle_admin@manual"
BATCH_NAME = re.compile(r"monthly-\d{8}")
SOURCE_FINGERPRINTS = "source_fingerprints.json"
LABELS = {"present": "현존", "demolished": "멸실", None: "미판정"}
HOLD_COLUMNS = ("complex_id", "name", "address", "보류코드", "보류시작일", "상세", "지도상태")
STATE_COLUMNS = ("name", "result_key", "existence", "as_of", "evidence", "effective_date", "effective_precision",
                 "pending_result_key", "pending_hold_code", "pending_as_of", "exclude_from", "exclude_reason")


def digest(*parts):
    return sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()[:16]


def current_state(db, complex_id):
    """The complex's judgment and training exclusion with days as text, or None when it is not listed."""
    row = db.execute("""
        SELECT c.name, s.result_key, s.existence, s.as_of, s.evidence, s.effective_date, s.effective_precision,
               s.pending_result_key, s.pending_hold_code, s.pending_as_of, x.exclude_from, x.reason
        FROM complexes c
        LEFT JOIN complex_lifecycle_state s ON s.complex_id = c.complex_id
        LEFT JOIN complex_training_exclusions x ON x.complex_id = c.complex_id
        WHERE c.complex_id = %s
    """, (complex_id,)).fetchone()
    if row is None:
        return None
    return {name: value.isoformat() if isinstance(value, date) else value for name, value in zip(STATE_COLUMNS, row)}


def preview(complex_id, state, result):
    """Print what a judgment changes: existence, effective date, training exclusion and the map marker."""
    from lifecycle_results import EXCLUSION_PREFIX, training_exclusion_date

    effective = str(result.effective_date) if result.effective_date else None
    excluded = state["exclude_from"]
    if excluded and not str(state["exclude_reason"] or "").startswith(EXCLUSION_PREFIX):
        after = f"{excluded} (직접 입력, 유지)"
    else:
        since = training_exclusion_date(result.existence, result.effective_date, result.evidence)
        after = since.isoformat() if since else None

    def marker(existence):
        return "숨김" if existence == "demolished" else "표시 대상"

    print(complex_id)
    print(f"  존치: {LABELS[state['existence']]} -> {LABELS[result.existence]}")
    print(f"  효력일: {state['effective_date'] or '없음'} -> {effective or '없음'}")
    print(f"  학습 제외: {excluded or '없음'} -> {after or '없음'}")
    print(f"  지도 마커: {marker(state['existence'])} -> {marker(result.existence)}")
    if hold := active_hold(state):
        print(f"  열린 보류: {hold} ({state['pending_as_of']})")


def lock_district(db, complex_id):
    """The lock the monthly metadata stages and the bundle apply take for this district."""
    from automatic_metadata import KAPT_LOCK_BASE
    from lifecycle_results import DISTRICT_CODES

    words = complex_id.split(" ")
    code = DISTRICT_CODES.get(words[1]) if len(words) > 1 else None
    if code:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (KAPT_LOCK_BASE + int(code),))


def apply_manual(db, result, today, *, dry):
    """Apply one operator judgment with its derived values: the one path of resolve, revert and --correct."""
    import apply_lifecycle_baseline as baseline

    if dry:
        summary, quarantined = baseline.dry_run(db, [result], today)
        baseline.print_summary(summary, quarantined)
        print("dry-run: 데이터 변경 없음")
        return summary
    with db.transaction():
        lock_district(db, result.complex_id)
        summary, quarantined = baseline.apply_each(db, [result], today)
        derived = None if quarantined else baseline.sync_derived(db, [result.complex_id], today)
    baseline.print_summary(summary, quarantined)
    if quarantined:
        raise SystemExit("정정이 격리되었습니다.")
    print(f"학습 제외 변경 {derived['changed']}건, 예측 재계산 예약 {', '.join(derived['republish']) or '없음'}")
    return summary


def correct(db, complex_id, existence, evidence, today, *, dry):
    """Replace one complex's confirmed judgment with a new result that names the one it corrects (3-2)."""
    import apply_lifecycle_baseline as baseline
    from lifecycle_results import LifecycleResult

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
            baseline.sync_derived(db, [complex_id], today)
        return None
    key = digest(current_key, existence, evidence)
    result = LifecycleResult(
        result_key=f"correction:{key}:{complex_id}", complex_id=complex_id, existence=existence,
        evidence=evidence, as_of=today.isoformat(), verifier_version="apply_lifecycle_baseline@manual",
        correction_of=current_key,
    )
    print(f"{complex_id}: {current_existence} ({current_key}) -> {existence}")
    return apply_manual(db, result, today, dry=dry)


def resolve(db, complex_id, existence, note, today, *, effective_date=None, precision=None, caution=None,
            successor_id=None, dry=False):
    """The operator's judgment of one complex, which closes its open hold (7-2)."""
    from lifecycle_results import LifecycleResult, effective_date_conflicts

    note = " ".join(note.split())
    if not note:
        raise SystemExit("--evidence에 확인한 근거를 적으세요")
    if existence == "present" and (effective_date or precision or caution or successor_id):
        raise SystemExit("효력일, 거래 주의 날짜, 후속 단지는 --existence demolished에서만 씁니다")
    if precision and not effective_date:
        raise SystemExit("--precision은 --effective-date와 함께 씁니다")
    if effective_date and effective_date > today:
        raise SystemExit(f"효력일은 오늘({today})보다 뒤일 수 없습니다")
    state = current_state(db, complex_id)
    if state is None:
        raise SystemExit(f"단지 ID를 찾을 수 없습니다: {complex_id}")
    successor_name = ""
    if successor_id:
        row = db.execute("SELECT name FROM complexes WHERE complex_id = %s", (successor_id,)).fetchone()
        if row is None or successor_id == complex_id:
            raise SystemExit(f"후속 단지 ID를 확인하세요: {successor_id}")
        successor_name = row[0].replace(";", ",")
    evidence = evidence_text({"basis": BASIS_OPERATOR, "successor_name": successor_name, "note": note},
                             caution.isoformat() if caution else "")
    effective = effective_date.isoformat() if effective_date else None
    precision = (precision or "day") if effective else None
    confirmed = state["result_key"] if state["existence"] else None
    key = digest(confirmed, existence, evidence, effective, precision, successor_id)
    result = LifecycleResult(
        result_key=f"manual-{today:%Y%m%d}:{key}:{complex_id}", complex_id=complex_id, existence=existence,
        evidence=evidence, as_of=today.isoformat(), verifier_version=VERSION, correction_of=confirmed,
        successor_id=successor_id, successor_status="linked" if successor_id else None,
        effective_date=effective, effective_precision=precision,
    )
    if ((state["existence"], state["evidence"], state["effective_date"]) == (existence, evidence, effective)
            and active_hold(state) is None):
        print(f"이미 같은 판정입니다: {complex_id} ({existence})")
        if not dry:
            import apply_lifecycle_baseline as baseline

            baseline.sync_derived(db, [complex_id], today)
        return None
    # A demolition dated before stored contracts would make the daily rechecks hold them.
    if effective and effective != state["effective_date"]:
        for _, day, count, last in effective_date_conflicts(db, [result]):
            raise SystemExit(f"효력일 {day} 이후 거래 {count}건(마지막 {last})이 있어 반영하지 않았습니다. 멸실이면 "
                             "--effective-date 없이 --trade-caution-from으로 지정하세요")
    preview(complex_id, state, result)
    if existence == "demolished" and state["effective_date"] and not effective:
        print(f"  주의: 지금 효력일 {state['effective_date']}이 지워집니다. 유지하려면 --effective-date로 다시 지정하세요")
    return apply_manual(db, result, today, dry=dry)


def check_batch(name):
    if not BATCH_NAME.fullmatch(name):
        raise SystemExit(f"묶음 이름은 monthly-YYYYMMDD입니다: {name}")
    folder = batches_dir() / name
    if not folder.is_dir():
        raise SystemExit(f"묶음 폴더가 없습니다: {folder}")
    return folder


def accept(db, batch, ids, today, *, dry=False):
    """Apply the confirmations the mass guard held back, as the bundle proposed them (7-2)."""
    from lifecycle_inbox import apply_bundle
    from lifecycle_results import LifecycleResult

    folder = check_batch(batch)
    recorded = db.execute("SELECT status FROM complex_lifecycle_batches WHERE batch = %s", (batch,)).fetchone()
    if recorded is None or recorded[0] != "applied":
        raise SystemExit(f"아직 반영하지 않은 묶음입니다: {batch}")
    path = folder / "proposals.jsonl"
    lines = path.read_text(encoding="utf-8").split("\n") if path.is_file() else []
    proposals = {row["complex_id"]: row for row in (json.loads(line) for line in lines if line.strip())}
    if not proposals:
        raise SystemExit(f"{batch}에는 {MASS_HOLD} 원본이 없습니다")
    chosen = []
    for cid in dict.fromkeys(ids if ids is not None else proposals):
        proposal = proposals.get(cid)
        state = current_state(db, cid) if proposal else None
        if proposal is None:
            reason = "이 묶음의 proposals.jsonl에 없음"
        elif state is None:
            reason = "목록에 없는 단지"
        elif active_hold(state) != MASS_HOLD or state["pending_as_of"] > proposal["as_of"]:
            reason = (f"그사이 상태가 바뀜(판정 {LABELS[state['existence']]} {state['as_of'] or ''}, "
                      f"보류 {active_hold(state) or '없음'})")
        else:
            result = LifecycleResult(**{**proposal, "result_key": f"accept-{today:%Y%m%d}:{cid}"})
            preview(cid, state, result)
            chosen.append(result)
            continue
        print(f"건너뜀 {cid}: {reason}")
    if not chosen:
        print("반영할 단지가 없습니다")
        return None
    if dry:
        # The district transactions inside become savepoints of this one and are rolled back with it.
        with db.transaction(force_rollback=True):
            summary = apply_bundle(db, chosen, today)
    else:
        summary = apply_bundle(db, chosen, today)
    print("집계: " + " ".join(f"{key}={value}" for key, value in summary.items()))
    if summary["conflicts"]:
        print("효력일 이후 거래가 있는 단지는 말소후거래 보류로 남겼습니다")
    if dry:
        print("dry-run: 데이터 변경 없음")
    return summary


def revert(db, batch, ids, today, *, dry=False):
    """Correct back every confirmation this bundle changed to the judgment before it (8)."""
    from lifecycle_inbox import parse_bundle, read_files
    from lifecycle_results import LifecycleResult

    folder = check_batch(batch)
    try:
        _, results = parse_bundle(batch, *read_files(folder))
    except ValueError as error:
        raise SystemExit(f"{batch}: {error}") from None
    confirmed = {r.complex_id: r for r in results if r.existence != "pending"}
    for cid in ids or ():
        if cid not in confirmed:
            print(f"건너뜀 {cid}: 이 묶음의 확정 판정이 아님")
    outcome = Counter()
    for cid, applied in confirmed.items():
        if ids and cid not in ids:
            continue
        state = current_state(db, cid)
        if state is None or state["result_key"] != applied.result_key:
            print(f"건너뜀 {cid}: 묶음 뒤에 판정이 바뀌었거나 반영되지 않음")
            outcome["건너뜀"] += 1
            continue
        previous = db.execute("""
            SELECT p.result_key, p.existence, p.evidence, p.effective_date, p.effective_precision,
                   p.successor_id, p.successor_status
            FROM complex_lifecycle_results p
            JOIN complex_lifecycle_results b ON b.result_key = %s
            WHERE p.complex_id = %s AND p.applied_status = 'applied' AND p.existence <> 'pending'
              AND p.recorded_at < b.recorded_at
            ORDER BY p.recorded_at DESC LIMIT 1
        """, (applied.result_key, cid)).fetchone()
        if previous is None:
            advice = (" 멸실이 잘못됐으면 resolve --existence present로 바로잡으세요"
                      if applied.existence == "demolished" else "")
            print(f"알림 {cid}: 직전 확정이 없어 되돌리지 않음({LABELS[applied.existence]}).{advice}")
            outcome["직전 확정 없음"] += 1
            continue
        key, existence, evidence, effective, precision, successor, status = previous
        result = LifecycleResult(
            result_key=f"revert-{today:%Y%m%d}:{digest(applied.result_key, key)}:{cid}", complex_id=cid,
            existence=existence, evidence=evidence, as_of=today.isoformat(), verifier_version=VERSION,
            correction_of=applied.result_key, successor_id=successor, successor_status=status,
            effective_date=effective.isoformat() if effective else None, effective_precision=precision,
        )
        preview(cid, state, result)
        apply_manual(db, result, today, dry=dry)
        outcome["되돌림"] += 1
    print(f"{batch}: " + (", ".join(f"{name} {count}" for name, count in outcome.items()) or "되돌릴 확정 판정 없음"))
    return outcome


def holds(db, today, *, show_all=False):
    """List the open holds with the map state they leave, and save the list as a CSV (7-1)."""
    found = db.execute("""
        SELECT s.complex_id, c.name, c.address, s.existence, s.as_of, s.pending_hold_code, s.pending_as_of,
               r.evidence,
               COALESCE(c.latitude BETWEEN 33 AND 39 AND c.longitude BETWEEN 124 AND 132, FALSE)
               AND EXISTS (SELECT 1 FROM trades t WHERE t.complex_id = c.complex_id
                           AND NOT t.is_cancelled AND t.price_manwon > 0)
        FROM complex_lifecycle_state s
        JOIN complexes c ON c.complex_id = s.complex_id
        LEFT JOIN complex_lifecycle_results r ON r.result_key = s.pending_result_key
        WHERE s.pending_hold_code IS NOT NULL
        ORDER BY s.pending_hold_code, s.pending_as_of, s.complex_id
    """).fetchall()
    rows, hidden = [], 0
    for cid, name, address, existence, as_of, code, since, evidence, drawable in found:
        state = {"existence": existence, "as_of": as_of.isoformat() if as_of else None,
                 "pending_hold_code": code, "pending_as_of": since.isoformat() if since else None}
        if active_hold(state) is None:
            continue
        if code == INCOMPLETE_HOLD and not show_all:
            hidden += 1
            continue
        marker = ("마커 숨김" if existence == "demolished" else
                  "마커 표시" if drawable else "마커 없음(좌표·거래 없음)")
        detail = (evidence or "").split("note=", 1)[-1]
        rows.append(dict(zip(HOLD_COLUMNS, (cid, name, address, code, state["pending_as_of"], detail,
                                            f"{LABELS[existence]}, {marker}"))))
    for row in rows:
        print(" | ".join(str(row[column]) for column in
                         ("보류코드", "보류시작일", "complex_id", "name", "address", "지도상태", "상세")))
    counts = Counter(row["보류코드"] for row in rows)
    print(f"열린 보류 {len(rows)}곳: " + (", ".join(f"{code} {count}" for code, count in sorted(counts.items()))
                                       or "없음"))
    if hidden:
        print(f"{INCOMPLETE_HOLD} {hidden}곳은 다음 달 자동 재판정이라 뺐습니다(--all로 표시)")
    path = data_dir() / "reports" / f"holds-{today:%Y%m%d}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    csv_output(path, rows, list(HOLD_COLUMNS))
    print(f"CSV: {path}")
    return rows


def import_reference(source, target, today):
    """Copy the baseline folders the check reads: the only step that reads outside Jipfit (10-2)."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise SystemExit("--from과 --to는 서로 겹치지 않는 폴더여야 합니다")
    missing = [folder for folder in REFERENCE_FOLDERS if not (source / folder).is_dir()]
    if missing:
        raise SystemExit(f"기준 캐시에 폴더가 없습니다: {', '.join(missing)} ({source})")
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"기준 자료가 이미 있습니다: {target}. 다시 복사하려면 기존 폴더를 옮긴 뒤 실행하세요")
    temporary = target.with_name(f".{target.name}.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    files, counts, size = {}, Counter(), 0

    def copy(path, name):
        body = path.read_bytes()
        destination = temporary / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        files[name] = sha256(body).hexdigest()
        return len(body)

    for folder in REFERENCE_FOLDERS:
        for path in sorted((source / folder).rglob("*")):
            if path.is_file() and path.suffix != ".tmp":
                size += copy(path, path.relative_to(source).as_posix())
                counts[folder] += 1
    # Kept only so seed-fingerprints can say whether it made the same records as the verifier did.
    fingerprints = source.parent / "results" / "fingerprints" / "latest.json"
    if fingerprints.is_file():
        size += copy(fingerprints, SOURCE_FINGERPRINTS)
    manifest = {"imported_on": today.isoformat(), "folders": dict(counts), "file_count": len(files), "files": files}
    (temporary / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if target.exists():
        target.rmdir()
    temporary.rename(target)
    print(f"기준 자료 {len(files):,}개 파일, {size / 1_000_000:,.0f}MB: {target}")
    for folder in REFERENCE_FOLDERS:
        print(f"  {folder} {counts[folder]:,}")
    print("  원본 지문 " + ("함께 복사" if SOURCE_FINGERPRINTS in files else "없음(시드 비교 생략)"))
    return manifest


def seed_fingerprints(db, reference, as_of):
    """Store the baseline fingerprints once, made from the copied reference (10-4)."""
    from lifecycle_check import store

    if count := store.count_fingerprints(db):
        raise SystemExit(f"지문이 이미 {count:,}개 있습니다. 다시 만들지 않습니다")
    if not (reference / "trade_detail").is_dir():
        raise SystemExit(f"기준 자료가 없습니다: {reference}. 먼저 import-reference를 실행하세요")
    document = baseline_fingerprints(reference, as_of)
    if not document["parcels"]:
        raise SystemExit(f"{as_of} 기준 기본 대장이 없습니다: {reference}")
    with db.transaction():
        store.save_fingerprints(db, document, f"seed-{as_of:%Y%m%d}")
    print(f"지문 {len(document['parcels']):,}개 지번 저장 (기준일 {as_of})")
    source = reference / SOURCE_FINGERPRINTS
    if not source.is_file():
        print("원본 지문 파일이 없어 비교하지 않았습니다")
        return document
    original = json.loads(source.read_text(encoding="utf-8"))
    ours, theirs = document["parcels"], original["parcels"]
    differing = sum(ours[key] != theirs[key] for key in ours.keys() & theirs.keys())
    if original["as_of"] == document["as_of"] and ours == theirs:
        print(f"원본 지문과 같습니다 (지번 {len(theirs):,}개)")
    else:
        print(f"원본 지문과 다릅니다: 원본 {original['as_of']} {len(theirs):,}개, 저장에만 있음 "
              f"{len(ours.keys() - theirs.keys()):,}, 원본에만 있음 {len(theirs.keys() - ours.keys()):,}, "
              f"내용 다름 {differing:,}")
    return document


def parser():
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = top.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("holds", help="열린 보류 목록")
    listing.add_argument("--all", action="store_true", help="수집미완도 표시")

    one = commands.add_parser("resolve", help="보류 단지 판정 반영")
    one.add_argument("complex_id")
    one.add_argument("--existence", choices=("present", "demolished"), required=True)
    one.add_argument("--evidence", required=True)
    one.add_argument("--effective-date", type=date.fromisoformat)
    one.add_argument("--precision", choices=("day", "month", "year"))
    one.add_argument("--trade-caution-from", type=date.fromisoformat)
    one.add_argument("--successor-id")
    one.add_argument("--dry-run", action="store_true")

    take = commands.add_parser("accept", help="대량확정보류 원본 확정 반영")
    take.add_argument("--batch", required=True)
    take.add_argument("--all", action="store_true")
    take.add_argument("complex_ids", nargs="*", metavar="COMPLEX_ID")
    take.add_argument("--dry-run", action="store_true")

    undo = commands.add_parser("revert", help="묶음의 확정 판정 되돌리기")
    undo.add_argument("--batch", required=True)
    undo.add_argument("--complex", nargs="+", action="extend", metavar="COMPLEX_ID")
    undo.add_argument("--dry-run", action="store_true")

    month = commands.add_parser("run", help="이번 달 판정 실행")
    month.add_argument("--dry-run", action="store_true", help="반영 없이 reports/rehearsal-<오늘>에 보고서만")
    month.add_argument("--offline", action="store_true",
                       help="받아 둔 자료로 판정(--dry-run과 함께면 기준 자료만)")

    copy = commands.add_parser("import-reference", help="전환 때 한 번: 기준 자료 복사")
    copy.add_argument("--from", dest="source", type=Path, required=True)
    copy.add_argument("--to", dest="target", type=Path)

    seed = commands.add_parser("seed-fingerprints", help="전환 때 한 번: 기준선 지문 저장")
    seed.add_argument("--as-of", type=date.fromisoformat, required=True)
    return top


def main(argv=None):
    top = parser()
    args = top.parse_args(argv)
    if args.command == "accept" and args.all == bool(args.complex_ids):
        top.error("accept에는 --all이나 단지 ID 중 하나만 지정합니다")
    today = kst_today()
    if args.command == "import-reference":
        import_reference(args.source, args.target or data_dir() / "reference", today)
        return 0

    from lifecycle_check import store
    from refresh import ensure_schema

    ensure_schema()
    if args.command == "run":
        return 1 if run_month(today, dry_run=args.dry_run, offline=args.offline) else 0
    with store.connect() as db:
        if args.command == "holds":
            holds(db, today, show_all=args.all)
        elif args.command == "resolve":
            resolve(db, args.complex_id, args.existence, args.evidence, today, effective_date=args.effective_date,
                    precision=args.precision, caution=args.trade_caution_from, successor_id=args.successor_id,
                    dry=args.dry_run)
        elif args.command == "accept":
            summary = accept(db, args.batch, None if args.all else args.complex_ids, today, dry=args.dry_run)
            return 1 if summary and summary["quarantined"] else 0
        elif args.command == "revert":
            revert(db, args.batch, args.complex, today, dry=args.dry_run)
        else:
            seed_fingerprints(db, data_dir() / "reference", args.as_of)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
