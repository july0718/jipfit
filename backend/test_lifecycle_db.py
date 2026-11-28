"""PostgreSQL integration test of the existence baseline steps. Skipped unless JIPFIT_TEST_DSN is set.

JIPFIT_TEST_DSN points at a disposable test server, e.g. "host=127.0.0.1 port=55987 user=postgres
dbname=postgres". The test creates its own database, works only inside it and drops it afterwards.
"""
from collections import Counter
from datetime import date
from decimal import Decimal
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
import uuid

DSN = os.environ.get("JIPFIT_TEST_DSN")
ROOT = Path(__file__).resolve().parent
TODAY = date(2026, 11, 26)
GONE = "서울특별시 구로구 구로동|1|옛단지"
DATED = "서울특별시 구로구 구로동|2|말소단지"
STAYS = "서울특별시 구로구 구로동|3|현존단지"
VILLA = "서울특별시 구로구 구로동|4|빌라"


def backup_rows(dump, table):
    """One table of a pg_dump backup, read as jipfit_lifecycle@d86dcdf did (dump_rows and copy_value)."""
    escapes = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}

    def value(field):
        if field == "\\N":
            return None
        return re.sub(r"\\(.)", lambda found: escapes.get(found[1], found[1]), field)

    text = subprocess.run(["pg_restore", "--data-only", "--table", table, "--file", "-", str(dump)], check=True,
                          capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(f"COPY public.{table} ("))
    columns = lines[start].split("(", 1)[1].split(") FROM stdin;", 1)[0].split(", ")
    rows = lines[start + 1:lines.index("\\.", start)]
    return [dict(zip(columns, map(value, row.split("\t")), strict=True)) for row in rows]


def real_psycopg():
    try:
        import psycopg
    except ModuleNotFoundError:
        return None
    return psycopg if getattr(psycopg, "__file__", None) else None  # not the unit tests' stand-in


