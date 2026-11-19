"""Download public BOK/REB releases and extract monthly policy rate + Gwangjin index.

No API keys, sample endpoint pagination, or private endpoints are used.
Run with bundled Python. Requires openpyxl for read-only XLSX extraction.
Existing official snapshots are reused unless --refresh is requested.
"""
from pathlib import Path
from datetime import date
import argparse
import calendar
import csv
import hashlib
import html
import io
import json
import re
import urllib.parse
import urllib.request
import warnings
import zipfile

import openpyxl

ROOT = Path.cwd()
BOK_URL = 'https://www.bok.or.kr/portal/singl/baseRate/list.do?menuNo=200643'
REB_BOARD_URL = 'https://www.reb.or.kr/r-one/portal/bbs/statdata/searchBulletin.do'
REB_PAGE_URL = 'https://www.reb.or.kr/r-one/portal/bbs/statdata/selectBulletinPage.do'
REB_META_URL = 'https://www.reb.or.kr/r-one/portal/bbs/statdata/selectBulletin.do'
REB_DOWNLOAD_URL = 'https://www.reb.or.kr/r-one/portal/bbs/statdata/downloadAttachFile.do'
RETRIEVED_ON = date.today().isoformat()


def download(url, filename, refresh=False):
    target = ROOT / filename
    if refresh or not target.exists():
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(request, timeout=90) as response:
            target.write_bytes(response.read())
    return target.read_bytes()


def month_range(start, end):
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month == 13:
            month = 1
            year += 1


def next_month(year, month, day=1):
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return date(year, month, day)


def extract_rates(content):
    events = {}
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', content, flags=re.S | re.I):
        cells = [html.unescape(re.sub('<[^>]+>', '', v)).strip()
                 for v in re.findall(r'<td\b[^>]*>(.*?)</td>', row, flags=re.S | re.I)]
        if len(cells) != 3 or not re.fullmatch(r'\d{4}', cells[0]):
            continue
        match = re.fullmatch(r'(\d{1,2})월\s*(\d{1,2})일', cells[1])
        if not match:
            continue
        effective = date(int(cells[0]), int(match[1]), int(match[2]))
        events[effective] = float(cells[2])
    if len(events) < 40:
        raise ValueError('Official BOK rate-history table was not found or changed format.')
    return sorted(events.items())


