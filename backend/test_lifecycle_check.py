"""The monthly existence check (plan-lifecycle-auto.md 11), on small cache folders with made-up registers.

The judgement, fingerprint, successor, collection and bundle tests are those of
jipfit_lifecycle@d86dcdf:test_monthly.py. The stage and operator tests run against a mock database that keeps
the lifecycle tables; no test calls an outside API.
"""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# Installs the psycopg stand-in when the package is missing, so it must be imported first.
from test_refresh_pipeline import check_placeholders
from test_lifecycle_inbox import InboxDB, Result

import lifecycle_admin as admin
import lifecycle_check
from lifecycle_check import collect, monthly, sources, stage, store
from lifecycle_check.common import csv_rows
from lifecycle_check.sources import SourceError, cache_file, parcel_params
import lifecycle_inbox as inbox
import refresh

ROOT = Path(__file__).resolve().parent
BASE_DAY = date(2026, 11, 26)
DAY = date(2026, 12, 28)
MONTH = "2026-12"
BATCH = "monthly-20261228"
DONG = "서울특별시 은평구 수색동"
KEY = "11380_10100_0_0345_0004"


def cid(lot="345-4", name="제일"):
    return f"{DONG}|{lot}|{name}"


def complex_(lot="345-4", name="제일", built="2007"):
    return {"complex_id": cid(lot, name), "name": name, "built": built, "address": f"{DONG} {lot}"}


def parcel(lot):
    bun, _, ji = lot.partition("-")
    return f"11380_10100_0_{bun.zfill(4)}_{(ji or '0').zfill(4)}"


def trade(complex_id, day="2019-11-14", cancelled="f"):
    return {"complex_id": complex_id, "contract_date": day, "is_cancelled": cancelled}


def home(**fields):
    return {"mainAtchGbCd": "0", "mainPurpsCdNm": "공동주택", "etcPurps": "아파트", "bldNm": "제일", "dongNm": "가동",
            "hhldCnt": "12", "useAprDay": "20070101", "mgmBldrgstPk": "C1", **fields}


def gone(**fields):
    return {**home(mgmBldrgstPk=""), "mgmShtregPk": "S1", "shterGbCd": "2", "shterDay": "20261201", **fields}


def state(complex_id, existence="present", as_of="2026-11-26", **fields):
    return {"complex_id": complex_id, "result_key": f"baseline-20261126:{complex_id}", "existence": existence,
            "as_of": as_of, "evidence": "basis=자동 현존 확인; note=기존", "effective_date": None,
            "effective_precision": None, "pending_hold_code": None, "pending_as_of": None, **fields}


def held(complex_id, code, as_of="2026-11-26", **fields):
    """A complex Jipfit holds without any confirmed judgment."""
    return state(complex_id, None, None, result_key=None, evidence=None, pending_hold_code=code,
                 pending_as_of=as_of, **fields)


def put(cache, service, params, rows, day=BASE_DAY):
    path = cache_file(cache, service, params)
    path.parent.mkdir(parents=True, exist_ok=True)
    if service in ("current", "closed", "demolition"):
        rows = [{**params, **row} for row in rows]  # Register rows name their parcel.
    path.write_text(json.dumps({"retrieved_on": day.isoformat(), "total": len(rows), "rows": rows},
                               ensure_ascii=False), encoding="utf-8")


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


class Folders(unittest.TestCase):
    """A reference cache and a monthly cache in a temporary data folder."""

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "lifecycle-data"
        self.reference = self.data / "reference"
        self.cache = self.data / "monthly" / MONTH
        self.cache.mkdir(parents=True)
        put(self.reference, "trade_detail", {"LAWD_CD": "11380", "DEAL_YMD": "201911"},
            [{"sggCd": "11380", "umdNm": "수색동", "umdCd": "10100"}])
        self.patch(patch("builtins.print"))

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def registers(self, key=KEY, current=(), closed=(), demolition=(), cache=None, **extra):
        cache = cache or self.cache
        day = BASE_DAY if cache == self.reference else DAY
        found = {"current": current, "closed": closed, "demolition": demolition, **extra}
        for service, rows in found.items():
            put(cache, service, parcel_params(key), list(rows), day)

    def before(self, key=KEY, **registers):
        """The registers the baseline saw."""
        self.registers(key, cache=self.reference, **registers)

    def cancelled(self, key=KEY, closed=None, **extra):
        self.registers(key, closed=closed or [gone()], **{"closed_outline": [], "closed_land": [], **extra})


# Judgement

class Month(Folders):
    """The judgement on a Jipfit stand-in, read back from the bundle and report files."""

    def setUp(self):
        super().setUp()
        self.complexes, self.trades, self.states, self.history = [], [], [], []
        self.runs = 0

    def add(self, complex_row, state_row=None, last_trade="2019-11-14"):
        self.complexes.append(complex_row)
        if last_trade:
            self.trades.append(trade(complex_row["complex_id"], last_trade))
        if state_row:
            self.states.append(state_row)
        return complex_row["complex_id"]

    def tables(self):
        return {"complexes": self.complexes, "trades": self.trades, "complex_lifecycle_state": self.states,
                "complex_lifecycle_results": self.history}

    def fingerprints(self):
        return monthly.baseline_fingerprints(self.reference, BASE_DAY)

    def judge(self, day=DAY):
        outcome = stage.evaluate(self.tables(), self.fingerprints(), self.cache, self.reference, day, "v")
        self.runs += 1
        self.out = self.root / f"out{self.runs}"
        monthly.write_batch({**outcome, "proposals": []}, self.out / "batches", self.out / "reports", day, "v")
        return outcome

    def run_month(self, day=DAY):
        self.judge(day)
        batch = f"monthly-{day:%Y%m%d}"
        results = {r["complex_id"]: r for r in lines(self.out / "batches" / batch / "results.jsonl")}
        reports = self.out / "reports" / batch
        return results, csv_rows(reports / "auto_confirmed.csv"), csv_rows(reports / "review.csv")

    def saved_fingerprints(self, day=DAY):
        return json.loads((self.out / "batches" / f"monthly-{day:%Y%m%d}" / "fingerprints.json").read_text("utf-8"))

    def kinds(self, review):
        return [row["구분"] for row in review]


class UnchangedTests(Month):
    def test_a_month_without_changes_has_no_results(self):
        self.add(complex_(), state(cid()))
        self.add(complex_("400", "옛단지"), state(cid("400", "옛단지"), "demolished"))
        for write in (self.before, self.registers):
            write(current=[home()])
            write(parcel("400"), closed=[gone(shterDay="20200420")])
        results, auto, review = self.run_month()
        self.assertEqual((results, auto, review), ({}, [], []))
        manifest = json.loads((self.out / "batches" / BATCH / "manifest.json").read_text("utf-8"))
        self.assertEqual((manifest["batch"], manifest["results"], manifest["counts"]), (BATCH, 0, {}))

    def test_a_human_decision_stays_while_the_register_records_are_the_same(self):
        # The register has no residential title, so the automatic rule cannot call it 현존.
        self.add(complex_(), state(cid(), evidence="basis=수동 확인표; note=현장 확인"))
        shop = home(mainPurpsCdNm="근린생활시설", etcPurps="", useAprDay="19900101")
        old = gone(shterDay="20100101")
        self.before(current=[shop], closed=[old])
        self.registers(current=[shop], closed=[old])
        results, auto, review = self.run_month()
        self.assertEqual((results, auto, review), ({}, [], []))

    def test_a_new_record_sends_a_present_complex_back_to_judgement(self):
        complex_id = self.add(complex_(), state(cid()))
        self.before(current=[home(useAprDay="19900101")])
        self.registers(current=[home(useAprDay="19900101")],
                       demolition=[{"mgmPmsrgstPk": "D1", "mainPurpsCdNm": "공동주택", "demolEndDay": "20261201"}])
        results, _, _ = self.run_month()
        self.assertEqual((results[complex_id]["existence"], results[complex_id]["hold_code"]),
                         ("pending", "범위미확인"))
        self.assertIn("D1", results[complex_id]["evidence"])

    def test_a_vanished_record_is_only_listed_for_review(self):
        self.add(complex_(), state(cid()))
        self.before(current=[home(), home(mgmBldrgstPk="C2", dongNm="나동")])
        self.registers(current=[home()])
        results, _, review = self.run_month()
        self.assertEqual((results, self.kinds(review)), ({}, ["지문에서 사라진 기록"]))
        self.assertIn("C2", review[0]["상세"])

    def test_incomplete_registers_keep_a_present_complex_and_its_fingerprint(self):
        self.add(complex_(), state(cid()))
        self.before(current=[home()])
        put(self.cache, "current", parcel_params(KEY), [], DAY)
        results, _, review = self.run_month()
        self.assertEqual((results, self.kinds(review)), ({}, ["수집미완"]))
        self.assertEqual(self.saved_fingerprints()["parcels"][KEY]["current"], [["C1", "20070101", "12"]])

    def test_an_unidentified_present_complex_is_kept_and_a_new_one_is_held(self):
        kept = self.add({**complex_("BL-1", "가"), "address": f"{DONG} BL-1"}, state(cid("BL-1", "가")))
        new = self.add({**complex_("BL-2", "나"), "address": f"{DONG} BL-2"})
        results, _, review = self.run_month()
        self.assertEqual((list(results), results[new]["hold_code"]), ([new], "식별실패"))
        self.assertIn(kept, {row["complex_id"] for row in review})

    def test_a_demolished_complex_with_a_returning_building_is_only_listed(self):
        self.add(complex_(), state(cid(), "demolished"))
        self.registers(current=[home()])
        results, _, review = self.run_month()
        self.assertEqual((results, self.kinds(review)), ({}, ["멸실 단지의 현존 신호"]))


