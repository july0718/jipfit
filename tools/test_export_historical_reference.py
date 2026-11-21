import gzip
import json
from pathlib import Path
import tempfile
import unittest

from export_historical_reference import export_reference, quantile


class StaticReferenceTest(unittest.TestCase):
    def test_interpolated_percentiles(self):
        self.assertAlmostEqual(quantile([i for i in range(100)], .1), 9.9)
        self.assertAlmostEqual(quantile([i for i in range(100)], .9), 89.1)

    def test_reads_source_audit_and_creates_static_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.csv.gz"
            dest = Path(tmp) / "dist/metadata/forecast-reference.json"
            with gzip.open(audit, "wt", encoding="utf8") as file:
                file.write("horizon_months,predicted_return_percent,actual_return_percent\n")
                for h in (3, 6, 12):
                    for i in range(30):
                        file.write(f"{h},10,{i}\n")
                file.write("3,missing,10\n")
            result = export_reference(audit, dest)
            self.assertEqual([a["sample_count"] for a in result["horizons"]], [30,30,30])
            self.assertFalse(result["calibrated_for_current_model"])
            self.assertEqual(json.loads(dest.read_text(encoding="utf8")), result)

    def test_invalid_audit_never_generates_fabricated_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.csv.gz"
            dest = Path(tmp) / "dist/metadata/forecast-reference.json"
            with gzip.open(audit, "wt", encoding="utf8") as file:
                file.write("horizon_months,predicted_return_percent,actual_return_percent\n3,10,20\n")
            with self.assertRaises(ValueError):
                export_reference(audit, dest)
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
