"""Run daily KST trade and monthly verified metadata updates, then publish JSON."""
import argparse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import sleep

import psycopg

from districts import DISTRICTS
from publish_snapshots import R2, publish, rollback
from refresh import KST, refresh_many, seconds_until_refresh
from refresh_metadata import apply_metadata


@contextmanager
def updater_lock():
    with psycopg.connect(connect_timeout=8, autocommit=True) as db:
        acquired, = db.execute("SELECT pg_try_advisory_lock(2011260600)").fetchone()
        if not acquired:
            raise RuntimeError("Another snapshot process is running")
        yield


def run(out, inbox, remote):
    failed = refresh_many(DISTRICTS)
    if failed:
        raise RuntimeError("Refresh failed for: " + ", ".join(failed))
    apply_metadata(inbox, datetime.now(KST).date())
    publish(out, remote)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/export"))
    parser.add_argument("--inbox", type=Path, default=Path("/metadata"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--rollback", metavar="VERSION")
    mode.add_argument("--publish-only", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            with updater_lock():
                remote = R2.configured()
                if args.rollback:
                    rollback(args.out, args.rollback, remote)
                    return
                if args.publish_only:
                    publish(args.out, remote)
                    return
                run(args.out, args.inbox, remote)
        except Exception as exc:
            # Avoid leaking credentials if an upstream client includes its request URL.
            print(f"Snapshot update failed ({type(exc).__name__}); check source access and configuration.", flush=True)
            if args.once or args.rollback or args.publish_only:
                raise SystemExit(1) from None
            sleep(3600)
        else:
            if args.once:
                return
            sleep(seconds_until_refresh())


if __name__ == "__main__":
    main()
