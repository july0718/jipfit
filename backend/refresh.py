"""Daily KST district refresh and monthly metadata inspection, also executable once with --once."""
from contextlib import nullcontext
from threading import Event, Lock, Thread
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time as clock
import uuid

import pandas as pd
import psycopg

from automatic_metadata import address_key, refresh_automatic_metadata
from districts import DISTRICTS, GWANGJIN
from facilities import ensure_facilities, refresh_sources, sync_complex_facilities
from features import build_inputs, input_month_range, is_in_input_window
from lifecycle_check import run_month
from lifecycle_inbox import apply_inbox
from lifecycle_results import empty_lifecycle_provider
from molit import fetch_month
from refresh_metadata import apply_metadata, apply_verified_complex_records, validate as validate_metadata

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
FIELDS = "complex_id,area_group,contract_date,area,floor,price_manwon,is_cancelled"
_schema_ready = False

SEONGDONG_VERIFIED_BATCH = "verified-seongdong-riverviewxi"
SEONGDONG_VERIFIED_RECORD = {
    "complex_id": "서울특별시 성동구 행당동|380|서울숲리버뷰자이",
    "expected_address": "서울특별시 성동구 행당동 380",
    "expected_name": "서울숲리버뷰자이",
    "evidence": "verified-pilot-representative-coordinate",
    "changes": {
        "latitude": 37.55605003075,
        "longitude": 127.03913424878,
    },
}


class ReconcileResult(tuple):
    """2-tuple (added, removed) that also carries reassigned trade count and affected complex IDs.

    excluded counts API rows dropped because they belong to a removed complex (complex_exclusions).
    """

    def __new__(cls, added, removed, *, reassigned=0, affected_complexes=(), excluded=0):
        obj = super().__new__(cls, (added, removed))
        obj.added = added
        obj.removed = removed
        obj.reassigned = reassigned
        obj.affected_complexes = tuple(affected_complexes)
        obj.excluded = excluded
        return obj


def issue_rebuild_complex_id(source_id, built, existing_ids=()):
    """Issue a deterministic suffixed complex_id preserving split_part(complex_id, ' ', 2)."""
    existing = set(existing_ids or ())
    base = f"{source_id}#{built}"
    if base not in existing:
        return base
    idx = 2
    while f"{base}-{idx}" in existing:
        idx += 1
    return f"{base}-{idx}"


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with psycopg.connect(connect_timeout=8) as db:
        db.execute("SELECT pg_advisory_xact_lock(1121506)")
        db.execute((ROOT/"db/trade_status.sql").read_text(encoding="utf-8"))
        db.execute((ROOT/"db/regions.sql").read_text(encoding="utf-8"))
        db.execute((ROOT/"db/trade_revisions.sql").read_text(encoding="utf-8"))
        db.execute((ROOT/"db/metadata_refresh.sql").read_text(encoding="utf-8"))
        if (ROOT/"db/user_accounts.sql").is_file():
            db.execute((ROOT/"db/user_accounts.sql").read_text(encoding="utf-8"))
        db.execute("""CREATE TABLE IF NOT EXISTS refresh_runs (
            day date PRIMARY KEY, finished_at timestamptz NOT NULL, added integer NOT NULL,
            removed integer NOT NULL, forecast_groups integer NOT NULL, slot_start timestamptz)""")
    _schema_ready = True


def _revision_score(old_key, new_key):
    _, old_group, old_day, old_area, old_floor, old_price, _ = old_key
    _, new_group, new_day, new_area, new_floor, new_price, _ = new_key
    area_diff = abs(float(new_area) - float(old_area))
    same_group = new_group == old_group
    same_day = new_day == old_day
    same_floor = old_floor is not None and new_floor == old_floor
    same_price = new_price == old_price
    if not (area_diff <= 5.0 or same_group or (int(same_day) + int(same_floor) + int(same_price) >= 2)):
        return None
    score = (
        (8 if old_area == new_area else 0)
        + (4 if same_group else 0)
        + (4 if same_day else 0)
        + (2 if same_floor else 0)
        + (2 if same_price else 0)
    )
    return (score, -area_diff, -abs(float(new_price) - float(old_price)), -abs((new_day - old_day).days))


def resolve_decisions(decisions, known_ids):
    """Follow source -> target chains to their terminal target; flag cycles and unknown endpoints."""
    valid_aliases = {}
    self_confirmed = set()
    invalid_targets = {}
    for src in decisions:
        visited = set()
        curr = src
        while True:
            if curr in visited:
                invalid_targets[src] = curr
                break
            if curr not in decisions:
                if curr in known_ids:
                    valid_aliases[src] = curr
                else:
                    invalid_targets[src] = curr
                break
            nxt = decisions[curr]
            if nxt == curr:
                if curr == src:
                    self_confirmed.add(src)
                elif curr in known_ids:
                    valid_aliases[src] = curr
                else:
                    invalid_targets[src] = curr
                break
            visited.add(curr)
            curr = nxt
    return valid_aliases, self_confirmed, invalid_targets


def pair_revisions(pending_rows, additions, aliases=None):
    """Pair unapproved missing trades 1:1 with plausible active replacement rows (including across aliased complexes)."""
    aliases = aliases or {}
    by_cid_missing = defaultdict(list)
    for tid, key in pending_rows:
        effective_cid = aliases.get(key[0], key[0])
        by_cid_missing[effective_cid].append((tid, key))
    remaining = Counter(additions)
    held_revisions = []
    for cid, missing_list in by_cid_missing.items():
        new_slots = []
        for new_key, count in list(remaining.items()):
            if new_key[0] == cid and not new_key[-1] and count > 0:
                for slot_idx in range(count):
                    new_slots.append((new_key, slot_idx))
        if not new_slots:
            continue
        scored = []
        for tid, old_key in missing_list:
            for idx, (new_key, _) in enumerate(new_slots):
                rank = _revision_score(old_key, new_key)
                if rank is not None:
                    scored.append((rank, tid, idx, new_key))
        scored.sort(key=lambda item: item[0], reverse=True)
        used_tids = set()
        used_slots = set()
        for _, tid, idx, new_key in scored:
            if tid in used_tids or idx in used_slots:
                continue
            used_tids.add(tid)
            used_slots.add(idx)
            remaining[new_key] -= 1
            if remaining[new_key] <= 0:
                del remaining[new_key]
            held_revisions.append((tid, new_key))
    return remaining, held_revisions


def _conflicts_with_demolition(target_cid, contract_date, lifecycle_state):
    """Check whether an active trade conflicts with a confirmed demolition effective date on target_cid."""
    info = lifecycle_state.get(target_cid)
    if not info:
        return False
    existence, _as_of, eff_date, eff_prec = info
    if existence != "demolished" or eff_date is None:
        return False
    if eff_prec == "day":
        return contract_date >= eff_date
    if eff_prec == "month":
        return (contract_date.year, contract_date.month) >= (eff_date.year, eff_date.month)
    if eff_prec == "year":
        return contract_date.year >= eff_date.year
    return False


def _match_period_decision(rules, contract_date, row_built):
    """Match a single unambiguous period decision for (contract_date, row_built).

    When multiple distinct targets cover the same date (coexisting old and new trades),
    an explicit buildYear discriminator match is required.
    """
    in_range = [
        r for r in rules
        if (r["valid_from"] is None or contract_date >= r["valid_from"])
        and (r["valid_to"] is None or contract_date <= r["valid_to"])
    ]
    if not in_range:
        return None
    distinct_targets = {r["target_id"] for r in in_range}
    exact_built = [r for r in in_range if r["built"] is not None and r["built"] == row_built]
    if len(distinct_targets) > 1:
        if len(exact_built) == 1:
            return exact_built[0]
        return None
    if len(exact_built) == 1:
        return exact_built[0]
    if len(exact_built) > 1:
        return None
    wildcard = [r for r in in_range if r["built"] is None]
    if len(wildcard) == 1:
        return wildcard[0]
    return None


