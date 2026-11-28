"""Per-API call pacing and the second ask of brief failures (plan-lifecycle-rate-limit.md 4-1).

time.monotonic and time.sleep are replaced by a made-up clock, so no test waits and no API is called.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier, local
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

# Installs the psycopg stand-in when the package is missing, so it must be imported first.
from test_lifecycle_check import Folders, Stage, answer, parcel

from lifecycle_check import collect, monthly, sources
from lifecycle_check.sources import SourceError, cache_file, parcel_params

BLD, SHT, ARCH = "BldRgstHubService", "ShtRgstHubService", "ArchPmsHubService"
SETTINGS = ("JIPFIT_LIFECYCLE_RATE", "JIPFIT_LIFECYCLE_RATE_MIN")
KEYS = {sources.KEY_NAME: "k"}


def url(service, **query):
    return f"{sources.MOLIT}{sources.SERVICES[service]}?" + "&".join(f"{k}={v}" for k, v in query.items())


def service_of(address):
    path = urlsplit(address).path
    return next(service for service, end in sources.SERVICES.items() if path.endswith(end))


class Clock:
    def __init__(self):
        self.now, self.slept, self.during_sleep = 0.0, [], []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        end = self.now + seconds
        while self.during_sleep and self.during_sleep[0][0] <= end:  # Something that happens meanwhile.
            self.now, action = self.during_sleep.pop(0)
            action()
        self.now = end


class Timed(Folders):
    """sources.request answers from self.replies and notes when each call left."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.patch(patch.object(sources.time, "monotonic", self.clock.monotonic))
        self.patch(patch.object(sources.time, "sleep", self.clock.sleep))
        self.patch(patch.object(sources, "request", self.request))
        self.output = self.patch(patch("builtins.print"))
        self.patch(patch.dict(os.environ))
        for name in SETTINGS:
            os.environ.pop(name, None)
        sources._paces.clear()
        self.addCleanup(sources._paces.clear)
        self.departed, self.replies, self.lots = [], {}, 0

    def request(self, address):
        service = service_of(address)
        bun = parse_qs(urlsplit(address).query).get("bun", [""])[0]
        self.departed.append((self.clock.now, service, bun))
        queue = self.replies.get((service, bun)) or self.replies.get(service)
        reply = queue.pop(0) if queue else answer([])
        if isinstance(reply, str):
            raise SourceError(reply)
        return reply

    def ask(self, service):
        return sources.request_molit(service, url(service))

    def read(self, service):
        """A whole fetch of one page, which tells the pace whether the response was a success."""
        self.lots += 1  # A new file each time; Windows can refuse to replace a file just written.
        return sources.fetch(self.cache, KEYS, service, parcel_params(parcel(str(self.lots))))

    def times(self, service=None):
        return [round(when, 6) for when, found, _ in self.departed if service in (None, found)]

    def printed(self, start=""):
        lines = (" ".join(map(str, call.args)) for call in self.output.call_args_list)
        return [line for line in lines if line.startswith(start)]

    def pace(self, api=ARCH):
        return sources.paces()[api]


