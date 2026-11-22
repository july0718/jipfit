"""Refresh sourced names monthly and geocode unpositioned complexes daily."""
from collections import defaultdict
import json
import os
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
            WHERE (c.latitude IS NULL OR c.longitude IS NULL)
              AND (a.attempted_on IS NULL OR a.address <> c.address OR a.attempted_on <= %s::date - 7)
              AND split_part(c.complex_id,' ',2) = ANY(%s)
              AND EXISTS (SELECT 1 FROM trades t WHERE t.complex_id=c.complex_id AND NOT t.is_cancelled AND t.price_manwon > 0)
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
            if point:
                updated += db.execute("""
                    UPDATE complexes SET latitude=%s, longitude=%s
                    WHERE complex_id=%s AND address=%s AND (latitude IS NULL OR longitude IS NULL)
                """, (*point, cid, address)).rowcount
            db.execute("""
                INSERT INTO complex_geocode_attempts(complex_id,address,attempted_on) VALUES (%s,%s,%s)
                ON CONFLICT (complex_id) DO UPDATE
                SET address=EXCLUDED.address, attempted_on=EXCLUDED.attempted_on
            """, (cid, address, today))
    print(f"Naver geocoding: considered={len(rows)} updated={updated}", flush=True)


def kapt_body(endpoint, key, **params):
    response = read_json(MOLIT_URL + "/" + endpoint, {"serviceKey": key, **params})["response"]
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


def refresh_kapt(today, key, districts):
    month = today.strftime("%Y-%m")
    for district in districts:
        district_name = DISTRICTS[district]
        batch = f"kapt-{month}-{district}"
        with psycopg.connect(connect_timeout=8) as db:
            if db.execute("SELECT 1 FROM complex_metadata_runs WHERE batch=%s", (batch,)).fetchone():
                continue
            existing = db.execute("""
                SELECT complex_id,name,address,built FROM complexes
                WHERE split_part(complex_id,' ',2)=%s
            """, (district_name,)).fetchall()
        source = []
        for code in kapt_codes(district, key):
            item = kapt_body("AptBasisInfoServiceV5/getAphusBassInfoV5", key, kaptCode=code)["item"]
            if item.get("kaptCode") != code:
                raise ValueError("K-apt detail code mismatch")
            source.append((code, item))
        updates = list(kapt_updates(existing, source))
        with psycopg.connect(connect_timeout=8) as db:
            started = db.execute("""
                INSERT INTO complex_metadata_runs(batch,checksum) VALUES (%s,%s)
                ON CONFLICT DO NOTHING RETURNING batch
            """, (batch, "automatic:kapt")).fetchone()
            if not started:
                continue
            applied = 0
            for cid, address, old_name, code, changes in updates:
                if not update_complex(db, cid, address, changes, expected_name=old_name):
                    continue
                db.execute("INSERT INTO complex_metadata_evidence VALUES (%s,%s,%s,%s)",
                           (batch, cid, "K-apt: " + code, Jsonb(changes)))
                applied += 1
        print(f"K-apt {district} {month}: matched changes={applied}", flush=True)


def refresh_automatic_metadata(today, districts):
    naver_id = os.environ.get("NAVER_MAPS_API_KEY_ID")
    naver_key = os.environ.get("NAVER_MAPS_API_KEY")
    kapt_key = os.environ.get("KAPT_SERVICE_KEY")
    jobs = []
    if naver_id and naver_key:
        jobs.append(("Naver Geocoding", geocode, {
            "x-ncp-apigw-api-key-id": naver_id,
            "x-ncp-apigw-api-key": naver_key,
            "Accept": "application/json",
        }))
    elif naver_id or naver_key:
        print("Naver Geocoding requires both Maps environment variables.", flush=True)
    if kapt_key:
        jobs.append(("K-apt", refresh_kapt, kapt_key))
    if jobs:
        with psycopg.connect(connect_timeout=8) as db:
            db.execute((ROOT / "db/metadata_refresh.sql").read_text(encoding="utf-8"))
    for name, update, options in jobs:
        try:
            update(today, options, districts)
        except (OSError, ValueError, KeyError, TypeError, psycopg.Error) as error:
            # Upstream exceptions may include authenticated request URLs.
            print(f"{name} update deferred ({type(error).__name__}).", flush=True)
