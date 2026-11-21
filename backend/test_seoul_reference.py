"""Test feature #19 with synthetic row-level residuals; never fabricate production data."""
import unittest

from districts import DISTRICTS
from seoul_reference import build_reference, validate_reference

DIGEST = "a" * 64


def example_records():
    records = []
    for code in DISTRICTS:
        for month in (f"2025-{i:02d}" for i in range(4, 10)):
            for horizon in (3, 6, 12):
                predicted = 5.0
                actual = 5.0 + (int(code[-1]) - 4)
                records.append(dict(district=code, month=month, horizon=horizon, model="seoul",
                                    complex_id=f"{code}-{month}", area_group=60,
                                    predicted_return_percent=predicted, actual_return_percent=actual,
                                    residual_pp=actual-predicted))
    return records


class SeoulReferenceTests(unittest.TestCase):
    def test_quantiles_and_holdout_metadata(self):
        result = build_reference(example_records(), DIGEST)
        self.assertEqual(result["kind"], "seoul_hgb_holdout")
        self.assertEqual(result["training_cutoff"], "2025-04")
        self.assertFalse(result["calibrated_for_current_model"])
        self.assertEqual([r["months"] for r in result["horizons"]], [3, 6, 12])
        self.assertEqual([r["sample_count"] for r in result["horizons"]], [150, 150, 150])
        validate_reference(result)

    def test_requires_all_25_districts(self):
        records = example_records()
        with self.assertRaises(ValueError):
            build_reference([r for r in records if r["district"] != "11680"], DIGEST)

    def test_requires_all_six_holdout_months(self):
        with self.assertRaises(ValueError):
            build_reference([r for r in example_records() if r["month"] != "2025-05"], DIGEST)

    def test_rejects_gwangjin_and_aggregated_or_invented_residuals(self):
        records = example_records()
        records[0]["model"] = "gwangjin_2025"
        with self.assertRaises(ValueError):
            build_reference(records, DIGEST)
        records = example_records()
        records[0]["residual_pp"] = 0
        with self.assertRaises(ValueError):
            build_reference(records, DIGEST)

    def test_rejects_duplicate_predictions(self):
        records = example_records()
        records.append(dict(records[0]))
        with self.assertRaises(ValueError):
            build_reference(records, DIGEST)

    def test_rejects_nan_and_bad_artifact_digest(self):
        records = example_records()
        records[1]["actual_return_percent"] = float("nan")
        with self.assertRaises(ValueError):
            build_reference(records, DIGEST)
        with self.assertRaises(ValueError):
            build_reference(example_records(), "0"*32)


if __name__ == "__main__":
    unittest.main()
