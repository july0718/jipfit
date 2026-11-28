"""Official API calls and their cache, from jipfit_lifecycle@d86dcdf:official_sources.py.

complete_snapshot comes from jipfit_lifecycle@d86dcdf:verify_lifecycle.py. Only the services the monthly
check asks for are kept, the key comes from the environment and retrieval days are KST days.

read_once is new in Jipfit: opening a file on the updater's Windows bind mount is slow and one run asks for
the same registers several times, so inside its block every file is read from disk once.

  JIPFIT_LIFECYCLE_READ_ONCE  0 reads every file again each time it is asked for
"""
import errno
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

from lifecycle_check.common import text, ymd

try:
    import resource
except ImportError:  # Windows, where no monthly run takes place.
    resource = None

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


# Reading once per run

MISSING = object()  # The saved answer for a file that is not there.


class ReadOnce:
    """The files one run has read. Every caller gets the same objects, so none of them may change one."""

    def __init__(self, keep):
        self.keep = keep  # False counts the reads and saves nothing.
        self.lock = threading.Lock()
        self.parsed, self.bodies, self.results = {}, {}, {}
        self.files = set()
        self.disk = self.reused = 0
        self.judge_seconds = None

    def get(self, saved, path, read, parse):
        """The saved answer for the path, else the file read from disk; MISSING when there is no file."""
        key = os.path.abspath(str(path))
        # Held while reading, so threads asking for the same file read it once.
        with self.lock:
            if key in saved:
                self.reused += 1
                return saved[key]
            try:
                raw = read(path)
            except (FileNotFoundError, NotADirectoryError):
                value = MISSING
            else:
                self.disk += 1
                self.files.add(key)
                value = parse(raw)  # A broken file raises here and is not saved.
            if self.keep:
                saved[key] = value
            return value

    def replace(self, path, body):
        """The run wrote the file: save what reading it back would give."""
        if not self.keep:
            return
        key, value = os.path.abspath(str(path)), json.loads(body)
        with self.lock:
            self.parsed[key] = value
            self.bodies.pop(key, None)

    def clear(self):
        with self.lock:
            for saved in (self.parsed, self.bodies, self.results):
                saved.clear()

    def stats(self):
        return {"disk": self.disk, "reused": self.reused, "files": len(self.files)}

    def summary(self):
        parts = [f"디스크 {self.disk:,}회(파일 {len(self.files):,}개)", f"재사용 {self.reused:,}회"]
        if resource:  # The peak of the whole process, in KB on Linux.
            parts.append(f"최대 메모리 {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:,.0f}MB")
        if self.judge_seconds is not None:
            minutes, seconds = divmod(round(self.judge_seconds), 60)
            parts.append(f"판정 {minutes}분 {seconds}초")
        return "Lifecycle 파일 읽기: " + " · ".join(parts)


# A module global rather than a context variable, so the collection threads see the same store.
_store = None


@contextmanager
def read_once():
    """Read each file once until the block ends, then forget it all; an inner block uses the outer one's."""
    global _store
    if _store is not None:
        yield _store
        return
    store = _store = ReadOnce((os.environ.get("JIPFIT_LIFECYCLE_READ_ONCE") or "").strip() != "0")
    try:
        yield store
    finally:
        _store = None
        store.clear()
        print(store.summary(), flush=True)


def read_text(path):
    return path.read_text(encoding="utf-8")


def read_json(path):
    """The parsed JSON file. Inside read_once() the same object is returned again and must not be changed."""
    if _store is None:
        return json.loads(read_text(path))
    value = _store.get(_store.parsed, path, read_text, json.loads)
    if value is MISSING:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
    return value


def read_bytes(path):
    """The file's bytes, read once inside read_once()."""
    if _store is None:
        return path.read_bytes()
    value = _store.get(_store.bodies, path, lambda p: p.read_bytes(), lambda body: body)
    if value is MISSING:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
    return value


def once_per_run(key, make):
    """make()'s result, made once per read_once() block. Only for files that no run writes."""
    if _store is None or not _store.keep:
        return make()
    if key not in _store.results:
        _store.results[key] = make()
    return _store.results[key]


def note_write(path, body):
    """The run wrote this text to a cache file, as fetch does."""
    if _store is not None:
        _store.replace(path, body)


def forget_reads():
    """Files were removed, so nothing saved may answer for them."""
    if _store is not None:
        _store.clear()


def note_judgement(seconds):
    if _store is not None:
        _store.judge_seconds = seconds


def cached(cache, service, params):
    p = cache_file(cache, service, params)
    if _store is None:
        return json.loads(read_text(p)) if p.exists() else None
    value = _store.get(_store.parsed, p, read_text, json.loads)
    return None if value is MISSING else value


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
    body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(p)
    note_write(p, body)
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
