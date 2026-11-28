"""Regression tests for refresh, automatic_metadata, fetch_macro, and refresh_metadata."""
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import ast
import json
import os
import re
import sys
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import pandas as pd

if "psycopg" not in sys.modules:
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        fake_psycopg = types.ModuleType("psycopg")
        fake_psycopg.Error = Exception
        fake_psycopg.connect = None
        fake_types = types.ModuleType("psycopg.types")
        fake_json = types.ModuleType("psycopg.types.json")

        class _Jsonb:
            def __init__(self, obj):
                self.obj = obj

        fake_json.Jsonb = _Jsonb
        sys.modules["psycopg"] = fake_psycopg
        sys.modules["psycopg.types"] = fake_types
        sys.modules["psycopg.types.json"] = fake_json

if "openpyxl" not in sys.modules:
    try:
        import openpyxl  # noqa: F401
    except ModuleNotFoundError:
        sys.modules["openpyxl"] = types.ModuleType("openpyxl")

import automatic_metadata as am
import fetch_macro
import refresh
import refresh_metadata

KST = timezone(timedelta(hours=9))

# psycopg3 treats every % in a query as a placeholder; only these forms are valid.
_BAD_PCT = re.compile(r"%(?![sbt%(])")


def check_placeholders(sql, params=None):
    """Reject SQL that psycopg3 would fail to parse, so fake DBs catch it like a real one."""
    if _BAD_PCT.search(sql):
        raise AssertionError(f"psycopg3 cannot parse % in query: {sql!r}")
    if params is not None and not isinstance(params, dict):
        expected = len(re.findall(r"%[sbt]", sql))
        if expected != len(params):
            raise AssertionError(f"{expected} placeholders but {len(params)} params: {sql!r}")


class FakeReconcileCursor:
    """Minimal in-memory cursor emulating the SQL statements used by refresh.reconcile."""

    def __init__(
        self, complexes, trades, decisions=None, missing=None,
        period_decisions=None, lifecycle_state=None, metadata_candidates=None,
    ):
        self.complexes = list(complexes)
        self.trades = list(trades)
        self.decisions = dict(decisions or {})
        self.missing = dict(missing or {})  # trade_id -> approved bool
        self.period_decisions = list(period_decisions or [])
        self.lifecycle_state = list(lifecycle_state or [])
        self.metadata_candidates = list(metadata_candidates or [])
        self.registered_complexes = []
        self.geocode_pending = set()
        self.identity_pending = {}
        self.revision_pending = {}
        self.trade_revisions = []
        self.inserted_trades = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self):
        return list(self._rows)

    def execute(self, sql, params=None):
        check_placeholders(sql, params)
        q = " ".join(sql.split())
        self._rows = []
        if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
            self._rows = list(self.complexes)
        elif q.startswith("SELECT source_id,target_id FROM refresh_identity_decisions"):
            self._rows = list(self.decisions.items())
        elif q.startswith("SELECT source_id, built, valid_from, valid_to, target_id, target_name, target_address, target_built FROM refresh_identity_period_decisions"):
            self._rows = list(self.period_decisions)
        elif q.startswith("SELECT complex_id, existence, as_of, effective_date, effective_precision FROM complex_lifecycle_state"):
            self._rows = list(self.lifecycle_state)
        elif q.startswith("SELECT complex_id, field, expected_value, proposed_value FROM complex_metadata_candidates"):
            self._rows = list(self.metadata_candidates)
        elif q.startswith("UPDATE refresh_identity_pending SET status='absent'"):
            for k, v in self.identity_pending.items():
                if v["status"] == "pending":
                    v["status"] = "absent"
        elif q.startswith("UPDATE refresh_identity_pending SET status='invalid_decision'"):
            for cid in params[2]:
                if cid in self.identity_pending:
                    self.identity_pending[cid]["status"] = "invalid_decision"
        elif q.startswith("UPDATE refresh_identity_pending SET status='resolved'"):
            resolved_ids = set(params[2])
            for k, v in self.identity_pending.items():
                if k in resolved_ids:
                    v["status"] = "resolved"
        elif q.startswith("SELECT trade_id,complex_id,area_group,contract_date,area,floor,price_manwon,is_cancelled FROM trades"):
            self._rows = list(self.trades)
        elif q.startswith("SELECT complex_id,area_group,contract_date,area,floor,price_manwon,is_cancelled FROM trade_revisions"):
            self._rows = [row[1:] for row in self.trade_revisions if not row[-1]]
        elif q.startswith("SELECT trade_id, approved FROM refresh_missing_trades"):
            self._rows = list(self.missing.items())
        elif q.startswith("DELETE FROM refresh_missing_trades"):
            keep = set(params[2])
            self.missing = {tid: ok for tid, ok in self.missing.items() if tid in keep}
        elif q.startswith("DELETE FROM refresh_revision_pending"):
            keep = set(params[2])
            self.revision_pending = {tid: row for tid, row in self.revision_pending.items() if tid in keep}
        elif q.startswith("UPDATE trades SET complex_id=%s WHERE trade_id = ANY(%s)"):
            target_cid, tids = params[0], set(params[1])
            self.trades = [
                (row[0], target_cid, *row[2:]) if row[0] in tids else row
                for row in self.trades
            ]
        elif q.startswith("WITH revised AS ( DELETE FROM trades WHERE trade_id = ANY(%s)"):
            removed = set(params[0])
            reason = params[1] if len(params) > 1 else "unspecified"
            kept = []
            for row in self.trades:
                if row[0] in removed:
                    self.trade_revisions.append((*row, reason))
                else:
                    kept.append(row)
            self.trades = kept
        return self

    def executemany(self, sql, seq):
        q = " ".join(sql.split())
        rows = list(seq)
        for row in rows:
            check_placeholders(sql, row)
        if q.startswith("INSERT INTO refresh_identity_pending"):
            for _, _, cid, candidates, source_rows in rows:
                self.identity_pending[cid] = {
                    "candidates": candidates,
                    "source_rows": json.loads(source_rows),
                    "status": "pending",
                }
        elif q.startswith("INSERT INTO refresh_missing_trades"):
            for tid, *_ in rows:
                self.missing.setdefault(tid, False)
        elif q.startswith("INSERT INTO refresh_revision_pending"):
            for tid, _, _, cid, cand_json, _, _ in rows:
                self.revision_pending[tid] = (cid, json.loads(cand_json))
        elif q.startswith("WITH inserted AS ( INSERT INTO complexes"):
            known = {c[0] for c in self.complexes}
            for cid, name, address, built, refresh_day, district in rows:
                if cid not in known:
                    known.add(cid)
                    self.complexes.append((cid, name, address, built))
                    self.registered_complexes.append((cid, refresh_day, district))
                    self.geocode_pending.add(cid)
        elif q.startswith("INSERT INTO trades("):
            next_id = max((r[0] for r in self.trades), default=1000) + 1
            for row in rows:
                self.inserted_trades.append(row)
                self.trades.append((next_id, *row))
                next_id += 1
        return self


class FakeReconcileDB:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur


