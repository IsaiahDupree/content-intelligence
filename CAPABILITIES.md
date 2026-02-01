# Content Intelligence Service - Capabilities

## Overview
AI-powered content analysis and generation service for the MediaPoster ecosystem.

**Port**: 6006  
**Repository**: https://github.com/IsaiahDupree/content-intelligence

## Real Implementations

| Endpoint | Implementation | Source |
|----------|---------------|--------|
| `/api/score/fate` | **FATEScorer** | `services/analysis/fate_scorer.py` |
| `/api/classify/awareness` | **AwarenessClassifier** | `services/analysis/awareness_classifier.py` |
| `/api/analyze/sentiment` | **SentimentAnalyzer** | `services/analysis/sentiment_analyzer_standalone.py` |
| `/api/generate/title` | **Groq/OpenAI** | AI with fallback |
| `/api/generate/caption` | **Groq/OpenAI** | AI with fallback |

## API Endpoints

### Health Check
```bash
curl http://localhost:6006/health
```

### FATE Scoring (Real Implementation)
```bash
curl -X POST http://localhost:6006/api/score/fate \
  -H "Content-Type: application/json" \
  -d '{"content": "Most founders fail because they miss this one pattern. I helped 127 entrepreneurs discover the mechanism."}'
```
**Response:**
```json
{
  "fate_score": {
    "focus": 0.55,
    "authority": 0.62,
    "tribe": 0.3,
    "emotion": 0.0,
    "overall": 0.37
  },
  "implementation": "real"
}
```

### Awareness Classification (Real Implementation)
```bash
curl -X POST http://localhost:6006/api/classify/awareness \
  -H "Content-Type: application/json" \
  -d '{"content": "Are you struggling with getting views on your content?"}'
```
**Response:**
```json
{
  "awareness_level": "problem_aware",
  "confidence": 0.82,
  "implementation": "real"
}
```

### Sentiment Analysis (Real Implementation)
```bash
curl -X POST http://localhost:6006/api/analyze/sentiment \
  -H "Content-Type: application/json" \
  -d '{"text": "This is amazing! I love how easy it is to use."}'
```
**Response:**
```json
{
  "sentiment": "positive",
  "score": 0.67,
  "confidence": 0.55,
  "emotions": {"joy": 0.6},
  "themes": [],
  "implementation": "real"
}
```

### Title Generation (AI-Powered)
```bash
curl -X POST http://localhost:6006/api/generate/title \
  -H "Content-Type: application/json" \
  -d '{"content": "How to grow your TikTok", "platform": "tiktok", "style": "viral", "count": 3}'
```

### Caption Generation (AI-Powered)
```bash
curl -X POST http://localhost:6006/api/generate/caption \
  -H "Content-Type: application/json" \
  -d '{"content": "Video about productivity tips", "platform": "instagram", "tone": "professional"}'
```

## Architecture

```
content-intelligence/
├── app.py                    # Flask application
├── services/
│   └── analysis/
│       ├── fate_scorer.py           # FATE scoring (F/A/T/E)
│       ├── awareness_classifier.py  # 5 awareness levels
│       └── sentiment_analyzer_standalone.py  # Sentiment analysis
├── config/
│   └── settings.py           # Environment configuration
└── shared/
    └── service_client.py     # Inter-service HTTP client
```

## Dependencies
```
flask>=3.0.0
httpx>=0.27.0
python-dotenv>=1.0.0
openai>=1.0.0
groq>=0.4.0
loguru>=0.7.0
```

## Environment Variables
```bash
PORT=6006
OPENAI_API_KEY=sk-...      # For AI title/caption generation
GROQ_API_KEY=gsk-...       # Preferred AI provider (faster)
ANTHROPIC_API_KEY=sk-ant-... # Optional fallback
```