def extract_gwangjin_index(workbook_bytes):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        workbook = openpyxl.load_workbook(io.BytesIO(workbook_bytes), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    # Public REB XLSX incorrectly declares A1 as its complete dimension.
    sheet.reset_dimensions()
    rows = list(sheet.iter_rows(values_only=True))
    header = next(row for row in rows if any(re.fullmatch(r'\d{4}년\s*\d{1,2}월', str(c or '')) for c in row))
    candidates = [row for row in rows if len(row) > 4 and tuple(row[:4]) == ('서울', '강북지역', '동북권', '광진구')]
    if len(candidates) != 1:
        raise ValueError(f'Expected one Gwangjin raw apartment-index row, found {len(candidates)}.')
    target = candidates[0]
    values = {}
    for position, label in enumerate(header):
        match = re.fullmatch(r'(\d{4})년\s*(\d{1,2})월', str(label or ''))
        if not match:
            continue
        value = target[position] if position < len(target) else None
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f'Missing/nonpositive official Gwangjin index for {label}.')
        values[(int(match[1]), int(match[2]))] = float(value)
    if len(values) < 120:
        raise ValueError('Insufficient Gwangjin index history.')
    return values, sheet.title


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--end-month', default='2026-10')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    bok_html = download(BOK_URL, 'bok_base_rate.html', args.refresh).decode('utf-8')
    events = extract_rates(bok_html)

    board_query = urllib.parse.urlencode({'page': 1, 'rows': 20, 'bbsCd': 'STATDATA', 'grpCd': 'B1006', 'searchType': '', 'searchWord': ''})
    board = json.loads(download(REB_BOARD_URL + '?' + board_query, 'rone_board.json', args.refresh))
    monthly = [row for row in board['data'] if row.get('listSubCd') == 'STATDATA03']
    if len(monthly) != 1:
        raise ValueError('Could not identify the official monthly house-price download post.')
    post = monthly[0]
    query = urllib.parse.urlencode({'bbsCd': post['bbsCd'], 'seq': post['seq'], 'noticeYn': post['noticeYn']})
    metadata = json.loads(download(REB_META_URL + '?' + query, 'rone_monthly_metadata.json', args.refresh))['data']
    files = [f for f in metadata['files'] if f['fileExt'].lower() == 'zip']
    if len(files) != 1:
        raise ValueError('Expected one public REB monthly ZIP attachment.')
    file = files[0]
    download_query = urllib.parse.urlencode({'bbsCd': file['bbsCd'], 'seq': file['seq'], 'fileSeq': file['fileSeq']})
    zip_url = REB_DOWNLOAD_URL + '?' + download_query
    zip_bytes = download(zip_url, 'reb_monthly_202609.zip', args.refresh)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        paths = [n for n in archive.namelist() if n.endswith('/(월) 매매가격지수_아파트.xlsx')]
        if len(paths) != 1:
            raise ValueError('Could not identify the raw monthly apartment sales-price index workbook.')
        member = paths[0]
        workbook_bytes = archive.read(member)
    (ROOT / 'reb_apartment_index.xlsx').write_bytes(workbook_bytes)
    indexes, sheet_name = extract_gwangjin_index(workbook_bytes)

    with (ROOT / 'policy_rate_events.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['effective_date', 'policy_rate', 'source_url'])
        writer.writerows((d.isoformat(), rate, BOK_URL) for d, rate in events)

    end = tuple(map(int, args.end_month.split('-')))
    first_index = min(indexes)
    last_index = max(indexes)
    rows = []
    for year, month in month_range(first_index, end):
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        known_events = [(d, rate) for d, rate in events if d <= month_end]
        if not known_events:
            raise ValueError(f'No policy rate available at {month_end}.')
        effective, rate = known_events[-1]
        index = indexes.get((year, month))
        index_available = next_month(year, month, 15).isoformat() if index is not None else ''
        # The official latest-release posting date is directly observed.
        if (year, month) == last_index:
            index_available = metadata['userDttm']
        rows.append({
            'reference_month': f'{year:04}-{month:02}',
            'policy_rate': rate,
            'gwangjin_apt_index': index if index is not None else '',
            'policy_rate_available_at': next_month(year, month).isoformat(),
            'gwangjin_apt_index_available_at': index_available,
            'policy_rate_last_effective_at': effective.isoformat(),
        })
    with (ROOT / 'macro_raw.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    manifest = {
        'retrieved_on': RETRIEVED_ON,
        'scope': 'Only policy rate and Gwangjin apartment sales-price index; no mortgage-rate, school, station or supply features.',
        'policy_rate': {
            'source_url': BOK_URL,
            'method': 'Official public rate-change table; month-end last effective rate, not a monthly average.',
            'events': len(events),
            'event_start': events[0][0].isoformat(),
            'event_end': events[-1][0].isoformat(),
            'availability_rule': 'Month-end rate is conservatively usable from the first day of the following month.',
            'historical_regime_note': 'Until February 2008 the official history refers to the overnight call-rate target; from March 2008 the BOK base rate.',
        },
        'gwangjin_apt_index': {
            'source_page_url': REB_PAGE_URL + '?' + query,
            'download_url': zip_url,
            'release_title': metadata['bbsTit'],
            'latest_posted_at': metadata['userDttm'],
            'archive_member': member,
            'sheet': sheet_name,
            'region_row': ['서울', '강북지역', '동북권', '광진구'],
            'series_type': 'Monthly apartment sale price survey index, raw/unadjusted series; not transaction-price index.',
            'start_month': '%04d-%02d' % first_index,
            'end_month': '%04d-%02d' % last_index,
            'observations': len(indexes),
            'availability_rule': 'Historical months: assumed next-month 15th from official monthly release schedule. Latest available snapshot: observed official post date.',
            'publication_date_limitation': 'Historical actual release dates were not individually retrieved; downstream model joins as of the first day of each anchor month; release dates may differ from the assumed next-month day 15.',
            'vintage_limitation': 'Latest official historical series downloaded in 2026, including any historical revisions/rebasing. Not a point-in-time vintage archive.',
        },
        'macro_rows': len(rows),
        'output_end_month': args.end_month,
        'original_hashes_sha256': {n: hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ['bok_base_rate.html', 'rone_board.json', 'rone_monthly_metadata.json', 'reb_monthly_202609.zip', 'reb_apartment_index.xlsx']},
    }
    (ROOT / 'source_metadata.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'rows': len(rows), 'index_observations': len(indexes), 'index_start': manifest['gwangjin_apt_index']['start_month'], 'index_end': manifest['gwangjin_apt_index']['end_month'], 'latest_rows': rows[-3:]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

