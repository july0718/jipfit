"""Port parity (plan-lifecycle-auto.md 9-2): lifecycle_check and the original monthly.py give the same output.

Both judge the real baseline cache with the same complexes and trades, once with the state the baseline left
(the unchanged month) and once with an emptied state (PLAN_MONTHLY.md 4-8). The test runs only when

  JIPFIT_LIFECYCLE_PARITY_SRC   the verifier repository (jipfit_lifecycle@d86dcdf), which is only read
  JIPFIT_LIFECYCLE_PARITY_DUMP  a Jipfit backup with complexes and trades (default: ../before_monthly.dump)
  JIPFIT_PG_RESTORE             pg_restore, when it is not on PATH

The original runs in a separate interpreter that writes no bytecode into its repository.
"""
from collections import Counter
from datetime import date
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

# Installs the psycopg stand-in when the package is missing, so it must be imported first.
import test_refresh_pipeline  # noqa: F401

from lifecycle_check import stage
from lifecycle_check.common import csv_rows
from lifecycle_check.monthly import write_batch

ROOT = Path(__file__).resolve().parent
SOURCE = os.environ.get("JIPFIT_LIFECYCLE_PARITY_SRC")
DUMP = Path(os.environ.get("JIPFIT_LIFECYCLE_PARITY_DUMP") or ROOT.parent / "before_monthly.dump")
PG_RESTORE = os.environ.get("JIPFIT_PG_RESTORE") or shutil.which("pg_restore")
DAY = date(2026, 11, 26)
BATCH = "monthly-20261126"
LOAD = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import monthly
monthly.TABLES = ("complexes", "trades")
tables = monthly.load_dump(sys.argv[2], Path(sys.argv[3]))
tables["trades"] = [{k: row[k] for k in ("complex_id", "contract_date", "is_cancelled")} for row in tables["trades"]]
Path(sys.argv[4]).write_text(json.dumps(tables, ensure_ascii=False), encoding="utf-8")
"""
RUN = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import monthly
tables = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
monthly.load_dump = lambda *_: tables
raise SystemExit(monthly.main(sys.argv[3:]))
"""


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


@unittest.skipUnless(SOURCE, "set JIPFIT_LIFECYCLE_PARITY_SRC to compare with the original monthly.py")
class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = cls.source = Path(SOURCE).resolve()
        cache = source / "source_cache"
        fingerprint_file = source / "results" / "fingerprints" / "latest.json"
        exports = sorted(source.glob("results/export_*/results.jsonl"))
        missing = [str(p) for p in (cache / "trade_detail", fingerprint_file, DUMP) if not p.exists()]
        if missing or not exports or not PG_RESTORE:
            raise AssertionError(f"일치 확인 입력이 없습니다: {missing}, export {len(exports)}, "
                                 f"pg_restore {PG_RESTORE}")
        temp = TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        root = cls.root = Path(temp.name)
        cls.python(LOAD, PG_RESTORE, DUMP, root / "dump.json")
        tables = json.loads((root / "dump.json").read_text(encoding="utf-8"))
        exported = lines(exports[-1])
        states = [{"complex_id": r["complex_id"], "result_key": r["result_key"], "existence": r["existence"],
                   "as_of": r["as_of"], "evidence": r["evidence"], "effective_date": r.get("effective_date"),
                   "effective_precision": r.get("effective_precision"), "pending_hold_code": None,
                   "pending_as_of": None} for r in exported]
        history = [{"complex_id": r["complex_id"], "existence": r["existence"],
                    "successor_id": r.get("successor_id"), "applied_status": "applied"} for r in exported]
        fingerprints = json.loads(fingerprint_file.read_text(encoding="utf-8"))
        print(f"\n일치 확인 입력: 단지 {len(tables['complexes']):,}, 거래 {len(tables['trades']):,}, "
              f"상태 {len(states):,}, 지문 지번 {len(fingerprints['parcels']):,}")
        cls.runs = {}
        for label, state_rows, history_rows in (("unchanged", states, history), ("cleared", [], [])):
            data = {**tables, "complex_lifecycle_state": state_rows, "complex_lifecycle_results": history_rows}
            (root / f"{label}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            old = root / label / "original"
            cls.python(RUN, root / f"{label}.json", "run", "--dump", DUMP, "--as-of", DAY.isoformat(),
                       "--cache", cache, "--reference-cache", cache, "--fingerprints", fingerprint_file,
                       "--results-dir", old, "--offline")
            with patch("builtins.print"):
                outcome = stage.evaluate(data, fingerprints, cache, cache, DAY, "parity")
            new = root / label / "ported"
            write_batch({**outcome, "proposals": []}, new / "batches", new / "reports", DAY, "parity")
            cls.runs[label] = (old / "monthly_20261126", new / "batches" / BATCH, new / "reports" / BATCH)

    @classmethod
    def python(cls, code, *args):
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        done = subprocess.run([sys.executable, "-B", "-c", code, str(cls.source), *map(str, args)], cwd=cls.root,
                              env=environment, capture_output=True, text=True, encoding="utf-8")
        if done.returncode:
            raise AssertionError(f"원본 실행 실패 {done.returncode}:\n{done.stdout}\n{done.stderr}")

    def compare(self, label):
        original, batch, reports = self.runs[label]
        expected, found = lines(original / "results.jsonl"), lines(batch / "results.jsonl")
        for row in (*expected, *found):
            row.pop("verifier_version")
        old, new = ({r["complex_id"]: r for r in rows} for rows in (expected, found))
        different = sorted(cid for cid in old.keys() | new.keys() if old.get(cid) != new.get(cid))
        tables = {name: (csv_rows(original / name), csv_rows(reports / name))
                  for name in ("auto_confirmed.csv", "review.csv")}
        documents = [json.loads((folder / "fingerprints.json").read_text(encoding="utf-8"))
                     for folder in (original, batch)]
        print(f"\n[{label}] results.jsonl {len(expected):,} / {len(found):,}건, 불일치 {len(different)}건, "
              f"분포 {dict(Counter((r['existence'], r.get('hold_code', '')) for r in found))}")
        for name, (a, b) in tables.items():
            print(f"[{label}] {name} {len(a):,} / {len(b):,}행, 같음 {a == b}")
        print(f"[{label}] 지문 지번 {len(documents[0]['parcels']):,} / {len(documents[1]['parcels']):,}, "
              f"같음 {documents[0] == documents[1]}")
        self.assertEqual(different[:5], [], f"불일치 {len(different)}건")
        self.assertEqual(expected, found)
        for name, (a, b) in tables.items():
            self.assertEqual(a, b, name)
        self.assertEqual(documents[0], documents[1])

        # Only the closing instruction about publication differs between the reports.
        def report(path):
            return [line for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.startswith("- 자동 확정")]

        self.assertEqual(report(original / "report.md"), report(reports / "report.md"))

    def test_the_unchanged_month_matches_the_original(self):
        self.compare("unchanged")

    def test_the_replay_with_an_emptied_state_matches_the_original(self):
        self.compare("cleared")


if __name__ == "__main__":
    unittest.main()
