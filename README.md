# Content Intelligence

Content Intelligence now contains two production surfaces: the original UGC format analysis API and the autonomous cross-platform Social Market Tape.

## Social Market Tape

Market Tape appends real metric observations across YouTube, TikTok, Instagram, X, Facebook, and Threads; archives raw provider payloads; computes velocity, acceleration, relative strength, trends, social candles, and predictions; and records quota, cost, source health, and central-sync receipts.

Discovery is market-led rather than niche-led. YouTube category charts provide a keyword-independent current universe, then the adaptive frontier ranks fresh terms by views/hour, breadth, engagement, repeat observations, freshness, and concentration. Exact external discovery queries are preserved as immutable video lineage and receive their own query-frontier ranking, so a measured Google or platform trend can be expanded without being reduced to generic title fragments. Most query capacity follows measured winners while a bounded fraction rotates across broad market sectors.

The lock-safe local runtime is installed under `~/Library/Application Support/ContentIntelligence`, supervised by launchd, and available at `http://127.0.0.1:6006/api/market-tape/status`.

Full architecture, commands, source states, APIs, safety controls, and current production evidence: [docs/MARKET-TAPE-V1.md](docs/MARKET-TAPE-V1.md). The current topic-neutral market snapshot is [docs/MARKET-TREND-RESEARCH-2026-08-19.md](docs/MARKET-TREND-RESEARCH-2026-08-19.md).

The local transcript bank turns performance-qualified Market Tape observations into auditable evidence. It downloads source audio to the configured external storage volume, transcribes locally with OpenAI Whisper, saves timestamped transcript JSON, and binds the audio/transcript SHA-256 hashes to the exact append-only metric observation. Cohorts fail closed below five videos, three creators, or 100,000 observed views; script relatability remains explicitly predictive until the published script has real audience outcomes.

Install or update unattended operation:

```bash
./scripts/install_market_tape_launchd.sh
```

Verify the Market Tape software:

```bash
python3 -m pytest tests/test_market_tape.py -q
python3 -m services.market_tape.cli keywords --limit 100 --window-hours 168 --min-videos 2
python3 -m services.market_tape.cli intelligence --limit 25 --window-hours 168 --min-videos 2
python3 -m services.market_tape.cli query-frontier --limit 100 --window-hours 168 --min-videos 2
python3 -m services.market_tape.cli sync --reconcile --force --drain --max-batches 250
python3 scripts/research_youtube_queries.py --query-file docs/adaptive-query-expansion-2026-08-19.txt --output-dir '/Volumes/My Passport/MarketTape/trend-frontier/manual'
python3 scripts/backfill_transcript_bank.py --platform youtube --limit 20 --model base --cookies-from-browser chrome --topic 'creator burnout creative struggle content views work'
```

`market_tape_app.py` is the lightweight production API entry point used by launchd. It avoids loading the UGC model/provider stack for Market Tape-only health, status, ranking, collection, and sync operations.

Install the hourly resumable backfill worker (20 performance-ranked videos per
batch; existing artifacts are skipped):

```bash
/bin/zsh -l scripts/install_transcript_backfill_launchd.sh
```

The worker stores audio, timestamped Whisper JSON, hashes, and append-only run
manifests under `/Volumes/My Passport/MarketTape/transcript-bank`.

Deploy and verify the Market Tape control-plane schema:

```bash
python3 scripts/market_tape_migration.py validate
python3 scripts/market_tape_migration.py status
python3 scripts/market_tape_migration.py apply --project-ref ivhfuhxorppptyuofbgq
python3 scripts/market_tape_migration.py verify --project-ref ivhfuhxorppptyuofbgq
python3 scripts/market_tape_migration.py counts --project-ref ivhfuhxorppptyuofbgq
python3 -m services.market_tape.cli sync --force --drain --max-batches 250
```

## UGC Format Classifier

A service for analyzing user-generated content, detecting format types, calculating performance scores, and recommending content repurposing strategies.

## Features

### 🎯 Core Capabilities

- **Format Classification**: Detects UGC format (testimonial, unboxing, demo, lifestyle, story, trend) with confidence scoring
- **Performance Scoring**: Calculates engagement, retention, and conversion metrics on a 0-100 scale
- **Repurposing Recommendations**: Suggests format adaptation strategies based on content characteristics and performance
- **Supabase Integration**: Persists all analysis results to Supabase with proper schema
- **Request Caching**: 24-hour TTL caching for classification results
- **Comprehensive Logging**: Structured JSON request/response logging
- **Security**: CORS headers, CSP, input validation, error handling

