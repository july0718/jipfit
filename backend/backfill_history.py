"""Fill the historical gap before the April 2025 Seoul bootstrap, one committed month at a time."""
import argparse

import pandas as pd
import psycopg

from districts import DISTRICTS, GWANGJIN
from urllib.error import HTTPError

from molit import MolitResponseError, TradeDataError, fetch_month
from refresh import reconcile


MONTHS = pd.period_range("2006-01", "2025-03", freq="M")
TARGETS = [code for code in DISTRICTS if code != GWANGJIN]


def backfill(codes):
    missing = []
    with psycopg.connect(autocommit=True, connect_timeout=8) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS trade_history_imports (
            district text NOT NULL, month date NOT NULL, api_rows integer NOT NULL,
            finished_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(district, month))""")
        db.execute("""CREATE INDEX CONCURRENTLY IF NOT EXISTS trades_district_contract_idx
            ON trades (split_part(complex_id,' ',2), contract_date)""")
        for code in codes:
            done = {r[0] for r in db.execute("SELECT month FROM trade_history_imports WHERE district=%s", (code,))}
            failed_months = 0
            for month in MONTHS:
                first = month.start_time.date()
                if first in done:
                    continue
                try:
                    complexes, rows = fetch_month(month.strftime("%Y%m"), code)
                except TradeDataError as exc:
                    missing.append(f"{code}/{month}")
                    failed_months += 1
                    print(f"INCOMPLETE {code} {month}: {exc}", flush=True)
                    continue
                except Exception as exc:
                    # Network exceptions can include the credentialed URL.
                    reason = type(exc).__name__
                    if isinstance(exc, MolitResponseError):
                        reason = str(exc)
                    elif isinstance(exc, HTTPError):
                        reason = f"HTTP {exc.code}"
                    raise SystemExit(f"{code} {month}: {reason}. Retry to resume.") from None
                total = sum(rows.values())
                with db.transaction():
                    db.execute("SELECT pg_advisory_xact_lock(%s)", (int(code),))
                    saved = db.execute("SELECT 1 FROM trade_history_imports WHERE district=%s AND month=%s",
                                       (code, first)).fetchone()
                    if saved:
                        continue
                    added, _ = reconcile(db, month, complexes, rows, code, history=True)
                    actual = db.execute("""SELECT count(*) FROM trades WHERE split_part(complex_id,' ',2)=%s
                        AND contract_date >= %s AND contract_date < %s""",
                        (DISTRICTS[code], first, (month+1).start_time.date())).fetchone()[0]
                    if actual != total:
                        raise ValueError(f"{code} {month}: API={total}, DB={actual}; month rolled back")
                    db.execute("INSERT INTO trade_history_imports(district, month, api_rows) VALUES (%s,%s,%s)",
                               (code, first, total))
                print(f"{code} {month}: verified={total}, added={added}", flush=True)
            if failed_months:
                print(f"INCOMPLETE {code}: {failed_months} months pending", flush=True)
                continue
            print(f"PASS {code}: {len(MONTHS)} historical months complete", flush=True)

    if missing:
        raise SystemExit("Pending months: " + ", ".join(missing))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--district", choices=TARGETS, help="Limit the import to one district")
    args = parser.parse_args()
    backfill([args.district] if args.district else TARGETS)