class JudgementTests(Month):
    def test_a_new_complex_that_stands_is_reported_present(self):
        complex_id = self.add(complex_())
        self.registers(current=[home()])
        result = self.run_month()[0][complex_id]
        self.assertEqual((result["existence"], result["result_key"], result["as_of"], result["verifier_version"]),
                         ("present", f"monthly-20261228:{complex_id}", "2026-12-28", "v"))
        self.assertTrue(result["evidence"].startswith("basis=자동 현존 확인; note="))

    def test_a_new_cancellation_is_confirmed_on_the_first_cancellation_day(self):
        complex_id = self.add(complex_(), state(cid()))
        self.before(current=[home()])
        self.cancelled()
        results, auto, _ = self.run_month()
        result = results[complex_id]
        self.assertEqual((result["existence"], result["effective_date"], result["effective_precision"]),
                         ("demolished", "2026-12-01", "day"))
        self.assertTrue(result["evidence"].startswith("basis=대장 말소 자동 확정; note="))
        self.assertNotIn("successor_id", result)
        self.assertEqual([(r["complex_id"], r["동수"], r["세대수"], r["첫말소일"], r["마지막계약일"], r["이전판정"])
                          for r in auto], [(complex_id, "1", "12", "2026-12-01", "2019-11-14", "현존")])

    def hold_code(self, complex_id):
        results, auto, _ = self.run_month()
        self.assertEqual(auto, [])
        self.assertEqual(results[complex_id]["existence"], "pending")
        self.assertNotIn("effective_date", results[complex_id])
        return results[complex_id]["hold_code"]

    def test_a_contract_after_the_cancellation_is_held(self):
        complex_id = self.add(complex_(), state(cid()), last_trade="2026-12-05")
        self.before(current=[home()])
        self.cancelled()
        self.assertEqual(self.hold_code(complex_id), "말소후거래")

    def test_a_cancelled_contract_does_not_count(self):
        complex_id = self.add(complex_())
        self.trades.append(trade(complex_id, "2026-12-05", cancelled="t"))
        self.cancelled()
        self.assertEqual(self.run_month()[0][complex_id]["existence"], "demolished")

    def test_hold_codes_follow_the_failing_condition(self):
        second = gone(mgmShtregPk="S2", dongNm="나동")
        demolished = {"mainPurpsCdNm": "공동주택", "demolExtngDay": "20180101"}
        home_there = home(useAprDay="20250101")
        cases = {
            "기록없음": {"closed": []},
            "범위미확인": {"closed": [gone(shterGbCd="6")]},
            "단기말소": {"closed": [gone(useAprDay="20240101")], "built": "2024", "trade": "2025-01-01"},
            "철거후거래": {"closed": [gone()], "demolition": [demolished]},
            # A different 동 of the same year was removed while another one stands.
            "일부존치": {"closed": [gone(dongNm="나동", hhldCnt="30", useAprDay="20070301")], "current": [home(bldNm="남은동")]},
            "수집미완": {"closed": [gone()], "closed_land": None},
        }
        for number, (code, case) in enumerate(cases.items(), start=500):
            built, last = case.pop("built", "2007"), case.pop("trade", "2019-11-14")
            lot = str(number)
            self.add(complex_(lot, code, built), last_trade=last)
            registers = {"closed_outline": [], "closed_land": [], **case}
            if registers["closed_land"] is None:
                del registers["closed_land"]
            self.registers(parcel(lot), **registers)
        # A standing building of another year is neither gone nor 현존.
        self.add(complex_("600", "다른연도", "1990"))
        self.registers(parcel("600"), current=[home_there], closed=[gone(useAprDay="19900101")])
        self.add(complex_("601", "총괄"))
        self.registers(parcel("601"), closed=[gone(), second], closed_outline=[], closed_land=[])
        results, auto, _ = self.run_month()
        found = {key.split("|")[-1]: value.get("hold_code") for key, value in results.items()}
        self.assertEqual(found, {**{code: code for code in cases}, "다른연도": "범위미확인", "총괄": "범위미확인"})
        self.assertEqual(auto, [])

    def test_several_buildings_need_a_matching_cancelled_outline(self):
        complex_id = self.add(complex_())
        self.cancelled(closed=[gone(), gone(mgmShtregPk="S2", dongNm="나동")],
                       closed_outline=[{"shterGbCd": "2", "hhldCnt": "24", "mainBldCnt": "2"}])
        results, auto, _ = self.run_month()
        self.assertEqual((results[complex_id]["existence"], auto[0]["동수"], auto[0]["세대수"]),
                         ("demolished", "2", "24"))

    def test_a_reissued_register_is_not_a_cancellation(self):
        complex_id = self.add(complex_("700", "재발급"))
        # The same 동 and approval date stand as an office today, so the cancellation re-issued the register.
        office = home(mainPurpsCdNm="업무시설", etcPurps="", useAprDay="20070101")
        self.cancelled(parcel("700"), current=[office])
        target = {"complex_id": complex_id, "name": "재발급", "built": "2007", "address": f"{DONG} 700",
                  "parcel_key": parcel("700"), "trades": 1, "last_trade": "2019-11-14"}
        found, code, note = monthly.cancellation(target, self.cache, DAY, ({}, [], {}, {}))
        self.assertEqual((found, code), (None, "범위미확인"))
        self.assertIn("재발급", note)

    def test_survivors_and_namesakes_hold_the_cancellation(self):
        attached = {"atchSigunguCd": "11380", "atchBjdongCd": "10100", "atchPlatGbCd": "0",
                    "atchBun": "0346", "atchJi": "0001"}
        survivor = self.add(complex_())
        self.cancelled(closed_land=[attached])
        self.registers(parcel("346-1"), current=[home(bldNm="남은동")])
        namesake = self.add(complex_("800", "한마음"))
        self.cancelled(parcel("800"), closed=[gone(bldNm="한마음")])
        put(self.reference, "kapt_info", {"kaptCode": "A1"},
            [{"kaptCode": "A1", "bjdCode": "1138010100", "kaptName": "한마음아파트", "kaptUsedate": "20070301"}])
        results, auto, _ = self.run_month()
        self.assertEqual((results[survivor]["hold_code"], results[namesake]["hold_code"], auto),
                         ("일부존치", "타지번잔존", []))

    def test_project_demolition_before_the_last_trade_comes_from_the_reference_cache(self):
        complex_id = self.add(complex_())
        self.cancelled()
        zones = {"C": (f"{DONG.split()[-1]} 345-4 일대", "s")}
        with patch.object(monthly, "project_pages",
                          return_value=(zones, {"C": [("2019-01-01", "h")]})) as pages:
            results, _, _ = self.run_month()
        pages.assert_called_once_with(self.reference)
        self.assertEqual(results[complex_id]["hold_code"], "철거후거래")

    def test_project_pages_read_the_saved_cleanup_pages(self):
        documents = self.reference / "public_documents"
        documents.mkdir(parents=True)
        pages = (("a", "mastr-cleanup-bsnsSumry", "<table><tr><th>정비구역 위치</th><td>수색동 345-4 일대</td></tr></table>"),
                 ("b", "cleanup-prtnelapse", "<script>x</script><td>2019- 01-01</td><td>[철거완료]</td>"))
        for name, kind, body in pages:
            (documents / f"{name}.json").write_text(json.dumps(
                {"url": f"https://cleanup.seoul.go.kr/{kind}.do?cafeId=1", "charset": "utf-8"}), encoding="utf-8")
            (documents / f"{name}.bin").write_bytes(body.encode("utf-8"))
        zones, removals = monthly.project_pages(self.reference)
        self.assertEqual((zones["1"][0], [day for day, _ in removals["1"]]), ("수색동 345-4 일대", ["2019-01-01"]))

    def test_the_same_open_hold_is_not_repeated(self):
        complex_id = self.add(complex_(), held(cid(), "기록없음"))
        self.registers()
        results, _, review = self.run_month()
        self.assertEqual((results, self.kinds(review)), ({}, ["보류(변화 없음)"]))
        self.assertIn(complex_id, review[0]["complex_id"])
        holds = csv_rows(self.out / "reports" / BATCH / "holds.csv")
        self.assertEqual([row["complex_id"] for row in holds], [complex_id])

    def test_a_changed_hold_and_a_resolved_hold_are_reported(self):
        changed = self.add(complex_(), held(cid(), "수집미완"))
        resolved = self.add(complex_("900", "해결"), state(
            cid("900", "해결"), pending_hold_code="범위미확인", pending_as_of="2026-11-27"))
        self.registers()
        self.registers(parcel("900"), current=[home()])
        results, _, _ = self.run_month()
        self.assertEqual((results[changed]["hold_code"], results[resolved]["existence"]), ("기록없음", "present"))

    def test_a_hold_closed_by_a_later_confirmation_is_not_open(self):
        old = state(cid(), pending_hold_code="범위미확인", pending_as_of="2026-10-01")
        self.assertIsNone(monthly.active_hold(old))
        self.assertEqual(monthly.branch(old), "present")
        self.assertEqual(monthly.active_hold({**old, "pending_as_of": "2026-12-01"}), "범위미확인")
        self.assertEqual(monthly.branch(held(cid(), "x")), "held")
        # The same code is reported again, because Jipfit no longer counts the old hold.
        complex_id = self.add(complex_(), old)
        self.before(current=[home(useAprDay="19900101")])
        self.registers(current=[home(useAprDay="19900101"), home(useAprDay="19910101", mgmBldrgstPk="C2")])
        self.assertEqual(self.run_month()[0][complex_id]["hold_code"], "범위미확인")

    def test_a_result_older_than_the_jipfit_judgment_is_skipped(self):
        self.add(complex_(), state(cid(), as_of="2026-12-30"))
        self.before(current=[home()])
        self.cancelled()
        results, auto, review = self.run_month()
        self.assertEqual((results, auto, self.kinds(review)), ({}, [], ["지문에서 사라진 기록", "기준일 충돌"]))
        self.assertEqual(self.saved_fingerprints()["parcels"][KEY]["closed"], [])

    def test_a_hold_dated_like_the_confirmation_is_not_sent(self):
        # Jipfit would count such a hold as already closed, and the new record would not be judged again.
        self.add(complex_(), state(cid(), as_of=DAY.isoformat()))
        self.before(current=[home(useAprDay="19900101")])
        self.registers(current=[home(useAprDay="19900101"), home(useAprDay="19910101", mgmBldrgstPk="C2")])
        results, _, review = self.run_month()
        self.assertEqual((results, self.kinds(review)), ({}, ["기준일 충돌"]))
        self.assertEqual(len(self.saved_fingerprints()["parcels"][KEY]["current"]), 1)


