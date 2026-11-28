"""Apply the monthly judgment bundles that the monthly check (lifecycle_check) writes into its bundle folder.

Each folder holds manifest.json and results.jsonl. Judgments go through
lifecycle_results.apply_lifecycle_results, one transaction per district together with the derived values,
so running a bundle again after a failure only repeats judgments that become noop. A new demolition that a
stored contract contradicts is applied as a hold instead. A bundle is recorded in complex_lifecycle_batches
once every district is done, or as rejected when its files are malformed. Recording a bundle that carries
fingerprints.json also takes over the fingerprints and closes that month's run, in the same transaction.
"""
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from automatic_metadata import KAPT_LOCK_BASE
from districts import DISTRICTS
from lifecycle_check import store
from lifecycle_results import (
    DISTRICT_CODES, LifecycleResult, apply_lifecycle_results, effective_date_conflicts, sync_derived,
)

# conflicts counts the demolitions turned into holds; they also appear under pending or noop.
SUMMARY_KEYS = ("applied", "noop", "pending", "quarantined", "skipped", "conflicts")
CONFLICT_HOLD = "말소후거래"  # The hold code the verifier gives the same facts.
MANIFEST_FIELDS = {"batch": str, "as_of": str, "verifier_version": str, "results": int, "sha256": str,
                   "counts": dict}


def read_files(folder):
    """(manifest bytes, result bytes); a missing file reads as None and makes the bundle malformed."""
    def read(name):
        path = folder / name
        return path.read_bytes() if path.is_file() else None

    return read("manifest.json"), read("results.jsonl")


def bundle_checksum(files):
    digest = sha256()
    for body in files:
        digest.update(b"-" if body is None else b"%d:" % len(body) + body)
    return digest.hexdigest()


def parse_bundle(batch, manifest_bytes, body):
    """(as_of, results) of a well-formed bundle, else ValueError naming what is wrong."""
    if manifest_bytes is None or body is None:
        raise ValueError("manifest.json 또는 results.jsonl 없음")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except ValueError:
        raise ValueError("manifest.json 형식 오류") from None
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json 형식 오류")
    wrong = [name for name, kind in MANIFEST_FIELDS.items() if type(manifest.get(name)) is not kind]
    if wrong:
        raise ValueError("manifest 필수 필드 오류: " + ", ".join(wrong))
    if manifest["batch"] != batch:
        raise ValueError("manifest의 묶음 이름이 폴더 이름과 다름")
    try:
        as_of = date.fromisoformat(manifest["as_of"])
    except ValueError:
        raise ValueError("manifest의 as_of 형식 오류") from None
    if sha256(body).hexdigest() != manifest["sha256"]:
        raise ValueError("results.jsonl의 SHA-256 불일치")
    results, keys, ids = [], set(), set()
    try:
        # Split on newlines only: evidence text may hold other Unicode line separators.
        for number, line in enumerate(body.decode("utf-8").split("\n"), start=1):
            if not line.strip():
                continue
            result = LifecycleResult(**json.loads(line))
            if not isinstance(result.complex_id, str) or not isinstance(result.result_key, str):
                raise ValueError
            if result.complex_id in ids or result.result_key in keys:
                raise ValueError
            ids.add(result.complex_id)
            keys.add(result.result_key)
            results.append(result)
    except (ValueError, TypeError):
        raise ValueError(f"results.jsonl 형식 오류 또는 중복 ({number}행)") from None
    if len(results) != manifest["results"]:
        raise ValueError("결과 수가 manifest와 다름")
    return as_of, results


def hold_trade_conflicts(db, results):
    """(results, count): new demolitions with stored contracts on or after the effective date become holds.

    The monthly run saw no such contract, so it arrived after that run's backup. Applying the demolition
    would hide the marker and leave later rechecks holding the contract. A hold is recorded rather than the
    judgment skipped, so the next monthly run judges the complex again. A judgment already applied, or one
    repeating the effective date in force (a successor link), passes unchanged.
    """
    found = {cid: (effective, count, last) for cid, effective, count, last in effective_date_conflicts(db, results)}
    if not found:
        return results, 0
    suspects = [result for result in results if result.complex_id in found]
    recorded = {row[0] for row in db.execute("""
        SELECT result_key FROM complex_lifecycle_results WHERE result_key = ANY(%s::text[])
    """, ([result.result_key for result in suspects],)).fetchall()}
    in_force = dict(db.execute("""
        SELECT complex_id, effective_date FROM complex_lifecycle_state
        WHERE existence = 'demolished' AND complex_id = ANY(%s::text[])
    """, ([result.complex_id for result in suspects],)).fetchall())
    held = {}
    for result in suspects:
        effective, count, last = found[result.complex_id]
        if result.result_key in recorded or in_force.get(result.complex_id) == effective:
            continue
        print(f"Lifecycle conflict {result.complex_id}: 효력일 {effective} 이후 거래 {count}건(마지막 {last}), "
              f"{CONFLICT_HOLD} 보류", flush=True)
        # Only facts from the bundle go into the record, so a rerun produces the same one.
        held[result.complex_id] = LifecycleResult(
            result_key=f"{result.result_key}:conflict", complex_id=result.complex_id, existence="pending",
            as_of=result.as_of, verifier_version=result.verifier_version, hold_code=CONFLICT_HOLD,
            evidence=f"basis=월간 판정 보류; note={CONFLICT_HOLD}: 효력일 {effective} 이후 계약이 있어 반영 보류. "
                     f"원래 판정: {result.evidence}")
    return [held.get(result.complex_id, result) for result in results], len(held)