def reconcile(db, month, complexes, wanted, district, *, history=False, refresh_day=None):
    first = month.start_time.date()
    end = (month + 1).start_time.date()
    raw_has_trades = bool(wanted)
    district_name = DISTRICTS[district]

    raw_by_built = getattr(wanted, "by_built", None)
    raw_build_years = getattr(wanted, "build_years", None) or {}

    with db.cursor() as cur:
        cur.execute("""
            SELECT complex_id, name, address, built FROM complexes
            WHERE split_part(complex_id,' ',2)=%s
        """, (district_name,))
        known_ids = set()
        known_info = {}
        current_names = defaultdict(list)
        current_parcels = defaultdict(list)
        current_by_lot = defaultdict(list)

        for cid, name, address, built in cur:
            known_ids.add(cid)
            known_info[cid] = (name, address, built)
            current_names[name, address, built].append(cid)
            current_parcels[address_key(address), built].append(cid)
            current_by_lot[address_key(address)].append(cid)

        aliases = {}
        held = {}
        held_rows_by_source = defaultdict(list)
        decisions = {}
        period_rules_by_source = defaultdict(list)
        lifecycle_state = {}
        candidate_by_lot = defaultdict(list)
        corrected_build_years = defaultdict(set)

        if refresh_day is not None:
            cur.execute("SELECT source_id,target_id FROM refresh_identity_decisions WHERE district=%s", (district,))
            decisions = dict(cur.fetchall())
            cur.execute("""
                SELECT complex_id, before_values ->> 'built'
                FROM complex_metadata_evidence
                WHERE split_part(complex_id,' ',2) = %s AND before_values ->> 'built' IS NOT NULL
            """, (district_name,))
            for cid, old_built in cur.fetchall():
                if old_built.isascii() and old_built.isdecimal():
                    corrected_build_years[cid].add(int(old_built))
            cur.execute("""
                SELECT source_id, built, valid_from, valid_to, target_id, target_name, target_address, target_built
                FROM refresh_identity_period_decisions
                WHERE district=%s
                ORDER BY decision_id
            """, (district,))
            for src_id, b_yr, v_from, v_to, tgt_id, tgt_name, tgt_addr, tgt_built in cur.fetchall():
                period_rules_by_source[src_id].append({
                    "source_id": src_id,
                    "built": b_yr,
                    "valid_from": v_from,
                    "valid_to": v_to,
                    "target_id": tgt_id,
                    "target_name": tgt_name,
                    "target_address": tgt_addr,
                    "target_built": tgt_built,
                })
            cur.execute("""
                SELECT complex_id, existence, as_of, effective_date, effective_precision
                FROM complex_lifecycle_state
                WHERE split_part(complex_id,' ',2)=%s
            """, (district_name,))
            for l_cid, l_ext, l_as_of, l_eff_date, l_eff_prec in cur.fetchall():
                lifecycle_state[l_cid] = (l_ext, l_as_of, l_eff_date, l_eff_prec)
            cur.execute("""
                SELECT complex_id, field, expected_value, proposed_value
                FROM complex_metadata_candidates
                WHERE district=%s AND status IN ('pending','held')
            """, (district,))
            for c_cid, c_field, c_exp, c_prop in cur.fetchall():
                if c_cid:
                    if c_cid in known_info:
                        candidate_by_lot[address_key(known_info[c_cid][1])].append(c_cid)
                    if c_field in ("address", "address+built") and c_prop:
                        prop_addr = c_prop.split("|")[0]
                        candidate_by_lot[address_key(prop_addr)].append(c_cid)
            cur.execute("""
                UPDATE refresh_identity_pending SET status='absent'
                WHERE district=%s AND month=%s AND status IN ('pending','invalid_decision')
            """, (district, first))

        valid_aliases, self_confirmed, invalid_targets = (
            resolve_decisions(decisions, known_ids) if refresh_day is not None else ({}, set(), {})
        )

        # Removed complexes are never registered again: drop their rows (and rows of unregistered
        # aliases that lead to them) before any identity matching.
        cur.execute("SELECT complex_id FROM complex_exclusions WHERE split_part(complex_id,' ',2)=%s",
                    (district_name,))
        excluded_ids = {row[0] for row in cur.fetchall()}
        excluded_rows = 0
        if excluded_ids:
            dropped = {
                source for source in set(complexes) | {row[0] for row in wanted}
                if source in excluded_ids
                or (source not in known_ids and invalid_targets.get(source) in excluded_ids)
            }
            if dropped:
                excluded_rows = sum(count for row, count in wanted.items() if row[0] in dropped)
                complexes = {key: value for key, value in complexes.items() if value[0] not in dropped}
                wanted = Counter({row: count for row, count in wanted.items() if row[0] not in dropped})
                if raw_by_built:
                    raw_by_built = Counter({row: count for row, count in raw_by_built.items()
                                            if row[0] not in dropped})
                raw_build_years = {key: value for key, value in raw_build_years.items() if key not in dropped}

        incoming = defaultdict(list)
        incoming_by_lot = defaultdict(list)
        for cid, name, address, built in complexes.values():
            incoming[address_key(address), built].append(cid)
            incoming_by_lot[address_key(address)].append(cid)

        # Expand row-level trades with buildYear so period decisions and multi-year conflicts
        # can be evaluated per row before collapsing.
        expanded_rows = Counter()
        if raw_by_built:
            for row8, count in raw_by_built.items():
                expanded_rows[row8] += count
        else:
            for row_t, count in wanted.items():
                if len(row_t) == 8:
                    expanded_rows[row_t] += count
                else:
                    src_cid = row_t[0]
                    default_built = complexes[src_cid][3] if src_cid in complexes else (
                        known_info[src_cid][2] if src_cid in known_info else 0
                    )
                    expanded_rows[(*row_t, default_built)] += count

        years_by_source = defaultdict(set)
        for (src_cid, _grp, _day, _area, _flr, _prc, _canc, row_built), cnt in expanded_rows.items():
            if cnt > 0:
                years_by_source[src_cid].add(row_built)
        for src_cid, yr_counter in raw_build_years.items():
            for y, cnt in yr_counter.items():
                if cnt > 0:
                    years_by_source[src_cid].add(y)

        invalid_decisions = {}
        period_remapped_wanted = Counter()
        period_handled_sources = set()
        resolved_sources = set()
        new_complexes_to_insert = {}

        for cid, name, address, built in list(complexes.values()):
            lot_key = address_key(address)
            parcel = lot_key, built
            lot_candidates = sorted(
                set(current_by_lot.get(lot_key, []) + incoming_by_lot.get(lot_key, []) + candidate_by_lot.get(lot_key, []))
                - {cid}
            )
            same_year_candidates = sorted(set(current_parcels.get(parcel, []) + incoming[parcel]) - {cid})
            all_candidates = sorted(set(lot_candidates) | set(same_year_candidates) | ({cid} if cid in known_ids else set()))

            # 1) Period-based rebuild / split mappings take precedence over known_ids and global aliases.
            if refresh_day is not None and cid in period_rules_by_source:
                period_handled_sources.add(cid)
                rules = period_rules_by_source[cid]
                src_rows = [
                    (row8, cnt) for row8, cnt in expanded_rows.items()
                    if row8[0] == cid and cnt > 0
                ]
                any_resolved = False
                for (src_cid, grp, day, area, flr, prc, canc, row_built), cnt in src_rows:
                    matched_rule = _match_period_decision(rules, day, row_built)
                    if matched_rule is None:
                        held_rows_by_source[cid].append([grp, day, area, flr, prc, canc, cnt, row_built])
                        continue
                    target_cid = matched_rule["target_id"]
                    if excluded_ids & {target_cid, valid_aliases.get(target_cid), invalid_targets.get(target_cid)}:
                        excluded_rows += cnt
                        any_resolved = True
                        continue
                    if target_cid in invalid_targets or len(target_cid.split(" ")) < 2 or target_cid.split(" ")[1] != district_name:
                        invalid_decisions[cid] = target_cid
                        held_rows_by_source[cid].append([grp, day, area, flr, prc, canc, cnt, row_built])
                        continue
                    target_cid = valid_aliases.get(target_cid, target_cid)
                    if not canc and _conflicts_with_demolition(target_cid, day, lifecycle_state):
                        held_rows_by_source[cid].append([grp, day, area, flr, prc, canc, cnt, row_built])
                        continue
                    if target_cid not in known_ids:
                        t_name = matched_rule["target_name"] or name
                        t_addr = matched_rule["target_address"] or address
                        t_built = matched_rule["target_built"] or row_built
                        new_complexes_to_insert[target_cid] = (target_cid, t_name, t_addr, t_built)
                    period_remapped_wanted[(target_cid, grp, day, area, flr, prc, canc)] += cnt
                    any_resolved = True

                if held_rows_by_source.get(cid):
                    held[cid] = sorted(
                        set(all_candidates) | {r["target_id"] for r in rules if r["target_id"] in known_ids}
                    )
                elif any_resolved or not src_rows:
                    resolved_sources.add(cid)
                continue

            # 2) Check multi-year buildYear conflict in the same month before fast paths.
            if refresh_day is not None and len(years_by_source.get(cid, ())) > 1:
                held[cid] = all_candidates
                continue

            # 3) Explicit general identity decision (refresh_identity_decisions).
            if cid in decisions:
                if cid in self_confirmed:
                    # If cid is already registered with a DIFFERENT build year, self-mapping cannot
                    # create a second row under the same PK; hold for period/suffix split decision.
                    if cid in known_ids and known_info[cid][2] != built and built not in corrected_build_years[cid]:
                        held[cid] = all_candidates
                        continue
                    if cid in known_ids and any(
                        not r8[6] and _conflicts_with_demolition(cid, r8[2], lifecycle_state)
                        for r8, cnt in expanded_rows.items() if r8[0] == cid and cnt > 0
                    ):
                        held[cid] = all_candidates
                        continue
                    resolved_sources.add(cid)
                    if cid not in known_ids:
                        new_complexes_to_insert[cid] = (cid, name, address, built)
                    continue
                if cid in valid_aliases:
                    target_cid = valid_aliases[cid]
                    if (known_info[target_cid][2] != built and built not in corrected_build_years[target_cid]) or any(
                        not r8[6] and _conflicts_with_demolition(target_cid, r8[2], lifecycle_state)
                        for r8, cnt in expanded_rows.items() if r8[0] == cid and cnt > 0
                    ):
                        held[cid] = sorted(set(all_candidates) | {target_cid})
                        continue
                    aliases[cid] = target_cid
                    resolved_sources.add(cid)
                    continue
                # The operator marked this source as an existing complex, but the terminal target is unknown or cyclic.
                # Never alias to it and never register the source as new: hold it for review instead.
                invalid_decisions[cid] = invalid_targets.get(cid, decisions[cid])
                held[cid] = same_year_candidates or lot_candidates
                continue

            # 4) Existing complex_id fast path: check buildYear mismatch, same-lot rebuild ambiguity,
            #    and confirmed demolition effective_date conflicts first!
            if cid in known_ids:
                if refresh_day is not None:
                    existing_built = known_info[cid][2]
                    other_lot_cids = [c for c in current_by_lot.get(lot_key, []) if c != cid]
                    has_demolition_conflict = any(
                        not r8[6] and _conflicts_with_demolition(cid, r8[2], lifecycle_state)
                        for r8, cnt in expanded_rows.items() if r8[0] == cid and cnt > 0
                    )
                    if (existing_built != built and built not in corrected_build_years[cid]) or has_demolition_conflict or (
                        other_lot_cids and any(known_info[c][2] == built for c in other_lot_cids)
                    ):
                        held[cid] = all_candidates
                        continue
                resolved_sources.add(cid)
                continue

            # 5) Unregistered source_id: check exact (name, address, built) match when unambiguous on the lot.
            matches = current_names.get((name, address, built), [])
            if len(matches) == 1 and matches[0] not in invalid_targets:
                target_cid = valid_aliases.get(matches[0], matches[0])
                has_demolition_conflict = refresh_day is not None and any(
                    not r8[6] and _conflicts_with_demolition(target_cid, r8[2], lifecycle_state)
                    for r8, cnt in expanded_rows.items() if r8[0] == cid and cnt > 0
                )
                if known_info[target_cid][2] == built and not has_demolition_conflict:
                    aliases[cid] = target_cid
                    resolved_sources.add(cid)
                    continue

            # 6) Check same-parcel / same-year / metadata-candidate conflicts before registering as new.
            candidates = same_year_candidates or lot_candidates
            if refresh_day is not None and (candidates or len(incoming_by_lot[lot_key]) > 1):
                held[cid] = candidates
                continue

            resolved_sources.add(cid)
            new_complexes_to_insert[cid] = (cid, name, address, built)

        effective_aliases = {**valid_aliases, **aliases}
        held_candidates = ({cand for cands in held.values() for cand in cands} | set(held)) & known_ids
        if held:
            raw_rows = defaultdict(list)
            for src_cid, rows_list in held_rows_by_source.items():
                if src_cid in held:
                    raw_rows[src_cid].extend(rows_list)
            for (*trade, row_built), count in expanded_rows.items():
                if trade[0] in held and trade[0] not in period_handled_sources:
                    raw_rows[trade[0]].append([*trade[1:], count, row_built])
            cur.executemany("""
                INSERT INTO refresh_identity_pending(district,month,source_id,candidates,source_rows)
                VALUES (%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (district,month,source_id) DO UPDATE
                SET candidates=EXCLUDED.candidates,source_rows=EXCLUDED.source_rows,status='pending'
            """, [(district, first, cid, candidates, json.dumps(raw_rows[cid], default=str))
                  for cid, candidates in held.items()])

        if invalid_decisions:
            # Excluded from actionable_months() until the decision points at a known complex.
            cur.execute("""
                UPDATE refresh_identity_pending SET status='invalid_decision'
                WHERE district=%s AND month=%s AND source_id=ANY(%s::text[])
            """, (district, first, list(invalid_decisions)))
            for cid, target in sorted(invalid_decisions.items()):
                print(f"{district} {month}: ignored invalid identity decision {cid} -> {target}; held for review",
                      flush=True)
        if refresh_day is not None and resolved_sources:
            cur.execute("""
                UPDATE refresh_identity_pending SET status='resolved'
                WHERE district=%s AND month=%s AND source_id=ANY(%s::text[]) AND status<>'resolved'
            """, (district, first, sorted(resolved_sources)))

        remapped_wanted = Counter(period_remapped_wanted)
        for trade, count in wanted.items():
            t7 = trade[:7]
            src_cid = t7[0]
            if src_cid in period_handled_sources or src_cid in held:
                continue
            remapped_wanted[(aliases.get(src_cid, src_cid), *t7[1:])] += count
        wanted = remapped_wanted
        complexes = new_complexes_to_insert

        insert_complex = """
            INSERT INTO complexes(complex_id,name,address,built) VALUES (%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """
        if refresh_day is None:
            cur.executemany(insert_complex, complexes.values())
        else:
            cur.executemany(f"""
                WITH inserted AS ({insert_complex} RETURNING complex_id),
                registered AS (
                    INSERT INTO complex_registrations(complex_id,refresh_day,district)
                    SELECT complex_id,%s,%s FROM inserted
                )
                INSERT INTO complex_geocode_pending(complex_id)
                SELECT complex_id FROM inserted
                ON CONFLICT DO NOTHING
            """, ((*row, refresh_day, district) for row in complexes.values()))
        areas = defaultdict(list)
        for cid, group, _, area, _, _, _ in wanted:
            areas[cid, group].append(float(area))
        cur.executemany("""
            INSERT INTO area_groups(complex_id, area_group, representative_area)
            VALUES (%s,%s,%s)
            ON CONFLICT (complex_id, area_group) DO UPDATE
            SET representative_area = EXCLUDED.representative_area
            WHERE NOT EXISTS (
                SELECT 1 FROM model_inputs m
                WHERE m.complex_id = EXCLUDED.complex_id AND m.area_group = EXCLUDED.area_group
            )
        """, [(cid, group, pd.Series(values).median()) for (cid, group), values in areas.items()])
        existing = defaultdict(list)
        cur.execute(f"SELECT trade_id,{FIELDS} FROM trades WHERE contract_date >= %s AND contract_date < %s AND split_part(complex_id,' ',2)=%s ORDER BY trade_id", (first, end, district_name))
        for trade_id, *row in cur:
            existing[tuple(row)].append(trade_id)
        if existing and not raw_has_trades:
            raise ValueError("Empty API month would remove existing trades")
        counts = Counter({k: len(ids) for k, ids in existing.items()})
        if history and (counts - wanted):
            raise ValueError(f"{district} {month}: existing trades differ from API; month rolled back")
        cancelled_removed = []
        approved_removed = []
        history_removed = []
        pending = []
        pending_rows = []
        reassigned_count = 0
        affected_complexes = set()
        if refresh_day is not None:
            def explicit_cancellations(key, absent):
                cancelled_key = (*key[:-1], True)
                return min(len(absent), max(0, wanted[cancelled_key] - counts[cancelled_key]))

            cur.execute("SELECT trade_id, approved FROM refresh_missing_trades WHERE district=%s AND month=%s",
                        (district, first))
            prior_missing = dict(cur.fetchall())
            approved = {tid for tid, ok in prior_missing.items() if ok}

            def resolve_existing_target(key):
                src_cid = key[0]
                if src_cid in held_candidates:
                    return None
                if src_cid in period_rules_by_source:
                    m_rule = _match_period_decision(
                        period_rules_by_source[src_cid], key[2],
                        known_info[src_cid][2] if src_cid in known_info else None,
                    )
                    if m_rule and m_rule["target_id"] not in invalid_targets:
                        tgt = valid_aliases.get(m_rule["target_id"], m_rule["target_id"])
                        return tgt if tgt != src_cid else None
                    return None
                tgt = effective_aliases.get(src_cid)
                return tgt if tgt and tgt != src_cid else None

            # Reassign existing trades in-place when an already-registered complex is aliased (A -> B)
            # and 1:1 correspondence with B's unmatched demand is unambiguous.
            candidates_by_target_key = defaultdict(list)
            for key, ids in list(existing.items()):
                target_cid = resolve_existing_target(key)
                if not target_cid:
                    continue
                target_key = (target_cid, *key[1:])
                for tid in ids[wanted[key]:]:
                    if tid not in approved:
                        candidates_by_target_key[target_key].append((key, tid))

            reassigned_by_target = defaultdict(list)
            for target_key, cand_list in candidates_by_target_key.items():
                needed = max(0, wanted[target_key] - counts[target_key])
                if 0 < len(cand_list) <= needed:
                    target_cid = target_key[0]
                    for src_key, tid in cand_list:
                        existing[src_key].remove(tid)
                        if not existing[src_key]:
                            del existing[src_key]
                        existing[target_key].append(tid)
                        counts[src_key] -= 1
                        if counts[src_key] <= 0:
                            del counts[src_key]
                        counts[target_key] += 1
                        reassigned_by_target[target_cid].append(tid)
                        reassigned_count += 1
                        affected_complexes.add(src_key[0])
                        affected_complexes.add(target_cid)
                    existing[target_key].sort()

            for target_cid, moved_tids in reassigned_by_target.items():
                cur.execute(
                    "UPDATE trades SET complex_id=%s WHERE trade_id = ANY(%s)",
                    (target_cid, moved_tids),
                )

            # Also handle explicit cancellations where the active row was stored under src_cid (A)
            # and the new cancellation row is now remapped under target_cid (B).
            cancel_cands_by_target = defaultdict(list)
            for key, ids in list(existing.items()):
                if key[-1]:
                    continue
                target_cid = resolve_existing_target(key)
                if not target_cid:
                    continue
                target_cancel_key = (target_cid, *key[1:-1], True)
                for tid in ids[wanted[key]:]:
                    if tid not in approved:
                        cancel_cands_by_target[target_cancel_key].append((key, tid))

            for target_cancel_key, cand_list in cancel_cands_by_target.items():
                target_valid_key = (*target_cancel_key[:-1], False)
                target_own_absent = max(0, len(existing.get(target_valid_key, [])) - wanted[target_valid_key])
                extra_cancel = max(0, wanted[target_cancel_key] - counts[target_cancel_key] - target_own_absent)
                if 0 < len(cand_list) <= extra_cancel:
                    for src_key, tid in cand_list:
                        existing[src_key].remove(tid)
                        if not existing[src_key]:
                            del existing[src_key]
                        cancelled_removed.append(tid)
                        affected_complexes.add(src_key[0])
                        affected_complexes.add(target_cancel_key[0])

            preserved_missing = []
            for key, ids in existing.items():
                if key[-1]:
                    continue
                if key[0] in held_candidates:
                    # Current exact matches invalidate old missing approvals. Explicit cancellations
                    # are reported under this complex's own key, so apply them while keeping
                    # ambiguous missing rows untouched.
                    absent = ids[wanted[key]:]
                    explicit = explicit_cancellations(key, absent)
                    cancelled_removed.extend(absent[:explicit])
                    preserved_missing.extend(tid for tid in absent[explicit:]
                                             if tid in prior_missing)
                    continue
                absent = ids[wanted[key]:]
                explicit = explicit_cancellations(key, absent)
                cancelled_removed.extend(absent[:explicit])
                for trade_id in absent[explicit:]:
                    if trade_id in approved:
                        approved_removed.append(trade_id)
                        affected_complexes.add(key[0])
                    else:
                        pending.append(trade_id)
                        pending_rows.append((trade_id, key))
            keep_missing = pending + preserved_missing
            cur.execute("""
                DELETE FROM refresh_missing_trades
                WHERE district=%s AND month=%s AND NOT (trade_id = ANY(%s::bigint[]))
            """, (district, first, keep_missing))
            cur.executemany("""
                INSERT INTO refresh_missing_trades(trade_id,district,month,first_seen,last_seen)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (trade_id) DO UPDATE SET last_seen=EXCLUDED.last_seen
            """, [(tid, district, first, refresh_day, refresh_day) for tid in pending])
        else:
            history_removed = [tid for k, ids in existing.items() if not k[-1] for tid in ids[wanted[k]:]]
        additions = wanted - counts
        held_revisions = []
        if refresh_day is not None:
            additions, held_revisions = pair_revisions(pending_rows, additions, aliases=effective_aliases)
            rev_ids = [tid for tid, _ in held_revisions]
            cur.execute("""
                DELETE FROM refresh_revision_pending
                WHERE district=%s AND month=%s AND NOT (trade_id = ANY(%s::bigint[]))
            """, (district, first, rev_ids))
            if held_revisions:
                cur.executemany("""
                    INSERT INTO refresh_revision_pending(
                        trade_id, district, month, complex_id, candidate_row, first_seen, last_seen
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT (trade_id) DO UPDATE
                    SET candidate_row=EXCLUDED.candidate_row, last_seen=EXCLUDED.last_seen
                """, [
                    (tid, district, first, new_key[0], json.dumps(list(new_key[1:]), default=str), refresh_day, refresh_day)
                    for tid, new_key in held_revisions
                ])
            if held or pending or held_revisions:
                print(
                    f"{district} {month}: identity_pending={len(held)} "
                    f"missing_trades={len(pending)} revision_pending={len(held_revisions)}",
                    flush=True,
                )

        def archive_removed(ids, reason):
            if not ids:
                return
            # Preserve the prior reported row before replacing the active version.
            cur.execute(f"""
                WITH revised AS (
                    DELETE FROM trades WHERE trade_id = ANY(%s)
                    RETURNING trade_id,{FIELDS}
                )
                INSERT INTO trade_revisions(trade_id,{FIELDS},reason)
                SELECT trade_id,{FIELDS},%s FROM revised
            """, (ids, reason))

        archive_removed(cancelled_removed, "cancelled")
        archive_removed(approved_removed, "approved_missing")
        archive_removed(history_removed, "history_sync")
        for key in additions:
            affected_complexes.add(key[0])
        cur.executemany(f"INSERT INTO trades({FIELDS}) VALUES (%s,%s,%s,%s,%s,%s,%s)", additions.elements())
        total_added = sum(additions.values())
        total_removed = len(cancelled_removed) + len(approved_removed) + len(history_removed)
        if excluded_rows:
            print(f"{district} {month}: dropped {excluded_rows} trade rows of removed complexes", flush=True)
        return ReconcileResult(
            total_added,
            total_removed,
            reassigned=reassigned_count,
            affected_complexes=sorted(affected_complexes),
            excluded=excluded_rows,
        )


