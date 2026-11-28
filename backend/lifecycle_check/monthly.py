"""The monthly judgement, from jipfit_lifecycle@d86dcdf:monthly.py (PLAN_MONTHLY.md 4-1 to 4-5).

evidence_text and first_cancellation come from jipfit_lifecycle@d86dcdf:export_results.py,
cancelled_after_trade from baseline.py. The rules are unchanged; only the mass-confirmation guard
(plan-lifecycle-auto.md 6-2) and the places the files are written to are new. Rows read from the database
carry strings and None, as the rows of a backup did.
"""
import hashlib
import json
import re
import shutil
from collections import Counter
from datetime import date

from lifecycle_check.collect import fetch_all
from lifecycle_check.common import csv_output, name_key, residential, text, ymd
from lifecycle_check.demolition import cancelled_roster, current_homes, holding_reason, kapt_records, missing_calls
from lifecycle_check.demolition import project_pages
from lifecycle_check.existence import BASIC, HOUSING, judge, real_cancellation
from lifecycle_check.facts import complex_facts, register_calls
from lifecycle_check.sources import cache_file, complete_snapshot, parcel_params

BASIS_PRESENT = "자동 현존 확인"
BASIS_CANCELLED = "대장 말소 자동 확정"
BASIS_HELD = "월간 판정 보류"
MASS_HOLD = "대량확정보류"
INCOMPLETE_HOLD = "수집미완"
# A monthly cache started longer ago than this holds another month's registers.
CACHE_DAYS = 7
CACHE_MARKER = "monthly.json"
# holding_reason's answers as DESIGN.md 7-b hold codes.
HELD_CODES = {
    "사업장 철거완료일이 마지막 계약 이전": "철거후거래",
    "준공 5년 이내 말소": "단기말소",
    "같은 이름·준공연도의 현행 주거동": "타지번잔존",
    "같은 이름·사용승인연도의 K-apt 단지": "타지번잔존",
    "폐쇄 부속지번 미수집": "수집미완",
    "부속지번 현행 대장 미수집": "수집미완",
    "부속지번에 말소 전 사용승인 주거동": "일부존치",
}
# Jipfit reads these two fields from the same positions.
TRADE_CAUTION = re.compile(r"^basis=[^;]*; trade_caution_from=(\d{4}-\d{2}-\d{2})(?:;|$)")
SUCCESSOR_NAME = re.compile(r"; successor_name=([^;]*); note=")
FINGERPRINT_PARTS = ("closed", "demolition", "current")
AUTO_FIELDS = ("complex_id", "name", "address", "built", "이전판정", "이전보류코드", "동수", "세대수", "첫말소일",
               "마지막말소일", "마지막계약일", "부속지번수", "철거대장", "후속단지ID", "근거")
REVIEW_FIELDS = ("complex_id", "name", "address", "built", "구분", "보류코드", "상세", "이전판정", "결과포함")


# Inputs

def linked_ids(results):
    """Complexes that ever had a successor ID applied; a monthly run never links them again."""
    return {r["complex_id"] for r in results
            if r["existence"] == "demolished" and r["successor_id"] and r["applied_status"] == "applied"}


def active_hold(state):
    """The hold code of a pending judgment newer than the confirmed one, else None.

    Jipfit keeps pending_* after a later confirmation, so the dates decide whether the hold is still open.
    """
    if not state or not state.get("pending_hold_code"):
        return None
    confirmed = state.get("as_of") if state.get("existence") else None
    if confirmed and (state.get("pending_as_of") or "") <= confirmed:
        return None
    return state["pending_hold_code"]


def branch(state):
    """Which row of PLAN_MONTHLY.md 4-3 applies."""
    if state and state.get("existence") == "demolished":
        return "demolished"
    if active_hold(state) is not None:
        return "held"
    if state and state.get("existence") == "present":
        return "present"
    return "new"


def previous_label(state):
    return {"present": "현존", "demolished": "멸실", None: "없음"}[(state or {}).get("existence")]


