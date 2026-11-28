"""Public facility catalogs and the facility summary of every complex.

  python facilities.py refresh [--dry-run]    # fetch the public catalogs, then recompute
  python facilities.py sync                   # recompute complex_facilities from the stored catalogs

The catalogs start from the snapshot in facilities_seed/ and are replaced source by source from the public
data. Request URLs carry the API keys, so only exception types are printed.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "facilities_seed"
KST = timezone(timedelta(hours=9))
LOCK = 1121507  # One replacement of the catalogs or of complex_facilities at a time.
SOURCES = ("apartments", "stations", "schools")
CATALOGS = {
    "apartments": ("facility_apartments", ("gu", "dong", "name", "latitude", "longitude", "households", "parking")),
    "stations": ("facility_stations", ("seq", "name", "lines", "latitude", "longitude")),
    "schools": ("facility_schools", ("seq", "name", "level", "address", "latitude", "longitude")),
}
ANCHORS = ("facility_school_anchors", ("name", "level", "address", "latitude", "longitude"))
KEYS = {"stations": "TDATA_SUBWAY_API_KEY", "schools": "NEIS_API_KEY"}  # Apartments need no key.
LEVELS = ("초등학교", "중학교", "고등학교")
EARTH = 6371008.8
RADIANS = math.pi / 180


def distance(lat, lon, y, x):
    """Straight-line (haversine) distance in metres."""
    a = (math.sin((y - lat) * RADIANS / 2) ** 2
         + math.cos(lat * RADIANS) * math.cos(y * RADIANS) * math.sin((x - lon) * RADIANS / 2) ** 2)
    return 2 * EARTH * math.asin(math.sqrt(min(1, a)))


def whole_metres(value):
    """Round halves up like JavaScript Math.round; Python's round() would round them to even."""
    return math.floor(value + 0.5)


def valid(lat, lon):
    return lat is not None and lon is not None and 37 <= lat <= 38.5 and 126 <= lon <= 128


def simple(name, dong):
    """Apartment name reduced for matching: no separators, no trailing '아파트', no leading dong name."""
    text = re.sub(r"[\s﻿·._-]", "", unicodedata.normalize("NFKC", name).lower()).removesuffix("아파트")
    prefix = dong.removesuffix("동")
    if len(prefix) >= 2 and text.startswith(prefix):
        text = text[len(prefix):]
    return text


def nearest(lat, lon, records):
    """[name, kind, metres, latitude, longitude] of the closest record; the earlier one wins a tie."""
    best, metres = None, math.inf
    for name, kind, y, x in records:
        found = distance(lat, lon, y, x)
        if found < metres:
            best, metres = (name, kind, y, x), found
    return [best[0], best[1], whole_metres(metres), best[2], best[3]] if best else None


def compute_facilities(complexes, catalog):
    """Facility summary of each complex, keyed by complex_id.

    complexes: dicts with complex_id, name, address, latitude and longitude. catalog: rows in source order,
    apartments [gu, dong, name, latitude, longitude, households, parking], stations [name, lines, latitude,
    longitude] and schools [name, level, address, latitude, longitude].
    """
    apartments = catalog["apartments"]
    by_name = defaultdict(list)
    for index, (gu, dong, name, *_) in enumerate(apartments):
        by_name[gu, dong, simple(name, dong)].append(index)
    stations = catalog["stations"]
    schools = [[(name, kind, y, x) for name, kind, _, y, x in catalog["schools"] if kind == level]
               for level in LEVELS]

    candidates, uses = {}, Counter()
    for home in complexes:
        words = re.split(r"\s+", home["address"])
        lat, lon = home["latitude"], home["longitude"]
        if len(words) < 3 or not words[2] or not valid(lat, lon):
            continue
        gu, dong = words[1], words[2]
        hits = [index for index in by_name.get((gu, dong, simple(home["name"], dong)), ())
                if distance(lat, lon, apartments[index][3], apartments[index][4]) <= 350]
        # An apartment is linked only when a single complex of the district (from complex_id) points at it.
        district = tuple(home["complex_id"].split(" ")[1:2])
        uses.update((district, index) for index in hits)
        if len(hits) == 1:
            candidates[home["complex_id"]] = (district, hits[0])

    nearby, result = {}, {}
    for home in complexes:
        key = candidates.get(home["complex_id"])
        households = parking = None
        if key and uses[key] == 1:
            count, spaces = apartments[key[1]][5:7]
            if count is not None and count > 0:
                households = count
                if isinstance(spaces, int) and spaces >= 0:
                    parking = spaces / count
        station = found = None
        lat, lon = home["latitude"], home["longitude"]
        if valid(lat, lon):
            if (lat, lon) not in nearby:
                nearby[lat, lon] = (nearest(lat, lon, stations),
                                    [row for row in (nearest(lat, lon, records) for records in schools) if row])
            station, found = nearby[lat, lon]
        result[home["complex_id"]] = {"households": households, "parking": parking,
                                      "station": station, "schools": found}
    return result


