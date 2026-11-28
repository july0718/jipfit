"""Mock-based tests for the existence baseline apply, training exclusion and complex removal steps."""
from collections import Counter
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

# Installs the psycopg stand-in when the package is missing, so it must be imported first.
from test_refresh_pipeline import FakeReconcileCursor, FakeReconcileDB, check_placeholders

import pandas as pd

import apply_lifecycle_baseline as apply
import delete_complexes as dc
import refresh
from lifecycle_results import LifecycleResult

ROOT = Path(__file__).resolve().parent
TODAY = date(2026, 11, 26)


class Result:
    def __init__(self, rows=(), rowcount=None):
        self.rows = list(rows)
        self.rowcount = len(self.rows) if rowcount is None else rowcount
        self.description = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class ScriptedDB:
    """Answers statements by fragment and records them with the transaction blocks around them."""

    def __init__(self, answers=()):
        self.answers = list(answers)  # (fragment, rows or callable(params))
        self.log = []

    def execute(self, sql, params=None):
        check_placeholders(sql, params)
        query = " ".join(sql.split())
        self.log.append((query, params))
        for fragment, rows in self.answers:
            if fragment in query:
                found = rows(params) if callable(rows) else rows
                return found if isinstance(found, Result) else Result(found)
        return Result()

    @contextmanager
    def transaction(self, savepoint_name=None, force_rollback=False):
        self.log.append(("BEGIN", force_rollback))
        yield self
        self.log.append(("END", force_rollback))

    def queries(self, fragment):
        return [(q, p) for q, p in self.log if fragment in q]


def judgment(cid="A", existence="demolished", **values):
    return LifecycleResult(**{"result_key": f"k:{cid}", "complex_id": cid, "existence": existence,
                              "evidence": "basis=t; note=n", "as_of": "2026-11-26", "verifier_version": "v",
                              **values})


