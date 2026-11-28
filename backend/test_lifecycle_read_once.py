"""One disk read per file and run (sources.read_once): same judgement, no stale or changed records."""
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import Lock
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

# Installs the psycopg stand-in when the package is missing, so it must be imported first.
from test_lifecycle_check import (BASE_DAY, DAY, KEY, Month, Stage, StageDB, answer, cid, complex_, gone, home,
                                  parcel, put, state)

import lifecycle_admin as admin
from lifecycle_check import collect, demolition, existence, monthly, sources, stage
from lifecycle_check.sources import cache_file, parcel_params

REAL_FETCH = sources.fetch  # Stage replaces it with a fake that never calls the API.
READ_ONCE = "JIPFIT_LIFECYCLE_READ_ONCE"
ATTACHED = {"atchSigunguCd": "11380", "atchBjdongCd": "10100", "atchPlatGbCd": "0", "atchBun": "0501",
            "atchJi": "0000"}


@contextmanager
def counted_reads(*folders, pause=0):
    """How often each file below the folders is opened for reading."""
    reads, lock = Counter(), Lock()

    def counted(method):
        def call(path, *args, **kwargs):
            if any(path.is_relative_to(folder) for folder in folders):
                with lock:
                    reads[path] += 1
                time.sleep(pause)
            return method(path, *args, **kwargs)

        return call

    with patch.object(Path, "read_text", counted(Path.read_text)), \
            patch.object(Path, "read_bytes", counted(Path.read_bytes)):
        yield reads


def repeated(reads):
    return {str(path): count for path, count in reads.items() if count > 1}


def environment(value=None):
    """READ_ONCE set to the value, or unset for the default."""
    changed = patch.dict(os.environ, {} if value is None else {READ_ONCE: value})
    if value is None:
        os.environ.pop(READ_ONCE, None)
    return changed


class Records(Month):
    """Every kind of file one run reads, with parcels that several complexes and steps ask for."""

    def setUp(self):
        super().setUp()
        self.patch(patch.dict(os.environ, {"JIPFIT_LIFECYCLE_DATA": str(self.data)}))
        os.environ.pop(READ_ONCE, None)
        existence._MANAGED.clear()
        # Two complexes on one lot, a cancellation with an attached lot, and a demolished one to be linked.
        self.add(complex_(), state(cid()))
        self.add(complex_("345-4", "함께"), state(cid("345-4", "함께")))
        self.before(current=[home()])
        self.registers(current=[home()])
        self.add(complex_("500", "말소"), state(cid("500", "말소")))
        self.before(parcel("500"), current=[home(bldNm="말소")], closed_land=[ATTACHED], closed_outline=[])
        self.cancelled(parcel("500"), closed=[gone(bldNm="말소")], closed_land=[ATTACHED])
        self.registers(parcel("501"), current=[])
        self.add(complex_("600", "옛단지"), state(cid("600", "옛단지"), "demolished"))
        self.add(complex_("600", "새단지", "2023"), last_trade="")
        self.registers(parcel("600"), current=[home(useAprDay="20230101")], closed=[gone(shterDay="20200420")])
        put(self.cache, "trade_detail", {"LAWD_CD": "11380", "DEAL_YMD": "202612"},
            [{"sggCd": "11380", "umdNm": "수색동", "umdCd": "10100"}], DAY)
        put(self.reference, "kapt_info", {"kaptCode": "A1"},
            [{"kaptCode": "A1", "bjdCode": "1138010100", "kaptName": "다른단지", "kaptUsedate": "20070301"}])
        documents = self.reference / "public_documents"
        documents.mkdir(parents=True)
        for name, kind, body in (
                ("a", "mastr-cleanup-bsnsSumry", "<th>정비구역 위치</th><td>수색동 999 일대</td>"),
                ("b", "cleanup-prtnelapse", "<td>2019-01-01</td><td>[철거완료]</td>")):
            (documents / f"{name}.json").write_text(json.dumps(
                {"url": f"https://cleanup.seoul.go.kr/{kind}.do?cafeId=1", "charset": "utf-8"}), encoding="utf-8")
            (documents / f"{name}.bin").write_bytes(body.encode("utf-8"))
        self.inputs = self.fingerprints()
        existence._MANAGED.clear()

    def decide(self):
        return stage.judge(self.tables(), self.inputs, self.cache, self.reference, DAY)


