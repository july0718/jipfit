"""Parallel collection with a time limit, from jipfit_lifecycle@d86dcdf:collect_registers.py.

keep_unreadable_responses and retry_brief_failures come from jipfit_lifecycle@d86dcdf:collect_housing_links.py.
They wrap the calls of one collection instead of replacing module functions, because the updater lives on.
"""
import hashlib
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote

from lifecycle_check import sources
from lifecycle_check.sources import SourceError

LABELS = {"current": "현행", "closed": "폐쇄말소", "demolition": "철거멸실",
          "closed_outline": "폐쇄총괄", "closed_land": "폐쇄부속지번", "trade_detail": "실거래 월"}


def keep_unreadable_responses(parse, folder, key):
    """Save a response that cannot be parsed, with the API key masked, so the cause can be checked."""
    secrets = [value.encode() for value in {key, unquote(key), quote(unquote(key), safe="")} if value]

    def page(raw, service, number):
        try:
            return parse(raw, service, number)
        except SourceError as exc:
            if str(exc) == "응답 형식 불일치":
                body = raw
                for secret in secrets:
                    body = body.replace(secret, b"***")
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{service}_{number}_{hashlib.sha256(body).hexdigest()[:8]}.txt").write_bytes(body)
            raise

    return page


def retry_brief_failures(ask, attempts=4, delay=3):
    """The permit API sometimes answers with an empty body or HTTP 503; ask again after a pause."""

    def request(service, url):
        for attempt in range(1, attempts + 1):
            try:
                body = ask(service, url)
                if body.strip() or attempt == attempts:
                    return body
            except SourceError as exc:
                if str(exc) != "HTTP 503" or attempt == attempts:
                    raise
            time.sleep(delay * attempt)

    return request


def fetch_all(calls, cache, keys, workers=8, *, deadline=None, refresh=True):
    """Fetch the calls in parallel and count how each one ended.

    A call that has not started by the deadline (time.monotonic) is left for the next attempt; calls already
    running finish, and every cached file is written whole.
    """
    ask = retry_brief_failures(sources.request_molit)
    parse = keep_unreadable_responses(sources.molit_page, cache / "format_errors", keys.get(sources.KEY_NAME) or "")

    def one(call):
        service, params = call
        if deadline is not None and time.monotonic() >= deadline:
            return f"{LABELS[service]} 시간 한도로 미조회"
        try:
            sources.fetch(cache, keys, service, params, refresh=refresh, ask=ask, parse=parse)
            return f"{LABELS[service]} 완료"
        except SourceError as exc:
            return f"{LABELS[service]} {exc}"

    outcome = Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, result in enumerate(pool.map(one, calls), 1):
            outcome[result] += 1
            if index % 500 == 0:
                print(f"{index}/{len(calls)}", dict(outcome), flush=True)
    return outcome
