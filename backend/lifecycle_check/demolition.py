"""Conditions for confirming a cancellation, from jipfit_lifecycle@d86dcdf:demolished.py.

kapt_records, project_pages and lot_removals come from jipfit_lifecycle@d86dcdf:auto_link.py, val from
project_evidence.py and the text lines of a cleanup page (Page) from public_documents.py.
"""
import os
import re
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from lifecycle_check import data_dir
from lifecycle_check.common import name_key, residential, ymd
from lifecycle_check.sources import complete_snapshot, once_per_run, parcel_params, read_bytes, read_json

TARGETS = ("pending1", "pending2")


def val(row, key):
    return str(row.get(key) or "").strip()


class Page(HTMLParser):
    """The visible text lines of a saved page."""

    def __init__(self, raw, charset):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.hidden = False
        for encoding in dict.fromkeys((charset, "utf-8-sig", "cp949")):
            if encoding:
                try:
                    self.feed(raw.decode(encoding))
                    self.close()
                    break
                except (LookupError, UnicodeDecodeError):
                    continue

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = False

    def handle_data(self, data):
        if not self.hidden and (value := " ".join(data.split())):
            self.lines.append(value)


def kapt_records(cache):
    records = {}
    for path in (cache / "kapt_info").glob("*.json"):
        for row in read_json(path)["rows"]:
            records[val(row, "kaptCode")] = row
    return list(records.values())


def project_pages(cache):
    """Cached 정비사업 zone locations and recorded demolition completion dates, by cafe id."""
    folder = os.path.abspath(cache)
    # Only the reference cache is never written during a run (prepare_cache), so only its result is kept.
    if folder != os.path.abspath(data_dir() / "reference"):
        return parse_project_pages(cache)
    return once_per_run(("project_pages", folder), lambda: parse_project_pages(cache))


def parse_project_pages(cache):
    zones, removals = {}, defaultdict(set)
    for meta in sorted((cache / "public_documents").glob("*.json")):
        details = read_json(meta)
        url, body = details.get("url", ""), meta.with_suffix(".bin")
        parts = urlsplit(url)
        cafe = parse_qs(parts.query).get("cafeId", [""])[0]
        summary, history = "mastr-cleanup-bsnsSumry" in url, "cleanup-prtnelapse" in url
        if parts.hostname != "cleanup.seoul.go.kr" or not cafe or not (summary or history) or not body.is_file():
            continue
        line = " | ".join(Page(read_bytes(body), details.get("charset")).lines)
        if summary and (location := re.search(r"정비구역 위치 \| ([^|]+)", line)):
            zones[cafe] = (location[1].strip(), url)
        if history:
            for found in re.findall(r"(\d{4})- ?(\d\d)- ?(\d\d) \| \[철거완료\]", line):
                removals[cafe].add(("-".join(found), url))
    return zones, {cafe: sorted(dates) for cafe, dates in removals.items()}


def lot_removals(zones, removals, lots):
    """Demolition dates of every zone whose location names one of the (법정동, parcel) lots."""
    found = {}
    for dong, parcel in lots:
        number = str(int(parcel["bun"])) + (f"-{int(parcel['ji'])}" if int(parcel["ji"]) else "")
        pattern = re.compile(rf"(?<![가-힣]){re.escape(dong)}\s*{number}(?!\d|-\d)")
        for cafe, (location, url) in zones.items():
            if pattern.search(location):
                for day, history in removals.get(cafe, []):
                    found[day, history] = url
    return found


def current_homes(cache):
    """Cached current residential titles by (시군구, 법정동, name) with their approval year and lot."""
    homes = defaultdict(set)
    for path in (cache / "current").glob("*.json"):
        for row in read_json(path)["rows"]:
            if residential(row) and val(row, "bldNm"):
                homes[val(row, "sigunguCd"), val(row, "bjdongCd"), name_key(val(row, "bldNm"))].add(
                    (val(row, "useAprDay")[:4], val(row, "platGbCd") or "0", val(row, "bun"), val(row, "ji")))
    return homes


def attached_lots(records, own):
    lots = {}
    for item in records:
        if not val(item, "atchSigunguCd") or not int(val(item, "atchBun") or 0):
            continue
        lot = {"sigunguCd": val(item, "atchSigunguCd"), "bjdongCd": val(item, "atchBjdongCd"),
               "platGbCd": val(item, "atchPlatGbCd") or "0", "bun": val(item, "atchBun").zfill(4),
               "ji": (val(item, "atchJi") or "0").zfill(4)}
        if lot != own:
            lots[tuple(lot.values())] = lot
    return list(lots.values())


