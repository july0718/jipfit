import os
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import urlopen
from xml.etree import ElementTree as ET

ENDPOINT = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"


from districts import DISTRICTS, GWANGJIN


class TradeDataError(ValueError):
    """A bad record in a particular month, safe to report without the request URL."""


class MolitResponseError(ValueError):
    """API-wide rejection, safe to report without the request URL."""


def fetch_month(month, district=GWANGJIN):
    key = os.environ.get("MOLIT_SERVICE_KEY")
    if not key:
        raise MolitResponseError("Set MOLIT_SERVICE_KEY to the decoded public data portal key")
    # The source can report a completion year later than an old contract.
    latest_build_year = date.today().year + 2
    page = 1
    seen = 0
    expected = None
    trades = Counter()
    complexes = {}
    while True:
        query = urlencode(dict(serviceKey=key, LAWD_CD=district, DEAL_YMD=month,
                               pageNo=page, numOfRows=1000))
        with urlopen(ENDPOINT + "?" + query, timeout=45) as response:
            root = ET.fromstring(response.read())
        code = root.findtext("header/resultCode")
        if code not in {"000", "00", "0000"}:
            raise MolitResponseError(f"MOLIT response error (code {code})")
        total = int(root.findtext("body/totalCount"))
        items = root.findall("body/items/item")
        if expected is None:
            expected = total
        if total != expected or int(root.findtext("body/pageNo")) != page or seen + len(items) > total:
            raise MolitResponseError(f"Inconsistent MOLIT pages: requested={page}, returned={root.findtext('body/pageNo')}, expected={expected}, total={total}, seen={seen}, items={len(items)}")
        for number, item in enumerate(items, start=1):
            row = {node.tag: (node.text or "").strip() for node in item}
            try:
                cancelled = row.get("cdealType", "") not in {"", "-"} or row.get("cdealDay", "") not in {"", "-"}
                day = date(int(row["dealYear"]), int(row["dealMonth"]), int(row["dealDay"]))
                dong, lot, name = (row[k] for k in ("umdNm", "jibun", "aptNm"))
                if not all((dong, lot, name)) or day.strftime("%Y%m") != month or row["sggCd"] != district:
                    raise ValueError(f"location/month mismatch: dong={dong!r}, lot={lot!r}, name={name!r}, district={row.get('sggCd')!r}, day={day}")
                address = f"서울특별시 {DISTRICTS[district]} {dong}"
                cid = "|".join((address, lot, name))
                area = Decimal(row["excluUseAr"])
                price = Decimal(row["dealAmount"].replace(",", ""))
                built = int(row["buildYear"])
                if area <= 0 or price <= 0 or not 1900 <= built <= latest_build_year:
                    raise ValueError(
                        f"Invalid trade values: district={district}, month={month}, "
                        f"apt={name}, day={day}, area={area}, price={price}, built={built}"
                    )
                complexes[cid] = (cid, name, address+" "+lot, built)
                group = int(round(area / 5)) * 5
                floor = int(row["floor"]) if row.get("floor") else None
            except (KeyError, ValueError, InvalidOperation) as exc:
                raise TradeDataError(f"page={page} row={number} apt={row.get('aptNm')!r} "
                                     f"({type(exc).__name__}: {exc})") from None
            trades[(cid, group, day, area, floor, price, cancelled)] += 1
        seen += len(items)
        if seen == total:
            return complexes, trades
        if not items:
            raise MolitResponseError(f"Incomplete MOLIT response: page={page}, seen={seen}, total={total}")
        page += 1

