import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from check_public_json import check, trade_filename
from districts import DISTRICTS
from publish_snapshots import R2, atomic_write, encode, materialize, rollback
from refresh_metadata import validate


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.release = "releases/20261121T000000000000Z"
        self.first_code, self.first_name = next(iter(DISTRICTS.items()))
        for code, name in DISTRICTS.items():
            cid = f"서울 {name} 샘플"
            key = {"complex_id": cid, "area_group": 60}
            self.write(self.release + f"/complexes/{code}.json", [key])
            self.write(self.release + f"/latest-trades/{code}.json", [key])
            self.write(self.release + f"/transactions/{code}/{trade_filename(cid)}",
                       {"complex_id": cid, "area_groups": {"60": [
                           {"contract_date": "2026-09-01", "area": 59.8,
                            "floor": 3, "price_manwon": 100000, "is_cancelled": True}
                       ]}})
        self.manifest = {"schema_version": 1, "release": self.release,
                         "districts": [{"code": code, "name": name} for code, name in DISTRICTS.items()],
                         "totals": {"complexes": 25, "area_groups": 25,
                                    "transactions": 25, "cancelled_transactions": 25}}

    def write(self, path, payload):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_validation_can_run_before_publishing_manifest(self):
        self.assertEqual(check(self.root, manifest=self.manifest), self.manifest["totals"])
        self.assertFalse((self.root / "manifest.json").exists())

    def test_missing_district_fails(self):
        path = self.release + f"/complexes/{self.first_code}.json"
        self.write(path, [])
        with self.assertRaisesRegex(ValueError, self.first_code):
            check(self.root, manifest=self.manifest)

    def test_mismatched_group_fails_even_with_identical_row_count(self):
        path = self.release + f"/latest-trades/{self.first_code}.json"
        self.write(path, [{"complex_id": f"서울 {self.first_name} 샘플", "area_group": 85}])
        with self.assertRaisesRegex(ValueError, self.first_code):
            check(self.root, manifest=self.manifest)

    def test_manifest_counts_cancelled_trades(self):
        self.manifest["totals"]["cancelled_transactions"] = 0
        with self.assertRaisesRegex(ValueError, "File totals differ"):
            check(self.root, manifest=self.manifest)


    def test_versioned_publication_updates_only_changed_trade_and_index(self):
        public = self.root / "public"
        first, initial = materialize(public, self.root, self.manifest)
        cid = f"서울 {self.first_name} 샘플"
        path = self.release + f"/transactions/{self.first_code}/{trade_filename(cid)}"
        doc = json.loads((self.root / path).read_bytes())
        doc["area_groups"]["60"][0]["price_manwon"] += 1
        self.write(path, doc)
        second, changed = materialize(public, self.root, self.manifest)
        self.assertEqual(len(changed - initial), 2)
        self.assertEqual(first["districts"][1:], second["districts"][1:])
        self.assertEqual(check(public, manifest=second), self.manifest["totals"])
        self.assertEqual(check(public, manifest=first), self.manifest["totals"])

    def test_rollback_repoints_to_previous_verified_content(self):
        public = self.root / "public"
        manifest, _ = materialize(public, self.root, self.manifest)
        atomic_write(public / manifest["release"] / "manifest.json", encode(manifest))
        atomic_write(public / "manifest.json", b"prior active manifest placeholder")
        rollback(public, manifest["release"].split("/")[-1])
        self.assertEqual(check(public), manifest["totals"])

    def test_r2_stages_new_objects_before_the_manifest_switch(self):
        class FakeR2(R2):
            def __init__(self):
                self.stored = {}
                self.writes = []
                self.fail = False

            def get(self, key):
                return self.stored.get(key), '"old"' if key in self.stored else None

            def immutable(self, key, data):
                if self.fail:
                    raise RuntimeError("simulated failure")
                self.stored.setdefault(key, json.loads(data))
                self.writes.append(key)

            def put(self, key, data, cache, **condition):
                self.stored[key] = json.loads(data)
                self.writes.append(key)

        public = self.root / "public"
        first, initial = materialize(public, self.root, self.manifest)
        remote = FakeR2()
        remote.publish(public, first, initial)
        self.assertEqual(remote.writes[-1], "manifest.json")
        cid = f"서울 {self.first_name} 샘플"
        path = self.release + f"/transactions/{self.first_code}/{trade_filename(cid)}"
        doc = json.loads((self.root / path).read_bytes())
        doc["area_groups"]["60"][0]["price_manwon"] += 1
        self.write(path, doc)
        second, changed = materialize(public, self.root, self.manifest)
        second["release"] = "releases/20261122T000000000000Z"
        remote.fail = True
        with self.assertRaises(RuntimeError):
            remote.publish(public, second, changed)
        self.assertEqual(remote.stored["manifest.json"], first)
        remote.writes.clear()
        remote.fail = False
        remote.publish(public, second, changed)
        self.assertEqual(sum(key.startswith("objects/") for key in remote.writes), 2)
        self.assertEqual(remote.stored["manifest.json"], second)

    def test_metadata_validation_requires_evidence_and_a_coordinate_pair(self):
        batch = "2026-11"
        row = {"complex_id": "서울 광진구 샘플", "expected_address": "서울 광진구 샘플",
               "evidence": "verified-source-record", "changes": {
                   "latitude": 37.5, "longitude": 127.1}}
        doc = {"batch": batch, "complexes": [row]}
        validate(doc, batch, 2026)
        del row["changes"]["longitude"]
        with self.assertRaisesRegex(ValueError, "numeric pair"):
            validate(doc, batch, 2026)
        row["evidence"] = ""
        with self.assertRaisesRegex(ValueError, "evidence"):
            validate(doc, batch, 2026)


if __name__ == "__main__":
    unittest.main()

