"""Official API calls and their cache, from jipfit_lifecycle@d86dcdf:official_sources.py.

complete_snapshot comes from jipfit_lifecycle@d86dcdf:verify_lifecycle.py. Only the services the monthly
check asks for are kept, the key comes from the environment and retrieval days are KST days.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

from lifecycle_check.common import text, ymd

KST = timezone(timedelta(hours=9))
MOLIT = "https://apis.data.go.kr/1613000/"
KEY_NAME = "MOLIT_SERVICE_KEY"
SERVICES = {
    "current": "BldRgstHubService/getBrTitleInfo",
    "closed": "ShtRgstHubService/getSrTitleInfo",
    "closed_outline": "ShtRgstHubService/getSrRecapTitleInfo",
    "closed_land": "ShtRgstHubService/getSrAtchJibunInfo",
    "demolition": "ArchPmsHubService/getApDemolExtngMgmRgstInfo",
    "trade_detail": "RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
}
# Every folder of the baseline cache the monthly check reads: 법정동 codes, K-apt basic records, project
# pages, the registers behind the fingerprints and, for a rehearsal on the baseline alone, the extra ones.
REFERENCE_FOLDERS = ("trade_detail", "kapt_info", "public_documents", "current", "closed", "demolition",
                     "closed_outline", "closed_land")


def today():
    return datetime.now(KST).date()


class SourceError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def credentials():
    return {KEY_NAME: os.environ.get(KEY_NAME)}


def request(url):
    # API errors may echo URLs. Report only safe status codes.
    try:
        with build_opener(NoRedirect()).open(Request(url), timeout=18) as response:
            body = response.read(20_000_001)
    except HTTPError as exc:
        raise SourceError(f"HTTP {exc.code}") from None
    except (URLError, OSError, HTTPException):
        raise SourceError("연결 실패") from None
    if len(body) > 20_000_000:
        raise SourceError("응답 크기 초과")
    return body


def code(value):
    value = str(value or "")
    return value if re.fullmatch(r"[A-Z0-9_-]{1,18}", value) else "미확인"


def molit_page(raw, service, page):
    try:
        if raw.lstrip().startswith(b"<"):
            root = ElementTree.fromstring(raw)
            status = root.findtext("./header/resultCode") or root.findtext(".//returnReasonCode")
            if status not in ("00", "0", "000"):
                raise SourceError(f"응답 코드 {code(status)}")
            body = root.find("body")
            return int(body.findtext("totalCount")), [
                {node.tag: node.text for node in item} for item in body.findall("./items/item")
            ] if int(body.findtext("pageNo")) == page else []
        response = json.loads(raw)
        if "OpenAPI_ServiceResponse" in response:
            error = response["OpenAPI_ServiceResponse"]["cmmMsgHeader"]
            raise SourceError(f"응답 코드 {code(error.get('returnReasonCode'))} ({code(error.get('errMsg'))})")
        response = response.get("response", response)
        status = str(response["header"]["resultCode"])
        if status not in ("00", "0", "000"):
            raise SourceError(f"응답 코드 {code(status)}")
        body = response["body"]
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        if int(body["pageNo"]) != page or not isinstance(items, list):
            raise SourceError("페이지 응답 불일치")
        return int(body["totalCount"]), items
    except (ValueError, TypeError, KeyError, AttributeError, ElementTree.ParseError):
        raise SourceError("응답 형식 불일치") from None


def cache_file(cache, service, params):
    digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
    return cache / service / f"{digest}.json"


def cached(cache, service, params):
    p = cache_file(cache, service, params)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def request_molit(service, url):
    for delay in (1.2, 2.5, 0.0):
        try:
            return request(url)
        except SourceError as exc:
            if str(exc) != "HTTP 429" or not delay:
                raise
            time.sleep(delay)


def fetch(cache, keys, service, params, *, refresh=False, ask=None, parse=None):
    """Fetch and cache only complete pages. ask and parse replace request_molit and molit_page (collect.py)."""
    p = cache_file(cache, service, params)
    if p.exists() and not refresh:
        return cached(cache, service, params)
    key = keys.get(KEY_NAME)
    if not key:
        raise SourceError("인증키 미설정")
    ask, parse = ask or request_molit, parse or molit_page
    result = []
    page_size = 1000 if service.startswith("trade") else 100
    total = None
    seen_pages = set()
    for page in range(1, 1001):
        query = dict(params, serviceKey=unquote(key))
        query.update(pageNo=page, numOfRows=page_size)
        raw = ask(service, f"{MOLIT}{SERVICES[service]}?{urlencode(query)}")
        count, rows = parse(raw, service, page)
        if total is None:
            total = count
        if count != total or count < 0 or not isinstance(rows, list) or len(result) + len(rows) > count:
            raise SourceError("전체 건수 불일치")
        if any(not isinstance(row, dict) for row in rows):
            raise SourceError("행 형식 불일치")
        signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).digest()
        if signature in seen_pages or (not rows and len(result) < count):
            raise SourceError("페이지 중복 또는 누락")
        seen_pages.add(signature)
        result.extend(rows)
        if len(result) == count:
            break
    else:
        raise SourceError("페이지 상한 도달")
    snapshot = {"retrieved_on": today().isoformat(), "total": total, "rows": result}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)
    return snapshot


def parcel_params(parcel):
    parts = re.split("[_|]", parcel)
    if not re.fullmatch(r"\d{5}[_|]\d{5}[_|][012][_|]\d{4}[_|]\d{4}", parcel):
        raise ValueError("유효하지 않은 지번키")
    return dict(zip(("sigunguCd", "bjdongCd", "platGbCd", "bun", "ji"), parts, strict=True))


def complete_snapshot(cache, service, parcel, as_of):
    snap = cached(cache, service, parcel)
    day = ymd(text(snap, "retrieved_on")) if snap else ""
    if day and day <= as_of.isoformat() and snap["total"] == len(snap["rows"]):
        return snap["rows"]
    return None