class RefreshPipelineTests(unittest.TestCase):
    def test_empty_api_response_raises_even_with_refresh_day(self):
        cid = "서울특별시 광진구 구의동|631-1|현대프라임"
        cur = FakeReconcileCursor(
            complexes=[(cid, "현대프라임", "서울특별시 광진구 구의동 631-1", 1997)],
            trades=[(101, cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)],
        )
        with self.assertRaisesRegex(ValueError, "Empty API month"):
            refresh.reconcile(
                FakeReconcileDB(cur),
                pd.Period("2026-10", freq="M"),
                {},
                Counter(),
                "11215",
                refresh_day=date(2026, 11, 25),
            )

    def test_all_held_month_does_not_trip_empty_api_error_or_double_hold_missing(self):
        old_cid = "서울특별시 광진구 구의동|631-1|현대프라임"
        new_cid = "서울특별시 광진구 구의동|631-1|구의현대프라임"
        cur = FakeReconcileCursor(
            complexes=[(old_cid, "현대프라임", "서울특별시 광진구 구의동 631-1", 1997)],
            trades=[(101, old_cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)],
        )
        added, removed = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {new_cid: (new_cid, "구의현대프라임", "서울특별시 광진구 구의동 631-1", 1997)},
            Counter({(new_cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False): 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((added, removed), (0, 0))
        self.assertIn(new_cid, cur.identity_pending)
        self.assertEqual(cur.missing, {})

        # Keep the approval on record until the identity conflict has been resolved.
        cur.missing[101] = True
        added2, removed2 = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {new_cid: (new_cid, "구의현대프라임", "서울특별시 광진구 구의동 631-1", 1997)},
            Counter({(new_cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False): 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((added2, removed2), (0, 0))
        self.assertEqual(cur.missing, {101: True})
        self.assertEqual(len(cur.trades), 1)

    def test_trade_revision_is_held_without_duplicate_and_applied_after_approval(self):
        cid = "서울특별시 광진구 구의동|631-1|현대프라임"
        old_trade = (cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)
        revised_trade = (cid, 85, date(2026, 10, 10), Decimal("84.97"), 11, Decimal("120000"), False)
        unrelated_new = (cid, 135, date(2026, 10, 25), Decimal("134.50"), 20, Decimal("250000"), False)
        cur = FakeReconcileCursor(
            complexes=[(cid, "현대프라임", "서울특별시 광진구 구의동 631-1", 1997)],
            trades=[(501, *old_trade)],
        )
        wanted = Counter({revised_trade: 1, unrelated_new: 1})
        added, removed = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", "서울특별시 광진구 구의동 631-1", 1997)},
            wanted,
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        # Revised row is held in refresh_revision_pending; unrelated new trade inserts normally.
        self.assertEqual((added, removed), (1, 0))
        self.assertIn(501, cur.missing)
        self.assertIn(501, cur.revision_pending)
        self.assertEqual(cur.inserted_trades, [unrelated_new])
        self.assertEqual(len(cur.trades), 2)  # old_trade (501) + unrelated_new (no duplicate!)

        # Operator approves missing trade 501; next run moves 501 to trade_revisions and inserts revised_trade.
        cur.missing[501] = True
        cur.inserted_trades.clear()
        added2, removed2 = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", "서울특별시 광진구 구의동 631-1", 1997)},
            wanted,
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((added2, removed2), (1, 1))
        self.assertEqual([r[0] for r in cur.trade_revisions], [501])
        self.assertEqual(cur.inserted_trades, [revised_trade])
        self.assertEqual(cur.revision_pending, {})
        self.assertEqual(cur.missing, {})

    def test_held_candidate_explicit_cancellation_moves_original_to_revisions(self):
        cid_a = "서울특별시 광진구 구의동|631-1|현대프라임"
        cid_b = "서울특별시 광진구 구의동|631-1|구의현대프라임"
        address = "서울특별시 광진구 구의동 631-1"
        original = (cid_a, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)
        cancelled = (*original[:-1], True)
        b_trade = (cid_b, 85, date(2026, 10, 12), Decimal("84.97"), 5, Decimal("121000"), False)
        cur = FakeReconcileCursor(
            complexes=[(cid_a, "현대프라임", address, 1997)],
            trades=[(201, *original)],
        )
        added, removed = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {
                cid_a: (cid_a, "현대프라임", address, 1997),
                cid_b: (cid_b, "구의현대프라임", address, 1997),
            },
            Counter({cancelled: 1, b_trade: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((added, removed), (1, 1))
        self.assertEqual([r[0] for r in cur.trade_revisions], [201])
        self.assertEqual([r[1:] for r in cur.trades], [cancelled])
        self.assertEqual(cur.missing, {})

    def test_coexisting_active_and_cancelled_trade_holds_missing_without_new_cancellation(self):
        cid = "서울특별시 광진구 구의동|631-1|현대프라임"
        address = "서울특별시 광진구 구의동 631-1"
        original = (cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)
        cancelled = (*original[:-1], True)
        # DB initially has 1 active and 1 cancelled trade (e.g. re-contract after cancellation).
        # Next API response omits the active trade without increasing cancelled count -> must hold, not delete.
        cur = FakeReconcileCursor(
            complexes=[(cid, "현대프라임", address, 1997)],
            trades=[(201, *original), (202, *cancelled)],
        )
        added, removed = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", address, 1997)},
            Counter({cancelled: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((added, removed), (0, 0))
        self.assertEqual(cur.trade_revisions, [])
        self.assertEqual(cur.trades, [(201, *original), (202, *cancelled)])
        self.assertEqual(cur.missing, {201: False})

    def test_revision_approval_does_not_suppress_subsequent_explicit_cancellation(self):
        cid = "서울특별시 광진구 구의동|631-1|현대프라임"
        address = "서울특별시 광진구 구의동 631-1"
        original = (cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)
        revised = (cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("125000"), False)
        cancelled = (*original[:-1], True)
        cur = FakeReconcileCursor(
            complexes=[(cid, "현대프라임", address, 1997)],
            trades=[(101, *original), (102, *original)],
        )
        # Step 1: 1 of 2 identical trades has a price revision -> held, then approved.
        refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", address, 1997)},
            Counter({original: 1, revised: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        cur.missing[102] = True
        a1, r1 = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", address, 1997)},
            Counter({original: 1, revised: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a1, r1), (1, 1))
        self.assertEqual([(r[0], r[-1]) for r in cur.trade_revisions], [(102, "approved_missing")])

        # Step 2: remaining original trade (101) is explicitly cancelled in API.
        a2, r2 = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", address, 1997)},
            Counter({cancelled: 1, revised: 1}),
            "11215",
            refresh_day=date(2026, 11, 26),
        )
        self.assertEqual((a2, r2), (1, 1))
        self.assertEqual([(r[0], r[-1]) for r in cur.trade_revisions], [(102, "approved_missing"), (101, "cancelled")])
        self.assertEqual(sorted(r[1:] for r in cur.trades), sorted([revised, cancelled]))
        self.assertEqual(cur.missing, {})

    def test_identity_decision_for_registered_complex_remaps_new_trades_and_protects_invalid(self):
        cid_a = "서울특별시 광진구 구의동|631-1|단지A"
        cid_b = "서울특별시 광진구 구의동|631-1|단지B"
        address = "서울특별시 광진구 구의동 631-1"
        new_trade = (cid_a, 85, date(2026, 10, 20), Decimal("84.97"), 5, Decimal("130000"), False)
        cur = FakeReconcileCursor(
            complexes=[(cid_a, "단지A", address, 1997), (cid_b, "단지B", address, 1997)],
            trades=[],
            decisions={cid_a: cid_b},
        )
        added, removed = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {cid_a: (cid_a, "단지A", address, 1997)},
            Counter({new_trade: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((added, removed), (1, 0))
        self.assertEqual(cur.inserted_trades[0][0], cid_b)

        # Invalid decision on already-registered complex A holds incoming rows and protects A's existing trades.
        existing_a = (cid_a, 85, date(2026, 10, 10), Decimal("84.97"), 3, Decimal("120000"), False)
        cur_inv = FakeReconcileCursor(
            complexes=[(cid_a, "단지A", address, 1997)],
            trades=[(301, *existing_a)],
            decisions={cid_a: "서울특별시 광진구 구의동|999|없는단지"},
            missing={301: True},
        )
        a_inv, r_inv = refresh.reconcile(
            FakeReconcileDB(cur_inv),
            pd.Period("2026-10", freq="M"),
            {cid_a: (cid_a, "단지A", address, 1997)},
            Counter({new_trade: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a_inv, r_inv), (0, 0))
        self.assertEqual(cur_inv.identity_pending[cid_a]["status"], "invalid_decision")
        self.assertEqual(cur_inv.trades, [(301, *existing_a)])
        self.assertEqual(cur_inv.missing, {301: True})

    def test_registered_complex_alias_reassignment_and_ambiguous_revision_hold(self):
        cid_a = "서울특별시 광진구 구의동|631-1|단지A"
        cid_a2 = "서울특별시 광진구 구의동|631-1|단지A2"
        cid_b = "서울특별시 광진구 구의동|631-1|단지B"
        address = "서울특별시 광진구 구의동 631-1"
        trade_a = (cid_a, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)
        trade_b = (cid_b, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)

        # Case 1: 1:1 unambiguous match -> reassigns A's existing trade to B in one transaction (no duplicate!).
        cur = FakeReconcileCursor(
            complexes=[(cid_a, "단지A", address, 1997), (cid_b, "단지B", address, 1997)],
            trades=[(101, *trade_a)],
            decisions={cid_a: cid_b},
        )
        added, removed = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {cid_a: (cid_a, "단지A", address, 1997)},
            Counter({trade_a: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((added, removed), (0, 0))
        self.assertEqual(cur.trades, [(101, *trade_b)])
        self.assertEqual(cur.missing, {})
        self.assertEqual(cur.revision_pending, {})

        # Case 2: Ambiguous 2-to-1 match (A and A2 both aliased to B, each had 1 identical trade, API has 1)
        # -> holds the replacement row in refresh_revision_pending instead of publishing a duplicate active trade.
        trade_a2 = (cid_a2, *trade_a[1:])
        cur_amb = FakeReconcileCursor(
            complexes=[
                (cid_a, "단지A", address, 1997),
                (cid_a2, "단지A2", address, 1997),
                (cid_b, "단지B", address, 1997),
            ],
            trades=[(101, *trade_a), (201, *trade_a2)],
            decisions={cid_a: cid_b, cid_a2: cid_b},
        )
        a_amb, r_amb = refresh.reconcile(
            FakeReconcileDB(cur_amb),
            pd.Period("2026-10", freq="M"),
            {cid_a: (cid_a, "단지A", address, 1997)},
            Counter({trade_a: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a_amb, r_amb), (0, 0))
        self.assertEqual(cur_amb.inserted_trades, [])
        self.assertEqual(len(cur_amb.revision_pending), 1)

        # Case 3: A -> B alias combined with a floor revision (10 -> 11) -> holds B's revised trade until approved.
        revised_on_a = (cid_a, 85, date(2026, 10, 10), Decimal("84.97"), 11, Decimal("120000"), False)
        cur_rev = FakeReconcileCursor(
            complexes=[(cid_a, "단지A", address, 1997), (cid_b, "단지B", address, 1997)],
            trades=[(101, *trade_a)],
            decisions={cid_a: cid_b},
        )
        a_rev, r_rev = refresh.reconcile(
            FakeReconcileDB(cur_rev),
            pd.Period("2026-10", freq="M"),
            {cid_a: (cid_a, "단지A", address, 1997)},
            Counter({revised_on_a: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a_rev, r_rev), (0, 0))
        self.assertEqual(cur_rev.trades, [(101, *trade_a)])
        self.assertIn(101, cur_rev.revision_pending)
        self.assertEqual(cur_rev.revision_pending[101][0], cid_b)

    def test_identity_decision_chains_invalid_targets_and_cycles(self):
        cid_a = "서울특별시 광진구 구의동|631-1|단지A"
        cid_b = "서울특별시 광진구 구의동|631-1|단지B"
        cid_c = "서울특별시 광진구 구의동|631-1|단지C"
        address = "서울특별시 광진구 구의동 631-1"
        trade_a = (cid_a, 85, date(2026, 10, 20), Decimal("84.97"), 5, Decimal("130000"), False)

        # 1) A -> B where B itself points to an unknown complex -> A must be held as invalid_decision, not aliased to B.
        cur_bad = FakeReconcileCursor(
            complexes=[(cid_a, "단지A", address, 1997), (cid_b, "단지B", address, 1997)],
            trades=[],
            decisions={cid_a: cid_b, cid_b: "서울특별시 광진구 구의동|999|없는단지"},
        )
        a1, r1 = refresh.reconcile(
            FakeReconcileDB(cur_bad),
            pd.Period("2026-10", freq="M"),
            {cid_a: (cid_a, "단지A", address, 1997)},
            Counter({trade_a: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a1, r1), (0, 0))
        self.assertEqual(cur_bad.inserted_trades, [])
        self.assertEqual(cur_bad.identity_pending[cid_a]["status"], "invalid_decision")

        # 2) Multi-hop chain A -> B -> C where C is valid -> A's trade lands on terminal target C.
        cur_chain = FakeReconcileCursor(
            complexes=[
                (cid_a, "단지A", address, 1997),
                (cid_b, "단지B", address, 1997),
                (cid_c, "단지C", address, 1997),
            ],
            trades=[],
            decisions={cid_a: cid_b, cid_b: cid_c},
        )
        a2, r2 = refresh.reconcile(
            FakeReconcileDB(cur_chain),
            pd.Period("2026-10", freq="M"),
            {cid_a: (cid_a, "단지A", address, 1997)},
            Counter({trade_a: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a2, r2), (1, 0))
        self.assertEqual(cur_chain.inserted_trades[0][0], cid_c)

        # 3) Cycle A -> B and B -> A -> held as invalid_decision.
        cur_cycle = FakeReconcileCursor(
            complexes=[(cid_a, "단지A", address, 1997), (cid_b, "단지B", address, 1997)],
            trades=[],
            decisions={cid_a: cid_b, cid_b: cid_a},
        )
        a3, r3 = refresh.reconcile(
            FakeReconcileDB(cur_cycle),
            pd.Period("2026-10", freq="M"),
            {cid_a: (cid_a, "단지A", address, 1997)},
            Counter({trade_a: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a3, r3), (0, 0))
        self.assertEqual(cur_cycle.inserted_trades, [])
        self.assertEqual(cur_cycle.identity_pending[cid_a]["status"], "invalid_decision")

    def test_molit_build_year_conflict_is_order_independent(self):
        import molit

        def make_xml(years):
            items = "".join(
                f"<item><sggCd>11215</sggCd><umdNm>구의동</umdNm><jibun>631-1</jibun><aptNm>현대</aptNm>"
                f"<dealYear>2026</dealYear><dealMonth>10</dealMonth><dealDay>{i + 1}</dealDay>"
                f"<excluUseAr>84.97</excluUseAr><dealAmount>120,000</dealAmount>"
                f"<buildYear>{y}</buildYear><floor>5</floor></item>"
                for i, y in enumerate(years)
            )
            return (
                f"<response><header><resultCode>000</resultCode></header>"
                f"<body><totalCount>{len(years)}</totalCount><pageNo>1</pageNo>"
                f"<items>{items}</items></body></response>"
            ).encode("utf-8")

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self._payload

        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "k"}):
            with patch.object(molit, "urlopen", return_value=FakeResp(make_xml([1995, 2020]))):
                c1, _ = molit.fetch_month("202610", "11215")
            with patch.object(molit, "urlopen", return_value=FakeResp(make_xml([2020, 1995]))):
                c2, _ = molit.fetch_month("202610", "11215")
            with patch.object(molit, "urlopen", return_value=FakeResp(make_xml([1995, 2020, 2020]))):
                c3, _ = molit.fetch_month("202610", "11215")
            with patch.object(molit, "urlopen", return_value=FakeResp(make_xml([2020, 1995, 2020]))):
                c4, _ = molit.fetch_month("202610", "11215")
        self.assertEqual(list(c1.values())[0][3], 1995)
        self.assertEqual(list(c2.values())[0][3], 1995)
        self.assertEqual(list(c3.values())[0][3], 2020)
        self.assertEqual(list(c4.values())[0][3], 2020)

    def test_next_kapt_morning_aligns_to_0600_kst(self):
        afternoon = datetime(2026, 11, 25, 14, 18, tzinfo=KST)
        self.assertEqual(am.next_kapt_morning(afternoon), datetime(2026, 11, 26, 6, 0, tzinfo=KST))
        early = datetime(2026, 11, 25, 2, 30, tzinfo=KST)
        self.assertEqual(am.next_kapt_morning(early), datetime(2026, 11, 25, 6, 0, tzinfo=KST))

    def test_dong_of_address(self):
        self.assertEqual(am.dong_of_address("서울특별시 구로구 신도림동 643"), "신도림동")
        self.assertEqual(am.dong_of_address("서울시 강남구 개포동 12"), "개포동")

    def test_kapt_partial_applies_safe_dongs_and_holds_blocked_dong(self):
        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        state = {
            "complexes": {
                "서울특별시 구로구 신도림동|643|옛신도림": ("옛신도림", "서울특별시 구로구 신도림동 643", 2001),
                "서울특별시 구로구 구로동|100|옛구로": ("옛구로", "서울특별시 구로구 구로동 100", 2002),
            },
            "verified": {},
            "evidence": {},
            "pending": {},
            "cache": {},
            "runs": {},
        }

        class FakeKaptDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                    k, a, nm, b = params
                    cur_name, cur_addr, cur_built = state["complexes"][k]
                    return FakeRes([(1,)] if (cur_addr, cur_name, cur_built) == (a, nm, b) else [])
                if q.startswith("SELECT checksum FROM complex_metadata_runs"):
                    val = state["runs"].get(params[0])
                    return FakeRes([(val,)] if val else [])
                if q.startswith("SELECT EXISTS ( SELECT 1 FROM kapt_metadata_pending"):
                    if "reason <> 'EMPTY_DETAIL'" in q:
                        has_retryable = any(r != "EMPTY_DETAIL" for r in state["pending"].values())
                        return FakeRes([(has_retryable,)])
                    if "next_retry_at" in q:
                        return FakeRes([(False,)])
                    return FakeRes([(bool(state["pending"]),)])
                if q.startswith("SELECT kapt_code, payload FROM kapt_metadata_cache"):
                    return FakeRes(list(state["cache"].items()))
                if q.startswith("SELECT kapt_code, COALESCE(next_retry_at <= NOW(), TRUE), reason FROM kapt_metadata_pending"):
                    return FakeRes([])
                if q.startswith("INSERT INTO kapt_metadata_pending"):
                    reason = "EMPTY_DETAIL" if "'EMPTY_DETAIL'" in q else params[3]
                    state["pending"][params[2]] = reason
                    return FakeRes()
                if q.startswith("INSERT INTO kapt_metadata_cache"):
                    state["cache"][params[1]] = params[2].obj
                    return FakeRes()
                if q.startswith("INSERT INTO complex_metadata_runs"):
                    state["runs"][params[0]] = params[1]
                    return FakeRes([(params[0],)])
                if q.startswith("UPDATE complex_metadata_runs SET checksum="):
                    state["runs"][params[1]] = params[0]
                    return FakeRes()
                if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                    return FakeRes([(cid, *vals) for cid, vals in state["complexes"].items()])
                if q.startswith("SELECT kapt_code,complex_id FROM kapt_verified_links"):
                    return FakeRes(list(state["verified"].items()))
                if q.startswith("DELETE FROM kapt_verified_links WHERE district=%s AND (kapt_code=%s OR complex_id=%s)"):
                    state["verified"] = {k: v for k, v in state["verified"].items() if k != params[1] and v != params[2]}
                    return FakeRes()
                if q.startswith("INSERT INTO kapt_verified_links"):
                    state["verified"][params[1]] = params[2]
                    return FakeRes()
                if q.startswith("UPDATE complexes SET name=%s"):
                    new_name, cid, addr, old_name, expected_built = params
                    cur_name, cur_addr, built = state["complexes"][cid]
                    if cur_name == old_name and cur_addr == addr and built == expected_built:
                        state["complexes"][cid] = (new_name, cur_addr, built)
                        return FakeRes(rowcount=1)
                    return FakeRes(rowcount=0)
                if q.startswith("INSERT INTO complex_metadata_evidence"):
                    state["evidence"][params[1]] = params[3].obj
                    return FakeRes()
                return FakeRes()

        listing = (
            ["A1", "A2", "A3"],
            {"A1": "신도림동", "A2": "구로동", "A3": "구로동"},
        )
        details = {
            "A1": {"item": {"kaptCode": "A1", "kaptName": "새신도림", "kaptAddr": "서울특별시 구로구 신도림동 643", "kaptUsedate": "20010501"}},
            "A2": {"item": {"kaptCode": "A2", "kaptName": "새구로", "kaptAddr": "서울특별시 구로구 구로동 100", "kaptUsedate": "20020501"}},
            "A3": {"item": {"kaptCode": "A3", "kaptName": "", "kaptAddr": "", "kaptUsedate": ""}},
        }

        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", return_value=listing), \
             patch.object(am, "kapt_body", side_effect=lambda ep, key, kaptCode: details[kaptCode]):
            status = am.refresh_kapt_district("2026-11", "key", "11530")
        self.assertEqual(status, "empty_detail_only")
        # 신도림동 is unaffected by 구로동 EMPTY_DETAIL -> immediately updated and verified.
        self.assertEqual(state["complexes"]["서울특별시 구로구 신도림동|643|옛신도림"][0], "새신도림")
        self.assertEqual(state["verified"].get("A1"), "서울특별시 구로구 신도림동|643|옛신도림")
        # 구로동 unverified complex is held while A3 in 구로동 has EMPTY_DETAIL.
        self.assertEqual(state["complexes"]["서울특별시 구로구 구로동|100|옛구로"][0], "옛구로")

        # Simulate a concurrent metadata edit after a full collection.
        complete_listing = (["A2"], {"A2": listing[1]["A2"]})
        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", return_value=complete_listing), \
             patch.object(am, "kapt_body", return_value=details["A2"]), \
             patch.object(am, "update_complex", return_value=False):
            self.assertEqual(am.refresh_kapt_district("2026-12", "key", "11530"), "partial")
        self.assertEqual(state["runs"]["kapt-2026-12-11530"], "automatic:kapt:partial")
        self.assertEqual(state["pending"].pop("A2"), "APPLY_CONFLICT")

        # Subsequent retry before next_retry_at skips kapt_listing entirely.
        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", side_effect=AssertionError("should not call listing")):
            status2 = am.refresh_kapt_district("2026-11", "key", "11530")
        self.assertEqual(status2, "empty_detail_only")

        # If a non-EMPTY_DETAIL retryable pending exists, early return reports "partial".
        state["pending"]["A4"] = "HTTP_429"
        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", side_effect=AssertionError("should not call listing")):
            status3 = am.refresh_kapt_district("2026-11", "key", "11530")
        self.assertEqual(status3, "partial")

    def test_kapt_partial_protects_verified_complex_held_by_deferred_code(self):
        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        cid_x = "서울특별시 구로구 구로동|100|기존단지"
        state = {
            "complexes": {cid_x: ("기존단지", "서울특별시 구로구 구로동 100", 2002)},
            "verified": {"A": cid_x},
            "evidence": {},
            "pending": {},
            "cache": {},
            "runs": {},
        }

        class FakeKaptDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                    k, a, nm, b = params
                    cur_name, cur_addr, cur_built = state["complexes"][k]
                    return FakeRes([(1,)] if (cur_addr, cur_name, cur_built) == (a, nm, b) else [])
                if q.startswith("SELECT checksum FROM complex_metadata_runs"):
                    val = state["runs"].get(params[0])
                    return FakeRes([(val,)] if val else [])
                if q.startswith("SELECT kapt_code, payload FROM kapt_metadata_cache"):
                    return FakeRes(list(state["cache"].items()))
                if q.startswith("SELECT kapt_code, COALESCE(next_retry_at <= NOW(), TRUE), reason FROM kapt_metadata_pending"):
                    return FakeRes([])
                if q.startswith("INSERT INTO kapt_metadata_pending"):
                    reason = "EMPTY_DETAIL" if "'EMPTY_DETAIL'" in q else params[3]
                    state["pending"][params[2]] = reason
                    return FakeRes()
                if q.startswith("INSERT INTO kapt_metadata_cache"):
                    state["cache"][params[1]] = params[2].obj
                    return FakeRes()
                if q.startswith("INSERT INTO complex_metadata_runs"):
                    state["runs"][params[0]] = params[1]
                    return FakeRes([(params[0],)])
                if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                    return FakeRes([(cid, *vals) for cid, vals in state["complexes"].items()])
                if q.startswith("SELECT kapt_code,complex_id FROM kapt_verified_links"):
                    return FakeRes(list(state["verified"].items()))
                if q.startswith("DELETE FROM kapt_verified_links WHERE district=%s AND (kapt_code=%s OR complex_id=%s)"):
                    state["verified"] = {k: v for k, v in state["verified"].items() if k != params[1] and v != params[2]}
                    return FakeRes()
                if q.startswith("INSERT INTO kapt_verified_links"):
                    state["verified"][params[1]] = params[2]
                    return FakeRes()
                if q.startswith("UPDATE complexes SET name=%s"):
                    new_name, cid, addr, old_name, expected_built = params
                    cur_name, cur_addr, built = state["complexes"][cid]
                    if cur_name == old_name and cur_addr == addr and built == expected_built:
                        state["complexes"][cid] = (new_name, cur_addr, built)
                        return FakeRes(rowcount=1)
                    return FakeRes(rowcount=0)
                if q.startswith("INSERT INTO complex_metadata_evidence"):
                    state["evidence"][params[1]] = params[3].obj
                    return FakeRes()
                return FakeRes()

        listing = (["A", "B"], {"A": "구로동", "B": "구로동"})
        details = {
            "A": {"item": {"kaptCode": "A", "kaptName": "", "kaptAddr": "", "kaptUsedate": ""}},
            "B": {"item": {"kaptCode": "B", "kaptName": "다른단지B", "kaptAddr": "서울특별시 구로구 구로동 100", "kaptUsedate": "20020501"}},
        }
        import io
        buf = io.StringIO()
        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", return_value=listing), \
             patch.object(am, "kapt_body", side_effect=lambda ep, key, kaptCode: details[kaptCode]), \
             patch("sys.stdout", buf):
            status = am.refresh_kapt_district("2026-11", "key", "11530")
        self.assertEqual(status, "empty_detail_only")
        self.assertEqual(state["complexes"][cid_x][0], "기존단지")
        self.assertEqual(state["verified"], {"A": cid_x})
        self.assertIn("held_names=1", buf.getvalue())

    def test_kapt_partial_with_zero_pending_resumes_final_apply(self):
        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        cid = "서울특별시 구로구 신도림동|643|옛신도림"
        batch = "kapt-2026-11-11530"
        cached_item = {
            "kaptCode": "A1",
            "kaptName": "새신도림",
            "kaptAddr": "서울특별시 구로구 신도림동 643",
            "kaptUsedate": "20010501",
        }
        state = {
            "complexes": {cid: ("옛신도림", "서울특별시 구로구 신도림동 643", 2001)},
            "verified": {},
            "evidence": {},
            "pending": {},
            "cache": {"A1": cached_item},
            "runs": {batch: "automatic:kapt:partial"},
        }

        class FakeKaptDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                    k, a, nm, b = params
                    cur_name, cur_addr, cur_built = state["complexes"][k]
                    return FakeRes([(1,)] if (cur_addr, cur_name, cur_built) == (a, nm, b) else [])
                if q.startswith("SELECT checksum FROM complex_metadata_runs"):
                    val = state["runs"].get(params[0])
                    return FakeRes([(val,)] if val else [])
                if q.startswith("SELECT EXISTS ( SELECT 1 FROM kapt_metadata_pending"):
                    if "reason <> 'EMPTY_DETAIL'" in q:
                        return FakeRes([(any(r != "EMPTY_DETAIL" for r in state["pending"].values()),)])
                    if "next_retry_at" in q:
                        return FakeRes([(False,)])
                    return FakeRes([(bool(state["pending"]),)])
                if q.startswith("SELECT kapt_code, payload FROM kapt_metadata_cache"):
                    return FakeRes(list(state["cache"].items()))
                if q.startswith("SELECT kapt_code, COALESCE(next_retry_at <= NOW(), TRUE), reason FROM kapt_metadata_pending"):
                    return FakeRes([])
                if q.startswith("INSERT INTO complex_metadata_runs"):
                    state["runs"][params[0]] = params[1]
                    return FakeRes([(params[0],)])
                if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                    return FakeRes([(k, *v) for k, v in state["complexes"].items()])
                if q.startswith("SELECT kapt_code,complex_id FROM kapt_verified_links"):
                    return FakeRes(list(state["verified"].items()))
                if q.startswith("DELETE FROM kapt_verified_links"):
                    state["verified"].clear()
                    return FakeRes()
                if q.startswith("INSERT INTO kapt_verified_links"):
                    state["verified"][params[1]] = params[2]
                    return FakeRes()
                if q.startswith("UPDATE complexes SET name=%s"):
                    new_name, k, addr, old_name, expected_built = params
                    cur_name, cur_addr, built = state["complexes"][k]
                    if cur_name == old_name and cur_addr == addr and built == expected_built:
                        state["complexes"][k] = (new_name, cur_addr, built)
                        return FakeRes(rowcount=1)
                    return FakeRes(rowcount=0)
                if q.startswith("INSERT INTO complex_metadata_evidence"):
                    state["evidence"][params[1]] = params[3].obj
                    return FakeRes()
                return FakeRes()

        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", return_value=(["A1"], {"A1": "신도림동"})), \
             patch.object(am, "kapt_body", side_effect=AssertionError("should use cache")):
            status = am.refresh_kapt_district("2026-11", "key", "11530")
        self.assertEqual(status, "completed")
        self.assertEqual(state["complexes"][cid][0], "새신도림")
        self.assertEqual(state["runs"][batch], "automatic:kapt")

    def test_kapt_concurrent_built_year_change_defers_without_name_or_verified_link(self):
        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        cid = "서울특별시 구로구 신도림동|643|옛단지"
        addr = "서울특별시 구로구 신도림동 643"
        for kapt_name in ("2000년단지명", "옛단지"):
            state = {
                "complexes": {cid: ["옛단지", addr, 2000]},
                "verified": {},
                "pending": {},
                "runs": {},
                "evidence": {},
            }

            class FakeKaptDB:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                @contextmanager
                def transaction(self):
                    yield

                def execute(self, sql, params=None):
                    check_placeholders(sql, params)
                    q = " ".join(sql.split())
                    if q.startswith("SELECT 1 FROM complex_metadata_runs WHERE batch LIKE %s AND batch > %s"):
                        return FakeRes([])
                    if q.startswith("SELECT checksum FROM complex_metadata_runs"):
                        val = state["runs"].get(params[0])
                        return FakeRes([(val,)] if val else [])
                    if q.startswith("INSERT INTO complex_metadata_runs"):
                        state["runs"][params[0]] = params[1]
                        return FakeRes([(params[0],)])
                    if q.startswith("UPDATE complex_metadata_runs SET checksum="):
                        state["runs"][params[1]] = params[0]
                        return FakeRes()
                    if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                        rows = [(k, *v) for k, v in state["complexes"].items()]
                        # Concurrent verified metadata job corrects built 2000 -> 2015 right after SELECT
                        state["complexes"][cid][2] = 2015
                        return FakeRes(rows)
                    if q.startswith("SELECT kapt_code,complex_id FROM kapt_verified_links"):
                        return FakeRes(list(state["verified"].items()))
                    if q.startswith("INSERT INTO kapt_verified_links"):
                        state["verified"][params[1]] = params[2]
                        return FakeRes()
                    if q.startswith("UPDATE complexes SET name=%s"):
                        new_name, k, a, old_name, expected_built = params
                        cur_name, cur_addr, cur_built = state["complexes"][k]
                        if cur_name == old_name and cur_addr == a and cur_built == expected_built:
                            state["complexes"][k][0] = new_name
                            return FakeRes(rowcount=1)
                        return FakeRes(rowcount=0)
                    if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                        k, a, nm, b = params
                        cur_name, cur_addr, cur_built = state["complexes"][k]
                        return FakeRes([(1,)] if (cur_addr, cur_name, cur_built) == (a, nm, b) else [])
                    if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND built=%s"):
                        k, a, b = params
                        _, cur_addr, cur_built = state["complexes"][k]
                        return FakeRes([(1,)] if (cur_addr, cur_built) == (a, b) else [])
                    if q.startswith("INSERT INTO kapt_metadata_pending"):
                        state["pending"][params[2]] = params[3]
                        return FakeRes()
                    return FakeRes()

            with self.subTest(kapt_name=kapt_name):
                with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
                     patch.object(am, "kapt_listing", return_value=(["K2000"], {"K2000": "신도림동"})), \
                     patch.object(am, "kapt_body", return_value={"item": {
                         "kaptCode": "K2000", "kaptName": kapt_name, "kaptAddr": addr, "kaptUsedate": "20000501",
                     }}):
                    status = am.refresh_kapt_district("2026-11", "key", "11530")
                self.assertEqual(status, "partial")
                self.assertEqual(state["complexes"][cid], ["옛단지", addr, 2015])
                self.assertEqual(state["verified"], {})
                self.assertEqual(state["pending"].get("K2000"), "APPLY_CONFLICT")
                self.assertEqual(state["runs"]["kapt-2026-11-11530"], "automatic:kapt:partial")

    def test_kapt_delayed_older_month_does_not_overwrite_completed_newer_month(self):
        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        cid = "서울특별시 구로구 신도림동|643|초기명"
        addr = "서울특별시 구로구 신도림동 643"
        state = {
            "complexes": {cid: ["초기명", addr, 2000]},
            "verified": {},
            "runs": {},
            "pending": {("kapt-2026-11-11530", "K1"): "pending"},
            "locks": [],
        }

        class FakeKaptDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                    k, a, nm, b = params
                    cur_name, cur_addr, cur_built = state["complexes"][k]
                    return FakeRes([(1,)] if (cur_addr, cur_name, cur_built) == (a, nm, b) else [])
                if q.startswith("SELECT pg_advisory_xact_lock"):
                    state["locks"].append(params[0])
                    return FakeRes()
                if q.startswith("SELECT 1 FROM complex_metadata_runs WHERE batch LIKE %s AND batch > %s"):
                    prefix, cur_batch = params
                    suffix = prefix.split("%", 1)[1]
                    has_newer = any(b.endswith(suffix) and b > cur_batch for b in state["runs"])
                    return FakeRes([(1,)] if has_newer else [])
                if q.startswith("SELECT checksum FROM complex_metadata_runs"):
                    val = state["runs"].get(params[0])
                    return FakeRes([(val,)] if val else [])
                if q.startswith("INSERT INTO complex_metadata_runs"):
                    state["runs"][params[0]] = params[1]
                    return FakeRes([(params[0],)])
                if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                    return FakeRes([(k, *v) for k, v in state["complexes"].items()])
                if q.startswith("UPDATE complexes SET name=%s"):
                    new_name, k, a, old_name, expected_built = params
                    cur_name, cur_addr, cur_built = state["complexes"][k]
                    if (cur_name, cur_addr, cur_built) == (old_name, a, expected_built):
                        state["complexes"][k][0] = new_name
                        return FakeRes(rowcount=1)
                    return FakeRes(rowcount=0)
                if q.startswith("UPDATE kapt_metadata_pending SET status = 'superseded'"):
                    if "WHERE batch = %s" in q:
                        for (b, c) in list(state["pending"]):
                            if b == params[0] and state["pending"][(b, c)] == "pending":
                                state["pending"][(b, c)] = "superseded"
                    return FakeRes()
                return FakeRes()

        # Simulate Nov worker starting BEFORE Dec completes, then Dec completes while Nov is fetching details.
        dec_completed = False

        def nov_fetch(ep, key, kaptCode):
            nonlocal dec_completed
            if not dec_completed:
                dec_completed = True
                with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
                     patch.object(am, "kapt_listing", return_value=(["K1"], {"K1": "신도림동"})), \
                     patch.object(am, "kapt_body", return_value={"item": {
                         "kaptCode": "K1", "kaptName": "12월단지명", "kaptAddr": addr, "kaptUsedate": "20000501",
                     }}):
                    self.assertEqual(am.refresh_kapt_district("2026-12", "key", "11530"), "completed")
            return {"item": {"kaptCode": "K1", "kaptName": "11월단지명", "kaptAddr": addr, "kaptUsedate": "20000501"}}

        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", return_value=(["K1"], {"K1": "신도림동"})), \
             patch.object(am, "kapt_body", side_effect=nov_fetch):
            status_nov = am.refresh_kapt_district("2026-11", "key", "11530")
        self.assertEqual(status_nov, "completed")
        self.assertEqual(state["complexes"][cid][0], "12월단지명")
        self.assertEqual(state["pending"][("kapt-2026-11-11530", "K1")], "superseded")
        self.assertIn(am.KAPT_LOCK_BASE + 11530, state["locks"])

    def test_kapt_same_month_disappeared_pending_code_is_marked_absent(self):
        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        cid = "서울특별시 구로구 신도림동|643|초기명"
        addr = "서울특별시 구로구 신도림동 643"
        batch = "kapt-2026-11-11530"
        state = {
            "complexes": {cid: ["초기명", addr, 2000]},
            "verified": {},
            "runs": {batch: "automatic:kapt:partial"},
            "pending": {(batch, "OLD_CODE"): "pending"},
        }

        class FakeKaptDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                    k, a, nm, b = params
                    cur_name, cur_addr, cur_built = state["complexes"][k]
                    return FakeRes([(1,)] if (cur_addr, cur_name, cur_built) == (a, nm, b) else [])
                if q.startswith("SELECT 1 FROM complex_metadata_runs WHERE batch LIKE %s AND batch > %s"):
                    return FakeRes([])
                if q.startswith("SELECT checksum FROM complex_metadata_runs"):
                    val = state["runs"].get(params[0])
                    return FakeRes([(val,)] if val else [])
                if q.startswith("SELECT EXISTS"):
                    return FakeRes([(True,)])
                if q.startswith("UPDATE kapt_metadata_pending SET status = 'absent'"):
                    b, active_codes = params
                    active_set = set(active_codes)
                    for (pb, pc), st in list(state["pending"].items()):
                        if pb == b and st == "pending" and pc not in active_set:
                            state["pending"][(pb, pc)] = "absent"
                    return FakeRes()
                if q.startswith("SELECT kapt_code, COALESCE"):
                    return FakeRes([
                        (pc, True, "HTTP_500")
                        for (pb, pc), st in state["pending"].items()
                        if pb == params[0] and st == "pending"
                    ])
                if q.startswith("INSERT INTO complex_metadata_runs"):
                    state["runs"][params[0]] = params[1]
                    return FakeRes([(params[0],)])
                if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                    return FakeRes([(k, *v) for k, v in state["complexes"].items()])
                if q.startswith("UPDATE complexes SET name=%s"):
                    new_name, k, a, old_name, expected_built = params
                    cur_name, cur_addr, cur_built = state["complexes"][k]
                    if (cur_name, cur_addr, cur_built) == (old_name, a, expected_built):
                        state["complexes"][k][0] = new_name
                        return FakeRes(rowcount=1)
                    return FakeRes(rowcount=0)
                return FakeRes()

        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", return_value=(["K1"], {"K1": "신도림동"})), \
             patch.object(am, "kapt_body", return_value={"item": {
                 "kaptCode": "K1", "kaptName": "새명", "kaptAddr": addr, "kaptUsedate": "20000501",
             }}):
            status = am.refresh_kapt_district("2026-11", "key", "11530")
        self.assertEqual(status, "completed")
        self.assertEqual(state["runs"][batch], "automatic:kapt")
        self.assertEqual(state["pending"][(batch, "OLD_CODE")], "absent")

    def test_kapt_locks_and_verifies_row_right_before_saving_verified_link(self):
        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        cid = "서울특별시 구로구 신도림동|643|초기명"
        addr = "서울특별시 구로구 신도림동 643"

        # Sub-case A: update_complex fails because another worker changed the name to "새명" AND built 2000 -> 2015.
        # The verification query before INSERT INTO kapt_verified_links must lock with FOR UPDATE and reject built=2015.
        # Sub-case B: old_name == name ("초기명"), but another worker changed name to "다른이름" right before verification.
        for mutate_after_select, incoming_name in (
            (["새명", addr, 2015], "새명"),
            (["다른이름", addr, 2000], "초기명"),
        ):
            state = {
                "complexes": {cid: ["초기명", addr, 2000]},
                "verified": {},
                "pending": {},
                "runs": {},
                "saw_for_update": False,
            }

            class FakeKaptDB:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                @contextmanager
                def transaction(self):
                    yield

                def execute(self, sql, params=None):
                    check_placeholders(sql, params)
                    q = " ".join(sql.split())
                    if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                        rows = [(k, *v) for k, v in state["complexes"].items()]
                        state["complexes"][cid] = list(mutate_after_select)
                        return FakeRes(rows)
                    if q.startswith("UPDATE complexes SET name=%s"):
                        return FakeRes(rowcount=0)
                    if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                        state["saw_for_update"] = "FOR UPDATE" in q
                        k, a, nm, b = params
                        cur_name, cur_addr, cur_built = state["complexes"][k]
                        return FakeRes([(1,)] if (cur_addr, cur_name, cur_built) == (a, nm, b) else [])
                    if q.startswith("INSERT INTO complex_metadata_runs"):
                        state["runs"][params[0]] = params[1]
                        return FakeRes([(params[0],)])
                    if q.startswith("UPDATE complex_metadata_runs SET checksum="):
                        state["runs"][params[1]] = params[0]
                        return FakeRes()
                    if q.startswith("INSERT INTO kapt_verified_links"):
                        state["verified"][params[1]] = params[2]
                        return FakeRes()
                    if q.startswith("INSERT INTO kapt_metadata_pending"):
                        state["pending"][params[2]] = params[3]
                        return FakeRes()
                    return FakeRes()

            with self.subTest(mutate=mutate_after_select, incoming_name=incoming_name):
                with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
                     patch.object(am, "kapt_listing", return_value=(["K1"], {"K1": "신도림동"})), \
                     patch.object(am, "kapt_body", return_value={"item": {
                         "kaptCode": "K1", "kaptName": incoming_name, "kaptAddr": addr, "kaptUsedate": "20000501",
                     }}):
                    st = am.refresh_kapt_district("2026-11", "key", "11530")
                self.assertTrue(state["saw_for_update"])
                self.assertEqual(st, "partial")
                self.assertEqual(state["verified"], {})
                self.assertEqual(state["pending"].get("K1"), "APPLY_CONFLICT")
                self.assertEqual(state["runs"]["kapt-2026-11-11530"], "automatic:kapt:partial")

    def test_kapt_completed_batch_rejects_late_concurrent_pending_and_cleans_up(self):
        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        cid = "서울특별시 구로구 신도림동|643|초기명"
        addr = "서울특별시 구로구 신도림동 643"
        batch = "kapt-2026-11-11530"
        state = {
            "complexes": {cid: ["초기명", addr, 2000]},
            "runs": {},
            "pending": {},
            "cache": {},
        }

        class FakeKaptDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT checksum FROM complex_metadata_runs"):
                    val = state["runs"].get(params[0])
                    return FakeRes([(val,)] if val else [])
                if q.startswith("INSERT INTO complex_metadata_runs"):
                    b, cs = params
                    if b in state["runs"] and state["runs"][b] != "automatic:kapt:partial":
                        return FakeRes([])
                    state["runs"][b] = cs
                    return FakeRes([(b,)])
                if q.startswith("INSERT INTO kapt_metadata_pending"):
                    b, _, code = params[0], params[1], params[2]
                    # SQL has WHERE NOT EXISTS (... r.batch = %s AND r.checksum = 'automatic:kapt' ...)
                    if state["runs"].get(b) != "automatic:kapt":
                        state["pending"][(b, code)] = "pending"
                    return FakeRes()
                if q.startswith("UPDATE kapt_metadata_pending SET status = 'resolved'"):
                    b = params[0]
                    for (pb, pc), st in list(state["pending"].items()):
                        if pb == b and st == "pending":
                            state["pending"][(pb, pc)] = "resolved"
                    return FakeRes()
                if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                    return FakeRes([(k, *v) for k, v in state["complexes"].items()])
                if q.startswith("UPDATE complexes SET name=%s"):
                    new_name, k, a, old_name, expected_built = params
                    state["complexes"][k][0] = new_name
                    return FakeRes(rowcount=1)
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                    return FakeRes([(1,)])
                return FakeRes()

        # Worker 2 enters refresh_kapt_district before Worker 1 completes; Worker 1 completes, then Worker 2's detail fails.
        worker1_ran = False

        def worker2_detail(ep, key, kaptCode):
            nonlocal worker1_ran
            if not worker1_ran:
                worker1_ran = True
                with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
                     patch.object(am, "kapt_listing", return_value=(["K1"], {"K1": "신도림동"})), \
                     patch.object(am, "kapt_body", return_value={"item": {
                         "kaptCode": "K1", "kaptName": "완료단지명", "kaptAddr": addr, "kaptUsedate": "20000501",
                     }}):
                    self.assertEqual(am.refresh_kapt_district("2026-11", "key", "11530"), "completed")
            raise TimeoutError("late detail failure")

        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeKaptDB()), \
             patch.object(am, "kapt_listing", return_value=(["K1"], {"K1": "신도림동"})), \
             patch.object(am, "kapt_body", side_effect=worker2_detail):
            st2 = am.refresh_kapt_district("2026-11", "key", "11530")

        self.assertEqual(st2, "completed")
        self.assertEqual(state["runs"][batch], "automatic:kapt")
        active_pending = [k for k, v in state["pending"].items() if v == "pending"]
        self.assertEqual(active_pending, [])

    def test_optional_credentials_do_not_fail_automatic_metadata(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(am.refresh_automatic_metadata(date(2026, 11, 25), ["11215"]), [])

    def test_geocode_400_records_cooldown_without_raising_while_503_raises(self):
        class FakeGeocodeDB:
            def __init__(self):
                self.recorded = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def commit(self):
                pass

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                class Res:
                    def __init__(self, rows):
                        self._rows = rows
                        self.rowcount = 1

                    def fetchall(self):
                        return self._rows

                    def fetchone(self):
                        return self._rows[0] if self._rows else None

                if "SELECT c.complex_id, c.address FROM complexes" in sql:
                    return Res([("서울특별시 광진구 구의동|1|불량", "서울특별시 광진구 구의동 1")])
                if "INSERT INTO complex_geocode_attempts" in sql:
                    self.recorded.append(params)
                return Res([])

        fake_db = FakeGeocodeDB()
        err_400 = HTTPError(am.NAVER_URL, 400, "Bad Request", {}, None)
        with patch.object(am.psycopg, "connect", return_value=fake_db), \
             patch.object(am, "read_json", side_effect=err_400):
            am.geocode(date(2026, 11, 25), {}, ["11215"])
        self.assertEqual(len(fake_db.recorded), 1)
        self.assertEqual(fake_db.recorded[0][2], date(2026, 11, 25))

        fake_db2 = FakeGeocodeDB()
        err_503 = HTTPError(am.NAVER_URL, 503, "Unavailable", {}, None)
        with patch.object(am.psycopg, "connect", return_value=fake_db2), \
             patch.object(am, "read_json", side_effect=err_503):
            with self.assertRaises(HTTPError):
                am.geocode(date(2026, 11, 25), {}, ["11215"])
        self.assertEqual(fake_db2.recorded, [])

    def test_geocode_service_error_status_aborts_without_recording_attempts(self):
        class FakeGeocodeDB:
            def __init__(self):
                self.recorded = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def commit(self):
                pass

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                class Res:
                    def __init__(self, rows):
                        self._rows = rows
                        self.rowcount = 1

                    def fetchall(self):
                        return self._rows

                    def fetchone(self):
                        return self._rows[0] if self._rows else None

                if "SELECT c.complex_id, c.address FROM complexes" in sql:
                    return Res([
                        ("서울특별시 광진구 구의동|1|단지1", "서울특별시 광진구 구의동 1"),
                        ("서울특별시 광진구 구의동|2|단지2", "서울특별시 광진구 구의동 2"),
                    ])
                if "INSERT INTO complex_geocode_attempts" in sql:
                    self.recorded.append(params)
                return Res([])

        fake_db = FakeGeocodeDB()
        bad_payload = {"status": "SYSTEM_ERROR", "errorMessage": "secret-internal-detail"}
        with patch.object(am.psycopg, "connect", return_value=fake_db), \
             patch.object(am, "read_json", return_value=bad_payload):
            with self.assertRaises(am.GeocodingServiceError) as ctx:
                am.geocode(date(2026, 11, 25), {}, ["11215"])
        self.assertIn("SYSTEM_ERROR", str(ctx.exception))
        self.assertNotIn("secret-internal-detail", str(ctx.exception))
        self.assertEqual(fake_db.recorded, [])

        # refresh_automatic_metadata treats GeocodingServiceError as a retryable failure.
        fake_db2 = FakeGeocodeDB()
        with patch.dict(os.environ, {"NAVER_MAPS_API_KEY_ID": "id", "NAVER_MAPS_API_KEY": "key"}, clear=True), \
             patch.object(am.psycopg, "connect", return_value=fake_db2), \
             patch.object(am, "read_json", return_value=bad_payload):
            failed = am.refresh_automatic_metadata(date(2026, 11, 25), ["11215"], include_kapt=False)
        self.assertEqual(failed, ["Naver Geocoding"])

    def test_geocode_broken_json_raises_service_error_without_cooldown(self):
        class FakeGeocodeDB:
            def __init__(self):
                self.recorded = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def commit(self):
                pass

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                class Res:
                    def __init__(self, rows):
                        self._rows = rows
                        self.rowcount = 1

                    def fetchall(self):
                        return self._rows

                    def fetchone(self):
                        return self._rows[0] if self._rows else None

                if "SELECT c.complex_id, c.address FROM complexes" in sql:
                    return Res([
                        ("서울특별시 광진구 구의동|1|단지1", "서울특별시 광진구 구의동 1"),
                        ("서울특별시 광진구 구의동|2|단지2", "서울특별시 광진구 구의동 2"),
                    ])
                if "INSERT INTO complex_geocode_attempts" in sql:
                    self.recorded.append(params)
                return Res([])

        class FakeResp:
            def __init__(self, raw):
                self._raw = raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self._raw

        for bad_body in (b"<html>502 Bad Gateway</html>", b"\xff\xfe\x00"):
            fake_db = FakeGeocodeDB()
            with self.subTest(bad_body=bad_body):
                with patch.object(am.psycopg, "connect", return_value=fake_db), \
                     patch.object(am, "urlopen", side_effect=lambda *a, **kw: FakeResp(bad_body)):
                    with self.assertRaises(am.GeocodingServiceError):
                        am.geocode(date(2026, 11, 25), {}, ["11215"])
                self.assertEqual(fake_db.recorded, [])

        fake_db2 = FakeGeocodeDB()
        with patch.dict(os.environ, {"NAVER_MAPS_API_KEY_ID": "id", "NAVER_MAPS_API_KEY": "key"}, clear=True), \
             patch.object(am.psycopg, "connect", return_value=fake_db2), \
             patch.object(am, "urlopen", side_effect=lambda *a, **kw: FakeResp(b"<html>error</html>")):
            failed = am.refresh_automatic_metadata(date(2026, 11, 25), ["11215"], include_kapt=False)
        self.assertEqual(failed, ["Naver Geocoding"])
        self.assertEqual(fake_db2.recorded, [])

    def test_select_monthly_post_picks_latest_when_multiple_exist(self):
        rows = [
            {"listSubCd": "STATDATA03", "seq": 100, "userDttm": "2026-10-15"},
            {"listSubCd": "STATDATA01", "seq": 200, "userDttm": "2026-11-20"},
            {"listSubCd": "STATDATA03", "seq": 105, "userDttm": "2026-11-15"},
        ]
        chosen = fetch_macro.select_monthly_post(rows)
        self.assertEqual(chosen["seq"], 105)

    def test_refresh_metadata_validate_checks_address_spaces_and_prefix(self):
        base = {
            "batch": "2026-11",
            "complexes": [{
                "complex_id": "서울특별시 광진구 구의동|631-1|현대프라임",
                "expected_address": "서울특별시 광진구 구의동 631-1",
                "evidence": "registry",
                "changes": {"address": "서울특별시 광진구 구의동 631-2"},
            }],
        }
        refresh_metadata.validate(base, "2026-11", 2026)

        bad_spaces = json.loads(json.dumps(base))
        bad_spaces["complexes"][0]["changes"]["address"] = "서울특별시  광진구 구의동 631-2"
        with self.assertRaisesRegex(ValueError, "single spaces"):
            refresh_metadata.validate(bad_spaces, "2026-11", 2026)

        bad_prefix = json.loads(json.dumps(base))
        bad_prefix["complexes"][0]["changes"]["address"] = "광진구 구의동 631-2"
        with self.assertRaisesRegex(ValueError, "district"):
            refresh_metadata.validate(bad_prefix, "2026-11", 2026)

    def test_load_home_rows_live_only_includes_address_and_built(self):
        import complex_list

        db = ScriptedDB([])
        rows = complex_list.load_home_rows(db, district="광진구", include_sort=False, live_only=True)
        self.assertEqual(rows, [])
        query, _ = db.calls[0]
        self.assertIn("c.address", query)
        self.assertIn("c.built", query)


class ScriptedDB:
    """Fake connection answering refresh()/recheck_history() queries by prefix and recording every call."""

    def __init__(self, answers):
        self.answers = answers  # list of (sql prefix, rows)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.savepoint_failures = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.savepoint_failures += 1
            raise

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def execute(self, sql, params=None):
        check_placeholders(sql, params)
        q = " ".join(sql.split())
        self.calls.append((q, params))
        rows = []
        for prefix, answer in self.answers:
            if q.startswith(prefix):
                rows = answer
                break

        class Res:
            def __iter__(self_inner):
                return iter(rows)

            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return list(rows)

        return Res()

    def ran(self, prefix):
        return [params for q, params in self.calls if q.startswith(prefix)]


class ApprovalIsolationTests(unittest.TestCase):
    DISTRICT = "11530"

    def test_invalid_identity_decision_is_held_instead_of_raising(self):
        known = "서울특별시 광진구 구의동|631-1|현대프라임"
        source = "서울특별시 광진구 구의동|631-1|구의현대프라임"
        cur = FakeReconcileCursor(
            complexes=[(known, "현대프라임", "서울특별시 광진구 구의동 631-1", 1997)],
            trades=[],
            decisions={source: "서울특별시 광진구 구의동|999|없는단지"},
        )
        trade = (source, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)
        added, removed = refresh.reconcile(
            FakeReconcileDB(cur),
            pd.Period("2026-10", freq="M"),
            {source: (source, "구의현대프라임", "서울특별시 광진구 구의동 631-1", 1997)},
            Counter({trade: 1}),
            "11215",
            refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((added, removed), (0, 0))
        self.assertEqual(cur.inserted_trades, [])
        self.assertEqual(cur.identity_pending[source]["status"], "invalid_decision")
        self.assertEqual(cur.identity_pending[source]["candidates"], [known])

    def test_present_approved_trade_survives_identity_hold(self):
        cid = "서울특별시 광진구 구의동|1|A"
        ambiguous = "서울특별시 광진구 구의동|1|B"
        address = "서울특별시 광진구 구의동 1"
        trade = (cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)
        cur = FakeReconcileCursor([(cid, "A", address, 1997)], [(42, *trade)], missing={42: True})
        result = refresh.reconcile(
            FakeReconcileDB(cur), pd.Period("2026-10", freq="M"),
            {cid: (cid, "A", address, 1997), ambiguous: (ambiguous, "B", address, 1997)},
            Counter({trade: 1, (ambiguous, *trade[1:]): 1}), "11215", refresh_day=date(2026, 11, 25),
        )
        self.assertEqual(result, (0, 0))
        self.assertEqual(cur.trades, [(42, *trade)])
        self.assertNotIn(42, cur.missing)

    def test_publish_pending_survives_macro_failure_and_is_consumed_on_retry(self):
        now = datetime.now(KST)
        today = now.date()
        slot_start = refresh.refresh_slot_start(now)
        month = pd.Period(today, freq="M") - 2
        first = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(today, slot_start)]),
            ("SELECT month FROM (", [(month.start_time.date(),)]),
            ("SELECT 1 FROM refresh_publish_pending", [(1,)]),
        ])
        retry = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(today, slot_start)]),
            ("SELECT 1 FROM refresh_publish_pending", [(1,)]),
        ])
        publishing = ScriptedDB([("SELECT day,added,removed FROM refresh_publish_pending", [(today, 2, 1)])])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=[first, retry, publishing]), \
             patch.object(refresh, "replay_approved_month", return_value=(2, 1)) as replay, \
             patch.object(refresh.subprocess, "run", side_effect=[TimeoutError, None]), \
             patch.object(refresh, "publish", return_value=7) as publish:
            with self.assertRaises(TimeoutError):
                refresh.refresh(self.DISTRICT, "tmp", now=now)
            refresh.refresh(self.DISTRICT, "tmp", now=now)
        replay.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(first.commits, 1)
        self.assertEqual(first.ran("INSERT INTO refresh_publish_pending"), [(self.DISTRICT, today, 2, 1)])
        self.assertEqual(publishing.ran("INSERT INTO district_refresh_runs"), [(self.DISTRICT, today, 2, 1, 7, slot_start)])
        self.assertEqual(publishing.ran("DELETE FROM refresh_publish_pending"), [(self.DISTRICT, today)])

    def test_regular_history_selection_excludes_months_on_approval_backoff(self):
        today = datetime.now(KST).date()
        db = ScriptedDB([])
        with patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "fetch_month") as fetch:
            refresh.recheck_history(self.DISTRICT)
        fetch.assert_not_called()
        query, params = next((q, p) for q, p in db.calls if q.startswith("SELECT m::date"))
        self.assertIn("f.month=m::date AND f.next_retry_on > %s::date", query)
        self.assertEqual(params[2:4], (self.DISTRICT, today))

    def test_replay_failure_is_recorded_and_returns_none(self):
        db = ScriptedDB([("INSERT INTO refresh_approval_failures", [(1,)])])
        month = pd.Period("2025-12", freq="M")
        with patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
             patch.object(refresh, "reconcile", side_effect=ValueError("Empty API month would remove existing trades")):
            self.assertIsNone(refresh.replay_approved_month(db, month, self.DISTRICT, date(2026, 11, 25)))
        (params,) = db.ran("INSERT INTO refresh_approval_failures")
        self.assertEqual(params[:4], (self.DISTRICT, date(2025, 12, 1),
                                      "Empty API month would remove existing trades", date(2026, 11, 25)))
        self.assertEqual(db.ran("DELETE FROM refresh_approval_failures"), [])

    def test_transient_approval_failure_is_available_for_daily_retry(self):
        db = ScriptedDB([])
        with patch.object(refresh, 'fetch_month', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                refresh.replay_approved_month(db, pd.Period('2025-12'), self.DISTRICT, date(2026, 11, 25))
        self.assertEqual(db.ran('INSERT INTO refresh_approval_failures'), [])

    def test_replay_success_clears_failure(self):
        db = ScriptedDB([])
        month = pd.Period("2025-12", freq="M")
        with patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
             patch.object(refresh, "reconcile", return_value=(2, 1)):
            self.assertEqual(refresh.replay_approved_month(db, month, self.DISTRICT, date(2026, 11, 25)), (2, 1))
        self.assertEqual(db.ran("DELETE FROM refresh_approval_failures"), [(self.DISTRICT, date(2025, 12, 1))])

    def test_replay_reason_hides_unknown_messages(self):
        self.assertEqual(refresh.replay_reason(ValueError("Invalid reviewed complex ID for district")),
                         "Invalid reviewed complex ID for district")
        self.assertEqual(refresh.replay_reason(ValueError("secret detail")), "ValueError")
        self.assertEqual(refresh.replay_reason(OSError("network")), "OSError")

    def test_failed_old_approval_does_not_block_daily_window(self):
        now = datetime.now(KST)
        today = now.date()
        slot_start = refresh.refresh_slot_start(now)
        anchor = pd.Period(today, freq="M")
        old = anchor - 11
        first = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at)) FROM district_refresh_runs", [(today - timedelta(days=1), now - timedelta(days=2))]),
            ("SELECT month FROM (", [(old.start_time.date(),)]),
            ("SELECT added,removed FROM daily_trade_checkpoints", []),
            ("INSERT INTO refresh_approval_failures", [(1,)]),
        ])
        second = ScriptedDB([
            ("SELECT 1 FROM district_refresh_runs", []),
            ("SELECT day,added,removed FROM refresh_publish_pending", [(today, 4, 0)]),
        ])
        connections = iter([first, second])
        reconciled = []

        def fake_reconcile(db, month, complexes, rows, district, **kw):
            reconciled.append(month)
            if month == old:
                raise ValueError("Empty API month would remove existing trades")
            return (1, 0)

        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=lambda *a, **kw: next(connections)), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
             patch.object(refresh, "reconcile", side_effect=fake_reconcile), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7) as publish:
            refresh.refresh(self.DISTRICT, "tmp", now=now)

        window = list(pd.period_range(anchor - 3, anchor))
        self.assertEqual(reconciled, window + [old])
        self.assertEqual(len(first.ran("INSERT INTO refresh_approval_failures")), 1)
        (checkpoint,) = first.ran("INSERT INTO daily_trade_checkpoints")
        self.assertEqual(checkpoint[2:], (len(window), 0, slot_start))
        self.assertEqual(first.ran("INSERT INTO refresh_publish_pending"), [(self.DISTRICT, today, len(window), 0)])
        self.assertEqual(first.commits, 1)
        publish.assert_called_once()
        self.assertEqual(len(second.ran("INSERT INTO district_refresh_runs")), 1)

    def test_recheck_runs_once_per_day_even_with_pending_approval(self):
        today = datetime.now(KST).date()
        db = ScriptedDB([("SELECT 1 FROM historical_rechecks", [(1,)])])
        with patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "fetch_month") as fetch:
            refresh.recheck_history(self.DISTRICT)
        fetch.assert_not_called()
        self.assertEqual(db.ran("SELECT month FROM ("), [])
        self.assertEqual(db.ran("SELECT 1 FROM historical_rechecks")[0], (self.DISTRICT, today))

    def test_recheck_failed_approval_backs_off(self):
        today = datetime.now(KST).date()
        old = pd.Period(today, freq="M") - 30
        db = ScriptedDB([
            ("SELECT 1 FROM historical_rechecks", []),
            ("SELECT month FROM (", [(old.start_time.date(),)]),
            ("INSERT INTO refresh_approval_failures", [(3,)]),
        ])
        with patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
             patch.object(refresh, "reconcile",
                          side_effect=ValueError("Empty API month would remove existing trades")):
            refresh.recheck_history(self.DISTRICT)
        self.assertEqual(db.rollbacks, 0)
        self.assertEqual(db.savepoint_failures, 1)
        self.assertEqual(len(db.ran("SELECT pg_advisory_xact_lock")), 1)
        (failed,) = db.ran("INSERT INTO historical_rechecks")
        self.assertEqual(failed, (self.DISTRICT, old.start_time.date(), today, "ValueError"))
        (backoff,) = db.ran("INSERT INTO refresh_approval_failures")
        self.assertEqual(backoff[2], "Empty API month would remove existing trades")
        # actionable_months is asked to drop months still backing off as of today.
        (actionable,) = db.ran("SELECT month FROM (")
        self.assertEqual(actionable[6], today)

    def test_actionable_months_excludes_approvals_blocked_by_identity_pending(self):
        today = date(2026, 11, 25)
        db = ScriptedDB([("SELECT month FROM (", [])])
        result = refresh.actionable_months(db, self.DISTRICT, today)
        self.assertEqual(result, [])
        query, params = db.calls[0]
        self.assertIn("WITH RECURSIVE decision_walk", query)
        self.assertIn("JOIN trades t USING (trade_id)", query)
        self.assertIn("refresh_identity_pending p", query)
        self.assertIn("t.complex_id = ANY(p.candidates)", query)
        self.assertEqual(
            params,
            (self.DISTRICT, self.DISTRICT, self.DISTRICT, "구로구", self.DISTRICT, self.DISTRICT, today, self.DISTRICT, today),
        )

    def test_recheck_history_advances_regular_queue_when_approval_is_blocked_by_identity(self):
        today = datetime.now(KST).date()
        regular_month = date(2025, 1, 1)
        db = ScriptedDB([
            ("SELECT 1 FROM historical_rechecks", []),
            ("SELECT month FROM (", []),
            ("SELECT m::date FROM generate_series", [(regular_month,)]),
        ])
        with patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())) as fetch, \
             patch.object(refresh, "reconcile", return_value=(0, 0)):
            refresh.recheck_history(self.DISTRICT)
        fetch.assert_called_once_with("202501", self.DISTRICT)
        (actionable_q, _) = next((q, p) for q, p in db.calls if q.startswith("SELECT month FROM ("))
        self.assertIn("t.complex_id = ANY(p.candidates)", actionable_q)

    def test_recheck_in_model_window_enqueues_publish_and_triggers_republish(self):
        today = datetime.now(KST).date()
        in_window = (pd.Period(today, freq="M") - 6).start_time.date()
        db = ScriptedDB([
            ("SELECT 1 FROM historical_rechecks", []),
            ("SELECT month FROM (", []),
            ("SELECT m::date FROM generate_series", [(in_window,)]),
        ])
        with patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
             patch.object(refresh, "reconcile", return_value=(1, 0)):
            needs_republish = refresh.recheck_history(self.DISTRICT)
        self.assertTrue(needs_republish)
        self.assertEqual(db.ran("INSERT INTO refresh_publish_pending"), [(self.DISTRICT, today, 1, 0)])

        # refresh_many calls refresh() a second time when recheck_history returns True.
        with patch.object(refresh, "refresh") as mock_refresh, \
             patch.object(refresh, "recheck_history", return_value=True):
            failed = refresh.refresh_many([self.DISTRICT])
        self.assertEqual(failed, [])
        self.assertEqual(mock_refresh.call_count, 2)

    def test_recheck_outside_window_or_zero_changes_does_not_republish(self):
        today = datetime.now(KST).date()
        old_month = (pd.Period(today, freq="M") - 30).start_time.date()
        in_window = (pd.Period(today, freq="M") - 6).start_time.date()
        for target_month, delta in ((old_month, (1, 0)), (in_window, (0, 0))):
            db = ScriptedDB([
                ("SELECT 1 FROM historical_rechecks", []),
                ("SELECT month FROM (", []),
                ("SELECT m::date FROM generate_series", [(target_month,)]),
            ])
            with patch.object(refresh, "ensure_schema"), \
                 patch.object(refresh.psycopg, "connect", return_value=db), \
                 patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
                 patch.object(refresh, "reconcile", return_value=delta):
                needs_republish = refresh.recheck_history(self.DISTRICT)
            self.assertFalse(needs_republish)
            self.assertEqual(db.ran("INSERT INTO refresh_publish_pending"), [])

    def test_out_of_window_same_day_approval_accumulates_publish_pending(self):
        now = datetime.now(KST)
        today = now.date()
        slot_start = refresh.refresh_slot_start(now)
        old_month = pd.Period(today, freq="M") - 30
        first = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(today, slot_start)]),
            ("SELECT month FROM (", [(old_month.start_time.date(),)]),
            ("SELECT 1 FROM refresh_publish_pending", [(1,)]),
        ])
        publishing = ScriptedDB([
            ("SELECT day,added,removed FROM refresh_publish_pending", [(today, 2, 1)]),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=[first, publishing]), \
             patch.object(refresh, "replay_approved_month", return_value=(2, 1)), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh(self.DISTRICT, "tmp", now=now)
        self.assertEqual(first.ran("INSERT INTO refresh_publish_pending"), [(self.DISTRICT, today, 2, 1)])
        self.assertEqual(publishing.ran("INSERT INTO district_refresh_runs"), [(self.DISTRICT, today, 2, 1, 7, slot_start)])

    def test_previous_day_publish_pending_is_credited_to_its_own_day(self):
        now = datetime.now(KST)
        today = now.date()
        slot_start = refresh.refresh_slot_start(now)
        yesterday = today - timedelta(days=1)
        yesterday_slot = datetime.combine(yesterday, refresh.time(6), KST)
        first = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(today, slot_start)]),
            ("SELECT month FROM (", []),
            ("SELECT 1 FROM refresh_publish_pending", [(1,)]),
        ])
        publishing = ScriptedDB([
            ("SELECT day,added,removed FROM refresh_publish_pending", [(yesterday, 3, 0), (today, 1, 0)]),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=[first, publishing]), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh(self.DISTRICT, "tmp", now=now)
        runs = publishing.ran("INSERT INTO district_refresh_runs")
        self.assertIn((self.DISTRICT, yesterday, 3, 0, 7, yesterday_slot), runs)
        self.assertIn((self.DISTRICT, today, 1, 0, 7, slot_start), runs)

    def test_checkpoint_publish_rereads_checkpoint_and_pending_under_lock(self):
        now = datetime.now(KST)
        today = now.date()
        slot_start = refresh.refresh_slot_start(now)
        prev_ts = now - timedelta(days=2)
        first = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(today - timedelta(days=1), prev_ts)]),
            ("SELECT month FROM (", []),
            ("SELECT added,removed FROM daily_trade_checkpoints", [(4, 0)]),
        ])
        publishing = ScriptedDB([
            ("SELECT 1 FROM district_refresh_runs", []),
            ("SELECT day,added,removed FROM refresh_publish_pending", [(today, 6, 0)]),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=[first, publishing]), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh(self.DISTRICT, "tmp", now=now)
        self.assertEqual(publishing.ran("INSERT INTO district_refresh_runs"), [(self.DISTRICT, today, 6, 0, 7, slot_start)])
        # Also verify checkpoint+approval path writes to refresh_publish_pending, not UPDATE daily_trade_checkpoints.
        old_month = pd.Period(today, freq="M") - 5
        first2 = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(today - timedelta(days=1), prev_ts)]),
            ("SELECT month FROM (", [(old_month.start_time.date(),)]),
            ("SELECT added,removed FROM daily_trade_checkpoints", [(4, 0)]),
        ])
        publishing2 = ScriptedDB([
            ("SELECT 1 FROM district_refresh_runs", []),
            ("SELECT day,added,removed FROM refresh_publish_pending", [(today, 6, 0)]),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=[first2, publishing2]), \
             patch.object(refresh, "replay_approved_month", return_value=(2, 0)), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh(self.DISTRICT, "tmp", now=now)
        self.assertEqual(first2.ran("UPDATE daily_trade_checkpoints"), [])
        self.assertEqual(first2.ran("INSERT INTO refresh_publish_pending"), [(self.DISTRICT, today, 2, 0)])
        self.assertEqual(publishing2.ran("INSERT INTO district_refresh_runs"), [(self.DISTRICT, today, 6, 0, 7, slot_start)])

    def test_checkpoint_publish_failure_across_midnight_preserves_prior_day_counts(self):
        day1 = datetime(2026, 11, 25, 23, 50, tzinfo=KST)
        day2 = datetime(2026, 11, 26, 6, 10, tzinfo=KST)
        state = {"checkpoints": {}, "pending": {}, "runs": {}}

        class StatefulRefreshDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def commit(self):
                pass

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                rows = []
                if q.startswith("SELECT max(day), max(COALESCE(slot_start, finished_at))"):
                    d_runs = [(dy, v[3]) for (d, dy), v in state["runs"].items() if d == params[0]]
                    rows = [(max(dy for dy, _ in d_runs), max(sl for _, sl in d_runs))] if d_runs else [(None, None)]
                elif q.startswith("SELECT month FROM ("):
                    rows = []
                elif q.startswith("SELECT added,removed FROM daily_trade_checkpoints"):
                    d, dy, sl = params
                    cp = state["checkpoints"].get((d, dy))
                    rows = [(cp[0], cp[1])] if cp and cp[2] >= sl else []
                elif q.startswith("INSERT INTO daily_trade_checkpoints"):
                    d, dy, a, r, sl = params
                    state["checkpoints"][(d, dy)] = (a, r, sl)
                elif q.startswith("INSERT INTO refresh_publish_pending"):
                    d, dy, a, r = params
                    pa, pr = state["pending"].get((d, dy), (0, 0))
                    state["pending"][(d, dy)] = (pa + a, pr + r)
                elif q.startswith("SELECT 1 FROM district_refresh_runs"):
                    d, sl = params
                    rows = [(1,)] if any(dk == d and v[3] >= sl for (dk, _), v in state["runs"].items()) else []
                elif q.startswith("SELECT day,added,removed FROM refresh_publish_pending"):
                    d, dy = params
                    rows = sorted((k_dy, a, r) for (k_d, k_dy), (a, r) in state["pending"].items() if k_d == d and k_dy <= dy)
                elif q.startswith("INSERT INTO district_refresh_runs"):
                    d, dy, a, r, n, sl = params
                    prev = state["runs"].get((d, dy))
                    if prev:
                        state["runs"][(d, dy)] = (prev[0] + a, prev[1] + r, n, max(prev[3], sl))
                    else:
                        state["runs"][(d, dy)] = (a, r, n, sl)
                elif q.startswith("DELETE FROM refresh_publish_pending"):
                    d, dy = params
                    state["pending"] = {k: v for k, v in state["pending"].items() if not (k[0] == d and k[1] <= dy)}

                class Res:
                    def fetchone(self_inner):
                        return rows[0] if rows else None

                    def fetchall(self_inner):
                        return list(rows)

                return Res()

        first_call = [True]

        def rec_day1(*a, **kw):
            if first_call[0]:
                first_call[0] = False
                return (2, 1)
            return (0, 0)

        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=lambda *a, **kw: StatefulRefreshDB()), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
             patch.object(refresh, "reconcile", side_effect=rec_day1), \
             patch.object(refresh.subprocess, "run", side_effect=RuntimeError("macro fail")):
            with self.assertRaises(RuntimeError):
                refresh.refresh(self.DISTRICT, "tmp", now=day1)

        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=lambda *a, **kw: StatefulRefreshDB()), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
             patch.object(refresh, "reconcile", return_value=(0, 0)), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh(self.DISTRICT, "tmp", now=day2)

        self.assertEqual(state["runs"][(self.DISTRICT, day1.date())][:3], (2, 1, 7))
        self.assertEqual(state["runs"][(self.DISTRICT, day2.date())][:3], (0, 0, 7))
        self.assertEqual(state["pending"], {})

    def test_legacy_unmigrated_checkpoint_is_migrated_once_to_publish_pending(self):
        now = datetime(2026, 11, 25, 6, 20, tzinfo=KST)
        today = now.date()
        slot_start = refresh.refresh_slot_start(now)
        # Pre-upgrade checkpoint had added=2, removed=1, created_at=06:05, slot_start=None, and nothing in refresh_publish_pending.
        created_at = datetime(2026, 11, 25, 6, 5, tzinfo=KST)
        first = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(today - timedelta(days=1), slot_start - timedelta(days=1))]),
            ("SELECT month FROM (", []),
            ("SELECT day, added, removed, created_at FROM daily_trade_checkpoints WHERE district = %s AND slot_start IS NULL", [(today, 2, 1, created_at)]),
            ("SELECT 1 FROM district_refresh_runs WHERE district=%s AND day=%s AND finished_at >= %s", []),
            ("SELECT added,removed FROM daily_trade_checkpoints", [(2, 1)]),
        ])
        publishing = ScriptedDB([
            ("SELECT 1 FROM district_refresh_runs", []),
            ("SELECT day,added,removed FROM refresh_publish_pending", [(today, 2, 1)]),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=[first, publishing]), \
             patch.object(refresh, "fetch_month") as fetch, \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh(self.DISTRICT, "tmp", now=now)
        fetch.assert_not_called()
        self.assertEqual(first.ran("INSERT INTO refresh_publish_pending"), [(self.DISTRICT, today, 2, 1)])
        self.assertEqual(publishing.ran("INSERT INTO district_refresh_runs"), [(self.DISTRICT, today, 2, 1, 7, slot_start)])

    def test_slot_start_0550_then_0600_runs_both_slots(self):
        t_0550 = datetime(2026, 11, 25, 5, 50, tzinfo=KST)
        t_0600 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        self.assertEqual(refresh.refresh_slot_start(t_0550), datetime(2026, 11, 24, 6, 0, tzinfo=KST))
        self.assertEqual(refresh.refresh_slot_start(t_0600), datetime(2026, 11, 25, 6, 0, tzinfo=KST))

        # After a 05:50 run (slot_start=11-24 06:00), a 06:00 run must NOT skip even though day==today.
        first = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(t_0550.date(), refresh.refresh_slot_start(t_0550))]),
            ("SELECT month FROM (", []),
            ("SELECT added,removed FROM daily_trade_checkpoints", []),
        ])
        publishing = ScriptedDB([
            ("SELECT 1 FROM district_refresh_runs", []),
            ("SELECT day,added,removed FROM refresh_publish_pending", [(t_0600.date(), 4, 0)]),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=[first, publishing]), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())) as fetch, \
             patch.object(refresh, "reconcile", return_value=(1, 0)), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh(self.DISTRICT, "tmp", now=t_0600)
        self.assertGreater(fetch.call_count, 0)

    def test_slot_start_0555_to_0605_does_not_skip_0600_slot(self):
        t_0555 = datetime(2026, 11, 25, 5, 55, tzinfo=KST)
        t_0610 = datetime(2026, 11, 25, 6, 10, tzinfo=KST)
        slot_0555 = refresh.refresh_slot_start(t_0555)  # 2026-11-24 06:00 KST
        # Run started at 05:55 and finished at 06:05 recorded slot_start = 2026-11-24 06:00.
        first = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(t_0555.date(), slot_0555)]),
            ("SELECT month FROM (", []),
            ("SELECT added,removed FROM daily_trade_checkpoints", []),
        ])
        publishing = ScriptedDB([
            ("SELECT 1 FROM district_refresh_runs", []),
            ("SELECT day,added,removed FROM refresh_publish_pending", []),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=[first, publishing]), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())) as fetch, \
             patch.object(refresh, "reconcile", return_value=(0, 0)), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh(self.DISTRICT, "tmp", now=t_0610)
        self.assertGreater(fetch.call_count, 0)

    def test_slot_start_0610_then_0620_skips_second_run(self):
        t_0610 = datetime(2026, 11, 25, 6, 10, tzinfo=KST)
        t_0620 = datetime(2026, 11, 25, 6, 20, tzinfo=KST)
        db = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(t_0610.date(), refresh.refresh_slot_start(t_0610))]),
            ("SELECT month FROM (", []),
            ("SELECT 1 FROM refresh_publish_pending", []),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "fetch_month") as fetch:
            refresh.refresh(self.DISTRICT, "tmp", now=t_0620)
        fetch.assert_not_called()

    def test_slot_start_0550_skips_if_yesterday_0600_completed(self):
        t_prev = datetime(2026, 11, 24, 6, 10, tzinfo=KST)
        t_0550 = datetime(2026, 11, 25, 5, 50, tzinfo=KST)
        db = ScriptedDB([
            ("SELECT max(day), max(COALESCE(slot_start, finished_at))", [(t_prev.date(), refresh.refresh_slot_start(t_prev))]),
            ("SELECT month FROM (", []),
            ("SELECT 1 FROM refresh_publish_pending", []),
        ])
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "x"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "fetch_month") as fetch:
            refresh.refresh(self.DISTRICT, "tmp", now=t_0550)
        fetch.assert_not_called()

    def test_worker_attempts_persist_across_restart_and_cap_at_four(self):
        store = refresh.MemoryAttemptStore()
        calls = 0

        def failing_job():
            nonlocal calls
            calls += 1
            return ["11530"]

        t0 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        # 2 failures before restart
        wait1 = refresh.run_slot("daily", failing_job, t0, store, max_retries=3, retry_delay=600)
        self.assertEqual(wait1, 600)
        wait2 = refresh.run_slot("daily", failing_job, t0 + timedelta(minutes=10), store, max_retries=3, retry_delay=600)
        self.assertEqual(wait2, 600)
        self.assertEqual(calls, 2)

        # Restart before next_attempt_at waits without running job
        wait_early = refresh.run_slot("daily", failing_job, t0 + timedelta(minutes=15), store, max_retries=3, retry_delay=600)
        self.assertEqual(wait_early, 300)
        self.assertEqual(calls, 2)

        # Remaining 2 attempts (attempt 3 and 4) run, then exhausted
        wait3 = refresh.run_slot("daily", failing_job, t0 + timedelta(minutes=20), store, max_retries=3, retry_delay=600)
        self.assertEqual(wait3, 600)
        wait4 = refresh.run_slot("daily", failing_job, t0 + timedelta(minutes=30), store, max_retries=3, retry_delay=600)
        self.assertGreater(wait4, 600)
        self.assertEqual(calls, 4)

        # Restart after exhausted skips until next 06:00 KST
        wait5 = refresh.run_slot("daily", failing_job, t0 + timedelta(minutes=40), store, max_retries=3, retry_delay=600)
        self.assertGreater(wait5, 600)
        self.assertEqual(calls, 4)

    def test_worker_skips_same_slot_after_done_on_restart(self):
        store = refresh.MemoryAttemptStore()
        calls = 0

        def ok_job():
            nonlocal calls
            calls += 1
            return []

        t0 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        wait1 = refresh.run_slot("daily", ok_job, t0, store, max_retries=3, retry_delay=600)
        self.assertEqual(calls, 1)
        self.assertEqual(wait1, 24 * 3600)

        # Restart 1 hour later in the same slot must skip
        wait2 = refresh.run_slot("daily", ok_job, t0 + timedelta(hours=1), store, max_retries=3, retry_delay=600)
        self.assertEqual(calls, 1)
        self.assertEqual(wait2, 23 * 3600)

    def test_worker_falls_back_to_memory_when_db_store_fails(self):
        # Fail-closed policy (#4): while DB is unreachable, independent workers must defer rather than both claiming in memory.
        store_a = refresh.DbAttemptStore()
        store_b = refresh.DbAttemptStore()
        calls = 0

        def job():
            nonlocal calls
            calls += 1
            return []

        t0 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        with patch.object(refresh.psycopg, "connect", side_effect=OSError("db down")):
            wait_a1 = refresh.run_slot("daily", job, t0, store_a, max_retries=3, retry_delay=600)
            wait_b1 = refresh.run_slot("daily", job, t0, store_b, max_retries=3, retry_delay=600)
            wait_a2 = refresh.run_slot("daily", job, t0 + timedelta(minutes=5), store_a, max_retries=3, retry_delay=600)
        self.assertEqual(calls, 0)
        self.assertEqual(wait_a1, 600)
        self.assertEqual(wait_b1, 600)
        self.assertEqual(wait_a2, 300)

    def test_run_slot_computes_wait_and_retry_from_finish_time_and_next_slot(self):
        store = refresh.MemoryAttemptStore()
        # 06:00 start, 06:40 finish -> sleep 23h 20m (wake at next day 06:00, not 06:40)
        times = iter([
            datetime(2026, 11, 25, 6, 0, tzinfo=KST),
            datetime(2026, 11, 25, 6, 40, tzinfo=KST),
        ])
        wait = refresh.run_slot("daily", lambda: [], store=store, now_fn=lambda: next(times))
        self.assertEqual(wait, 23 * 3600 + 20 * 60)

        # 05:50 start (previous slot), 06:10 finish -> wake immediately (1s) for today's 06:00 slot
        store_boundary = refresh.MemoryAttemptStore()
        times_b = iter([
            datetime(2026, 11, 25, 5, 50, tzinfo=KST),
            datetime(2026, 11, 25, 6, 10, tzinfo=KST),
        ])
        wait_b = refresh.run_slot("daily", lambda: [], store=store_boundary, now_fn=lambda: next(times_b))
        self.assertEqual(wait_b, 1)

        # 06:00 start, 06:15 failure -> next_attempt_at is 06:25 (finish + 10m), not 06:10
        store_fail = refresh.MemoryAttemptStore()
        times_f = iter([
            datetime(2026, 11, 25, 6, 0, tzinfo=KST),
            datetime(2026, 11, 25, 6, 15, tzinfo=KST),
        ])
        wait_f = refresh.run_slot("daily", lambda: ["11530"], store=store_fail, retry_delay=600, now_fn=lambda: next(times_f))
        self.assertEqual(wait_f, 600)
        slot_0600 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        self.assertEqual(
            store_fail.peek("daily", slot_0600, datetime(2026, 11, 25, 6, 16, tzinfo=KST))[2],
            datetime(2026, 11, 25, 6, 25, tzinfo=KST),
        )

    def test_worker_attempt_limit_after_crash_and_concurrent_claim_exclusion(self):
        from threading import Barrier, Event, Thread

        store = refresh.MemoryAttemptStore()
        t0 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        # Simulate SIGKILL right after 4th attempt was recorded (attempts=4, status='running', lease set).
        store.sync("daily", t0, 4, "running", None, t0 + timedelta(hours=2))
        ran = 0

        def should_not_run():
            nonlocal ran
            ran += 1
            return ["11530"]

        wait = refresh.run_slot("daily", should_not_run, t0 + timedelta(minutes=31), store, max_retries=3, retry_delay=600)
        self.assertEqual(ran, 0)
        self.assertGreater(wait, 600)
        self.assertEqual(store.peek("daily", t0, t0 + timedelta(minutes=31))[1], "running")
        after_lease = t0 + timedelta(hours=2, seconds=1)
        refresh.run_slot("daily", should_not_run, after_lease, store, max_retries=3, retry_delay=600)
        self.assertEqual(store.peek("daily", t0, after_lease)[1], "exhausted")
        self.assertEqual(ran, 0)

        # Simulate BaseException crash mid-attempt 4 via run_slot -> finally settles to exhausted, no 5th run.
        store_be = refresh.MemoryAttemptStore()
        for m in (0, 10, 20):
            refresh.run_slot("daily", lambda: ["11530"], t0 + timedelta(minutes=m), store_be, max_retries=3, retry_delay=600)

        def crash_4th():
            raise KeyboardInterrupt("simulated kill")

        with self.assertRaises(KeyboardInterrupt):
            refresh.run_slot("daily", crash_4th, t0 + timedelta(minutes=30), store_be, max_retries=3, retry_delay=600)
        self.assertEqual(store_be.peek("daily", t0, t0 + timedelta(minutes=30))[1], "exhausted")
        refresh.run_slot("daily", should_not_run, t0 + timedelta(minutes=31), store_be, max_retries=3, retry_delay=600)
        self.assertEqual(ran, 0)

        # Two workers racing for the same slot: only one claims and executes.
        race_store = refresh.MemoryAttemptStore()
        barrier = Barrier(2)
        started = Event()
        release = Event()
        race_runs = []

        def slow_job():
            race_runs.append(1)
            started.set()
            release.wait(timeout=2)
            return []

        def worker_fn():
            barrier.wait()
            refresh.run_slot("daily", slow_job, t0, race_store, max_retries=3, retry_delay=600)

        t1 = Thread(target=worker_fn)
        t2 = Thread(target=worker_fn)
        t1.start()
        t2.start()
        started.wait(timeout=2)
        release.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(len(race_runs), 1)

    def test_db_attempt_store_reconciles_memory_fallback_on_recovery(self):
        store = refresh.DbAttemptStore()
        db_rows = {}
        t0 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)

        class FakeAttemptDB:
            def __init__(self, up):
                self.up = up

            def __enter__(self):
                if not self.up:
                    raise OSError("db down")
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                rows = []
                rowcount = 1
                if q.startswith("INSERT INTO refresh_worker_attempts") and "DO NOTHING" in q:
                    db_rows.setdefault((params[0], params[1]), [0, "deferred", None, None, None])
                elif q.startswith("SELECT attempts, status, next_attempt_at, lease_until"):
                    r = db_rows.get((params[0], params[1]))
                    if r is not None:
                        rows = [tuple(r)]
                elif q.startswith("UPDATE refresh_worker_attempts SET status='deferred', lease_until=NULL"):
                    w, sl = params
                    db_rows[(w, sl)][1] = "deferred"
                    db_rows[(w, sl)][3:] = [None, None]
                elif q.startswith("UPDATE refresh_worker_attempts SET attempts=%s, status=%s, next_attempt_at=%s, lease_until=%s, claim_token=%s"):
                    att, st, nxt, lease, tok, w, sl = params
                    db_rows[(w, sl)] = [att, st, nxt, lease, tok]
                elif q.startswith("UPDATE refresh_worker_attempts SET attempts=%s, status='running'"):
                    att, lease, tok, w, sl = params
                    db_rows[(w, sl)] = [att, "running", None, lease, tok]
                elif q.startswith("UPDATE refresh_worker_attempts SET status='exhausted'"):
                    w, sl = params
                    db_rows[(w, sl)][1:] = ["exhausted", None, None, None]
                elif q.startswith("UPDATE refresh_worker_attempts SET lease_until = %s"):
                    new_lease, w, sl, tok = params
                    cur = db_rows.get((w, sl))
                    if cur and cur[1] == "running" and cur[4] == tok:
                        cur[3] = new_lease
                        rowcount = 1
                    else:
                        rowcount = 0
                elif q.startswith("INSERT INTO refresh_worker_attempts") and "ON CONFLICT (worker, slot_start) DO UPDATE" in q:
                    w, sl, att, st, nxt, tok = params
                    prev = db_rows.get((w, sl))
                    if prev is None or prev[4] == tok or (prev[4] is None and prev[0] < att and prev[1] not in ("done", "exhausted")):
                        rec = max(prev[0], att) if prev else att
                        db_rows[(w, sl)] = [rec, st, nxt, None, None]
                        rows = [(rec,)]

                class Res:
                    def __init__(self_inner):
                        self_inner.rowcount = rowcount

                    def fetchone(self_inner):
                        return rows[0] if rows else None

                return Res()

        calls = 0

        def fail_job():
            nonlocal calls
            calls += 1
            return ["11530"]

        # 06:00: DB is reachable during claim (attempt 1 runs), then DB drops during finish -> recorded in fallback with retry at 06:10.
        conns = iter([FakeAttemptDB(True), FakeAttemptDB(False)])
        with patch.object(refresh.psycopg, "connect", side_effect=lambda **kw: next(conns)):
            wait1 = refresh.run_slot("daily", fail_job, t0, store, max_retries=3, retry_delay=600)
        self.assertEqual(wait1, 600)
        self.assertEqual(calls, 1)

        # 06:01: DB recovers -> must honor 06:10 cooldown (540s remaining) and sync deferred status to DB.
        with patch.object(refresh.psycopg, "connect", side_effect=lambda **kw: FakeAttemptDB(True)):
            wait2 = refresh.run_slot("daily", fail_job, t0 + timedelta(minutes=1), store, max_retries=3, retry_delay=600)
        self.assertEqual(calls, 1)
        self.assertEqual(wait2, 540)
        self.assertEqual(db_rows[("daily", t0)][0], 1)
        self.assertEqual(db_rows[("daily", t0)][1], "deferred")

        # 06:10: retry due -> runs as attempt 2 (not reset to 1).
        with patch.object(refresh.psycopg, "connect", side_effect=lambda **kw: FakeAttemptDB(True)):
            wait3 = refresh.run_slot("daily", fail_job, t0 + timedelta(minutes=10), store, max_retries=3, retry_delay=600)
        self.assertEqual(calls, 2)
        self.assertEqual(wait3, 600)
        self.assertEqual(db_rows[("daily", t0)][0], 2)

    def test_expired_worker_fencing_and_lease_heartbeat_renewal(self):
        import time as pytime

        store = refresh.MemoryAttemptStore()
        t0 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        # Worker 1 claims at 06:00 with 2h lease.
        out1, (att1, tok1) = store.claim_attempt("daily", t0, t0, max_attempts=4, lease_seconds=7200)
        self.assertEqual((out1, att1), ("claimed", 1))

        # At 08:01 Worker 1's lease has expired -> Worker 2 claims attempt 2.
        t_0801 = t0 + timedelta(hours=2, minutes=1)
        out2, (att2, tok2) = store.claim_attempt("daily", t0, t_0801, max_attempts=4, lease_seconds=7200)
        self.assertEqual((out2, att2), ("claimed", 2))
        self.assertNotEqual(tok1, tok2)

        # At 08:05 stale Worker 1 finishes with 'deferred' -> must be rejected by fencing token!
        t_0805 = t0 + timedelta(hours=2, minutes=5)
        self.assertFalse(
            store.finish("daily", t0, "deferred", t_0805 + timedelta(minutes=10), attempts=att1, claim_token=tok1)
        )
        # Worker 3 arrives at 08:06 while Worker 2 is still running -> must NOT claim attempt 3!
        out3, wait3 = store.claim_attempt("daily", t0, t0 + timedelta(hours=2, minutes=6), max_attempts=4, lease_seconds=7200)
        self.assertEqual(out3, "skip")
        self.assertGreater(wait3, 3600)

        # Active Worker 2 can still finish with its valid token tok2.
        self.assertTrue(store.finish("daily", t0, "done", None, attempts=att2, claim_token=tok2))

        # Verify lease heartbeat renewal during long-running job in run_slot.
        hb_store = refresh.MemoryAttemptStore()
        current_time = [t0]

        def slow_job_with_clock_advance():
            current_time[0] = t0 + timedelta(hours=1, minutes=30)
            pytime.sleep(0.06)
            return []

        refresh.run_slot(
            "daily",
            slow_job_with_clock_advance,
            store=hb_store,
            now_fn=lambda: current_time[0],
            lease_seconds=7200,
            heartbeat_interval=0.015,
        )
        self.assertEqual(hb_store.peek("daily", t0, current_time[0])[1], "done")

    def test_legacy_running_row_with_next_attempt_at_is_treated_as_deferred(self):
        store = refresh.MemoryAttemptStore()
        t0 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        # Pre-upgrade row had status='running' and next_attempt_at=06:10 with lease_until=None.
        store.sync("daily", t0, 1, "running", t0 + timedelta(minutes=10), None, None)
        calls = 0

        def job():
            nonlocal calls
            calls += 1
            return []

        wait = refresh.run_slot("daily", job, t0 + timedelta(minutes=1), store, max_retries=3, retry_delay=600)
        self.assertEqual(calls, 0)
        self.assertEqual(wait, 540)
        self.assertEqual(store.peek("daily", t0, t0 + timedelta(minutes=1))[1], "deferred")

    def test_single_district_worker_scope_does_not_block_full_seoul_worker(self):
        from pathlib import Path

        store = refresh.MemoryAttemptStore()
        t0 = datetime(2026, 11, 25, 6, 0, tzinfo=KST)
        single_daily, single_meta_auto = refresh.worker_names("11215", None)
        _, single_meta_dir = refresh.worker_names("11215", Path("/metadata"))
        full_daily, full_meta = refresh.worker_names(None, Path("/metadata"))
        auto_only_daily, auto_only_meta = refresh.worker_names(None, None)

        self.assertEqual((single_daily, single_meta_auto), ("daily:11215", "metadata:11215:auto-only"))
        self.assertEqual(single_meta_dir, "metadata:11215")
        self.assertEqual((full_daily, full_meta), ("daily", "metadata"))
        self.assertEqual((auto_only_daily, auto_only_meta), ("daily", "metadata:auto-only"))

        ran = []
        refresh.run_slot(single_daily, lambda: (ran.append("single_daily") or []), t0, store)
        refresh.run_slot(single_meta_auto, lambda: (ran.append("single_meta_auto") or []), t0, store)
        # Same district with --metadata-dir added in the same slot must still run (#9)!
        refresh.run_slot(single_meta_dir, lambda: (ran.append("single_meta_dir") or []), t0 + timedelta(minutes=2), store)
        refresh.run_slot(full_daily, lambda: (ran.append("full_daily") or []), t0 + timedelta(minutes=5), store)
        refresh.run_slot(full_meta, lambda: (ran.append("full_meta") or []), t0 + timedelta(minutes=5), store)
        self.assertEqual(ran, ["single_daily", "single_meta_auto", "single_meta_dir", "full_daily", "full_meta"])


