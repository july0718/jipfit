"""The monthly stage of the updater (plan-lifecycle-auto.md 3 to 6), new in Jipfit.

evaluate and gather follow the order of jipfit_lifecycle@d86dcdf:monthly.py run (and
collect_dong_codes.py for the missing dongs). Around them are the run record, the time limit, the guard and
the bundle that lifecycle_inbox applies.

  JIPFIT_LIFECYCLE_DATA              data folder (reference, monthly, batches, reports)
  JIPFIT_LIFECYCLE_DIR               bundle folder, when not <data>/batches
  JIPFIT_LIFECYCLE_COLLECT_MINUTES   collection time of one attempt (90)
  JIPFIT_LIFECYCLE_MAX_AUTO          confirmations a month may apply without the operator (20)
"""
from collections import Counter
import os
import re
import shutil
import time

from lifecycle_check import batches_dir, data_dir, existence, sources, store, verifier_version
from lifecycle_check.collect import fetch_all
from lifecycle_check.facts import register_calls
from lifecycle_check.monthly import (
    CACHE_MARKER, INCOMPLETE_HOLD, MASS_HOLD, candidate_rows, collect_basic, collect_extra, encode_results,
    extra_calls, first_pass, hold_mass_confirmations, judge_month, linked_ids, prepare_cache, write_batch,
    write_reports,
)
from lifecycle_check.targets import dong_codes, identify, needed_months
from lifecycle_inbox import apply_inbox

MAX_ATTEMPTS = 4  # The first attempt and the three retries of the monthly slot.
WORKERS = 8


class Paths:
    def __init__(self):
        data = data_dir()
        self.reference, self.monthly, self.reports = data / "reference", data / "monthly", data / "reports"
        self.batches = batches_dir()


def setting(name, default):
    return int((os.environ.get(name) or "").strip() or default)


def judging_day(today):
    """The day the judgement is made: never before the day a register in the cache was retrieved."""
    return max(today, sources.today())


def input_problem(paths, cache, offline):
    """(label, message) for a missing prerequisite that no retry can supply, else None."""
    if not (paths.reference / "trade_detail").is_dir():
        return "reference", f"기준 자료가 없습니다: {paths.reference} (lifecycle_admin.py import-reference)"
    if offline and not cache.is_dir():
        return "cache", f"월별 캐시가 없습니다: {cache}"
    if not offline and not sources.credentials().get(sources.KEY_NAME):
        return "key", f"{sources.KEY_NAME}가 없습니다"
    return None


def read_inputs(today):
    with store.connect() as db:
        tables, fingerprints = store.load_inputs(db)
    if not fingerprints["parcels"]:
        raise ValueError("기준선 지문이 없습니다. 먼저 lifecycle_admin.py seed-fingerprints를 실행하세요")
    if fingerprints["as_of"] > today.isoformat():
        raise ValueError(f"지문 기준일({fingerprints['as_of']})이 실행 기준일({today})보다 뒤입니다")
    return tables, fingerprints


def states_of(tables):
    return {row["complex_id"]: row for row in tables["complex_lifecycle_state"]}


def identified(tables, cache, reference, as_of):
    codes, conflicts = dong_codes(reference, cache)
    return identify(tables["complexes"], tables["trades"], codes, as_of, conflicts)


def gather(tables, fingerprints, cache, reference, workers=WORKERS):
    """Collect what this month's judgement needs into its cache and return how many calls are still missing."""
    deadline = time.monotonic() + setting("JIPFIT_LIFECYCLE_COLLECT_MINUTES", 90) * 60
    keys = sources.credentials()

    def month_calls(failed):
        return [("trade_detail", {"LAWD_CD": district, "DEAL_YMD": month})
                for district, month in needed_months(failed, tables["trades"])]

    # Dongs the baseline cache does not know are revealed by the trade months of their complexes.
    targets, failed = identified(tables, cache, reference, sources.today())
    if calls := month_calls(failed):
        print(f"법정동코드를 확인할 실거래 월 {len(calls)}", flush=True)
        for name, count in sorted(fetch_all(calls, cache, keys, 1, deadline=deadline, refresh=False).items()):
            print(name, count, flush=True)
        targets, failed = identified(tables, cache, reference, sources.today())
    collect_basic(targets, cache, keys, sources.today(), workers, deadline)
    day = sources.today()
    plan = first_pass(tables["complexes"], targets, failed, states_of(tables), cache, reference, fingerprints, day)
    rows = candidate_rows(plan, cache, day)
    collect_extra(rows, cache, keys, day, workers, deadline)
    day = sources.today()
    unfetched = [call for call in month_calls(failed) if not sources.cache_file(cache, *call).exists()]
    return len(unfetched) + len(register_calls(targets, cache, day)) + len(extra_calls(rows, cache, day))