def publish(db, anchor, macro_path, district):
    first_period, last_period = input_month_range(anchor)
    cur = db.execute("""
        SELECT t.complex_id,t.area_group,t.contract_date,t.area,t.floor,t.price_manwon,c.built
        FROM trades t JOIN complexes c USING(complex_id)
        WHERE split_part(t.complex_id,' ',2)=%s AND NOT t.is_cancelled AND t.contract_date >= %s AND t.contract_date < %s
          AND NOT EXISTS (SELECT 1 FROM complex_training_exclusions x
                          WHERE x.complex_id=t.complex_id AND t.contract_date >= x.exclude_from)
    """, (DISTRICTS[district], first_period.start_time.date(), (last_period + 1).start_time.date()))
    frame = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    inputs = build_inputs(frame, anchor, macro_path)
    with db.cursor() as cur:
        cur.executemany("UPDATE area_groups SET representative_area=%s WHERE complex_id=%s AND area_group=%s",
                        inputs[["representative_area","complex_id","area_group"]].itertuples(index=False,name=None))
        cur.execute("ALTER TABLE model_inputs ADD COLUMN IF NOT EXISTS indexed_log_current double precision")
        columns = inputs.drop(columns="representative_area")
        clean = columns.astype(object).where(columns.notna(), None)
        cur.execute("DELETE FROM model_inputs WHERE month=%s AND split_part(complex_id,' ',2)=%s", (str(anchor),DISTRICTS[district]))
        cur.executemany("INSERT INTO model_inputs ("+",".join(columns.columns)+") VALUES ("+
                        ",".join(["%s"]*len(columns.columns))+")", clean.itertuples(index=False,name=None))
    return len(inputs)


