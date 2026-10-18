"""Tests for UGC Format Classification, Scoring, and Recommendations."""
import unittest
from services.ugc_format.classifier import UGCFormatClassifier
from services.ugc_format.scorer import PerformanceScorer
from services.ugc_format.recommender import RepurposingRecommender


class TestUGCClassifier(unittest.TestCase):
    """Test UGC Format Classifier."""

    def setUp(self):
        self.classifier = UGCFormatClassifier()

    def test_testimonial_detection(self):
        """Test detection of testimonial format."""
        content = "I absolutely love this product! It's amazing. Highly recommend to everyone!"
        result = self.classifier.classify(content)
        self.assertEqual(result.format, "testimonial")
        self.assertGreater(result.confidence, 0.2)

    def test_unboxing_detection(self):
        """Test detection of unboxing format."""
        content = "Let's unbox this brand new product! First look at the packaging. So excited!"
        result = self.classifier.classify(content)
        self.assertEqual(result.format, "unboxing")
        self.assertGreater(result.confidence, 0.2)

    def test_demo_detection(self):
        """Test detection of demo format."""
        content = "Here's how to use this feature. Let me show you step by step."
        result = self.classifier.classify(content)
        self.assertEqual(result.format, "demo")

    def test_confidence_normalization(self):
        """Test that confidence is normalized 0-1."""
        content = "Test content"
        result = self.classifier.classify(content)
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)


class TestPerformanceScorer(unittest.TestCase):
    """Test Performance Scoring."""

    def setUp(self):
        self.scorer = PerformanceScorer()

    def test_high_performance_scoring(self):
        """Test scoring for high-performing content."""
        metrics = {
            "views": 100000,
            "likes": 15000,
            "comments": 2000,
            "shares": 1000,
            "watch_time_avg": 0.85,
            "completion_rate": 0.90,
            "click_through_rate": 0.10,
            "conversions": 500
        }
        result = self.scorer.score(metrics)
        self.assertGreater(result.overall_score, 50)
        self.assertGreater(result.engagement_score, 0)
        self.assertGreater(result.retention_score, 50)

    def test_low_performance_scoring(self):
        """Test scoring for low-performing content."""
        metrics = {
            "views": 100,
            "likes": 5,
            "comments": 0,
            "shares": 0,
            "watch_time_avg": 0.1,
            "completion_rate": 0.2,
            "click_through_rate": 0.01,
            "conversions": 0
        }
        result = self.scorer.score(metrics)
        self.assertLess(result.overall_score, 50)

    def test_score_normalization(self):
        """Test that scores are normalized 0-100."""
        metrics = {
            "views": 50000,
            "likes": 5000,
            "comments": 500,
            "shares": 250,
            "watch_time_avg": 0.75,
            "completion_rate": 0.80,
            "click_through_rate": 0.05,
            "conversions": 100
        }
        result = self.scorer.score(metrics)
        self.assertGreaterEqual(result.overall_score, 0)
        self.assertLessEqual(result.overall_score, 100)
        self.assertGreaterEqual(result.engagement_score, 0)
        self.assertLessEqual(result.engagement_score, 100)


class TestRepurposingRecommender(unittest.TestCase):
    """Test Repurposing Recommendations."""

    def setUp(self):
        self.recommender = RepurposingRecommender()

    def test_testimonial_recommendations(self):
        """Test recommendations for testimonial format."""
        performance = {"overall_score": 75}
        recommendations = self.recommender.recommend("testimonial", performance)

        # Should have 5 recommendations (one for each other format)
        self.assertEqual(len(recommendations), 5)

        # Highest should be story or lifestyle
        self.assertIn(recommendations[0].format, ["story", "lifestyle"])
        self.assertGreater(recommendations[0].viability_score, 75)

    def test_recommendation_scoring(self):
        """Test that recommendations have valid scores."""
        recommendations = self.recommender.recommend("unboxing", {"overall_score": 50})

        for rec in recommendations:
            self.assertGreaterEqual(rec.viability_score, 0)
            self.assertLessEqual(rec.viability_score, 100)
            self.assertIsNotNone(rec.rationale)

    def test_performance_affects_viability(self):
        """Test that performance score affects viability recommendations."""
        high_perf = {"overall_score": 90}
        low_perf = {"overall_score": 20}

        high_recs = self.recommender.recommend("demo", high_perf)
        low_recs = self.recommender.recommend("demo", low_perf)

        # High performance should generally have higher viability
        avg_high = sum(r.viability_score for r in high_recs) / len(high_recs)
        avg_low = sum(r.viability_score for r in low_recs) / len(low_recs)

        self.assertGreater(avg_high, avg_low)


if __name__ == "__main__":
    unittest.main()