class PaceTests(Timed):
    def test_calls_to_one_api_leave_a_fixed_interval_apart(self):
        for _ in range(5):
            self.ask("current")
        self.assertEqual(self.times(), [0, 0.5, 1.0, 1.5, 2.0])

    def test_the_interval_follows_the_setting(self):
        os.environ["JIPFIT_LIFECYCLE_RATE"] = "4"
        for _ in range(3):
            self.ask("current")
        self.assertEqual(self.times(), [0, 0.25, 0.5])

    def test_a_call_after_a_quiet_time_leaves_at_once(self):
        self.ask("current")
        self.clock.now = 10.0
        self.ask("current")
        self.assertEqual((self.times(), self.clock.slept), ([0, 10.0], []))

    def test_the_wait_is_outside_the_lock(self):
        pace = self.pace(BLD)
        held = []
        self.clock.during_sleep = [(0.1, lambda: held.append(pace.lock.locked()))]
        self.ask("current")
        self.ask("current")
        self.assertEqual(held, [False])

    def test_different_apis_do_not_wait_for_each_other(self):
        for service in ("current", "demolition", "closed", "trade_detail"):
            self.ask(service)
        self.assertEqual((self.times(), self.clock.slept), ([0, 0, 0, 0], []))

    def test_the_services_of_one_api_share_the_interval(self):
        for service in ("closed", "closed_outline", "closed_land"):
            self.ask(service)
        self.assertEqual(self.times(), [0, 0.5, 1.0])
        self.assertEqual(sorted(sources.paces()), sorted([BLD, SHT, ARCH, "RTMSDataSvcAptTradeDev"]))

    def test_the_retries_after_429_keep_the_interval(self):
        self.replies["demolition"] = ["HTTP 429", "HTTP 429"]
        self.ask("demolition")
        # Each 429 halves the rate and pauses 5 seconds, which outlasts the 1.2 and 2.5 second waits.
        self.assertEqual(self.times(), [0, 5.0, 10.0])
        self.assertEqual(self.pace().rate, 1.0)

    def test_the_retries_of_brief_failures_keep_the_interval(self):
        for first in ("HTTP 503", b""):
            with self.subTest(first=first):
                sources._paces.clear()
                os.environ["JIPFIT_LIFECYCLE_RATE"] = os.environ["JIPFIT_LIFECYCLE_RATE_MIN"] = "0.2"
                self.departed.clear()
                self.clock.now = 0.0
                self.replies["demolition"] = [first]
                collect.retry_brief_failures(sources.request_molit)("demolition", url("demolition"))
                # The retry is ready after 3 seconds and still leaves 5 seconds after the first call.
                self.assertEqual(self.times(), [0, 5.0])

    def test_429_halves_the_rate_down_to_the_minimum(self):
        os.environ["JIPFIT_LIFECYCLE_RATE"] = "4"
        pace = self.pace()
        found = []
        for _ in range(3):
            pace.throttled()
            found.append(pace.rate)
        self.assertEqual(found, [2.0, 1.0, 1.0])
        # A line only when the rate changes.
        self.assertEqual(self.printed(), [
            "Lifecycle 호출 속도 ArchPmsHubService 초당 4.00 → 2.00회 (HTTP 429)",
            "Lifecycle 호출 속도 ArchPmsHubService 초당 2.00 → 1.00회 (HTTP 429)"])
        self.ask("demolition")
        self.ask("current")  # Another API is not paused.
        self.assertEqual((self.times("demolition"), self.times("current")), ([5.0], [5.0]))
        self.assertEqual(self.clock.slept, [5.0])

    def test_calls_already_waiting_take_the_pause_too(self):
        self.ask("demolition")
        pace = self.pace()
        # The second call has reserved 0.5 when another thread's 429 arrives at 0.2.
        self.clock.during_sleep = [(0.2, pace.throttled)]
        self.ask("demolition")
        self.ask("demolition")
        self.assertEqual(self.times(), [0, 5.2, 6.2])

    def test_a_request_that_stays_throttled_fails_as_before(self):
        self.replies["demolition"] = ["HTTP 429"] * 3
        with self.assertRaisesRegex(SourceError, "^HTTP 429$"):
            self.ask("demolition")
        self.assertEqual((len(self.departed), self.pace().rate), (3, 1.0))

    def test_every_two_hundred_successes_raise_the_rate_up_to_the_setting(self):
        pace = self.pace()
        pace.throttled()
        for rate in (1.25, 1.5, 1.75, 2.0, 2.0):
            before = pace.rate
            for _ in range(199):
                pace.succeeded()
            self.assertEqual(pace.rate, before)
            self.read("demolition")  # The 200th success comes from a real fetch.
            self.assertEqual(pace.rate, rate)
        raised = self.printed("Lifecycle 호출 속도 ArchPmsHubService")[1:]
        self.assertEqual((len(raised), raised[0]),
                         (4, "Lifecycle 호출 속도 ArchPmsHubService 초당 1.00 → 1.25회 (성공 200회)"))

    def test_any_error_ends_the_successes_in_a_row(self):
        pace = self.pace()
        pace.throttled()
        # An HTTP error, an empty response and the portal's error inside a normal response.
        for reply, text in (("HTTP 500", "HTTP 500"), (b"", "응답 형식 불일치"),
                            (answer([], status="22"), "응답 코드 22")):
            with self.subTest(reply=reply):
                pace.failed()
                for _ in range(199):
                    pace.succeeded()
                self.replies["demolition"] = [reply]
                with self.assertRaisesRegex(SourceError, text):
                    self.read("demolition")
                self.assertEqual(pace.successes, 0)
                self.read("demolition")
                self.assertEqual((pace.successes, pace.rate), (1, 1.0))

    def test_wrong_settings_use_the_defaults_and_say_so_once(self):
        for value in ("abc", "0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                sources._paces.clear()
                self.output.reset_mock()
                os.environ.update({name: value for name in SETTINGS})
                for _ in range(2):
                    pace = self.pace()
                self.assertEqual((pace.rate, pace.limit, pace.floor), (2.0, 2.0, 1.0))
                self.assertEqual(self.printed(), [
                    f"Lifecycle 설정 {name}={value}: 0보다 큰 숫자가 아니어서 기본값 {default}로 수집합니다."
                    for name, default in zip(SETTINGS, ("2", "1"))])

    def test_empty_settings_are_the_defaults_without_a_notice(self):
        os.environ.update({name: " " for name in SETTINGS})
        self.assertEqual((self.pace().limit, self.pace().floor, self.printed()), (2.0, 1.0, []))

    def test_a_rate_below_the_minimum_is_never_raised_by_429(self):
        os.environ["JIPFIT_LIFECYCLE_RATE"] = "0.25"
        pace = self.pace()
        pace.throttled()
        self.assertEqual((pace.rate, pace.floor, self.printed()), (0.25, 0.25, []))

    def test_the_rate_line_shows_what_each_api_runs_at(self):
        self.pace().throttled()
        self.assertEqual(sources.rate_line(), "Lifecycle 호출 속도: BldRgstHubService 초당 2.00회 · "
                         "ShtRgstHubService 초당 2.00회 · ArchPmsHubService 초당 1.00회 · "
                         "RTMSDataSvcAptTradeDev 초당 2.00회")


class ThreadTests(Timed):
    def test_eight_threads_never_leave_at_the_same_time(self):
        # The clock stands still: a call leaves at the moment its thread slept until.
        woke, ready, left = local(), Barrier(8), []

        def sleep(seconds):
            woke.at = seconds

        def request(address):
            left.append(getattr(woke, "at", 0.0))
            woke.at = 0.0
            return answer([])

        def worker(_):
            ready.wait(timeout=10)
            for _ in range(25):
                sources.request_molit("current", url("current"))

        with patch.object(sources.time, "sleep", sleep), patch.object(sources, "request", request), \
                ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))
        self.assertEqual(sorted(left), [number * 0.5 for number in range(200)])