def evidence_text(row, caution):
    # basis and trade_caution_from lead so Jipfit can read the date without parsing free text.
    parts = [f"basis={row['basis']}"]
    if caution:
        parts.append(f"trade_caution_from={caution}")
    if row["successor_name"]:
        parts.append(f"successor_name={row['successor_name']}")
    parts.append(f"note={row['note']}")
    return "; ".join(parts)


def cancelled_after_trade(parcel_key, last_trade, built, cache, as_of):
    """The first residential cancellation date when the last trade is on or after it (trades to review)."""
    if not parcel_key or not last_trade:
        return ""
    closed = complete_snapshot(cache, "closed", parcel_params(parcel_key), as_of) or []
    days = sorted(ymd(text(b, "shterDay")) for b in closed if residential(b) and text(b, "shterGbCd") in ("2", "6")
                  and text(b, "useAprDay")[:4] >= str(built)[:4] and ymd(text(b, "shterDay")))
    return days[0] if days and last_trade >= days[0] else ""


def first_cancellation(parcel_key, built, cache, as_of):
    """The first residential cancellation date of the parcel, "" when the cached registers do not show one."""
    # cancelled_after_trade answers with that date only for a trade on or after it; the latest date always is.
    return cancelled_after_trade(parcel_key, "9999-12-31", built, cache, as_of)


# Register fingerprints (4-1)

