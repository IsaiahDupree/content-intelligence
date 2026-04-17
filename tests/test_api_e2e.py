"""End-to-end API integration tests."""
import unittest
import json
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestAPIEndpoints(unittest.TestCase):
    """Test all API endpoints."""

    def setUp(self):
        """Set up test client."""
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_health_endpoint(self):
        """Test health check endpoint."""
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertEqual(data['status'], 'healthy')
        self.assertIn('version', data)
        self.assertIn('timestamp', data)

    def test_classify_endpoint(self):
        """Test classification endpoint."""
        response = self.client.post('/api/classify',
            json={"content": "I love this product! Highly recommend it!"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)

        self.assertEqual(data['status'], 'success')
        self.assertIn('format', data)
        self.assertIn('confidence', data)
        self.assertIn('all_scores', data)

    def test_classify_missing_content(self):
        """Test classification with missing content."""
        response = self.client.post('/api/classify', json={})
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertEqual(data['error'], 'content required')

    def test_score_endpoint(self):
        """Test scoring endpoint."""
        metrics = {
            "views": 10000,
            "likes": 500,
            "comments": 50,
            "shares": 25,
            "watch_time_avg": 0.7,
            "completion_rate": 0.8,
            "click_through_rate": 0.02,
            "conversions": 10
        }

        response = self.client.post('/api/score',
            json={"metrics": metrics, "content_id": "test_123"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)

        self.assertEqual(data['status'], 'success')
        self.assertIn('overall_score', data)
        self.assertIn('engagement_score', data)
        self.assertIn('retention_score', data)
        self.assertIn('conversion_score', data)

    def test_score_missing_metrics(self):
        """Test scoring with missing metrics."""
        response = self.client.post('/api/score', json={})
        self.assertEqual(response.status_code, 400)

    def test_repurpose_endpoint(self):
        """Test repurposing recommendations endpoint."""
        response = self.client.get('/api/repurpose/test_123',
            json={"format": "testimonial", "performance": {"overall_score": 70}})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)

        self.assertEqual(data['status'], 'success')
        self.assertIn('recommendations', data)
        self.assertGreater(len(data['recommendations']), 0)

        # Check recommendation structure
        rec = data['recommendations'][0]
        self.assertIn('format', rec)
        self.assertIn('viability_score', rec)
        self.assertIn('rationale', rec)

    def test_checkback_endpoint(self):
        """Test checkback endpoint."""
        response = self.client.post('/api/checkback/test_123')

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)

        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['content_id'], 'test_123')
        self.assertIn('job_id', data)

    def test_analyze_full_endpoint(self):
        """Test full end-to-end analysis endpoint."""
        payload = {
            "content": "Check out this amazing unboxing! First look at the packaging!",
            "content_id": "e2e_test_123",
            "metrics": {
                "views": 5000,
                "likes": 250,
                "comments": 25,
                "shares": 10,
                "watch_time_avg": 0.65,
                "completion_rate": 0.75,
                "click_through_rate": 0.01,
                "conversions": 5
            }
        }

        response = self.client.post('/api/analyze', json=payload)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)

        self.assertEqual(data['status'], 'success')
        self.assertIn('format_classification', data)
        self.assertIn('performance_score', data)
        self.assertIn('recommendations', data)

        # Verify structure
        self.assertIn('format', data['format_classification'])
        self.assertIn('confidence', data['format_classification'])

        self.assertIn('overall', data['performance_score'])
        self.assertIn('engagement', data['performance_score'])

        self.assertGreater(len(data['recommendations']), 0)

    def test_cors_headers(self):
        """Test CORS headers are present."""
        response = self.client.get('/health')

        # Check CORS headers
        self.assertIn('Access-Control-Allow-Origin', response.headers)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], '*')

    def test_security_headers(self):
        """Test security headers are present."""
        response = self.client.get('/health')

        # Check security headers
        self.assertIn('X-Content-Type-Options', response.headers)
        self.assertIn('X-Frame-Options', response.headers)
        self.assertIn('Content-Security-Policy', response.headers)


if __name__ == "__main__":
    unittest.main()