def cancelled_roster(row, cache, as_of):
    """The old buildings, when all of the lot's residential titles were cancelled after the last trade."""
    if row["판정"] not in TARGETS or row.get("거래검증자료") != "집핏 PostgreSQL 백업":
        return None
    key = parcel_params(row["구지번키"])
    latest = ymd(row.get("최근유효계약일"))
    current, closed, events, outlines = (complete_snapshot(cache, service, key, as_of)
                                         for service in ("current", "closed", "demolition", "closed_outline"))
    if not latest or any(records is None for records in (current, closed, events, outlines)):
        return None
    old = [b for b in closed if residential(b)]
    households = sum(int(val(b, "hhldCnt") or 0) for b in old)
    if (not old or not households or any(residential(b) for b in current)
            or row["준공연도"] not in {val(b, "useAprDay")[:4] for b in old}
            or any(val(b, "shterGbCd") != "2" or not ymd(val(b, "useAprDay")) for b in old)):
        return None
    closes = sorted({ymd(val(b, "shterDay")) for b in old})
    if not closes[0] or not latest < closes[0] <= closes[-1] <= as_of.isoformat():
        return None
    built = min(ymd(val(b, "useAprDay")) for b in old)
    removed = [day for e in events for field in ("demolEndDay", "demolExtngDay") if (day := ymd(val(e, field)))]
    if any(built < day <= latest for day in removed):
        return None
    # Several buildings count as the whole roster only when a cancelled outline totals the same households.
    if len(old) > 1 and not any(
            val(o, "shterGbCd") == "2" and int(val(o, "hhldCnt") or 0) == households
            and int(val(o, "mainBldCnt") or 0) >= len(old) for o in outlines):
        return None
    return {"key": key, "old": old, "households": households, "built": built, "latest": latest,
            "closes": closes, "recorded": any(latest < day <= as_of.isoformat() for day in removed)}


def holding_reason(row, found, cache, as_of, namesakes, managed, zones, removals):
    """Why the roster alone is not enough, and how many attached lots were checked."""
    key, latest, built = found["key"], found["latest"], found["built"]
    dong = row["구지번"].split()[2]
    if any(built < day <= latest for day, _ in lot_removals(zones, removals, [(dong, key)])):
        return "사업장 철거완료일이 마지막 계약 이전", 0
    if int(found["closes"][0][:4]) - int(built[:4]) < 5:
        return "준공 5년 이내 말소", 0
    names = {name_key(row["원래거래단지명"]), *(name_key(val(b, "bldNm")) for b in found["old"])} - {""}
    own = (key["platGbCd"], key["bun"], key["ji"])
    if any(year == row["준공연도"] and tuple(lot) != own for name in names
           for year, *lot in namesakes.get((key["sigunguCd"], key["bjdongCd"], name), ())):
        return "같은 이름·준공연도의 현행 주거동", 0
    if any(val(r, "bjdCode") == key["sigunguCd"] + key["bjdongCd"] and name_key(val(r, "kaptName")) in names
           and ymd(val(r, "kaptUsedate"))[:4] == row["준공연도"] for r in managed):
        return "같은 이름·사용승인연도의 K-apt 단지", 0
    records = complete_snapshot(cache, "closed_land", key, as_of)
    if records is None:
        return "폐쇄 부속지번 미수집", 0
    lots = attached_lots(records, key)
    for lot in lots:
        titles = complete_snapshot(cache, "current", lot, as_of)
        if titles is None:
            return "부속지번 현행 대장 미수집", len(lots)
        # Housing approved after the cancellation is a later building, not a survivor.
        if any(residential(b) and not ymd(val(b, "useAprDay")) > found["closes"][-1] for b in titles):
            return "부속지번에 말소 전 사용승인 주거동", len(lots)
    return "", len(lots)


def missing_calls(rows, cache, as_of):
    """Register queries still needed to check the candidates' attached lots."""
    calls = {}
    for row in rows:
        found = cancelled_roster(row, cache, as_of)
        if not found:
            continue
        records = complete_snapshot(cache, "closed_land", found["key"], as_of)
        wanted = [("closed_land", found["key"])] if records is None else [
            ("current", lot) for lot in attached_lots(records, found["key"])
            if complete_snapshot(cache, "current", lot, as_of) is None]
        for service, params in wanted:
            calls[service, tuple(params.values())] = (service, params)
    return list(calls.values())