APPROVAL_BACKOFF_DAYS = (1, 3, 7, 14)
APPROVAL_BLOCKED_AFTER = 3
SAFE_REPLAY_REASONS = (
    "Empty API month would remove existing trades",
    "Invalid reviewed complex ID for district",
)


def actionable_months(db, district, today=None):
    """Return contract months with operator-approved removals or identity decisions awaiting replay.

    Identity decisions count only when their full chain terminates at a valid complex of this district
    (or confirms the source as its own complex) without cycles. With ``today``, months still backing off
    after a failed replay are left out.
    """
    rows = db.execute("""
        SELECT month FROM (
            WITH RECURSIVE decision_walk(source_id, target_id, terminal_self, visited) AS (
                SELECT d.source_id, d.target_id, (d.source_id = d.target_id), ARRAY[d.source_id]
                FROM refresh_identity_decisions d
                WHERE d.district = %s
                UNION ALL
                SELECT w.source_id, d.target_id, (d.source_id = d.target_id), w.visited || d.source_id
                FROM decision_walk w
                JOIN refresh_identity_decisions d
                  ON d.district = %s AND d.source_id = w.target_id
                WHERE NOT w.terminal_self AND NOT (d.source_id = ANY(w.visited))
            ),
            valid_decisions AS (
                SELECT w.source_id, w.target_id
                FROM decision_walk w
                LEFT JOIN refresh_identity_decisions next_d
                  ON next_d.district = %s AND next_d.source_id = w.target_id
                WHERE w.terminal_self
                   OR (next_d.source_id IS NULL AND EXISTS (
                       SELECT 1 FROM complexes c
                       WHERE c.complex_id = w.target_id AND split_part(c.complex_id, ' ', 2) = %s
                   ))
            )
            SELECT DISTINCT r.month
            FROM refresh_missing_trades r JOIN trades t USING (trade_id)
            WHERE r.district=%s AND r.approved
              AND NOT EXISTS (
                  SELECT 1 FROM refresh_identity_pending p
                  WHERE p.district=r.district AND p.month=r.month
                    AND p.status IN ('pending','invalid_decision')
                    AND (t.complex_id = p.source_id OR t.complex_id = ANY(p.candidates))
              )
            UNION
            SELECT DISTINCT p.month
            FROM refresh_identity_pending p
            LEFT JOIN valid_decisions v
              ON v.source_id=p.source_id
            WHERE p.district=%s AND p.status IN ('pending','invalid_decision')
              AND (
                  v.source_id IS NOT NULL
                  OR EXISTS (
                      SELECT 1 FROM refresh_identity_period_decisions pd
                      WHERE pd.district = p.district AND pd.source_id = p.source_id
                        AND split_part(pd.target_id, ' ', 2) = split_part(p.source_id, ' ', 2)
                  )
              )
            UNION
            SELECT DISTINCT date_trunc('month', t.contract_date)::date AS month
            FROM trades t
            JOIN valid_decisions v ON v.source_id = t.complex_id
            WHERE v.target_id <> v.source_id
              AND NOT EXISTS (
                  SELECT 1 FROM refresh_missing_trades m
                  WHERE m.trade_id = t.trade_id AND NOT m.approved
              )
        ) months
        WHERE %s::date IS NULL OR NOT EXISTS (
            SELECT 1 FROM refresh_approval_failures f
            WHERE f.district=%s AND f.month=months.month AND f.next_retry_on > %s::date
        )
    """, (
        district, district, district, DISTRICTS[district],
        district, district, today, district, today,
    )).fetchall()
    return sorted(pd.Period(row[0], freq="M") for row in rows)


def replay_reason(exc):
    """Short failure label: known safety messages verbatim, otherwise only the exception type."""
    if isinstance(exc, ValueError) and str(exc) in SAFE_REPLAY_REASONS:
        return str(exc)
    return type(exc).__name__


def record_approval_failure(db, district, month, today, reason):
    """Back off a failed approved month (1, 3, 7, then every 14 days) and return its attempt count."""
    attempts = db.execute("""
        INSERT INTO refresh_approval_failures AS f
            (district, month, attempts, last_error, last_attempt_on, next_retry_on)
        VALUES (%s, %s, 1, %s, %s, %s::date + %s::int)
        ON CONFLICT (district, month) DO UPDATE
        SET attempts = CASE WHEN f.last_attempt_on < EXCLUDED.last_attempt_on
                            THEN f.attempts + 1 ELSE f.attempts END,
            last_error = CASE WHEN f.last_attempt_on <= EXCLUDED.last_attempt_on
                              THEN EXCLUDED.last_error ELSE f.last_error END,
            last_attempt_on = GREATEST(f.last_attempt_on, EXCLUDED.last_attempt_on),
            next_retry_on = CASE WHEN f.last_attempt_on < EXCLUDED.last_attempt_on
                                 THEN EXCLUDED.last_attempt_on
                                      + (%s::int[])[LEAST(f.attempts + 1, %s::int)]
                                 ELSE f.next_retry_on END
        RETURNING attempts
    """, (district, month.start_time.date(), reason, today, today, APPROVAL_BACKOFF_DAYS[0],
          list(APPROVAL_BACKOFF_DAYS), len(APPROVAL_BACKOFF_DAYS))).fetchone()[0]
    label = "APPROVAL BLOCKED" if attempts >= APPROVAL_BLOCKED_AFTER else "approved month deferred"
    print(f"{district} {month}: {label} ({reason}); attempt {attempts}, see refresh_approval_failures",
          flush=True)
    return attempts


def clear_approval_failure(db, district, month):
    db.execute("DELETE FROM refresh_approval_failures WHERE district=%s AND month=%s",
               (district, month.start_time.date()))


def replay_approved_month(db, month, district, today):
    """Replay one approved month inside a savepoint; on failure keep the outer transaction and back off.

    Returns (added, removed) on success, or None when the month was deferred.
    """
    try:
        with db.transaction():
            complexes, rows = fetch_month(month.strftime("%Y%m"), district)
            result = reconcile(db, month, complexes, rows, district, refresh_day=today)
    except ValueError as exc:
        if str(exc) not in SAFE_REPLAY_REASONS:
            raise
        record_approval_failure(db, district, month, today, replay_reason(exc))
        return None
    clear_approval_failure(db, district, month)
    return result


def refresh_slot_start(now):
    """Return the most recent 06:00 KST at or before now."""
    six = datetime.combine(now.date(), time(6), KST)
    return six if now >= six else six - timedelta(days=1)


def monthly_slot_start(now):
    """Return the 06:00 KST on the 1st of the active daily slot's month."""
    daily_slot = refresh_slot_start(now)
    slot_day = daily_slot.date()
    return datetime(slot_day.year, slot_day.month, 1, 6, 0, tzinfo=KST)


def next_monthly_slot_start(slot_start):
    """Return the 06:00 KST on the 1st of the month following slot_start."""
    if slot_start.month == 12:
        return datetime(slot_start.year + 1, 1, 1, 6, 0, tzinfo=KST)
    return datetime(slot_start.year, slot_start.month + 1, 1, 6, 0, tzinfo=KST)


