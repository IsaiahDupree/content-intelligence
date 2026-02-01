"""
Tests for content-intelligence service health and endpoints.
"""
import pytest
from app import app


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_health_endpoint(client):
    """Test health check returns healthy status."""
    response = client.get('/health')
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'healthy'
    assert data['service'] == 'content-intelligence'
    assert 'version' in data
    assert 'timestamp' in data


def test_analyze_content(client):
    """Test content analysis endpoint."""
    response = client.post('/api/analyze/content', json={
        'title': 'Test Title',
        'description': 'Test description',
        'transcript': 'Test transcript'
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert 'analysis' in data


def test_generate_title(client):
    """Test title generation endpoint."""
    response = client.post('/api/generate/title', json={
        'content': 'Test content',
        'platform': 'tiktok',
        'style': 'viral',
        'count': 3
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert len(data['titles']) == 3


def test_generate_caption(client):
    """Test caption generation endpoint."""
    response = client.post('/api/generate/caption', json={
        'content': 'Test content',
        'platform': 'instagram',
        'include_hashtags': True
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert 'caption' in data
    assert 'hashtags' in data


def test_score_fate(client):
    """Test FATE scoring endpoint."""
    response = client.post('/api/score/fate', json={
        'content_id': 'test-uuid',
        'metrics': {}
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert 'fate_score' in data
    assert 'overall' in data['fate_score']


def test_classify_awareness(client):
    """Test awareness classification endpoint."""
    response = client.post('/api/classify/awareness', json={
        'content': 'Test content for awareness'
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert 'awareness_level' in data
    assert 'confidence' in data


def test_analyze_sentiment(client):
    """Test sentiment analysis endpoint."""
    response = client.post('/api/analyze/sentiment', json={
        'text': 'This is great!'
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert 'sentiment' in data
    assert 'score' in data


def test_vision_analyze_requires_path(client):
    """Test vision analysis requires image_path."""
    response = client.post('/api/vision/analyze', json={})
    assert response.status_code == 400


def test_recommend(client):
    """Test recommendations endpoint."""
    response = client.post('/api/recommend', json={
        'content_id': 'test-uuid',
        'type': 'similar',
        'count': 5
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'success'
    assert 'recommendations' in data