class RetryTests(Timed):
    """collect_basic on five parcels whose current register ends in a different way each."""

    def setUp(self):
        super().setUp()
        self.patch(patch.object(sources, "today", return_value=sources.today()))
        self.targets = [{"parcel_key": parcel(str(lot))} for lot in range(1, 6)]
        # HTTP 503 is asked four times inside one call, so all four have to fail for the call to fail.
        self.replies = {("current", "0001"): ["HTTP 502"], ("current", "0002"): ["HTTP 503"] * 4,
                        ("current", "0003"): [answer([], status="04")],
                        ("current", "0004"): [answer([], status="22")], ("current", "0005"): ["HTTP 500"]}

    def collect(self, deadline=None):
        monthly.collect_basic(self.targets, self.cache, KEYS, sources.today(), 1, deadline)

    def asked(self, service="current"):
        return Counter(bun for _, found, bun in self.departed if found == service)

    def test_only_brief_failures_are_asked_once_more_after_a_minute(self):
        path = cache_file(self.cache, "current", parcel_params(parcel("1")))
        with sources.read_once() as store:
            self.collect()
            self.assertEqual(self.asked(), Counter({"0001": 2, "0002": 5, "0003": 2, "0004": 1, "0005": 1}))
            self.assertEqual(self.asked("closed"), Counter({f"{lot:04}": 1 for lot in range(1, 6)}))
            first, second = [when for when, service, bun in self.departed if (service, bun) == ("current", "0001")]
            self.assertGreaterEqual(second - max(when for when, _, _ in self.departed[:-3]), 60)
            self.assertEqual(self.clock.slept.count(60), 1)
            # What the second ask brought is in the cache and in the read store.
            reused = store.stats()["reused"]
            for lot in ("1", "2", "3"):
                self.assertIsNotNone(sources.cached(self.cache, "current", parcel_params(parcel(lot))))
            self.assertEqual(store.stats()["reused"], reused + 3)
            self.assertTrue(path.is_file())
            for lot in ("4", "5"):
                self.assertIsNone(sources.cached(self.cache, "current", parcel_params(parcel(lot))))
        lines = [line for line in self.printed() if not line.startswith("Lifecycle")]
        self.assertEqual(lines, [
            "지번 5 · 조회할 기본 대장 15", "철거멸실 완료 5", "폐쇄말소 완료 5", "현행 HTTP 500 1",
            "현행 HTTP 502 1", "현행 HTTP 503 1", "현행 응답 코드 04 1", "현행 응답 코드 22 1",
            "다시 묻기 3건", "현행 완료 3"])

    def test_a_second_failure_is_left_for_the_next_attempt(self):
        self.replies[("current", "0001")] = ["HTTP 502"] * 2
        self.collect()
        self.assertEqual((self.asked()["0001"], self.printed("다시 묻기"), self.printed("현행 HTTP 502")),
                         (2, ["다시 묻기 3건"], ["현행 HTTP 502 1"] * 2))

    def test_nothing_is_asked_again_without_a_minute_left(self):
        # The first pass takes a few seconds of the clock; 59 seconds are not enough for the pause.
        self.collect(deadline=59.0)
        self.assertEqual((self.asked()["0001"], self.printed("다시 묻기")), (1, []))
        self.assertNotIn(60, self.clock.slept)

    def test_a_minute_left_is_enough_to_ask_again(self):
        self.collect(deadline=600.0)
        self.assertEqual((self.asked()["0001"], self.printed("다시 묻기")), (2, ["다시 묻기 3건"]))

    def test_nothing_failed_means_no_pause(self):
        self.replies = {}
        self.collect()
        self.assertEqual((len(self.departed), self.printed("다시 묻기")), (15, []))
        self.assertNotIn(60, self.clock.slept)


