import unittest

from nms.anomaly import detect_anomalies


class AnomalyTests(unittest.TestCase):
    def test_detects_outlier_against_median_baseline(self):
        labels = [str(index) for index in range(7)]
        values = [10, 11, 10, 12, 11, 10, 100]
        result = detect_anomalies(labels, values)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["index"], 6)
        self.assertEqual(result[0]["severity"], "high")

    def test_ignores_insufficient_samples(self):
        self.assertEqual(detect_anomalies(["1", "2"], [1, 100]), [])


if __name__ == "__main__":
    unittest.main()