def _apply_seongdong_verified_coordinate(db, today):
    """Apply Seongdong-gu representative coordinate through the verified metadata validation/update path."""
    row = db.execute("""
        SELECT complex_id FROM complexes
        WHERE address=%s AND name=%s AND (latitude IS NULL OR longitude IS NULL)
    """, (SEONGDONG_VERIFIED_RECORD["expected_address"], SEONGDONG_VERIFIED_RECORD["expected_name"])).fetchone()
    if not row:
        return False
    cid = row[0] if isinstance(row, tuple) else row["complex_id"]
    record = {**SEONGDONG_VERIFIED_RECORD, "complex_id": cid}
    doc = {"batch": SEONGDONG_VERIFIED_BATCH, "complexes": [record]}
    validate_metadata(doc, SEONGDONG_VERIFIED_BATCH, today.year)
    checksum = sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return apply_verified_complex_records(db, SEONGDONG_VERIFIED_BATCH, checksum, [record], today=today)


def refresh(district, temp, *, now=None):
    if not os.environ.get("MOLIT_SERVICE_KEY"):
        raise ValueError("MOLIT_SERVICE_KEY is required")
    ensure_schema()
    now = now or datetime.now(KST)
    slot_start = refresh_slot_start(now)
    today = slot_start.date()
    anchor = pd.Period(today, freq="M")
    lock_id = 1121505 if district == GWANGJIN else int(district)
    added = removed = 0
    in_window_reassigned = 0
    already_ran_slot = False
    replay_errors = []

    with psycopg.connect(connect_timeout=8) as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
        prev_row = db.execute(
            "SELECT max(day), max(COALESCE(slot_start, finished_at)) FROM district_refresh_runs WHERE district=%s",
            (district,),
        ).fetchone()
        previous, last_slot = prev_row if prev_row else (None, None)
        if district == GWANGJIN and previous is None:
            gw_row = db.execute(
                "SELECT max(day), max(COALESCE(slot_start, finished_at)) FROM refresh_runs"
            ).fetchone()
            previous, last_slot = gw_row if gw_row else (None, None)
        pending_approvals = actionable_months(db, district, today)
        already_ran_slot = last_slot is not None and last_slot >= slot_start

        def replay(months):
            """Replay approved months one savepoint each; failures are recorded and skipped."""
            nonlocal added, removed, in_window_reassigned
            replayed = []
            for month in months:
                try:
                    result = replay_approved_month(db, month, district, today)
                except Exception as exc:
                    replay_errors.append(exc)
                    continue
                if result is not None:
                    added += result[0]
                    removed += result[1]
                    if is_in_input_window(month, anchor):
                        in_window_reassigned += getattr(result, "reassigned", 0)
                    replayed.append(month)
            return replayed

        def enqueue_publish_pending(pending_added, pending_removed):
            db.execute("""
                INSERT INTO refresh_publish_pending VALUES (%s,%s,%s,%s)
                ON CONFLICT (district,day) DO UPDATE
                SET added=refresh_publish_pending.added + EXCLUDED.added,
                    removed=refresh_publish_pending.removed + EXCLUDED.removed
            """, (district, today, pending_added, pending_removed))

        if already_ran_slot:
            replayed = replay(pending_approvals)
            if replayed and (added or removed or in_window_reassigned):
                enqueue_publish_pending(added, removed)
                added = removed = in_window_reassigned = 0
            pending_publish = db.execute("""
                SELECT 1 FROM refresh_publish_pending WHERE district=%s AND day<=%s
            """, (district, today)).fetchone()
            db.commit()
            if not pending_publish:
                if replay_errors:
                    raise replay_errors[0]
                if replayed:
                    print(f"{district} {today}: replayed approvals added={added} removed={removed}", flush=True)
                return
        else:
            # Revisit the reporting window and catch up after downtime.
            start = min(anchor-3, pd.Period(previous, freq="M")-3) if previous else min(anchor-3, pd.Period("2026-10", freq="M"))
            if district != GWANGJIN and previous is None:
                start = anchor - 19
            # One-time migration for pre-upgrade checkpoints (slot_start IS NULL) whose publish had failed.
            legacy_cps = db.execute("""
                SELECT day, added, removed, created_at
                FROM daily_trade_checkpoints
                WHERE district = %s AND slot_start IS NULL
            """, (district,)).fetchall()
            for cp_day, cp_added, cp_removed, cp_created in legacy_cps:
                db.execute("""
                    UPDATE daily_trade_checkpoints
                    SET slot_start = created_at
                    WHERE district = %s AND day = %s AND slot_start IS NULL
                """, (district, cp_day))
                if not (cp_added or cp_removed):
                    continue
                already_published = db.execute("""
                    SELECT 1 FROM district_refresh_runs
                    WHERE district=%s AND day=%s AND finished_at >= %s
                """, (district, cp_day, cp_created)).fetchone()
                if not already_published:
                    db.execute("""
                        INSERT INTO refresh_publish_pending VALUES (%s,%s,%s,%s)
                        ON CONFLICT (district,day) DO UPDATE
                        SET added=refresh_publish_pending.added + EXCLUDED.added,
                            removed=refresh_publish_pending.removed + EXCLUDED.removed
                    """, (district, cp_day, cp_added, cp_removed))
            checkpoint = db.execute(
                "SELECT added,removed FROM daily_trade_checkpoints WHERE district=%s AND day=%s AND COALESCE(slot_start, created_at) >= %s",
                (district, today, slot_start),
            ).fetchone()
            if checkpoint and not pending_approvals:
                added, removed = checkpoint
            elif checkpoint:
                replayed = replay(pending_approvals)
                if replayed and (added or removed or in_window_reassigned):
                    enqueue_publish_pending(added, removed)
                added, removed = checkpoint
            else:
                # Resume from per-month checkpoints within the same slot so a late-month API error
                # never repeats earlier completed months.
                done_months = {
                    row[0]: (row[1], row[2], row[3])
                    for row in db.execute("""
                        SELECT month, added, removed, reassigned
                        FROM daily_trade_month_checkpoints
                        WHERE district = %s AND slot_start = %s
                    """, (district, slot_start)).fetchall()
                }
                window = set(pd.period_range(start, anchor))
                new_added = new_removed = new_reassigned = 0
                window_error = None
                for month in sorted(window):
                    m_date = month.start_time.date()
                    if m_date in done_months and month not in pending_approvals:
                        m_a, m_r, m_re = done_months[m_date]
                        added += m_a
                        removed += m_r
                        if is_in_input_window(month, anchor):
                            in_window_reassigned += m_re
                        continue
                    try:
                        with (db.transaction() if hasattr(db, "transaction") else nullcontext()):
                            complexes, rows = fetch_month(month.strftime("%Y%m"), district)
                            res = reconcile(db, month, complexes, rows, district, refresh_day=today)
                            a, r = res
                            m_re = getattr(res, "reassigned", 0)
                            clear_approval_failure(db, district, month)
                            db.execute("""
                                INSERT INTO daily_trade_month_checkpoints(
                                    district, slot_start, month, day, added, removed, reassigned
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (district, slot_start, month) DO UPDATE
                                SET added = EXCLUDED.added,
                                    removed = EXCLUDED.removed,
                                    reassigned = EXCLUDED.reassigned,
                                    day = EXCLUDED.day
                            """, (district, slot_start, m_date, today, a, r, m_re))
                    except Exception as exc:
                        window_error = exc
                        continue
                    added += a
                    removed += r
                    new_added += a
                    new_removed += r
                    if is_in_input_window(month, anchor):
                        in_window_reassigned += m_re
                        new_reassigned += m_re

                if window_error is not None:
                    if new_added or new_removed or new_reassigned:
                        enqueue_publish_pending(new_added, new_removed)
                    db.commit()
                    raise window_error

                # Older approved months are isolated so one bad approval cannot block the window.
                before_replay_a, before_replay_r, before_replay_re = added, removed, in_window_reassigned
                replay(sorted(set(pending_approvals) - window))
                new_added += added - before_replay_a
                new_removed += removed - before_replay_r
                new_reassigned += in_window_reassigned - before_replay_re

                db.execute("""
                    INSERT INTO daily_trade_checkpoints(district, day, added, removed, created_at, slot_start)
                    VALUES (%s,%s,%s,%s,clock_timestamp(),%s)
                    ON CONFLICT (district, day) DO UPDATE
                    SET added=EXCLUDED.added,
                        removed=EXCLUDED.removed,
                        created_at=EXCLUDED.created_at,
                        slot_start=EXCLUDED.slot_start
                """, (district, today, added, removed, slot_start))
                if new_added or new_removed or new_reassigned:
                    enqueue_publish_pending(new_added, new_removed)
            db.commit()

    # Run external macro fetch outside any DB transaction or advisory lock.
    subprocess.run(
        [sys.executable, str(ROOT/"fetch_macro.py"), "--district", district, "--end-month", str(anchor-1)],
        cwd=temp, check=True, timeout=180,
    )

    with psycopg.connect(connect_timeout=8) as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
        completed = already_ran_slot or db.execute(
            "SELECT 1 FROM district_refresh_runs WHERE district=%s AND COALESCE(slot_start, finished_at) >= %s",
            (district, slot_start),
        ).fetchone()
        pending = db.execute("""
            SELECT day,added,removed FROM refresh_publish_pending
            WHERE district=%s AND day<=%s
        """, (district, today)).fetchall()
        if completed and not pending:
            if replay_errors:
                raise replay_errors[0]
            return
        today_added = today_removed = 0
        prior_days = []
        for pending_day, pending_added, pending_removed in pending:
            if pending_day == today:
                today_added += pending_added
                today_removed += pending_removed
            else:
                prior_days.append((pending_day, pending_added, pending_removed))
        n = publish(db, anchor, Path(temp)/"macro_raw.csv", district)
        if district == "11200":
            _apply_seongdong_verified_coordinate(db, today)
        for prior_day, prior_added, prior_removed in prior_days:
            prior_slot = datetime.combine(prior_day, time(6), KST)
            db.execute("""
                INSERT INTO district_refresh_runs(district, day, finished_at, added, removed, forecast_groups, slot_start)
                VALUES (%s,%s,clock_timestamp(),%s,%s,%s,%s)
                ON CONFLICT (district, day) DO UPDATE
                SET added=district_refresh_runs.added + EXCLUDED.added,
                    removed=district_refresh_runs.removed + EXCLUDED.removed
            """, (district, prior_day, prior_added, prior_removed, n, prior_slot))
            if district == GWANGJIN:
                db.execute("""
                    INSERT INTO refresh_runs(day, finished_at, added, removed, forecast_groups, slot_start)
                    VALUES (%s,clock_timestamp(),%s,%s,%s,%s)
                    ON CONFLICT (day) DO UPDATE
                    SET added=refresh_runs.added + EXCLUDED.added,
                        removed=refresh_runs.removed + EXCLUDED.removed
                """, (prior_day, prior_added, prior_removed, n, prior_slot))
        if not completed or any(d == today for d, _, _ in pending):
            db.execute("""
                INSERT INTO district_refresh_runs(district, day, finished_at, added, removed, forecast_groups, slot_start)
                VALUES (%s,%s,clock_timestamp(),%s,%s,%s,%s)
                ON CONFLICT (district, day) DO UPDATE
                SET finished_at=clock_timestamp(),
                    slot_start=GREATEST(COALESCE(district_refresh_runs.slot_start, EXCLUDED.slot_start), EXCLUDED.slot_start),
                    added=district_refresh_runs.added + EXCLUDED.added,
                    removed=district_refresh_runs.removed + EXCLUDED.removed,
                    forecast_groups=EXCLUDED.forecast_groups
            """, (district, today, today_added, today_removed, n, slot_start))
            if district == GWANGJIN:
                db.execute("""
                    INSERT INTO refresh_runs(day, finished_at, added, removed, forecast_groups, slot_start)
                    VALUES (%s,clock_timestamp(),%s,%s,%s,%s)
                    ON CONFLICT (day) DO UPDATE
                    SET finished_at=clock_timestamp(),
                        slot_start=GREATEST(COALESCE(refresh_runs.slot_start, EXCLUDED.slot_start), EXCLUDED.slot_start),
                        added=refresh_runs.added + EXCLUDED.added,
                        removed=refresh_runs.removed + EXCLUDED.removed,
                        forecast_groups=EXCLUDED.forecast_groups
                """, (today, today_added, today_removed, n, slot_start))
        db.execute("DELETE FROM refresh_publish_pending WHERE district=%s AND day<=%s", (district, today))
    print(f"{district} {today}: added={today_added} removed={today_removed} forecast_groups={n}", flush=True)
    if replay_errors:
        raise replay_errors[0]