def evaluate(tables, fingerprints, cache, reference, as_of, version):
    """The judgement of one month from the database rows and the cached records."""
    existence._MANAGED.clear()
    states = states_of(tables)
    targets, failed = identified(tables, cache, reference, as_of)
    plan = first_pass(tables["complexes"], targets, failed, states, cache, reference, fingerprints, as_of)
    return judge_month(tables["complexes"], targets, failed, states, linked_ids(tables["complex_lifecycle_results"]),
                       plan, cache, reference, fingerprints, as_of, version)


def judge(tables, fingerprints, cache, reference, as_of):
    outcome = evaluate(tables, fingerprints, cache, reference, as_of, verifier_version())
    return hold_mass_confirmations(outcome, states_of(tables), setting("JIPFIT_LIFECYCLE_MAX_AUTO", 20))


def counted(outcome):
    results = outcome["results"]
    holds = [r for r in results if r["existence"] == "pending"]
    return {"대상": outcome["summary"]["대상 단지"],
            "현존 확인": sum(r["existence"] == "present" for r in results),
            "자동 확정": 0 if outcome["proposals"] else len(outcome["auto"]),
            MASS_HOLD: len(outcome["proposals"]),
            "보류": len(holds),
            "운영자 확인": sum(r["hold_code"] != INCOMPLETE_HOLD for r in holds),
            "후속 연결": sum(r.get("successor_status") == "linked" for r in results)}


def log_line(batch, outcome, remaining):
    found = counted(outcome)
    collected = "받은 자료로 판정" if remaining is None else (
        f"수집 미완({remaining:,})" if remaining else "수집 완료")
    mass = f"({MASS_HOLD} {found[MASS_HOLD]:,})" if found[MASS_HOLD] else ""
    return (f"Lifecycle {batch}: 대상 {found['대상']:,} / {collected} / 현존 확인 {found['현존 확인']:,} / "
            f"자동 확정 {found['자동 확정']:,}{mass} / 보류 {found['보류']:,}(운영자 확인 {found['운영자 확인']:,}) / "
            f"후속 연결 {found['후속 연결']:,}")


def run_month(today=None, *, dry_run=False, offline=False, workers=WORKERS):
    """Run or resume the check of today's month and return failure labels for the caller to retry.

    dry_run judges and writes reports without changing the database or the fingerprints. offline judges
    what is already cached (a rehearsal: the reference cache alone).
    """
    # A single run before 06:00 passes the previous slot day; the month is that of the actual judging day.
    today = judging_day(today or sources.today())
    paths = Paths()
    with store.connect() as lock:
        if not store.try_lock(lock):
            print("Lifecycle: 다른 월간 판정이 실행 중입니다.", flush=True)
            return ["lifecycle:locked"]
        if dry_run:
            return rehearse(paths, today, offline, workers)
        return advance(paths, today, offline, workers)


