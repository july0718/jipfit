"""Monthly existence check ported from jipfit_lifecycle@d86dcdf:monthly.py (plan-lifecycle-auto.md).

This module imports only the standard library, so lifecycle_admin.py can copy the reference data on a host
without the database packages. run_month loads the rest when it is called.
"""
import hashlib
import os
from pathlib import Path

# Raise this when a judgement rule changes.
RULES_VERSION = "1"
PACKAGE = Path(__file__).resolve().parent


def verifier_version():
    """The containers have no git, so the package files themselves identify the code."""
    digest = hashlib.sha256()
    for path in sorted(PACKAGE.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return f"jipfit/lifecycle_check@{RULES_VERSION}+{digest.hexdigest()[:12]}"


def data_dir():
    """lifecycle-data: the mounted folder in the updater, the repository's own folder on a host."""
    return Path(os.environ.get("JIPFIT_LIFECYCLE_DATA") or PACKAGE.parents[1] / "lifecycle-data")


def batches_dir():
    return Path(os.environ.get("JIPFIT_LIFECYCLE_DIR") or data_dir() / "batches")


def run_month(today=None, **options):
    """Run or resume this month's check; returns the failures for the caller to retry."""
    from lifecycle_check import stage

    return stage.run_month(today, **options)
