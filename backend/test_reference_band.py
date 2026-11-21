"""Feature 19 historical reference tests. Run: python -m unittest test_reference_band"""
import unittest

import pandas as pd

from reference_band import historical_reference, summarize_reference


class ReferenceBandTest(unittest.TestCase):
    def test_residual_quantiles_are_actual_minus_predicted(self):
        rows = []
        for months in (3, 6, 12):
            for i in range(100):
                rows.append({
                    "horizon_months": months,
                    "predicted_return_percent": 10,
                    "actual_return_percent": 10 + i - 50,
                })
        result = summarize_reference(pd.DataFrame(rows))
        self.assertFalse(result["calibrated_for_current_model"])
        self.assertEqual([row["months"] for row in result["horizons"]], [3, 6, 12])
        for row in result["horizons"]:
            self.assertEqual(row["sample_count"], 100)
            self.assertAlmostEqual(row["p10_pp"], -40.1, places=8)
            self.assertAlmostEqual(row["p90_pp"], 39.1, places=8)

    def test_bundled_audit_is_readable_and_has_reported_sample_counts(self):
        historical_reference.cache_clear()
        result = historical_reference()
        self.assertIsNotNone(result, "The shipped audit.csv.gz must be readable")
        self.assertEqual([row["months"] for row in result["horizons"]], [3, 6, 12])
        self.assertEqual([row["sample_count"] for row in result["horizons"]], [1510, 1143, 535])
        for row in result["horizons"]:
            self.assertLessEqual(row["p10_pp"], row["p90_pp"])

    def test_missing_horizon_must_not_create_fabricated_bounds(self):
        rows = pd.DataFrame({
            "horizon_months": [3] * 100,
            "predicted_return_percent": [0] * 100,
            "actual_return_percent": [10] * 100,
        })
        with self.assertRaises(ValueError):
            summarize_reference(rows)

    def test_invalid_rows_are_ignored_not_filled_with_zeros(self):
        rows = []
        for months in (3, 6, 12):
            rows.extend([{"horizon_months": months, "predicted_return_percent": 0,
                          "actual_return_percent": 1}] * 30)
            rows.append({"horizon_months": months,
                         "predicted_return_percent": 0, "actual_return_percent": float("nan")})
        result = summarize_reference(pd.DataFrame(rows))
        self.assertEqual([r["sample_count"] for r in result["horizons"]], [30,30,30])


if __name__ == "__main__":
    unittest.main()
