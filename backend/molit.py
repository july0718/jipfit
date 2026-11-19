import os
from collections import Counter
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import urlopen
from xml.etree import ElementTree as ET

ENDPOINT = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"


def fetch_month(month):
    key = os.environ.get("MOLIT_SERVICE_KEY")
    if not key:
        raise ValueError("Set MOLIT_SERVICE_KEY to the decoded public data portal key")
    page = 1
    seen = 0
    expected = None
    trades = Counter()
    complexes = {}
    while True:
        query = urlencode(dict(serviceKey=key, LAWD_CD="11215", DEAL_YMD=month,
                               pageNo=page, numOfRows=1000))
        with urlopen(ENDPOINT + "?" + query, timeout=45) as response:
            root = ET.fromstring(response.read())
        code = root.findtext("header/resultCode")
        if code not in {"000", "00", "0000"}:
            raise ValueError(f"MOLIT response error (code {code})")
        total = int(root.findtext("body/totalCount"))
        items = root.findall("body/items/item")
        if expected is None:
            expected = total
        if total != expected or int(root.findtext("body/pageNo")) != page or seen + len(items) > total:
            raise ValueError("Inconsistent MOLIT pages")
        for item in items:
            row = {node.tag: (node.text or "").strip() for node in item}
            if row.get("cdealType", "") not in {"", "-"} or row.get("cdealDay", "") not in {"", "-"}:
                continue
            day = date(int(row["dealYear"]), int(row["dealMonth"]), int(row["dealDay"]))
            dong, lot, name = (row[k] for k in ("umdNm", "jibun", "aptNm"))
            if not all((dong, lot, name)) or day.strftime("%Y%m") != month or row["sggCd"] != "11215":
                raise ValueError("Invalid trade location or contract month")
            address = "서울특별시 광진구 " + dong
            cid = "|".join((address, lot, name))
            area = Decimal(row["excluUseAr"])
            price = Decimal(row["dealAmount"].replace(",", ""))
            built = int(row["buildYear"])
            if area <= 0 or price <= 0 or not 1900 <= built <= day.year:
                raise ValueError("Invalid trade values")
            complexes[cid] = (cid, name, address+" "+lot, built)
            group = int(round(area / 5)) * 5
            floor = int(row["floor"]) if row.get("floor") else None
            trades[(cid, group, day, area, floor, price)] += 1
        seen += len(items)
        if seen == total:
            return complexes, trades
        if not items:
            raise ValueError("Incomplete MOLIT response")
        page += 1