class SqlPlaceholderTest(unittest.TestCase):
    MODULES = (
        "apply_lifecycle_baseline.py",
        "automatic_metadata.py",
        "complex_list.py",
        "delete_complexes.py",
        "facilities.py",
        "train_seoul.py",
        "features.py",
        "fetch_macro.py",
        "lifecycle_admin.py",
        "lifecycle_check/__init__.py",
        "lifecycle_check/collect.py",
        "lifecycle_check/common.py",
        "lifecycle_check/demolition.py",
        "lifecycle_check/existence.py",
        "lifecycle_check/facts.py",
        "lifecycle_check/monthly.py",
        "lifecycle_check/sources.py",
        "lifecycle_check/stage.py",
        "lifecycle_check/store.py",
        "lifecycle_check/targets.py",
        "lifecycle_inbox.py",
        "lifecycle_results.py",
        "main.py",
        "refresh.py",
        "refresh_metadata.py",
    )

    def test_checker_rejects_bare_percent(self):
        with self.assertRaises(AssertionError):
            check_placeholders("DELETE FROM kapt_metadata_cache WHERE batch LIKE ('kapt-%-' || %s) AND batch < %s", ("11530", "b"))
        check_placeholders("DELETE FROM kapt_metadata_cache WHERE batch LIKE %s AND batch < %s", ("kapt-%-11530", "b"))

    def test_literal_sql_in_backend_is_psycopg3_safe(self):
        base = os.path.dirname(os.path.abspath(__file__))
        checked = 0
        for name in self.MODULES:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("execute", "executemany") and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    sql = node.args[0].value
                    with self.subTest(module=name, line=node.lineno):
                        self.assertIsNone(_BAD_PCT.search(sql), sql)
                    checked += 1
        self.assertGreater(checked, 20)

    def test_kapt_cache_cleanup_uses_district_pattern(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "automatic_metadata.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertIn('WHERE batch LIKE %s AND batch < %s', source)
        self.assertIn('(f"kapt-%-{district}", batch)', source)


class RefreshDesignAcceptanceTests(unittest.TestCase):
    """Acceptance scenario verification tests for plan_refresh.md Section 6."""

    def test_acceptance_1_duplicate_multiplicity_partial_cancel_and_missing_revision_hold(self):
        cid = "서울특별시 광진구 구의동|631-1|현대프라임"
        addr = "서울특별시 광진구 구의동 631-1"
        active_key = (cid, 85, date(2026, 10, 10), Decimal("84.97"), 10, Decimal("120000"), False)
        cancel_key = (*active_key[:-1], True)

        cur = FakeReconcileCursor(
            complexes=[(cid, "현대프라임", addr, 1997)],
            trades=[],
        )
        # Collect same month twice with 2 identical active trades -> multiplicity=2 preserved.
        a1, r1 = refresh.reconcile(
            FakeReconcileDB(cur), pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", addr, 1997)},
            Counter({active_key: 2}), "11215", refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a1, r1), (2, 0))
        self.assertEqual(len(cur.trades), 2)

        a2, r2 = refresh.reconcile(
            FakeReconcileDB(cur), pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", addr, 1997)},
            Counter({active_key: 2}), "11215", refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a2, r2), (0, 0))
        self.assertEqual(len(cur.trades), 2)

        # Next collection: 1 of the 2 identical trades is explicitly cancelled -> 1 remains active, 1 cancelled.
        a3, r3 = refresh.reconcile(
            FakeReconcileDB(cur), pd.Period("2026-10", freq="M"),
            {cid: (cid, "현대프라임", addr, 1997)},
            Counter({active_key: 1, cancel_key: 1}), "11215", refresh_day=date(2026, 11, 26),
        )
        self.assertEqual((a3, r3), (1, 1))
        active_rows = [row for row in cur.trades if not row[-1]]
        cancelled_rows = [row for row in cur.trades if row[-1]]
        self.assertEqual(len(active_rows), 1)
        self.assertEqual(len(cancelled_rows), 1)
        self.assertEqual(len(cur.trade_revisions), 1)
        self.assertEqual(cur.trade_revisions[0][-1], "cancelled")

    def test_acceptance_2_rename_new_ambiguous_and_same_lot_name_rebuild(self):
        import molit

        old_cid = "서울특별시 광진구 구의동|631-1|현대프라임"
        addr = "서울특별시 광진구 구의동 631-1"
        new_rebuild_cid = refresh.issue_rebuild_complex_id(old_cid, 2026, [old_cid])
        self.assertEqual(new_rebuild_cid, "서울특별시 광진구 구의동|631-1|현대프라임#2026")
        self.assertEqual(new_rebuild_cid.split(" ")[1], "광진구")

        # 1) Unverified same-lot, same-name trade with different buildYear (2026 vs 1997) is held, not mixed.
        old_active = (old_cid, 85, date(2026, 10, 5), Decimal("84.97"), 10, Decimal("120000"), False)
        cur_hold = FakeReconcileCursor(
            complexes=[(old_cid, "현대프라임", addr, 1997)],
            trades=[(101, *old_active)],
        )
        tc_unverified = molit.TradeCounts(
            {old_active: 1, (old_cid, 85, date(2026, 10, 20), Decimal("84.97"), 15, Decimal("180000"), False): 1},
            by_built={
                (*old_active, 1997): 1,
                (old_cid, 85, date(2026, 10, 20), Decimal("84.97"), 15, Decimal("180000"), False, 2026): 1,
            },
            build_years={old_cid: Counter({1997: 1, 2026: 1})},
        )
        a_h, r_h = refresh.reconcile(
            FakeReconcileDB(cur_hold), pd.Period("2026-10", freq="M"),
            {old_cid: (old_cid, "현대프라임", addr, 2026)},
            tc_unverified, "11215", refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a_h, r_h), (0, 0))
        self.assertIn(old_cid, cur_hold.identity_pending)
        self.assertEqual({row[-1] for row in cur_hold.identity_pending[old_cid]['source_rows']}, {1997, 2026})
        self.assertEqual(cur_hold.trades, [(101, *old_active)])

        # A reviewed alias for a former name cannot route a different building's sale.
        alias = '서울특별시 광진구 구의동|631-1|옛별칭'
        cur_alias = FakeReconcileCursor([(old_cid, '현대프라임', addr, 1997)], [], decisions={alias: old_cid})
        alias_trade = (alias, *old_active[1:])
        refresh.reconcile(
            FakeReconcileDB(cur_alias), pd.Period('2026-10', freq='M'),
            {alias: (alias, '옛별칭', addr, 2026)}, Counter({alias_trade: 1}),
            '11215', refresh_day=date(2026, 11, 25),
        )
        self.assertFalse(cur_alias.inserted_trades)
        self.assertIn(alias, cur_alias.identity_pending)

        # 2) Same-parcel different-name & different-buildYear complex without decision is ALSO held (no auto new ID from year diff).
        diff_name_same_lot = "서울특별시 광진구 구의동|631-1|더샵현대"
        cur_lot_hold = FakeReconcileCursor(
            complexes=[(old_cid, "현대프라임", addr, 1997)],
            trades=[],
        )
        a_lh, r_lh = refresh.reconcile(
            FakeReconcileDB(cur_lot_hold), pd.Period("2026-10", freq="M"),
            {diff_name_same_lot: (diff_name_same_lot, "더샵현대", addr, 2026)},
            Counter({(diff_name_same_lot, 85, date(2026, 10, 20), Decimal("84.97"), 15, Decimal("180000"), False): 1}),
            "11215", refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a_lh, r_lh), (0, 0))
        self.assertIn(diff_name_same_lot, cur_lot_hold.identity_pending)
        self.assertEqual(cur_lot_hold.identity_pending[diff_name_same_lot]["candidates"], [old_cid])

        # 3) Verified period/discriminator decision splits old (1997) and rebuilt (2026) in the SAME month,
        #    routes late cancellation of old 1997 trade to old_cid, and registers new_rebuild_cid with geocode pending.
        old_cancel = (*old_active[:-1], True)
        new_trade_raw = (old_cid, 85, date(2026, 10, 20), Decimal("84.97"), 15, Decimal("180000"), False)
        brand_new_cid = "서울특별시 광진구 자양동|777|자양신규"
        brand_new_addr = "서울특별시 광진구 자양동 777"
        brand_new_trade = (brand_new_cid, 60, date(2026, 10, 22), Decimal("59.95"), 8, Decimal("110000"), False)

        cur_split = FakeReconcileCursor(
            complexes=[(old_cid, "현대프라임", addr, 1997)],
            trades=[(101, *old_active)],
            period_decisions=[
                (old_cid, 1997, None, date(2026, 10, 31), old_cid, "현대프라임", addr, 1997),
                (old_cid, 2026, date(2026, 10, 1), None, new_rebuild_cid, "현대프라임", addr, 2026),
            ],
        )
        tc_split = molit.TradeCounts(
            {old_cancel: 1, new_trade_raw: 1, brand_new_trade: 1},
            by_built={
                (*old_cancel, 1997): 1,
                (*new_trade_raw, 2026): 1,
                (*brand_new_trade, 2025): 1,
            },
            build_years={
                old_cid: Counter({1997: 1, 2026: 1}),
                brand_new_cid: Counter({2025: 1}),
            },
        )
        a_s, r_s = refresh.reconcile(
            FakeReconcileDB(cur_split), pd.Period("2026-10", freq="M"),
            {
                old_cid: (old_cid, "현대프라임", addr, 2026),
                brand_new_cid: (brand_new_cid, "자양신규", brand_new_addr, 2025),
            },
            tc_split, "11215", refresh_day=date(2026, 11, 25),
        )
        self.assertEqual((a_s, r_s), (3, 1))
        # Old trade 101 was cancelled under old_cid; new rebuild trade is stored under new_rebuild_cid.
        self.assertEqual([(r[0], r[1], r[-1]) for r in cur_split.trade_revisions], [(101, old_cid, "cancelled")])
        trades_by_cid = {r[1]: r[1:] for r in cur_split.trades}
        self.assertEqual(trades_by_cid[old_cid][-1], True)
        self.assertEqual(trades_by_cid[new_rebuild_cid][-1], False)
        self.assertEqual(trades_by_cid[new_rebuild_cid][5], Decimal("180000"))
        self.assertIn(new_rebuild_cid, cur_split.geocode_pending)
        self.assertIn(brand_new_cid, cur_split.geocode_pending)

        # 4) Confirmed demolition with effective_date holds post-demolition active trade on old_cid,
        #    while as_of alone without effective_date does NOT act as a trade cutoff date.
        cur_dem_asof = FakeReconcileCursor(
            complexes=[(old_cid, "현대프라임", addr, 1997)],
            trades=[],
            lifecycle_state=[(old_cid, "demolished", date(2026, 10, 1), None, None)],
        )
        a_da, _ = refresh.reconcile(
            FakeReconcileDB(cur_dem_asof), pd.Period("2026-10", freq="M"),
            {old_cid: (old_cid, "현대프라임", addr, 1997)},
            Counter({old_active: 1}), "11215", refresh_day=date(2026, 11, 25),
        )
        self.assertEqual(a_da, 1)

        cur_dem_eff = FakeReconcileCursor(
            complexes=[(old_cid, "현대프라임", addr, 1997)],
            trades=[],
            lifecycle_state=[(old_cid, "demolished", date(2026, 10, 20), date(2026, 10, 1), "day")],
        )
        a_de, _ = refresh.reconcile(
            FakeReconcileDB(cur_dem_eff), pd.Period("2026-10", freq="M"),
            {old_cid: (old_cid, "현대프라임", addr, 1997)},
            Counter({old_active: 1}), "11215", refresh_day=date(2026, 11, 25),
        )
        self.assertEqual(a_de, 0)
        self.assertIn(old_cid, cur_dem_eff.identity_pending)

    def test_acceptance_3_kapt_candidates_dedup_conflicts_stale_approval_and_missing_complex(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        cid_addr_diff = "서울특별시 구로구 신도림동|643|신도림단지"
        cid_year_diff = "서울특별시 구로구 구로동|100|구로단지"
        cid_both_diff = "서울특별시 구로구 구로동|200|복합변경단지"
        cid_missing = "서울특별시 구로구 개봉동|300|누락보존단지"
        cid_fmt_only = "서울특별시 구로구 고척동|400|표기차이단지"

        state = {
            "complexes": {
                cid_addr_diff: ["신도림단지", "서울특별시 구로구 신도림동 643", 2001, 37.50, 126.88],
                cid_year_diff: ["구로단지", "서울특별시 구로구 구로동 100", 2002, 37.49, 126.89],
                cid_both_diff: ["복합변경단지", "서울특별시 구로구 구로동 200", 1999, 37.48, 126.87],
                cid_missing: ["누락보존단지", "서울특별시 구로구 개봉동 300", 2005, 37.495, 126.85],
                cid_fmt_only: ["표기차이단지", "서울특별시 구로구 고척동 400", 2010, 37.505, 126.86],
            },
            "verified": {
                "K_ADDR": cid_addr_diff,
                "K_YEAR": cid_year_diff,
                "K_BOTH": cid_both_diff,
                "K_MISS": cid_missing,
                "K_FMT": cid_fmt_only,
            },
            "candidates": {},  # candidate_id -> dict
            "observations": set(),  # (candidate_id, batch)
            "runs": {},
            "evidence": {},
            "publish_pending": {},
            "geocode_pending": set(),
            "geocode_attempts": {cid_addr_diff: "서울특별시 구로구 신도림동 643"},
        }

        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        class FakeMonthlyDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT checksum FROM complex_metadata_runs"):
                    val = state["runs"].get(params[0])
                    return FakeRes([(val,)] if val else [])
                if q.startswith("INSERT INTO complex_metadata_runs"):
                    state["runs"][params[0]] = params[1]
                    return FakeRes([(params[0],)])
                if q.startswith("SELECT complex_id, name, address, built FROM complexes"):
                    return FakeRes([(k, v[0], v[1], v[2]) for k, v in state["complexes"].items()])
                if q.startswith("SELECT name, address, built, latitude, longitude FROM complexes"):
                    v = state["complexes"].get(params[0])
                    return FakeRes([tuple(v)] if v else [])
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s AND name=%s AND built=%s"):
                    k, a, nm, b = params
                    v = state["complexes"][k]
                    return FakeRes([(1,)] if (v[1], v[0], v[2]) == (a, nm, b) else [])
                if q.startswith("SELECT kapt_code,complex_id FROM kapt_verified_links"):
                    return FakeRes(list(state["verified"].items()))
                if q.startswith("INSERT INTO kapt_verified_links"):
                    state["verified"][params[1]] = params[2]
                    return FakeRes()
                if q.startswith("UPDATE complex_metadata_candidates SET status = 'superseded'"):
                    if 'last_month<=' in q:
                        dist, kcode, cid_val, month = params
                        for cand in state['candidates'].values():
                            if (cand['district'], cand['kapt_code'], cand['complex_id']) == (dist, kcode, cid_val) and cand['last_month'] <= month and cand['field'] in ('name', 'address', 'built', 'address+built') and cand['status'] in ('pending', 'held'):
                                cand['status'] = 'superseded'
                        return FakeRes()
                    dist, kcode, cid_val, fld, new_key = params
                    for k, cand in state["candidates"].items():
                        if (cand["district"] == dist and cand["kapt_code"] == kcode
                                and (cand["complex_id"] or "") == cid_val and cand["field"] == fld
                                and cand["status"] == "pending" and k != new_key):
                            cand["status"] = "superseded"
                    return FakeRes()
                if q.startswith("INSERT INTO complex_metadata_candidates("):
                    (cid_key, dist, kcode, cid_val, fld, exp_v, prop_v, src,
                     f_m, l_m, f_obs, l_obs, st, rsn, c_ids) = params
                    if cid_key in state["candidates"]:
                        existing_c = state["candidates"][cid_key]
                        existing_c["last_month"] = max(existing_c["last_month"], l_m)
                        existing_c["last_observed_on"] = max(existing_c["last_observed_on"], l_obs)
                        existing_c["candidate_ids"] = c_ids
                    else:
                        state["candidates"][cid_key] = {
                            "candidate_id": cid_key, "district": dist, "kapt_code": kcode,
                            "complex_id": cid_val, "field": fld, "expected_value": exp_v,
                            "proposed_value": prop_v, "source": src, "first_month": f_m,
                            "last_month": l_m, "first_observed_on": f_obs, "last_observed_on": l_obs,
                            "status": st, "reason": rsn, "candidate_ids": c_ids, "evidence": None,
                        }
                    return FakeRes()
                if q.startswith("INSERT INTO complex_metadata_candidate_observations"):
                    state["observations"].add((params[0], params[1]))
                    return FakeRes()
                if q.startswith("UPDATE complexes SET"):
                    cid_val = params[-2] if "name=%s" not in q and "built=%s" not in q else None
                    # Parse dynamic update_complex query
                    where_part = q.split("WHERE ", 1)[1]
                    set_part = q.split("SET ", 1)[1].split(" WHERE ", 1)[0]
                    set_cols = [col.split("=")[0].strip() for col in set_part.split(",")]
                    param_cols = [c for c in set_cols if c not in ("latitude", "longitude") or "%s" in set_part]
                    # count %s in SET
                    set_clauses = [col.strip() for col in set_part.split(",")]
                    n_set_params = sum(1 for c in set_clauses if "%s" in c)
                    set_vals = params[:n_set_params]
                    where_vals = params[n_set_params:]
                    target_cid = where_vals[0]
                    expected_addr = where_vals[1]
                    cur_v = state["complexes"].get(target_cid)
                    if not cur_v or cur_v[1] != expected_addr:
                        return FakeRes(rowcount=0)
                    idx = 2
                    if "AND name=%s" in where_part:
                        if cur_v[0] != where_vals[idx]:
                            return FakeRes(rowcount=0)
                        idx += 1
                    if "AND built=%s" in where_part:
                        if cur_v[2] != where_vals[idx]:
                            return FakeRes(rowcount=0)
                        idx += 1
                    p_idx = 0
                    col_idx = {"name": 0, "address": 1, "built": 2, "latitude": 3, "longitude": 4}
                    for clause in set_clauses:
                        cname, cval = [x.strip() for x in clause.split("=", 1)]
                        if cval == "NULL":
                            cur_v[col_idx[cname]] = None
                        else:
                            cur_v[col_idx[cname]] = set_vals[p_idx]
                            p_idx += 1
                    return FakeRes(rowcount=1)
                if q.startswith("INSERT INTO complex_geocode_pending"):
                    state["geocode_pending"].add(params[0])
                    return FakeRes()
                if q.startswith("DELETE FROM complex_geocode_pending"):
                    state["geocode_pending"].discard(params[0])
                    return FakeRes()
                if q.startswith("DELETE FROM complex_geocode_attempts"):
                    state["geocode_attempts"].pop(params[0], None)
                    return FakeRes()
                if q.startswith("INSERT INTO complex_metadata_evidence"):
                    state["evidence"][(params[0], params[1])] = {"evidence": params[2], "changes": params[3].obj}
                    return FakeRes()
                if q.startswith("UPDATE complex_metadata_evidence SET candidate_ids"):
                    ids, field_evidence, batch, cid = params
                    state['evidence'][(batch, cid)].update(candidate_ids=ids, field_evidence=field_evidence.obj)
                    return FakeRes()
                if q.startswith("UPDATE complex_metadata_evidence"):
                    ev = state["evidence"].get((params[2], params[3]))
                    if ev is not None:
                        ev["candidate_id"] = params[0]
                        ev["before_values"] = params[1].obj if params[1] else None
                    return FakeRes()
                if q.startswith("SELECT candidate_id, field, expected_value, proposed_value"):
                    ids, cid = params
                    return FakeRes(rows=[(c['candidate_id'], c['field'], c['expected_value'], c['proposed_value'])
                                         for c in state['candidates'].values()
                                         if c['candidate_id'] in ids and c['complex_id'] == cid and c['status'] == 'pending'])
                if q.startswith("UPDATE complex_metadata_candidates SET status = 'approved'"):
                    if "candidate_id = ANY" in q:
                        for ck in params[2]:
                            if ck in state["candidates"]:
                                state["candidates"][ck]["status"] = "approved"
                                state["candidates"][ck]["evidence"] = params[0]
                    else:
                        ev_str, _, t_cid, fld, prop_v, expected = params
                        for cand in state["candidates"].values():
                            if cand["complex_id"] == t_cid and cand["field"] == fld and cand["proposed_value"] == prop_v and cand["expected_value"] == expected and cand["status"] == "pending":
                                cand["status"] = "approved"
                                cand["evidence"] = ev_str
                    return FakeRes()
                if q.startswith("UPDATE complex_metadata_candidates SET status = 'held', reason = 'STALE_APPROVAL'"):
                    if "candidate_id = ANY" in q:
                        for ck in params[1]:
                            if ck in state["candidates"]:
                                state["candidates"][ck]["status"] = "held"
                                state["candidates"][ck]["reason"] = "STALE_APPROVAL"
                    else:
                        _, t_cid, flds = params
                        for cand in state["candidates"].values():
                            if cand["complex_id"] == t_cid and cand["field"] in flds and cand["status"] == "pending":
                                cand["status"] = "held"
                                cand["reason"] = "STALE_APPROVAL"
                    return FakeRes()
                if q.startswith("INSERT INTO refresh_publish_pending"):
                    state["publish_pending"][(params[0], params[1])] = (params[2], params[3])
                    return FakeRes()
                return FakeRes()

        listing = (
            ["K_ADDR", "K_YEAR", "K_BOTH", "K_FMT", "K_CROSS"],
            {"K_ADDR": "신도림동", "K_YEAR": "구로동", "K_BOTH": "구로동", "K_FMT": "고척동", "K_CROSS": "구로동"},
        )
        details = {
            # Address changed on verified link (643 -> 643-1)
            "K_ADDR": {"item": {"kaptCode": "K_ADDR", "kaptName": "신도림단지", "kaptAddr": "서울특별시 구로구 신도림동 643-1", "kaptUsedate": "20010501"}},
            # Build year changed on verified link (2002 -> 2003)
            "K_YEAR": {"item": {"kaptCode": "K_YEAR", "kaptName": "구로단지", "kaptAddr": "서울특별시 구로구 구로동 100", "kaptUsedate": "20030501"}},
            # Both address and year changed on verified link
            "K_BOTH": {"item": {"kaptCode": "K_BOTH", "kaptName": "복합변경단지", "kaptAddr": "서울특별시 구로구 구로동 201", "kaptUsedate": "20200501"}},
            # Address formatting difference only ('서울시  구로구 고척동 400' -> normalized same parcel!)
            "K_FMT": {"item": {"kaptCode": "K_FMT", "kaptName": "표기차이단지", "kaptAddr": "서울시  구로구 고척동 400", "kaptUsedate": "20100501"}},
            # Cross-district address in K-apt listing
            "K_CROSS": {"item": {"kaptCode": "K_CROSS", "kaptName": "타구단지", "kaptAddr": "서울특별시 양천구 목동 10", "kaptUsedate": "20080501"}},
        }

        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeMonthlyDB()), \
             patch.object(am, "kapt_listing", return_value=listing), \
             patch.object(am, "kapt_body", side_effect=lambda ep, key, kaptCode: details[kaptCode]):
            st1 = am.refresh_kapt_district("2026-11", "key", "11530")
        self.assertEqual(st1, "completed")
        # None of the addresses or build years were overwritten by K-apt alone!
        self.assertEqual(state["complexes"][cid_addr_diff][1], "서울특별시 구로구 신도림동 643")
        self.assertEqual(state["complexes"][cid_year_diff][2], 2002)
        self.assertEqual(state["complexes"][cid_both_diff][1:3], ["서울특별시 구로구 구로동 200", 1999])
        # Missing complex from K-apt listing kept its verified link and coordinates!
        self.assertEqual(state["verified"]["K_MISS"], cid_missing)
        self.assertEqual(state["complexes"][cid_missing][3:5], [37.495, 126.85])
        # Formatting-only address produced NO address-diff candidate and kept its coordinates!
        fmt_cands = [c for c in state["candidates"].values() if c["complex_id"] == cid_fmt_only]
        self.assertEqual(fmt_cands, [])

        initial_cand_count = len(state["candidates"])
        self.assertGreaterEqual(initial_cand_count, 4)

        # Next month (2026-12) observes the exact same K-apt response -> candidate count stays identical (no duplicate proposals)!
        with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeMonthlyDB()), \
             patch.object(am, "kapt_listing", return_value=listing), \
             patch.object(am, "kapt_body", side_effect=lambda ep, key, kaptCode: details[kaptCode]):
            st2 = am.refresh_kapt_district("2026-12", "key", "11530")
        self.assertEqual(st2, "completed")
        self.assertEqual(len(state["candidates"]), initial_cand_count)
        addr_cand = next(c for c in state["candidates"].values() if c["complex_id"] == cid_addr_diff and c["field"] == "address")
        self.assertEqual((addr_cand["first_month"], addr_cand["last_month"]), ("2026-11", "2026-12"))

        # Now apply verified metadata approval for cid_addr_diff (address-only) and cid_year_diff (built year),
        # plus a second file with a stale approval on cid_both_diff.
        with TemporaryDirectory() as tmp:
            inbox = Path(tmp)
            ok_batch = {
                "batch": "2026-11",
                "complexes": [
                    {
                        "complex_id": cid_addr_diff,
                        "expected_address": "서울특별시 구로구 신도림동 643",
                        "expected_name": "신도림단지",
                        "expected_built": 2001,
                        "expected_name": "신도림단지", "expected_built": 2001,
                        "candidate_id": addr_cand["candidate_id"],
                        "field_evidence": {"address": "건축물대장 지번정정 확인"},
                        "evidence": "건축물대장 지번정정 확인",
                        "changes": {"address": "서울특별시 구로구 신도림동 643-1"},
                    },
                    {
                        "complex_id": cid_year_diff,
                        "expected_address": "서울특별시 구로구 구로동 100",
                        "expected_built": 2002,
                        "evidence": "사용승인일 대장 확인",
                        "changes": {"built": 2003},
                    },
                ],
            }
            stale_batch = {
                "batch": "new-stale",
                "complexes": [
                    {
                        "complex_id": cid_both_diff,
                        "expected_address": "서울특별시 구로구 구로동 999-오래된주소",
                        "evidence": "오래된 승인",
                        "changes": {"address": "서울특별시 구로구 구로동 201"},
                    },
                ],
            }
            (inbox / "2026-11.json").write_text(json.dumps(ok_batch, ensure_ascii=False), encoding="utf-8")
            (inbox / "new-stale.json").write_text(json.dumps(stale_batch, ensure_ascii=False), encoding="utf-8")

            with patch.object(am.psycopg, "connect", side_effect=lambda *a, **kw: FakeMonthlyDB()):
                self.assertEqual(refresh_metadata.apply_metadata(inbox, date(2026, 11, 26)), 1)

        # 2026-11.json succeeded even though new-stale.json failed!
        # Address-only change cleared coordinates, queued geocode_pending, and cleared old geocode_attempts.
        self.assertEqual(
            state["complexes"][cid_addr_diff],
            ["신도림단지", "서울특별시 구로구 신도림동 643-1", 2001, None, None],
        )
        self.assertIn(cid_addr_diff, state["geocode_pending"])
        self.assertNotIn(cid_addr_diff, state["geocode_attempts"])
        self.assertEqual(state["candidates"][addr_cand["candidate_id"]]["status"], "approved")
        self.assertEqual(
            state["evidence"][("2026-11", cid_addr_diff)]["before_values"],
            {"address": "서울특별시 구로구 신도림동 643"},
        )
        self.assertEqual(state['evidence'][('2026-11', cid_addr_diff)]['candidate_ids'], [addr_cand['candidate_id']])
        self.assertEqual(state['evidence'][('2026-11', cid_addr_diff)]['field_evidence'],
                         {'address': '건축물대장 지번정정 확인'})
        wrong_candidate = {
            'complex_id': cid_year_diff, 'expected_address': '서울특별시 구로구 구로동 100',
            'expected_name': '구로단지', 'expected_built': 2003,
            'evidence': '검증 자료', 'changes': {'built': 2004}, 'candidate_id': addr_cand['candidate_id'],
        }
        with self.assertRaisesRegex(ValueError, 'Invalid metadata candidates'):
            refresh_metadata.apply_verified_complex_records(FakeMonthlyDB(), 'new-wrong', 'checksum', [wrong_candidate])
        self.assertEqual(state['complexes'][cid_year_diff][2], 2003)
        # Built year change updated built and enqueued a 0-count publish request for 11530!
        self.assertEqual(state["complexes"][cid_year_diff][2], 2003)
        self.assertEqual(state["publish_pending"].get(("11530", date(2026, 11, 26))), (0, 0))
        # Stale approval did not modify cid_both_diff and marked its pending candidate as held/STALE_APPROVAL.
        self.assertEqual(state["complexes"][cid_both_diff][1], "서울특별시 구로구 구로동 200")

    def test_acceptance_4_address_change_clears_coords_discards_stale_geocode_and_applies_bundled_coords(self):
        cid_stale = "서울특별시 광진구 구의동|1|주소변경단지"
        cid_bundled = "서울특별시 광진구 구의동|2|동봉좌표단지"
        state = {
            "complexes": {
                cid_stale: ["주소변경단지", "서울특별시 광진구 구의동 1", 2000, 37.53, 127.08],
                cid_bundled: ["동봉좌표단지", "서울특별시 광진구 구의동 2", 2000, 37.53, 127.08],
            },
            "pending": set(),
            "attempts": {},
        }

        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        class FakeGeoDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def commit(self):
                pass

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("UPDATE complexes SET address=%s, latitude=NULL, longitude=NULL"):
                    new_addr, k, old_addr = params
                    if state["complexes"][k][1] == old_addr:
                        state["complexes"][k][1] = new_addr
                        state["complexes"][k][3] = None
                        state["complexes"][k][4] = None
                        return FakeRes(rowcount=1)
                    return FakeRes(rowcount=0)
                if q.startswith("UPDATE complexes SET address=%s, latitude=%s, longitude=%s"):
                    new_addr, lat, lng, k, old_addr = params
                    if state["complexes"][k][1] == old_addr:
                        state["complexes"][k][1] = new_addr
                        state["complexes"][k][3] = lat
                        state["complexes"][k][4] = lng
                        return FakeRes(rowcount=1)
                    return FakeRes(rowcount=0)
                if q.startswith("INSERT INTO complex_geocode_pending"):
                    state["pending"].add(params[0])
                    return FakeRes()
                if q.startswith("DELETE FROM complex_geocode_pending"):
                    state["pending"].discard(params[0])
                    return FakeRes()
                if q.startswith("DELETE FROM complex_geocode_attempts"):
                    state["attempts"].pop(params[0], None)
                    return FakeRes()
                if "SELECT c.complex_id, c.address FROM complexes c" in q:
                    # Return the old address for cid_stale to simulate an in-flight Naver query started before address change
                    return FakeRes([(cid_stale, "서울특별시 광진구 구의동 1")])
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id=%s AND address=%s FOR UPDATE"):
                    k, addr = params
                    return FakeRes([(1,)] if state["complexes"][k][1] == addr else [])
                if q.startswith("UPDATE complexes SET latitude=%s, longitude=%s"):
                    lat, lng, k, addr, _ = params
                    cur = state["complexes"][k]
                    if cur[1] == addr and (cur[3] is None or cur[4] is None or k in state["pending"]):
                        cur[3], cur[4] = lat, lng
                        return FakeRes(rowcount=1)
                    return FakeRes(rowcount=0)
                if q.startswith("INSERT INTO complex_geocode_attempts"):
                    k, addr, day = params
                    cur = state["complexes"][k]
                    if cur[1] == addr and (cur[3] is None or cur[4] is None or k in state["pending"]):
                        state["attempts"][k] = (addr, day)
                    return FakeRes()
                return FakeRes()

        db = FakeGeoDB()
        # 1) Address-only update clears coordinates immediately and adds to geocode_pending.
        self.assertTrue(
            refresh_metadata.update_complex(
                db, cid_stale, "서울특별시 광진구 구의동 1",
                {"address": "서울특별시 광진구 구의동 1-2"},
            )
        )
        self.assertEqual(state["complexes"][cid_stale][3:5], [None, None])
        self.assertIn(cid_stale, state["pending"])

        # 2) Bundled verified coordinates update immediately without geocode_pending.
        self.assertTrue(
            refresh_metadata.update_complex(
                db, cid_bundled, "서울특별시 광진구 구의동 2",
                {"address": "서울특별시 광진구 구의동 2-1", "latitude": 37.541, "longitude": 127.091},
            )
        )
        self.assertEqual(state["complexes"][cid_bundled][3:5], [37.541, 127.091])
        self.assertNotIn(cid_bundled, state["pending"])

        # 3) Late Naver response for old address ("서울특별시 광진구 구의동 1") is discarded!
        naver_resp = {
            "status": "OK",
            "addresses": [{"jibunAddress": "서울특별시 광진구 구의동 1", "y": "37.530", "x": "127.080"}],
        }
        with patch.object(am.psycopg, "connect", return_value=db), \
             patch.object(am, "read_json", return_value=naver_resp):
            am.geocode(date(2026, 11, 26), {}, ["11215"])
        self.assertEqual(state["complexes"][cid_stale][3:5], [None, None])
        self.assertIn(cid_stale, state["pending"])

    def test_acceptance_5_input_window_zero_delta_reassignment_built_change_and_publish_recovery(self):
        today = datetime.now(KST).date()
        anchor = pd.Period(today, freq="M")
        in_window = (anchor - 6).start_time.date()
        db = ScriptedDB([
            ("SELECT 1 FROM historical_rechecks", []),
            ("SELECT h.month FROM historical_rechecks", []),
            ("SELECT month FROM (", [(in_window,)]),
        ])
        # Alias reassignment in input window has added=0, removed=0, reassigned=1 -> must still enqueue (0, 0) publish!
        zero_delta_reassigned = refresh.ReconcileResult(0, 0, reassigned=1)
        with patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", return_value=db), \
             patch.object(refresh, "fetch_month", return_value=({}, Counter())), \
             patch.object(refresh, "reconcile", return_value=zero_delta_reassigned):
            needs_republish = refresh.recheck_history("11530", today=today)
        self.assertTrue(needs_republish)
        self.assertEqual(db.ran("INSERT INTO refresh_publish_pending"), [("11530", today, 0, 0)])

    def test_acceptance_6_lifecycle_contract_empty_provider_and_state_transitions(self):
        import complex_list
        import lifecycle_results as lr

        cid_a = "서울특별시 광진구 구의동|631-1|현대프라임"
        cid_b = "서울특별시 광진구 자양동|100|자양현대"

        # 1) Default empty provider returns 0 results.
        self.assertEqual(lr.empty_lifecycle_provider("11215", date(2026, 11, 26), [cid_a, cid_b]), [])

        state = {
            "complexes": {
                cid_a: ("현대프라임", "서울특별시 광진구 구의동 631-1", 1997, 37.537, 127.097),
                cid_b: ("자양현대", "서울특별시 광진구 자양동 100", 1999, 37.531, 127.081),
            },
            "results": {},      # result_key -> dict
            "lifecycle": {},    # complex_id -> dict
            "quarantine": [],   # list of (result_key, complex_id, existence, reason)
        }

        class FakeRes:
            def __init__(self, rows=None, rowcount=1):
                self._rows = list(rows or [])
                self.rowcount = rowcount

            def fetchall(self):
                return list(self._rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

        class FakeLifecycleDB:
            @contextmanager
            def transaction(self):
                yield

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT payload_digest FROM complex_lifecycle_results"):
                    r = state["results"].get(params[0])
                    return FakeRes([(r["payload_digest"],)] if r else [])
                if q.startswith("SELECT 1 FROM complexes WHERE complex_id = %s FOR UPDATE"):
                    return FakeRes([(1,)] if params[0] in state["complexes"] else [])
                if q.startswith("SELECT result_key, existence, as_of, pending_as_of FROM complex_lifecycle_state"):
                    s = state["lifecycle"].get(params[0])
                    return FakeRes([(s["result_key"], s["existence"], s["as_of"], s["pending_as_of"])] if s else [])
                if q.startswith("INSERT INTO complex_lifecycle_quarantine"):
                    state["quarantine"].append(params[:4])
                    return FakeRes()
                if q.startswith("INSERT INTO complex_lifecycle_results("):
                    (r_key, c_id, ext, as_of_d, ver, ev, dig, h_code, corr, succ_id, succ_st, eff_d, eff_p) = params
                    applied_st = "pending_recorded" if "'pending_recorded'" in q else "applied"
                    state["results"][r_key] = {
                        "result_key": r_key, "complex_id": c_id, "existence": ext, "as_of": as_of_d,
                        "verifier_version": ver, "evidence": ev, "payload_digest": dig,
                        "hold_code": h_code, "correction_of": corr, "successor_id": succ_id,
                        "successor_status": succ_st, "effective_date": eff_d, "effective_precision": eff_p,
                        "applied_status": applied_st,
                    }
                    return FakeRes()
                if q.startswith("INSERT INTO complex_lifecycle_state( complex_id, pending_result_key"):
                    c_id, p_key, p_code, p_as_of = params
                    s = state["lifecycle"].setdefault(c_id, {
                        "result_key": None, "existence": None, "as_of": None,
                        "verifier_version": None, "evidence": None,
                        "effective_date": None, "effective_precision": None,
                        "pending_result_key": None, "pending_hold_code": None, "pending_as_of": None,
                    })
                    s.update(pending_result_key=p_key, pending_hold_code=p_code, pending_as_of=p_as_of)
                    return FakeRes()
                if q.startswith("INSERT INTO complex_lifecycle_state( complex_id, result_key, existence"):
                    c_id, r_key, ext, as_of_d, ver, ev, eff_d, eff_p = params
                    s = state["lifecycle"].setdefault(c_id, {
                        "pending_result_key": None, "pending_hold_code": None, "pending_as_of": None,
                    })
                    s.update(
                        result_key=r_key, existence=ext, as_of=as_of_d, verifier_version=ver,
                        evidence=ev, effective_date=eff_d, effective_precision=eff_p,
                    )
                    return FakeRes()
                return FakeRes()

        db = FakeLifecycleDB()
        scope_check = lr.apply_lifecycle_results(db, [
            lr.LifecycleResult('wrong-scope', cid_b, 'demolished', 'registry', '2026-11-13', 'v1'),
            lr.LifecycleResult('future', cid_a, 'demolished', 'registry', '2026-12-31', 'v1'),
        ], allowed_ids={cid_a}, cutoff=date(2026, 11, 26))
        self.assertEqual(scope_check['quarantined'], 2)
        self.assertFalse(state['lifecycle'])
        state['quarantine'].clear()
        # Step 1: present on cid_a, pending on cid_b (with successor info -> must not create new complex or change state).
        s1 = lr.apply_lifecycle_results(db, [
            lr.LifecycleResult(
                result_key="r-a-present-1", complex_id=cid_a, existence="present",
                evidence="registry:2026-10", as_of="2026-10-01", verifier_version="v1",
            ),
            lr.LifecycleResult(
                result_key="r-b-pending-1", complex_id=cid_b, existence="pending",
                evidence="review:hold", hold_code="AMBIGUOUS_LEDGER",
                successor_id="서울특별시 광진구 자양동|100|자양신축", successor_status="candidate",
                as_of="2026-10-01", verifier_version="v1",
            ),
        ])
        self.assertEqual(s1, {"applied": 1, "noop": 0, "pending": 1, "quarantined": 0})
        self.assertEqual(state["lifecycle"][cid_a]["existence"], "present")
        self.assertIsNone(state["lifecycle"][cid_b]["existence"])
        self.assertEqual(len(state["complexes"]), 2)  # No new complex created from successor_id!

        # Step 2: demolished on cid_a, followed by pending on cid_a -> state stays demolished!
        s2 = lr.apply_lifecycle_results(db, [
            lr.LifecycleResult(
                result_key="r-a-demolished-1", complex_id=cid_a, existence="demolished",
                evidence="closed-ledger:2026-11", as_of="2026-11-01", verifier_version="v1",
                effective_date="2026-10-15", effective_precision="day",
            ),
            lr.LifecycleResult(
                result_key="r-a-pending-after-dem", complex_id=cid_a, existence="pending",
                evidence="recheck-hold", hold_code="RECHECK", as_of="2026-11-10", verifier_version="v1",
            ),
        ])
        self.assertEqual(s2, {"applied": 1, "noop": 0, "pending": 1, "quarantined": 0})
        self.assertEqual(state["lifecycle"][cid_a]["existence"], "demolished")
        # Coordinates of cid_a are preserved!
        self.assertEqual(state["complexes"][cid_a][3:5], (37.537, 127.097))

        # Step 3: Exact duplicate is noop; same key with different content, older as_of, unknown ID,
        #         missing evidence, and unapproved present after demolished are all quarantined without affecting cid_b!
        s3 = lr.apply_lifecycle_results(db, [
            # Exact duplicate -> noop
            lr.LifecycleResult(
                result_key="r-a-demolished-1", complex_id=cid_a, existence="demolished",
                evidence="closed-ledger:2026-11", as_of="2026-11-01", verifier_version="v1",
                effective_date="2026-10-15", effective_precision="day",
            ),
            # Same key, different content -> DUPLICATE_KEY_CONFLICT
            lr.LifecycleResult(
                result_key="r-a-demolished-1", complex_id=cid_a, existence="present",
                evidence="tampered", as_of="2026-11-01", verifier_version="v1",
            ),
            # Out-of-order older as_of -> STALE_AS_OF
            lr.LifecycleResult(
                result_key="r-a-old", complex_id=cid_a, existence="present",
                evidence="old-record", as_of="2026-09-01", verifier_version="v1",
            ),
            # Normal 'present' after 'demolished' without correction_of -> DEMOLISHED_CONFLICT_WITHOUT_CORRECTION
            lr.LifecycleResult(
                result_key="r-a-unapproved-revive", complex_id=cid_a, existence="present",
                evidence="normal-check", as_of="2026-11-20", verifier_version="v1",
            ),
            # Unregistered complex_id -> UNKNOWN_COMPLEX_ID
            lr.LifecycleResult(
                result_key="r-unknown", complex_id="서울특별시 광진구 구의동|999|미등록",
                existence="demolished", evidence="ev", as_of="2026-11-01", verifier_version="v1",
            ),
            # Missing evidence -> MISSING_EVIDENCE
            lr.LifecycleResult(
                result_key="r-no-evidence", complex_id=cid_b,
                existence="demolished", evidence="   ", as_of="2026-11-01", verifier_version="v1",
            ),
            # Valid present result on cid_b in the same batch must still succeed!
            lr.LifecycleResult(
                result_key="r-b-present-1", complex_id=cid_b,
                existence="present", evidence="ledger-ok", as_of="2026-11-01", verifier_version="v1",
            ),
        ])
        self.assertEqual(s3, {"applied": 1, "noop": 1, "pending": 0, "quarantined": 5})
        self.assertEqual(state["lifecycle"][cid_a]["existence"], "demolished")
        self.assertEqual(state["lifecycle"][cid_b]["existence"], "present")
        reasons = [q[3] for q in state["quarantine"]]
        self.assertEqual(
            reasons,
            [
                "DUPLICATE_KEY_CONFLICT",
                "STALE_AS_OF",
                "DEMOLISHED_CONFLICT_WITHOUT_CORRECTION",
                "UNKNOWN_COMPLEX_ID",
                "MISSING_EVIDENCE",
            ],
        )

        # Step 4: Explicit correction_of matching the demolished result_key revives cid_a to 'present'.
        s4 = lr.apply_lifecycle_results(db, [
            lr.LifecycleResult(
                result_key="r-a-correction-1", complex_id=cid_a, existence="present",
                evidence="manual-reverification-correction", as_of="2026-11-25",
                verifier_version="v1", correction_of="r-a-demolished-1",
            ),
        ])
        self.assertEqual(s4["applied"], 1)
        self.assertEqual(state["lifecycle"][cid_a]["existence"], "present")

        # Verify load_home_rows joins complex_lifecycle_state and exposes demolition_confirmed boolean.
        home_db = ScriptedDB([
            ("SELECT c.complex_id,", [
                {
                    "complex_id": cid_a, "name": "현대프라임", "address": "서울특별시 광진구 구의동 631-1",
                    "built": 1997, "latitude": 37.537, "longitude": 127.097,
                    "area_group": 85, "representative_area": 84.97, "predictable": True,
                    "demolition_confirmed": True,
                },
            ]),
            ("SELECT complex_id, count(*)", [{"complex_id": cid_a, "trade_count": 12, "last_trade": date(2026, 10, 10)}]),
            ("SELECT DISTINCT ON (complex_id, area_group)", [{"complex_id": cid_a, "area_group": 85, "price_manwon": 120000}]),
            ("SELECT DISTINCT ON (complex_id) m.*", []),
        ])
        rows_full = complex_list.load_home_rows(home_db, district="광진구", include_sort=True)
        self.assertEqual(rows_full[0]["demolition_confirmed"], True)
        # Historical marker_price_manwon and trade_count are preserved for list & detail views!
        self.assertEqual(rows_full[0]["marker_price_manwon"], 120000)
        self.assertEqual(rows_full[0]["trade_count"], 12)
        self.assertIn("LEFT JOIN complex_lifecycle_state ls USING (complex_id)", home_db.calls[0][0])

    def test_acceptance_7_partial_503_resume_31day_month_boundary_and_retry_limits(self):
        # 1) Daily 4-month window: first 3 months succeed, 4th month fails with 503.
        #    On retry 10 minutes later, first 3 months are loaded from daily_trade_month_checkpoints and not re-fetched!
        now = datetime(2026, 11, 26, 6, 0, tzinfo=KST)
        slot_start = refresh.refresh_slot_start(now)
        month_cps = {}
        district_cps = {}
        pending_pub = {}

        class Res:
            def __init__(self, rows=None):
                self._rows = list(rows or [])

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)

        class PartialWindowDB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @contextmanager
            def transaction(self):
                yield

            def commit(self):
                pass

            def execute(self, sql, params=None):
                check_placeholders(sql, params)
                q = " ".join(sql.split())
                if q.startswith("SELECT max(day), max(COALESCE(slot_start, finished_at))"):
                    return Res([(date(2026, 11, 25), slot_start - timedelta(days=1))])
                if q.startswith("SELECT month FROM ("):
                    return Res([])
                if q.startswith("SELECT added,removed FROM daily_trade_checkpoints"):
                    cp = district_cps.get((params[0], params[1]))
                    return Res([cp] if cp else [])
                if q.startswith("SELECT month, added, removed, reassigned FROM daily_trade_month_checkpoints"):
                    d, sl = params
                    return Res([(m, a, r, re_c) for ( dist_k, sl_k, m), (a, r, re_c) in month_cps.items() if dist_k == d and sl_k == sl])
                if q.startswith("INSERT INTO daily_trade_month_checkpoints"):
                    d, sl, m, _day, a, r, re_c = params
                    month_cps[(d, sl, m)] = (a, r, re_c)
                    return Res()
                if q.startswith("INSERT INTO daily_trade_checkpoints"):
                    d, dy, a, r, _sl = params
                    district_cps[(d, dy)] = (a, r)
                    return Res()
                if q.startswith("INSERT INTO refresh_publish_pending"):
                    d, dy, a, r = params
                    pa, pr = pending_pub.get((d, dy), (0, 0))
                    pending_pub[(d, dy)] = (pa + a, pr + r)
                    return Res()
                if q.startswith("SELECT day,added,removed FROM refresh_publish_pending"):
                    d, dy = params
                    return Res([(k_dy, a, r) for (k_d, k_dy), (a, r) in pending_pub.items() if k_d == d and k_dy <= dy])
                if q.startswith("DELETE FROM refresh_publish_pending"):
                    pending_pub.clear()
                    return Res()
                return Res([])

        fetched_months = []

        def fetch_fail_on_nov(ym, dist):
            fetched_months.append(ym)
            if ym == "202611":
                raise HTTPError("https://apis.data.go.kr/test", 503, "Service Unavailable", {}, None)
            return {}, Counter()

        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "k"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=lambda *a, **kw: PartialWindowDB()), \
             patch.object(refresh, "fetch_month", side_effect=fetch_fail_on_nov), \
             patch.object(refresh, "reconcile", return_value=refresh.ReconcileResult(1, 0)):
            with self.assertRaises(HTTPError):
                refresh.refresh("11530", "tmp", now=now)

        self.assertEqual(fetched_months, ["202608", "202609", "202610", "202611"])
        self.assertEqual(len(month_cps), 3)
        self.assertEqual(district_cps, {})
        self.assertEqual(pending_pub[("11530", now.date())], (3, 0))

        # Retry 10 minutes later: 202608, 202609, 202610 are skipped; only 202611 is fetched!
        fetched_months.clear()
        with patch.dict(os.environ, {"MOLIT_SERVICE_KEY": "k"}), \
             patch.object(refresh, "ensure_schema"), \
             patch.object(refresh.psycopg, "connect", side_effect=lambda *a, **kw: PartialWindowDB()), \
             patch.object(refresh, "fetch_month", side_effect=lambda ym, d: (fetched_months.append(ym) or ({}, Counter()))), \
             patch.object(refresh, "reconcile", return_value=refresh.ReconcileResult(2, 0)), \
             patch.object(refresh.subprocess, "run"), \
             patch.object(refresh, "publish", return_value=7):
            refresh.refresh("11530", "tmp", now=now + timedelta(minutes=10))

        self.assertEqual(fetched_months, ["202611"])
        self.assertEqual(district_cps[("11530", now.date())], (5, 0))

        # 2) Monthly worker across a 31-day month (October) and boundary 2026-11-01 05:59 vs 06:00 KST:
        store = refresh.MemoryAttemptStore()
        monthly_calls = []

        def failing_monthly_job():
            monthly_calls.append(1)
            return ["K-apt"]

        oct1_0600 = datetime(2026, 10, 1, 6, 0, tzinfo=KST)
        m_slot_oct = refresh.monthly_slot_start(oct1_0600)
        m_next_oct = refresh.next_monthly_slot_start(m_slot_oct)
        self.assertEqual(m_slot_oct, oct1_0600)
        self.assertEqual(m_next_oct, datetime(2026, 11, 1, 6, 0, tzinfo=KST))

        # Initial + 3 retries at 30-min (1800s) intervals = 4 attempts total.
        for idx, offset_min in enumerate((0, 30, 60, 90), start=1):
            t_cur = oct1_0600 + timedelta(minutes=offset_min)
            wait = refresh.run_slot(
                "metadata", failing_monthly_job, t_cur, store,
                max_retries=3, retry_delay=1800,
                slot_start=refresh.monthly_slot_start(t_cur),
                next_slot=refresh.next_monthly_slot_start(refresh.monthly_slot_start(t_cur)),
            )
            if idx < 4:
                self.assertEqual(wait, 1800)
            else:
                self.assertGreater(wait, 25 * 24 * 3600)
        self.assertEqual(len(monthly_calls), 4)

        # Day 31 of October (2026-10-31 12:00 KST) and 2026-11-01 05:59 KST (30d 23h 59m after Oct 1 06:00):
        # Must NOT expire or reset the October attempt count!
        for boundary_ts in (
            datetime(2026, 10, 31, 12, 0, tzinfo=KST),
            datetime(2026, 11, 1, 5, 59, tzinfo=KST),
        ):
            m_s = refresh.monthly_slot_start(boundary_ts)
            self.assertEqual(m_s, oct1_0600)
            refresh.run_slot(
                "metadata", failing_monthly_job, boundary_ts, store,
                max_retries=3, retry_delay=1800,
                slot_start=m_s,
                next_slot=refresh.next_monthly_slot_start(m_s),
            )
        self.assertEqual(len(monthly_calls), 4)

        # At 2026-11-01 06:00 KST, the November monthly slot opens and runs attempt 1 of November!
        nov1_0600 = datetime(2026, 11, 1, 6, 0, tzinfo=KST)
        m_s_nov = refresh.monthly_slot_start(nov1_0600)
        self.assertEqual(m_s_nov, nov1_0600)
        refresh.run_slot(
            "metadata", lambda: (monthly_calls.append("nov") or []), nov1_0600, store,
            max_retries=3, retry_delay=1800,
            slot_start=m_s_nov,
            next_slot=refresh.next_monthly_slot_start(m_s_nov),
        )
        self.assertEqual(monthly_calls[-1], "nov")


if __name__ == "__main__":
    unittest.main()
