"""Apply browser-collected coordinates to matching apartments, preserving existing points."""
import argparse
import json
from pathlib import Path

import psycopg


def apply(document):
    points = document["coordinates"]
    if not isinstance(points, dict) or not points:
        raise ValueError("Expected a nonempty coordinates object")
    updated = 0
    with psycopg.connect() as db:
        for cid, p in points.items():
            lat, lng = p["lat"], p["lng"]
            if not (33 <= lat <= 39 and 124 <= lng <= 132):
                raise ValueError(f"Invalid coordinates for {cid}")
            row = db.execute("SELECT address FROM complexes WHERE complex_id=%s", (cid,)).fetchone()
            if not row or row[0] != p["address"]:
                raise ValueError(f"Unknown complex or changed address: {cid}")
            updated += db.execute("""UPDATE complexes SET latitude=%s, longitude=%s
                WHERE complex_id=%s AND (latitude IS NULL OR longitude IS NULL)""", (lat, lng, cid)).rowcount
    print(f"Updated {updated} complexes; {len(points)-updated} already positioned.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    apply(json.loads(parser.parse_args().file.read_text(encoding="utf-8-sig")))
