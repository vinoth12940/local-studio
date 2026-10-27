import unittest
from stats import mean


class StatsTests(unittest.TestCase):
    def test_mean_of_integers(self):
        self.assertEqual(mean([2, 2, 2]), 2)

    def test_mean_keeps_fractions(self):
        self.assertEqual(mean([1, 2, 3, 4]), 2.5)


if __name__ == '__main__':
    unittest.main()
