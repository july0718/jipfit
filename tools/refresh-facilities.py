"""Refresh public facility snapshots; credentials belong in the process environment."""
import csv
import datetime
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1] / "frontend/facilities"
TODAY = datetime.date.today().isoformat()

def request(url, fields=None, referer=None):
    headers = {"User-Agent":"Jipfit/1.0"}
    if referer:
        headers["Referer"] = referer
    req = Request(url, data=urlencode(fields).encode() if fields else None, headers=headers)
    with urlopen(req, timeout=40) as response:
        return response.read()

def geo(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (ValueError, TypeError):
        return None
    return (lat, lon) if 37 <= lat <= 38.5 and 126 <= lon <= 128 else None

def count(value):
    value = str(value or "").strip().replace(",", "")
    return int(value) if value.isdecimal() else None

def apartments(old):
    fields = {"srvType":"S","infId":"OA-15818","serviceKind":"0","pageNo":"1","ssUserId":"SAMPLE_VIEW",
              "gridTotalCnt":"","strWhere":"","strOrderby":"SN ASC","filterCol":"필터선택","txtFilter":""}
    raw = request("https://datafile.seoul.go.kr/bigfile/iot/sheet/csv/download.do", fields,
                  "https://data.seoul.go.kr/dataList/OA-15818/S/1/datasetView.do")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        xy = geo(r["좌표Y"], r["좌표X"])
        if xy and r["주소(시군구)"] and r["주소(읍면동)"] and r["k-아파트명"] and "오피스텔" not in r["k-단지분류(아파트,주상복합등등)"]:
            rows.append([r["주소(시군구)"],r["주소(읍면동)"],r["k-아파트명"],*xy,count(r["k-전체세대수"]),count(r["주차대수"])])
    return "서울시 공동주택 아파트 정보 OA-15818", rows

def stations(old):
    key = os.environ["TDATA_SUBWAY_API_KEY"]
    url = "https://t-data.seoul.go.kr/apig/apiman-gateway/tapi/TaimsKsccDvSubwayStationGeom/1.0?"
    rows = {}
    start = 1
    while True:
        data = json.loads(request(url + urlencode({"apikey":key,"startRow":start,"rowCnt":500})))
        if not isinstance(data,list):
            raise ValueError("unexpected station response")
        before = len(rows)
        for r in data:
            xy = geo(r["convY"], r["convX"])
            if xy and r["stnKrNm"] and r["lineNm"]:
                rows.setdefault((r["stnKrNm"],*xy),set()).add(r["lineNm"])
        if len(data)<500:
            break
        if len(rows)==before or start>10000:
            raise ValueError("station pagination stalled")
        start += len(data)
    return "서울 교통빅데이터플랫폼 지하철역 좌표정보", [[name,"·".join(sorted(lines)),y,x] for (name,y,x),lines in rows.items()]

def schools(old):
    key = os.environ["NEIS_API_KEY"]
    # Anchors retain coordinates for school identity checks across refreshes.
    anchors = json.loads((ROOT/"school-anchors.json").read_text(encoding="utf8"))
    def identity(name,level,address):
        return (name.strip(),level,re.sub(r"\s+","",address.split("(")[0]))
    coords = {identity(n,k,a):(y,x) for n,k,a,y,x in anchors}
    url = "https://open.neis.go.kr/hub/schoolInfo?"
    page = 1
    rows = []
    processed = 0
    while True:
        data = json.loads(request(url + urlencode({"KEY":key,"Type":"json","ATPT_OFCDC_SC_CODE":"B10","pIndex":page,"pSize":500})))
        result = data["schoolInfo"]
        total = result[0]["head"][0]["list_total_count"]
        records = result[1]["row"]
        for r in records:
            n,k,a = r["SCHUL_NM"],r["SCHUL_KND_SC_NM"],r["ORG_RDNMA"]
            xy = coords.get(identity(n,k,a or ""))
            if xy:
                rows.append([n,k,a,*xy])
        processed += len(records)
        if processed == total:
            break
        if not records or processed>total:
            raise ValueError("incomplete school catalog")
        page += 1
    return "나이스 서울 학교 명부 · 기존 도로명주소 일치 좌표", rows

def main():
    failures = []
    for name,loader,key in [("apartments",apartments,None),("stations",stations,"TDATA_SUBWAY_API_KEY"),("schools",schools,"NEIS_API_KEY")]:
        if key and not os.getenv(key):
            print(name, "using bundled snapshot")
            continue
        target = ROOT/(name+".json")
        old = json.loads(target.read_text(encoding="utf8"))
        try:
            source, rows = loader(old)
            if len(rows)<0.70*len(old["rows"]):
                raise ValueError("incomplete catalog")
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps({"source":source+" · 조회 "+TODAY,"rows":rows},ensure_ascii=False,separators=(",",":")),encoding="utf8")
            temp.replace(target)
            print(name, len(rows))
        except Exception as err:
            # URL exceptions can contain credentials. Report the failure type only.
            print(name, "previous snapshot retained", type(err).__name__)
            failures.append(name)
    if failures:
        raise SystemExit(1)

if __name__=="__main__":
    main()