class ResultFileTests(unittest.TestCase):
    def write(self, *lines):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "results.jsonl"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def line(self, cid, key=None):
        return json.dumps({"result_key": key or "k" + cid, "complex_id": cid, "existence": "present",
                           "evidence": "e", "as_of": "2026-11-26", "verifier_version": "v"})

    def test_reads_json_lines_into_the_contract(self):
        results = apply.read_results(self.write(self.line("A"), "", self.line("B")))
        self.assertEqual([r.complex_id for r in results], ["A", "B"])
        self.assertIsNone(results[0].effective_date)

    def test_malformed_or_duplicated_files_stop_before_any_apply(self):
        for lines in (["not json"], [json.dumps({"complex_id": "A"})], [json.dumps({"unknown": 1})],
                      [self.line("A"), self.line("A", "other")], [self.line("A", "same"), self.line("B", "same")], [""]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                apply.read_results(self.write(*lines))

    def test_training_exclusion_needs_demolition_without_effective_date_and_a_caution_date(self):
        evidence = "basis=수동 확인표; trade_caution_from=2019-12-13; note=n"
        self.assertEqual(apply.training_exclusion_date("demolished", None, evidence), date(2019, 12, 13))
        self.assertIsNone(apply.training_exclusion_date("demolished", date(2020, 1, 1), evidence))
        self.assertIsNone(apply.training_exclusion_date("present", None, evidence))
        # Free text after the fixed fields can never create an exclusion.
        self.assertIsNone(apply.training_exclusion_date(
            "demolished", None, "basis=t; note=x; trade_caution_from=2019-12-13"))
        self.assertIsNone(apply.training_exclusion_date("demolished", None, "basis=t; note=n"))


class ApplyTests(unittest.TestCase):
    def test_dry_run_applies_inside_rolled_back_batches(self):
        db = ScriptedDB()
        seen = []

        def fake_apply(conn, results, **kwargs):
            seen.append((results[0].complex_id, kwargs["allowed_ids"], kwargs["cutoff"]))
            return {"applied": 1, "noop": 0, "pending": 0, "quarantined": 0}

        with patch.object(apply, "apply_lifecycle_results", fake_apply), patch.object(apply, "DRY_RUN_BATCH", 2):
            summary, quarantined = apply.dry_run(db, [judgment(c) for c in "ABCDE"], TODAY)
        self.assertEqual((summary["applied"], quarantined), (5, []))
        self.assertEqual(seen[0], ("A", None, TODAY))
        self.assertEqual([entry for entry in db.log if entry[0] in ("BEGIN", "END")],
                         [("BEGIN", True), ("END", True)] * 3)

    def test_quarantined_records_are_reported_with_reasons(self):
        db = ScriptedDB([("FROM complex_lifecycle_quarantine", [("UNKNOWN_COMPLEX_ID",)])])
        outcomes = iter(({"applied": 0, "noop": 0, "pending": 0, "quarantined": 1},
                         {"applied": 0, "noop": 1, "pending": 0, "quarantined": 0}))
        with patch.object(apply, "apply_lifecycle_results", lambda *a, **k: next(outcomes)):
            summary, quarantined = apply.apply_each(db, [judgment("A"), judgment("B")], TODAY)
        self.assertEqual(summary, {"applied": 0, "noop": 1, "pending": 0, "quarantined": 1})
        self.assertEqual(quarantined, [("k:A", "A", "UNKNOWN_COMPLEX_ID")])

    def run_apply(self, db, results, **kwargs):
        with patch.object(apply, "apply_each") as real, patch.object(apply, "sync_derived") as sync, \
                patch.object(apply, "dry_run", return_value=({k: 0 for k in apply.SUMMARY_KEYS}, [])) as dry, \
                patch.object(apply, "metadata_revision_text", return_value="r"), patch("builtins.print"):
            real.return_value = ({k: 0 for k in apply.SUMMARY_KEYS}, [])
            sync.return_value = {"existence_status": 0, "training_exclusions": 0, "changed": 0, "republish": []}
            try:
                apply.run_apply(db, results, TODAY, **kwargs)
                return real, sync, dry, None
            except SystemExit as exc:
                return real, sync, dry, exc

    def db_for_run(self, conflicts=()):
        return ScriptedDB([("unnest(", list(conflicts)),
                           ("SELECT count(*) FROM refresh_revision_pending", [(0,)]),
                           ("SELECT count(*) FROM complex_lifecycle_state", [(0,)])])

    def test_dry_run_never_applies_or_syncs(self):
        real, sync, dry, _ = self.run_apply(self.db_for_run(), [judgment()], dry=True)
        self.assertEqual((real.called, sync.called, dry.called), (False, False, True))

    def test_real_run_refuses_effective_dates_that_conflict_with_stored_trades(self):
        result = judgment(effective_date="2020-01-01", effective_precision="day")
        conflict = [("A", date(2020, 1, 1), 1, date(2020, 2, 1))]
        real, sync, _, error = self.run_apply(self.db_for_run(conflict), [result], dry=False)
        self.assertIsNotNone(error)
        self.assertEqual((real.called, sync.called), (False, False))
        real, sync, _, error = self.run_apply(self.db_for_run(conflict), [result], dry=False, allow_conflicts=True)
        self.assertEqual((error, real.called, sync.called), (None, True, True))

    def sync_db(self, state, existing=(), trade_in_window=False):
        return ScriptedDB([
            ("UPDATE complexes c", Result(rowcount=1)),
            ("FROM complex_lifecycle_state WHERE", [state]),
            ("SELECT complex_id, exclude_from, reason", list(existing)),
            ("SELECT 1 FROM trades", [(1,)] if trade_in_window else []),
            ("SELECT count(*) FROM complex_training_exclusions", [(1,)]),
        ])

    def test_sync_copies_status_and_adds_exclusion_from_evidence(self):
        cid = "서울특별시 강남구 개포동|1|A"
        state = (cid, "k1", "demolished", None, "basis=t; trade_caution_from=2019-01-31; note=n")
        db = self.sync_db(state)
        outcome = apply.sync_derived(db, [cid], TODAY)
        copy = db.queries("UPDATE complexes c")[0][0]
        self.assertIn("WHEN 'present' THEN '현존' WHEN 'demolished' THEN '멸실'", copy)
        insert = db.queries("INSERT INTO complex_training_exclusions")[0][1]
        self.assertEqual(insert, (cid, date(2019, 1, 31), "lifecycle:k1"))
        self.assertEqual((outcome["changed"], outcome["republish"]), (1, []))
        self.assertEqual(db.queries("refresh_publish_pending"), [])

    def test_sync_queues_a_rebuild_when_the_input_window_is_touched(self):
        cid = "서울특별시 강남구 개포동|1|A"
        state = (cid, "k1", "demolished", None, "basis=t; trade_caution_from=2025-06-01; note=n")
        db = self.sync_db(state, trade_in_window=True)
        self.assertEqual(apply.sync_derived(db, [cid], TODAY)["republish"], ["11680"])
        self.assertEqual(db.queries("INSERT INTO refresh_publish_pending")[0][1], ("11680", TODAY))
        # The window for November 2026 is April 2025 through September 2026.
        self.assertEqual(db.queries("SELECT 1 FROM trades")[0][1],
                         (cid, date(2025, 6, 1), date(2026, 10, 1)))

    def test_sync_removes_only_its_own_exclusion_after_a_correction(self):
        cid = "서울특별시 강남구 개포동|1|A"
        present = (cid, "k2", "present", None, "정정")
        db = self.sync_db(present, existing=[(cid, date(2019, 1, 31), "lifecycle:k1")])
        apply.sync_derived(db, [cid], TODAY)
        self.assertTrue(db.queries("DELETE FROM complex_training_exclusions"))
        manual = self.sync_db(present, existing=[(cid, date(2019, 1, 31), "직접 입력")])
        apply.sync_derived(manual, [cid], TODAY)
        self.assertEqual(manual.queries("DELETE FROM complex_training_exclusions"), [])

    def test_correction_names_the_current_result(self):
        db = ScriptedDB([("FROM complex_lifecycle_state", [("k1", "demolished", "old")])])
        with patch.object(apply, "apply_each", return_value=({k: 0 for k in apply.SUMMARY_KEYS}, [])) as real, \
                patch.object(apply, "sync_derived", return_value={"changed": 0, "republish": []}), \
                patch("builtins.print"):
            apply.correct(db, "A", "present", "현장 확인", TODAY, dry=False)
        sent = real.call_args.args[1][0]
        self.assertEqual((sent.existence, sent.correction_of, sent.as_of), ("present", "k1", "2026-11-26"))
        self.assertTrue(sent.result_key.startswith("correction:"))

    def test_correction_requires_a_confirmed_state_and_is_idempotent(self):
        with self.assertRaises(SystemExit):
            apply.correct(ScriptedDB(), "A", "present", "e", TODAY, dry=False)
        db = ScriptedDB([("FROM complex_lifecycle_state", [("k2", "present", "e")])])
        with patch.object(apply, "apply_each") as real, patch.object(apply, "sync_derived"), patch("builtins.print"):
            self.assertIsNone(apply.correct(db, "A", "present", "e", TODAY, dry=False))
        real.assert_not_called()

    def test_command_requires_evidence_for_a_correction(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            apply.main(["--correct", "A", "--existence", "present"])


class TrainingInputTests(unittest.TestCase):
    CLAUSE = ("NOT EXISTS (SELECT 1 FROM complex_training_exclusions x WHERE x.complex_id{eq}t.complex_id "
              "AND t.contract_date >= x.exclude_from)")

    def test_forecast_input_query_leaves_out_excluded_contracts(self):
        db = ScriptedDB()
        with patch.object(refresh, "build_inputs", side_effect=ValueError("stop")), self.assertRaises(ValueError):
            refresh.publish(db, pd.Period("2026-11", freq="M"), "macro.csv", "11530")
        self.assertIn(self.CLAUSE.format(eq="="), db.log[0][0])

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn is not installed")
    def test_training_query_leaves_out_excluded_contracts(self):
        import train_seoul

        class Cursor:
            description = [type("C", (), {"name": n})() for n in
                           ("complex_id", "area_group", "contract_date", "area", "floor", "price_manwon", "built")]

            def fetchall(self):
                return [("A", 60, date(2020, 1, 1), 59.0, 1, 1000.0, 1990)]

        class DB(ScriptedDB):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                return Cursor()

        db = DB()
        with patch("builtins.print"):
            train_seoul.read_trades(db, "11530", "구로구", pd.Period("2026-11", freq="M"))
        self.assertIn(self.CLAUSE.format(eq=" = "), db.log[0][0])


class ExcludingCursor(FakeReconcileCursor):
    def __init__(self, *args, excluded=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.excluded = list(excluded)

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if " ".join(sql.split()).startswith("SELECT complex_id FROM complex_exclusions"):
            self._rows = [(cid,) for cid in self.excluded]
        return self


class ReregistrationTests(unittest.TestCase):
    VILLA = "서울특별시 구로구 구로동|745-70|(745-70)"
    ALIAS = "서울특별시 구로구 구로동|745-70|새이름"
    OTHER = "서울특별시 구로구 구로동|1-1|다른단지"

    def trade(self, cid):
        return (cid, 60, date(2026, 10, 10), Decimal("59.5"), 3, Decimal("30000"), False)

    def source(self, cid):
        return (cid, cid.split("|")[-1], cid.split("|")[0] + " " + cid.split("|")[1], 2002)

    def reconcile(self, cursor, ids, **kwargs):
        with patch("builtins.print"):
            return refresh.reconcile(
                FakeReconcileDB(cursor), pd.Period("2026-10", freq="M"), {c: self.source(c) for c in ids},
                Counter({self.trade(c): 1 for c in ids}), "11530", **kwargs)

    def test_removed_complex_is_not_registered_and_its_trades_are_dropped(self):
        for kwargs in ({"refresh_day": TODAY}, {"history": True}):
            with self.subTest(kwargs=kwargs):
                cursor = ExcludingCursor([], [], excluded=[self.VILLA])
                result = self.reconcile(cursor, [self.VILLA, self.OTHER], **kwargs)
                self.assertEqual((result.added, result.excluded), (1, 1))
                self.assertEqual([row[0] for row in cursor.inserted_trades], [self.OTHER])
                self.assertNotIn(self.VILLA, {row[0] for row in cursor.complexes} | set(cursor.identity_pending))

    def test_alias_leading_to_a_removed_complex_is_dropped_too(self):
        cursor = ExcludingCursor([], [], excluded=[self.VILLA], decisions={self.ALIAS: self.VILLA})
        result = self.reconcile(cursor, [self.ALIAS], refresh_day=TODAY)
        self.assertEqual((result.added, result.excluded, cursor.complexes, cursor.identity_pending), (0, 1, [], {}))

    def test_period_rule_leading_to_a_removed_complex_drops_the_rows(self):
        rule = (self.ALIAS, None, None, None, self.VILLA, None, None, None)
        cursor = ExcludingCursor([], [], excluded=[self.VILLA], period_decisions=[rule])
        result = self.reconcile(cursor, [self.ALIAS], refresh_day=TODAY)
        self.assertEqual((result.added, result.excluded, cursor.complexes, cursor.identity_pending), (0, 1, [], {}))

    def test_without_exclusions_registration_is_unchanged(self):
        cursor = ExcludingCursor([], [])
        result = self.reconcile(cursor, [self.VILLA], refresh_day=TODAY)
        self.assertEqual((result.added, result.excluded), (1, 0))

    def test_backfill_count_check_allows_dropped_rows(self):
        source = (ROOT / "backfill_history.py").read_text(encoding="utf-8")
        self.assertIn("actual != total - result.excluded", source)


class DeleteTests(unittest.TestCase):
    IDS = ["서울특별시 구로구 구로동|745-70|(745-70)", "서울특별시 구로구 구로동|806-33|구로아트빌라4동(806-33)"]

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.targets = {cid: "빌라" for cid in self.IDS}

    def db(self, *, present=None, excluded=(), unlisted=False, archive=None):
        present = self.IDS if present is None else present

        def delete_rows(params):
            # The archive must be complete on disk before the first row is deleted.
            if archive is not None:
                self.assertTrue(archive.is_file())
            return Result(rowcount=1)

        columns = [("complexes", "complex_id")] + ([("new_table", "complex_id")] if unlisted else [])
        return ScriptedDB([
            ("DELETE FROM", delete_rows),
            ("FROM complexes WHERE complex_id = ANY(%s::text[]) FOR UPDATE", [(c,) for c in present]),
            ("SELECT complex_id FROM complex_exclusions", [(c,) for c in excluded]),
            ("information_schema.columns", columns),
            ('FROM "new_table"', [(1,)]),
            ("SELECT to_jsonb(t)::text", [('{"n": 59.8200}',)]),
        ])

    def test_target_list_is_validated(self):
        path = self.root / "targets.json"
        path.write_text(json.dumps([{"complex_id": self.IDS[0], "reason": "빌라"}]), encoding="utf-8")
        self.assertEqual(dc.read_targets(path), {self.IDS[0]: "빌라"})
        for bad in ([], [{"complex_id": self.IDS[0]}], [{"complex_id": "A", "reason": "r"}],
                    [{"complex_id": self.IDS[0], "reason": "r"}] * 2):
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dc.read_targets(path)

    def test_delete_order_satisfies_every_foreign_key(self):
        schema = "".join((ROOT / "db" / name).read_text(encoding="utf-8") for name in
                         ("init.sql", "trade_revisions.sql", "metadata_refresh.sql", "user_accounts.sql"))
        order = [name for name, _ in dc.TABLES]
        tables = re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \((.*?)\n\);", schema, re.S)
        checked = 0
        for child, body in tables:
            for parent in re.findall(r"REFERENCES (\w+)", body):
                if parent in order:
                    self.assertIn(child, order, f"{child} references {parent}")
                    self.assertLess(order.index(child), order.index(parent), f"{child} before {parent}")
                    checked += 1
        self.assertGreater(checked, 12)
        # Every table that stores a complex ID is archived.
        holders = {name for name, body in tables
                   if re.search(r"^\s*(complex_id|left_id) text|, (complex_id|left_id) text", body, re.M)}
        self.assertEqual(holders - set(order) - {"complex_exclusions"}, set())

    def test_delete_archives_then_deletes_in_order_and_blocks_reregistration(self):
        archive = self.root / "archive.json"
        db = self.db(archive=archive)
        with patch("builtins.print"):
            dc.delete(db, self.targets, archive, TODAY)
        deleted = [q.split()[2] for q, _ in db.queries("DELETE FROM")]
        self.assertEqual(deleted, [name for name, _ in dc.TABLES])
        saved = json.loads(archive.read_text(encoding="utf-8"))
        self.assertEqual(saved["tables"]["trades"], ['{"n": 59.8200}'])
        self.assertEqual(saved["targets"], self.targets)
        self.assertEqual([p[0] for _, p in db.queries("INSERT INTO complex_exclusions")], sorted(self.IDS))
        self.assertEqual(db.queries("pg_advisory_xact_lock")[0][1], (11530,))

    def test_dry_run_writes_nothing(self):
        db = self.db()
        with patch("builtins.print"):
            dc.delete(db, self.targets, self.root / "archive.json", TODAY, dry=True)
        self.assertFalse((self.root / "archive.json").exists())
        self.assertEqual(db.queries("DELETE FROM") + db.queries("INSERT INTO"), [])
        self.assertEqual(db.log[0], ("BEGIN", True))

    def test_unknown_target_unlisted_table_or_existing_archive_stop_before_deleting(self):
        existing = self.root / "existing.json"
        existing.write_text("keep", encoding="utf-8")
        cases = ((self.db(present=self.IDS[:1]), self.root / "a.json", ValueError),
                 (self.db(unlisted=True), self.root / "b.json", ValueError),
                 (self.db(), existing, FileExistsError))
        for db, path, error in cases:
            with self.subTest(path=path.name), patch("builtins.print"), self.assertRaises(error):
                dc.delete(db, self.targets, path, TODAY)
            self.assertEqual(db.queries("DELETE FROM"), [])
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")

    def test_repeating_a_finished_delete_changes_nothing(self):
        db = self.db(present=[], excluded=self.IDS)
        with patch("builtins.print"):
            self.assertEqual(dc.delete(db, self.targets, self.root / "a.json", TODAY), {})
        self.assertEqual(db.queries("DELETE FROM"), [])

    def archive_file(self, without=(), extra=()):
        path = self.root / "archive.json"
        names = [name for name, _ in dc.TABLES if name not in without] + list(extra)
        path.write_text(json.dumps({"version": 1, "targets": self.targets,
                                    "tables": {name: [f'{{"t": "{name}"}}'] for name in names}}),
                        encoding="utf-8")
        return path

    def test_restore_inserts_in_reverse_order_and_lifts_the_block(self):
        db = ScriptedDB()
        with patch("builtins.print"):
            dc.restore(db, self.archive_file())
        inserts = db.queries("INSERT INTO")
        self.assertEqual([q.split()[2] for q, _ in inserts], [name for name, _ in reversed(dc.TABLES)])
        for query, _ in inserts:
            self.assertEqual("OVERRIDING SYSTEM VALUE" in query, query.split()[2] in dc.IDENTITY_TABLES)
        self.assertTrue(db.queries("DELETE FROM complex_exclusions"))

    def test_an_archive_written_before_the_facility_table_is_still_restored(self):
        db = ScriptedDB()
        with patch("builtins.print"):
            dc.restore(db, self.archive_file(without=("complex_facilities",)))
        self.assertEqual([q.split()[2] for q, _ in db.queries("INSERT INTO")],
                         [name for name, _ in reversed(dc.TABLES) if name != "complex_facilities"])
        self.assertTrue(db.queries("DELETE FROM complex_exclusions"))

    def test_any_other_difference_in_the_archived_tables_is_refused(self):
        for difference in ({"without": ("trades",)}, {"without": ("complex_facilities", "trades")},
                           {"extra": ("app_users",)},
                           {"without": ("complex_facilities",), "extra": ("app_users",)}):
            db = ScriptedDB()
            with self.subTest(**difference), self.assertRaises(ValueError):
                dc.restore(db, self.archive_file(**difference))
            self.assertEqual(db.log, [])

    def test_restore_refuses_existing_complexes_and_foreign_files(self):
        db = ScriptedDB([("SELECT complex_id FROM complexes", [(self.IDS[0],)])])
        with self.assertRaises(ValueError):
            dc.restore(db, self.archive_file())
        self.assertEqual(db.queries("INSERT INTO"), [])
        other = self.root / "other.json"
        other.write_text(json.dumps({"version": 1, "targets": {}, "tables": {"app_users": []}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            dc.restore(ScriptedDB(), other)


class RevisionTests(unittest.TestCase):
    # SHA-256 of the query GET /api/metadata-revision ran in main.py at 33bbd79, before it moved here.
    ENDPOINT_QUERY = "03df148899dcd508efa0be22cf7d408f4559b2020286ec56000da99fb3506b75"

    def test_the_query_is_the_one_the_endpoint_ran(self):
        self.assertEqual(sha256(apply.REVISION_SQL.encode("utf-8")).hexdigest(), self.ENDPOINT_QUERY)

    def test_the_digest_comes_from_the_given_connection(self):
        db = ScriptedDB([("SELECT md5(", [("abc",)])])
        self.assertEqual(apply.metadata_revision(db), "abc")
        self.assertEqual(db.log, [(" ".join(apply.REVISION_SQL.split()), None)])
        self.assertEqual(apply.metadata_revision_text(db), "abc")

    def test_a_failure_is_named_without_pointing_at_the_removed_endpoint(self):
        with patch.object(apply, "metadata_revision", side_effect=RuntimeError("secret")):
            self.assertEqual(apply.metadata_revision_text(ScriptedDB()), "확인 불가(RuntimeError)")

    def test_neither_the_script_nor_the_api_keeps_the_endpoint(self):
        for name in ("apply_lifecycle_baseline.py", "main.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("/api/metadata-revision", source, name)
            self.assertNotIn("from main import", source, name)


class PackagingTests(unittest.TestCase):
    def test_new_tables_are_idempotent_and_prepared_by_api_and_updater(self):
        sql = (ROOT / "db/metadata_refresh.sql").read_text(encoding="utf-8")
        for table in ("complex_training_exclusions", "complex_exclusions"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} (", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS existence_status text", sql)
        for module in ("main.py", "refresh.py"):
            self.assertIn("db/metadata_refresh.sql", (ROOT / module).read_text(encoding="utf-8"), module)

    def test_image_contains_the_new_commands(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for name in ("apply_lifecycle_baseline.py", "delete_complexes.py"):
            self.assertIn(name, dockerfile)


if __name__ == "__main__":
    unittest.main()
