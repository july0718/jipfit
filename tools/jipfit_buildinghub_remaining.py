#!/usr/bin/env python3
"""Resume the Seoul parcel BuildingHUB audit without overwriting old results.

Run locally where jipfit_full_targets.csv, existing per-parcel JSON and the
DATA_GO_KR_API_KEY environment variable are available. Python >=3.10; stdlib only.
This script NEVER updates Jipfit PostgreSQL, maps, or trade histories.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))
BASE_URL = "https://apis.data.go.kr/1613000"
SERVICES = {
    "building_registry": "BldRgstHubService/getBrTitleInfo",
    "closed_registry": "ShtRgstHubService/getSrTitleInfo",
    "demolition_registry": "ArchPmsHubService/getApDemolExtngMgmRgstInfo",
}
SUCCESS_CODES = {"0", "00", "000", "0000", "NORMAL_SERVICE", "NORMAL SERVICE"}
QUOTA_CODES = {"22", "23", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_PER_SECOND_EXCEEDS_ERROR"}
AUTH_CODES = {"20", "30", "31", "32", "SERVICE_ACCESS_DENIED_ERROR", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "DEADLINE_HAS_EXPIRED_ERROR", "UNREGISTERED_IP_ERROR"}
QUOTA_WORDS = ("LIMITED_NUMBER_OF_SERVICE_REQUESTS", "일일 호출", "호출 허용량", "요청제한횟수", "요청 한도 초과", "초당 호출")


class ApiStop(RuntimeError):
    """Stop entire batch; no partial per-parcel JSON is written."""


class QuotaStop(ApiStop):
    pass


class AuthStop(ApiStop):
    pass


class RetryableError(RuntimeError):
    pass


class ParcelError(RuntimeError):
    pass


def now_iso():
    return datetime.now(KST).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_child(elem: ET.Element, name: str):
    return next((child for child in elem if local_name(child.tag) == name), None)


def nested_to_dict(node: ET.Element):
    children = list(node)
    if not children:
        return (node.text or "").strip()
    output: dict[str, Any] = {}
    for child in children:
        k = local_name(child.tag)
        v = nested_to_dict(child)
        if k in output:
            if not isinstance(output[k], list):
                output[k] = [output[k]]
            output[k].append(v)
        else:
            output[k] = v
    return output


def identify_api_error(text: str) -> tuple[str, str] | None:
    upper = text.upper()
    try:
        root = ET.fromstring(text)
        codes = [(local_name(e.tag), (e.text or "").strip()) for e in root.iter()]
        for name, value in codes:
            if name in {"resultCode", "returnReasonCode", "errorCode"} and value:
                message = " ".join(v for n, v in codes if n in {"resultMsg", "returnAuthMsg", "errMsg", "errorMessage"})
                if value in QUOTA_CODES or "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in value:
                    return "quota", f"{value} {message}".strip()
                if value in AUTH_CODES:
                    return "auth", f"{value} {message}".strip()
                if value not in SUCCESS_CODES:
                    return "error", f"{value} {message}".strip()
    except ET.ParseError:
        pass
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            header = data.get("response", {}).get("header", {}) if isinstance(data.get("response"), dict) else data
            code = str(header.get("resultCode", header.get("returnReasonCode", ""))).strip()
            msg = str(header.get("resultMsg", header.get("errMsg", "")))
            if code in QUOTA_CODES or "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in code:
                return "quota", f"{code} {msg}".strip()
            if code in AUTH_CODES:
                return "auth", f"{code} {msg}".strip()
            if code and code not in SUCCESS_CODES:
                return "error", f"{code} {msg}".strip()
    except (ValueError, TypeError):
        pass
    if any(word in upper for word in QUOTA_WORDS):
        return "quota", "API response explicitly indicates a rate or daily quota limit"
    return None


def decode_xml_page(raw: bytes) -> tuple[int, list[dict[str, Any]], int | None]:
    text = raw.decode("utf-8-sig", errors="replace")
    error = identify_api_error(text)
    if error:
        category, info = error
        if category == "quota":
            raise QuotaStop(info)
        if category == "auth":
            raise AuthStop(info)
        raise ParcelError(f"API result error: {info}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RetryableError("API did not return valid XML") from exc
    body = next((node for node in root.iter() if local_name(node.tag) == "body"), None)
    if body is None:
        raise RetryableError("API response is missing body")
    total_node = first_child(body, "totalCount")
    if total_node is None or not (total_node.text or "").strip().isdigit():
        raise RetryableError("API response has no valid totalCount")
    total = int((total_node.text or "").strip())
    items_node = first_child(body, "items")
    items = [nested_to_dict(node) for node in items_node if local_name(node.tag) == "item"] if items_node is not None else []
    page_node = first_child(body, "pageNo")
    reported_page = int(page_node.text) if page_node is not None and (page_node.text or "").isdigit() else None
    return total, items, reported_page


class Budget:
    def __init__(self, path: Path, daily_cap: int):
        self.path, self.daily_cap = path, daily_cap
        self.day = datetime.now(KST).date().isoformat()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "date_kst" not in data or "requests" not in data:
                raise ApiStop("Invalid API request counter. Inspect .state/request-budget.json")
        else:
            data = {}
        self.count = int(data.get("requests", 0)) if data.get("date_kst") == self.day else 0
        if self.count < 0:
            raise ApiStop("Invalid negative request count")

    def account_before_request(self):
        today = datetime.now(KST).date().isoformat()
        if today != self.day:
            self.day, self.count = today, 0
        if self.daily_cap and self.count >= self.daily_cap:
            raise QuotaStop(f"Local daily request cap ({self.daily_cap}) reached for {self.day}")
        self.count += 1
        atomic_json(self.path, {"date_kst": self.day, "requests": self.count, "updated_at": now_iso()})


def http_get(url: str, budget: Budget, timeout: float, pause: float) -> bytes:
    budget.account_before_request()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jipfit-buildinghub-audit/2"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read()
        return data
    except urllib.error.HTTPError as exc:
        body = exc.read(8192).decode("utf-8-sig", errors="replace")
        parsed = identify_api_error(body)
        if exc.code == 429 or (parsed and parsed[0] == "quota"):
            raise QuotaStop(f"HTTP {exc.code}: API quota exceeded") from exc
        if exc.code in {401, 403} or (parsed and parsed[0] == "auth"):
            raise AuthStop(f"HTTP {exc.code}: API access/key denied") from exc
        if exc.code in {408, 425, 500, 502, 503, 504}:
            raise RetryableError(f"HTTP {exc.code}") from exc
        raise ParcelError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RetryableError(f"Network connection failure: {type(exc).__name__}") from exc
    finally:
        if pause > 0:
            time.sleep(pause)


@dataclass(frozen=True)
class Parcel:
    sgg: str
    dong: str
    land: str
    bun: str
    ji: str
    address: str

    @property
    def filename(self):
        return f"{self.sgg}_{self.dong}_{self.land}_{self.bun}_{self.ji}.json"

    def query(self):
        return {"sigunguCd": self.sgg, "bjdongCd": self.dong, "platGbCd": self.land, "bun": self.bun, "ji": self.ji}


def field(row: dict[str, str], *aliases):
    normalized = {re.sub(r"[\s_\-]", "", key).lower(): str(value or "").strip() for key, value in row.items() if key}
    for key in aliases:
        val = normalized.get(re.sub(r"[\s_\-]", "", key).lower(), "")
        if val:
            return val
    return ""


def parse_parcel(row: dict[str, str]) -> Parcel:
    address = field(row, "address", "parcel_address", "주소", "지번주소", "대지위치", "platPlc")
    code = field(row, "법정동코드", "legal_dong_code", "bjdong_code_10")
    sgg = field(row, "sigunguCd", "sigungu_cd", "시군구코드", "sgg_cd", "sggCode")
    dong = field(row, "bjdongCd", "bjdong_cd", "법정동코드5자리", "dongCode", "bjdong")
    if re.fullmatch(r"\d{10}", code) and not (sgg and dong):
        sgg, dong = code[:5], code[5:]
    if re.fullmatch(r"\d{10}", dong) and not sgg:
        sgg, dong = dong[:5], dong[5:]
    land = field(row, "platGbCd", "plat_gb_cd", "대지구분코드", "land_type", "산여부")
    bun = field(row, "bun", "본번", "본번지", "main_lot", "mainNo", "bonbun")
    ji = field(row, "ji", "부번", "부번지", "sub_lot", "subNo", "bubun")
    if not bun:
        lot = field(row, "지번", "lot", "parcel_no", "jibun")
        if not lot and address:
            m = re.search(r"(?:^|\s)(산?\s*\d+(?:-\d+)?)\s*$", address)
            lot = m.group(1).replace(" ", "") if m else ""
        match = re.fullmatch(r"\s*(산)?\s*(\d+)(?:-(\d+))?\s*", lot)
        if match:
            land = land or ("1" if match.group(1) else "0")
            bun, ji = match.group(2), match.group(3) or "0"
    if not ji:
        ji = "0"
    if not land:
        land = "1" if re.search(r"\b산\s*\d", address) else "0"
    if land in ("산", "산번지", "Y", "y", "true", "True"):
        land = "1"
    if land in ("대지", "일반", "N", "n", "false", "False"):
        land = "0"
    if not re.fullmatch(r"\d{5}", sgg) or not re.fullmatch(r"\d{5}", dong):
        raise ValueError("Missing/invalid 5-digit sigunguCd or bjdongCd")
    if land not in {"0", "1", "2"}:
        raise ValueError("Invalid platGbCd")
    if not bun.isdigit() or not ji.isdigit() or len(bun) > 4 or len(ji) > 4:
        raise ValueError("Invalid bun/ji; nonstandard parcels require an official address mapping")
    return Parcel(sgg, dong, land, bun.zfill(4), ji.zfill(4), address)


def read_targets(path: Path) -> tuple[list[Parcel], list[dict]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp949")
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        raise ValueError("Target CSV is empty")
    valid, invalid, seen = [], [], set()
    for lineno, row in enumerate(rows, 2):
        try:
            p = parse_parcel(row)
        except ValueError as exc:
            invalid.append({"line": lineno, "reason": str(exc), "address": field(row, "address", "주소", "지번주소", "대지위치")})
            continue
        if p.filename not in seen:
            seen.add(p.filename)
            valid.append(p)
    return valid, invalid


def explicitly_failed(node: Any, depth=0) -> bool:
    if depth > 12:
        return False
    if isinstance(node, dict):
        for key, value in node.items():
            k = key.lower()
            if k in {"status", "state"} and str(value).lower() in {"error", "failed", "failure", "partial", "incomplete"}:
                return True
            if k in {"resultcode", "returnreasoncode"} and str(value) not in SUCCESS_CODES:
                return True
            if k in {"error", "exception", "failure"} and value not in (None, "", False, [], {}):
                return True
            if explicitly_failed(value, depth + 1):
                return True
    elif isinstance(node, list):
        return any(explicitly_failed(x, depth + 1) for x in node)
    return False


def completed_result(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(payload, dict) and bool(payload) and not explicitly_failed(payload)


def collect_service(service: str, parcel: Parcel, key: str, budget: Budget, *, rows_per_page: int,
                    max_pages: int, attempts: int, timeout: float, pause: float) -> dict[str, Any]:
    expected = None
    items: list[dict[str, Any]] = []
    pages = 0
    for page in range(1, max_pages + 1):
        query = {"serviceKey": key, **parcel.query(), "numOfRows": rows_per_page, "pageNo": page}
        url = f"{BASE_URL}/{SERVICES[service]}?{urllib.parse.urlencode(query)}"
        for attempt in range(1, attempts + 1):
            try:
                raw = http_get(url, budget, timeout, pause)
                total, page_items, reported_page = decode_xml_page(raw)
                break
            except (QuotaStop, AuthStop):
                raise
            except (RetryableError, ParcelError):
                if attempt == attempts:
                    raise
                time.sleep(min(2 ** (attempt - 1), 8))
        if reported_page is not None and reported_page != page:
            raise ParcelError(f"Pagination mismatch: page {page}, API page {reported_page}")
        if expected is None:
            expected = total
        if total != expected:
            raise ParcelError(f"totalCount changed between pages: {expected} -> {total}")
        if not isinstance(page_items, list):
            raise ParcelError("Unexpected item payload")
        items.extend(page_items)
        pages += 1
        if len(items) == expected:
            break
        if not page_items or len(items) > expected:
            raise ParcelError(f"Incomplete/duplicated page: {len(items)} of {expected}")
    if len(items) != expected:
        raise ParcelError(f"Pagination page limit ({max_pages}) reached: {len(items)} of {expected}")
    return {"totalCount": expected, "pages": pages, "items": items, "endpoint": SERVICES[service]}


def save_csv(path: Path, columns: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def run(args) -> int:
    targets = Path(args.targets).expanduser().resolve()
    results = Path(args.results).expanduser().resolve()
    if not targets.is_file():
        raise ValueError(f"Target CSV not found: {targets}")
    valid, invalid = read_targets(targets)
    if args.expected_total and len(valid) + len(invalid) != args.expected_total:
        raise ValueError(f"Target total {len(valid)} valid + {len(invalid)} invalid != expected {args.expected_total}. Check columns and duplicates before making API calls.")
    saved = [p for p in valid if completed_result(results / p.filename)]
    pending = [p for p in valid if not completed_result(results / p.filename)]
    print(f"TARGETS total_rows={len(valid)+len(invalid)} valid_unique={len(valid)} invalid={len(invalid)}")
    print(f"PREVIOUS completed_json={len(saved)} remaining_valid={len(pending)}")
    print(f"RESULTS {results}")
    if args.dry_run:
        if invalid:
            print("INVALID TARGETS (first 10)", json.dumps(invalid[:10], ensure_ascii=False))
        return 0
    if not args.daily_request_cap and not args.confirm_unlimited_daily_cap:
        raise ValueError("Supply --daily-request-cap below the approved API quota, or explicitly --confirm-unlimited-daily-cap. No requests sent.")
    key = os.environ.get("DATA_GO_KR_API_KEY", "").strip()
    if not key or "여기에" in key:
        raise ValueError("DATA_GO_KR_API_KEY is missing. Set the decoding API key in local environment, not the repository.")
    if invalid and not args.allow_invalid_targets:
        raise ValueError("Target CSV has invalid/nonstandard parcels. Fix them or use --allow-invalid-targets; no requests sent.")
    state = results / ".state"
    state.mkdir(parents=True, exist_ok=True)
    lock = state / "audit.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()}\nstarted={now_iso()}\n")
    except FileExistsError as exc:
        raise ApiStop("Another audit or stale audit.lock exists. Check processes and remove stale lock manually.") from exc
    try:
        budget = Budget(state / "request-budget.json", args.daily_request_cap)
        if invalid:
            save_csv(state / "invalid-targets.csv", ["line", "address", "reason"], invalid)
        if args.max_records > 0:
            pending = pending[:args.max_records]
        started = now_iso()
        processed, errors, error_streak, stop_reason = 0, [], 0, ""
        before = budget.count
        for pos, parcel in enumerate(pending, 1):
            filename = parcel.filename
            dest = results / filename
            if completed_result(dest):
                continue
            print(f"[{pos}/{len(pending)}] {parcel.address or filename}", flush=True)
            try:
                payload = {"schema_version": "jipfit-buildinghub-audit-v2", "retrieved_at": now_iso(),
                           "parcel": {**parcel.query(), "address": parcel.address}}
                for service in SERVICES:
                    payload[service] = collect_service(service, parcel, key, budget,
                        rows_per_page=args.rows_per_page, max_pages=args.max_pages,
                        attempts=args.attempts, timeout=args.timeout, pause=args.pause)
                if dest.exists():
                    backup = state / "backups" / f"{dest.stem}.{datetime.now(KST).strftime('%Y%m%dT%H%M%S%f')}.json"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, backup)
                atomic_json(dest, payload)
                processed += 1
                error_streak = 0
            except (QuotaStop, AuthStop) as exc:
                stop_reason = f"{type(exc).__name__}: {exc}"
                print(f"STOP {stop_reason}", file=sys.stderr, flush=True)
                break
            except (RetryableError, ParcelError, ValueError, OSError) as exc:
                error_streak += 1
                print(f"FAILED {filename}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                errors.append({"filename": filename, "reason": f"{type(exc).__name__}: {exc}"})
                if error_streak >= args.max_consecutive_errors:
                    stop_reason = f"Too many consecutive parcel failures ({error_streak})"
                    print(f"STOP {stop_reason}", file=sys.stderr, flush=True)
                    break
        if errors:
            save_csv(state / "last-run-errors.csv", ["filename", "reason"], errors)
        else:
            (state / "last-run-errors.csv").unlink(missing_ok=True)
        final_saved = sum(completed_result(results / p.filename) for p in valid)
        summary = {"started": started, "finished": now_iso(), "total_valid_targets": len(valid),
                   "invalid_targets": len(invalid), "previously_completed": len(saved),
                   "newly_completed": processed, "completed_now": final_saved,
                   "remaining_valid": len(valid) - final_saved, "batch_failures": len(errors),
                   "api_requests_this_run": budget.count - before,
                   "api_requests_today": budget.count, "stop_reason": stop_reason,
                   "note": "No Jipfit DB, trades, coordinates, or map statuses changed"}
        atomic_json(state / "last-run-summary.json", summary)
        print("SUMMARY", json.dumps(summary, ensure_ascii=False), flush=True)
        return 3 if stop_reason else 2 if errors else 0
    finally:
        lock.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Audit missing Seoul parcels against three BuildingHUB APIs, safely resumable")
    p.add_argument("--targets", required=True, help="Existing jipfit_full_targets.csv")
    p.add_argument("--results", required=True, help="Existing jipfit_buildinghub_results directory")
    p.add_argument("--dry-run", action="store_true", help="Only count valid, existing and missing targets; never call API")
    p.add_argument("--expected-total", type=int, default=9188, help="Expected count of UNIQUE parcels (0 disables this guard)")
    p.add_argument("--max-records", type=int, default=200, help="New parcels per run; 0 = all remaining")
    p.add_argument("--daily-request-cap", type=int, default=0, help="Local conservative cap for total API calls per KST day across runs")
    p.add_argument("--confirm-unlimited-daily-cap", action="store_true", help="Explicitly permit run without a known local daily cap; remote 22/23 still stops")
    p.add_argument("--allow-invalid-targets", action="store_true", help="Skip target rows without valid numeric parcel keys; report them")
    p.add_argument("--rows-per-page", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=100)
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--pause", type=float, default=0.35, help="Delay after every HTTP call")
    p.add_argument("--max-consecutive-errors", type=int, default=10)
    args = p.parse_args(argv)
    if args.expected_total < 0 or args.max_records < 0 or args.daily_request_cap < 0 or args.rows_per_page <= 0 or args.max_pages <= 0 or args.attempts <= 0 or args.max_consecutive_errors <= 0 or args.timeout <= 0 or args.pause < 0:
        p.error("Nonnegative caps and positive paging/retry/timeouts are required")
    try:
        return run(args)
    except (ValueError, ApiStop, OSError, json.JSONDecodeError) as exc:
        print(f"STOP {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
