"""Refresh sourced names monthly and geocode unpositioned complexes daily."""

class KaptAuthenticationError(Exception):
    pass

from urllib.error import HTTPError, URLError
from collections import defaultdict
import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg
from psycopg.types.json import Jsonb

from districts import DISTRICTS
from refresh_metadata import ROOT, update_complex

NAVER_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
MOLIT_URL = "https://apis.data.go.kr/1613000"


def read_json(url, params, headers=None):
    request = Request(url + "?" + urlencode(params), headers=headers or {})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def address_key(value):
    return " ".join(str(value or "").split()).removeprefix("서울시 ").removeprefix("서울특별시 ")


def exact_point(address, candidates):
    wanted = address_key(address)
    points = set()
    for candidate in candidates:
        names = {address_key(candidate.get(field)) for field in ("jibunAddress", "roadAddress")}
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
    with psycopg.connect(connect_timeout=8) as db:
        rows = db.execute("""
            SELECT c.complex_id, c.address FROM complexes c
            LEFT JOIN complex_geocode_attempts a USING (complex_id)
            LEFT JOIN complex_geocode_pending p USING (complex_id)
            WHERE (c.latitude IS NULL OR c.longitude IS NULL OR p.complex_id IS NOT NULL)
              AND (a.attempted_on IS NULL OR a.address <> c.address OR a.attempted_on <= %s::date - 7)
              AND split_part(c.complex_id,' ',2) = ANY(%s)
              AND (p.complex_id IS NOT NULL OR EXISTS (SELECT 1 FROM trades t WHERE t.complex_id=c.complex_id AND NOT t.is_cancelled AND t.price_manwon > 0))
            ORDER BY a.attempted_on NULLS FIRST, c.complex_id
            LIMIT 50
        """, (today, [DISTRICTS[code] for code in districts])).fetchall()
    updated = 0
    cache = {}
    for cid, address in rows:
        if address not in cache:
            response = read_json(NAVER_URL, {"query": address}, headers)
            if response.get("status") != "OK" or not isinstance(response.get("addresses"), list):
                raise ValueError("Unexpected Geocoding response")
            cache[address] = exact_point(address, response["addresses"])
        point = cache[address]
        with psycopg.connect(connect_timeout=8) as db:
            # Serialize with verified metadata edits and discard stale results.
            if not db.execute("""
                SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s FOR UPDATE
            """, (cid, address)).fetchone():
                continue
            if point:
                updated += db.execute("""
                    UPDATE complexes SET latitude=%s, longitude=%s
                    WHERE complex_id=%s AND address=%s AND (latitude IS NULL OR longitude IS NULL
                        OR EXISTS (SELECT 1 FROM complex_geocode_pending WHERE complex_id=%s))
                """, (*point, cid, address, cid)).rowcount
                db.execute("DELETE FROM complex_geocode_pending WHERE complex_id=%s", (cid,))
            db.execute("""
                INSERT INTO complex_geocode_attempts(complex_id,address,attempted_on) VALUES (%s,%s,%s)
                ON CONFLICT (complex_id) DO UPDATE
                SET address=EXCLUDED.address, attempted_on=EXCLUDED.attempted_on
            """, (cid, address, today))
    print(f"Naver geocoding: considered={len(rows)} updated={updated}", flush=True)