def apply_bundle(db, results, today):
    """Apply one bundle district by district; judgments of complexes no longer listed are skipped."""
    summary = Counter({key: 0 for key in SUMMARY_KEYS})
    by_district = defaultdict(list)
    for result in results:
        words = result.complex_id.split(" ")
        code = DISTRICT_CODES.get(words[1]) if len(words) > 1 else None
        if code:
            by_district[code].append(result)
        else:
            summary["skipped"] += 1
    for code in sorted(by_district):
        with db.transaction():
            # The same lock as the monthly metadata stages of this district.
            db.execute("SELECT pg_advisory_xact_lock(%s)", (KAPT_LOCK_BASE + int(code),))
            allowed = {row[0] for row in db.execute("""
                SELECT complex_id FROM complexes WHERE split_part(complex_id, ' ', 2) = %s
            """, (DISTRICTS[code],)).fetchall()}
            listed = [result for result in by_district[code] if result.complex_id in allowed]
            checked, conflicts = hold_trade_conflicts(db, listed)
            outcome = apply_lifecycle_results(db, checked, allowed_ids=allowed, cutoff=today)
            if listed:
                sync_derived(db, [result.complex_id for result in listed], today)
        summary.update(outcome)
        summary["conflicts"] += conflicts
        summary["skipped"] += len(by_district[code]) - len(listed)
    return dict(summary)


def read_fingerprints(folder):
    """The fingerprint document of a monthly bundle; other bundles carry none."""
    path = folder / "fingerprints.json"
    return store.parse_fingerprints(path.read_bytes()) if path.is_file() else None


def record(db, batch, checksum, status, summary, fingerprints=None):
    """Record a bundle; an applied monthly one moves the fingerprints and closes its run in the same step."""
    with db.transaction():
        db.execute("""
            INSERT INTO complex_lifecycle_batches(batch, checksum, status, summary)
            VALUES (%s, %s, %s, %s) ON CONFLICT (batch) DO NOTHING
        """, (batch, checksum, status, Jsonb(summary)))
        if status == "applied" and fingerprints is not None:
            store.save_fingerprints(db, fingerprints, batch)
            store.mark_applied(db, batch)


def apply_inbox(root, today):
    """Apply the unrecorded bundles under root, oldest first, and return the names of those that failed.

    A failure stops the run: applying a newer bundle first would make the older judgments stale.
    """
    root = Path(root)
    # Folders starting with a dot are bundles still being published.
    folders = sorted((path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")),
                     key=lambda path: path.name) if root.is_dir() else []
    if not folders:
        return []
    # Autocommit keeps each transaction() block a real transaction instead of a savepoint.
    with psycopg.connect(autocommit=True, connect_timeout=8) as db:
        recorded = dict(db.execute("SELECT batch, checksum FROM complex_lifecycle_batches").fetchall())
        for folder in folders:
            batch = folder.name
            try:
                files = read_files(folder)
                checksum = bundle_checksum(files)
                if batch in recorded:
                    if recorded[batch] != checksum:
                        print(f"Lifecycle {batch}: 기록된 묶음과 내용이 다릅니다. 반영하지 않습니다.", flush=True)
                    continue
                try:
                    as_of, results = parse_bundle(batch, *files)
                    fingerprints = read_fingerprints(folder)
                except ValueError as error:
                    record(db, batch, checksum, "rejected", {"reason": str(error)})
                    print(f"Lifecycle {batch}: rejected ({error})", flush=True)
                    continue
                if as_of > today:
                    print(f"Lifecycle {batch}: 기준일 {as_of}까지 반영을 미룹니다.", flush=True)
                    continue
                summary = apply_bundle(db, results, today)
                record(db, batch, checksum, "applied", summary, fingerprints)
            except (OSError, psycopg.Error) as error:
                print(f"Lifecycle {batch}: deferred ({type(error).__name__})", flush=True)
                return [batch]
            print(f"Lifecycle {batch}: applied " + " ".join(f"{key}={summary[key]}" for key in SUMMARY_KEYS),
                  flush=True)
    return []
