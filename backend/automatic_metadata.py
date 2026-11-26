"""Refresh sourced names and candidate metadata monthly and geocode unpositioned complexes daily."""

class KaptAuthenticationError(Exception):
    pass


class GeocodingServiceError(OSError):
    pass


class _KaptApplyConflict(Exception):
    pass

from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from hashlib import sha256
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg
from psycopg.types.json import Jsonb

from districts import DISTRICTS
from lifecycle_results import apply_lifecycle_results, empty_lifecycle_provider
from refresh_metadata import ROOT, update_complex

NAVER_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
MOLIT_URL = "https://apis.data.go.kr/1613000"
KST = timezone(timedelta(hours=9))
KAPT_LOCK_BASE = 2_000_000


def next_kapt_morning(now_kst=None):
    now = now_kst or datetime.now(KST)
    target = datetime.combine(now.date(), dt_time(6), KST)
    if now >= target:
        target += timedelta(days=1)
    return target


def next_monthly_kapt_morning(now_kst=None):
    now = now_kst or datetime.now(KST)
    six_today = datetime.combine(now.date(), dt_time(6), KST)
    slot_day = now.date() if now >= six_today else (now.date() - timedelta(days=1))
    if slot_day.month == 12:
        return datetime(slot_day.year + 1, 1, 1, 6, 0, tzinfo=KST)
    return datetime(slot_day.year, slot_day.month + 1, 1, 6, 0, tzinfo=KST)


def read_json(url, params, headers=None):
    request = Request(url + "?" + urlencode(params), headers=headers or {})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def address_key(value):
    return " ".join(str(value or "").split()).removeprefix("서울시 ").removeprefix("서울특별시 ")


def canonical_seoul_address(value):
    norm = address_key(value)
    return f"서울특별시 {norm}" if norm else ""


def candidate_key(district, kapt_code, complex_id, field, expected_value, proposed_value, reason):
    raw = "|".join((
        str(district or ""),
        str(kapt_code or ""),
        str(complex_id or ""),
        str(field or ""),
        str(expected_value if expected_value is not None else ""),
        str(proposed_value if proposed_value is not None else ""),
        str(reason or ""),
    ))
    return "cand-" + sha256(raw.encode("utf-8")).hexdigest()[:24]


def record_metadata_candidate(
    db, *, month, batch, district, kapt_code="", complex_id=None,
    field, expected_value=None, proposed_value=None, reason,
    status="pending", candidate_ids=None, payload=None, observed_on=None,
):
    obs_day = observed_on or date.today()
    cid_key = candidate_key(
        district, kapt_code, complex_id, field, expected_value, proposed_value, reason
    )
    source = f"K-apt: {kapt_code}" if kapt_code else "K-apt"
    cand_list = list(candidate_ids if candidate_ids is not None else ([complex_id] if complex_id else []))
    exp_str = str(expected_value) if expected_value is not None else None
    prop_str = str(proposed_value) if proposed_value is not None else None

    db.execute("""
        UPDATE complex_metadata_candidates
        SET status = 'superseded'
        WHERE district = %s
          AND kapt_code = %s
          AND COALESCE(complex_id, '') = %s
          AND field = %s
          AND status = 'pending'
          AND candidate_id <> %s
    """, (district, kapt_code or "", complex_id or "", field, cid_key))

    db.execute("""
        INSERT INTO complex_metadata_candidates(
            candidate_id, district, kapt_code, complex_id, field,
            expected_value, proposed_value, source, first_month, last_month,
            first_observed_on, last_observed_on, status, reason, candidate_ids
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (candidate_id) DO UPDATE
        SET last_month = GREATEST(complex_metadata_candidates.last_month, EXCLUDED.last_month),
            last_observed_on = GREATEST(complex_metadata_candidates.last_observed_on, EXCLUDED.last_observed_on),
            candidate_ids = EXCLUDED.candidate_ids,
            status = CASE
                WHEN complex_metadata_candidates.status = 'superseded' THEN 'pending'
                ELSE complex_metadata_candidates.status
            END
    """, (
        cid_key, district, kapt_code or "", complex_id, field,
        exp_str, prop_str, source, month, month,
        obs_day, obs_day, status, reason, cand_list,
    ))

    db.execute("""
        INSERT INTO complex_metadata_candidate_observations(candidate_id, batch, observed_on, payload)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (candidate_id, batch) DO UPDATE
        SET observed_on = EXCLUDED.observed_on,
            payload = EXCLUDED.payload
    """, (cid_key, batch, obs_day, Jsonb(payload or {})))
    return cid_key


def candidate_address_keys(candidate):
    names = {
        address_key(candidate.get(field))
        for field in ("jibunAddress", "roadAddress")
    }
    elements = {}
    for element in candidate.get("addressElements") or ():
        long_name = (element.get("longName") or "").strip()
        if not long_name:
            continue
        for kind in element.get("types") or ():
            elements[kind] = long_name

    if elements.get("LAND_NUMBER") and (elements.get("DONGMYUN") or elements.get("RI")):
        jibun = " ".join(
            elements[k]
            for k in ("SIDO", "SIGUGUN", "DONGMYUN", "RI", "LAND_NUMBER")
            if elements.get(k)
        )
        names.add(address_key(jibun))

    if elements.get("ROAD_NAME") and elements.get("BUILDING_NUMBER"):
        road = " ".join(
            elements[k]
            for k in ("SIDO", "SIGUGUN", "ROAD_NAME", "BUILDING_NUMBER")
            if elements.get(k)
        )
        names.add(address_key(road))

    names.discard("")
    return names