class ReadOnceTests(Records):
    def test_one_block_reads_every_file_once(self):
        with counted_reads(self.data) as reads, sources.read_once() as opened:
            self.decide()
            monthly.baseline_fingerprints(self.reference, BASE_DAY)
            self.decide()
        self.assertEqual(repeated(reads), {})
        found = {(path.relative_to(self.data).parts[0], path.parent.name, path.suffix) for path in reads}
        for kind in (("reference", "trade_detail", ".json"), ("monthly", "2026-12", None),
                     ("reference", "kapt_info", ".json"), ("reference", "public_documents", ".json"),
                     ("reference", "public_documents", ".bin"), ("reference", "current", ".json")):
            if kind[2]:
                self.assertIn(kind, found)
        for service in ("current", "closed", "demolition", "closed_outline", "closed_land", "trade_detail"):
            self.assertTrue(any(path.is_relative_to(self.cache / service) for path in reads), service)
        stats = opened.stats()
        self.assertEqual(stats["disk"], stats["files"])
        self.assertGreater(stats["reused"], stats["disk"])

    def test_without_the_store_the_same_files_are_read_again(self):
        with environment("0"), counted_reads(self.data) as reads, sources.read_once() as opened:
            self.decide()
            monthly.baseline_fingerprints(self.reference, BASE_DAY)
        self.assertGreater(len(repeated(reads)), 10)
        self.assertEqual(opened.stats()["reused"], 0)
        self.assertEqual((opened.parsed, opened.bodies, opened.results), ({}, {}, {}))

    def test_seeding_reads_the_reference_cache_once(self):
        with counted_reads(self.reference) as reads:
            admin.seed_fingerprints(StageDB(), self.reference, BASE_DAY)
        self.assertTrue(reads)
        self.assertEqual(repeated(reads), {})
        self.assertIsNone(sources._store)

    def test_the_judgement_is_the_same_with_and_without_the_store(self):
        found = {}
        for value in ("0", "1"):
            existence._MANAGED.clear()
            # A limit of 0 turns the confirmation into a proposal, so both result files have rows.
            with environment(value), patch.dict(os.environ, {"JIPFIT_LIFECYCLE_MAX_AUTO": "0"}), \
                    sources.read_once() as opened:
                outcome = self.decide()
            self.assertEqual(opened.stats()["reused"] > 0, value == "1")
            self.assertTrue(outcome["results"] and outcome["proposals"])
            found[value] = (monthly.encode_results(outcome["results"]),
                            monthly.encode_results(outcome["proposals"]),
                            monthly.report_text("monthly-20261228", outcome), outcome["summary"],
                            outcome["auto"], outcome["review"], outcome["fingerprints"])
        self.assertEqual(found["0"], found["1"])

    def test_the_judgement_leaves_the_saved_records_as_they_are_on_disk(self):
        def digest(opened):
            body = json.dumps([opened.parsed, opened.bodies, {str(k): v for k, v in opened.results.items()}],
                              sort_keys=True, ensure_ascii=False,
                              default=lambda value: value.hex() if isinstance(value, bytes) else
                              "missing" if value is sources.MISSING else sorted(value))
            return sha256(body.encode("utf-8")).hexdigest()

        with sources.read_once() as opened:
            self.decide()
            before = digest(opened)
            self.decide()
            monthly.baseline_fingerprints(self.reference, BASE_DAY)
            monthly.baseline_fingerprints(self.reference, BASE_DAY)
            after_seed = digest(opened)
            self.decide()
            self.assertEqual(digest(opened), after_seed)
            # The first judgement did not change what it read either.
            for key, value in opened.parsed.items():
                if value is not sources.MISSING:
                    self.assertEqual(value, json.loads(Path(key).read_text(encoding="utf-8")), key)
            for key, value in opened.bodies.items():
                self.assertEqual(value, Path(key).read_bytes(), key)
            (saved,) = opened.results.values()
        self.assertEqual(saved, demolition.parse_project_pages(self.reference))
        self.assertNotEqual(before, "")

    def test_only_the_reference_cache_keeps_its_project_pages(self):
        with sources.read_once() as opened:
            self.assertEqual(demolition.project_pages(self.cache), ({}, {}))
            self.assertEqual(opened.results, {})
            self.assertIs(demolition.project_pages(self.reference), demolition.project_pages(self.reference))

    def test_removing_an_old_cache_forgets_what_was_read(self):
        params = parcel_params(KEY)
        monthly.write_json(self.cache / monthly.CACHE_MARKER, {"started": DAY.isoformat()})
        with sources.read_once() as opened:
            self.assertIsNotNone(sources.cached(self.cache, "current", params))
            monthly.prepare_cache(self.cache, self.reference, DAY + timedelta(days=6))
            self.assertTrue(opened.parsed)
            monthly.prepare_cache(self.cache, self.reference, DAY + timedelta(days=7))
            self.assertEqual((opened.parsed, opened.bodies, opened.results), ({}, {}, {}))
            self.assertIsNone(sources.cached(self.cache, "current", params))


