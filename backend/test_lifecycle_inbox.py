"""Mock-based tests for the monthly lifecycle inbox (the judgment bundles of jipfit_lifecycle)."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

# Installs the psycopg stand-in when the package is missing, so it must be imported first.
from test_refresh_pipeline import check_placeholders

import lifecycle_inbox as inbox
import refresh

ROOT = Path(__file__).resolve().parent
TODAY = date(2026, 12, 29)
GURO = "서울특별시 구로구 구로동|1|구로단지"
GANGNAM = "서울특별시 강남구 개포동|2|강남단지"
GANGNAM_NEW = "서울특별시 강남구 개포동|3|새단지"
DELETED = "서울특별시 구로구 구로동|4|삭제단지"


class Result:
    def __init__(self, rows=(), rowcount=None):
        self.rows = list(rows)
        self.rowcount = len(self.rows) if rowcount is None else rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class InboxDB:
    """The tables the inbox touches, with transactions that roll back on an error."""

    def __init__(self, complexes=(GURO, GANGNAM, GANGNAM_NEW)):
        self.data = {"complexes": set(complexes), "results": {}, "state": {}, "quarantine": [], "batches": {},
                     "exclusions": {}, "status": {}, "publish": [], "trades": set(), "contracts": {}}
        self.log, self.connects, self.broken = [], [], set()

    def connect(self, **kwargs):
        self.connects.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @contextmanager
    def transaction(self, savepoint_name=None, force_rollback=False):
        saved = deepcopy(self.data)
        try:
            yield self
        except BaseException:
            self.data = saved
            raise

    def ran(self, fragment):
        return [params for query, params in self.log if fragment in query]

    def execute(self, sql, params=None):
        check_placeholders(sql, params)
        q = " ".join(sql.split())
        self.log.append((q, params))
        data = self.data
        if q.startswith("SELECT batch, checksum FROM complex_lifecycle_batches"):
            return Result([(batch, row[0]) for batch, row in data["batches"].items()])
        if q.startswith("INSERT INTO complex_lifecycle_batches"):
            batch, checksum, status, summary = params
            data["batches"].setdefault(batch, (checksum, status, summary.obj))
            return Result()
        if q.startswith("SELECT pg_advisory_xact_lock(%s)"):
            if params[0] - 2_000_000 in self.broken:
                raise inbox.psycopg.Error("connection lost")
            return Result()
        if q.startswith("SELECT pg_advisory_xact_lock(hashtextextended"):
            return Result()
        if q.startswith("SELECT v.complex_id, v.effective_date, count(*), max(t.contract_date) FROM unnest("):
            later = [(cid, day, [d for d in data["contracts"].get(cid, ()) if d >= day]) for cid, day in zip(*params)]
            return Result([(cid, day, len(found), max(found)) for cid, day, found in later if found])
        if q.startswith("SELECT result_key FROM complex_lifecycle_results WHERE result_key = ANY"):
            return Result([(key,) for key in params[0] if key in data["results"]])
        if q.startswith("SELECT complex_id, effective_date FROM complex_lifecycle_state WHERE existence"):
            return Result([(cid, s["effective_date"]) for cid, s in data["state"].items()
                           if cid in params[0] and s["existence"] == "demolished"])
        if q.startswith("SELECT complex_id FROM complexes WHERE split_part"):
            return Result([(c,) for c in sorted(data["complexes"]) if c.split(" ")[1] == params[0]])
        if q.startswith("SELECT payload_digest FROM complex_lifecycle_results"):
            found = data["results"].get(params[0])
            return Result([(found["digest"],)] if found else [])
        if q.startswith("SELECT 1 FROM complexes WHERE complex_id = %s FOR UPDATE"):
            return Result([(1,)] if params[0] in data["complexes"] else [])
        if q.startswith("SELECT result_key, existence, as_of, pending_as_of FROM complex_lifecycle_state"):
            s = data["state"].get(params[0])
            return Result([(s["result_key"], s["existence"], s["as_of"], s["pending_as_of"])] if s else [])
        if q.startswith("INSERT INTO complex_lifecycle_quarantine"):
            data["quarantine"].append(params[:4])
            return Result()
        if q.startswith("INSERT INTO complex_lifecycle_results("):
            data["results"][params[0]] = {"complex_id": params[1], "existence": params[2], "digest": params[6],
                                          "successor_id": params[9]}
            return Result()
        empty = dict.fromkeys(("result_key", "existence", "as_of", "evidence", "effective_date",
                               "pending_hold_code", "pending_as_of"))
        if q.startswith("INSERT INTO complex_lifecycle_state( complex_id, pending_result_key"):
            cid, _, code, as_of = params
            data["state"].setdefault(cid, dict(empty)).update(pending_hold_code=code, pending_as_of=as_of)
            return Result()
        if q.startswith("INSERT INTO complex_lifecycle_state( complex_id, result_key, existence"):
            cid, key, existence, as_of, _, evidence, effective, _ = params
            data["state"].setdefault(cid, dict(empty)).update(
                result_key=key, existence=existence, as_of=as_of, evidence=evidence, effective_date=effective)
            return Result()
        if q.startswith("UPDATE complexes c SET existence_status"):
            confirmed_only = "s.existence IS NOT NULL" in q
            for cid in params[0]:
                if cid in data["state"] and (data["state"][cid]["existence"] or not confirmed_only):
                    data["status"][cid] = data["state"][cid]["existence"]
            return Result(rowcount=len(params[0]))
        if q.startswith("SELECT complex_id, result_key, existence, effective_date, evidence"):
            return Result([(cid, s["result_key"], s["existence"], s["effective_date"], s["evidence"])
                           for cid, s in sorted(data["state"].items()) if cid in params[0]])
        if q.startswith("SELECT complex_id, exclude_from, reason"):
            return Result([(cid, *row) for cid, row in data["exclusions"].items() if cid in params[0]])
        if q.startswith("INSERT INTO complex_training_exclusions"):
            data["exclusions"][params[0]] = (params[1], params[2])
            return Result()
        if q.startswith("DELETE FROM complex_training_exclusions"):
            data["exclusions"].pop(params[0], None)
            return Result()
        if q.startswith("SELECT 1 FROM trades"):
            return Result([(1,)] if params[0] in data["trades"] else [])
        if q.startswith("INSERT INTO refresh_publish_pending"):
            data["publish"].append(params)
            return Result()
        if q.startswith("SELECT count(*) FROM complex_training_exclusions"):
            return Result([(sum(cid in params[0] for cid in data["exclusions"]),)])
        raise AssertionError(f"unexpected query: {q}")


def judgment(cid, existence="present", day="2026-12-28", **extra):
    return {"result_key": f"monthly-{day.replace('-', '')}:{cid}", "complex_id": cid, "existence": existence,
            "as_of": day, "verifier_version": "v", "evidence": "basis=t; note=n", **extra}


class InboxCase(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = InboxDB()
        for target in (patch.object(inbox.psycopg, "connect", self.db.connect), patch("builtins.print")):
            started = target.start()
            self.addCleanup(target.stop)
        self.output = started

    def bundle(self, rows, day="2026-12-28", batch=None, **manifest):
        batch = batch or "monthly-" + day.replace("-", "")
        folder = self.root / batch
        folder.mkdir(exist_ok=True)
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        (folder / "results.jsonl").write_bytes(body)
        document = {"batch": batch, "as_of": day, "verifier_version": "v", "results": len(rows),
                    "sha256": sha256(body).hexdigest(), "counts": {}, **manifest}
        (folder / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
        return folder

    def apply(self, today=TODAY):
        return inbox.apply_inbox(self.root, today)

    def printed(self):
        return "\n".join(str(call.args[0]) for call in self.output.call_args_list)

    def recorded(self):
        return {batch: row[1] for batch, row in self.db.data["batches"].items()}


class ApplyInboxTests(InboxCase):
    def test_a_missing_or_empty_inbox_runs_no_query(self):
        self.assertEqual(inbox.apply_inbox(self.root / "none", TODAY), [])
        (self.root / ".monthly-20261228.tmp").mkdir()  # Still being published.
        (self.root / "note.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.apply(), [])
        self.assertEqual((self.db.connects, self.db.log), ([], []))

    def test_a_bundle_is_applied_by_district_and_recorded_once(self):
        self.bundle([judgment(GURO), judgment(GANGNAM, "demolished", effective_date="2026-12-01",
                                              effective_precision="day"),
                     judgment(GANGNAM_NEW, "pending", hold_code="범위미확인")])
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.db.connects, [{"autocommit": True, "connect_timeout": 8}])
        self.assertEqual([p[0] for p in self.db.ran("pg_advisory_xact_lock(%s)")], [2_011_530, 2_011_680])
        # The complex that is only held keeps whatever status it had.
        self.assertEqual(self.db.data["status"], {GURO: "present", GANGNAM: "demolished"})
        self.assertEqual(self.db.data["batches"]["monthly-20261228"][1:],
                         ("applied", {"applied": 2, "noop": 0, "pending": 1, "quarantined": 0, "skipped": 0,
                                      "conflicts": 0}))
        self.assertIn("applied=2 noop=0 pending=1 quarantined=0 skipped=0 conflicts=0", self.printed())
        self.db.log.clear()
        self.assertEqual(self.apply(), [])
        self.assertEqual([q for q, _ in self.db.log],
                         ["SELECT batch, checksum FROM complex_lifecycle_batches"])

    def test_the_same_name_with_other_content_only_warns(self):
        self.bundle([judgment(GURO)])
        self.apply()
        self.bundle([judgment(GURO, "demolished")])
        before = deepcopy(self.db.data)
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.db.data, before)
        self.assertIn("내용이 다릅니다", self.printed())

    def test_malformed_bundles_are_rejected_and_later_ones_applied(self):
        valid = judgment(GURO)
        self.bundle([valid], batch="monthly-20261201", sha256="0" * 64)
        self.bundle([valid], batch="monthly-20261202", as_of="soon")
        self.bundle([valid], batch="monthly-20261203", results=2)
        self.bundle([valid], batch="monthly-20261204", counts=None)
        self.bundle([valid], batch="monthly-20261205", other_name=1)
        self.bundle([valid, valid], batch="monthly-20261206")
        self.bundle([{**valid, "unknown": 1}], batch="monthly-20261207")
        self.bundle([{**valid, "complex_id": 3}], batch="monthly-20261208")
        (self.root / "monthly-20261209").mkdir()
        renamed = self.bundle([valid], batch="monthly-20261210")
        renamed.rename(self.root / "monthly-20261211")
        broken = self.bundle([valid], batch="monthly-20261212")
        (broken / "manifest.json").write_text("{", encoding="utf-8")
        self.bundle([judgment(GANGNAM)])
        self.assertEqual(self.apply(), [])
        status = self.recorded()
        # An extra manifest field is allowed; every other bundle above is malformed.
        self.assertEqual(status.pop("monthly-20261205"), "applied")
        self.assertEqual(status.pop("monthly-20261228"), "applied")
        self.assertEqual((set(status.values()), len(status)), ({"rejected"}, 10))
        self.assertEqual(set(self.db.data["state"]), {GURO, GANGNAM})
        self.db.log.clear()
        self.apply()
        self.assertEqual(len(self.db.log), 1)

    def test_a_future_as_of_waits_without_a_record(self):
        self.bundle([judgment(GURO)])
        self.assertEqual(self.apply(date(2026, 12, 27)), [])
        self.assertEqual((self.recorded(), self.db.data["state"]), ({}, {}))
        self.assertEqual(self.apply(date(2026, 12, 28)), [])
        self.assertEqual(self.recorded(), {"monthly-20261228": "applied"})

    def test_results_of_removed_complexes_are_skipped_and_counted(self):
        self.bundle([judgment(GURO), judgment(DELETED), judgment("서울특별시 없는구 동|1|x"),
                     judgment("서울특별시 종로구 동|1|x")])
        self.assertEqual(self.apply(), [])
        summary = self.db.data["batches"]["monthly-20261228"][2]
        self.assertEqual((summary["applied"], summary["skipped"], summary["quarantined"]), (1, 3, 0))
        self.assertEqual(self.db.data["quarantine"], [])

    def test_an_empty_bundle_is_recorded(self):
        self.bundle([])
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.recorded(), {"monthly-20261228": "applied"})

    def test_a_district_error_leaves_the_bundle_for_a_rerun(self):
        self.bundle([judgment(GURO), judgment(GANGNAM)])
        self.bundle([judgment(GURO, day="2026-12-29")], day="2026-12-29")
        self.db.broken = {11680}
        self.assertEqual(self.apply(), ["monthly-20261228"])
        # The first district stays applied, nothing is recorded and the newer bundle waits.
        self.assertEqual((self.recorded(), set(self.db.data["state"])), ({}, {GURO}))
        self.assertEqual(self.db.data["state"][GURO]["result_key"], f"monthly-20261228:{GURO}")
        self.db.broken = set()
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.db.data["batches"]["monthly-20261228"][2]["noop"], 1)
        self.assertEqual(self.db.data["batches"]["monthly-20261228"][2]["applied"], 1)
        self.assertEqual(self.db.data["state"][GURO]["result_key"], f"monthly-20261229:{GURO}")
        self.assertEqual(self.db.data["quarantine"], [])

    def test_a_failure_while_applying_rolls_the_district_back(self):
        self.bundle([judgment(GURO)])
        with patch.object(inbox, "sync_derived", side_effect=inbox.psycopg.Error("x")):
            self.assertEqual(self.apply(), ["monthly-20261228"])
        self.assertEqual((self.db.data["state"], self.db.data["results"], self.recorded()), ({}, {}, {}))


class CarriedJudgmentTests(InboxCase):
    """A successor link repeats the effective date and caution date, so neither is lost (PLAN_MONTHLY 4-5)."""

    def confirmed(self, cid, **values):
        self.db.data["state"][cid] = {"result_key": "baseline", "existence": "demolished",
                                      "as_of": date(2026, 11, 26), "evidence": "", "effective_date": None,
                                      "pending_hold_code": None, "pending_as_of": None, **values}

    def test_the_effective_date_and_training_exclusion_survive_a_successor_link(self):
        caution = "basis=수동 확인표; trade_caution_from=2026-05-01; note=n"
        self.confirmed(GURO, evidence=caution)
        self.db.data["exclusions"][GURO] = (date(2026, 5, 1), "lifecycle:baseline")
        self.db.data["trades"] = {GURO}
        self.confirmed(GANGNAM, effective_date=date(2020, 4, 20))
        link = {"successor_id": GANGNAM_NEW, "successor_status": "linked"}
        carried = ("basis=수동 확인표; trade_caution_from=2026-05-01; successor_name=새단지; "
                   "note=후속 연결(같은 지번): x; 이전 근거: n")
        self.bundle([judgment(GURO, "demolished", evidence=carried, **link),
                     judgment(GANGNAM, "demolished", effective_date="2020-04-20", effective_precision="day", **link)])
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.db.data["exclusions"][GURO],
                         (date(2026, 5, 1), f"lifecycle:monthly-20261228:{GURO}"))
        self.assertEqual(self.db.data["state"][GANGNAM]["effective_date"], date(2020, 4, 20))
        self.assertEqual(self.db.data["publish"], [])  # Unchanged exclusions queue no forecast rebuild.
        self.assertEqual({r["successor_id"] for r in self.db.data["results"].values()}, {GANGNAM_NEW})


class TradeConflictTests(InboxCase):
    """A contract stored on or after a new effective date turns the demolition into a hold."""

    def demolition(self, cid, effective="2026-12-01", **extra):
        return judgment(cid, "demolished", effective_date=effective, effective_precision="day", **extra)

    def state(self, cid, existence="present", effective=None):
        self.db.data["state"][cid] = {"result_key": "baseline", "existence": existence, "as_of": date(2026, 11, 26),
                                      "evidence": "basis=t; note=n", "effective_date": effective,
                                      "pending_hold_code": None, "pending_as_of": None}

    def summary(self):
        return self.db.data["batches"]["monthly-20261228"][2]

    def test_a_new_demolition_with_a_later_contract_is_held(self):
        self.state(GURO)
        self.state(GANGNAM)
        self.db.data["contracts"] = {GURO: [date(2026, 12, 1)], GANGNAM: [date(2026, 11, 30)]}
        self.bundle([self.demolition(GURO), self.demolition(GANGNAM)])
        self.assertEqual(self.apply(), [])
        held = self.db.data["state"][GURO]
        self.assertEqual((held["existence"], held["result_key"], held["pending_hold_code"], held["pending_as_of"]),
                         ("present", "baseline", "말소후거래", date(2026, 12, 28)))
        self.assertEqual(self.db.data["status"], {GURO: "present", GANGNAM: "demolished"})
        self.assertEqual(sorted(self.db.data["results"]),
                         sorted([f"monthly-20261228:{GURO}:conflict", f"monthly-20261228:{GANGNAM}"]))
        self.assertEqual((self.summary()["applied"], self.summary()["pending"], self.summary()["conflicts"]),
                         (1, 1, 1))
        self.assertEqual(self.db.data["quarantine"], [])
        self.assertIn(f"{GURO}: 효력일 2026-12-01 이후 거래 1건(마지막 2026-12-01), 말소후거래 보류", self.printed())

    def test_a_rerun_keeps_an_already_applied_demolition(self):
        self.bundle([self.demolition(GURO), self.demolition(GANGNAM)])
        self.db.broken = {11680}
        self.assertEqual(self.apply(), ["monthly-20261228"])
        # A contract arrives before the retry; the first district is already applied.
        self.db.broken = set()
        self.db.data["contracts"] = {GURO: [date(2026, 12, 2)]}
        self.assertEqual(self.apply(), [])
        self.assertEqual((self.summary()["noop"], self.summary()["applied"], self.summary()["conflicts"]), (1, 1, 0))
        self.assertIsNone(self.db.data["state"][GURO]["pending_hold_code"])

    def test_a_held_conflict_repeats_as_the_same_record(self):
        self.db.data["contracts"] = {GURO: [date(2026, 12, 2)]}
        self.bundle([self.demolition(GURO), self.demolition(GANGNAM)])
        self.db.broken = {11680}
        self.apply()
        self.db.broken = set()
        self.db.data["contracts"][GURO].append(date(2026, 12, 3))
        self.assertEqual(self.apply(), [])
        self.assertEqual((self.summary()["noop"], self.summary()["conflicts"], self.summary()["quarantined"]),
                         (1, 1, 0))
        self.assertIsNone(self.db.data["state"][GURO]["existence"])

    def test_only_a_changed_effective_date_is_checked_on_a_confirmed_demolition(self):
        for cid in (GURO, GANGNAM):
            self.state(cid, "demolished", date(2020, 4, 20))
            self.db.data["contracts"][cid] = [date(2020, 5, 1)]
        self.bundle([self.demolition(GURO, "2020-04-20", successor_id=GANGNAM_NEW, successor_status="linked"),
                     self.demolition(GANGNAM, "2020-03-01")])
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.db.data["state"][GURO]["result_key"], f"monthly-20261228:{GURO}")
        self.assertEqual((self.db.data["state"][GANGNAM]["result_key"],
                          self.db.data["state"][GANGNAM]["pending_hold_code"]), ("baseline", "말소후거래"))
        self.assertEqual(self.summary()["conflicts"], 1)


class DailyWorkerTests(unittest.TestCase):
    def test_nothing_happens_without_the_environment_variable(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(refresh, "apply_inbox") as apply:
            self.assertEqual(refresh.apply_lifecycle_inbox(TODAY), [])
        apply.assert_not_called()

    def test_failed_bundles_and_errors_join_the_failed_list(self):
        with patch.dict(os.environ, {"JIPFIT_LIFECYCLE_DIR": "/lifecycle"}), patch("builtins.print"):
            with patch.object(refresh, "apply_inbox", return_value=["monthly-20261228"]) as apply:
                self.assertEqual(refresh.apply_lifecycle_inbox(TODAY), ["lifecycle:monthly-20261228"])
            apply.assert_called_once_with("/lifecycle", TODAY)
            with patch.object(refresh, "apply_inbox", side_effect=RuntimeError("x")):
                self.assertEqual(refresh.apply_lifecycle_inbox(TODAY), ["lifecycle-inbox:RuntimeError"])

    def order(self, calls):
        def step(name, value=()):
            def run(*args, **kwargs):
                calls.append(name)
                return list(value)
            return run

        return (patch.object(refresh, "ensure_schema"), patch.object(refresh, "refresh_many", step("trades")),
                patch.object(refresh, "refresh_automatic_metadata", step("metadata")),
                patch.object(refresh, "apply_metadata", step("verified")),
                patch.object(refresh, "seed_facilities", step("seed")),
                patch.object(refresh, "refresh_facility_sources", step("facilities")),
                patch.object(refresh, "apply_lifecycle_inbox", step("inbox", ["lifecycle:b"])),
                patch.object(refresh, "sync_facilities", step("sync", ["facilities:sync"])))

    def test_a_single_run_applies_the_inbox_after_the_updates(self):
        calls = []
        patches = self.order(calls)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with self.assertRaises(SystemExit) as raised:
            refresh.run_once(["11530"], metadata_dir=Path("m"))
        self.assertEqual(calls, ["seed", "trades", "metadata", "verified", "facilities", "inbox", "sync"])
        self.assertIn("lifecycle:b, facilities:sync", str(raised.exception))

    def test_the_daily_job_applies_the_inbox_and_the_monthly_job_does_not(self):
        calls, jobs = [], {}

        class Stop(Exception):
            pass

        class Thread:
            def __init__(self, target, name):
                jobs[name] = target

            def start(self):
                pass

            def join(self):
                pass

        def run_slot(worker, job, **kwargs):
            calls.append(job())
            raise Stop

        for item in (*self.order(calls), patch.object(refresh, "Thread", Thread),
                     patch.object(refresh, "run_slot", run_slot), patch("builtins.print"),
                     patch("sys.argv", ["refresh.py", "--metadata-dir", "m"])):
            item.start()
            self.addCleanup(item.stop)
        refresh.main()
        with self.assertRaises(Stop):
            jobs["daily-refresh"]()
        self.assertEqual(calls, ["seed", "trades", "metadata", "verified", "inbox", "sync",
                                 ["lifecycle:b", "facilities:sync"]])
        calls.clear()
        with self.assertRaises(Stop):
            jobs["metadata-refresh"]()
        self.assertIn("facilities", calls)
        self.assertNotIn("inbox", calls)
        self.assertNotIn("sync", calls)


class PackagingTests(unittest.TestCase):
    def test_the_table_is_idempotent_and_the_updater_reads_the_bundles(self):
        sql = (ROOT / "db/metadata_refresh.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS complex_lifecycle_batches (", sql)
        self.assertIn("lifecycle_inbox.py", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
        compose = (ROOT.parent / "compose.yaml").read_text(encoding="utf-8")
        updater = compose.split("\n  updater:")[1]
        self.assertIn("JIPFIT_LIFECYCLE_DIR: /lifecycle-data/batches", updater)
        # No folder outside Jipfit feeds the updater any more.
        self.assertNotIn("lifecycle-inbox", compose)
        ignored = (ROOT.parent / ".gitignore").read_text(encoding="utf-8").split()
        self.assertEqual(("/lifecycle-data/" in ignored, "/lifecycle-inbox/" in ignored), (True, False))

    def test_the_monthly_provider_stays_empty(self):
        source = (ROOT / "refresh.py").read_text(encoding="utf-8")
        # The inbox is the only path for the verifier's judgments; no other provider is connected.
        self.assertEqual(set(re.findall(r"lifecycle_provider=(\w+)", source)),
                         {"empty_lifecycle_provider", "lifecycle_provider"})


if __name__ == "__main__":
    unittest.main()
