import unittest

from score_sticky_notes import optimal_iou_matches, score


class StickyNoteScoringTests(unittest.TestCase):
    def test_matches_normalized_oracle_boxes_and_scores_text(self):
        result = score(
            {
                "image_size": [1000, 1000],
                "oracle": [{"bounding_box": [0.1, 0.2, 0.3, 0.4], "text": "Keep this note"}],
                "detections": [{"box": [205, 105, 395, 295], "text": "Keep this note"}],
            },
            0.5,
        )
        self.assertEqual(result["true_positives"], 1)
        self.assertEqual(result["false_positives"], 0)
        self.assertEqual(result["false_negatives"], 0)
        self.assertGreater(result["precision"], 0.99)
        self.assertGreater(result["recall"], 0.99)
        self.assertGreater(result["mean_matched_ocr_similarity"], 0.99)

    def test_route_bbox_is_normalized_against_image_size(self):
        result = score({
            "image_size": [1000, 1000],
            "oracle": [{"box": [100, 200, 300, 400], "text": "note"}],
            "detections": [{"bbox": [0.1, 0.2, 0.3, 0.4], "text": "note"}],
        }, 0.5)
        self.assertEqual(result["true_positives"], 1)

    def test_rejects_fixture_oracle_mismatch(self):
        with self.assertRaisesRegex(ValueError, "fixture/oracle mismatch"):
            score({
                "fixture_id": "quick-share",
                "oracle_id": "other-fixture",
                "oracle": [{"box": [0, 0, 1, 1]}],
                "detections": [],
            }, 0.5)

    def test_unmatched_detection_is_false_positive_and_oracle_is_missed(self):
        result = score(
            {
                "oracle": [{"box": [0, 0, 100, 100], "text": "expected"}],
                "detections": [{"box": [200, 200, 300, 300], "text": "other"}],
            },
            0.5,
        )
        self.assertEqual(result["true_positives"], 0)
        self.assertEqual(result["false_positives"], 1)
        self.assertEqual(result["false_negatives"], 1)
        self.assertEqual(result["precision"], 0.0)
        self.assertEqual(result["recall"], 0.0)

    def test_matching_maximizes_total_iou_not_oracle_order(self):
        oracle = [[0, 0, 100, 100], [50, 0, 150, 100]]
        detections = [
            {"box": [25, 0, 125, 100]},
            {"box": [0, 0, 100, 100]},
        ]
        matches = optimal_iou_matches(oracle, detections, 0.5)
        self.assertEqual(matches, {0: 1, 1: 0})

    def test_matching_maximizes_tp_cardinality_before_total_iou(self):
        oracle = [[0, 0, 100, 100], [0, 50, 100, 150]]
        detections = [
            {"box": [0, 0, 100, 100]},
            {"box": [0, -50, 100, 50]},
        ]
        matches = optimal_iou_matches(oracle, detections, 0.3)
        self.assertEqual(matches, {0: 1, 1: 0})

    def test_matching_is_one_to_one_for_adjacent_notes(self):
        result = score(
            {
                "oracle": [
                    {"box": [0, 0, 100, 100], "text": "left"},
                    {"box": [100, 0, 200, 100], "text": "right"},
                ],
                "detections": [
                    {"box": [0, 0, 100, 100], "text": "left"},
                    {"box": [100, 0, 200, 100], "text": "right"},
                ],
            },
            0.5,
        )
        self.assertEqual(result["true_positives"], 2)
        self.assertEqual(result["false_positives"], 0)