def recheck_history(district, *, today=None):
    """Revisit one older contract month per district per day; retry transient errors on the same pinned month."""
    ensure_schema()
    today = today or datetime.now(KST).date()
    anchor = pd.Period(today, freq="M")
    cutoff = (anchor - 4).start_time.date()
    lock_id = 1121505 if district == GWANGJIN else int(district)
    target_date = None
    needs_republish = False
    with psycopg.connect(connect_timeout=8) as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
        # One completed recheck per district per day, approved months included.
        if db.execute("SELECT 1 FROM historical_rechecks WHERE district=%s AND checked_on=%s AND status='ok'",
                      (district, today)).fetchone():
            return False
        # If an earlier attempt today failed with a transient error, retry that same pinned target month.
        pinned = db.execute("""
            SELECT h.month FROM historical_rechecks h
            WHERE h.district=%s AND h.checked_on=%s AND h.status='failed'
              AND NOT EXISTS (
                  SELECT 1 FROM refresh_approval_failures f
                  WHERE f.district=h.district AND f.month=h.month AND f.next_retry_on > %s::date
              )
            ORDER BY h.month LIMIT 1
        """, (district, today, today)).fetchone()
        pending_old = [m.start_time.date() for m in actionable_months(db, district, today)
                       if m.start_time.date() <= cutoff]
        if pinned:
            target_date = pinned[0]
            is_approval = target_date in pending_old
        elif pending_old:
            target_date = pending_old[0]
            is_approval = True
        else:
            is_approval = False
            row = db.execute("""
                SELECT m::date FROM generate_series(date '2006-01-01', %s::date, interval '1 month') AS months(m)
                LEFT JOIN historical_rechecks h ON h.district=%s AND h.month=m::date
                WHERE NOT EXISTS (
                    SELECT 1 FROM refresh_approval_failures f
                    WHERE f.district=%s AND f.month=m::date AND f.next_retry_on > %s::date
                )
                ORDER BY
                    COALESCE(h.status = 'failed' AND h.checked_on = %s::date - 1
                             AND h.last_error <> 'ValueError', FALSE) DESC,
                    (h.checked_on IS NULL) DESC,
                    (h.status = 'failed' AND h.checked_on <= %s::date - 7) DESC,
                    h.checked_on ASC NULLS FIRST,
                    m DESC
                LIMIT 1
            """, (cutoff, district, district, today, today, today)).fetchone()
            target_date = row[0] if row else None
        if not target_date:
            return False
        month = pd.Period(target_date, freq="M")
        try:
            with db.transaction():
                complexes, rows = fetch_month(month.strftime("%Y%m"), district)
                res = reconcile(db, month, complexes, rows, district, refresh_day=today)
                a, r = res
                reassigned = getattr(res, "reassigned", 0)
        except Exception as exc:
            reason = type(exc).__name__
            db.execute("""
                INSERT INTO historical_rechecks(district, month, checked_on, status, last_error)
                VALUES (%s, %s, %s, 'failed', %s)
                ON CONFLICT (district, month) DO UPDATE
                SET checked_on=EXCLUDED.checked_on, status='failed', last_error=EXCLUDED.last_error
            """, (district, target_date, today, reason))
            review_required = isinstance(exc, ValueError) and str(exc) in SAFE_REPLAY_REASONS
            if is_approval and review_required:
                record_approval_failure(db, district, month, today, replay_reason(exc))
            print(f"{district} historical {month}: deferred ({reason})", flush=True)
            if not review_required:
                db.commit()
                raise
        else:
            db.execute("""
                INSERT INTO historical_rechecks(district, month, checked_on, status, last_error)
                VALUES (%s, %s, %s, 'ok', NULL)
                ON CONFLICT (district, month) DO UPDATE
                SET checked_on=EXCLUDED.checked_on, status='ok', last_error=NULL
            """, (district, target_date, today))
            clear_approval_failure(db, district, month)
            if (a + r + reassigned) > 0 and is_in_input_window(month, anchor):
                db.execute("""
                    INSERT INTO refresh_publish_pending VALUES (%s,%s,%s,%s)
                    ON CONFLICT (district,day) DO UPDATE
                    SET added=refresh_publish_pending.added + EXCLUDED.added,
                        removed=refresh_publish_pending.removed + EXCLUDED.removed
                """, (district, today, a, r))
                needs_republish = True
            print(f"{district} historical {month}: added={a} removed={r}", flush=True)
    return needs_republish


def refresh_many(districts, *, now=None):
    failed = []
    with tempfile.TemporaryDirectory() as temp:
        for district in districts:
            try:
                refresh(district, temp, now=now)
            except Exception as exc:
                failed.append(district)
                print(f"{district} update failed ({type(exc).__name__}): check source availability and credentials.", flush=True)
                continue
            try:
                needs_republish = recheck_history(
                    district,
                    today=refresh_slot_start(now).date() if now is not None else None,
                )
            except Exception as exc:
                failed.append(district)
                print(f"{district} historical recheck failed ({type(exc).__name__}); daily update retained.", flush=True)
                continue
            if needs_republish:
                try:
                    refresh(district, temp, now=now)
                except Exception as exc:
                    failed.append(district)
                    print(f"{district} republish after historical recheck deferred ({type(exc).__name__}).", flush=True)
    return failed


def run_lifecycle_month(today):
    """The monthly existence check when JIPFIT_LIFECYCLE_AUTO is 1; failures join the monthly retry."""
    if os.environ.get("JIPFIT_LIFECYCLE_AUTO") != "1":
        return []
    try:
        return run_month(today)
    except Exception as exc:
        print(f"Lifecycle month deferred ({type(exc).__name__}).", flush=True)
        return [f"lifecycle:{type(exc).__name__}"]


def apply_lifecycle_inbox(today):
    """Apply unrecorded lifecycle bundles when JIPFIT_LIFECYCLE_DIR is set; failures join the daily retry."""
    root = os.environ.get("JIPFIT_LIFECYCLE_DIR")
    if not root:
        return []
    try:
        return [f"lifecycle:{batch}" for batch in apply_inbox(root, today)]
    except Exception as exc:
        print(f"Lifecycle inbox deferred ({type(exc).__name__}).", flush=True)
        return [f"lifecycle-inbox:{type(exc).__name__}"]


def seed_facilities():
    """Fill empty facility tables when the updater starts; a failure never stops the refresh schedule."""
    try:
        with psycopg.connect(connect_timeout=8) as db:
            ensure_facilities(db)
    except Exception as exc:
        print(f"Facility seed deferred ({type(exc).__name__}).", flush=True)


def refresh_facility_sources(today):
    """Monthly public facility catalogs; failed sources join the monthly retry."""
    try:
        return [f"facilities:{name}" for name in refresh_sources(today)]
    except Exception as exc:
        print(f"Facility refresh deferred ({type(exc).__name__}).", flush=True)
        return [f"facilities:{type(exc).__name__}"]


def sync_facilities():
    """Recompute every complex's facilities after the day's changes; a failure joins the daily retry."""
    try:
        with psycopg.connect(connect_timeout=8) as db:
            # A seed that failed when the updater started is loaded here; filled tables are only counted.
            ensure_facilities(db, compute=False)
            sync_complex_facilities(db)
        return []
    except Exception as exc:
        print(f"Facility sync deferred ({type(exc).__name__}).", flush=True)
        return ["facilities:sync"]


def seconds_until_refresh(now=None):
    now = now or datetime.now(KST)
    next_run = datetime.combine(now.date(), time(6), KST)
    if now >= next_run:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


