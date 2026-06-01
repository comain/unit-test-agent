import unittest

from legacy_job import legacy_ratio, legacy_total


class LegacyJobTest(unittest.TestCase):
    def test_legacy_total(self):
        self.assertEqual(legacy_total([{"qty": "2"}, {"qty": "3"}]), 5)

    def test_legacy_ratio_uses_python2_integer_division(self):
        self.assertEqual(legacy_ratio(3, 2), 1)


if __name__ == "__main__":
    unittest.main()