## Quick Start

### Installation

```bash
cd /Users/isaiahdupree/Documents/Software/content-intelligence
pip3 install -r requirements.txt
cp .env.example .env
# Edit .env with actual credentials
python3 app.py
```

Service runs on `http://localhost:6006`

## API Endpoints

### Classification
```bash
POST /api/classify
{
  "content": "Your UGC content",
  "content_id": "optional_id"
}
→ {format, confidence, all_scores, reasoning}
```

### Scoring
```bash
POST /api/score
{
  "content_id": "post_123",
  "metrics": {
    "views": 50000,
    "likes": 5000,
    "comments": 500,
    "shares": 250,
    "watch_time_avg": 0.75,
    "completion_rate": 0.80,
    "click_through_rate": 0.05,
    "conversions": 100
  }
}
→ {overall_score, engagement_score, retention_score, conversion_score, metrics_breakdown}
```

### Repurposing
```bash
GET /api/repurpose/{content_id}
{
  "format": "testimonial",
  "performance": {"overall_score": 75}
}
→ {recommendations: [{format, viability_score, rationale}]}
```

### Full E2E Analysis
```bash
POST /api/analyze
{content, content_id, metrics}
→ Complete analysis stored in Supabase
```

### Health Check
```bash
GET /health
→ {status, service, version, timestamp}
```

## Testing

```bash
# Run all tests
python3 -m pytest tests/ -v

# Unit tests only
python3 -m pytest tests/test_ugc_format.py -v

# E2E API tests
python3 -m pytest tests/test_api_e2e.py -v
```

## Architecture

### Services
- **Classifier** (`services/ugc_format/classifier.py`) - Detects content formats
- **Scorer** (`services/ugc_format/scorer.py`) - Calculates performance metrics
- **Recommender** (`services/ugc_format/recommender.py`) - Generates repurposing suggestions
- **Cache** (`services/ugc_format/cache.py`) - 24h classification caching

### Middleware
- **Validation** - Request schema validation
- **Errors** - Centralized error handling
- **Security** - CORS, CSP, security headers
- **Logging** - Structured JSON logging
- **Supabase** - Database persistence

## Database Schema

### New Columns in `content` Table

**ai_ugc_format** (jsonb)
```json
{
  "format": "testimonial|unboxing|demo|lifestyle|story|trend",
  "confidence": 0.0-1.0,
  "all_scores": {...},
  "reasoning": "..."
}
```

**ai_performance_score** (jsonb)
```json
{
  "overall_score": 0-100,
  "engagement_score": 0-100,
  "retention_score": 0-100,
  "conversion_score": 0-100,
  "metrics_breakdown": {...},
  "timestamp": "ISO8601"
}
```

**ai_repurpose_formats** (jsonb)
```json
{
  "recommendations": [{format, viability_score, rationale}],
  "updated_at": "ISO8601"
}
```

## Configuration

### Environment Variables (.env)
```
PORT=6006
SUPABASE_URL=https://ivhfuhxorppptyuofbgq.supabase.co
SUPABASE_SERVICE_KEY=your_key
GROQ_API_KEY=your_key (optional)
OPENAI_API_KEY=your_key (optional)
```

## Deployment

### Vercel
```json
{
  "buildCommand": "pip install -r requirements.txt",
  "startCommand": "python3 app.py",
  "env": {
    "SUPABASE_URL": "@supabase_url",
    "SUPABASE_SERVICE_KEY": "@supabase_key"
  }
}
```

### Local Production
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:6006 app:app
```

## Performance Metrics

- Classification: <100ms (with caching)
- Scoring: <50ms
- Recommendations: <30ms
- Full E2E: <300ms

## Status

✅ **Features Completed: 26/59 (44%)**
- Phase 1-3: Core API, Classification, Scoring
- Phase 4-5: Repurposing Engine, Supabase Integration
- Unit & E2E Tests: 20 tests passing

🚀 **Ready for:** Production deployment, Supabase migration, E2E validation

## Next Steps

1. Apply Supabase migrations
2. Configure environment variables
3. Deploy to Vercel
4. Monitor /api/health endpoint
5. Integrate with MPLite/ACTP workflows

---

Created: 2026-04-17 | Last Updated: 2026-04-17