def exact_point(address, candidates):
    wanted = address_key(address)
    points = set()
    for candidate in candidates:
        names = candidate_address_keys(candidate)
        if wanted not in names:
            continue
        try:
            point = float(candidate["y"]), float(candidate["x"])
        except (KeyError, TypeError, ValueError):
            continue
        if 33 <= point[0] <= 39 and 124 <= point[1] <= 132:
            points.add(point)
    return next(iter(points)) if len(points) == 1 else None


def geocode(today, headers, districts):
    updated = 0
    cache = {}
    failed_addresses = set()

    with psycopg.connect(connect_timeout=8) as db:
        rows = db.execute("""
            SELECT c.complex_id, c.address FROM complexes c
            LEFT JOIN complex_geocode_attempts a USING (complex_id)
            LEFT JOIN complex_geocode_pending p USING (complex_id)
            WHERE (c.latitude IS NULL OR c.longitude IS NULL OR p.complex_id IS NOT NULL)
              AND (a.attempted_on IS NULL OR a.address <> c.address OR a.attempted_on <= %s::date - 7)
              AND split_part(c.complex_id,' ',2) = ANY(%s)
              AND NOT EXISTS (
                  SELECT 1 FROM complex_lifecycle_state ls
                  WHERE ls.complex_id = c.complex_id AND ls.existence = 'demolished'
              )
              AND (p.complex_id IS NOT NULL OR EXISTS (SELECT 1 FROM trades t WHERE t.complex_id=c.complex_id AND NOT t.is_cancelled AND t.price_manwon > 0))
            ORDER BY (p.complex_id IS NOT NULL) DESC, a.attempted_on NULLS FIRST, c.complex_id
            LIMIT 50
        """, (today, [DISTRICTS[code] for code in districts])).fetchall()
        db.commit()

        def record_permanent_error(cid, address):
            with db.transaction():
                db.execute("""
                    INSERT INTO complex_geocode_attempts(complex_id,address,attempted_on,last_error_at)
                    SELECT v.cid, v.addr, v.day, now()
                    FROM (VALUES (%s::text, %s::text, %s::date)) AS v(cid, addr, day)
                    JOIN complexes c ON c.complex_id = v.cid AND c.address = v.addr
                    WHERE (c.latitude IS NULL OR c.longitude IS NULL
                           OR EXISTS (SELECT 1 FROM complex_geocode_pending p WHERE p.complex_id = c.complex_id))
                      AND NOT EXISTS (
                          SELECT 1 FROM complex_lifecycle_state ls
                          WHERE ls.complex_id = c.complex_id AND ls.existence = 'demolished'
                      )
                    ON CONFLICT (complex_id) DO UPDATE
                    SET address=EXCLUDED.address, attempted_on=EXCLUDED.attempted_on, last_error_at=now()
                """, (cid, address, today))

        for cid, address in rows:
            if address in failed_addresses:
                record_permanent_error(cid, address)
                continue
            if address not in cache:
                try:
                    response = read_json(NAVER_URL, {"query": address}, headers)
                except HTTPError as exc:
                    if exc.code not in (400, 404, 422):
                        raise
                    record_permanent_error(cid, address)
                    failed_addresses.add(address)
                    continue
                except (URLError, TimeoutError, ConnectionError):
                    raise
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise GeocodingServiceError("Geocoding invalid JSON") from None

                if not isinstance(response, dict):
                    raise GeocodingServiceError("Geocoding status=non_dict")
                status = response.get("status")
                if status != "OK" or not isinstance(response.get("addresses"), list):
                    raise GeocodingServiceError(f"Geocoding status={status!r}")

                try:
                    cache[address] = exact_point(address, response["addresses"])
                except (ValueError, KeyError, TypeError, AttributeError):
                    record_permanent_error(cid, address)
                    failed_addresses.add(address)
                    continue
            point = cache[address]
            with db.transaction():
                # Serialize with verified metadata edits and discard stale results or already-verified coordinates.
                if not db.execute("""
                    SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s FOR UPDATE
                """, (cid, address)).fetchone():
                    continue
                if point:
                    rowcount = db.execute("""
                        UPDATE complexes SET latitude=%s, longitude=%s
                        WHERE complex_id=%s AND address=%s AND (latitude IS NULL OR longitude IS NULL
                            OR EXISTS (SELECT 1 FROM complex_geocode_pending WHERE complex_id=%s))
                    """, (*point, cid, address, cid)).rowcount
                    if not rowcount:
                        continue
                    updated += rowcount
                    db.execute("DELETE FROM complex_geocode_pending WHERE complex_id=%s", (cid,))
                    db.execute("""
                        INSERT INTO complex_geocode_attempts(complex_id,address,attempted_on) VALUES (%s,%s,%s)
                        ON CONFLICT (complex_id) DO UPDATE
                        SET address=EXCLUDED.address, attempted_on=EXCLUDED.attempted_on, last_error_at=NULL
                    """, (cid, address, today))
                else:
                    db.execute("""
                        INSERT INTO complex_geocode_attempts(complex_id,address,attempted_on)
                        SELECT v.cid, v.addr, v.day
                        FROM (VALUES (%s::text, %s::text, %s::date)) AS v(cid, addr, day)
                        JOIN complexes c ON c.complex_id = v.cid AND c.address = v.addr
                        WHERE c.latitude IS NULL OR c.longitude IS NULL
                           OR EXISTS (SELECT 1 FROM complex_geocode_pending p WHERE p.complex_id = c.complex_id)
                        ON CONFLICT (complex_id) DO UPDATE
                        SET address=EXCLUDED.address, attempted_on=EXCLUDED.attempted_on, last_error_at=NULL
                    """, (cid, address, today))
    print(
        f"Naver geocoding: considered={len(rows)} updated={updated} failed_addresses={len(failed_addresses)}",
        flush=True,
    )


