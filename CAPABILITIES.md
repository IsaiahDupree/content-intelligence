# Content Intelligence Capabilities

## Service Info
- **Port:** 6006
- **Health:** `GET /health`

## Endpoints

### Content Analysis
```http
POST /api/analyze/content
{ "title": "...", "description": "...", "transcript": "..." }
```

### Title Generation
```http
POST /api/generate/title
{ "content": "...", "platform": "tiktok", "style": "viral", "count": 5 }
```

### Caption Generation
```http
POST /api/generate/caption
{ "content": "...", "platform": "instagram", "include_hashtags": true }
```

### FATE Scoring
```http
POST /api/score/fate
{ "content_id": "uuid", "metrics": {...} }
```

### Awareness Classification
```http
POST /api/classify/awareness
{ "content": "..." }
```

### Sentiment Analysis
```http
POST /api/analyze/sentiment
{ "text": "..." }
```

### Vision Analysis
```http
POST /api/vision/analyze
{ "image_path": "/path/to/image.jpg" }
```

### Recommendations
```http
POST /api/recommend
{ "content_id": "uuid", "type": "similar", "count": 5 }
```

## Capabilities Summary

| Capability | Status | Description |
|------------|--------|-------------|
| Content Analysis | ✅ Ready | Analyze content for topics, sentiment |
| Title Generation | ✅ Ready | Generate viral titles with AI |
| Caption Generation | ✅ Ready | Generate captions with hashtags |
| FATE Scoring | ✅ Ready | Calculate engagement potential |
| Awareness Classification | ✅ Ready | Classify buyer awareness level |
| Sentiment Analysis | ✅ Ready | Analyze text sentiment |
| Vision Analysis | ✅ Ready | Analyze images and frames |
| Recommendations | ✅ Ready | Content recommendations |