def _request(url, fields=None, referer=None):
    headers = {"User-Agent": "Jipfit/1.0"}
    if referer:
        headers["Referer"] = referer
    request = Request(url, data=urlencode(fields).encode() if fields else None, headers=headers)
    with urlopen(request, timeout=40) as response:
        return response.read()


def _coordinates(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (ValueError, TypeError):
        return None
    return (lat, lon) if valid(lat, lon) else None


def _count(value):
    value = str(value or "").strip().replace(",", "")
    return int(value) if value.isdecimal() else None


def fetch_apartments():
    """(source label, rows) of the Seoul apartment complex file, officetels excluded."""
    fields = {"srvType": "S", "infId": "OA-15818", "serviceKind": "0", "pageNo": "1", "ssUserId": "SAMPLE_VIEW",
              "gridTotalCnt": "", "strWhere": "", "strOrderby": "SN ASC", "filterCol": "필터선택", "txtFilter": ""}
    raw = _request("https://datafile.seoul.go.kr/bigfile/iot/sheet/csv/download.do", fields,
                   "https://data.seoul.go.kr/dataList/OA-15818/S/1/datasetView.do")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        xy = _coordinates(r["좌표Y"], r["좌표X"])
        if (xy and r["주소(시군구)"] and r["주소(읍면동)"] and r["k-아파트명"]
                and "오피스텔" not in r["k-단지분류(아파트,주상복합등등)"]):
            rows.append([r["주소(시군구)"], r["주소(읍면동)"], r["k-아파트명"], *xy,
                         _count(r["k-전체세대수"]), _count(r["주차대수"])])
    return "서울시 공동주택 아파트 정보 OA-15818", rows


def fetch_stations(key):
    """(source label, rows) of the subway station coordinates, lines merged per station."""
    url = "https://t-data.seoul.go.kr/apig/apiman-gateway/tapi/TaimsKsccDvSubwayStationGeom/1.0?"
    rows = {}
    start = 1
    while True:
        data = json.loads(_request(url + urlencode({"apikey": key, "startRow": start, "rowCnt": 500})))
        if not isinstance(data, list):
            raise ValueError("unexpected station response")
        before = len(rows)
        for r in data:
            xy = _coordinates(r["convY"], r["convX"])
            if xy and r["stnKrNm"] and r["lineNm"]:
                rows.setdefault((r["stnKrNm"], *xy), set()).add(r["lineNm"])
        if len(data) < 500:
            break
        if len(rows) == before or start > 10000:
            raise ValueError("station pagination stalled")
        start += len(data)
    return ("서울 교통빅데이터플랫폼 지하철역 좌표정보",
            [[name, "·".join(sorted(lines)), y, x] for (name, y, x), lines in rows.items()])


def fetch_schools(key, anchors):
    """(source label, rows) of the NEIS school list; anchors [name, level, address, lat, lon] give coordinates."""
    def identity(name, level, address):
        return (name.strip(), level, re.sub(r"\s+", "", address.split("(")[0]))

    coordinates = {identity(n, k, a): (y, x) for n, k, a, y, x in anchors}
    url = "https://open.neis.go.kr/hub/schoolInfo?"
    page = 1
    rows = []
    processed = 0
    while True:
        data = json.loads(_request(url + urlencode(
            {"KEY": key, "Type": "json", "ATPT_OFCDC_SC_CODE": "B10", "pIndex": page, "pSize": 500})))
        result = data["schoolInfo"]
        total = result[0]["head"][0]["list_total_count"]
        records = result[1]["row"]
        for r in records:
            n, k, a = r["SCHUL_NM"], r["SCHUL_KND_SC_NM"], r["ORG_RDNMA"]
            xy = coordinates.get(identity(n, k, a or ""))
            if xy:
                rows.append([n, k, a, *xy])
        processed += len(records)
        if processed == total:
            break
        if not records or processed > total:
            raise ValueError("incomplete school catalog")
        page += 1
    return "나이스 서울 학교 명부 · 기존 도로명주소 일치 좌표", rows


def _lock(db):
    db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))