class ClassificationTests(unittest.TestCase):
    def raised(self, raw, page=1):
        with self.assertRaises(SourceError) as caught:
            sources.molit_page(raw, "current", page)
        return str(caught.exception)

    def test_the_texts_the_portal_functions_raise_are_sorted(self):
        portal = (b'{"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "%s", "errMsg": "%s"}}}')
        xml = b"<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>%s</returnReasonCode>" \
              b"</cmmMsgHeader></OpenAPI_ServiceResponse>"
        again = [self.raised(portal % (b"04", b"HTTP_ERROR")), self.raised(portal % (b"05", b"SERVICETIMEOUT_ERROR")),
                 self.raised(xml % b"04"), self.raised(answer([], status="05")), self.raised(b""),
                 self.raised(b"<broken"), "HTTP 429", "HTTP 502", "HTTP 503", "HTTP 504", "연결 실패"]
        # sources.code shows names longer than 18 characters as 미확인, so only the number can be judged.
        self.assertEqual(again[:5], ["응답 코드 04 (HTTP_ERROR)", "응답 코드 05 (미확인)",
                                     "응답 코드 04", "응답 코드 05", "응답 형식 불일치"])
        kept = [self.raised(portal % (b"22", b"LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR")),
                self.raised(portal % (b"30", b"SERVICE_KEY_IS_NOT_REGISTERED_ERROR")),
                self.raised(answer([], status="40")), self.raised(answer([], page=2)),
                "HTTP 400", "HTTP 401", "HTTP 500", "응답 크기 초과", "인증키 미설정", "전체 건수 불일치",
                "행 형식 불일치", "페이지 중복 또는 누락", "페이지 상한 도달", "시간 한도로 미조회", "응답 코드 미확인"]
        self.assertEqual(kept[3], "페이지 응답 불일치")
        self.assertEqual([text for text in again if not collect.worth_asking_again(text)], [])
        self.assertEqual([text for text in kept if collect.worth_asking_again(text)], [])


class DeadlineTests(Timed):
    def test_the_time_limit_ends_as_before(self):
        calls = [("current", parcel_params(parcel(str(lot)))) for lot in range(1, 4)]
        failed = []
        outcome = collect.fetch_all(calls, self.cache, KEYS, 1, deadline=0.0, failed=failed)
        self.assertEqual((outcome, failed, self.departed), (Counter({"현행 시간 한도로 미조회": 3}), [], []))

    def test_a_call_that_started_in_time_is_sent_even_if_its_turn_comes_later(self):
        os.environ["JIPFIT_LIFECYCLE_RATE"] = "0.5"
        calls = [("current", parcel_params(parcel(str(lot)))) for lot in range(1, 4)]
        outcome = collect.fetch_all(calls, self.cache, KEYS, 1, deadline=1.0)
        # The second starts at 0, waits for its turn at 2 and is sent; the third would start at 2.
        self.assertEqual((self.times(), outcome),
                         ([0, 2.0], Counter({"현행 완료": 2, "현행 시간 한도로 미조회": 1})))

    def test_the_returned_count_is_the_same_with_and_without_the_list(self):
        calls = [("current", parcel_params(parcel("1")))]
        self.replies["current"] = ["HTTP 502"]
        self.assertEqual(collect.fetch_all(calls, self.cache, KEYS, 1), Counter({"현행 HTTP 502": 1}))
        self.replies["current"] = ["HTTP 502"]
        failed = []
        self.assertEqual(collect.fetch_all(calls, self.cache, KEYS, 1, failed=failed),
                         Counter({"현행 HTTP 502": 1}))
        self.assertEqual(failed, calls)


class RunTests(Stage):
    def setUp(self):
        super().setUp()
        sources._paces.clear()
        self.addCleanup(sources._paces.clear)
        self.output = self.patch(patch("builtins.print"))

    def test_a_run_starts_with_the_rates_and_keeps_a_lowered_one(self):
        self.new_complex()
        self.seed()
        self.assertEqual(self.run_stage(dry_run=True), [])
        sources.paces()[ARCH].throttled()
        self.assertEqual(self.run_stage(dry_run=True), [])
        lines = [str(call.args[0]) for call in self.output.call_args_list
                 if str(call.args[0]).startswith("Lifecycle 호출 속도:")]
        self.assertEqual([f"{ARCH} 초당 {rate}회" in line for rate, line in zip(("2.00", "1.00"), lines)],
                         [True, True])
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