def kapt_body(endpoint, key, **params):
    for attempt in range(3):
        time.sleep(1.0)
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
        if "OpenAPI_ServiceResponse" not in payload:
            break
        header = payload["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
        code = str(header.get("returnReasonCode", ""))
        if code == "04" and attempt < 2:
            time.sleep(2 ** (attempt + 1))
            continue
        # Only log a short numeric reason code; never log the credentialed URL.
        reason = code if code.isascii() and code.isdigit() and len(code) <= 3 else "unknown"
        print(f"K-apt {endpoint}: portal error {reason} after {attempt + 1} request(s)", flush=True)
        raise ValueError("K-apt portal error")
    response = payload["response"]
    if str(response["header"]["resultCode"]) not in {"0", "00", "000", "0000"}:
        raise ValueError("K-apt API returned an error")
    return response["body"]


def kapt_codes(district, key):
    codes = set()
    page = 1
    expected = None
    while True:
        body = kapt_body("AptListService4/getSigunguAptList4", key,
                         sigunguCode=district, pageNo=page, numOfRows=100)
        total = int(body["totalCount"])
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
        if len(codes) == total:
            return sorted(codes)
        if not items or len(codes) > total:
            raise ValueError("Incomplete K-apt listing")
        page += 1


def kapt_updates(existing, source):
    """Require a unique property at the same parcel and original build year."""
    current = defaultdict(list)
    candidates = defaultdict(list)
    for cid, name, address, built in existing:
        current[address_key(address), str(built)].append((cid, name, address))
    for code, record in source:
        date = str(record.get("kaptUsedate") or "").replace("-", "")
        address = address_key(record.get("kaptAddr"))
        name = " ".join(str(record.get("kaptName") or "").split())
        if address and name and len(date) == 8 and date.isdigit():
            candidates[address, date[:4]].append((code, name))
    for key, items in candidates.items():
        matches = current.get(key, [])
        if len(items) == len(matches) == 1:
            code, name = items[0]
            cid, old_name, address = matches[0]
            if name != old_name:
                yield cid, address, old_name, code, {"name": name}

def defer_kapt_detail(db, batch, district, code, reason):
    db.execute(
        """
        INSERT INTO kapt_metadata_pending
            (batch, district, kapt_code,
             reason, status, attempts,
             last_attempt_at, next_retry_at)
        VALUES
            (%s, %s, %s, %s, 'pending', 1,
             NOW(), NOW() + INTERVAL '30 minutes')
        ON CONFLICT (batch, kapt_code)
        DO UPDATE SET
            reason = EXCLUDED.reason,
            status = 'pending',
            attempts = kapt_metadata_pending.attempts + 1,
            last_attempt_at = NOW(),
            next_retry_at = NOW() + INTERVAL '30 minutes'
        """,
        (batch, district, code, reason)
    )


def refresh_kapt_district(month, key, district):
    district_name = DISTRICTS[district]
    batch = f"kapt-{month}-{district}"
    with psycopg.connect(connect_timeout=8) as db:
        if db.execute("SELECT 1 FROM complex_metadata_runs WHERE batch=%s", (batch,)).fetchone():
            return
        existing = db.execute("""
            SELECT complex_id,name,address,built FROM complexes
            WHERE split_part(complex_id,' ',2)=%s
        """, (district_name,)).fetchall()
    source = []
    codes = kapt_codes(district, key)
    deferred = []

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

        # 상세정보가 비어 있어 보류된 단지
        pending = dict(cache_db.execute(
            """
            SELECT kapt_code,
                   COALESCE(next_retry_at <= NOW(), TRUE)
            FROM kapt_metadata_pending
            WHERE batch = %s AND status = 'pending'
            """,
            (batch,)
        ).fetchall())

        for index, code in enumerate(codes, 1):

            # 재조회 시각 전이라면 해당 단지만 건너뜀
            if code in pending and not pending[code]:
                deferred.append(code)
                continue

            # 정상 캐시가 있으면 API 재호출 생략
            if code in cached and code not in pending:
                item = cached[code]

            else:
                try:
                    item = kapt_body(
                        "AptBasisInfoServiceV5/getAphusBassInfoV5",
                        key,
                        kaptCode=code
                    )["item"]

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

                    deferred.append(code)

                    print(
                        f"K-apt SKIPPED: district={district} "
                        f"progress={index}/{len(codes)} "
                        f"code={code} reason={reason}",
                        flush=True
                    )

                    continue

                # 31개 필드처럼 구조는 있지만 값이 모두 빈 경우
                if isinstance(item, dict) and item and all(
                    value is None or value == ""
                    for value in item.values()
                ):
                    cache_db.execute(
                        """
                        INSERT INTO kapt_metadata_pending
                            (batch, district, kapt_code,
                             reason, status, attempts,
                             last_attempt_at, next_retry_at)
                        VALUES
                            (%s, %s, %s, 'EMPTY_DETAIL',
                             'pending', 1, NOW(),
                             NOW() + INTERVAL '24 hours')
                        ON CONFLICT (batch, kapt_code)
                        DO UPDATE SET
                            reason = 'EMPTY_DETAIL',
                            status = 'pending',
                            attempts = kapt_metadata_pending.attempts + 1,
                            last_attempt_at = NOW(),
                            next_retry_at = NOW() + INTERVAL '24 hours'
                        """,
                        (batch, district, code)
                    )

                    deferred.append(code)

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

                    deferred.append(code)

                    print(
                        f"K-apt SKIPPED: district={district} "
                        f"progress={index}/{len(codes)} "
                        f"code={code} reason=CODE_MISMATCH",
                        flush=True
                    )

                    continue

                # 정상 응답만 캐시에 저장
                cache_db.execute(
                    """
                    INSERT INTO kapt_metadata_cache
                        (batch, kapt_code, payload)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (batch, kapt_code)
                    DO UPDATE SET payload = EXCLUDED.payload
                    """,
                    (batch, code, Jsonb(item))
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

    # 보류 단지가 있으면 정상 수집 결과만 보존
    # 이 구의 단지명 변경은 아직 반영하지 않음
    if deferred:
        print(
            f"K-apt {district} {month}: status=partial "
            f"collected={len(source)}/{len(codes)} "
            f"pending={len(deferred)}",
            flush=True
        )
        return "partial"

    print(
        f"K-apt {district}: collected={len(source)}/{len(codes)}",
        flush=True
    )
    updates = list(kapt_updates(existing, source))
    with psycopg.connect(connect_timeout=8) as db:
        started = db.execute("""
            INSERT INTO complex_metadata_runs(batch,checksum) VALUES (%s,%s)
            ON CONFLICT DO NOTHING RETURNING batch
        """, (batch, "automatic:kapt")).fetchone()
        if not started:
            return
        applied = 0
        for cid, address, old_name, code, changes in updates:
            if not update_complex(db, cid, address, changes, expected_name=old_name):
                continue
            db.execute("INSERT INTO complex_metadata_evidence VALUES (%s,%s,%s,%s)",
                       (batch, cid, "K-apt: " + code, Jsonb(changes)))
            applied += 1

        # 최종 반영 성공 시 임시 수집 데이터 삭제
        db.execute(
            "DELETE FROM kapt_metadata_cache WHERE batch = %s",
            (batch,)
        )

    print(f"K-apt {district} {month}: matched changes={applied}", flush=True)
    

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
            failed.append(district)  # 추가할 코드

            safe_reasons = {
                "K-apt listing changed during pagination",
                "Incomplete K-apt listing",
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


def refresh_automatic_metadata(today, districts):
    naver_id = os.environ.get("NAVER_MAPS_API_KEY_ID")
    naver_key = os.environ.get("NAVER_MAPS_API_KEY")
    kapt_key = os.environ.get("KAPT_SERVICE_KEY")

    jobs = []
    failed = []

    if naver_id and naver_key:
        jobs.append(("Naver Geocoding", geocode, {
            "x-ncp-apigw-api-key-id": naver_id,
            "x-ncp-apigw-api-key": naver_key,
            "Accept": "application/json",
        }))
    elif naver_id or naver_key:
        print(
            "Naver Geocoding requires both Maps environment variables.",
            flush=True
        )

    if kapt_key:
        jobs.append(("K-apt", refresh_kapt, kapt_key))

    if jobs:
        with psycopg.connect(connect_timeout=8) as db:
            db.execute(
                (ROOT / "db/metadata_refresh.sql").read_text(encoding="utf-8")
            )

    for name, update, options in jobs:
        try:
            result = update(today, options, districts)

            if name == "K-apt" and result:
                failed.append(name)

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