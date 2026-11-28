"""Tests of the facility catalogs, the per-complex summary and where the updater calls them."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import importlib.util
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import quote

# Installs the psycopg stand-in when the package is missing, so it must be imported first.
from test_refresh_pipeline import check_placeholders

import facilities
import refresh

ROOT = Path(__file__).resolve().parent
TODAY = date(2026, 12, 1)
HOME = {"complex_id": "서울특별시 광진구 구의동|1|현대", "name": "현대",
        "address": "서울특별시 광진구 구의동 1", "latitude": 37.5, "longitude": 127.1}
APT = ["광진구", "구의동", "구의현대아파트", 37.5, 127.1, 200, 250]
CATALOG = {"apartments": [APT], "stations": [["A", "1호선", 37.5, 127.1]],
           "schools": [["S", "초등학교", "도로 1", 37.501, 127.1]]}


def other(number, **changes):
    return {**HOME, "complex_id": f"서울특별시 광진구 구의동|{number}|현대", **changes}


def facility(home=HOME, **catalog):
    return facilities.compute_facilities([home], {**CATALOG, **catalog})[home["complex_id"]]


class Rows(list):
    def fetchall(self):
        return list(self)

    def fetchone(self):
        return self[0] if self else None


class FacilityDB:
    """In-memory facility tables answering the statements of facilities.py; transactions roll back on errors.

    Rows come back newest first unless the statement orders them, as a database is free to do.
    """

    def __init__(self, **tables):
        names = ("facility_apartments", "facility_stations", "facility_schools", "facility_school_anchors",
                 "facility_sources", "complex_facilities", "complexes")
        self.tables = {name: list(tables.get(name, [])) for name in names}
        self.log = []

    def __enter__(self):
        self._saved = deepcopy(self.tables)
        return self

    def __exit__(self, kind, *args):
        if kind:
            self.tables = self._saved
        return False

    @contextmanager
    def transaction(self):
        saved = deepcopy(self.tables)
        self.log.append(("BEGIN", None))
        try:
            yield self
        except BaseException:
            self.tables = saved
            self.log.append(("ROLLBACK", None))
            raise
        self.log.append(("COMMIT", None))

    @contextmanager
    def cursor(self):
        yield self

    def statements(self, fragment=""):
        return [(q, p) for q, p in self.log if fragment in q]

    def execute(self, sql, params=None):
        check_placeholders(sql, params)
        q = " ".join(sql.split())
        self.log.append((q, params))
        if q.startswith("SELECT pg_advisory_xact_lock"):
            return Rows()
        if found := re.fullmatch(r"SELECT count\(\*\) FROM (\w+)", q):
            return Rows([(len(self.tables[found[1]]),)])
        if q == "SELECT 1 FROM facility_sources WHERE source = %s AND refreshed_month = %s":
            return Rows((1,) for row in self.tables["facility_sources"]
                        if (row["source"], row["refreshed_month"]) == params)
        if q.startswith("INSERT INTO facility_sources("):
            row = dict(zip(("source", "label", "row_count", "refreshed_month"), params))
            self.tables["facility_sources"] = [
                r for r in self.tables["facility_sources"] if r["source"] != row["source"]] + [row]
            return Rows()
        if found := re.fullmatch(r"DELETE FROM (\w+)", q):
            self.tables[found[1]] = []
            return Rows()
        if found := re.fullmatch(r"SELECT (.+?) FROM (\w+)(?: ORDER BY (\w+))?", q):
            columns, table, order = found[1].split(", "), found[2], found[3]
            rows = self.tables[table][::-1]
            if order:
                rows = sorted(rows, key=lambda row: row[order])
            return Rows(tuple(row[column] for column in columns) for row in rows)
        raise AssertionError(f"unexpected statement: {q}")

    def executemany(self, sql, rows):
        q = " ".join(sql.split())
        found = re.match(r"INSERT INTO (\w+) ?\(([^)]*)\) VALUES", q)
        columns = found[2].split(", ")
        for row in rows:
            check_placeholders(sql, row)
            self.log.append((q, row))
            self.tables[found[1]].append(dict(zip(columns, row, strict=True)))


def stored(source, *rows, month=None, label=None):
    """Tables holding the given catalog rows (seq in the given order) and their source record."""
    table, columns = facilities.CATALOGS[source]
    values = [(seq, *row) for seq, row in enumerate(rows)] if columns[0] == "seq" else rows
    return {table: [dict(zip(columns, row)) for row in values],
            "facility_sources": [{"source": source, "label": label or source, "row_count": len(rows),
                                  "refreshed_month": month}]}


def catalog_db(catalog=CATALOG, homes=(HOME,), **extra):
    tables = {"facility_sources": [], "complexes": [dict(home) for home in homes]}
    for source, rows in catalog.items():
        part = stored(source, *rows)
        tables["facility_sources"] += part.pop("facility_sources")
        tables.update(part)
    tables.update(extra)
    return FacilityDB(**tables)


class ComputeTests(unittest.TestCase):
    def test_a_unique_identity_is_linked_with_straight_line_distances(self):
        found = facility()
        self.assertEqual((found["households"], found["parking"]), (200, 1.25))
        self.assertEqual(found["station"], ["A", "1호선", 0, 37.5, 127.1])
        self.assertEqual(found["schools"], [["S", "초등학교", 111, 37.501, 127.1]])
        self.assertGreater(facilities.distance(37.5, 127, 37.51, 127), 1100)

    def test_ambiguous_identities_distant_matches_and_missing_coordinates_stay_unknown(self):
        both = facilities.compute_facilities([HOME, other(2)], CATALOG)
        self.assertEqual([item["households"] for item in both.values()], [None, None])
        self.assertIsNone(facility(apartments=[APT, APT])["parking"])
        self.assertIsNone(facility({**HOME, "latitude": 37.6})["households"])
        unplaced = facility({**HOME, "latitude": None})
        self.assertEqual(unplaced, {"households": None, "parking": None, "station": None, "schools": None})

    def test_verified_zero_parking_is_distinct_from_unknown(self):
        for spaces, expected in ((0, 0), (None, None)):
            self.assertEqual(facility(apartments=[[*APT[:6], spaces]])["parking"], expected)
        for households in (0, None):
            found = facility(apartments=[[*APT[:5], households, 250]])
            self.assertEqual((found["households"], found["parking"]), (None, None))

    def test_names_are_reduced_before_matching(self):
        self.assertEqual(facilities.simple("구의 현대·2.차-아파트", "구의동"), "현대2차")
        self.assertEqual(facilities.simple("ＬＧ_자이", "자양동"), "lg자이")
        # A one-letter dong prefix and an inner '아파트' stay.
        self.assertEqual(facilities.simple("중곡아파트", "중동"), "중곡")
        self.assertEqual(facilities.simple("아파트먼트", "구의동"), "아파트먼트")

    def test_distances_round_halves_up_like_javascript(self):
        self.assertEqual([facilities.whole_metres(v) for v in (0.5, 1.5, 2.5, 2.4999)], [1, 2, 3, 2])
        self.assertEqual(round(2.5), 2)

    def test_the_single_complex_rule_counts_within_a_district(self):
        neighbour = {**HOME, "complex_id": "서울특별시 성동구 구의동|1|현대"}
        found = facilities.compute_facilities([HOME, neighbour], CATALOG)
        self.assertEqual([item["households"] for item in found.values()], [200, 200])

    def test_an_apartment_shared_with_an_ambiguous_complex_is_not_linked(self):
        # The second complex sees two apartments, one of them the first complex's only match.
        far = ["광진구", "구의동", "현대", 37.505, 127.1, 100, 100]
        between = other(2, latitude=37.5025)
        found = facilities.compute_facilities([HOME, between], {**CATALOG, "apartments": [APT, far]})
        self.assertEqual([item["households"] for item in found.values()], [None, None])

    def test_equal_distances_keep_the_earlier_row_and_absent_levels_are_left_out(self):
        twins = [["여고", "고등학교", "a", 37.501, 127.1], ["고교", "고등학교", "b", 37.501, 127.1]]
        self.assertEqual([row[0] for row in facility(schools=twins)["schools"]], ["여고"])
        self.assertEqual([row[0] for row in facility(schools=twins[::-1])["schools"]], ["고교"])
        self.assertEqual(facility(schools=[])["schools"], [])


class SeedTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("builtins.print")
        self.printed = patcher.start()
        self.addCleanup(patcher.stop)

    def seed_folder(self, catalog):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for source, rows in catalog.items():
            (root / f"{source}.json").write_text(
                json.dumps({"source": f"{source} 사본", "rows": rows}, ensure_ascii=False), encoding="utf-8")
        (root / "school-anchors.json").write_text(
            json.dumps([["S", "초등학교", "도로 1", 37.501, 127.1]], ensure_ascii=False), encoding="utf-8")
        return patch.object(facilities, "SEED", root)

    def test_empty_tables_take_the_snapshot_in_file_order_and_are_computed_once(self):
        schools = [["뒤", "초등학교", "a", 37.501, 127.1], ["앞", "초등학교", "b", 37.501, 127.1]]
        db = FacilityDB(complexes=[dict(HOME)])
        with self.seed_folder({**CATALOG, "schools": schools}):
            facilities.ensure_facilities(db)
        self.assertEqual([(r["seq"], r["name"]) for r in db.tables["facility_schools"]], [(0, "뒤"), (1, "앞")])
        self.assertEqual([r["seq"] for r in db.tables["facility_stations"]], [0])
        self.assertEqual([(r["source"], r["label"], r["row_count"], r["refreshed_month"])
                          for r in db.tables["facility_sources"]],
                         [("apartments", "apartments 사본", 1, None), ("stations", "stations 사본", 1, None),
                          ("schools", "schools 사본", 2, None)])
        self.assertEqual(len(db.tables["facility_school_anchors"]), 1)
        (row,) = db.tables["complex_facilities"]
        self.assertEqual((row["households"], json.loads(row["schools"])[0][0]), (200, "뒤"))
        self.assertEqual(db.log[1], ("SELECT pg_advisory_xact_lock(%s)", (facilities.LOCK,)))

    def test_filled_tables_are_neither_seeded_nor_recomputed(self):
        db = catalog_db(facility_school_anchors=[{"name": "S"}], complex_facilities=[{"complex_id": "kept"}])
        before = deepcopy(db.tables)
        with patch.object(facilities, "_seed", side_effect=AssertionError("must not seed")), \
             patch.object(facilities, "sync_complex_facilities", side_effect=AssertionError("must not compute")):
            facilities.ensure_facilities(db)
        self.assertEqual(db.tables, before)

    def test_an_empty_summary_is_computed_from_the_stored_catalogs(self):
        db = catalog_db(facility_school_anchors=[{"name": "S"}])
        with patch.object(facilities, "_seed", side_effect=AssertionError("must not seed")):
            facilities.ensure_facilities(db)
        self.assertEqual([r["complex_id"] for r in db.tables["complex_facilities"]], [HOME["complex_id"]])

    def test_a_failed_computation_keeps_the_snapshot_for_the_daily_sync(self):
        db = FacilityDB(complexes=[dict(HOME)])
        with self.seed_folder(CATALOG), self.assertRaises(RuntimeError), \
                patch.object(facilities, "compute_facilities", side_effect=RuntimeError("x")):
            with db:
                facilities.ensure_facilities(db)
        self.assertEqual(db.tables["complex_facilities"], [])
        # In PostgreSQL the first transaction() block has committed; the fake restores only the failed block.
        self.assertEqual([q for q, _ in db.log if q in ("COMMIT", "ROLLBACK")], ["COMMIT", "ROLLBACK"])

    def test_a_caller_that_syncs_anyway_gets_the_seed_without_the_computation(self):
        db = FacilityDB(complexes=[dict(HOME)])
        with self.seed_folder(CATALOG), \
                patch.object(facilities, "sync_complex_facilities", side_effect=AssertionError("must not compute")):
            facilities.ensure_facilities(db, compute=False)
        self.assertEqual(len(db.tables["facility_schools"]), 1)
        self.assertEqual(db.tables["complex_facilities"], [])

    def test_the_bundled_snapshot_loads(self):
        db = FacilityDB(complexes=[dict(HOME)])
        facilities.ensure_facilities(db)
        self.assertEqual({r["source"]: r["row_count"] for r in db.tables["facility_sources"]},
                         {"apartments": 2712, "stations": 517, "schools": 1317})
        self.assertEqual(len(db.tables["facility_school_anchors"]), 1317)
        self.assertEqual([r["seq"] for r in db.tables["facility_schools"]], list(range(1317)))

    def test_a_failed_seed_is_reported_by_type_and_the_updater_goes_on(self):
        with patch.object(refresh.psycopg, "connect", return_value=FacilityDB()), \
             patch.object(refresh, "ensure_facilities", side_effect=RuntimeError("secret")):
            self.assertIsNone(refresh.seed_facilities())
        message = " ".join(str(c) for c in self.printed.call_args_list)
        self.assertIn("RuntimeError", message)
        self.assertNotIn("secret", message)

    def test_the_updater_seeds_after_the_schema_and_before_its_workers(self):
        calls = []

        class Thread:
            def __init__(self, target, name):
                pass

            def start(self):
                calls.append("thread")

            def join(self):
                pass

        with patch.object(refresh, "ensure_schema", lambda: calls.append("schema")), \
             patch.object(refresh, "seed_facilities", lambda: calls.append("seed")), \
             patch.object(refresh, "Thread", Thread), patch("sys.argv", ["refresh.py"]):
            refresh.main()
        self.assertEqual(calls, ["schema", "seed", "thread", "thread"])

    def test_only_the_updater_and_the_commands_seed(self):
        for name in ("main.py", "apply_lifecycle_baseline.py", "delete_complexes.py", "lifecycle_admin.py",
                     "backfill_history.py"):
            self.assertNotIn("ensure_facilities", (ROOT / name).read_text(encoding="utf-8"), name)
        source = (ROOT / "refresh.py").read_text(encoding="utf-8")
        schema_step = source.split("def ensure_schema():")[1].split("\ndef ")[0]
        self.assertNotIn("facilities", schema_step)


class SyncTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("builtins.print")
        self.printed = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_lock_is_taken_first_and_the_summary_replaced_as_a_whole(self):
        unplaced = other(2, latitude=None, longitude=None)
        db = catalog_db(homes=(HOME, unplaced), complex_facilities=[{"complex_id": "stale"}])
        self.assertEqual(facilities.sync_complex_facilities(db), 2)
        self.assertEqual(db.log[:2], [("BEGIN", None),
                                      ("SELECT pg_advisory_xact_lock(%s)", (facilities.LOCK,))])
        self.assertEqual(db.log[-1], ("COMMIT", None))
        rows = {r["complex_id"]: r for r in db.tables["complex_facilities"]}
        self.assertEqual(set(rows), {HOME["complex_id"], unplaced["complex_id"]})
        self.assertEqual(json.loads(rows[HOME["complex_id"]]["station"]), ["A", "1호선", 0, 37.5, 127.1])
        self.assertEqual((rows[unplaced["complex_id"]]["station"], rows[unplaced["complex_id"]]["schools"]),
                         (None, None))
        self.printed.assert_called_with("Facilities sync: 2 complexes", flush=True)

    def test_stations_and_schools_are_read_in_source_order(self):
        twins = [["먼저", "고등학교", "a", 37.501, 127.1], ["나중", "고등학교", "b", 37.501, 127.1]]
        stations = [["첫 역", "1호선", 37.5, 127.1], ["둘째 역", "2호선", 37.5, 127.1]]
        db = catalog_db({**CATALOG, "schools": twins, "stations": stations})
        db.tables["facility_schools"].reverse()
        facilities.sync_complex_facilities(db)
        (row,) = db.tables["complex_facilities"]
        self.assertEqual((json.loads(row["schools"])[0][0], json.loads(row["station"])[0]), ("먼저", "첫 역"))
        self.assertTrue(db.statements("FROM facility_schools ORDER BY seq"))
        self.assertTrue(db.statements("FROM facility_stations ORDER BY seq"))

    def test_an_empty_catalog_leaves_the_summary_untouched(self):
        db = catalog_db({**CATALOG, "schools": []}, complex_facilities=[{"complex_id": "kept"}])
        with self.assertRaises(ValueError):
            facilities.sync_complex_facilities(db)
        self.assertEqual(db.tables["complex_facilities"], [{"complex_id": "kept"}])


class RefreshSourceTests(unittest.TestCase):
    NEW = {"apartments": [APT, APT], "stations": [["B", "2호선", 37.5, 127.1], ["C", "3호선", 37.6, 127.1]],
           "schools": [["T", "중학교", "도로 2", 37.5, 127.1]]}

    def setUp(self):
        self.db = catalog_db(facility_school_anchors=[
            {"name": "S", "level": "초등학교", "address": "도로 1", "latitude": 37.501, "longitude": 127.1}])
        self.fetched, self.anchors = [], None

    def run_refresh(self, *, keys=("TDATA_SUBWAY_API_KEY", "NEIS_API_KEY"), fail=(), rows=None, **options):
        rows = {**self.NEW, **(rows or {})}

        def fetcher(source):
            def fetch(*args):
                self.fetched.append(source)
                if source == "schools":
                    self.anchors = args[1]
                if source in fail:
                    raise OSError("https://example.invalid/?key=secret")
                return f"{source} 출처", rows[source]
            return fetch

        environment = {name: "secret" for name in keys}
        with patch.dict("os.environ", environment, clear=True), patch("builtins.print") as printed, \
             patch.object(facilities.psycopg, "connect", return_value=self.db), \
             patch.object(facilities, "fetch_apartments", fetcher("apartments")), \
             patch.object(facilities, "fetch_stations", fetcher("stations")), \
             patch.object(facilities, "fetch_schools", fetcher("schools")), \
             patch.object(facilities, "sync_complex_facilities") as sync:
            failed = facilities.refresh_sources(TODAY, **options)
        output = "\n".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertNotIn("secret", output)
        return failed, sync, output

    def sources(self):
        return {r["source"]: (r["label"], r["row_count"], r["refreshed_month"])
                for r in self.db.tables["facility_sources"]}

    def test_every_source_is_replaced_in_response_order_and_the_summary_recomputed(self):
        failed, sync, output = self.run_refresh()
        self.assertEqual(failed, [])
        self.assertEqual(self.sources(), {
            "apartments": ("apartments 출처 · 조회 2026-12-01", 2, "2026-12"),
            "stations": ("stations 출처 · 조회 2026-12-01", 2, "2026-12"),
            "schools": ("schools 출처 · 조회 2026-12-01", 1, "2026-12")})
        self.assertEqual([(r["seq"], r["name"]) for r in self.db.tables["facility_stations"]], [(0, "B"), (1, "C")])
        self.assertEqual(self.anchors, [["S", "초등학교", "도로 1", 37.501, 127.1]])
        sync.assert_called_once_with(self.db)
        self.assertIn("Facilities 2026-12: apartments 2 / stations 2 / schools 1", output)

    def test_a_source_refreshed_this_month_is_not_fetched_again(self):
        self.run_refresh()
        self.fetched.clear()
        failed, sync, _ = self.run_refresh()
        self.assertEqual((failed, self.fetched), ([], []))
        sync.assert_not_called()
        # The next month fetches again.
        for row in self.db.tables["facility_sources"]:
            row["refreshed_month"] = "2026-11"
        self.run_refresh()
        self.assertEqual(self.fetched, ["apartments", "stations", "schools"])

    def test_a_source_without_its_key_is_skipped_without_failing(self):
        failed, sync, output = self.run_refresh(keys=())
        self.assertEqual((failed, self.fetched), ([], ["apartments"]))
        self.assertEqual(self.sources()["stations"], ("stations", 1, None))
        self.assertIn("NEIS_API_KEY not configured", output)
        sync.assert_called_once()

    def test_a_short_or_empty_result_keeps_the_stored_rows(self):
        table = "facility_apartments"
        self.db.tables[table] = self.db.tables[table] * 3
        failed, sync, _ = self.run_refresh(keys=(), rows={"apartments": [APT, APT]})  # 2 of 3 is under 70%.
        self.assertEqual((failed, len(self.db.tables[table])), (["apartments"], 3))
        self.assertEqual(self.sources()["apartments"], ("apartments", 1, None))
        sync.assert_not_called()
        self.db.tables[table] = []
        self.assertEqual(self.run_refresh(keys=(), rows={"apartments": []})[0], ["apartments"])

    def test_a_failed_source_is_named_and_the_others_still_update(self):
        failed, sync, output = self.run_refresh(fail=("stations",))
        self.assertEqual(failed, ["stations"])
        self.assertEqual(self.sources()["stations"][2], None)
        self.assertEqual(self.sources()["schools"][2], "2026-12")
        self.assertIn("stations: previous data retained (OSError)", output)
        sync.assert_called_once()

    def test_a_dry_run_only_counts(self):
        before = deepcopy(self.db.tables)
        failed, sync, output = self.run_refresh(dry_run=True)
        self.assertEqual((failed, self.db.tables), ([], before))
        self.assertEqual(self.db.statements("pg_advisory_xact_lock"), [])
        sync.assert_not_called()
        self.assertIn("apartments 2 (stored 1)", output)

    def test_a_failed_recomputation_is_reported_as_sync(self):
        with patch.dict("os.environ", {}, clear=True), patch("builtins.print"), \
             patch.object(facilities.psycopg, "connect", return_value=self.db), \
             patch.object(facilities, "fetch_apartments", return_value=("출처", [APT])), \
             patch.object(facilities, "sync_complex_facilities", side_effect=RuntimeError("x")):
            self.assertEqual(facilities.refresh_sources(TODAY), ["sync"])


class FetchTests(unittest.TestCase):
    def test_apartments_keep_located_complexes_without_officetels(self):
        header = "주소(시군구),주소(읍면동),k-아파트명,좌표Y,좌표X,k-단지분류(아파트,주상복합등등),k-전체세대수,주차대수"
        header = header.replace("k-단지분류(아파트,주상복합등등)", '"k-단지분류(아파트,주상복합등등)"')
        text = "\n".join([header, '광진구,구의동,현대,37.5,127.1,아파트,"1,200",0', "광진구,구의동,오피,37.5,127.1,오피스텔,10,10",
                          "광진구,구의동,밖,35.1,129.0,아파트,10,10", "광진구,구의동,미상,37.5,127.1,아파트,,"])
        for encoding in ("utf-8-sig", "cp949"):
            with patch.object(facilities, "_request", return_value=text.encode(encoding)):
                _, rows = facilities.fetch_apartments()
            self.assertEqual(rows, [["광진구", "구의동", "현대", 37.5, 127.1, 1200, 0],
                                    ["광진구", "구의동", "미상", 37.5, 127.1, None, None]])

    def test_stations_merge_lines_across_pages_and_refuse_a_stalled_pager(self):
        record = lambda name, line: {"convY": "37.5", "convX": "127.1", "stnKrNm": name, "lineNm": line}
        first = [record("A", "2호선")] * 499 + [record("B", "5호선")]
        pages = iter([first, [record("A", "1호선")]])
        with patch.object(facilities, "_request", lambda url: json.dumps(next(pages)).encode()):
            _, rows = facilities.fetch_stations("k")
        self.assertEqual(rows, [["A", "1호선·2호선", 37.5, 127.1], ["B", "5호선", 37.5, 127.1]])
        with patch.object(facilities, "_request", return_value=json.dumps([record("A", "2호선")] * 500).encode()), \
                self.assertRaises(ValueError):
            facilities.fetch_stations("k")

    def test_transfer_stations_share_their_lines_and_keep_each_platform(self):
        record = lambda name, line, y: {"convY": str(y), "convX": "127.1", "stnKrNm": name, "lineNm": line}
        data = [record("환승", "2호선", 37.5), record("환승", "7호선", 37.504), record("환승", "9호선", 37.508),
                record("신촌", "2호선", 37.6), record("신촌", "경의중앙선", 37.6065), record("홀로", "1호선", 37.7)]
        with patch.object(facilities, "_request", return_value=json.dumps(data).encode()):
            _, rows = facilities.fetch_stations("k")
        # 445 m steps chain into one station (ends 890 m apart); 723 m apart stays two stations.
        self.assertEqual(rows, [["환승", "2호선·7호선·9호선", 37.5, 127.1], ["환승", "2호선·7호선·9호선", 37.504, 127.1],
                                ["환승", "2호선·7호선·9호선", 37.508, 127.1], ["신촌", "2호선", 37.6, 127.1],
                                ["신촌", "경의중앙선", 37.6065, 127.1], ["홀로", "1호선", 37.7, 127.1]])
        found = facilities.nearest(37.5079, 127.1, rows)
        self.assertEqual(found[:2], ["환승", "2호선·7호선·9호선"])
        self.assertEqual(found[3], 37.508)

    def test_schools_take_coordinates_only_from_a_matching_anchor(self):
        anchors = [["가초", "초등학교", "서울특별시 광진구 길 1", 37.5, 127.1]]
        school = lambda name, address: {"SCHUL_NM": name, "SCHUL_KND_SC_NM": "초등학교", "ORG_RDNMA": address}
        answer = {"schoolInfo": [{"head": [{"list_total_count": 3}]}, {"row": [
            school("가초", "서울특별시 광진구 길1 (구의동)"), school("가초", "서울특별시 광진구 길 2"), school("나초", None)]}]}
        with patch.object(facilities, "_request", return_value=json.dumps(answer).encode()):
            _, rows = facilities.fetch_schools("k", anchors)
        self.assertEqual(rows, [["가초", "초등학교", "서울특별시 광진구 길1 (구의동)", 37.5, 127.1]])
        answer["schoolInfo"][0]["head"][0]["list_total_count"] = 2
        with patch.object(facilities, "_request", return_value=json.dumps(answer).encode()), \
                self.assertRaises(ValueError):
            facilities.fetch_schools("k", anchors)


class UpdaterStepTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("builtins.print")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_sources_carry_the_facilities_label(self):
        with patch.object(refresh, "refresh_sources", return_value=["stations", "sync"]) as monthly:
            self.assertEqual(refresh.refresh_facility_sources(TODAY), ["facilities:stations", "facilities:sync"])
        monthly.assert_called_once_with(TODAY)
        with patch.object(refresh, "refresh_sources", side_effect=RuntimeError("x")):
            self.assertEqual(refresh.refresh_facility_sources(TODAY), ["facilities:RuntimeError"])

    def test_the_daily_sync_runs_on_its_own_connection_and_labels_its_failure(self):
        db = FacilityDB()
        calls = []
        with patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "ensure_facilities", lambda d, **kw: calls.append(("seed", d, kw))), \
             patch.object(refresh, "sync_complex_facilities") as sync:
            sync.side_effect = lambda d: calls.append(("sync", d))
            self.assertEqual(refresh.sync_facilities(), [])
            # A seed missed at start-up is retried first, without computing twice.
            self.assertEqual(calls, [("seed", db, {"compute": False}), ("sync", db)])
            sync.side_effect = RuntimeError("x")
            self.assertEqual(refresh.sync_facilities(), ["facilities:sync"])


class CommandTests(unittest.TestCase):
    def run_command(self, *argv, failed=()):
        calls = []
        note = lambda name, value=None: lambda *a, **kw: calls.append((name, kw)) or value
        with patch.object(refresh, "ensure_schema", note("schema")), patch("builtins.print"), \
             patch.object(facilities.psycopg, "connect", return_value=FacilityDB()), \
             patch.object(facilities, "ensure_facilities", note("seed")), \
             patch.object(facilities, "sync_complex_facilities", note("sync")), \
             patch.object(facilities, "refresh_sources", note("refresh", list(failed))):
            code = facilities.main(list(argv))
        return code, calls

    def test_commands_prepare_the_tables_and_a_dry_run_writes_nothing(self):
        self.assertEqual(self.run_command("sync"), (0, [("schema", {}), ("seed", {}), ("sync", {})]))
        self.assertEqual(self.run_command("refresh"),
                         (0, [("schema", {}), ("seed", {}), ("refresh", {"dry_run": False})]))
        self.assertEqual(self.run_command("refresh", "--dry-run"), (0, [("refresh", {"dry_run": True})]))
        self.assertEqual(self.run_command("refresh", failed=["stations"])[0], 1)

    def test_the_image_holds_the_module_and_the_snapshot(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("facilities.py", dockerfile)
        self.assertIn("COPY facilities_seed/ facilities_seed/", dockerfile)

    def test_the_updater_gets_the_keys(self):
        updater = (ROOT.parent / "compose.yaml").read_text(encoding="utf-8").split("\n  updater:")[1]
        for name in ("NEIS_API_KEY", "TDATA_SUBWAY_API_KEY"):
            self.assertIn(f"{name}: ${{{name}:-}}", updater)
            self.assertIn(f"\n{name}=\n", (ROOT.parent / ".env.example").read_text(encoding="utf-8"))


def installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ValueError:  # A stand-in module installed by another test has no spec.
        return False


@unittest.skipUnless(all(installed(name) for name in ("fastapi", "psycopg", "sklearn", "joblib")),
                     "needs the API packages")
class FacilityApiTests(unittest.TestCase):
    def get(self, path, query="", rows=(), sources=()):
        import main

        class ApiDB(FacilityDB):
            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                self.log.append((" ".join(sql.split()), params))
                return Rows(dict(row) for row in (sources if "facility_sources" in sql else rows))

        db = ApiDB()
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                 "root_path": "", "headers": [], "client": ("test", 1), "server": ("test", 80)}
        with patch.object(main, "connect", return_value=db):
            asyncio.run(main.app(scope, receive, send))
        body = b"".join(message.get("body", b"") for message in sent[1:])
        return sent[0]["status"], json.loads(body), db

    def test_the_district_summary_is_keyed_by_complex_with_sources_in_order(self):
        item = {"households": 206, "parking": 1.08, "station": ["흑석", "9호선", 412, 37.5, 126.96],
                "schools": [["가초", "초등학교", 300, 37.5, 126.9]]}
        sources = [{"source": name, "label": name + " 기준"} for name in ("schools", "apartments", "stations")]
        status, body, db = self.get("/api/facilities", "district=" + quote("광진구"),
                                    rows=[{"complex_id": HOME["complex_id"], **item}], sources=sources)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"sources": ["apartments 기준", "stations 기준", "schools 기준"],
                                "items": {HOME["complex_id"]: item}})
        self.assertEqual(db.statements("FROM complex_facilities")[0][1], ("광진구",))

    def test_empty_tables_answer_empty(self):
        status, body, _ = self.get("/api/facilities", "district=" + quote("광진구"))
        self.assertEqual((status, body), (200, {"sources": [], "items": {}}))

    def test_a_missing_or_unknown_district_is_rejected(self):
        for query in ("", "district=" + quote("없는구"), "district="):
            status, _, db = self.get("/api/facilities", query)
            self.assertEqual((status, db.log), (422, []), query)

    def test_the_revision_route_is_gone(self):
        self.assertEqual(self.get("/api/metadata-revision")[0], 404)


if __name__ == "__main__":
    unittest.main()