def parcel_fingerprint(cache, parcel_key, as_of):
    """The register records the judgement depends on, or None while a basic register is missing."""
    params = parcel_params(parcel_key)
    registers = {service: complete_snapshot(cache, service, params, as_of) for service in BASIC}
    if any(rows is None for rows in registers.values()):
        return None
    current, closed, events = (registers[service] for service in BASIC)

    def records(rows, fields):
        return sorted([text(row, field) for field in fields] for row in rows)

    found = {
        "closed": records((b for b in closed if real_cancellation(b, current)),
                          ("mgmShtregPk", "shterGbCd", "shterDay")),
        "demolition": records((e for e in events if any(w in text(e, "mainPurpsCdNm") for w in HOUSING)),
                              ("mgmPmsrgstPk", "demolEndDay", "demolExtngDay")),
        "current": records((b for b in current if residential(b)), ("mgmBldrgstPk", "useAprDay", "hhldCnt")),
    }
    digest = hashlib.sha256(json.dumps(found, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"sha256": digest, **found}


def fingerprint_changes(before, after):
    """(new records, vanished records) per part."""
    added, removed = {}, {}
    for part in FINGERPRINT_PARTS:
        old, new = (Counter(map(tuple, found[part])) for found in (before, after))
        if new - old:
            added[part] = sorted((new - old).elements())
        if old - new:
            removed[part] = sorted((old - new).elements())
    return added, removed


def describe_changes(changes):
    return " / ".join(f"{part}: " + ", ".join("|".join(record) for record in records)
                      for part, records in changes.items())


def cached_parcels(cache):
    """Parcel keys of the cached basic registers. Files do not store their query, so a row's parcel is
    accepted only when it hashes to the file's name. Parcels without any record are read at comparison."""
    keys = set()
    for service in BASIC:
        for path in (cache / service).glob("*.json"):
            rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
            if not rows:
                continue
            params = {k: str(rows[0].get(k) or "") for k in ("sigunguCd", "bjdongCd", "platGbCd", "bun", "ji")}
            if cache_file(cache, service, params).name == path.name:
                keys.add("_".join(params.values()))
    return sorted(keys)


def baseline_fingerprints(reference, as_of):
    """The fingerprint document of every parcel the reference cache holds at that day."""
    parcels = {}
    for key in cached_parcels(reference):
        found = parcel_fingerprint(reference, key, as_of)
        if found:
            parcels[key] = found
    return {"as_of": as_of.isoformat(), "parcels": parcels}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


# Judgement (4-3)

def a_track_row(target, facts):
    """The columns demolished.cancelled_roster and holding_reason read, filled from the identified target."""
    return {"판정": "pending1", "거래검증자료": "집핏 PostgreSQL 백업", "구지번키": facts["parcel_key"],
            "최근유효계약일": facts["last_trade"], "준공연도": facts["built"], "구지번": target["address"],
            "원래거래단지명": target["name"]}


def roster_failure(facts, closed, events, as_of, judged):
    """Why cancelled_roster found no roster, as a hold code. This only labels; it decides nothing."""
    old = [b for b in closed if residential(b)]
    if not facts["current_homes"] and not old:
        return "기록없음", "현행·폐쇄 주거동 기록 없음"
    if facts["current_homes"]:
        if facts["current_same_year"] and (facts["closed_same_year"]
                                           or judged in ("같은연도말소", "승인후공동주택철거")):
            return "일부존치", f"같은 연도의 현행 주거동과 말소·철거 신호({judged})"
        return "범위미확인", f"구지번에 현행 주거동 {facts['current_homes']}동"
    if not sum(int(text(b, "hhldCnt") or 0) for b in old):
        return "범위미확인", "말소 주거동 세대수 0"
    if facts["built"] not in {text(b, "useAprDay")[:4] for b in old}:
        return "범위미확인", "준공연도에 사용승인된 말소동 없음"
    if any(text(b, "shterGbCd") != "2" for b in old):
        return "범위미확인", f"말소 코드 {', '.join(facts['closed_codes']) or '공란'}"
    if any(not ymd(text(b, "useAprDay")) for b in old):
        return "범위미확인", "말소동 사용승인일 공란"
    closes = sorted(ymd(text(b, "shterDay")) for b in old)
    if not closes[0] or closes[-1] > as_of.isoformat():
        return "범위미확인", "말소일 공란 또는 기준일 이후"
    latest = ymd(facts["last_trade"])
    if not latest:
        return "범위미확인", "유효 계약 없음"
    if latest >= closes[0]:
        return "말소후거래", f"마지막 계약 {latest}, 첫 말소 {closes[0]}"
    built = min(ymd(text(b, "useAprDay")) for b in old)
    removed = [day for e in events for field in ("demolEndDay", "demolExtngDay") if (day := ymd(text(e, field)))]
    if any(built < day <= latest for day in removed):
        return "철거후거래", f"마지막 계약 {latest} 이전의 철거 기록"
    return "범위미확인", "총괄표제부의 세대수·주건축물 수가 말소동과 맞지 않음"


def cancellation(target, cache, as_of, context, judged=""):
    """(confirmed facts or None, hold code, note) for the cancellation rule of 4-3."""
    facts = complex_facts(target, cache, as_of)
    if not facts["registers_complete"]:
        return None, "수집미완", ", ".join(facts["missing_registers"])
    params = parcel_params(target["parcel_key"])
    current, closed, events = (complete_snapshot(cache, service, params, as_of) for service in BASIC)
    row = a_track_row(target, facts)
    found = cancelled_roster(row, cache, as_of)
    if not found:
        if (not facts["current_homes"] and facts["closed_homes"]
                and complete_snapshot(cache, "closed_outline", params, as_of) is None):
            return None, "수집미완", "폐쇄 총괄표제부 미수집"
        return (None, *roster_failure(facts, closed, events, as_of, judged))
    # A re-issued register is not a cancellation: every old title with households must be a real one.
    real = [b for b in found["old"] if real_cancellation(b, current)]
    if (any(int(text(b, "hhldCnt") or 0) and b not in real for b in found["old"])
            or facts["built"] not in {text(b, "useAprDay")[:4] for b in real}):
        return None, "범위미확인", "대장 재발급으로 보이는 말소 포함"
    reason, lots = holding_reason(row, found, cache, as_of, *context)
    if reason:
        return None, HELD_CODES.get(reason, "범위미확인"), reason
    return {**found, "lots": lots}, "", ""


def first_pass(complexes, targets, failed, states, cache, reference, fingerprints, as_of):
    """The step of every complex that needs only the basic registers."""
    by_target = {t["complex_id"]: t for t in targets}
    reasons = {f["complex_id"]: f["reason"] for f in failed}
    baseline_day = date.fromisoformat(fingerprints["as_of"])
    compared, plan = {}, {}

    def compare(key):
        if key not in compared:
            before = fingerprints["parcels"].get(key) or parcel_fingerprint(reference, key, baseline_day)
            after = parcel_fingerprint(cache, key, as_of)
            compared[key] = (None if before is None or after is None else fingerprint_changes(before, after))
        return compared[key]

    for complex_ in complexes:
        cid = complex_["complex_id"]
        state, target = states.get(cid), by_target.get(cid)
        item = plan[cid] = {"branch": branch(state), "target": target, "judged": "", "note": ""}
        if item["branch"] == "demolished":
            item["step"] = "demolished"
        elif not target:
            item.update(step="unidentified", note=reasons.get(cid, "지번키 없음"))
        else:
            existence, code, note = judge(target, cache, as_of, kapt_cache=reference)
            item.update(judged=code, note=note)
            if existence == "현존":
                item["step"] = "present"
            elif code == "수집미완":
                item["step"] = "incomplete"
            elif item["branch"] != "present":
                item["step"] = "cancel"
            else:
                changes = compare(target["parcel_key"])
                if changes is None:
                    item["step"] = "unfingerprinted"
                else:
                    item["step"] = "cancel" if changes[0] else "unchanged"
                    item["new_records"] = changes[0]
        if target and item["branch"] != "demolished" and (changes := compare(target["parcel_key"])):
            item["vanished"] = changes[1]
    return plan


def link_successor(cid, target, year, recorded, gone, complexes, by_parcel):
    """(successor IDs, how, review note) from Jipfit's own complexes (4-4)."""
    def later(other):
        built = str(other.get("built") or "")
        return built.isdigit() and int(built) >= year

    def district(value):
        words = value.split(" ")
        return words[1] if len(words) > 1 else ""

    notes = []
    names = [name.strip() for name in recorded.split(" · ") if name.strip()]
    if target:
        same = [c for c in by_parcel.get(target["parcel_key"], ())
                if c["complex_id"] != cid and c["complex_id"] not in gone and later(c)]
        if len(same) == 1:
            if names and name_key(same[0]["name"]) not in {name_key(n) for n in names}:
                notes.append(f"같은 지번 후속({same[0]['name']})과 기록된 이름({recorded})이 다름")
            return [same[0]["complex_id"]], "같은 지번", " / ".join(notes)
        if same:
            notes.append("같은 지번 후보 여러 개: " + ", ".join(c["complex_id"] for c in same))
    if names:
        found = []
        for name in names:
            key = name_key(name)
            matches = [c for c in complexes if key and c["complex_id"] != cid
                       and district(c["complex_id"]) == district(cid) and name_key(c["name"]) == key and later(c)]
            if len(matches) > 1:
                notes.append(f"이름 후보 여러 개({name}): " + ", ".join(c["complex_id"] for c in matches))
            found.append(matches[0]["complex_id"] if len(matches) == 1 else None)
        if all(found):
            return list(dict.fromkeys(found)), "기록된 이름", " / ".join(notes)
    return [], "", " / ".join(notes)


def link_evidence(state, ids, how, name):
    """The current evidence with the link added. Jipfit replaces the whole text, so the caution date and
    the earlier note are carried over."""
    evidence = state.get("evidence") or ""
    basis = re.match(r"basis=([^;]*)", evidence)
    caution = TRADE_CAUTION.match(evidence)
    recorded = SUCCESSOR_NAME.search(evidence)
    note = f"후속 연결({how}): {ids[0]}"
    if ids[1:]:
        note += ", 함께 기록된 후속: " + ", ".join(ids[1:])
    if evidence:
        note += "; 이전 근거: " + (evidence.split("; note=", 1)[1] if "; note=" in evidence else evidence)
    row = {"basis": basis[1] if basis else "월간 후속 연결", "note": note,
           "successor_name": recorded[1] if recorded else name.replace(";", ",")}
    return evidence_text(row, caution[1] if caution else "")


def judge_month(complexes, targets, failed, states, linked, plan, cache, reference, fingerprints, as_of, version):
    """Results (changes only), report rows and the fingerprints this month would leave behind."""
    context = (current_homes(cache), kapt_records(reference), *project_pages(reference))
    names = {c["complex_id"]: c["name"] for c in complexes}
    by_parcel = {}
    for target in targets:
        by_parcel.setdefault(target["parcel_key"], []).append(target)
    day = as_of.isoformat()
    results, auto, review, counts = {}, {}, [], Counter()
    keep_fingerprint = set()

    def note(complex_, kind, code="", detail="", included=False):
        state = states.get(complex_["complex_id"])
        review.append({**{k: complex_[k] for k in ("complex_id", "name", "address", "built")}, "구분": kind,
                       "보류코드": code, "상세": detail, "이전판정": previous_label(state),
                       "결과포함": "Y" if included else "N"})

    def emit(complex_, existence, evidence, **extra):
        """Add a result unless Jipfit holds something as new, which would quarantine or silently close it."""
        cid = complex_["complex_id"]
        state = states.get(cid) or {}
        confirmed = state.get("as_of") if state.get("existence") else None
        pending = (state.get("pending_as_of") or "") if existence == "pending" else ""
        if pending > day or (confirmed and (confirmed > day or (confirmed == day and state["existence"] != existence))):
            note(complex_, "기준일 충돌", extra.get("hold_code", ""),
                 f"집핏 판정 {confirmed or '없음'}, 보류 {state.get('pending_as_of') or '없음'}")
            if plan[cid]["target"]:
                keep_fingerprint.add(plan[cid]["target"]["parcel_key"])
            return False
        results[cid] = {"result_key": f"monthly-{as_of:%Y%m%d}:{cid}", "complex_id": cid, "existence": existence,
                        "as_of": day, "verifier_version": version, **extra, "evidence": evidence}
        return True

    def hold(complex_, code, detail):
        counts[f"보류 {code}"] += 1
        if active_hold(states.get(complex_["complex_id"])) == code:
            note(complex_, "보류(변화 없음)", code, detail)
            return
        evidence = evidence_text({"basis": BASIS_HELD, "successor_name": "", "note": f"{code}: {detail}"}, "")
        if emit(complex_, "pending", evidence, hold_code=code):
            note(complex_, "보류", code, detail, True)

    confirmed = {}
    for complex_ in complexes:
        cid = complex_["complex_id"]
        item, state = plan[cid], states.get(cid)
        step, kept = item["step"], item["branch"] == "present"
        if item.get("vanished"):
            note(complex_, "지문에서 사라진 기록", "", describe_changes(item["vanished"]))
        if step == "present":
            counts["현존"] += 1
            if item["branch"] != "present":
                emit(complex_, "present", evidence_text(
                    {"basis": BASIS_PRESENT, "successor_name": "", "note": item["note"]}, ""))
        elif step == "unchanged":
            counts["현존 유지(새 대장 기록 없음)"] += 1
        elif step == "unfingerprinted":
            counts["현존 유지(지문 없음)"] += 1
            note(complex_, "지문 없음", item["judged"], "비교할 지문이 없어 현재 판정 유지")
        elif step in ("unidentified", "incomplete"):
            code = "식별실패" if step == "unidentified" else "수집미완"
            if kept:
                counts[f"현존 유지({code})"] += 1
                note(complex_, code, code, item["note"])
            else:
                hold(complex_, code, item["note"])
        elif step == "cancel":
            found, code, detail = cancellation(item["target"], cache, as_of, context, item["judged"])
            if found:
                confirmed[cid] = found
            else:
                if item.get("new_records"):
                    detail += " [새 기록 " + describe_changes(item["new_records"]) + "]"
                hold(complex_, code, detail)

    gone = {cid for cid, state in states.items() if state.get("existence") == "demolished"} | set(confirmed)
    for complex_ in complexes:
        cid = complex_["complex_id"]
        item, state = plan[cid], states.get(cid) or {}
        target, found = item["target"], confirmed.get(cid)
        if item["step"] != "demolished" and not found:
            continue
        if not found:
            counts["멸실 유지"] += 1
            if target:
                facts = complex_facts(target, cache, as_of)
                if facts["registers_complete"] and facts["current_same_year"]:
                    note(complex_, "멸실 단지의 현존 신호", "",
                         f"{facts['built']}년 사용승인 현행 주거동 {facts['current_same_year']}동")
        ids, how, remark = [], "", ""
        recorded = SUCCESSOR_NAME.search(state.get("evidence") or "")
        recorded = recorded[1] if recorded else ""
        if found or cid not in linked:
            first = found["closes"][0] if found else (
                (first_cancellation(target["parcel_key"], complex_["built"], cache, as_of) if target else "")
                or state.get("effective_date") or "")
            if first:
                ids, how, remark = link_successor(cid, target, int(first[:4]), recorded, gone, complexes, by_parcel)
            else:
                counts["후속 연결 불가(첫 말소 연도 미확인)"] += 1
        if remark:
            note(complex_, "후속 연결 확인", "", remark, bool(ids))
        link = {"successor_id": ids[0], "successor_status": "linked"} if ids else {}
        if found:
            counts["말소 확정"] += 1
            closes = found["closes"]
            period = closes[0] if len(closes) == 1 else f"{closes[0]}~{closes[-1]}"
            summary = (f"종전 주거 표제부 {len(found['old'])}동·{found['households']}세대 전부 말소({period}), "
                       f"마지막 계약 {found['latest']}, 구지번에 현행 주거동 없음, 부속지번 {found['lots']}곳에 "
                       "말소 전 사용승인 주거동 없음" + (", 철거대장 있음" if found["recorded"] else ""))
            if ids:
                summary += f", 후속 연결({how}): " + ", ".join(ids)
            name = recorded or (names[ids[0]].replace(";", ",") if ids else "")
            evidence = evidence_text({"basis": BASIS_CANCELLED, "successor_name": name, "note": summary}, "")
            if emit(complex_, "demolished", evidence, effective_date=closes[0], effective_precision="day", **link):
                auto[cid] = {**{k: complex_[k] for k in ("complex_id", "name", "address", "built")},
                             "이전판정": previous_label(states.get(cid)),
                             "이전보류코드": active_hold(states.get(cid)) or "", "동수": len(found["old"]),
                             "세대수": found["households"], "첫말소일": closes[0], "마지막말소일": closes[-1],
                             "마지막계약일": found["latest"], "부속지번수": found["lots"],
                             "철거대장": "Y" if found["recorded"] else "N", "후속단지ID": " | ".join(ids),
                             "근거": summary}
        elif ids:
            # The confirmed judgment is repeated with the link added (4-5).
            carried = {k: state[k] for k in ("effective_date", "effective_precision") if state.get(k)}
            if emit(complex_, "demolished", link_evidence(state, ids, how, names[ids[0]]), **carried, **link):
                counts["기존 멸실 후속 연결"] += 1

    parcels = dict(fingerprints["parcels"])
    for key in by_parcel:
        latest = None if key in keep_fingerprint else parcel_fingerprint(cache, key, as_of)
        if latest:
            parcels[key] = latest
    summary = {
        "대상 단지": len(complexes), "지번키 식별": len(targets), "식별 실패": Counter(f["reason"] for f in failed),
        "지번": len(by_parcel),
        "기본 대장 미완 지번": len({parcel_key for parcel_key, rows in by_parcel.items()
                            if register_calls(rows[:1], cache, as_of)}),
        "이전 판정": Counter(previous_label(states.get(c["complex_id"])) for c in complexes),
        "판정": counts, "결과": Counter(r["existence"] for r in results.values()),
    }
    ordered = [results[cid] for cid in sorted(results)]
    return {"results": ordered, "auto": [auto[cid] for cid in sorted(auto)], "review": review, "summary": summary,
            "fingerprints": {"as_of": day, "parcels": parcels},
            "previous": {cid: previous_label(states.get(cid)) for cid in results}}


# Mass confirmation guard (plan-lifecycle-auto.md 6-2)

def hold_mass_confirmations(outcome, states, limit):
    """More confirmations than the monthly limit suggest bad data, so all of them become holds.

    The original judgments are kept as proposals the operator can accept. Returns the outcome to publish,
    which is the given one when the limit is not exceeded.
    """
    auto = {row["complex_id"]: row for row in outcome["auto"]}
    if len(auto) <= limit:
        return {**outcome, "proposals": []}
    results, proposals, review = [], [], list(outcome["review"])
    for result in outcome["results"]:
        cid = result["complex_id"]
        if cid not in auto or result["existence"] != "demolished":
            results.append(result)
            continue
        proposals.append({**result, "result_key": f"proposal-{result['result_key']}"})
        state, row = states.get(cid) or {}, auto[cid]
        detail = f"자동 확정 {len(auto)}곳이 한도 {limit}곳을 넘어 보류. {row['근거']}"
        entry = {**{k: row[k] for k in ("complex_id", "name", "address", "built")}, "보류코드": MASS_HOLD,
                 "상세": detail, "이전판정": row["이전판정"], "결과포함": "N"}
        if active_hold(state) == MASS_HOLD:
            review.append({**entry, "구분": "보류(변화 없음)"})
        elif (state.get("pending_as_of") or "") > result["as_of"]:
            review.append({**entry, "구분": "기준일 충돌"})
        else:
            evidence = evidence_text(
                {"basis": BASIS_HELD, "successor_name": "", "note": f"{MASS_HOLD}: {detail}"}, "")
            results.append({**{k: result[k] for k in ("result_key", "complex_id")}, "existence": "pending",
                            **{k: result[k] for k in ("as_of", "verifier_version")}, "hold_code": MASS_HOLD,
                            "evidence": evidence})
            review.append({**entry, "구분": "보류", "결과포함": "Y"})
    kept = {result["complex_id"] for result in results}
    summary = {**outcome["summary"], "결과": Counter(r["existence"] for r in results),
               "판정": outcome["summary"]["판정"] + Counter({f"보류 {MASS_HOLD}": len(proposals)})}
    return {**outcome, "results": results, "proposals": proposals, "review": review, "summary": summary,
            "previous": {cid: label for cid, label in outcome["previous"].items() if cid in kept}}


# Collection (4-2)

def prepare_cache(cache, reference, as_of):
    """Only a new folder or this month's own cache may be written; the reference cache never.

    The automatic run cannot be given another folder, so a monthly cache older than CACHE_DAYS is removed
    and collected again.
    """
    target, base = cache.resolve(), reference.resolve()
    if target == base or base.is_relative_to(target) or target.is_relative_to(base):
        raise ValueError(f"기본 캐시에는 쓰지 않습니다: {cache}")
    marker = cache / CACHE_MARKER
    if marker.is_file():
        started = date.fromisoformat(json.loads(marker.read_text(encoding="utf-8"))["started"])
        age = (as_of - started).days
        if age < 0:
            raise ValueError(f"{started}에 시작한 캐시가 기준일({as_of})보다 뒤입니다: {cache}")
        if age < CACHE_DAYS:
            return
        print(f"{started}에 시작한 월별 캐시를 지우고 다시 받습니다: {cache}", flush=True)
        shutil.rmtree(cache)
    elif cache.exists() and any(cache.iterdir()):
        raise ValueError(f"월별 캐시가 아닌 폴더입니다({CACHE_MARKER} 없음): {cache}")
    write_json(marker, {"started": as_of.isoformat()})


def extra_calls(rows, cache, as_of):
    """closed_outline, closed_land and attached-lot titles still needed for the cancellation candidates."""
    outlines = {}
    for row in rows:
        params = parcel_params(row["구지번키"])
        if complete_snapshot(cache, "closed_outline", params, as_of) is None:
            outlines[tuple(params.values())] = ("closed_outline", params)
    return list(outlines.values()) + missing_calls(rows, cache, as_of)


def candidate_rows(plan, cache, as_of):
    """Complexes whose cancellation could be confirmed: no current home on the lot, closed homes recorded."""
    rows = []
    for item in plan.values():
        if item["step"] != "cancel":
            continue
        facts = complex_facts(item["target"], cache, as_of)
        if not facts["current_homes"] and facts["closed_homes"]:
            rows.append(a_track_row(item["target"], facts))
    return rows


def collect_basic(targets, cache, keys, as_of, workers, deadline=None):
    calls = register_calls(targets, cache, as_of)
    print(f"지번 {len({t['parcel_key'] for t in targets})} · 조회할 기본 대장 {len(calls)}", flush=True)
    if calls:
        for name, count in sorted(fetch_all(calls, cache, keys, workers, deadline=deadline).items()):
            print(name, count, flush=True)


def collect_extra(rows, cache, keys, as_of, workers, deadline=None):
    """Each round can reveal more lots, so ask until nothing new is needed or nothing more arrives."""
    asked = set()
    for _ in range(3):
        calls = [call for call in extra_calls(rows, cache, as_of)
                 if (call[0], tuple(call[1].values())) not in asked]
        if not calls:
            break
        asked.update((service, tuple(params.values())) for service, params in calls)
        print(f"말소 확정 후보 {len(rows)} · 조회할 추가 대장 {len(calls)}", flush=True)
        for name, count in sorted(fetch_all(calls, cache, keys, workers, deadline=deadline).items()):
            print(name, count, flush=True)


# Bundle (4-5) and reports

def encode_results(results):
    return "".join(json.dumps(result, ensure_ascii=False) + "\n" for result in results).encode("utf-8")


def manifest_for(batch, as_of, version, results, body):
    return {"batch": batch, "as_of": as_of, "verifier_version": version, "results": len(results),
            "sha256": hashlib.sha256(body).hexdigest(),
            "counts": dict(Counter(r["existence"] for r in results))}


def report_text(batch, outcome):
    summary = outcome["summary"]
    lines = [f"# 월간 존치 판정 {batch}", "", f"- 기준일: {outcome['fingerprints']['as_of']}"]
    for title, value in summary.items():
        if isinstance(value, dict):
            lines += ["", f"## {title}", "", "| 항목 | 수 |", "| :--- | ---: |"]
            lines += [f"| {name} | {count} |" for name, count in sorted(value.items())] or ["| 없음 | 0 |"]
        else:
            lines.append(f"- {title}: {value}")
    lines += ["", "## 바뀐 단지", ""]
    if outcome["results"]:
        lines += ["| 단지 ID | 이전 | 이번 | 보류 코드 | 후속 |", "| :--- | :--- | :--- | :--- | :--- |"]
        for result in outcome["results"]:
            cells = (result["complex_id"], outcome["previous"][result["complex_id"]], result["existence"],
                     result.get("hold_code", ""), result.get("successor_id", ""))
            lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    else:
        lines.append("없음")
    held = (f" (한도를 넘어 반영하지 않고 {MASS_HOLD}. 확인한 뒤 lifecycle_admin.py accept)"
            if outcome.get("proposals") else "")
    lines += ["", f"- 자동 확정 {len(outcome['auto'])}곳: auto_confirmed.csv{held}",
              f"- 검토 {len(outcome['review'])}행: review.csv", ""]
    return "\n".join(lines)


def hold_rows(outcome):
    """The holds this run leaves open: new ones and the unchanged ones."""
    return [row for row in outcome["review"] if row["구분"].startswith("보류")]


def fresh_folder(folder):
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    return folder


def write_reports(folder, batch, outcome):
    fresh_folder(folder)
    (folder / "report.md").write_text(report_text(batch, outcome), encoding="utf-8")
    csv_output(folder / "auto_confirmed.csv", outcome["auto"], list(AUTO_FIELDS))
    csv_output(folder / "review.csv", outcome["review"], list(REVIEW_FIELDS))
    csv_output(folder / "holds.csv", hold_rows(outcome), list(REVIEW_FIELDS))
    return folder


def write_batch(outcome, batches, reports, as_of, version, before_publish=None):
    """Write the reports and the bundle; the bundle appears under its final name only when it is whole.

    before_publish(batch) runs just before that rename, so the caller can record the name first.
    """
    batch = f"monthly-{as_of:%Y%m%d}"
    folder = batches / batch
    if folder.exists():
        raise ValueError(f"묶음 폴더가 이미 있습니다: {folder}")
    write_reports(reports / batch, batch, outcome)
    # apply_inbox skips names starting with a dot, so a half-written bundle is never read.
    temporary = fresh_folder(batches / f".{batch}")
    body = encode_results(outcome["results"])
    (temporary / "results.jsonl").write_bytes(body)
    write_json(temporary / "manifest.json",
               manifest_for(batch, as_of.isoformat(), version, outcome["results"], body))
    (temporary / "proposals.jsonl").write_bytes(encode_results(outcome.get("proposals", [])))
    write_json(temporary / "fingerprints.json", outcome["fingerprints"])
    if before_publish:
        before_publish(batch)
    temporary.rename(folder)
    return folder