class SuccessorTests(Month):
    def demolished(self, lot="345-4", name="제일", **fields):
        values = {"evidence": "basis=수동 확인표; note=옛 단지 확인", "effective_date": "2020-04-20",
                  "effective_precision": "day", **fields}
        complex_id = self.add(complex_(lot, name), state(cid(lot, name), "demolished", **values))
        self.registers(parcel(lot), closed=[gone(shterDay="20200420")])
        return complex_id

    def successor(self, lot, name, built="2023"):
        complex_id = self.add(complex_(lot, name, built), state(cid(lot, name)), last_trade="")
        if not cache_file(self.cache, "current", parcel_params(parcel(lot))).exists():
            self.registers(parcel(lot), current=[home(useAprDay=f"{built}0101")])
            self.before(parcel(lot), current=[home(useAprDay=f"{built}0101")])
        return complex_id

    def test_the_only_later_complex_on_the_lot_is_linked_and_the_judgment_is_carried(self):
        old = self.demolished(evidence="basis=전수 조사 검토; trade_caution_from=2019-12-13; note=착공 후 거래",
                              effective_date=None, effective_precision=None)
        new = self.add(complex_("345-4", "새단지", "2023"), state(cid("345-4", "새단지")), last_trade="")
        self.registers(current=[home(useAprDay="20230101")], closed=[gone(shterDay="20200420")])
        result = self.run_month()[0][old]
        self.assertEqual((result["existence"], result["successor_id"], result["successor_status"]),
                         ("demolished", new, "linked"))
        self.assertNotIn("effective_date", result)
        self.assertTrue(result["evidence"].startswith(
            "basis=전수 조사 검토; trade_caution_from=2019-12-13; successor_name=새단지; note=후속 연결(같은 지번)"))
        self.assertTrue(result["evidence"].endswith("이전 근거: 착공 후 거래"))

    def test_the_effective_date_is_repeated(self):
        old = self.demolished(evidence="현장 확인")
        self.add(complex_("345-4", "새단지", "2023"), last_trade="")
        self.registers(current=[home(useAprDay="20230101")], closed=[gone(shterDay="20200420")])
        result = self.run_month()[0][old]
        self.assertEqual((result["effective_date"], result["effective_precision"]), ("2020-04-20", "day"))
        self.assertIn("basis=월간 후속 연결", result["evidence"])
        self.assertIn("이전 근거: 현장 확인", result["evidence"])

    def test_recorded_names_are_linked_only_when_each_is_found_once(self):
        evidence = "basis=수동 확인표; successor_name=새1단지 · 새2단지; note=n"
        old = self.demolished(evidence=evidence)
        first = self.successor("10", "새1단지")
        self.assertEqual(self.run_month()[0], {})
        second = self.successor("11", "새2단지")
        results, _, _ = self.run_month(DAY + timedelta(days=1))
        self.assertEqual(results[old]["successor_id"], first)
        self.assertIn(f"함께 기록된 후속: {second}", results[old]["evidence"])
        self.assertIn("successor_name=새1단지 · 새2단지", results[old]["evidence"])

    def test_two_candidates_or_an_earlier_one_are_not_linked(self):
        old = self.demolished(evidence="basis=수동 확인표; successor_name=새단지; note=n")
        self.successor("10", "새단지")
        self.successor("11", "새단지아파트")
        self.successor("12", "이전단지", "2019")
        results, _, review = self.run_month()
        self.assertEqual(results, {})
        self.assertIn(old, [r["complex_id"] for r in review if r["구분"] == "후속 연결 확인"])

    def test_a_demolished_candidate_and_an_existing_link_are_left_alone(self):
        old = self.demolished()
        self.add(complex_("345-4", "함께", "2021"),
                 state(cid("345-4", "함께"), "demolished"), last_trade="")
        self.assertEqual(self.run_month()[0], {})
        self.add(complex_("345-4", "새단지", "2023"), last_trade="")
        self.history = [{"complex_id": old, "existence": "demolished", "successor_id": "x",
                         "applied_status": "applied"}]
        results, _, _ = self.run_month(DAY + timedelta(days=1))
        self.assertNotIn(old, results)
        self.assertEqual(monthly.linked_ids(self.history), {old})

    def test_a_new_confirmation_carries_its_successor(self):
        old = self.add(complex_(), state(cid()))
        new = self.add(complex_("345-4", "새단지", "2026"), last_trade="")
        self.before(current=[home()])
        self.cancelled()
        results, auto, _ = self.run_month()
        self.assertEqual((results[old]["existence"], results[old]["successor_id"], results[old]["successor_status"]),
                         ("demolished", new, "linked"))
        self.assertIn("successor_name=새단지; ", results[old]["evidence"])
        self.assertEqual(auto[0]["후속단지ID"], new)


class BundleTests(Month):
    def setUp(self):
        super().setUp()
        self.gone = self.add(complex_(), state(cid()))
        self.here = self.add(complex_("900", "남음"))
        self.before(current=[home()])
        self.cancelled()
        self.registers(parcel("900"), current=[home(bldNm="남음")])

    def test_the_manifest_covers_the_results_and_the_bundle_appears_whole(self):
        outcome = stage.evaluate(self.tables(), self.fingerprints(), self.cache, self.reference, DAY, "v")
        batches, reports, names = self.root / "b", self.root / "r", []

        def before_publish(batch):
            names.extend(sorted(path.name for path in batches.iterdir()))
            names.append(batch)

        folder = monthly.write_batch({**outcome, "proposals": []}, batches, reports, DAY, "v", before_publish)
        # While the caller records the name, only the hidden folder apply_inbox skips exists.
        self.assertEqual(names, [f".{BATCH}", BATCH])
        self.assertEqual([p.name for p in batches.iterdir()], [BATCH])
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["fingerprints.json", "manifest.json", "proposals.jsonl", "results.jsonl"])
        self.assertEqual(sorted(p.name for p in (reports / BATCH).iterdir()),
                         ["auto_confirmed.csv", "holds.csv", "report.md", "review.csv"])
        body = (folder / "results.jsonl").read_bytes()
        manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
        self.assertEqual((manifest["sha256"], manifest["results"], manifest["counts"]),
                         (sha256(body).hexdigest(), 2, {"demolished": 1, "present": 1}))
        as_of, results = inbox.parse_bundle(BATCH, *inbox.read_files(folder))
        self.assertEqual((as_of, len(results)), (DAY, 2))
        self.assertEqual(inbox.read_fingerprints(folder)["as_of"], DAY.isoformat())
        with self.assertRaises(ValueError):
            monthly.write_batch({**outcome, "proposals": []}, batches, reports, DAY, "v")

    def test_confirmations_within_the_limit_are_published(self):
        outcome = stage.evaluate(self.tables(), self.fingerprints(), self.cache, self.reference, DAY, "v")
        guarded = monthly.hold_mass_confirmations(outcome, {}, 1)
        self.assertEqual((guarded["results"], guarded["proposals"]), (outcome["results"], []))

    def test_too_many_confirmations_all_become_holds_with_the_originals_kept(self):
        second = self.add(complex_("500", "둘째"), state(cid("500", "둘째")))
        self.before(parcel("500"), current=[home()])
        self.cancelled(parcel("500"))
        outcome = stage.evaluate(self.tables(), self.fingerprints(), self.cache, self.reference, DAY, "v")
        guarded = monthly.hold_mass_confirmations(outcome, {s["complex_id"]: s for s in self.states}, 1)
        results = {r["complex_id"]: r for r in guarded["results"]}
        for complex_id in (self.gone, second):
            result = results[complex_id]
            self.assertEqual((result["existence"], result["hold_code"], result["result_key"]),
                             ("pending", "대량확정보류", f"{BATCH}:{complex_id}"))
            self.assertNotIn("effective_date", result)
            self.assertTrue(result["evidence"].startswith("basis=월간 판정 보류; note=대량확정보류: "))
        self.assertEqual(results[self.here]["existence"], "present")
        originals = {r["complex_id"]: r for r in outcome["results"]}
        self.assertEqual(guarded["proposals"], [
            {**originals[c], "result_key": f"proposal-{BATCH}:{c}"} for c in sorted((self.gone, second))])
        self.assertEqual((len(guarded["auto"]), guarded["summary"]["결과"]), (2, Counter(pending=2, present=1)))
        batches = self.root / "b"
        folder = monthly.write_batch(guarded, batches, self.root / "r", DAY, "v")
        self.assertEqual(lines(folder / "proposals.jsonl"), guarded["proposals"])
        report = (self.root / "r" / BATCH / "report.md").read_text("utf-8")
        self.assertIn("대량확정보류. 확인한 뒤 lifecycle_admin.py accept", report)

    def test_a_hold_jipfit_already_has_is_not_repeated_by_the_guard(self):
        complex_id = self.gone
        self.states[0].update(pending_hold_code="대량확정보류", pending_as_of="2026-11-27")
        outcome = stage.evaluate(self.tables(), self.fingerprints(), self.cache, self.reference, DAY, "v")
        guarded = monthly.hold_mass_confirmations(outcome, {s["complex_id"]: s for s in self.states}, 0)
        self.assertEqual([r["complex_id"] for r in guarded["results"]], [self.here])
        self.assertEqual([r["complex_id"] for r in guarded["proposals"]], [complex_id])
        self.assertIn((complex_id, "보류(변화 없음)"), {(r["complex_id"], r["구분"]) for r in guarded["review"]})


