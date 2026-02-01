#!/usr/bin/env python3
"""
Integration tests for all content-intelligence endpoints.
Run with: pytest tests/test_all_endpoints.py -v
"""
import pytest
import json
import os


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["status"] == "healthy"
        assert data["service"] == "content-intelligence"


class TestFATEScoreEndpoint:
    def test_fate_score_works(self, client):
        response = client.post("/api/score/fate", json={
            "content": "Most founders fail because they miss this pattern. I helped 127 entrepreneurs."
        })
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "fate_score" in data
        assert "focus" in data["fate_score"]
        assert "authority" in data["fate_score"]
        assert "tribe" in data["fate_score"]
        assert "emotion" in data["fate_score"]


class TestAwarenessClassifyEndpoint:
    def test_awareness_classify_works(self, client):
        response = client.post("/api/classify/awareness", json={
            "content": "Are you struggling with getting views on your content?"
        })
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "awareness_level" in data


class TestSentimentAnalyzeEndpoint:
    def test_sentiment_analyze_works(self, client):
        response = client.post("/api/analyze/sentiment", json={
            "text": "This is amazing! I love how easy it is to use."
        })
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "sentiment" in data
        assert "score" in data


class TestVisionAnalyzeEndpoint:
    def test_vision_analyze_requires_input(self, client):
        response = client.post("/api/vision/analyze", json={})
        assert response.status_code == 400


class TestTitleGenerateEndpoint:
    def test_title_generate_works(self, client):
        response = client.post("/api/generate/title", json={
            "content": "How to grow your TikTok following fast",
            "platform": "tiktok",
            "count": 3
        })
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "titles" in data


class TestCaptionGenerateEndpoint:
    def test_caption_generate_works(self, client):
        response = client.post("/api/generate/caption", json={
            "content": "Video about productivity tips",
            "platform": "instagram"
        })
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "caption" in data


@pytest.fixture
def client():
    """Create test client."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
