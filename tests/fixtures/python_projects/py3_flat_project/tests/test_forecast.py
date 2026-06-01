import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.forecast import StoreForecast, forecast_for_store


class ForecastTest(unittest.TestCase):
    def test_forecast_uses_recent_history(self):
        self.assertEqual(forecast_for_store([2, 4, 8, 10], uplift=1.0), 7)

    def test_store_forecast_normalizes_sku(self):
        result = StoreForecast("S001").predict(" sku 42 ", [3, 3, 6])

        self.assertEqual(result["store_id"], "S001")
        self.assertEqual(result["sku"], "SKU-42")
        self.assertEqual(result["quantity"], 4)


if __name__ == "__main__":
    unittest.main()