class FingerprintTests(Month):
    def test_baseline_fingerprints_read_the_reference_cache(self):
        self.before(current=[home()], closed=[gone(shterDay="20100101", dongNm="다동", hhldCnt="20")])
        document = monthly.baseline_fingerprints(self.reference, BASE_DAY)
        self.assertEqual((document["as_of"], list(document["parcels"])), ("2026-11-26", [KEY]))
        found = document["parcels"][KEY]
        self.assertEqual((found["closed"], found["current"], found["demolition"]),
                         ([["S1", "2", "20100101"]], [["C1", "20070101", "12"]], []))
        self.assertEqual(len(found["sha256"]), 64)
        self.assertEqual(store.parse_fingerprints(json.dumps(document).encode()), document)

    def test_a_malformed_fingerprint_document_is_refused(self):
        for body in (b"{", b"[]", b'{"as_of": "x", "parcels": {}}', b'{"as_of": "2026-12-01", "parcels": []}',
                     b'{"as_of": "2026-12-01", "parcels": {"k": {"sha256": "a"}}}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                store.parse_fingerprints(body)


# Official API calls

def answer(rows, total=None, page=1, status="00"):
    return json.dumps({"response": {"header": {"resultCode": status}, "body": {
        "items": {"item": rows}, "totalCount": len(rows) if total is None else total, "pageNo": page}}}).encode()


class SourceTests(Folders):
    def setUp(self):
        super().setUp()
        self.patch(patch.object(sources, "today", return_value=DAY))
        self.sleeps = self.patch(patch.object(collect.time, "sleep"))

    def fetch(self, *replies, calls=None):
        urls = []
        queue = list(replies)

        def request(url):
            urls.append(url)
            reply = queue.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(sources, "request", request):
            outcome = collect.fetch_all(calls or [("current", parcel_params(KEY))], self.cache,
                                        {"MOLIT_SERVICE_KEY": "s%2Bkey"}, 1)
        return outcome, urls

    def snapshot(self, service="current"):
        return sources.cached(self.cache, service, parcel_params(KEY))

    def test_pages_are_joined_into_one_snapshot_dated_today_in_korea(self):
        rows = [{"n": str(i)} for i in range(101)]
        outcome, urls = self.fetch(answer(rows[:100], 101), answer(rows[100:], 101, 2))
        self.assertEqual(outcome, Counter({"현행 완료": 1}))
        self.assertEqual(self.snapshot(), {"retrieved_on": "2026-12-28", "total": 101, "rows": rows})
        self.assertIn("serviceKey=s%2Bkey", urls[0])
        self.assertIn("pageNo=2&numOfRows=100", urls[1])

    def test_brief_failures_are_asked_again_and_other_ones_leave_no_file(self):
        outcome, urls = self.fetch(SourceError("HTTP 503"), b"", answer([]))
        self.assertEqual((outcome, len(urls)), (Counter({"현행 완료": 1}), 3))
        cache_file(self.cache, "current", parcel_params(KEY)).unlink()
        outcome, urls = self.fetch(SourceError("HTTP 500"))
        self.assertEqual((outcome, len(urls), self.snapshot()), (Counter({"현행 HTTP 500": 1}), 1, None))

    def test_an_unreadable_response_is_kept_with_the_key_masked(self):
        outcome, _ = self.fetch(b'{"broken": "s+key"}')
        self.assertEqual(outcome, Counter({"현행 응답 형식 불일치": 1}))
        (saved,) = (self.cache / "format_errors").iterdir()
        self.assertEqual(saved.read_bytes(), b'{"broken": "***"}')

    def test_the_key_comes_from_the_environment(self):
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "from-env"}):
            self.assertEqual(sources.credentials(), {"MOLIT_SERVICE_KEY": "from-env"})
        with patch.object(sources, "request") as request:
            outcome = collect.fetch_all([("current", parcel_params(KEY))], self.cache, {"MOLIT_SERVICE_KEY": None}, 1)
        self.assertEqual(outcome, Counter({"현행 인증키 미설정": 1}))
        request.assert_not_called()

    def test_only_a_fresh_or_this_months_cache_is_written(self):
        reference = self.reference
        used = self.root / "used"
        put(used, "current", parcel_params(KEY), [])
        for folder in (reference, reference.parent, reference / "current", used):
            with self.subTest(folder=folder.name), self.assertRaises(ValueError):
                monthly.prepare_cache(folder, reference, DAY)
        monthly.prepare_cache(self.cache, reference, DAY)
        put(self.cache, "current", parcel_params(KEY), [])
        monthly.prepare_cache(self.cache, reference, DAY + timedelta(days=6))
        self.assertIsNotNone(self.snapshot())
        # A week-old cache holds registers too old to judge with, so it is collected again.
        monthly.prepare_cache(self.cache, reference, DAY + timedelta(days=7))
        self.assertEqual([p.name for p in self.cache.iterdir()], ["monthly.json"])
        with self.assertRaises(ValueError):
            monthly.prepare_cache(self.cache, reference, DAY)


# The stage and the operator commands

class StageDB(InboxDB):
    """InboxDB plus the complex rows, the run and fingerprint tables and the operator queries."""

    def __init__(self):
        super().__init__(complexes=())
        self.data.update(meta={}, trade_rows=[], runs={}, fingerprints={}, reasons={}, count=0)
        self.locked = False

    def add(self, row, trades=("2019-11-14",), coordinates=True):
        complex_id = row["complex_id"]
        self.data["complexes"].add(complex_id)
        self.data["meta"][complex_id] = {**row, "coordinates": coordinates}
        for day in trades:
            self.add_trade(complex_id, day)
        return complex_id

    def add_trade(self, complex_id, day, cancelled=False):
        self.data["trade_rows"].append((complex_id, day, "t" if cancelled else "f"))
        if not cancelled:
            self.data["contracts"].setdefault(complex_id, []).append(date.fromisoformat(day))

    def confirm(self, complex_id, existence="present", day=BASE_DAY, evidence="basis=t; note=기준", effective=None):
        result = {"result_key": f"baseline-20261126:{complex_id}", "complex_id": complex_id, "existence": existence,
                  "as_of": day.isoformat(), "verifier_version": "v", "evidence": evidence}
        if effective:
            result.update(effective_date=effective, effective_precision="day")
        inbox.apply_lifecycle_results(self, [result])

    def hold(self, complex_id, code, day=BASE_DAY):
        inbox.apply_lifecycle_results(self, [{
            "result_key": f"hold-{day:%Y%m%d}:{complex_id}", "complex_id": complex_id, "existence": "pending",
            "as_of": day.isoformat(), "verifier_version": "v", "hold_code": code,
            "evidence": f"basis=월간 판정 보류; note={code}: 설명"}])

    def state(self, complex_id):
        return self.data["state"].get(complex_id)

    def public_view(self, complex_id):
        """What the API shows: the judgment, its copy, the marker and the parts of metadata_revision."""
        row = self.state(complex_id) or {}
        confirmed = (row.get("existence"), row.get("result_key"), row.get("as_of")) if row.get("existence") else None
        return (row.get("existence"), self.data["status"].get(complex_id),
                row.get("existence") == "demolished", confirmed)

    @contextmanager
    def transaction(self, savepoint_name=None, force_rollback=False):
        before = deepcopy(self.data)
        with super().transaction():
            yield self
        if force_rollback:
            self.data = before

    def cursor(self):
        database = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def copy(self, query):
                yield SimpleNamespace(rows=lambda: iter(database.copy_rows(query)))

        return Cursor()

    def copy_rows(self, query):
        self.log.append((" ".join(query.split()), None))
        data = self.data
        (table, columns), = [(name, cols) for name, (cols, sql) in store.READS.items() if sql == query]
        if table == "complexes":
            return [tuple(data["meta"][c][k] for k in columns) for c in sorted(data["meta"])]
        if table == "trades":
            return list(data["trade_rows"])
        if table == "complex_lifecycle_state":
            rows = [{"complex_id": c, **s} for c, s in sorted(data["state"].items())]
        else:
            rows = sorted(data["results"].values(), key=lambda r: r["complex_id"])
        return [tuple(None if row.get(k) is None else str(row[k]) for k in columns) for row in rows]

    def execute(self, sql, params=None):
        check_placeholders(sql, params)
        q = " ".join(sql.split())
        data = self.data
        handled = self.handle(q, params, data)
        if handled is None:
            return super().execute(sql, params)
        self.log.append((q, params))
        return handled

    def handle(self, q, params, data):
        if q.startswith("SET "):
            return Result()
        if q.startswith("SELECT pg_try_advisory_lock"):
            return Result([(not self.locked,)])
        runs = data["runs"]
        if q.startswith("SELECT status, attempts, batch FROM complex_lifecycle_runs"):
            run = runs.get(params[0])
            return Result([(run["status"], run["attempts"], run["batch"])] if run else [])
        if q.startswith("INSERT INTO complex_lifecycle_runs"):
            run = runs.setdefault(params[0], {"status": "collecting", "attempts": 0, "batch": None,
                                              "remaining": None, "error": None, "summary": {}})
            run.update(attempts=run["attempts"] + 1, error=None)
            return Result([(run["attempts"],)])
        if q.startswith("UPDATE complex_lifecycle_runs SET remaining_calls"):
            remaining, error, month = params
            if runs[month]["status"] != "applied":
                runs[month].update(error=error, **({} if remaining is None else {"remaining": remaining}))
            return Result()
        if q.startswith("UPDATE complex_lifecycle_runs SET batch"):
            batch, as_of, month = params
            runs[month].update(batch=batch, as_of=as_of)
            return Result()
        if q.startswith("UPDATE complex_lifecycle_runs SET status = 'judged'"):
            remaining, summary, month = params
            if runs[month]["status"] == "collecting":
                runs[month].update(status="judged", error=None,
                                   **({} if remaining is None else {"remaining": remaining}),
                                   **({} if summary is None else {"summary": summary.obj}))
            return Result()
        if q.startswith("UPDATE complex_lifecycle_runs SET status = 'applied'"):
            for run in runs.values():
                if run["batch"] == params[0]:
                    run.update(status="applied", error=None)
            return Result()
        prints = data["fingerprints"]
        if q.startswith("SELECT parcel_key, as_of, digest, records FROM complex_lifecycle_fingerprints"):
            return Result([(key, row["as_of"], row["digest"], row["records"]) for key, row in prints.items()])
        if q.startswith("SELECT count(*) FROM complex_lifecycle_fingerprints"):
            return Result([(len(prints),)])
        if q.startswith("INSERT INTO complex_lifecycle_fingerprints"):
            as_of, batch, parcels = params
            day = date.fromisoformat(as_of)
            for key, value in parcels.obj.items():
                old = prints.get(key)
                if old is None or (old["digest"] != value["sha256"] and old["as_of"] <= day):
                    prints[key] = {"as_of": day, "digest": value["sha256"], "batch": batch,
                                   "records": {k: v for k, v in value.items() if k != "sha256"}}
            return Result()
        if q.startswith("INSERT INTO complex_lifecycle_results("):
            data["count"] += 1
            names = ("result_key", "complex_id", "existence", "as_of", "verifier_version", "evidence", "digest",
                     "hold_code", "correction_of", "successor_id", "successor_status", "effective_date",
                     "effective_precision")
            data["results"][params[0]] = {
                **dict(zip(names, params)), "order": data["count"],
                "applied_status": "applied" if "'applied'" in q else "pending_recorded"}
            return Result()
        empty = dict.fromkeys(("result_key", "existence", "as_of", "evidence", "effective_date",
                               "effective_precision", "pending_result_key", "pending_hold_code", "pending_as_of"))
        if q.startswith("INSERT INTO complex_lifecycle_state( complex_id, pending_result_key"):
            complex_id, key, code, as_of = params
            data["state"].setdefault(complex_id, dict(empty)).update(
                pending_result_key=key, pending_hold_code=code, pending_as_of=as_of)
            return Result()
        if q.startswith("INSERT INTO complex_lifecycle_state( complex_id, result_key, existence"):
            complex_id, key, existence, as_of, _, evidence, effective, precision = params
            data["state"].setdefault(complex_id, dict(empty)).update(
                result_key=key, existence=existence, as_of=as_of, evidence=evidence, effective_date=effective,
                effective_precision=precision)
            return Result()
        if q.startswith("SELECT c.name, s.result_key"):
            complex_id = params[0]
            if complex_id not in data["meta"]:
                return Result()
            row = data["state"].get(complex_id, empty)
            since, reason = data["exclusions"].get(complex_id, (None, None))
            return Result([(data["meta"][complex_id]["name"], *(row.get(k) for k in (
                "result_key", "existence", "as_of", "evidence", "effective_date", "effective_precision",
                "pending_result_key", "pending_hold_code", "pending_as_of")), since, reason)])
        if q.startswith("SELECT name FROM complexes WHERE complex_id"):
            found = data["meta"].get(params[0])
            return Result([(found["name"],)] if found else [])
        if q.startswith("SELECT result_key, existence, evidence FROM complex_lifecycle_state"):
            row = data["state"].get(params[0])
            return Result([(row["result_key"], row["existence"], row["evidence"])]
                          if row and row["existence"] else [])
        if q.startswith("SELECT status FROM complex_lifecycle_batches WHERE batch"):
            found = data["batches"].get(params[0])
            return Result([(found[1],)] if found else [])
        if q.startswith("SELECT reason FROM complex_lifecycle_quarantine"):
            return Result([(row[3],) for row in data["quarantine"] if (row[0], row[1]) == tuple(params)])
        if q.startswith("SELECT p.result_key, p.existence"):
            key, complex_id = params
            limit = data["results"][key]["order"]
            earlier = [r for r in data["results"].values() if r["complex_id"] == complex_id
                       and r["applied_status"] == "applied" and r["existence"] != "pending" and r["order"] < limit]
            if not earlier:
                return Result()
            found = max(earlier, key=lambda r: r["order"])
            return Result([tuple(found[k] for k in (
                "result_key", "existence", "evidence", "effective_date", "effective_precision", "successor_id",
                "successor_status"))])
        if q.startswith("SELECT s.complex_id, c.name, c.address, s.existence"):
            rows = []
            for complex_id, row in sorted(data["state"].items(),
                                          key=lambda item: (item[1]["pending_hold_code"] or "", item[0])):
                if not row["pending_hold_code"]:
                    continue
                meta = data["meta"][complex_id]
                record = data["results"].get(row["pending_result_key"], {})
                rows.append((complex_id, meta["name"], meta["address"], row["existence"], row["as_of"],
                             row["pending_hold_code"], row["pending_as_of"], record.get("evidence"),
                             meta["coordinates"] and bool(data["contracts"].get(complex_id))))
            return Result(rows)
        return None