def _row_count(db, table):
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _read(db, table, columns, order=""):
    return [list(row) for row in db.execute(f"SELECT {', '.join(columns)} FROM {table}{order}").fetchall()]


def _insert(db, table, columns, rows):
    marks = ", ".join(["%s"] * len(columns))
    with db.cursor() as cur:
        cur.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", rows)


def _replace_catalog(db, source, rows, label, month):
    """Swap one source's rows (numbered in their given order) and record where they came from."""
    table, columns = CATALOGS[source]
    db.execute(f"DELETE FROM {table}")
    _insert(db, table, columns,
            [(seq, *row) for seq, row in enumerate(rows)] if columns[0] == "seq" else [tuple(row) for row in rows])
    db.execute("""
        INSERT INTO facility_sources(source, label, row_count, refreshed_month) VALUES (%s, %s, %s, %s)
        ON CONFLICT (source) DO UPDATE SET label = EXCLUDED.label, row_count = EXCLUDED.row_count,
            refreshed_month = EXCLUDED.refreshed_month, updated_at = clock_timestamp()
    """, (source, label, len(rows), month))


def _seed(name):
    return json.loads((SEED / f"{name}.json").read_text(encoding="utf-8"))


def ensure_facilities(db, *, compute=True):
    """Load the bundled snapshot into empty catalog tables, then compute complex_facilities if it is empty.

    Called when the updater starts, before the daily sync, and by the commands below. The snapshot is
    committed before the computation, so a failed computation is repeated by the daily sync. compute=False
    leaves the computation to a caller that runs the sync anyway.
    """
    with db.transaction():
        _lock(db)
        for source in SOURCES:
            if not _row_count(db, CATALOGS[source][0]):
                document = _seed(source)
                _replace_catalog(db, source, document["rows"], document["source"], None)
                print(f"Facilities seed {source}: {len(document['rows']):,}", flush=True)
        table, columns = ANCHORS
        if not _row_count(db, table):
            _insert(db, table, columns, [tuple(row) for row in _seed("school-anchors")])
        computed = _row_count(db, "complex_facilities")
    if compute and not computed:
        sync_complex_facilities(db)


def load_catalog(db):
    """The stored catalogs in the shape compute_facilities takes; stations and schools in source order."""
    return {source: _read(db, table, [c for c in columns if c != "seq"],
                          " ORDER BY seq" if "seq" in columns else "")
            for source, (table, columns) in CATALOGS.items()}