class StoreTests(Records):
    def fetch(self, rows, **options):
        return sources.fetch(self.cache, {sources.KEY_NAME: "k"}, "current", parcel_params(parcel("700")),
                             ask=lambda service, url: answer(rows), **options)

    def test_a_written_file_replaces_the_saved_answer_without_another_read(self):
        params = parcel_params(parcel("700"))
        path = cache_file(self.cache, "current", params)
        with counted_reads(self.cache) as reads, sources.read_once():
            self.assertIsNone(sources.cached(self.cache, "current", params))
            self.assertIsNone(sources.cached(self.cache, "current", params))
            written = self.fetch([{"n": "1"}])
            found = sources.cached(self.cache, "current", params)
            self.assertEqual(found["rows"], [{"n": "1"}])
            self.assertIsNot(found, written)  # Parsed from the written text, as a read from disk would be.
            self.assertEqual(self.fetch([{"n": "unused"}]), found)
            self.fetch([{"n": "2"}], refresh=True)
            self.assertEqual(sources.cached(self.cache, "current", params)["rows"], [{"n": "2"}])
            self.assertEqual(sources.read_json(path), sources.cached(self.cache, "current", params))
        self.assertEqual(reads[path], 1)  # The first look, when there was no file.
        self.assertEqual(sources.cached(self.cache, "current", params)["rows"], [{"n": "2"}])

    def test_a_missing_or_broken_file_fails_as_before(self):
        path = self.cache / "kapt_info" / "x.json"
        with counted_reads(self.cache) as reads, sources.read_once():
            for read in (sources.read_json, sources.read_json, sources.read_bytes, sources.read_bytes):
                with self.assertRaises(FileNotFoundError):
                    read(path.with_name("none"))
            path.parent.mkdir(parents=True)
            path.write_text("{", encoding="utf-8")
            for _ in range(2):
                with self.assertRaises(json.JSONDecodeError):
                    sources.read_json(path)
        self.assertEqual((reads[path.with_name("none")], reads[path]), (2, 2))  # A broken file is not saved.

    def test_nothing_is_saved_outside_a_block(self):
        params = parcel_params(parcel("700"))
        path = cache_file(self.cache, "current", params)

        def saved():
            return sources.cached(self.cache, "current", params)["rows"][0]["bun"], sources.read_bytes(path)

        def change(rows):
            put(self.cache, "current", params, rows, DAY)

        change([{"bun": "1"}])
        self.assertEqual(saved()[0], "1")
        change([{"bun": "2"}])
        self.assertEqual(saved()[0], "2")
        with sources.read_once() as outer:
            first = saved()
            change([{"bun": "3"}])
            with sources.read_once() as inner:
                self.assertIs(inner, outer)
                self.assertEqual(saved(), first)
            self.assertEqual(saved(), first)  # The inner block's end forgets nothing.
        self.assertEqual(saved()[0], "3")
        with self.assertRaises(RuntimeError), sources.read_once():
            saved()
            raise RuntimeError
        change([{"bun": "4"}])
        self.assertEqual(saved()[0], "4")
        with environment("0"), sources.read_once():
            self.assertEqual(saved()[0], "4")
            change([{"bun": "5"}])
            self.assertEqual(saved()[0], "5")

    def test_collection_threads_share_the_store(self):
        old = [("current", parcel_params(parcel(str(lot)))) for lot in range(800, 810)]
        new = [("current", parcel_params(parcel(str(lot)))) for lot in range(900, 940)]
        for service, params in old:
            put(self.cache, service, params, [], DAY)

        def request(service, url):
            return answer([{"bun": parse_qs(urlsplit(url).query)["bun"][0]}])

        keys = {sources.KEY_NAME: "k"}
        with patch.object(sources, "request_molit", request), counted_reads(self.cache, pause=0.002) as reads, \
                sources.read_once():
            outcome = collect.fetch_all(old * 8 + new, self.cache, keys, 8, refresh=False)
            again = collect.fetch_all((old + new) * 3, self.cache, keys, 8, refresh=False)
            rows = [sources.cached(self.cache, *call)["rows"] for call in new]
        self.assertEqual((outcome, again), (Counter({"현행 완료": 120}), Counter({"현행 완료": 150})))
        self.assertEqual(rows, [[{"bun": params["bun"]}] for _, params in new])
        self.assertEqual(repeated(reads), {})
        self.assertEqual({reads[cache_file(self.cache, *call)] for call in old}, {1})
        self.assertEqual({reads[cache_file(self.cache, *call)] for call in new}, {0})