class Stage(Folders):
    """run_month on the mock database; the API answers come from self.answers."""

    def setUp(self):
        super().setUp()
        self.db = StageDB()
        self.clock = DAY
        self.answers, self.failing, self.calls = {}, set(), []
        self.patch(patch.object(inbox.psycopg, "connect", self.db.connect))
        self.patch(patch.object(sources, "today", lambda: self.clock))
        self.patch(patch.object(sources, "fetch", self.fetch))
        self.patch(patch.dict(os.environ, {"JIPFIT_LIFECYCLE_DATA": str(self.data), "MOLIT_SERVICE_KEY": "k",
                                           "JIPFIT_LIFECYCLE_AUTO": "1"}))
        for name in ("JIPFIT_LIFECYCLE_DIR", "JIPFIT_LIFECYCLE_COLLECT_MINUTES", "JIPFIT_LIFECYCLE_MAX_AUTO"):
            os.environ.pop(name, None)
        self.cache.rmdir()  # run_month prepares it.

    def fetch(self, cache, _keys, service, params, *, refresh=False, ask=None, parse=None):
        if cache_file(cache, service, params).exists() and not refresh:
            return None
        name = params.get("bun") or params["DEAL_YMD"]
        self.calls.append((service, name))
        if (service, name) in self.failing:
            raise SourceError("HTTP 429")
        put(cache, service, params, self.answers.get((service, name), []), self.clock)
        path = cache_file(cache, service, params)
        sources.note_write(path, path.read_text(encoding="utf-8"))  # As the real fetch does.

    def seed(self):
        document = monthly.baseline_fingerprints(self.reference, BASE_DAY)
        if not document["parcels"]:
            self.before(parcel("999"), current=[home()])
            document = monthly.baseline_fingerprints(self.reference, BASE_DAY)
        store.save_fingerprints(self.db, document, "seed-20261126")

    def gone_complex(self, lot="345-4", name="제일"):
        """A standing complex of the baseline whose registers are cancelled this month."""
        complex_id = self.db.add(complex_(lot, name))
        self.db.confirm(complex_id)
        self.before(parcel(lot), current=[home()])
        bun = parcel_params(parcel(lot))["bun"]
        self.answers[("closed", bun)] = [gone()]
        return complex_id

    def new_complex(self, lot="900", name="새로", standing=True):
        complex_id = self.db.add(complex_(lot, name))
        if standing:
            self.answers[("current", parcel_params(parcel(lot))["bun"])] = [home(bldNm=name)]
        return complex_id

    def run_stage(self, day=DAY, **options):
        self.clock = max(self.clock, day)
        return lifecycle_check.run_month(day, workers=1, **options)

    def run_row(self):
        return self.db.data["runs"].get(MONTH)

    def batches(self):
        folder = self.data / "batches"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def prints(self):
        return self.db.data["fingerprints"]