def sync_complex_facilities(db):
    """Replace complex_facilities from the stored catalogs in one transaction; return the number of complexes."""
    with db.transaction():
        _lock(db)
        catalog = load_catalog(db)
        if not all(catalog.values()):
            raise ValueError("facility catalog is empty")
        fields = ("complex_id", "name", "address", "latitude", "longitude")
        complexes = [dict(zip(fields, row)) for row in _read(db, "complexes", fields, " ORDER BY complex_id")]
        computed = compute_facilities(complexes, catalog)

        def encoded(value):
            return None if value is None else json.dumps(value, ensure_ascii=False)

        db.execute("DELETE FROM complex_facilities")
        with db.cursor() as cur:
            cur.executemany("""
                INSERT INTO complex_facilities(complex_id, households, parking, station, schools)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
            """, [(complex_id, item["households"], item["parking"], encoded(item["station"]),
                   encoded(item["schools"])) for complex_id, item in computed.items()])
    print(f"Facilities sync: {len(computed):,} complexes", flush=True)
    return len(computed)


def refresh_sources(today, *, dry_run=False):
    """Replace each catalog from its public source and return the names that failed.

    A source already refreshed this month and a source without its API key are left alone. A result under
    70% of the stored rows is rejected. If anything changed, complex_facilities is recomputed ("sync" in the
    returned list means that step failed). dry_run only fetches and counts.
    """
    month = f"{today:%Y-%m}"
    failed, report, changed = [], [], False
    for source in SOURCES:
        try:
            with psycopg.connect(connect_timeout=8) as db:
                if db.execute("SELECT 1 FROM facility_sources WHERE source = %s AND refreshed_month = %s",
                              (source, month)).fetchone():
                    report.append(f"{source} already refreshed")
                    continue
                anchors = _read(db, *ANCHORS) if source == "schools" else None
            variable = KEYS.get(source)
            key = os.environ.get(variable) if variable else None
            if variable and not key:
                print(f"Facilities {source}: skipped ({variable} not configured)", flush=True)
                report.append(f"{source} skipped")
                continue
            # The download runs outside any transaction.
            if source == "apartments":
                label, rows = fetch_apartments()
            elif source == "stations":
                label, rows = fetch_stations(key)
            else:
                label, rows = fetch_schools(key, anchors)
            with psycopg.connect(connect_timeout=8) as db:
                if not dry_run:
                    _lock(db)
                current = _row_count(db, CATALOGS[source][0])
                if not rows or len(rows) < 0.70 * current:
                    print(f"Facilities {source}: received {len(rows):,} rows, stored {current:,}", flush=True)
                    raise ValueError("incomplete catalog")
                if dry_run:
                    report.append(f"{source} {len(rows):,} (stored {current:,})")
                    continue
                _replace_catalog(db, source, rows, f"{label} · 조회 {today.isoformat()}", month)
            changed = True
            report.append(f"{source} {len(rows):,}")
        except Exception as exc:
            print(f"Facilities {source}: previous data retained ({type(exc).__name__})", flush=True)
            report.append(f"{source} failed")
            failed.append(source)
    print(f"Facilities {month}{' dry-run' if dry_run else ''}: {' / '.join(report)}", flush=True)
    if changed:
        try:
            with psycopg.connect(connect_timeout=8) as db:
                sync_complex_facilities(db)
        except Exception as exc:
            print(f"Facilities sync failed ({type(exc).__name__})", flush=True)
            failed.append("sync")
    return failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh_command = commands.add_parser("refresh", help="fetch the public catalogs and recompute")
    refresh_command.add_argument("--dry-run", action="store_true", help="fetch and count only; nothing is written")
    commands.add_parser("sync", help="recompute complex_facilities from the stored catalogs")
    args = parser.parse_args(argv)
    dry = args.command == "refresh" and args.dry_run
    try:
        if not dry:
            from refresh import ensure_schema  # refresh imports this module.

            ensure_schema()
            with psycopg.connect(connect_timeout=8) as db:
                ensure_facilities(db)
        if args.command == "sync":
            with psycopg.connect(connect_timeout=8) as db:
                sync_complex_facilities(db)
            return 0
        return 1 if refresh_sources(datetime.now(KST).date(), dry_run=dry) else 0
    except Exception as exc:
        print(f"Facilities {args.command} failed ({type(exc).__name__}).", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
