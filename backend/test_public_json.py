import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from check_public_json import check, trade_filename
from districts import DISTRICTS


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


if __name__ == "__main__":
    unittest.main()