class MemoryAttemptStore:
    """Thread-safe in-memory attempt store used for tests and as a local cooldown tracker during DB outages."""

    def __init__(self, *, retention_days=65):
        self._lock = Lock()
        self._retention_days = retention_days
        # (worker, slot_start) -> (attempts, status, next_attempt_at, lease_until, claim_token)
        self._rows = {}

    def _prune(self, now, retention_days=None):
        days = retention_days if retention_days is not None else self._retention_days
        cutoff = now - timedelta(days=days)
        self._rows = {k: v for k, v in self._rows.items() if k[1] >= cutoff}

    def peek(self, worker, slot_start, now):
        with self._lock:
            self._prune(now)
            return self._rows.get((worker, slot_start))

    def sync(self, worker, slot_start, attempts, status, next_attempt_at, lease_until, claim_token=None):
        with self._lock:
            self._rows[(worker, slot_start)] = (attempts, status, next_attempt_at, lease_until, claim_token)

    def claim_attempt(
        self, worker, slot_start, now, *,
        max_attempts, retry_delay=600, lease_seconds=7200, next_slot=None, retention_days=None,
    ):
        next_slot = next_slot or (slot_start + timedelta(days=1))
        remaining = max(1, (next_slot - now).total_seconds())
        with self._lock:
            self._prune(now, retention_days=retention_days)
            key = (worker, slot_start)
            prev = self._rows.get(key)
            if prev:
                attempts, status, next_attempt_at, lease_until, *rest = prev
                cur_token = rest[0] if rest else None
                # Normalize legacy rows that stored retry cooldowns under status='running'.
                if status == "running" and next_attempt_at is not None:
                    status = "deferred"
                    lease_until = None
                    cur_token = None
                    self._rows[key] = (attempts, status, next_attempt_at, None, None)
                if status in ("done", "exhausted"):
                    return ("skip", remaining)
                if status == "running" and lease_until is not None and lease_until > now:
                    return ("skip", min(remaining, max(1, (lease_until - now).total_seconds())))
                if attempts >= max_attempts:
                    self._rows[key] = (attempts, "exhausted", None, None, None)
                    return ("skip", remaining)
                if status == "deferred" and next_attempt_at is not None and next_attempt_at > now:
                    return ("skip", min(remaining, max(1, (next_attempt_at - now).total_seconds())))
            else:
                attempts = 0
            new_attempts = attempts + 1
            new_lease = now + timedelta(seconds=lease_seconds)
            token = uuid.uuid4().hex
            self._rows[key] = (new_attempts, "running", None, new_lease, token)
            return ("claimed", (new_attempts, token))

    def renew_lease(self, worker, slot_start, now, *, claim_token, lease_seconds=7200):
        with self._lock:
            key = (worker, slot_start)
            prev = self._rows.get(key)
            if not prev:
                return False
            attempts, status, next_attempt_at, _, *rest = prev
            cur_token = rest[0] if rest else None
            if status != "running" or (cur_token is not None and cur_token != claim_token):
                return False
            self._rows[key] = (attempts, "running", next_attempt_at, now + timedelta(seconds=lease_seconds), cur_token)
            return True

    def finish(self, worker, slot_start, status, next_attempt_at, *, attempts=None, claim_token=None):
        with self._lock:
            key = (worker, slot_start)
            prev = self._rows.get(key)
            if prev:
                prev_attempts, prev_status, _, _, *rest = prev
                cur_token = rest[0] if rest else None
                if cur_token is not None and claim_token != cur_token:
                    return False
                if prev_status in ("done", "exhausted") and cur_token is None and claim_token is not None:
                    return False
                recorded = max(prev_attempts, attempts or 1)
                kept_token = claim_token or cur_token
            else:
                recorded = attempts or 1
                kept_token = claim_token
            self._rows[key] = (recorded, status, next_attempt_at, None, kept_token)
            return True


class DbAttemptStore:
    """Persist worker slot attempts atomically in PostgreSQL; fail closed (defer) when DB is unreachable."""

    def __init__(self, *, retention_days=65):
        self._retention_days = retention_days
        self._fallback = MemoryAttemptStore(retention_days=retention_days)

    def _defer_on_outage(self, worker, slot_start, now, *, max_attempts, retry_delay, next_slot=None):
        next_slot = next_slot or (slot_start + timedelta(days=1))
        raw_remaining = (next_slot - now).total_seconds()
        remaining = max(1, raw_remaining)
        mem = self._fallback.peek(worker, slot_start, now)
        if mem:
            att, st, nxt, _, *_ = mem
            if st in ("done", "exhausted"):
                return ("skip", remaining)
            if st == "deferred" and nxt is not None and nxt > now:
                return ("skip", min(remaining, max(1, (nxt - now).total_seconds())))
            new_att = att + 1
        else:
            new_att = 1
        if new_att < max_attempts and raw_remaining > retry_delay:
            next_at = now + timedelta(seconds=retry_delay)
            self._fallback.sync(worker, slot_start, new_att, "deferred", next_at, None, None)
            return ("skip", retry_delay)
        self._fallback.sync(worker, slot_start, new_att, "exhausted", None, None, None)
        return ("skip", remaining)

    def claim_attempt(
        self, worker, slot_start, now, *,
        max_attempts, retry_delay=600, lease_seconds=7200, next_slot=None, retention_days=None,
    ):
        next_slot = next_slot or (slot_start + timedelta(days=1))
        keep_days = retention_days if retention_days is not None else self._retention_days
        remaining = max(1, (next_slot - now).total_seconds())
        mem = self._fallback.peek(worker, slot_start, now)
        try:
            with psycopg.connect(connect_timeout=8) as db:
                db.execute(
                    "DELETE FROM refresh_worker_attempts WHERE slot_start < %s",
                    (now - timedelta(days=keep_days),),
                )
                db.execute("""
                    INSERT INTO refresh_worker_attempts(
                        worker, slot_start, attempts, status, next_attempt_at, lease_until, claim_token
                    )
                    VALUES (%s, %s, 0, 'deferred', NULL, NULL, NULL)
                    ON CONFLICT (worker, slot_start) DO NOTHING
                """, (worker, slot_start))
                row = db.execute("""
                    SELECT attempts, status, next_attempt_at, lease_until, claim_token
                    FROM refresh_worker_attempts
                    WHERE worker=%s AND slot_start=%s
                    FOR UPDATE
                """, (worker, slot_start)).fetchone()
                if row:
                    attempts, status, next_attempt_at, lease_until, *rest = row
                    cur_token = rest[0] if rest else None
                else:
                    attempts, status, next_attempt_at, lease_until, cur_token = (0, "deferred", None, None, None)

                # Normalize legacy rows that stored retry cooldowns under status='running'.
                if status == "running" and next_attempt_at is not None:
                    status = "deferred"
                    lease_until = None
                    cur_token = None
                    db.execute("""
                        UPDATE refresh_worker_attempts
                        SET status='deferred', lease_until=NULL, claim_token=NULL
                        WHERE worker=%s AND slot_start=%s
                    """, (worker, slot_start))

                if mem is not None:
                    mem_attempts, mem_status, mem_next_at, mem_lease, *mem_rest = mem
                    mem_token = mem_rest[0] if mem_rest else None
                    mem_ahead = (
                        (mem_status in ("done", "exhausted") and status not in ("done", "exhausted"))
                        or mem_attempts > attempts
                        or (mem_attempts == attempts and status == "running" and mem_status == "deferred"
                            and (cur_token is None or cur_token == mem_token))
                    )
                    if mem_ahead:
                        attempts = max(attempts, mem_attempts)
                        status = mem_status
                        next_attempt_at = mem_next_at
                        lease_until = mem_lease
                        cur_token = None if status != "running" else mem_token
                        db.execute("""
                            UPDATE refresh_worker_attempts
                            SET attempts=%s, status=%s, next_attempt_at=%s, lease_until=%s, claim_token=%s
                            WHERE worker=%s AND slot_start=%s
                        """, (attempts, status, next_attempt_at, lease_until, cur_token, worker, slot_start))

                if status in ("done", "exhausted"):
                    self._fallback.sync(worker, slot_start, attempts, status, next_attempt_at, lease_until, None)
                    return ("skip", remaining)
                if status == "running" and lease_until is not None and lease_until > now:
                    self._fallback.sync(worker, slot_start, attempts, status, next_attempt_at, lease_until, cur_token)
                    return ("skip", min(remaining, max(1, (lease_until - now).total_seconds())))
                if attempts >= max_attempts:
                    db.execute("""
                        UPDATE refresh_worker_attempts
                        SET status='exhausted', next_attempt_at=NULL, lease_until=NULL, claim_token=NULL
                        WHERE worker=%s AND slot_start=%s
                    """, (worker, slot_start))
                    self._fallback.sync(worker, slot_start, attempts, "exhausted", None, None, None)
                    return ("skip", remaining)
                if status == "deferred" and next_attempt_at is not None and next_attempt_at > now:
                    self._fallback.sync(worker, slot_start, attempts, status, next_attempt_at, lease_until, None)
                    return ("skip", min(remaining, max(1, (next_attempt_at - now).total_seconds())))

                new_attempts = attempts + 1
                new_lease = now + timedelta(seconds=lease_seconds)
                token = uuid.uuid4().hex
                db.execute("""
                    UPDATE refresh_worker_attempts
                    SET attempts=%s, status='running', next_attempt_at=NULL, lease_until=%s, claim_token=%s
                    WHERE worker=%s AND slot_start=%s
                """, (new_attempts, new_lease, token, worker, slot_start))
            self._fallback.sync(worker, slot_start, new_attempts, "running", None, new_lease, token)
            return ("claimed", (new_attempts, token))
        except Exception as exc:
            print(f"Worker attempt store claim failed ({type(exc).__name__}); deferring until DB recovers.", flush=True)
            return self._defer_on_outage(
                worker, slot_start, now, max_attempts=max_attempts, retry_delay=retry_delay, next_slot=next_slot
            )

    def renew_lease(self, worker, slot_start, now, *, claim_token, lease_seconds=7200):
        new_lease = now + timedelta(seconds=lease_seconds)
        try:
            with psycopg.connect(connect_timeout=8) as db:
                updated = db.execute("""
                    UPDATE refresh_worker_attempts
                    SET lease_until = %s
                    WHERE worker = %s AND slot_start = %s
                      AND status = 'running' AND claim_token = %s
                """, (new_lease, worker, slot_start, claim_token)).rowcount
            if updated == 1:
                self._fallback.renew_lease(
                    worker, slot_start, now, claim_token=claim_token, lease_seconds=lease_seconds
                )
                return True
            return False
        except Exception:
            return self._fallback.renew_lease(
                worker, slot_start, now, claim_token=claim_token, lease_seconds=lease_seconds
            )

    def finish(self, worker, slot_start, status, next_attempt_at, *, attempts=None, claim_token=None):
        mem = self._fallback.peek(worker, slot_start, datetime.now(KST))
        recorded = max(mem[0] if mem else 0, attempts or 1)
        try:
            with psycopg.connect(connect_timeout=8) as db:
                row = db.execute("""
                    INSERT INTO refresh_worker_attempts(
                        worker, slot_start, attempts, status, next_attempt_at, lease_until, claim_token
                    )
                    VALUES (%s, %s, %s, %s, %s, NULL, NULL)
                    ON CONFLICT (worker, slot_start) DO UPDATE
                    SET attempts = GREATEST(refresh_worker_attempts.attempts, EXCLUDED.attempts),
                        status = EXCLUDED.status,
                        next_attempt_at = EXCLUDED.next_attempt_at,
                        lease_until = NULL,
                        claim_token = NULL
                    WHERE refresh_worker_attempts.claim_token = %s
                       OR (refresh_worker_attempts.claim_token IS NULL
                           AND refresh_worker_attempts.attempts < EXCLUDED.attempts
                           AND refresh_worker_attempts.status NOT IN ('done', 'exhausted'))
                    RETURNING attempts
                """, (worker, slot_start, recorded, status, next_attempt_at, claim_token)).fetchone()
            if not row:
                return False
            self._fallback.finish(
                worker, slot_start, status, next_attempt_at, attempts=row[0], claim_token=claim_token
            )
            return True
        except Exception as exc:
            print(f"Worker attempt store finish failed ({type(exc).__name__}); recording in memory fallback.", flush=True)
            return self._fallback.finish(
                worker, slot_start, status, next_attempt_at, attempts=recorded, claim_token=claim_token
            )


