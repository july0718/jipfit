"""PostgreSQL integration test of the existence baseline steps. Skipped unless JIPFIT_TEST_DSN is set.

JIPFIT_TEST_DSN points at a disposable test server, e.g. "host=127.0.0.1 port=55987 user=postgres
dbname=postgres". The test creates its own database, works only inside it and drops it afterwards.
"""
from collections import Counter
from datetime import date
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
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
        conninfo = f"{DSN} dbname={self.name}"
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
        before = dc.collect(self.db, [VILLA])
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


if __name__ == "__main__":
    unittest.main()