def advance(paths, today, offline, workers):
    month = f"{today:%Y-%m}"
    with store.connect() as db:
        run = store.read_run(db, month)
    if run and run["status"] in ("applied", "disabled"):
        reason = "이미 반영한 달" if run["status"] == "applied" else "사용하지 않기로 한 달"
        print(f"Lifecycle {month}: {reason}입니다.", flush=True)
        return []
    if run and run["status"] == "judged":
        return apply(paths, month, run["batch"], today)
    if run and run["batch"] and (paths.batches / run["batch"]).is_dir():
        # Stopped between publishing the bundle and recording that it was judged.
        with store.connect() as db:
            store.mark_judged(db, month)
        return apply(paths, month, run["batch"], today)

    cache = paths.monthly / month
    if problem := input_problem(paths, cache, offline):
        print(f"Lifecycle {month}: {problem[1]}", flush=True)
        return [f"lifecycle:{problem[0]}"]
    with store.connect() as db:
        attempt = store.start_attempt(db, month)
    try:
        tables, fingerprints = read_inputs(today)
        remaining = None
        if not offline:
            existence._MANAGED.clear()
            prepare_cache(cache, paths.reference, sources.today())
            remaining = gather(tables, fingerprints, cache, paths.reference, workers)
            if remaining and attempt < MAX_ATTEMPTS:
                with store.connect() as db:
                    store.note_run(db, month, remaining, "lifecycle:collecting")
                print(f"Lifecycle {month}: 남은 조회 {remaining:,} (시도 {attempt}/{MAX_ATTEMPTS}). 다음 시도에서 "
                      "이어받습니다.", flush=True)
                return ["lifecycle:collecting"]
        as_of = judging_day(today)
        if f"{as_of:%Y-%m}" != month:
            # A bundle dated in the next month would take the name that month's own check needs.
            with store.connect() as db:
                store.note_run(db, month, remaining, "lifecycle:month-changed")
            print(f"Lifecycle {month}: 수집 중에 달이 바뀌어 판정하지 않았습니다. 이번 달 실행이 새로 판정합니다.",
                  flush=True)
            return ["lifecycle:month-changed"]
        outcome = judge(tables, fingerprints, cache, paths.reference, as_of)

        def record_name(batch):
            with store.connect() as db:
                store.name_batch(db, month, batch, as_of)

        folder = write_batch(outcome, paths.batches, paths.reports, as_of, verifier_version(), record_name)
        with store.connect() as db:
            store.mark_judged(db, month, remaining, {**counted(outcome), "judgement": outcome["summary"]})
        print(log_line(folder.name, outcome, remaining), flush=True)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}" if isinstance(exc, ValueError) else type(exc).__name__
        print(f"Lifecycle {month}: {message}", flush=True)
        try:
            with store.connect() as db:
                store.note_run(db, month, None, message[:500])
        except Exception:
            pass  # The error itself is what the caller must see.
        raise
    return apply(paths, month, folder.name, today)


def apply(paths, month, batch, today):
    """Apply the unrecorded bundles; recording this month's one also closes the run and moves fingerprints."""
    failed = apply_inbox(paths.batches, judging_day(today))
    with store.connect() as db:
        run = store.read_run(db, month)
    if not run or run["status"] != "applied":
        label = failed[0] if failed else batch
        print(f"Lifecycle {batch}: 반영이 끝나지 않았습니다. {paths.batches / str(batch)}와 위 기록을 확인하세요.",
              flush=True)
        return [f"lifecycle:{label}"]
    remove_old_caches(paths.monthly, month)
    return []


def remove_old_caches(folder, month):
    """Keep this month's and last month's cache; older ones go once a month has been applied."""
    year, number = map(int, month.split("-"))
    previous = f"{year - (number == 1):04}-{(number - 2) % 12 + 1:02}"
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        if re.fullmatch(r"\d{4}-\d{2}", path.name) and path.name < previous and (path / CACHE_MARKER).is_file():
            try:
                shutil.rmtree(path)
            except OSError as exc:
                print(f"지난 월별 캐시를 지우지 못했습니다({type(exc).__name__}): {path}", flush=True)


def rehearse(paths, today, offline, workers):
    """Judge without touching the database or the fingerprints and write reports/rehearsal-<day>."""
    cache = paths.reference if offline else paths.monthly / f"{today:%Y-%m}"
    if problem := input_problem(paths, cache, offline):
        print(f"Lifecycle rehearsal: {problem[1]}", flush=True)
        return [f"lifecycle:{problem[0]}"]
    tables, fingerprints = read_inputs(today)
    remaining = None
    if not offline:
        existence._MANAGED.clear()
        prepare_cache(cache, paths.reference, sources.today())
        remaining = gather(tables, fingerprints, cache, paths.reference, workers)
    as_of = judging_day(today)
    outcome = judge(tables, fingerprints, cache, paths.reference, as_of)
    name = f"rehearsal-{as_of:%Y%m%d}"
    folder = write_reports(paths.reports / name, name, outcome)
    (folder / "results.jsonl").write_bytes(encode_results(outcome["results"]))
    (folder / "proposals.jsonl").write_bytes(encode_results(outcome["proposals"]))
    print(log_line(name, outcome, remaining), flush=True)
    print(f"리허설: DB와 지문은 바꾸지 않았습니다. 보류 코드 "
          f"{dict(Counter(r['hold_code'] for r in outcome['results'] if r['existence'] == 'pending'))}, "
          f"보고서 {folder}", flush=True)
    return []
