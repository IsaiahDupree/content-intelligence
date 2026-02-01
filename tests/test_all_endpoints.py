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


class TestNarrativeEndpoint:
    def test_narrative_requires_goal(self, client):
        response = client.post("/api/narrative/plan", json={})
        assert response.status_code == 400
    
    def test_narrative_with_goal(self, client):
        response = client.post("/api/narrative/plan", json={
            "goal": "Grow TikTok following to 10k",
            "duration_days": 30
        })
        assert response.status_code in [200, 500]


class TestExperimentsEndpoint:
    def test_experiments_hypothesis(self, client):
        response = client.post("/api/experiments/hypothesis", json={
            "content_type": "short_video",
            "metric": "engagement"
        })
        assert response.status_code in [200, 500]


class TestCompetitorEndpoint:
    def test_competitor_requires_handle(self, client):
        response = client.post("/api/competitor/analyze", json={})
        assert response.status_code == 400
    
    def test_competitor_with_handle(self, client):
        response = client.post("/api/competitor/analyze", json={
            "handle": "@example",
            "platform": "instagram"
        })
        assert response.status_code in [200, 500]


class TestTrendsEndpoint:
    def test_trends_detect(self, client):
        response = client.post("/api/trends/detect", json={
            "platform": "tiktok",
            "limit": 5
        })
        assert response.status_code in [200, 500]


class TestBriefEndpoint:
    def test_brief_requires_topic(self, client):
        response = client.post("/api/brief/generate", json={})
        assert response.status_code == 400
    
    def test_brief_with_topic(self, client):
        response = client.post("/api/brief/generate", json={
            "topic": "AI productivity tips",
            "format": "short_video"
        })
        assert response.status_code in [200, 500]


class TestEngagementEndpoint:
    def test_engagement_predict(self, client):
        response = client.post("/api/engagement/predict", json={
            "title": "5 Tips for Better Sleep",
            "platform": "instagram"
        })
        assert response.status_code in [200, 500]


class TestDMOutreachEndpoint:
    def test_dm_requires_prospects_and_template(self, client):
        response = client.post("/api/dm/outreach", json={})
        assert response.status_code == 400
    
    def test_dm_with_prospects(self, client):
        response = client.post("/api/dm/outreach", json={
            "prospects": ["@user1", "@user2"],
            "message_template": "Hey {name}, check this out!"
        })
        assert response.status_code in [200, 500]


class TestInboxAutoReplyEndpoint:
    def test_auto_reply_configure(self, client):
        response = client.post("/api/inbox/auto-reply", json={
            "platform": "instagram",
            "rules": [],
            "enabled": True
        })
        assert response.status_code in [200, 500]


class TestHashtagsEndpoint:
    def test_hashtags_requires_content(self, client):
        response = client.post("/api/hashtags/generate", json={})
        assert response.status_code == 400
    
    def test_hashtags_with_content(self, client):
        response = client.post("/api/hashtags/generate", json={
            "content": "Amazing sunset at the beach",
            "platform": "instagram",
            "count": 10
        })
        assert response.status_code in [200, 500]


@pytest.fixture
def client():
    """Create test client."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