def worker_names(district=None, metadata_dir=None):
    """Build scoped worker keys so partial district or auto-only runs never block fuller runs."""
    daily_key = f"daily:{district}" if district else "daily"
    base_meta = f"metadata:{district}" if district else "metadata"
    metadata_key = base_meta if metadata_dir else f"{base_meta}:auto-only"
    return daily_key, metadata_key


def run_slot(
    worker, job, now=None, store=None, *,
    max_retries=3, retry_delay=600, now_fn=None, lease_seconds=7200, heartbeat_interval=None,
    slot_start=None, next_slot=None, retention_days=None,
):
    """Execute at most one attempt for the current slot and return seconds to sleep."""
    if now_fn is None:
        now_fn = (lambda: datetime.now(KST)) if now is None else (lambda: now)
    started_at = now if now is not None else now_fn()
    slot_start = slot_start or refresh_slot_start(started_at)
    next_slot = next_slot or (slot_start + timedelta(days=1))
    max_attempts = 1 + max_retries

    claim_kwargs = dict(
        max_attempts=max_attempts,
        retry_delay=retry_delay,
        lease_seconds=lease_seconds,
    )
    if next_slot != slot_start + timedelta(days=1):
        claim_kwargs["next_slot"] = next_slot
    if retention_days is not None:
        claim_kwargs["retention_days"] = retention_days

    outcome, value = store.claim_attempt(
        worker, slot_start, started_at, **claim_kwargs
    )
    if outcome == "skip":
        return value

    if isinstance(value, tuple):
        attempts, claim_token = value
    else:
        attempts, claim_token = value, None

    hb_interval = (
        heartbeat_interval
        if heartbeat_interval is not None
        else min(max(1.0, lease_seconds / 3.0), 600.0)
    )
    stop_hb = Event()

    def _heartbeat_loop():
        while not stop_hb.wait(hb_interval):
            if hasattr(store, "renew_lease"):
                store.renew_lease(
                    worker, slot_start, now_fn(),
                    claim_token=claim_token, lease_seconds=lease_seconds,
                )

    hb_thread = Thread(target=_heartbeat_loop, name=f"{worker}-lease-heartbeat", daemon=True)
    hb_thread.start()

    settled = False
    try:
        failed = []
        try:
            failed = list(job() or [])
        except Exception as exc:
            failed.append(type(exc).__name__)
            print(f"{worker} worker error: {type(exc).__name__}", flush=True)

        stop_hb.set()
        hb_thread.join(timeout=1.0)

        finished_at = now_fn()
        raw_remaining = (next_slot - finished_at).total_seconds()
        remaining = max(1, raw_remaining)

        if not failed:
            store.finish(worker, slot_start, "done", None, attempts=attempts, claim_token=claim_token)
            settled = True
            print(f"{worker} refresh completed; next run at 06:00 KST", flush=True)
            return remaining

        pending = ", ".join(failed)
        if attempts < max_attempts and raw_remaining > retry_delay:
            next_at = finished_at + timedelta(seconds=retry_delay)
            store.finish(worker, slot_start, "deferred", next_at, attempts=attempts, claim_token=claim_token)
            settled = True
            print(
                f"{worker} refresh deferred: {pending}; "
                f"retry {attempts}/{max_retries} in {retry_delay:.0f} seconds",
                flush=True,
            )
            return retry_delay

        store.finish(worker, slot_start, "exhausted", None, attempts=attempts, claim_token=claim_token)
        settled = True
        print(
            f"{worker} refresh deferred: {pending}; retry limit ({max_retries}) reached; next run at 06:00 KST",
            flush=True,
        )
        return remaining
    finally:
        stop_hb.set()
        hb_thread.join(timeout=1.0)
        if not settled:
            aborted_at = now_fn()
            if attempts >= max_attempts or (next_slot - aborted_at).total_seconds() <= retry_delay:
                store.finish(worker, slot_start, "exhausted", None, attempts=attempts, claim_token=claim_token)
            else:
                store.finish(
                    worker,
                    slot_start,
                    "deferred",
                    aborted_at + timedelta(seconds=retry_delay),
                    attempts=attempts,
                    claim_token=claim_token,
                )


def run_once(districts=None, *, metadata_dir=None, now=None, lifecycle_provider=empty_lifecycle_provider):
    """Execute one immediate pass of daily trades, geocoding, monthly metadata, and verified files."""
    target_districts = list(districts or DISTRICTS)
    ensure_schema()
    seed_facilities()
    now = now or datetime.now(KST)
    slot_day = refresh_slot_start(now).date()

    failed = refresh_many(target_districts, now=now)
    metadata_failed = list(
        refresh_automatic_metadata(
            slot_day,
            target_districts,
            lifecycle_provider=lifecycle_provider,
        ) or []
    )
    if metadata_dir:
        try:
            apply_metadata(metadata_dir, slot_day)
        except Exception as exc:
            metadata_failed.append(f"verified-metadata ({type(exc).__name__})")
    # After K-apt and the verified files, so both steps read the corrected names and addresses.
    metadata_failed.extend(refresh_facility_sources(slot_day))
    metadata_failed.extend(run_lifecycle_month(slot_day))
    metadata_failed.extend(apply_lifecycle_inbox(slot_day))
    metadata_failed.extend(sync_facilities())

    errors = []
    if failed:
        errors.append("Failed districts: " + ", ".join(failed))
    if metadata_failed:
        errors.append("Failed metadata: " + ", ".join(metadata_failed))
    if errors:
        raise SystemExit("; ".join(errors))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--district", choices=DISTRICTS)
    parser.add_argument("--metadata-dir", type=Path)
    args = parser.parse_args()

    districts = (
        [args.district]
        if args.district
        else list(DISTRICTS)
    )

    if args.once:
        run_once(districts, metadata_dir=args.metadata_dir)
        return

    store = DbAttemptStore()
    daily_worker_name, metadata_worker_name = worker_names(args.district, args.metadata_dir)

    # 1. 일일 실거래·좌표·개별 승인(new-*.json) 갱신 전용 반복문
    def daily_worker():
        print("Daily refresh worker started", flush=True)

        def job():
            now_kst = datetime.now(KST)
            slot_day = refresh_slot_start(now_kst).date()
            failed = refresh_many(districts, now=now_kst)
            failed.extend(refresh_automatic_metadata(
                slot_day,
                districts,
                include_kapt=False,
            ))
            if args.metadata_dir:
                try:
                    apply_metadata(args.metadata_dir, slot_day, daily_only=True)
                except Exception as exc:
                    failed.append(f"verified-metadata:{type(exc).__name__}")
            failed.extend(apply_lifecycle_inbox(slot_day))
            failed.extend(sync_facilities())
            return failed

        while True:
            wait = run_slot(daily_worker_name, job, store=store, max_retries=3, retry_delay=600)
            clock.sleep(max(1, wait))

    # 2. 월간 메타데이터·검증배치·멸실·재건축 판정 전용 반복문
    def metadata_worker():
        print("Metadata refresh worker started", flush=True)

        def job(slot_day):
            failed = []
            try:
                failed.extend(
                    refresh_automatic_metadata(
                        slot_day,
                        districts,
                        include_geocode=False,
                        lifecycle_provider=empty_lifecycle_provider,
                    ) or []
                )
                if args.metadata_dir:
                    apply_metadata(args.metadata_dir, slot_day)
            except Exception as exc:
                # The existence check below counts its own attempts, so it runs in every attempt of the slot.
                failed.append(type(exc).__name__)
                print(f"{metadata_worker_name} metadata step error: {type(exc).__name__}", flush=True)
            failed.extend(refresh_facility_sources(slot_day))
            failed.extend(run_lifecycle_month(slot_day))
            return failed

        while True:
            now_kst = datetime.now(KST)
            m_slot = monthly_slot_start(now_kst)
            m_next = next_monthly_slot_start(m_slot)
            wait = run_slot(
                metadata_worker_name,
                lambda: job(refresh_slot_start(now_kst).date()),
                now=now_kst,
                now_fn=lambda: datetime.now(KST),
                store=store,
                max_retries=3,
                retry_delay=1800,
                slot_start=m_slot,
                next_slot=m_next,
                retention_days=65,
            )
            clock.sleep(max(1, wait))

    ensure_schema()
    seed_facilities()

    daily_thread = Thread(
        target=daily_worker,
        name="daily-refresh"
    )

    metadata_thread = Thread(
        target=metadata_worker,
        name="metadata-refresh"
    )

    daily_thread.start()
    metadata_thread.start()

    daily_thread.join()
    metadata_thread.join()


if __name__ == "__main__":
    main()