class RunTests(Stage):
    """Whole runs with the real fetch and its threads; only the HTTP answers are made up."""

    def setUp(self):
        super().setUp()
        os.environ.pop(READ_ONCE, None)
        self.patch(patch.object(sources, "fetch", REAL_FETCH))
        self.patch(patch.object(sources, "request_molit", self.request))
        self.output = self.patch(patch("builtins.print"))

    def request(self, service, url):
        query = parse_qs(urlsplit(url).query)
        name = query.get("bun", query.get("DEAL_YMD"))[0]
        return answer(self.answers.get((service, name), []))

    def lines_about_reads(self):
        return [str(call.args[0]) for call in self.output.call_args_list
                if str(call.args[0]).startswith("Lifecycle 파일 읽기")]

    def test_a_run_reads_every_cache_file_at_most_once_and_says_so_once(self):
        old, new = self.gone_complex(), self.new_complex()
        self.seed()
        with counted_reads(self.reference, self.data / "monthly") as reads:
            self.assertEqual(lifecycle_run(), [])
        self.assertEqual(repeated(reads), {})
        self.assertEqual((self.db.state(old)["existence"], self.db.state(new)["existence"]),
                         ("demolished", "present"))
        (line,) = self.lines_about_reads()
        found = re.fullmatch(r"Lifecycle 파일 읽기: 디스크 ([\d,]+)회\(파일 ([\d,]+)개\) · 재사용 [\d,]+회"
                             r"(?: · 최대 메모리 [\d,]+MB)? · 판정 \d+분 \d+초", line)
        self.assertIsNotNone(found, line)
        # Only reference files came from disk: a fetched file is opened once, before it exists, and not read back.
        self.assertTrue(any(path.is_relative_to(self.cache) for path in reads))
        on_disk = sum(path.is_relative_to(self.reference) and path.is_file() for path in reads)
        self.assertEqual(found.groups(), (str(on_disk), str(on_disk)))
        self.assertIsNone(sources._store)

    def test_a_rehearsal_and_the_next_run_each_start_empty(self):
        self.gone_complex()
        self.seed()
        self.assertEqual(lifecycle_run(dry_run=True), [])
        with counted_reads(self.reference, self.data / "monthly") as reads:
            self.assertEqual(lifecycle_run(dry_run=True), [])
        # The second run reads the cached files again, once each.
        self.assertTrue(any(path.is_relative_to(self.cache) for path in reads))
        self.assertEqual(repeated(reads), {})
        self.assertEqual(len(self.lines_about_reads()), 2)

    def test_a_refused_run_opens_no_block(self):
        self.db.locked = True
        self.assertEqual(lifecycle_run(), ["lifecycle:locked"])
        self.assertEqual(self.lines_about_reads(), [])


def lifecycle_run(**options):
    return stage.run_month(DAY, **options)


if __name__ == "__main__":
    unittest.main()
