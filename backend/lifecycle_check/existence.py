"""Confirm complexes that still stand, from jipfit_lifecycle@d86dcdf:existence.py (DESIGN.md section 7a).

현존 needs, on the representative lot, a current main residential title approved in the complex's built
year, no same-year residential title cancelled with code 2 or 6 (말소·일부말소; 1·7 are register
tidy-ups), and no housing demolition recorded after that approval. The name is not compared, because
register building names vary. Two further ways to stand were added on 2026-11-26 (see judge).
Everything else goes on to the demolition investigation with a code.
"""
from datetime import date

from lifecycle_check.common import name_key, residential, text, ymd
from lifecycle_check.demolition import kapt_records
from lifecycle_check.sources import complete_snapshot, parcel_params

BASIC = ("current", "closed", "demolition")
HOUSING = ("공동주택", "아파트")
_MANAGED = {}


def managed_records(cache):
    """Every cached K-apt basic record (read once per cache folder and run)."""
    if cache not in _MANAGED:
        _MANAGED[cache] = kapt_records(cache)
    return _MANAGED[cache]


def real_cancellation(title, current):
    """A cancelled main title that may mean demolition, rather than a re-issued or partial register.

    Not a sign: no households (a 관리동 or shop part), a current main title with the same 동 or name and
    approval date (or the same approval date and households), or a cancellation within 30 days of its
    own approval with a current main title approved within 30 days that has the same households or name.
    """
    if text(title, "shterGbCd") not in ("2", "6") or text(title, "mainAtchGbCd") != "0":
        return False
    households, approved = text(title, "hhldCnt"), ymd(text(title, "useAprDay"))
    if not int(households or 0):
        return False
    label = text(title, "dongNm") or text(title, "bldNm")
    mains = [b for b in current if text(b, "mainAtchGbCd") == "0"]
    if any(ymd(text(b, "useAprDay")) == approved and ((text(b, "dongNm") or text(b, "bldNm")) == label
                                                       or text(b, "hhldCnt") == households) for b in mains):
        return False

    def days(a, b):
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) if a and b else 9999

    same_name = name_key(text(title, "bldNm"))
    if days(ymd(text(title, "shterDay")), approved) <= 30 and any(
            days(ymd(text(b, "useAprDay")), approved) <= 30
            and (text(b, "hhldCnt") == households or (same_name and name_key(text(b, "bldNm")) == same_name))
            for b in mains):
        return False
    return True


def judge(target, cache, as_of, kapt_cache=None):
    """(existence, code, note): existence is 현존 or 조사; code is empty for 현존.

    K-apt basic records come from kapt_cache when the registers are cached elsewhere (monthly runs).

    Ways to stand, tried in order: a same-year residential main building (the base rule), a same-year
    main building of any purpose (오피스텔·도시형생활주택 registered as 업무시설), a K-apt complex on the
    same lot with the same approval year and name (new complexes not yet registered), residential
    buildings approved one to three years after the built year (the recorded year differs), and
    residential buildings with a blank approval date whose name matches. A rebuilt complex has a much
    later approval year, so these are the original buildings unless a real cancellation
    (real_cancellation) or a later housing demolition is recorded.
    """
    params = parcel_params(target["parcel_key"])
    registers = {service: complete_snapshot(cache, service, params, as_of) for service in BASIC}
    if any(rows is None for rows in registers.values()):
        return "조사", "수집미완", ", ".join(s for s, rows in registers.items() if rows is None)
    current, closed, events = (registers[service] for service in BASIC)
    built = str(target["built"])
    real = [b for b in closed if real_cancellation(b, current)]

    def cancelled(wanted):
        return [b for b in real if wanted(b) and text(b, "useAprDay")[:4] == built]

    def housing_removed_after(buildings):
        approved = min(ymd(text(b, "useAprDay")) for b in buildings)
        return sorted(day for e in events if any(word in text(e, "mainPurpsCdNm") for word in HOUSING)
                      for field in ("demolEndDay", "demolExtngDay") if (day := ymd(text(e, field))) and day > approved)

    def cancellation_note(gone):
        days = sorted(ymd(text(b, "shterDay")) for b in gone)
        return f"{len(gone)}동, {days[0]}"

    homes = [b for b in current if residential(b)]
    same = [b for b in homes if text(b, "useAprDay")[:4] == built]
    if same:
        if gone := cancelled(residential):
            return "조사", "같은연도말소", cancellation_note(gone)
        if removed := housing_removed_after(same):
            return "조사", "승인후공동주택철거", removed[-1]
        return "현존", "", f"{built}년 사용승인 주거동 {len(same)}동"

    main = [b for b in current if text(b, "mainAtchGbCd") == "0" and text(b, "useAprDay")[:4] == built]
    gone = cancelled(lambda b: True)
    lot = str(int(params["bun"])) + (f"-{int(params['ji'])}" if int(params["ji"]) else "")
    name = name_key(target.get("name", ""))[:4]
    managed = [r for r in managed_records(kapt_cache or cache)
               if str(r.get("bjdCode") or "") == params["sigunguCd"] + params["bjdongCd"]
               and params["platGbCd"] == "0" and str(r.get("kaptAddr") or "").split()[3:4] == [lot]
               and (day := ymd(str(r.get("kaptUsedate") or ""))) and day[:4] == built and day <= as_of.isoformat()
               and name and name_key(str(r.get("kaptName") or "")).startswith(name)]
    if main or managed:
        if gone:
            return "조사", "같은연도말소", cancellation_note(gone)
        if main and (removed := housing_removed_after(main)):
            return "조사", "승인후공동주택철거", removed[-1]
        if main:
            return "현존", "", f"{built}년 사용승인 주건축물 {len(main)}동(용도: " + ", ".join(
                sorted({text(b, "mainPurpsCdNm") for b in main})) + ")"
        return "현존", "", "K-apt " + ", ".join(str(r.get("kaptCode") or r.get("kaptName")) for r in managed)

    if not homes:
        old = [b for b in closed if residential(b)]
        return "조사", "현행주거동없음" if old else "주거동기록없음", ""
    lost = [b for b in real if residential(b)]
    later = [b for b in homes if built.isdigit() and text(b, "useAprDay")[:4].isdigit()
             and 0 < int(text(b, "useAprDay")[:4]) - int(built) <= 3]
    if later and not lost and not housing_removed_after(later):
        years = sorted({text(b, "useAprDay")[:4] for b in later})
        return "현존", "", f"사용승인 {', '.join(years)}년(준공연도 {built}과 1~3년 차이) 주거동 {len(later)}동"
    blank = [b for b in homes if not text(b, "useAprDay")]
    if (blank and name and not lost and not events
            and any(name_key(text(b, "bldNm")).startswith(name) for b in blank)):
        return "현존", "", f"사용승인일 공란, 단지명 일치 주거동 {len(blank)}동"
    years = sorted({text(b, "useAprDay")[:4] for b in homes if text(b, "useAprDay")})
    if not years:
        return "조사", "사용승인일공란", ""
    near = built.isdigit() and any(abs(int(year) - int(built)) == 1 for year in years)
    return "조사", "준공연도1년차" if near else "준공연도다름", ", ".join(years)
