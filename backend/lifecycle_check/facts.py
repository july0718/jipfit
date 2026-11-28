"""One fact record per complex from its parcel's basic registers, from jipfit_lifecycle@d86dcdf:facts.py."""
from lifecycle_check.common import name_key, residential, text, ymd
from lifecycle_check.sources import cached, complete_snapshot, parcel_params

BASIC = ("current", "closed", "demolition")


def register_calls(targets, cache, as_of, since=None):
    """Basic registers still to fetch, once per parcel: missing, incomplete, or retrieved outside the window."""
    calls, seen = [], set()
    for target in targets:
        key = target["parcel_key"]
        if key in seen:
            continue
        seen.add(key)
        params = parcel_params(key)
        for service in BASIC:
            snapshot = cached(cache, service, params)
            day = str((snapshot or {}).get("retrieved_on") or "")
            if (not snapshot or not isinstance(snapshot.get("rows"), list)
                    or snapshot.get("total") != len(snapshot["rows"])
                    or not day or day > as_of.isoformat() or (since and day < since.isoformat())):
                calls.append((service, params))
    return calls


def complex_facts(target, cache, as_of):
    params = parcel_params(target["parcel_key"])
    registers = {service: complete_snapshot(cache, service, params, as_of) for service in BASIC}
    missing = [service for service, rows in registers.items() if rows is None]
    current, closed, events = (registers[service] or [] for service in BASIC)
    built, name = str(target["built"]), name_key(target["name"])
    homes = [b for b in current if residential(b)]
    old = [b for b in closed if residential(b)]
    cancelled = sorted(day for b in old if (day := ymd(text(b, "shterDay"))))
    removed = sorted(day for e in events for field in ("demolEndDay", "demolExtngDay") if (day := ymd(text(e, field))))
    return {
        "complex_id": target["complex_id"], "parcel_key": target["parcel_key"], "built": built,
        "registers_complete": not missing, "missing_registers": missing,
        "current_titles": len(current), "current_homes": len(homes),
        "current_households": sum(int(text(b, "hhldCnt") or 0) for b in homes),
        "current_years": sorted({text(b, "useAprDay")[:4] for b in homes if text(b, "useAprDay")}),
        "current_same_year": sum(text(b, "useAprDay")[:4] == built for b in homes),
        "current_name_match": sum(bool(name) and name_key(text(b, "bldNm")) == name for b in homes),
        "current_names": list(dict.fromkeys(text(b, "bldNm") for b in homes if text(b, "bldNm"))),
        "closed_titles": len(closed), "closed_homes": len(old),
        "closed_households": sum(int(text(b, "hhldCnt") or 0) for b in old),
        "closed_same_year": sum(text(b, "useAprDay")[:4] == built for b in old),
        "closed_codes": sorted({text(b, "shterGbCd") for b in old if text(b, "shterGbCd")}),
        "first_cancelled": cancelled[0] if cancelled else "", "last_cancelled": cancelled[-1] if cancelled else "",
        "demolition_records": len(events), "last_demolition": removed[-1] if removed else "",
        "trades": int(target.get("trades") or 0), "last_trade": target.get("last_trade", ""),
    }
