"""Turn the complex list into parcel keys, from jipfit_lifecycle@d86dcdf:targets.py.

법정동 codes come from the official MOLIT trade responses cached in trade_detail; Jipfit addresses use the
same dong names. The baseline cache is read together with the trade months fetched this month for dongs it
does not know (jipfit_lifecycle@d86dcdf:collect_dong_codes.py).
"""
import re
from collections import Counter, defaultdict

from lifecycle_check.sources import read_json

# Same table as the Jipfit backend (backend/districts.py).
DISTRICTS = {
    "11215": "광진구", "11200": "성동구", "11110": "종로구", "11140": "중구",
    "11170": "용산구", "11230": "동대문구", "11260": "중랑구", "11290": "성북구",
    "11305": "강북구", "11320": "도봉구", "11350": "노원구", "11380": "은평구",
    "11410": "서대문구", "11440": "마포구", "11470": "양천구", "11500": "강서구",
    "11530": "구로구", "11545": "금천구", "11560": "영등포구", "11590": "동작구",
    "11620": "관악구", "11650": "서초구", "11680": "강남구", "11710": "송파구",
    "11740": "강동구",
}
CODES = {name: code for code, name in DISTRICTS.items()}
LOT = re.compile(r"(산)?(\d{1,4})(?:-(\d{1,4}))?")
FIELDS = ("complex_id", "name", "built", "address", "parcel_key", "sigunguCd", "bjdongCd", "platGbCd", "bun", "ji",
          "same_parcel", "trades", "first_trade", "last_trade")


def dong_codes(cache, *supplements):
    """(시군구코드, 법정동명) → 법정동코드 from cached trade responses.

    These are reported as conflicts instead, since they come from stray rows (a dong filed under the wrong
    district): a name seen with two codes, a name that is not the usual one for its code, and a name and
    code that belong mostly to another district. A supplement replaces the same month of an earlier cache.
    """
    seen = defaultdict(Counter)
    paths = {path.name: path for folder in (cache, *supplements) for path in (folder / "trade_detail").glob("*.json")}
    for path in paths.values():
        for row in read_json(path).get("rows", []):
            sgg, name, code = (str(row.get(k) or "").strip() for k in ("sggCd", "umdNm", "umdCd"))
            if re.fullmatch(r"\d{5}", sgg) and name and re.fullmatch(r"\d{5}", code):
                seen[sgg, name][code] += 1
    owners, homes = defaultdict(Counter), defaultdict(Counter)
    for (sgg, name), codes in seen.items():
        for code, count in codes.items():
            owners[sgg, code][name] += count
            homes[name, code][sgg] += count

    def sole_top(counter, key):
        return sum(c == counter[key] for c in counter.values()) == 1 and counter.most_common(1)[0][0] == key

    codes, conflicts = {}, {}
    for (sgg, name), found in seen.items():
        code = next(iter(found))
        if len(found) == 1 and sole_top(owners[sgg, code], name) and sole_top(homes[name, code], sgg):
            codes[sgg, name] = code
        else:
            conflicts[sgg, name] = sorted(found)
    return codes, conflicts


def valid_trades(trades, as_of):
    dates = defaultdict(list)
    for trade in trades:
        day = trade["contract_date"]
        if trade["is_cancelled"] == "f" and day <= as_of.isoformat():
            dates[trade["complex_id"]].append(day)
    return dates


def parse(complex_, codes, conflicts=None):
    """The parcel fields of one complex, or the reason it cannot be identified."""
    place, lot, _ = (complex_["complex_id"].split("|") + ["", ""])[:3]
    words = place.split()
    if len(words) != 3 or words[0] != "서울특별시":
        return None, "서울 주소 아님"
    district = CODES.get(words[1])
    if not district:
        return None, "시군구 미확인"
    matched = LOT.fullmatch(lot)
    if not matched:
        return None, "지번 형식"
    if complex_["address"].split()[3:] != [lot]:
        return None, "주소와 단지ID 지번 불일치"
    if (district, words[2]) in (conflicts or {}):
        return None, "법정동코드 충돌"
    dong = codes.get((district, words[2]))
    if not dong:
        return {"sigunguCd": district, "dong_name": words[2]}, "법정동코드 미확보"
    mountain, bun, ji = matched.groups()
    return {"sigunguCd": district, "bjdongCd": dong, "platGbCd": "1" if mountain else "0",
            "bun": bun.zfill(4), "ji": (ji or "0").zfill(4)}, ""


def identify(complexes, trades, codes, as_of, conflicts=None):
    """Targets with parcel keys and trade summaries, and the complexes that could not be identified."""
    dates = valid_trades(trades, as_of)
    targets, failed = [], []
    for complex_ in complexes:
        cid = complex_["complex_id"]
        found = sorted(dates.get(cid, []))
        parcel, reason = parse(complex_, codes, conflicts)
        if reason:
            failed.append({"complex_id": cid, "name": complex_["name"], "address": complex_["address"],
                           "reason": reason, **({"sigunguCd": parcel["sigunguCd"], "dong_name": parcel["dong_name"]}
                                                if parcel else {}),
                           "trade_months": " ".join(sorted({day[:7].replace("-", "") for day in found}))})
            continue
        targets.append({**{k: complex_[k] for k in ("complex_id", "name", "built", "address")}, **parcel,
                        "parcel_key": "_".join(parcel.values()), "trades": len(found),
                        "first_trade": found[0] if found else "", "last_trade": found[-1] if found else ""})
    shared = Counter(t["parcel_key"] for t in targets)
    for target in targets:
        target["same_parcel"] = shared[target["parcel_key"]]
    return [{k: t[k] for k in FIELDS} for t in targets], failed


def needed_months(failed, trades):
    """Trade months to fetch so that every dong lacking a code appears in an official response."""
    missing = defaultdict(set)
    for item in failed:
        if item["reason"] == "법정동코드 미확보":
            missing[item["sigunguCd"], item["dong_name"]].add(item["complex_id"])
    months = defaultdict(set)   # (district, month) → dongs it would reveal
    for trade in trades:
        for (district, dong), ids in missing.items():
            if trade["complex_id"] in ids and trade["is_cancelled"] == "f":
                months[district, trade["contract_date"][:7].replace("-", "")].add(dong)
    calls, open_ = [], set(missing)
    while open_:
        # Greedy cover: the month revealing the most remaining dongs, the latest month on ties.
        best = max(months, key=lambda key: (len({(key[0], d) for d in months[key]} & open_), key[1]), default=None)
        gained = {(best[0], d) for d in months[best]} & open_ if best else set()
        if not gained:
            break
        calls.append(best)
        open_ -= gained
    return sorted(calls)