@unittest.skipUnless(DSN and real_psycopg(), "set JIPFIT_TEST_DSN (and install psycopg) to run")
class LifecycleDatabaseTests(unittest.TestCase):
    def setUp(self):
        import refresh

        psycopg = real_psycopg()
        self.name = "jipfit_test_" + uuid.uuid4().hex[:12]
        admin = psycopg.connect(DSN, autocommit=True)
        admin.execute(f"CREATE DATABASE {self.name} TEMPLATE template0 ENCODING 'UTF8'")
        self.addCleanup(admin.close)
        self.addCleanup(admin.execute, f"DROP DATABASE {self.name} WITH (FORCE)")
        conninfo = self.conninfo = f"{DSN} dbname={self.name}"
        self.db = psycopg.connect(conninfo, autocommit=True)
        self.addCleanup(self.db.close)
        schema = (ROOT / "db/init.sql").read_text(encoding="utf-8").split("INSERT INTO")[0]
        self.db.execute(schema + "COMMIT;")
        self.db.execute("""ALTER TABLE model_inputs ALTER COLUMN momentum6 DROP NOT NULL,
            ALTER COLUMN momentum12 DROP NOT NULL, ALTER COLUMN volatility DROP NOT NULL""")
        connect = psycopg.connect
        with patch.object(refresh.psycopg, "connect", lambda **kw: connect(conninfo, **kw)):
            for _ in range(2):  # The schema step is idempotent.
                refresh._schema_ready = False
                refresh.ensure_schema()
        refresh._schema_ready = False
        for number, cid in enumerate((GONE, DATED, STAYS, VILLA), start=1):
            self.db.execute("INSERT INTO complexes(complex_id,name,address,built,latitude,longitude) "
                            "VALUES (%s,%s,%s,1990,37.5,126.9)",
                            (cid, cid.split("|")[-1], f"서울특별시 구로구 구로동 {number}"))
            self.db.execute("INSERT INTO area_groups VALUES (%s,60,59.5)", (cid,))
            for month in (3, 4, 5, 6):
                self.trade(cid, date(2026, month, 10))
        self.trade(GONE, date(2019, 1, 5))

    def trade(self, cid, day):
        self.db.execute("INSERT INTO trades(complex_id,area_group,contract_date,area,floor,price_manwon) "
                        "VALUES (%s,60,%s,59.5,3,30000)", (cid, day))

    def value(self, sql, params=None):
        return self.db.execute(sql, params).fetchone()[0]

    def results(self):
        from lifecycle_results import LifecycleResult

        def make(cid, existence, **extra):
            return LifecycleResult(**{
                "result_key": f"baseline-20261126:{cid}", "complex_id": cid, "existence": existence,
                "evidence": "basis=t; note=n", "as_of": "2026-11-26", "verifier_version": "v", **extra})

        return [
            make(GONE, "demolished", evidence="basis=t; trade_caution_from=2026-05-01; note=n"),
            make(DATED, "demolished", effective_date="2026-07-01", effective_precision="day"),
            make(STAYS, "present"),
        ]

    def test_apply_correct_and_exclude_inputs(self):
        import apply_lifecycle_baseline as apply
        import refresh

        with patch("builtins.print"):
            apply.run_apply(self.db, self.results(), TODAY, dry=True)
            self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_results"), 0)
            first = apply.run_apply(self.db, self.results(), TODAY, dry=False)
            second = apply.run_apply(self.db, self.results(), TODAY, dry=False)
        self.assertEqual((first["applied"], first["quarantined"], second["noop"]), (3, 0, 3))
        statuses = dict(self.db.execute("SELECT complex_id, existence_status FROM complexes").fetchall())
        self.assertEqual([statuses[c] for c in (GONE, DATED, STAYS, VILLA)], ["멸실", "멸실", "현존", None])
        self.assertEqual(apply.marker_ids(self.db), {STAYS, VILLA})
        self.assertEqual(self.db.execute("SELECT complex_id, exclude_from FROM complex_training_exclusions").fetchall(),
                         [(GONE, date(2026, 5, 1))])
        # The excluded contracts fall inside the input window, so a rebuild is queued and they leave the inputs.
        self.assertEqual(self.value("SELECT count(*) FROM refresh_publish_pending WHERE district='11530'"), 1)
        refresh.publish(self.db, __import__("pandas").Period("2026-11", freq="M"), ROOT / "macro_raw.csv", "11530")
        inputs = {row[0] for row in self.db.execute("SELECT complex_id FROM model_inputs").fetchall()}
        self.assertEqual(inputs, {DATED, STAYS, VILLA})
        self.assertEqual(self.value("SELECT count(*) FROM trades WHERE complex_id=%s", (GONE,)), 5)
        if importlib.util.find_spec("sklearn"):
            import train_seoul
            with patch("builtins.print"):
                frame = train_seoul.read_trades(self.db, "11530", "구로구", __import__("pandas").Period("2026-11", freq="M"))
            self.assertEqual(int((frame.complex_id == GONE).sum()), 3)

        with patch("builtins.print"):
            apply.correct(self.db, GONE, "present", "현장 확인", TODAY, dry=False)
        self.assertEqual(self.value("SELECT existence FROM complex_lifecycle_state WHERE complex_id=%s", (GONE,)),
                         "present")
        self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_results WHERE complex_id=%s", (GONE,)), 2)
        self.assertEqual(self.value("SELECT count(*) FROM complex_training_exclusions"), 0)
        self.assertEqual(self.value("SELECT existence_status FROM complexes WHERE complex_id=%s", (GONE,)), "현존")

    def publish_bundle(self, root, day, results):
        batch = "monthly-" + day.replace("-", "")
        folder = root / batch
        folder.mkdir()
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results).encode("utf-8")
        (folder / "results.jsonl").write_bytes(body)
        (folder / "manifest.json").write_text(json.dumps(
            {"batch": batch, "as_of": day, "verifier_version": "v", "results": len(results),
             "sha256": sha256(body).hexdigest(), "counts": {}}), encoding="utf-8")

    def test_inbox_applies_monthly_bundles_and_keeps_derived_values(self):
        import apply_lifecycle_baseline as apply
        import lifecycle_inbox

        def monthly(cid, existence, **extra):
            return {"result_key": f"monthly-20261228:{cid}", "complex_id": cid, "existence": existence,
                    "evidence": "basis=t; note=n", "as_of": "2026-12-28", "verifier_version": "v", **extra}

        def exclusion():
            return self.db.execute("SELECT complex_id, exclude_from FROM complex_training_exclusions").fetchall()

        with patch("builtins.print"):
            apply.run_apply(self.db, self.results(), TODAY, dry=False)
        self.db.execute("DELETE FROM refresh_publish_pending")
        connect = lifecycle_inbox.psycopg.connect
        link = {"successor_id": STAYS, "successor_status": "linked"}
        with TemporaryDirectory() as temp, patch("builtins.print"), patch.object(
                lifecycle_inbox.psycopg, "connect", lambda **kw: connect(self.conninfo, **kw)):
            root = Path(temp)
            self.publish_bundle(root, "2026-12-28", [
                # Successor links repeat the effective date and the caution date of the current judgment.
                monthly(GONE, "demolished", evidence="basis=t; trade_caution_from=2026-05-01; note=linked", **link),
                monthly(DATED, "demolished", effective_date="2026-07-01", effective_precision="day", **link),
                monthly(VILLA, "pending", hold_code="범위미확인"),
                monthly("서울특별시 구로구 구로동|9|삭제된단지", "present")])
            (root / "monthly-20261227").mkdir()
            self.assertEqual(lifecycle_inbox.apply_inbox(root, date(2026, 12, 29)), [])
            self.assertEqual(lifecycle_inbox.apply_inbox(root, date(2026, 12, 29)), [])
        batches = {row[0]: row[1:] for row in self.db.execute(
            "SELECT batch, status, summary FROM complex_lifecycle_batches").fetchall()}
        self.assertEqual(batches["monthly-20261227"][0], "rejected")
        self.assertEqual(batches["monthly-20261228"], (
            "applied", {"applied": 2, "noop": 0, "pending": 1, "quarantined": 0, "skipped": 1, "conflicts": 0}))
        self.assertEqual(exclusion(), [(GONE, date(2026, 5, 1))])
        self.assertEqual(self.value("SELECT effective_date FROM complex_lifecycle_state WHERE complex_id=%s",
                                    (DATED,)), date(2026, 7, 1))
        self.assertEqual(self.value("SELECT count(*) FROM refresh_publish_pending"), 0)
        self.assertEqual(self.value("SELECT pending_hold_code FROM complex_lifecycle_state WHERE complex_id=%s",
                                    (VILLA,)), "범위미확인")
        self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_results WHERE successor_id=%s",
                                    (STAYS,)), 2)
        self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_quarantine"), 0)

        # STAYS has a contract on 2026-06-10, after the new effective date, so the demolition is held.
        late = {**monthly(STAYS, "demolished", effective_date="2026-06-01", effective_precision="day"),
                "result_key": f"monthly-20261229:{STAYS}", "as_of": "2026-12-29"}
        with TemporaryDirectory() as temp, patch("builtins.print"), patch.object(
                lifecycle_inbox.psycopg, "connect", lambda **kw: connect(self.conninfo, **kw)):
            self.publish_bundle(Path(temp), "2026-12-29", [late])
            self.assertEqual(lifecycle_inbox.apply_inbox(temp, date(2026, 12, 29)), [])
        self.assertEqual(self.db.execute(
            "SELECT existence, pending_hold_code, pending_result_key FROM complex_lifecycle_state "
            "WHERE complex_id=%s", (STAYS,)).fetchone(),
            ("present", "말소후거래", f"monthly-20261229:{STAYS}:conflict"))
        self.assertEqual(self.value("SELECT existence_status FROM complexes WHERE complex_id=%s", (STAYS,)), "현존")
        self.assertEqual(self.value(
            "SELECT summary->>'conflicts' FROM complex_lifecycle_batches WHERE batch='monthly-20261229'"), "1")
        self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_quarantine"), 0)

    def public_revision(self):
        """The digest GET /api/metadata-revision returns, from the query in main.py (fastapi is not needed)."""
        source = (ROOT / "main.py").read_text(encoding="utf-8").split("def metadata_revision():")[1]
        query = source.split('db.execute("""')[1].split('""")')[0]
        return self.db.execute(query).fetchone()[0]

    def public_state(self):
        import apply_lifecycle_baseline as apply

        return (self.public_revision(), apply.marker_ids(self.db), self.db.execute(
            "SELECT complex_id, existence, result_key, as_of FROM complex_lifecycle_state WHERE existence IS NOT NULL "
            "ORDER BY complex_id").fetchall(), self.db.execute(
            "SELECT complex_id, existence_status FROM complexes ORDER BY complex_id").fetchall())

    def test_the_monthly_check_runs_on_postgresql(self):
        import apply_lifecycle_baseline as apply
        import lifecycle_admin as admin
        import lifecycle_check
        from lifecycle_check import monthly, sources, store

        def key(lot):
            return sources.parcel_params(f"11530_10200_0_{lot:04}_0000")

        def put(cache, service, lot, rows, day):
            params = lot if isinstance(lot, dict) else key(lot)
            path = sources.cache_file(cache, service, params)
            path.parent.mkdir(parents=True, exist_ok=True)
            if service in ("current", "closed", "demolition"):
                rows = [{**params, **row} for row in rows]
            path.write_text(json.dumps({"retrieved_on": day.isoformat(), "total": len(rows), "rows": rows}),
                            encoding="utf-8")

        home = {"mainAtchGbCd": "0", "mainPurpsCdNm": "공동주택", "bldNm": "현존단지", "dongNm": "가동", "hhldCnt": "12",
                "useAprDay": "19900101", "mgmBldrgstPk": "C1"}
        old = {**home, "mgmBldrgstPk": "", "mgmShtregPk": "S1", "shterGbCd": "2", "shterDay": "20261201"}
        removal = {"mgmPmsrgstPk": "D1", "mainPurpsCdNm": "공동주택", "demolEndDay": "20261201"}
        answers, clock = {}, [date(2026, 12, 1)]

        def fetch(cache, _keys, service, params, *, refresh=False, ask=None, parse=None):
            put(cache, service, params, answers.get((service, int(params["bun"])), []), clock[0])
            path = sources.cache_file(cache, service, params)
            sources.note_write(path, path.read_text(encoding="utf-8"))  # As the real fetch does.

        with patch("builtins.print"):
            apply.run_apply(self.db, self.results(), TODAY, dry=False)
        connect = store.psycopg.connect

        def isolated(*args, **kwargs):
            return connect(self.conninfo, **kwargs)

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)
        reference = data / "reference"
        put(reference, "trade_detail", {"LAWD_CD": "11530", "DEAL_YMD": "202603"},
            [{"sggCd": "11530", "umdNm": "구로동", "umdCd": "10200"}], TODAY)
        for service, rows in (("current", [home]), ("closed", []), ("demolition", [])):
            put(reference, service, 3, rows, TODAY)

        def run(day, **options):
            clock[0] = day
            return lifecycle_check.run_month(day, workers=1, **options)

        def state(cid):
            return self.db.execute("SELECT existence, pending_hold_code, result_key FROM complex_lifecycle_state "
                                   "WHERE complex_id=%s", (cid,)).fetchone()

        environment = {"JIPFIT_LIFECYCLE_DATA": str(data), "MOLIT_SERVICE_KEY": "k",
                       "JIPFIT_LIFECYCLE_DIR": str(data / "batches"), "JIPFIT_LIFECYCLE_MAX_AUTO": "20"}
        with patch("builtins.print"), patch.dict(os.environ, environment), \
                patch.object(store.psycopg, "connect", isolated), patch.object(sources, "fetch", fetch), \
                patch.object(sources, "today", lambda: clock[0]):
            admin.seed_fingerprints(self.db, reference, TODAY)
            self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_fingerprints"), 1)

            # Another process holds the lock.
            self.assertTrue(store.try_lock(self.db))
            self.assertEqual(run(date(2026, 12, 1)), ["lifecycle:locked"])
            self.db.execute("SELECT pg_advisory_unlock(%s)", (store.LOCK_KEY,))

            # A month of holds only: VILLA is new without records, STAYS has a new demolition record.
            # The operating database gives new complexes this value, which a hold must not touch.
            self.db.execute("UPDATE complexes SET existence_status='unconfirmed' WHERE complex_id=%s", (VILLA,))
            answers.update({("current", 3): [home], ("demolition", 3): [removal]})
            before = self.public_state()
            self.assertEqual(run(date(2026, 12, 1), dry_run=True), [])
            self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_runs"), 0)
            self.assertEqual(run(date(2026, 12, 1)), [])
            self.assertEqual(self.public_state(), before)
            self.assertEqual((state(VILLA)[:2], state(STAYS)[:2]), ((None, "기록없음"), ("present", "일부존치")))
            self.assertEqual(self.db.execute(
                "SELECT month, status, attempts, batch, as_of, remaining_calls, summary->>'보류' "
                "FROM complex_lifecycle_runs").fetchone(),
                ("2026-12", "applied", 1, "monthly-20261201", date(2026, 12, 1), 0, "2"))
            seeded = self.db.execute("SELECT batch, as_of FROM complex_lifecycle_fingerprints "
                                     "WHERE parcel_key='11530_10200_0_0003_0000'").fetchone()
            self.assertEqual(seeded, ("monthly-20261201", date(2026, 12, 1)))
            count = self.value("SELECT count(*) FROM complex_lifecycle_results")
            self.assertEqual(run(date(2026, 12, 2)), [])
            self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_results"), count)

            # The mass guard holds the confirmation and accept applies it.
            answers.update({("current", 3): [], ("closed", 3): [old]})
            with patch.dict(os.environ, {"JIPFIT_LIFECYCLE_MAX_AUTO": "0"}):
                self.assertEqual(run(date(2027, 1, 1)), [])
            self.assertEqual(state(STAYS)[:2], ("present", "대량확정보류"))
            self.assertEqual({r["complex_id"] for r in admin.holds(self.db, date(2027, 1, 2))}, {STAYS, VILLA})
            unchanged = self.public_state()
            admin.accept(self.db, "monthly-20270101", None, date(2027, 1, 2), dry=True)
            self.assertEqual(self.public_state(), unchanged)
            summary = admin.accept(self.db, "monthly-20270101", [STAYS], date(2027, 1, 2))
            self.assertEqual((summary["applied"], summary["quarantined"]), (1, 0))
            self.assertEqual(state(STAYS)[0::2], ("demolished", f"accept-20270102:{STAYS}"))
            self.assertNotIn(STAYS, apply.marker_ids(self.db))

            # An operator decision, then the fingerprint keeps it through the next month.
            admin.resolve(self.db, STAYS, "present", "현장 확인", date(2027, 1, 3), dry=True)
            self.assertEqual(state(STAYS)[0], "demolished")
            admin.resolve(self.db, STAYS, "present", "현장 확인", date(2027, 1, 3))
            admin.resolve(self.db, VILLA, "present", "현장 확인", date(2027, 1, 3))
            self.assertEqual(admin.holds(self.db, date(2027, 1, 3), show_all=True), [])
            with self.assertRaises(SystemExit):  # Stored contracts follow this date.
                admin.resolve(self.db, VILLA, "demolished", "x", date(2027, 1, 3), effective_date=date(2026, 5, 1))
            self.assertEqual(run(date(2027, 2, 1)), [])
            self.assertEqual((state(STAYS)[0], state(VILLA)[0]), ("present", "present"))
            self.assertEqual((data / "batches/monthly-20270201/results.jsonl").read_bytes(), b"")

            # A new cancellation record reopens it; revert restores the judgment before the batch.
            answers[("closed", 3)] = [old, {**old, "mgmShtregPk": "S2", "dongNm": "나동"}]
            put(data / "unused", "current", 3, [], clock[0])
            answers[("closed_outline", 3)] = [{"shterGbCd": "2", "hhldCnt": "24", "mainBldCnt": "2"}]
            self.assertEqual(run(date(2027, 3, 1)), [])
            self.assertEqual(state(STAYS)[0::2], ("demolished", f"monthly-20270301:{STAYS}"))
            before_revert = self.public_state()
            admin.revert(self.db, "monthly-20270301", None, date(2027, 3, 2), dry=True)
            self.assertEqual(self.public_state(), before_revert)
            outcome = admin.revert(self.db, "monthly-20270301", None, date(2027, 3, 2))
            self.assertEqual(outcome["되돌림"], 1)
            self.assertEqual(state(STAYS)[0], "present")
            self.assertEqual(self.value("SELECT existence_status FROM complexes WHERE complex_id=%s", (STAYS,)), "현존")
        self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_quarantine"), 0)
        self.assertEqual(self.value("SELECT count(*) FROM complex_lifecycle_batches WHERE status='applied'"), 4)

    @unittest.skipUnless(shutil.which("pg_dump") and shutil.which("pg_restore"), "pg_dump and pg_restore needed")
    def test_the_database_read_gives_the_values_a_backup_gave(self):
        import apply_lifecycle_baseline as apply
        from lifecycle_check import store

        odd = "서울특별시 구로구 구로동|9|탭\t역\\슬래시"
        self.db.execute("INSERT INTO complexes(complex_id,name,address,built) VALUES (%s,%s,%s,1990)",
                        (odd, "줄\n바꿈\\N", "서울특별시 구로구 구로동 9"))
        self.db.execute("INSERT INTO area_groups VALUES (%s,60,59.5)", (odd,))
        self.trade(odd, date(2026, 2, 3))
        self.db.execute("UPDATE trades SET is_cancelled = TRUE WHERE complex_id=%s", (odd,))
        results = self.results()
        with patch("builtins.print"):
            apply.run_apply(self.db, results, TODAY, dry=False)
            apply.correct(self.db, GONE, "present", "basis=t; note=a\tb\nc", TODAY, dry=False)
        info = __import__("psycopg").conninfo.conninfo_to_dict(self.conninfo)
        dump = Path(self.enterContext(TemporaryDirectory())) / "backup.dump"
        subprocess.run(["pg_dump", "-Fc", "-h", info["host"], "-p", str(info["port"]), "-U", info["user"],
                        "-d", info["dbname"], "-f", str(dump)], check=True, capture_output=True)
        connect = store.psycopg.connect
        with patch.object(store.psycopg, "connect", lambda **kw: connect(self.conninfo, **kw)):
            with store.connect() as db:
                tables, fingerprints = store.load_inputs(db)
        self.assertEqual(fingerprints, {"as_of": None, "parcels": {}})
        for table, (columns, _) in store.READS.items():
            def comparable(rows):
                return sorted((tuple(row[c] for c in columns) for row in rows), key=repr)

            with self.subTest(table=table):
                expected = comparable(backup_rows(dump, table))
                self.assertTrue(expected)
                self.assertEqual(comparable(tables[table]), expected)
        self.assertIn((odd, "줄\n바꿈\\N", "1990"),
                      {(r["complex_id"], r["name"], r["built"]) for r in tables["complexes"]})
        states = {r["complex_id"]: r for r in tables["complex_lifecycle_state"]}
        self.assertEqual((states[GONE]["evidence"], states[GONE]["effective_date"], states[DATED]["effective_date"]),
                         ("basis=t; note=a\tb\nc", None, "2026-07-01"))
        self.assertEqual({r["is_cancelled"] for r in tables["trades"]}, {"t", "f"})

    def test_delete_blocks_reregistration_and_restores_every_row(self):
        import delete_complexes as dc
        import pandas as pd
        import refresh

        user = self.value("INSERT INTO app_users(username,salt,password_hash) VALUES ('t','\\x00','\\x00') RETURNING id")
        self.db.execute("INSERT INTO favorites(user_id,complex_id,area_group) VALUES (%s,%s,60)", (user, VILLA))
        self.db.execute("INSERT INTO saved_comparisons VALUES (%s,%s,60,%s,60,6,'')", (user, STAYS, VILLA))
        self.db.execute("INSERT INTO complex_notes VALUES (%s,%s,'n')", (user, VILLA))
        trade = self.value("SELECT min(trade_id) FROM trades WHERE complex_id=%s", (VILLA,))
        self.db.execute("INSERT INTO refresh_missing_trades VALUES (%s,'11530','2026-03-01',%s,%s,false)",
                        (trade, TODAY, TODAY))
        self.db.execute("INSERT INTO refresh_revision_pending VALUES (%s,'11530','2026-03-01',%s,'[]',%s,%s)",
                        (trade, VILLA, TODAY, TODAY))
        self.db.execute("INSERT INTO complex_geocode_pending VALUES (%s)", (VILLA,))
        self.db.execute("""INSERT INTO complex_facilities(complex_id, households, parking, station, schools)
            VALUES (%s, 10, 0.5, '["역", "1호선", 3, 37.5, 126.9]', '[]')""", (VILLA,))
        before = dc.collect(self.db, [VILLA])
        self.assertEqual(len(before["complex_facilities"]), 1)
        with TemporaryDirectory() as temp, patch("builtins.print"):
            archive = Path(temp) / "archive.json"
            dc.delete(self.db, {VILLA: "빌라"}, archive, TODAY, dry=True)
            self.assertEqual(dc.collect(self.db, [VILLA]), before)
            dc.delete(self.db, {VILLA: "빌라"}, archive, TODAY)
            self.assertEqual({k: v for k, v in dc.collect(self.db, [VILLA]).items() if v}, {})
            source = (VILLA, "빌라", "서울특별시 구로구 구로동 4", 1990)
            row = (VILLA, 60, date(2026, 10, 10), Decimal("59.5"), 3, Decimal("30000"), False)
            with self.db.transaction():
                result = refresh.reconcile(self.db, pd.Period("2026-10", freq="M"), {VILLA: source},
                                           Counter({row: 1}), "11530", refresh_day=TODAY)
            self.assertEqual((result.added, result.excluded), (0, 1))
            self.assertEqual(self.value("SELECT count(*) FROM complexes WHERE complex_id=%s", (VILLA,)), 0)
            dc.restore(self.db, archive, dry=True)
            self.assertEqual(self.value("SELECT count(*) FROM complexes WHERE complex_id=%s", (VILLA,)), 0)
            dc.restore(self.db, archive)
            self.assertTrue(json.loads(archive.read_text(encoding="utf-8"))["tables"]["favorites"])
        self.assertEqual(dc.collect(self.db, [VILLA]), before)
        self.assertEqual(self.value("SELECT count(*) FROM complex_exclusions"), 0)
        # An archive written before complex_facilities was listed has no such table and is still restorable.
        with TemporaryDirectory() as temp, patch("builtins.print"):
            archive = Path(temp) / "old-archive.json"
            dc.delete(self.db, {VILLA: "빌라"}, archive, TODAY)
            document = json.loads(archive.read_text(encoding="utf-8"))
            del document["tables"]["complex_facilities"]
            archive.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            dc.restore(self.db, archive)
        self.assertEqual(dc.collect(self.db, [VILLA]), {**before, "complex_facilities": []})

    def test_facilities_are_seeded_synced_and_refreshed_on_postgresql(self):
        import facilities

        connect = __import__("psycopg").connect
        isolated = lambda **kw: connect(self.conninfo, **kw)
        # A first computation that fails leaves the committed snapshot behind for the next sync.
        with patch("builtins.print"), patch.object(facilities, "compute_facilities", side_effect=RuntimeError), \
                self.assertRaises(RuntimeError), connect(self.conninfo) as db:
            facilities.ensure_facilities(db)
        self.assertEqual((self.value("SELECT count(*) FROM facility_schools"),
                          self.value("SELECT count(*) FROM complex_facilities")), (1317, 0))
        with patch("builtins.print"), connect(self.conninfo) as db:
            facilities.ensure_facilities(db)
        counts = [self.value(f"SELECT count(*) FROM {table}") for table in (
            "facility_apartments", "facility_stations", "facility_schools", "facility_school_anchors",
            "complex_facilities")]
        self.assertEqual(counts, [2712, 517, 1317, 1317, 4])
        seed = json.loads((ROOT / "facilities_seed/schools.json").read_text(encoding="utf-8"))["rows"]
        with connect(self.conninfo) as db:
            self.assertEqual(facilities.load_catalog(db)["schools"], seed)
            catalog = facilities.load_catalog(db)
        # What the database holds equals the pure computation over the stored complexes.
        homes = [dict(zip(("complex_id", "name", "address", "latitude", "longitude"), row)) for row in
                 self.db.execute("SELECT complex_id, name, address, latitude, longitude FROM complexes").fetchall()]
        expected = facilities.compute_facilities(homes, catalog)
        rows = self.db.execute("SELECT complex_id, households, parking, station, schools "
                               "FROM complex_facilities").fetchall()
        self.assertEqual({r[0]: dict(zip(("households", "parking", "station", "schools"), r[1:])) for r in rows},
                         expected)

        # A second sync waits until the first one commits instead of colliding with it.
        first, second = connect(self.conninfo), connect(self.conninfo)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        errors = []

        def run_second():
            try:
                facilities.sync_complex_facilities(second)
                second.commit()
            except Exception as exc:
                errors.append(exc)

        with patch("builtins.print"):
            first.execute("SELECT 1")  # An open transaction keeps the lock until commit.
            facilities.sync_complex_facilities(first)
            worker = Thread(target=run_second)
            worker.start()
            worker.join(1.0)
            self.assertTrue(worker.is_alive())
            first.commit()
            worker.join(30)
        self.assertEqual((worker.is_alive(), errors), (False, []))

        station = ["새 역", "1호선", 37.5, 126.9]
        with patch("builtins.print"), patch.dict(os.environ, {"TDATA_SUBWAY_API_KEY": "k"}, clear=False), \
             patch.dict(os.environ, {"NEIS_API_KEY": ""}), \
             patch.object(facilities.psycopg, "connect", isolated), \
             patch.object(facilities, "fetch_apartments", return_value=("짧은 응답", [])), \
             patch.object(facilities, "fetch_stations", return_value=("새 출처", [station] * 400)):
            self.assertEqual(facilities.refresh_sources(date(2026, 12, 1)), ["apartments"])
        self.assertEqual(self.value("SELECT count(*) FROM facility_apartments"), 2712)
        self.assertEqual(self.db.execute(
            "SELECT label, row_count, refreshed_month FROM facility_sources WHERE source='stations'").fetchone(),
            ("새 출처 · 조회 2026-12-01", 400, "2026-12"))
        self.assertEqual(self.value("SELECT station->>0 FROM complex_facilities WHERE complex_id=%s", (STAYS,)),
                         "새 역")


if __name__ == "__main__":
    unittest.main()