def kapt_body(endpoint, key, **params):
    time.sleep(0.05)
    try:
        payload = read_json(
            MOLIT_URL + "/" + endpoint,
            {"serviceKey": key, **params}
        )
    except HTTPError as error:
        if error.code in (401, 403):
            raise KaptAuthenticationError(
                f"HTTP_{error.code}"
            ) from error
        raise
    if "OpenAPI_ServiceResponse" in payload:
        header = payload["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
        code = str(header.get("returnReasonCode", ""))
        # Only log a short numeric reason code; never log the credentialed URL.
        reason = code if code.isascii() and code.isdigit() and len(code) <= 3 else "unknown"
        print(f"K-apt {endpoint}: portal error {reason} after 1 request(s)", flush=True)
        raise ValueError("K-apt portal error")
    response = payload["response"]
    if str(response["header"]["resultCode"]) not in {"0", "00", "000", "0000"}:
        raise ValueError("K-apt API returned an error")
    return response["body"]


def kapt_listing(district, key):
    codes = set()
    code_dongs = {}
    page = 1
    expected = None
    while True:
        body = kapt_body("AptListService4/getSigunguAptList4", key,
                         sigunguCode=district, pageNo=page, numOfRows=100)
        total = int(body["totalCount"])
        if total <= 0:
            raise ValueError("Empty K-apt district listing")
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        if expected is None:
            expected = total
        new_codes = {row["kaptCode"] for row in items}
        if total != expected or len(new_codes) != len(items) or codes & new_codes:
            raise ValueError("K-apt listing changed during pagination")
        codes.update(new_codes)
        for row in items:
            code_dongs[row["kaptCode"]] = str(row.get("as3") or "").strip()
        if len(codes) == total:
            return sorted(codes), code_dongs
        if not items or len(codes) > total:
            raise ValueError("Incomplete K-apt listing")
        page += 1


def kapt_codes(district, key):
    return kapt_listing(district, key)[0]


def dong_of_address(address):
    parts = address_key(address).split()
    return parts[1] if len(parts) >= 2 else ""


def complete_detail(item, code):
    if not isinstance(item, dict) or item.get("kaptCode") != code:
        return False
    raw_date = str(item.get("kaptUsedate") or "").replace("-", "")
    if len(raw_date) != 8 or not raw_date.isascii() or not raw_date.isdigit():
        return False
    try:
        date.fromisoformat(f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}")
    except ValueError:
        return False
    return all(isinstance(item.get(k), str) and item[k].strip() for k in ("kaptName", "kaptAddr"))


def kapt_matches(existing, source):
    """Require a unique property at the same parcel and original build year."""
    current = defaultdict(list)
    candidates = defaultdict(list)
    for cid, name, address, built in existing:
        current[address_key(address), str(built)].append((cid, name, address))
    for code, record in source:
        raw_date = str(record.get("kaptUsedate") or "").replace("-", "")
        address = address_key(record.get("kaptAddr"))
        name = " ".join(str(record.get("kaptName") or "").split())
        if address and name and len(raw_date) == 8 and raw_date.isdigit():
            candidates[address, raw_date[:4]].append((code, name))
    for key, items in candidates.items():
        matches = current.get(key, [])
        if len(items) == len(matches) == 1:
            code, name = items[0]
            cid, old_name, address = matches[0]
            yield cid, address, old_name, code, name


def defer_kapt_detail(db, batch, district, code, reason):
    db.execute(
        """
        INSERT INTO kapt_metadata_pending
            (batch, district, kapt_code,
             reason, status, attempts,
             last_attempt_at, next_retry_at)
        SELECT %s, %s, %s, %s, 'pending', 1,
               NOW(), NOW() + INTERVAL '30 minutes'
        WHERE NOT EXISTS (
            SELECT 1 FROM complex_metadata_runs r
            WHERE (r.batch = %s AND r.checksum = 'automatic:kapt')
               OR (r.batch LIKE %s AND r.batch > %s)
        )
        ON CONFLICT (batch, kapt_code)
        DO UPDATE SET
            reason = EXCLUDED.reason,
            status = 'pending',
            attempts = kapt_metadata_pending.attempts + 1,
            last_attempt_at = NOW(),
            next_retry_at = NOW() + INTERVAL '30 minutes'
        """,
        (batch, district, code, reason, batch, f"kapt-%-{district}", batch)
    )


def refresh_kapt_district(month, key, district):
    district_name = DISTRICTS[district]
    batch = f"kapt-{month}-{district}"
    with psycopg.connect(connect_timeout=8) as db:
        newer_run = db.execute("""
            SELECT 1 FROM complex_metadata_runs
            WHERE batch LIKE %s AND batch > %s
            LIMIT 1
        """, (f"kapt-%-{district}", batch)).fetchone()
        if newer_run:
            db.execute("""
                UPDATE kapt_metadata_pending
                SET status = 'superseded', next_retry_at = NULL
                WHERE batch = %s AND status = 'pending'
            """, (batch,))
            db.execute("DELETE FROM kapt_metadata_cache WHERE batch = %s", (batch,))
            return "completed"
        run_row = db.execute("""
            SELECT checksum
            FROM complex_metadata_runs
            WHERE batch = %s
        """, (batch,)).fetchone()
        if run_row and run_row[0] == "automatic:kapt":
            db.execute("""
                UPDATE kapt_metadata_pending
                SET status = 'resolved', next_retry_at = NULL
                WHERE batch = %s AND status = 'pending'
            """, (batch,))
            db.execute("DELETE FROM kapt_metadata_cache WHERE batch = %s", (batch,))
            return "completed"
        if run_row and run_row[0] == "automatic:kapt:partial":
            has_pending = db.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM kapt_metadata_pending
                    WHERE batch = %s AND status = 'pending'
                )
            """, (batch,)).fetchone()[0]
            if has_pending:
                has_due = db.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM kapt_metadata_pending
                        WHERE batch = %s AND status = 'pending'
                          AND COALESCE(next_retry_at <= NOW(), TRUE)
                    )
                """, (batch,)).fetchone()[0]
                if not has_due:
                    has_retryable = db.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM kapt_metadata_pending
                            WHERE batch = %s AND status = 'pending'
                              AND reason <> 'EMPTY_DETAIL'
                        )
                    """, (batch,)).fetchone()[0]
                    return "partial" if has_retryable else "empty_detail_only"
    source = []
    codes, code_dongs = kapt_listing(district, key)
    retryable_deferred = []
    empty_detail_deferred = []

    with psycopg.connect(
        connect_timeout=8,
        autocommit=True
    ) as cache_db:

        # 정상 조회된 단지 캐시
        cached = dict(cache_db.execute(
            """
            SELECT kapt_code, payload
            FROM kapt_metadata_cache
            WHERE batch = %s
            """,
            (batch,)
        ).fetchall())

        # 이번 달 최신 목록에서 사라진 과거 보류 코드는 absent로 정리
        cache_db.execute("""
            UPDATE kapt_metadata_pending
            SET status = 'absent', next_retry_at = NULL
            WHERE batch = %s AND status = 'pending'
              AND NOT (kapt_code = ANY(%s::text[]))
        """, (batch, codes))

        # 상세정보가 비어 있거나 실패해 보류된 단지
        pending = {
            row[0]: (row[1], row[2])
            for row in cache_db.execute(
                """
                SELECT kapt_code,
                       COALESCE(next_retry_at <= NOW(), TRUE),
                       reason
                FROM kapt_metadata_pending
                WHERE batch = %s AND status = 'pending'
                """,
                (batch,)
            ).fetchall()
        }

        for index, code in enumerate(codes, 1):

            # 재조회 시각 전이라면 해당 단지만 건너뜀
            if code in pending and not pending[code][0]:
                if pending[code][1] == "EMPTY_DETAIL":
                    empty_detail_deferred.append(code)
                else:
                    retryable_deferred.append(code)
                continue

            # 정상 캐시가 있으면 API 재호출 생략
            if code not in pending and complete_detail(cached.get(code), code):
                item = cached[code]

            else:
                try:
                    body = kapt_body(
                        "AptBasisInfoServiceV5/getAphusBassInfoV5",
                        key,
                        kaptCode=code,
                    )
                    if not isinstance(body, dict):
                        raise TypeError("Unexpected K-apt detail body")
                    item = body.get("item")

                except (
                    HTTPError, URLError, TimeoutError,
                    ValueError, KeyError, TypeError
                ) as exc:
                    status = getattr(exc, "code", None)

                    reason = (
                        f"HTTP_{status}"
                        if isinstance(exc, HTTPError)
                        else type(exc).__name__
                    )

                    defer_kapt_detail(
                        cache_db, batch, district, code, reason
                    )

                    retryable_deferred.append(code)

                    print(
                        f"K-apt SKIPPED: district={district} "
                        f"progress={index}/{len(codes)} "
                        f"code={code} reason={reason}",
                        flush=True
                    )

                    continue

                # 31개 필드처럼 구조는 있지만 값이 모두 빈 경우
                incomplete = isinstance(item, dict) and item.get("kaptCode") == code and not complete_detail(item, code)
                if incomplete or item is None or (
                    isinstance(item, dict)
                    and all(
                        value is None
                        or (isinstance(value, str) and not value.strip())
                        for value in item.values()
                    )
                ):
                    retry_at = next_monthly_kapt_morning()
                    cache_db.execute(
                        """
                        INSERT INTO kapt_metadata_pending
                            (batch, district, kapt_code,
                             reason, status, attempts,
                             last_attempt_at, next_retry_at)
                        SELECT %s, %s, %s, 'EMPTY_DETAIL',
                               'pending', 1, NOW(), %s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM complex_metadata_runs r
                            WHERE (r.batch = %s AND r.checksum = 'automatic:kapt')
                               OR (r.batch LIKE %s AND r.batch > %s)
                        )
                        ON CONFLICT (batch, kapt_code)
                        DO UPDATE SET
                            reason = 'EMPTY_DETAIL',
                            status = 'pending',
                            attempts = kapt_metadata_pending.attempts + 1,
                            last_attempt_at = NOW(),
                            next_retry_at = EXCLUDED.next_retry_at
                        """,
                        (batch, district, code, retry_at, batch, f"kapt-%-{district}", batch)
                    )

                    empty_detail_deferred.append(code)

                    print(
                        f"K-apt EMPTY_DETAIL: district={district} "
                        f"progress={index}/{len(codes)} "
                        f"code={code}",
                        flush=True
                    )
                    continue

                # 빈 응답이 아닌 단지코드 불일치는 기존처럼 오류
                if not isinstance(item, dict) or item.get("kaptCode") != code:
                    defer_kapt_detail(
                        cache_db, batch, district, code,
                        "CODE_MISMATCH"
                    )

                    retryable_deferred.append(code)

                    print(
                        f"K-apt SKIPPED: district={district} "
                        f"progress={index}/{len(codes)} "
                        f"code={code} reason=CODE_MISMATCH",
                        flush=True
                    )

                    continue

                # 정상 응답만 캐시에 저장 (이미 완료된 배치에는 남기지 않음)
                cache_db.execute(
                    """
                    INSERT INTO kapt_metadata_cache
                        (batch, kapt_code, payload)
                    SELECT %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM complex_metadata_runs r
                        WHERE (r.batch = %s AND r.checksum = 'automatic:kapt')
                           OR (r.batch LIKE %s AND r.batch > %s)
                    )
                    ON CONFLICT (batch, kapt_code)
                    DO UPDATE SET payload = EXCLUDED.payload
                    """,
                    (batch, code, Jsonb(item), batch, f"kapt-%-{district}", batch)
                )

                cached[code] = item

                # 이전 보류 단지가 정상 복구된 경우
                cache_db.execute(
                    """
                    UPDATE kapt_metadata_pending
                    SET status = 'resolved',
                        last_attempt_at = NOW(),
                        next_retry_at = NULL
                    WHERE batch = %s AND kapt_code = %s
                      AND status = 'pending'
                    """,
                    (batch, code)
                )

            # 캐시 포함 최종 검증
            if not isinstance(item, dict) or item.get("kaptCode") != code:
                print(
                    f"K-apt CACHE MISMATCH: district={district} "
                    f"code={code}",
                    flush=True
                )
                raise ValueError("K-apt detail code mismatch")

            source.append((code, item))

        total_deferred = len(retryable_deferred) + len(empty_detail_deferred)
    checksum = "automatic:kapt:partial" if total_deferred else "automatic:kapt"

    with psycopg.connect(connect_timeout=8) as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (KAPT_LOCK_BASE + int(district),))
        newer_run = db.execute("""
            SELECT 1 FROM complex_metadata_runs
            WHERE batch LIKE %s AND batch > %s
            LIMIT 1
        """, (f"kapt-%-{district}", batch)).fetchone()
        if newer_run:
            db.execute("""
                UPDATE kapt_metadata_pending
                SET status = 'superseded', next_retry_at = NULL
                WHERE batch = %s AND status = 'pending'
            """, (batch,))
            db.execute("DELETE FROM kapt_metadata_cache WHERE batch = %s", (batch,))
            return "completed"
        claimed = db.execute("""
            INSERT INTO complex_metadata_runs(batch, checksum)
            VALUES (%s, %s)
            ON CONFLICT (batch) DO UPDATE
            SET checksum = EXCLUDED.checksum,
                applied_at = clock_timestamp()
            WHERE complex_metadata_runs.checksum = 'automatic:kapt:partial'
            RETURNING batch
        """, (batch, checksum)).fetchone()
        if not claimed:
            # 다른 실행이 이미 이번 달 완료 기록을 남긴 경우 잔여 보류·캐시를 정리하고 종료
            db.execute("""
                UPDATE kapt_metadata_pending
                SET status = 'resolved', next_retry_at = NULL
                WHERE batch = %s AND status = 'pending'
            """, (batch,))
            db.execute("DELETE FROM kapt_metadata_cache WHERE batch = %s", (batch,))
            return "completed"

        existing = db.execute("""
            SELECT complex_id, name, address, built
            FROM complexes
            WHERE split_part(complex_id, ' ', 2) = %s
        """, (district_name,)).fetchall()
        existing_built = {cid: built for cid, _, _, built in existing}
        existing_by_cid = {cid: (name, address, built) for cid, name, address, built in existing}

        applied = 0
        verified = dict(db.execute(
            "SELECT kapt_code,complex_id FROM kapt_verified_links WHERE district=%s", (district,)
        ).fetchall())
        verified_by_cid = {cid: v_code for v_code, cid in verified.items()}
        deferred_codes = set(retryable_deferred) | set(empty_detail_deferred)
        protected = {verified[c]: c for c in deferred_codes if c in verified}
        blocked_dongs = set()
        unknown_dong_deferred = False
        for d_code in deferred_codes:
            if d_code in verified:
                continue
            dong = code_dongs.get(d_code, "")
            if dong:
                blocked_dongs.add(dong)
            else:
                unknown_dong_deferred = True

        held_names = 0
        concurrent_deferred = 0
        matched_codes = set()
        matched_cids = set()
        conflicted_codes = set()
        conflicted_cids = set()

        for cid, address, old_name, code, name in kapt_matches(existing, source):
            if total_deferred:
                if cid in protected and verified.get(code) != cid:
                    held_names += int(name != old_name)
                    continue
                dong = dong_of_address(address)
                safe = (
                    verified.get(code) == cid
                    or (not unknown_dong_deferred and bool(dong) and dong not in blocked_dongs)
                )
                if not safe:
                    held_names += int(name != old_name)
                    continue

            # Preserve existing verified links; if a different link already exists, hold as LINK_CONFLICT.
            prior_cid_for_code = verified.get(code)
            prior_code_for_cid = verified_by_cid.get(cid)
            if (prior_cid_for_code is not None and prior_cid_for_code != cid) or (
                prior_code_for_cid is not None and prior_code_for_cid != code
            ):
                conflicted_codes.add(code)
                conflicted_cids.add(cid)
                if prior_cid_for_code:
                    conflicted_cids.add(prior_cid_for_code)
                if prior_code_for_cid:
                    conflicted_codes.add(prior_code_for_cid)
                held_names += int(name != old_name)
                record_metadata_candidate(
                    db, month=month, batch=batch, district=district,
                    kapt_code=code, complex_id=cid, field="identity",
                    expected_value=prior_code_for_cid or prior_cid_for_code,
                    proposed_value=f"{code}:{cid}",
                    reason="LINK_CONFLICT",
                    candidate_ids=sorted({c for c in (cid, prior_cid_for_code) if c}),
                    payload={"kapt_name": name, "address": address},
                )
                continue

            expected_built = existing_built[cid]
            changes = {"name": name}
            try:
                with db.transaction():
                    updated_name = False
                    if old_name != name:
                        updated_name = update_complex(
                            db, cid, address, changes,
                            expected_name=old_name, expected_built=expected_built,
                        )
                    # Lock the target complex row and verify address, name, and build year right before
                    # recording the verified link so concurrent edits cannot slip in.
                    verified_row = db.execute("""
                        SELECT 1 FROM complexes
                        WHERE complex_id=%s AND address=%s AND name=%s AND built=%s
                        FOR UPDATE
                    """, (cid, address, name, expected_built)).fetchone()
                    if not verified_row:
                        raise _KaptApplyConflict()
                    if verified.get(code) != cid:
                        db.execute("INSERT INTO kapt_verified_links VALUES (%s,%s,%s)", (district, code, cid))
                        verified[code] = cid
                        verified_by_cid[cid] = code
                    if updated_name:
                        db.execute("""
                            INSERT INTO complex_metadata_evidence
                                (batch, complex_id, evidence, changes)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (batch, complex_id) DO UPDATE
                            SET evidence = EXCLUDED.evidence, changes = EXCLUDED.changes
                        """, (batch, cid, "K-apt: " + code, Jsonb(changes)))
                        db.execute("""
                            UPDATE complex_metadata_evidence
                            SET before_values = %s
                            WHERE batch = %s AND complex_id = %s
                        """, (Jsonb({"name": old_name}), batch, cid))
                        applied += 1
                    db.execute("""
                        UPDATE complex_metadata_candidates SET status = 'superseded'
                        WHERE district=%s AND kapt_code=%s AND complex_id=%s AND last_month<=%s
                          AND field IN ('name', 'address', 'built', 'address+built')
                          AND status IN ('pending', 'held')
                    """, (district, code, cid, month))
                    matched_codes.add(code)
                    matched_cids.add(cid)
            except _KaptApplyConflict:
                concurrent_deferred += 1
                defer_kapt_detail(db, batch, district, code, "APPLY_CONFLICT")
                print(
                    f"K-apt {district} {month}: skipped concurrently modified complex {cid}",
                    flush=True,
                )
                continue

        # Detect field change candidates and unlinked/ambiguous holds from remaining K-apt records.
        current_by_parcel_year = defaultdict(list)
        current_by_parcel = defaultdict(list)
        current_by_name = defaultdict(list)
        for cid, name, address, built in existing:
            norm_addr = address_key(address)
            current_by_parcel_year[norm_addr, str(built)].append(cid)
            current_by_parcel[norm_addr].append(cid)
            current_by_name[name].append(cid)

        source_by_parcel_year = defaultdict(list)
        for code, record in source:
            raw_date = str(record.get("kaptUsedate") or "").replace("-", "")
            norm_addr = address_key(record.get("kaptAddr"))
            if norm_addr and len(raw_date) == 8 and raw_date.isdigit():
                source_by_parcel_year[norm_addr, raw_date[:4]].append(code)

        for code, record in source:
            if code in matched_codes or code in conflicted_codes:
                continue
            raw_date = str(record.get("kaptUsedate") or "").replace("-", "")
            if len(raw_date) != 8 or not raw_date.isdigit():
                continue
            kapt_built = int(raw_date[:4])
            kapt_addr_raw = " ".join(str(record.get("kaptAddr") or "").split())
            kapt_addr_norm = address_key(kapt_addr_raw)
            kapt_addr_canon = canonical_seoul_address(kapt_addr_raw)
            kapt_name = " ".join(str(record.get("kaptName") or "").split())
            addr_parts = kapt_addr_norm.split()
            cross_district = not addr_parts or addr_parts[0] != district_name

            linked_cid = verified.get(code)
            if linked_cid and linked_cid in existing_by_cid:
                matched_cids.add(linked_cid)
                old_name, old_address, old_built = existing_by_cid[linked_cid]
                old_addr_norm = address_key(old_address)
                addr_changed = kapt_addr_norm != old_addr_norm
                built_changed = kapt_built != old_built
                name_changed = kapt_name != old_name

                if cross_district:
                    record_metadata_candidate(
                        db, month=month, batch=batch, district=district,
                        kapt_code=code, complex_id=linked_cid, field="address",
                        expected_value=old_address, proposed_value=kapt_addr_canon or kapt_addr_raw,
                        reason="CROSS_DISTRICT_ADDRESS",
                        payload=record,
                    )
                    continue

                if addr_changed and built_changed:
                    record_metadata_candidate(
                        db, month=month, batch=batch, district=district,
                        kapt_code=code, complex_id=linked_cid, field="address+built",
                        expected_value=f"{old_address}|{old_built}",
                        proposed_value=f"{kapt_addr_canon}|{kapt_built}",
                        reason="ADDRESS_AND_YEAR_CHANGED",
                        payload=record,
                    )
                    continue

                if addr_changed:
                    record_metadata_candidate(
                        db, month=month, batch=batch, district=district,
                        kapt_code=code, complex_id=linked_cid, field="address",
                        expected_value=old_address, proposed_value=kapt_addr_canon,
                        reason="VERIFIED_LINK_ADDRESS_DIFF",
                        payload=record,
                    )
                if built_changed:
                    record_metadata_candidate(
                        db, month=month, batch=batch, district=district,
                        kapt_code=code, complex_id=linked_cid, field="built",
                        expected_value=str(old_built), proposed_value=str(kapt_built),
                        reason="VERIFIED_LINK_BUILT_DIFF",
                        payload=record,
                    )
                if name_changed and (addr_changed or built_changed):
                    record_metadata_candidate(
                        db, month=month, batch=batch, district=district,
                        kapt_code=code, complex_id=linked_cid, field="name",
                        expected_value=old_name, proposed_value=kapt_name,
                        reason="VERIFIED_LINK_NAME_DIFF",
                        payload=record,
                    )
                continue

            # Unlinked K-apt code
            if cross_district:
                record_metadata_candidate(
                    db, month=month, batch=batch, district=district,
                    kapt_code=code, complex_id=None, field="address",
                    expected_value=None, proposed_value=kapt_addr_canon or kapt_addr_raw,
                    reason="CROSS_DISTRICT_ADDRESS",
                    payload=record,
                )
                continue

            py_key = (kapt_addr_norm, str(kapt_built))
            same_py_cids = current_by_parcel_year.get(py_key, [])
            same_py_codes = source_by_parcel_year.get(py_key, [])
            if len(same_py_cids) > 1 or (len(same_py_codes) > 1 and same_py_cids):
                for c in same_py_cids:
                    matched_cids.add(c)
                record_metadata_candidate(
                    db, month=month, batch=batch, district=district,
                    kapt_code=code,
                    complex_id=same_py_cids[0] if len(same_py_cids) == 1 else None,
                    field="identity",
                    expected_value=None,
                    proposed_value=f"{kapt_addr_canon}|{kapt_built}|{kapt_name}",
                    reason="MULTIPLE_CANDIDATES",
                    candidate_ids=same_py_cids,
                    payload=record,
                )
                continue

            same_parcel_cids = current_by_parcel.get(kapt_addr_norm, [])
            if same_parcel_cids:
                for c in same_parcel_cids:
                    matched_cids.add(c)
                record_metadata_candidate(
                    db, month=month, batch=batch, district=district,
                    kapt_code=code,
                    complex_id=same_parcel_cids[0] if len(same_parcel_cids) == 1 else None,
                    field="identity",
                    expected_value=None,
                    proposed_value=f"{kapt_addr_canon}|{kapt_built}|{kapt_name}",
                    reason="REBUILD_OR_SPLIT_SUSPECTED",
                    candidate_ids=same_parcel_cids,
                    payload=record,
                )
                continue

            same_name_cids = current_by_name.get(kapt_name, [])
            record_metadata_candidate(
                db, month=month, batch=batch, district=district,
                kapt_code=code,
                complex_id=same_name_cids[0] if len(same_name_cids) == 1 else None,
                field="identity",
                expected_value=None,
                proposed_value=f"{kapt_addr_canon}|{kapt_built}|{kapt_name}",
                reason="UNLINKED_CODE",
                candidate_ids=same_name_cids,
                payload=record,
            )

        # Record uncompared existing complexes in the district.
        codes_set = set(codes)
        empty_set = set(empty_detail_deferred)
        retry_set = set(retryable_deferred)
        for cid, old_name, old_address, old_built in existing:
            if cid in matched_cids or cid in conflicted_cids:
                continue
            linked_code = verified_by_cid.get(cid)
            if linked_code:
                if linked_code in empty_set:
                    record_metadata_candidate(
                        db, month=month, batch=batch, district=district,
                        kapt_code=linked_code, complex_id=cid, field="identity",
                        expected_value=f"{old_address}|{old_built}|{old_name}",
                        proposed_value=None,
                        reason="EMPTY_DETAIL",
                    )
                elif linked_code not in codes_set:
                    record_metadata_candidate(
                        db, month=month, batch=batch, district=district,
                        kapt_code=linked_code, complex_id=cid, field="identity",
                        expected_value=f"{old_address}|{old_built}|{old_name}",
                        proposed_value=None,
                        reason="CODE_MISSING",
                    )
            elif not total_deferred and not retry_set:
                record_metadata_candidate(
                    db, month=month, batch=batch, district=district,
                    kapt_code="", complex_id=cid, field="identity",
                    expected_value=f"{old_address}|{old_built}|{old_name}",
                    proposed_value=None,
                    reason="UNMATCHED_COMPLEX",
                )

        # 현재 달 최신 목록에서 사라진 보류 코드 정리
        db.execute("""
            UPDATE kapt_metadata_pending
            SET status = 'absent', next_retry_at = NULL
            WHERE batch = %s AND status = 'pending'
              AND NOT (kapt_code = ANY(%s::text[]))
        """, (batch, codes))

        # 새 달 목록을 한 차례 처리했으므로 이전 달 보류·캐시는 새 달 조회로 대체합니다.
        db.execute("""
            UPDATE kapt_metadata_pending
            SET status = 'superseded', next_retry_at = NULL
            WHERE district = %s AND batch < %s AND status = 'pending'
        """, (district, batch))
        db.execute("""
            DELETE FROM kapt_metadata_cache
            WHERE batch LIKE %s AND batch < %s
        """, (f"kapt-%-{district}", batch))

        if concurrent_deferred:
            db.execute("UPDATE complex_metadata_runs SET checksum=%s WHERE batch=%s",
                       ("automatic:kapt:partial", batch))
        elif not total_deferred:
            db.execute(
                "DELETE FROM kapt_metadata_cache WHERE batch = %s", (batch,)
            )

    if retryable_deferred or concurrent_deferred:
        status = "partial"
    elif empty_detail_deferred:
        status = "empty_detail_only"
    else:
        status = "completed"

    print(
        f"K-apt {district} {month}: status={status} "
        f"collected={len(source)}/{len(codes)} applied={applied} "
        f"retryable={len(retryable_deferred) + concurrent_deferred} "
        f"empty_detail={len(empty_detail_deferred)} held_names={held_names}",
        flush=True,
    )
    return status


def refresh_kapt(today, key, districts):
    month = today.strftime("%Y-%m")
    failed = []

    for district in districts:
        try:
            result = refresh_kapt_district(month, key, district)

            if result == "partial":
                failed.append(district)
        except KaptAuthenticationError as error:
            print(
                f"K-apt AUTH BLOCKED: {error}. "
                f"Stopping all remaining districts.",
                flush=True
            )
            raise

        except (OSError, ValueError, KeyError, TypeError, psycopg.Error) as error:
            failed.append(district)

            safe_reasons = {
                "K-apt listing changed during pagination",
                "Incomplete K-apt listing",
                "Empty K-apt district listing",
                "Complex changed during K-apt update",
                "K-apt detail code mismatch",
                "K-apt API returned an error",
                "K-apt portal error",
            }

            reason = (
                str(error)
                if isinstance(error, ValueError) and str(error) in safe_reasons
                else type(error).__name__
            )

            print(
                f"K-apt {district} {month}: deferred ({reason})",
                flush=True
            )

    return failed


def refresh_lifecycle_district(today, district, provider=empty_lifecycle_provider):
    district_name = DISTRICTS[district]
    month = today.strftime("%Y-%m")
    if provider is empty_lifecycle_provider:
        return {"applied": 0, "noop": 0, "pending": 0, "quarantined": 0}
    with psycopg.connect(connect_timeout=8) as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (KAPT_LOCK_BASE + int(district),))
        completed = db.execute("""
            SELECT 1 FROM monthly_refresh_stages
            WHERE month=%s AND district=%s AND stage='lifecycle' AND status='completed'
        """, (month, district)).fetchone()
        if completed:
            return {"applied": 0, "noop": 0, "pending": 0, "quarantined": 0}
        complex_ids = [
            row[0] for row in db.execute("""
                SELECT complex_id
                FROM complexes
                WHERE split_part(complex_id, ' ', 2) = %s
                ORDER BY complex_id
            """, (district_name,)).fetchall()
        ]
        results = provider(district, today, complex_ids) or ()
        summary = apply_lifecycle_results(db, results, allowed_ids=set(complex_ids), cutoff=today)
        db.execute("""
            INSERT INTO monthly_refresh_stages(month, district, stage, status, slot_start)
            VALUES (%s, %s, 'lifecycle', 'completed', %s)
            ON CONFLICT (month, district, stage) DO UPDATE
            SET status='completed', updated_at=clock_timestamp(), last_error=NULL, next_retry_at=NULL
        """, (month, district, datetime.combine(today.replace(day=1), dt_time(6), KST)))
        return summary


def refresh_lifecycle(today, provider, districts):
    failed = []
    for district in districts:
        try:
            refresh_lifecycle_district(today, district, provider)
        except (OSError, ValueError, KeyError, TypeError, psycopg.Error) as error:
            failed.append(district)
            print(
                f"Lifecycle {district} {today.strftime('%Y-%m')}: deferred ({type(error).__name__})",
                flush=True,
            )
    return failed


def refresh_automatic_metadata(
    today, districts, *, include_geocode=True, include_kapt=True, lifecycle_provider=None,
):
    naver_id = os.environ.get("NAVER_MAPS_API_KEY_ID")
    naver_key = os.environ.get("NAVER_MAPS_API_KEY")
    kapt_key = os.environ.get("KAPT_SERVICE_KEY")

    jobs = []
    failed = []

    if include_geocode and naver_id and naver_key:
        jobs.append(("Naver Geocoding", geocode, {
            "x-ncp-apigw-api-key-id": naver_id,
            "x-ncp-apigw-api-key": naver_key,
            "Accept": "application/json",
        }))
    elif include_geocode and (naver_id or naver_key):
        print(
            "Naver Geocoding requires both Maps environment variables.",
            flush=True
        )
    elif include_geocode:
        print("Naver Geocoding skipped (credentials not configured).", flush=True)

    if include_kapt and kapt_key:
        jobs.append(("K-apt", refresh_kapt, kapt_key))
    elif include_kapt:
        print("K-apt refresh skipped (KAPT_SERVICE_KEY not configured).", flush=True)

    if lifecycle_provider is not None:
        jobs.append(("Lifecycle", refresh_lifecycle, lifecycle_provider))

    if jobs:
        with psycopg.connect(connect_timeout=8) as db:
            db.execute("SELECT pg_advisory_xact_lock(1121506)")
            db.execute(
                (ROOT / "db/metadata_refresh.sql").read_text(encoding="utf-8")
            )

    for name, update, options in jobs:
        try:
            result = update(today, options, districts)

            if name in ("K-apt", "Lifecycle") and result:
                failed.extend(f"{name}:{district}" for district in result)

        except KaptAuthenticationError as error:
            failed.append(name)
            print(
                f"K-apt authentication failure: {error}. "
                "Check API key and service permissions.",
                flush=True
            )

        except (OSError, ValueError, KeyError, TypeError, psycopg.Error) as error:
            failed.append(name)
            print(
                f"{name} update deferred ({type(error).__name__}).",
                flush=True
            )

    return failed