class StageTests(Stage):
    def test_nothing_happens_while_the_automatic_check_is_off(self):
        for value in (None, "0", "true"):
            with self.subTest(value=value), patch.dict(os.environ, {} if value is None else
                                                       {"JIPFIT_LIFECYCLE_AUTO": value}):
                if value is None:
                    os.environ.pop("JIPFIT_LIFECYCLE_AUTO")
                with patch.object(refresh, "run_month") as run:
                    self.assertEqual(refresh.run_lifecycle_month(DAY), [])
                run.assert_not_called()
        self.assertEqual((self.db.connects, self.db.log, self.calls), ([], [], []))

    def test_errors_of_the_automatic_check_join_the_failed_list(self):
        with patch.object(refresh, "run_month", side_effect=RuntimeError("x")):
            self.assertEqual(refresh.run_lifecycle_month(DAY), ["lifecycle:RuntimeError"])

    def test_a_month_is_collected_judged_applied_and_closed(self):
        old = self.gone_complex()
        new = self.new_complex()
        self.seed()
        before = {p: p.read_bytes() for p in self.reference.rglob("*.json")}
        self.assertEqual(self.run_stage(), [])
        self.assertEqual({p: p.read_bytes() for p in self.reference.rglob("*.json")}, before)
        self.assertEqual(sorted(set(self.calls)), sorted([
            ("current", "0345"), ("closed", "0345"), ("demolition", "0345"), ("closed_outline", "0345"),
            ("closed_land", "0345"), ("current", "0900"), ("closed", "0900"), ("demolition", "0900")]))
        self.assertEqual((self.db.state(old)["existence"], self.db.state(new)["existence"]),
                         ("demolished", "present"))
        self.assertEqual(self.db.data["status"], {old: "demolished", new: "present"})
        run = self.run_row()
        self.assertEqual((run["status"], run["attempts"], run["batch"], run["remaining"]), ("applied", 1, BATCH, 0))
        self.assertEqual((run["summary"]["대상"], run["summary"]["자동 확정"], run["summary"]["현존 확인"]), (2, 1, 1))
        self.assertEqual(self.db.data["batches"][BATCH][1], "applied")
        self.assertEqual((self.prints()[KEY]["batch"], self.prints()[KEY]["records"]["current"]), (BATCH, []))
        result = self.db.data["results"][f"{BATCH}:{old}"]
        self.assertTrue(result["verifier_version"].startswith("jipfit/lifecycle_check@1+"))
        self.assertEqual(len(result["verifier_version"].split("+")[1]), 12)

    def test_the_log_line_summarises_the_month(self):
        self.gone_complex()
        self.new_complex()
        self.seed()
        with patch("builtins.print") as output:
            self.run_stage()
        printed = [str(call.args[0]) for call in output.call_args_list]
        self.assertIn(f"Lifecycle {BATCH}: 대상 2 / 수집 완료 / 현존 확인 1 / 자동 확정 1 / 보류 0(운영자 확인 0) / "
                      "후속 연결 0", printed)

    def test_an_applied_or_disabled_month_does_nothing(self):
        self.seed()
        for status in ("applied", "disabled"):
            self.db.data["runs"][MONTH] = {"status": status, "attempts": 1, "batch": BATCH}
            self.db.log.clear()
            self.assertEqual(self.run_stage(), [])
            self.assertEqual([q.split()[1] for q, _ in self.db.log], ["pg_try_advisory_lock(%s)", "status,"])
            self.assertEqual((self.calls, self.batches()), ([], []))

    def test_a_second_run_at_the_same_time_is_refused(self):
        self.db.locked = True
        self.assertEqual(self.run_stage(), ["lifecycle:locked"])
        self.assertEqual((self.db.data["runs"], self.calls), ({}, []))

    def test_missing_prerequisites_use_no_attempt(self):
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": ""}):
            self.assertEqual(self.run_stage(), ["lifecycle:key"])
        self.assertEqual(self.run_stage(offline=True), ["lifecycle:cache"])
        (self.reference / "trade_detail").rename(self.reference / "moved")
        self.assertEqual(self.run_stage(), ["lifecycle:reference"])
        self.assertEqual(self.db.data["runs"], {})

    def test_a_check_without_fingerprints_fails_and_says_why(self):
        self.new_complex()
        with self.assertRaises(ValueError):
            self.run_stage()
        run = self.run_row()
        self.assertEqual((run["status"], run["attempts"]), ("collecting", 1))
        self.assertIn("seed-fingerprints", run["error"])
        self.assertEqual((self.calls, self.batches()), ([], []))

    def test_incomplete_collection_is_retried_and_the_fourth_attempt_judges(self):
        standing = self.db.add(complex_("700", "그대로"))
        self.db.confirm(standing)
        self.before(parcel("700"), current=[home()])
        new = self.new_complex()
        self.seed()
        self.failing = {(service, bun) for service in ("closed",) for bun in ("0700", "0900")}
        self.patch(patch.object(monthly.time, "sleep"))  # The pause before the same attempt asks again.
        for attempt in (1, 2, 3):
            self.assertEqual(self.run_stage(), ["lifecycle:collecting"])
            run = self.run_row()
            self.assertEqual((run["status"], run["attempts"], run["remaining"], run["error"]),
                             ("collecting", attempt, 2, "lifecycle:collecting"))
            self.assertEqual(self.batches(), [])
        # A retry only asks for what is still missing.
        self.calls.clear()
        self.assertEqual(self.run_stage(), [])
        # HTTP 429 is a brief failure, so each of the two is asked once more in the same attempt.
        self.assertEqual(sorted(self.calls), [("closed", "0700")] * 2 + [("closed", "0900")] * 2)
        run = self.run_row()
        self.assertEqual((run["status"], run["attempts"], run["remaining"]), ("applied", 4, 2))
        results = {r["complex_id"]: r for r in lines(self.data / "batches" / BATCH / "results.jsonl")}
        self.assertEqual(list(results), [new])
        self.assertEqual(results[new]["hold_code"], "수집미완")
        self.assertEqual(self.db.state(standing)["result_key"], f"baseline-20261126:{standing}")

    def test_the_time_limit_stops_the_collection_and_keeps_whole_files(self):
        for lot in ("700", "900"):
            self.new_complex(lot, lot)
        self.seed()
        clock = [0.0]
        fetch = self.fetch

        def slow(*args, **kwargs):
            fetch(*args, **kwargs)
            clock[0] += 3600

        with patch.object(sources, "fetch", slow), patch.object(collect.time, "monotonic", lambda: clock[0]):
            self.assertEqual(self.run_stage(), ["lifecycle:collecting"])
        # 90 minutes allow the call that starts at 0 and the one that starts at 60.
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.run_row()["remaining"], 4)
        files = list(self.cache.rglob("*.json"))
        self.assertEqual(len(files), 3)  # The marker and the two answers.
        for path in files:
            json.loads(path.read_text("utf-8"))
        self.assertEqual(list(self.cache.rglob("*.tmp")), [])
        self.calls.clear()
        self.assertEqual(self.run_stage(), [])
        self.assertEqual(len(self.calls), 4)

    def test_a_judged_month_only_applies_its_bundle(self):
        old = self.gone_complex()
        self.seed()
        with patch.object(stage, "apply_inbox", return_value=[BATCH]):
            self.assertEqual(self.run_stage(), [f"lifecycle:{BATCH}"])
        self.assertEqual(self.run_row()["status"], "judged")
        body = (self.data / "batches" / BATCH / "results.jsonl").read_bytes()
        self.calls.clear()
        with patch.object(stage, "evaluate") as evaluate:
            self.assertEqual(self.run_stage(DAY + timedelta(days=1)), [])
        evaluate.assert_not_called()
        self.assertEqual((self.calls, self.batches()), ([], [BATCH]))
        self.assertEqual((self.data / "batches" / BATCH / "results.jsonl").read_bytes(), body)
        self.assertEqual((self.run_row()["status"], self.db.state(old)["existence"]), ("applied", "demolished"))

    def test_a_stop_between_publishing_and_recording_is_resumed(self):
        self.gone_complex()
        self.seed()
        with patch.object(store, "mark_judged", side_effect=RuntimeError("stopped")), self.assertRaises(RuntimeError):
            self.run_stage()
        self.assertEqual((self.run_row()["status"], self.run_row()["batch"], self.batches()),
                         ("collecting", BATCH, [BATCH]))
        with patch.object(stage, "evaluate") as evaluate:
            self.assertEqual(self.run_stage(), [])
        evaluate.assert_not_called()
        self.assertEqual((self.run_row()["status"], self.run_row()["attempts"]), ("applied", 1))

    def test_an_error_while_applying_keeps_the_fingerprints_until_a_rerun_finishes(self):
        old = self.gone_complex()
        self.seed()
        seeded = deepcopy(self.prints())
        record = store.save_fingerprints

        def fail(db, document, batch):
            record(db, document, batch)
            raise inbox.psycopg.Error("connection lost")

        # The error comes after the fingerprints were written, inside the recording transaction.
        with patch.object(store, "save_fingerprints", fail):
            self.assertEqual(self.run_stage(), [f"lifecycle:{BATCH}"])
        self.assertEqual(self.db.data["batches"], {})
        self.assertEqual((self.prints(), self.run_row()["status"]), (seeded, "judged"))
        self.assertEqual(self.db.state(old)["existence"], "demolished")
        self.assertEqual(self.run_stage(), [])
        self.assertEqual(self.db.data["batches"][BATCH][2]["noop"], 1)
        self.assertEqual(self.db.data["batches"][BATCH][2]["applied"], 0)
        self.assertEqual((self.prints()[KEY]["batch"], self.run_row()["status"]), (BATCH, "applied"))

    def test_the_daily_apply_finishes_the_run_and_the_fingerprints(self):
        self.gone_complex()
        self.seed()
        with patch.object(stage, "apply_inbox", return_value=[BATCH]):
            self.run_stage()
        self.assertEqual(self.prints()[KEY]["batch"], "seed-20261126")
        with patch.dict(os.environ, {"JIPFIT_LIFECYCLE_DIR": str(self.data / "batches")}):
            self.assertEqual(refresh.apply_lifecycle_inbox(DAY + timedelta(days=1)), [])
        self.assertEqual((self.prints()[KEY]["batch"], self.run_row()["status"]), (BATCH, "applied"))

    def test_a_malformed_fingerprint_file_rejects_the_bundle(self):
        self.gone_complex()
        self.seed()
        with patch.object(stage, "apply_inbox", return_value=[BATCH]):
            self.run_stage()
        (self.data / "batches" / BATCH / "fingerprints.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.run_stage(), [f"lifecycle:{BATCH}"])
        self.assertEqual((self.db.data["batches"][BATCH][1], self.run_row()["status"]), ("rejected", "judged"))
        self.assertEqual(self.prints()[KEY]["batch"], "seed-20261126")

    def test_holds_leave_the_public_state_as_it_was(self):
        standing = self.db.add(complex_(), trades=("2019-11-14",))
        self.db.confirm(standing)
        fresh = self.db.add(complex_("900", "새로"))
        self.before(current=[home(useAprDay="19900101")])
        self.answers[("current", "0345")] = [home(useAprDay="19900101"),
                                             home(useAprDay="19910101", mgmBldrgstPk="C2")]
        self.db.data["status"].update({standing: "present", fresh: "unconfirmed"})
        self.seed()
        before = [self.db.public_view(c) for c in (standing, fresh)]
        self.assertEqual(self.run_stage(), [])
        self.assertEqual([self.db.public_view(c) for c in (standing, fresh)], before)
        self.assertEqual(before[1], (None, "unconfirmed", False, None))
        self.assertEqual((self.db.state(standing)["pending_hold_code"], self.db.state(fresh)["pending_hold_code"]),
                         ("범위미확인", "기록없음"))

    def test_mass_confirmations_are_held_by_the_limit(self):
        first, second = self.gone_complex(), self.gone_complex("500", "둘째")
        self.seed()
        with patch.dict(os.environ, {"JIPFIT_LIFECYCLE_MAX_AUTO": "1"}):
            self.assertEqual(self.run_stage(), [])
        for complex_id in (first, second):
            self.assertEqual((self.db.state(complex_id)["existence"], self.db.state(complex_id)["pending_hold_code"]),
                             ("present", "대량확정보류"))
        self.assertEqual(len(lines(self.data / "batches" / BATCH / "proposals.jsonl")), 2)
        self.assertEqual((self.run_row()["summary"]["자동 확정"], self.run_row()["summary"]["대량확정보류"]), (0, 2))

    def test_the_default_limit_is_twenty(self):
        with patch.object(stage, "evaluate", return_value={}), \
                patch.object(stage, "hold_mass_confirmations") as guard:
            stage.judge({"complex_lifecycle_state": []}, {}, self.cache, self.reference, DAY)
        self.assertEqual(guard.call_args.args[2], 20)

    def test_a_missing_dong_asks_only_its_trade_month_and_is_identified(self):
        row = {"complex_id": "서울특별시 은평구 신사동|1|새동", "name": "새동", "built": "2007",
               "address": "서울특별시 은평구 신사동 1"}
        complex_id = self.db.add(row, trades=("2026-11-03",))
        known = self.new_complex()
        self.answers[("trade_detail", "202611")] = [{"sggCd": "11380", "umdNm": "신사동", "umdCd": "10900"}]
        self.answers[("current", "0001")] = [home(bldNm="새동")]
        self.seed()
        self.assertEqual(self.run_stage(), [])
        self.assertEqual([c for c in self.calls if c[0] == "trade_detail"], [("trade_detail", "202611")])
        self.assertEqual(self.calls[0], ("trade_detail", "202611"))
        self.assertEqual((self.db.state(complex_id)["existence"], self.db.state(known)["existence"]),
                         ("present", "present"))
        self.assertTrue(cache_file(self.cache, "trade_detail", {"LAWD_CD": "11380", "DEAL_YMD": "202611"}).is_file())

    def test_an_unfetched_trade_month_counts_as_missing(self):
        row = {"complex_id": "서울특별시 은평구 신사동|1|새동", "name": "새동", "built": "2007",
               "address": "서울특별시 은평구 신사동 1"}
        complex_id = self.db.add(row, trades=("2026-11-03",))
        self.failing = {("trade_detail", "202611")}
        self.seed()
        self.assertEqual(self.run_stage(), ["lifecycle:collecting"])
        self.assertEqual(self.run_row()["remaining"], 1)
        for _ in range(3):
            outcome = self.run_stage()
        self.assertEqual(outcome, [])
        self.assertEqual(self.db.state(complex_id)["pending_hold_code"], "식별실패")

    def test_the_month_is_that_of_the_judging_day(self):
        self.seed()
        self.clock = date(2027, 1, 1)  # A single run before 06:00 on the 1st passes the previous day.
        self.assertEqual(lifecycle_check.run_month(date(2026, 12, 31), workers=1), [])
        self.assertEqual((list(self.db.data["runs"]), self.batches()), (["2027-01"], ["monthly-20270101"]))

    def test_a_collection_that_runs_into_the_next_month_leaves_the_judgement_to_it(self):
        self.new_complex()
        self.seed()
        fetch = self.fetch

        def overnight(*args, **kwargs):
            fetch(*args, **kwargs)
            self.clock = date(2027, 1, 1)

        with patch.object(sources, "fetch", overnight):
            self.assertEqual(self.run_stage(date(2026, 12, 31)), ["lifecycle:month-changed"])
        self.assertEqual((self.run_row()["status"], self.batches()), ("collecting", []))
        self.assertEqual(self.run_stage(date(2027, 1, 1)), [])
        self.assertEqual(self.batches(), ["monthly-20270101"])

    def test_offline_judges_what_was_collected(self):
        self.gone_complex()
        self.seed()
        self.failing = {("closed_land", "0345")}
        self.assertEqual(self.run_stage(), ["lifecycle:collecting"])
        self.calls.clear()
        self.assertEqual(self.run_stage(offline=True), [])
        self.assertEqual(self.calls, [])
        (result,) = lines(self.data / "batches" / BATCH / "results.jsonl")
        self.assertEqual(result["hold_code"], "수집미완")

    def test_old_monthly_caches_are_removed_after_a_month_is_applied(self):
        self.seed()
        for name in ("2026-10", "2026-11", "keep"):
            monthly.write_json(self.data / "monthly" / name / "monthly.json", {"started": "2026-10-01"})
        (self.data / "monthly" / "2026-09").mkdir()
        self.assertEqual(self.run_stage(), [])
        self.assertEqual(sorted(p.name for p in (self.data / "monthly").iterdir()),
                         ["2026-09", "2026-11", MONTH, "keep"])

    def test_a_rehearsal_changes_nothing(self):
        old = self.gone_complex()
        self.seed()
        before = deepcopy(self.db.data)
        self.assertEqual(self.run_stage(dry_run=True), [])
        self.assertEqual(self.db.data, before)
        self.assertEqual(self.batches(), [])
        folder = self.data / "reports" / "rehearsal-20261228"
        self.assertEqual([r["complex_id"] for r in lines(folder / "results.jsonl")], [old])
        self.assertTrue((folder / "report.md").is_file())
        self.assertTrue(self.cache.is_dir())

    def test_an_offline_rehearsal_judges_the_reference_cache_alone(self):
        old = self.gone_complex()
        fresh = self.db.add(complex_("900", "새로"))
        self.seed()
        before = deepcopy(self.db.data)
        self.assertEqual(self.run_stage(dry_run=True, offline=True), [])
        self.assertEqual((self.db.data, self.calls, self.cache.exists()), (before, [], False))
        results = lines(self.data / "reports" / "rehearsal-20261228" / "results.jsonl")
        self.assertEqual([(r["complex_id"], r["hold_code"]) for r in results], [(fresh, "수집미완")])
        self.assertNotIn(old, [r["complex_id"] for r in results])

    def test_the_next_month_leaves_an_operator_decision_alone(self):
        complex_id = self.db.add(complex_())
        # No residential title: the automatic rule cannot call it 현존, so the first month holds it.
        shop = home(mainPurpsCdNm="근린생활시설", etcPurps="", useAprDay="19900101")
        self.answers[("current", "0345")] = [shop]
        self.seed()
        self.assertEqual(self.run_stage(), [])
        self.assertEqual(self.db.state(complex_id)["pending_hold_code"], "기록없음")
        admin.resolve(self.db, complex_id, "present", "현장 확인", DAY + timedelta(days=1))
        self.assertIsNone(monthly.active_hold({k: str(v) if v else v
                                               for k, v in self.db.state(complex_id).items()}))
        january = date(2027, 1, 1)
        self.clock = january
        self.assertEqual(lifecycle_check.run_month(january, workers=1), [])
        self.assertEqual(lines(self.data / "batches" / "monthly-20270101" / "results.jsonl"), [])


class OperatorTests(Stage):
    def setUp(self):
        super().setUp()
        self.today = DAY + timedelta(days=2)
        self.held = self.db.add(complex_())
        self.db.confirm(self.held)
        self.db.hold(self.held, "범위미확인", DAY)
        self.successor = self.db.add(complex_("345-4", "새단지", "2026"), trades=())

    def resolve(self, existence="demolished", note="현장 확인", **options):
        return admin.resolve(self.db, self.held, existence, note, self.today, **options)

    def test_a_dry_run_shows_the_change_and_changes_nothing(self):
        before = deepcopy(self.db.data)
        with patch("builtins.print") as output:
            summary = self.resolve(effective_date=date(2026, 12, 1), successor_id=self.successor, dry=True)
        self.assertEqual((summary["applied"], self.db.data), (1, before))
        printed = "\n".join(str(call.args[0]) for call in output.call_args_list)
        for expected in ("존치: 현존 -> 멸실", "효력일: 없음 -> 2026-12-01", "지도 마커: 표시 대상 -> 숨김",
                         "학습 제외: 없음 -> 없음", "열린 보류: 범위미확인"):
            self.assertIn(expected, printed)

    def test_a_demolition_corrects_the_confirmed_judgment(self):
        self.resolve(effective_date=date(2026, 12, 1), successor_id=self.successor)
        row = self.db.state(self.held)
        result = self.db.data["results"][row["result_key"]]
        self.assertRegex(result["result_key"], rf"^manual-20261230:[0-9a-f]{{16}}:")
        self.assertEqual((result["existence"], result["verifier_version"], result["correction_of"]),
                         ("demolished", "lifecycle_admin@manual", f"baseline-20261126:{self.held}"))
        self.assertEqual(result["evidence"], "basis=운영자 확인; successor_name=새단지; note=현장 확인")
        self.assertEqual((result["effective_date"], result["effective_precision"], result["successor_status"]),
                         (date(2026, 12, 1), "day", "linked"))
        self.assertEqual(self.db.data["status"][self.held], "demolished")
        self.assertEqual(self.db.data["quarantine"], [])

    def test_a_caution_date_leads_the_evidence_and_excludes_the_later_contracts(self):
        self.db.add_trade(self.held, "2026-12-05")
        self.resolve(caution=date(2026, 12, 1), note="말소 뒤 계약은\n입주권")
        row = self.db.state(self.held)
        self.assertEqual(row["evidence"],
                         "basis=운영자 확인; trade_caution_from=2026-12-01; note=말소 뒤 계약은 입주권")
        self.assertEqual(self.db.data["exclusions"][self.held][0], date(2026, 12, 1))

    def test_a_present_decision_closes_the_hold_without_hiding_the_marker(self):
        self.resolve("present")
        row = self.db.state(self.held)
        self.assertEqual((row["existence"], row["as_of"]), ("present", self.today))
        self.assertEqual(admin.holds(self.db, self.today), [])
        with patch("builtins.print"):
            self.assertIsNone(self.resolve("present"))

    def test_a_new_complex_can_be_decided_without_a_correction(self):
        fresh = self.db.add(complex_("900", "새로"))
        self.db.hold(fresh, "기록없음", DAY)
        admin.resolve(self.db, fresh, "present", "확인", self.today)
        self.assertIsNone(self.db.data["results"][self.db.state(fresh)["result_key"]]["correction_of"])

    def test_invalid_decisions_are_refused_before_any_change(self):
        self.db.add_trade(self.held, "2026-12-05")
        before = deepcopy(self.db.data)
        cases = (dict(existence="present", effective_date=date(2026, 12, 1)),
                 dict(existence="present", caution=date(2026, 12, 1)),
                 dict(precision="day"), dict(note="  "), dict(effective_date=self.today + timedelta(days=1)),
                 dict(successor_id="없는 단지"), dict(successor_id=self.held),
                 # A contract after the effective date asks for the caution date instead.
                 dict(effective_date=date(2026, 12, 1)))
        for options in cases:
            with self.subTest(options=options), self.assertRaises(SystemExit):
                self.resolve(**options)
        with self.assertRaises(SystemExit):
            admin.resolve(self.db, "없는 단지", "present", "확인", self.today)
        self.assertEqual(self.db.data, before)

    def test_holds_hide_incomplete_ones_and_write_a_csv(self):
        waiting = self.db.add(complex_("900", "수집"), trades=(), coordinates=False)
        self.db.hold(waiting, "수집미완", DAY)
        closed = self.db.add(complex_("901", "닫힘"))
        self.db.hold(closed, "기록없음", BASE_DAY - timedelta(days=1))
        self.db.confirm(closed)
        rows = admin.holds(self.db, self.today)
        self.assertEqual([(r["complex_id"], r["보류코드"], r["보류시작일"], r["지도상태"]) for r in rows],
                         [(self.held, "범위미확인", "2026-12-28", "현존, 마커 표시")])
        self.assertEqual(rows[0]["상세"], "범위미확인: 설명")
        path = self.data / "reports" / "holds-20261230.csv"
        self.assertEqual(csv_rows(path), rows)
        shown = admin.holds(self.db, self.today, show_all=True)
        self.assertEqual({r["complex_id"]: r["지도상태"] for r in shown}[waiting], "미판정, 마커 없음(좌표·거래 없음)")
        self.assertEqual(len(csv_rows(path)), 2)


class MassOperatorTests(Stage):
    """accept and revert on a month the mass guard held."""

    def setUp(self):
        super().setUp()
        self.first, self.second = self.gone_complex(), self.gone_complex("500", "둘째")
        self.fresh = self.new_complex()
        self.seed()
        self.today = DAY + timedelta(days=2)

    def held_month(self):
        with patch.dict(os.environ, {"JIPFIT_LIFECYCLE_MAX_AUTO": "1"}):
            self.assertEqual(self.run_stage(), [])

    def test_accept_applies_the_original_confirmation(self):
        self.held_month()
        summary = admin.accept(self.db, BATCH, [self.first], self.today)
        self.assertEqual((summary["applied"], summary["conflicts"], summary["quarantined"]), (1, 0, 0))
        row = self.db.state(self.first)
        self.assertEqual((row["existence"], row["result_key"], row["effective_date"], row["as_of"]),
                         ("demolished", f"accept-20261230:{self.first}", date(2026, 12, 1), DAY))
        self.assertTrue(row["evidence"].startswith("basis=대장 말소 자동 확정"))
        self.assertEqual(self.db.state(self.second)["existence"], "present")
        self.assertEqual([r["complex_id"] for r in admin.holds(self.db, self.today)], [self.second])

    def test_accept_all_skips_decided_complexes_and_holds_later_contracts(self):
        self.held_month()
        admin.resolve(self.db, self.first, "present", "현장 확인", self.today)
        self.db.add_trade(self.second, "2026-12-05")
        with patch("builtins.print") as output:
            summary = admin.accept(self.db, BATCH, None, self.today)
        self.assertEqual((summary["applied"], summary["conflicts"]), (0, 1))
        self.assertEqual(self.db.state(self.first)["existence"], "present")
        self.assertEqual((self.db.state(self.second)["existence"], self.db.state(self.second)["pending_hold_code"]),
                         ("present", "말소후거래"))
        printed = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn(f"건너뜀 {self.first}: 그사이 상태가 바뀜", printed)

    def test_accept_dry_run_changes_nothing(self):
        self.held_month()
        before = deepcopy(self.db.data)
        summary = admin.accept(self.db, BATCH, None, self.today, dry=True)
        self.assertEqual((summary["applied"], self.db.data), (2, before))

    def test_accept_needs_an_applied_batch_with_proposals(self):
        with self.assertRaises(SystemExit):
            admin.accept(self.db, BATCH, None, self.today)
        with self.assertRaises(SystemExit):
            admin.accept(self.db, "../x", None, self.today)
        self.assertEqual(self.run_stage(), [])
        with self.assertRaises(SystemExit):
            admin.accept(self.db, BATCH, None, self.today)

    def test_revert_restores_the_judgment_before_the_batch(self):
        self.assertEqual(self.run_stage(), [])
        self.assertEqual(self.db.state(self.first)["existence"], "demolished")
        prints = deepcopy(self.prints())
        before = deepcopy(self.db.data)
        admin.revert(self.db, BATCH, [self.first], self.today, dry=True)
        self.assertEqual(self.db.data, before)
        with patch("builtins.print") as output:
            outcome = admin.revert(self.db, BATCH, None, self.today)
        self.assertEqual(outcome, Counter({"되돌림": 2, "직전 확정 없음": 1}))
        for complex_id in (self.first, self.second):
            row = self.db.state(complex_id)
            self.assertEqual((row["existence"], row["effective_date"], row["evidence"]),
                             ("present", None, "basis=t; note=기준"))
            result = self.db.data["results"][row["result_key"]]
            self.assertEqual(result["correction_of"], f"{BATCH}:{complex_id}")
            self.assertEqual(self.db.data["status"][complex_id], "present")
        self.assertEqual(self.db.state(self.fresh)["result_key"], f"{BATCH}:{self.fresh}")
        self.assertIn(f"알림 {self.fresh}: 직전 확정이 없어", "\n".join(str(c.args[0]) for c in output.call_args_list))
        self.assertEqual((self.prints(), self.db.data["quarantine"]), (prints, []))
        self.assertEqual(admin.revert(self.db, BATCH, [self.first], self.today), Counter({"건너뜀": 1}))

    def test_revert_removes_a_successor_link_and_keeps_the_effective_date(self):
        old = self.db.add(complex_("800", "옛단지"))
        self.db.confirm(old, "demolished", effective="2020-04-20", evidence="basis=수동; note=멸실")
        self.db.add(complex_("800", "후속", "2025"), trades=())
        self.answers[("closed", "0800")] = [gone(shterDay="20200420")]
        self.assertEqual(self.run_stage(), [])
        self.assertIn("successor_name=후속", self.db.state(old)["evidence"])
        admin.revert(self.db, BATCH, [old], self.today)
        row = self.db.state(old)
        self.assertEqual((row["existence"], row["effective_date"], row["evidence"]),
                         ("demolished", date(2020, 4, 20), "basis=수동; note=멸실"))
        self.assertIsNone(self.db.data["results"][row["result_key"]]["successor_id"])


class TransferTests(Folders):
    def source(self):
        source = self.root / "verifier" / "source_cache"
        for folder in sources.REFERENCE_FOLDERS:
            put(source, folder, parcel_params(KEY), [home()])
        put(source, "housing", parcel_params(KEY), [])
        (source / "current" / "half.tmp").write_text("x", encoding="utf-8")
        return source

    def test_import_copies_only_the_folders_the_check_reads(self):
        source = self.source()
        document = monthly.baseline_fingerprints(source, BASE_DAY)
        monthly.write_json(source.parent / "results" / "fingerprints" / "latest.json", document)
        target = self.root / "jipfit" / "reference"
        target.mkdir(parents=True)
        manifest = admin.import_reference(source, target, DAY)
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         sorted([*sources.REFERENCE_FOLDERS, "MANIFEST.json", "source_fingerprints.json"]))
        self.assertEqual((manifest["file_count"], list(target.rglob("*.tmp"))), (9, []))
        saved = json.loads((target / "MANIFEST.json").read_text("utf-8"))
        for name, digest in saved["files"].items():
            self.assertEqual(sha256((target / name).read_bytes()).hexdigest(), digest)
        for bad_source, bad_target in ((source, target), (source, source / "copy"), (source, source.parent),
                                       (self.root / "none", self.root / "t")):
            with self.subTest(target=bad_target), self.assertRaises(SystemExit):
                admin.import_reference(bad_source, bad_target, DAY)

        db = StageDB()
        with patch("builtins.print") as output:
            admin.seed_fingerprints(db, target, BASE_DAY)
        printed = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("원본 지문과 같습니다 (지번 1개)", printed)
        self.assertEqual([(k, v["batch"]) for k, v in db.data["fingerprints"].items()], [(KEY, "seed-20261126")])
        with self.assertRaises(SystemExit):
            admin.seed_fingerprints(db, target, BASE_DAY)

    def test_the_seed_reports_a_difference_from_the_original(self):
        source = self.source()
        monthly.write_json(source.parent / "results" / "fingerprints" / "latest.json",
                           {"as_of": "2026-11-26", "parcels": {}})
        target = self.root / "reference"
        admin.import_reference(source, target, DAY)
        with patch("builtins.print") as output:
            admin.seed_fingerprints(StageDB(), target, BASE_DAY)
        self.assertIn("원본 지문과 다릅니다", "\n".join(str(call.args[0]) for call in output.call_args_list))

    def test_the_import_runs_without_the_database_packages(self):
        source = self.source()
        code = ("import sys\n"
                "for name in ('psycopg', 'pandas'):\n"
                "    sys.modules[name] = None\n"
                "import lifecycle_admin\n"
                "raise SystemExit(lifecycle_admin.main(sys.argv[1:]))\n")
        target = self.root / "reference"
        done = subprocess.run([sys.executable, "-c", code, "import-reference", "--from", str(source), "--to",
                               str(target)], cwd=ROOT, capture_output=True,
                              env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertTrue((target / "MANIFEST.json").is_file())


class DatabaseReadTests(unittest.TestCase):
    def test_one_read_only_snapshot_returns_the_rows_a_backup_would(self):
        db = StageDB()
        complex_id = db.add(complex_(), trades=("2019-11-14",))
        db.add_trade(complex_id, "2019-12-01", cancelled=True)
        db.confirm(complex_id, "demolished", effective="2020-04-20")
        with patch.object(store.psycopg, "connect", db.connect):
            with store.connect() as connection:
                tables, fingerprints = store.load_inputs(connection)
        queries = [q for q, _ in db.log]
        start = queries.index("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        self.assertTrue(all(q.startswith(("SET ", "COPY ", "SELECT parcel_key")) for q in queries[start:]))
        self.assertEqual(tables["complexes"], [{"complex_id": complex_id, "name": "제일", "address": f"{DONG} 345-4",
                                                "built": "2007"}])
        self.assertEqual([r["is_cancelled"] for r in tables["trades"]], ["f", "t"])
        (row,) = tables["complex_lifecycle_state"]
        self.assertEqual((row["as_of"], row["effective_date"], row["pending_hold_code"]),
                         ("2026-11-26", "2020-04-20", None))
        self.assertEqual(fingerprints, {"as_of": None, "parcels": {}})


class WiringTests(unittest.TestCase):
    FACILITY_STEP = staticmethod(refresh.refresh_facility_sources)

    def order(self, calls):
        def step(name):
            def run(*args, **kwargs):
                calls.append(name)
                return []
            return run

        return (patch.object(refresh, "ensure_schema"), patch.object(refresh, "refresh_many", step("trades")),
                patch.object(refresh, "refresh_automatic_metadata", step("metadata")),
                patch.object(refresh, "apply_metadata", step("verified")),
                patch.object(refresh, "seed_facilities", step("seed")),
                patch.object(refresh, "refresh_facility_sources", step("facilities")),
                patch.object(refresh, "run_lifecycle_month", step("lifecycle")),
                patch.object(refresh, "apply_lifecycle_inbox", step("inbox")),
                patch.object(refresh, "sync_facilities", step("sync")), patch("builtins.print"))

    def start(self, patches):
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_a_single_run_checks_after_the_metadata_and_before_the_inbox(self):
        calls = []
        self.start(self.order(calls))
        refresh.run_once(["11530"], metadata_dir=Path("m"))
        self.assertEqual(calls, ["seed", "trades", "metadata", "verified", "facilities", "lifecycle", "inbox",
                                 "sync"])

    def monthly_job(self, calls, extra=()):
        jobs, results = {}, []

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
            results.append(job())
            raise Stop

        self.start((*self.order(calls), *extra, patch.object(refresh, "Thread", Thread),
                    patch.object(refresh, "run_slot", run_slot),
                    patch("sys.argv", ["refresh.py", "--metadata-dir", "m"])))
        refresh.main()
        with self.assertRaises(Stop):
            jobs["metadata-refresh"]()
        return results[0]

    def test_the_monthly_job_checks_after_the_metadata(self):
        calls = []
        self.assertEqual(self.monthly_job(calls), [])
        self.assertEqual(calls, ["seed", "metadata", "verified", "facilities", "lifecycle"])

    def test_a_failing_metadata_step_still_runs_the_check(self):
        calls = []

        def broken(*args, **kwargs):
            raise OSError("file")

        failed = self.monthly_job(calls, (patch.object(refresh, "apply_metadata", broken),))
        self.assertEqual((failed, calls), (["OSError"], ["seed", "metadata", "facilities", "lifecycle"]))

    def test_a_failing_facility_step_still_runs_the_check(self):
        for outcome, label in (({"return_value": ["stations"]}, "facilities:stations"),
                               ({"side_effect": RuntimeError("x")}, "facilities:RuntimeError")):
            calls = []
            with self.subTest(label=label):
                failed = self.monthly_job(calls, (
                    patch.object(refresh, "refresh_facility_sources", self.FACILITY_STEP),
                    patch.object(refresh, "refresh_sources", **outcome)))
                self.assertEqual((failed, calls), ([label], ["seed", "metadata", "verified", "lifecycle"]))
                self.doCleanups()


class PackagingTests(unittest.TestCase):
    def test_the_tables_are_idempotent(self):
        sql = (ROOT / "db/metadata_refresh.sql").read_text(encoding="utf-8")
        for table in ("complex_lifecycle_runs", "complex_lifecycle_fingerprints"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} (", sql)

    def test_the_image_and_the_updater_hold_the_check(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("lifecycle_admin.py", dockerfile)
        self.assertIn("COPY lifecycle_check/*.py lifecycle_check/", dockerfile)
        updater = (ROOT.parent / "compose.yaml").read_text(encoding="utf-8").split("\n  updater:")[1]
        for line in ("JIPFIT_LIFECYCLE_DATA: /lifecycle-data", "JIPFIT_LIFECYCLE_AUTO: ${JIPFIT_LIFECYCLE_AUTO:-0}",
                     "JIPFIT_LIFECYCLE_MAX_AUTO: ${JIPFIT_LIFECYCLE_MAX_AUTO:-20}",
                     "- ./lifecycle-data:/lifecycle-data\n",
                     "- ./lifecycle-data/reference:/lifecycle-data/reference:ro"):
            self.assertIn(line, updater)

    def test_no_running_code_names_the_verifier_folder(self):
        for path in (*sorted((ROOT / "lifecycle_check").glob("*.py")), ROOT / "lifecycle_admin.py"):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("PARITY_SRC", source)
                self.assertNotIn("source_cache\"", source)


if __name__ == "__main__":
    unittest.main()
