# Content Intelligence Service - Capabilities

## Overview
AI-powered content analysis and generation service for the MediaPoster ecosystem.

**Port**: 6006  
**Repository**: https://github.com/IsaiahDupree/content-intelligence  
**Total Code**: 58,594 lines (moved from MediaPoster)

## Real Implementations

| Endpoint | Implementation | Source |
|----------|---------------|--------|
| `/api/score/fate` | ✅ **FATEScorer** | `services/analysis/fate_scorer.py` |
| `/api/classify/awareness` | ✅ **AwarenessClassifier** | `services/analysis/awareness_classifier.py` |
| `/api/analyze/sentiment` | ✅ **SentimentAnalyzer** | `services/analysis/sentiment_analyzer_standalone.py` |
| `/api/vision/analyze` | ✅ **VisionAnalyzer** | `services/analysis/vision_analyzer_standalone.py` |
| `/api/generate/title` | ✅ **Groq/OpenAI** | AI with fallback |
| `/api/generate/caption` | ✅ **Groq/OpenAI** | AI with fallback |
| `/api/narrative/plan` | ✅ **NarrativeScheduler** | `services/narrative/scheduler.py` |
| `/api/experiments/hypothesis` | ✅ **HypothesisEngine** | `services/experiments/hypothesis_engine.py` |
| `/api/competitor/analyze` | ✅ **CompetitorAnalyzer** | `services/competitor_audit/competitor_analyzer.py` |
| `/api/trends/detect` | ✅ **TrendDetector** | `services/trend_intelligence/trend_detector.py` |
| `/api/brief/generate` | ✅ **BriefGenerator** | `services/content_brief/brief_generator.py` |
| `/api/engagement/predict` | ✅ **EngagementService** | `services/engagement/engagement_service.py` |
| `/api/dm/outreach` | ✅ **OutreachSequencer** | `services/dm_outreach/outreach_sequencer.py` |
| `/api/inbox/auto-reply` | ✅ **AutoReplyEngine** | `services/inbox/auto_reply_engine.py` |
| `/api/hashtags/generate` | ✅ **HashtagGenerator** | `services/instagram/hashtag_generator.py` |
| `/api/crm/leads` | ✅ **CRM** | External: `/Local EverReach CRM/` |
| `/api/crm/relationship-score` | ✅ **CRM** | External: `/Local EverReach CRM/` |
| `/api/safari/publish` | ✅ **Safari** | External: `/Safari Automation/` |
| `/api/safari/dm` | ✅ **Safari** | External: `/Safari Automation/` |

## Services Copied from MediaPoster

### Analysis Services (2,142 lines)
| File | Lines | Purpose |
|------|-------|---------|
| `fate_scorer.py` | 291 | FATE framework scoring |
| `awareness_classifier.py` | 318 | Eugene Schwartz awareness levels |
| `sentiment_analyzer.py` | 338 | AI sentiment analysis |
| `sentiment_analyzer_standalone.py` | 168 | Rule-based sentiment |
| `vision_analyzer_standalone.py` | 230 | OpenAI Vision API |
| `ai_content_analyzer.py` | 492 | Content analysis orchestration |

### Generation Services (1,114 lines)
| File | Lines | Purpose |
|------|-------|---------|
| `ai_title_generator.py` | 527 | Viral title generation |
| `ai_content_generator.py` | 237 | Content generation |
| `hook_generator.py` | 350 | Hook pattern generation |

### Recommendation Services (105 lines)
| File | Lines | Purpose |
|------|-------|---------|
| `ai_recommendation_service.py` | 105 | Content recommendations |

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

---

## CIOS (Content Intelligence Operating System)

**Port**: 5570
**Repository**: Media Vault (`/media-vault/backend/`)
**Status**: 🟡 In Development (0/25 features)
**Launch Script**: `harness/launch-cios.sh`

CIOS is the orchestration and command-center layer built on top of content-intelligence. It coordinates multiple pipelines to generate content briefs, track performance, generate insights, and enable autonomous content cycles.

### Core Features (Being Implemented)

| Tier | Features | Status |
|------|----------|--------|
| **Tier 1 (Critical)** | CIOS-01: Schema, CIOS-02: API, CIOS-03: Brief Steps 1-3, CIOS-04: Brief Gen, CIOS-18: Launch | 🟡 Pending |
| **Tier 2 (High)** | CIOS-05 through CIOS-22 (11 features: Pipelines, Dashboard, Event Bus) | 🟡 Pending |
| **Tier 3 (Medium)** | CIOS-12 through CIOS-24 (7 features: UI, Analytics, Autonomous Cycles) | 🟡 Pending |
| **Tier 4 (Low)** | CIOS-25: ACD MCP Integration | 🟡 Pending |

### CIOS Endpoints (Will Be Implemented)

#### Health & Status
```bash
GET /api/cios/health
```
Returns aggregate health of all 6 subsystems + pipeline queue depth

#### Content Brief Pipeline
```bash
POST /api/cios/pipeline/content-brief
{
  "topic": "string",
  "platform": "tiktok|instagram|twitter|linkedin",
  "geo_optimize": boolean,  # Optional TikTok GEO optimization
  "geo_location": "string"  # Optional location for GEO
}
```
Response: Full ContentBrief JSON with topic, platform, format, hook, script_outline, hashtags, B-roll suggestions

#### Performance Sync Pipeline
```bash
POST /api/cios/pipeline/performance-sync
```
Updates all performance data, detects tier changes, generates insights

#### Weekly Digest Pipeline
```bash
POST /api/cios/pipeline/weekly-digest
{
  "send_email": boolean
}
```
Synthesizes goals, performance, emerging formats, unreviewed insights

#### Goal Alignment Scan
```bash
POST /api/cios/pipeline/goal-align
```
Queries AIL for goal gaps, creates actionable insights

#### Prospect Brief Generation
```bash
POST /api/cios/pipeline/prospect-brief
{
  "contact_name": "string",
  "platform": "string"
}
```
Generates prospect-specific outreach brief, enriched from CRMLite

#### Cross-System Search
```bash
GET /api/cios/search?q={query}
```
Unified search across AIL, TikTok, Media Vault, and Content Briefs

#### Pipeline Management
```bash
GET /api/cios/pipelines
GET /api/cios/pipelines/{run_id}
POST /api/cios/pipelines/{run_id}/replay
PATCH /api/cios/briefs/{id}/status
```

### CIOS Database Schema (4 Tables)

#### cios_pipeline_runs
Track all pipeline executions:
- `run_id` (UUID, PK)
- `run_type` (content-brief, performance-sync, etc.)
- `status` (running, completed, failed)
- `input` (JSONB)
- `output` (JSONB)
- `steps` (JSONB array with timing)
- `started_at`, `completed_at`

#### cios_content_briefs
Store generated briefs:
- `id` (UUID, PK)
- `topic`, `platform`, `format`
- `brief` (JSONB - full ContentBrief)
- `production_status` (draft, in_production, published, cancelled)
- `geo_optimized`, `geo_brief`
- `created_at`, `updated_at`

#### cios_insight_log
Actionable insights:
- `id` (UUID, PK)
- `type` (viral_pattern, format_emerging, goal_gap, etc.)
- `priority` (high, medium, low)
- `content` (JSONB)
- `source_service` (tiktok, ail, vault, etc.)
- `reviewed_at`, `action_taken`

#### cios_distribution_queue
Publishing schedule:
- `id` (UUID, PK)
- `brief_id` (FK to cios_content_briefs)
- `scheduled_for` (timestamp)
- `priority_score` (0-100)
- `status` (queued, published, failed)
- `blotato_post_id` (after publishing)

### CIOS Real-Time Event Bus

**Channel**: `cios-events` (Supabase Realtime)

**Events Broadcast**:
- `PIPELINE_STARTED` — pipeline execution began
- `PIPELINE_STEP_COMPLETED` — individual step finished
- `PIPELINE_COMPLETED` — pipeline execution finished (success or failure)
- `PERFORMANCE_UPDATED` — performance tiers changed
- `INSIGHT_GENERATED` — new insight_log entry created
- `BRIEF_CREATED` — new content brief generated
- `VIRAL_DETECTED` — post reached viral tier

Dashboard subscribes to these events for live updates without polling.

### CIOS Autonomous Cycles

#### Morning Cycle (8am)
1. Run goal-alignment pipeline
2. Generate 3 content briefs (one per priority platform)
3. Auto-fill distribution queue for next 24h
4. Log results to insight_log

#### Evening Cycle (6pm)
1. Run performance-sync pipeline
2. Creator scan via tiktok-creator-watch
3. Generate viral_pattern and format_emerging insights
4. Update AIL with new high-performing content

### CIOS Dashboard

**Frontend**: Next.js at port 5571

**Pages**:
- `/` — Command center: pipeline launchers, live run stream, system health
- `/briefs` — Content brief library with filters and production workflow
- `/intelligence` — Insight feed with priority and action tracking
- `/performance` — Cross-platform analytics and trend charts
- `/queue` — Distribution queue with scheduling and manual publish
- `/memory` — AIL memory browser and document ingestion
- `/settings` — Autonomous cycle configuration

### Integration Points

CIOS orchestrates these 6 external systems:

1. **AIL** (Semantic search & knowledge ingestion)
   - POST /api/query — topic context retrieval
   - POST /api/ingest/social — re-embed updated performance data
   - POST /api/ingest/obsidian — optional vault sync

2. **TikTok Analytics** (Performance data)
   - GET /api/posts/check-batch — performance updates
   - POST /api/creators/scan-all — trending creator detection
   - POST /api/geo/generate-brief — TikTok GEO optimization

3. **Media Vault** (Video library)
   - POST /api/performance/check-all — vault item performance
   - GET /api/search/broll — B-roll suggestion for topic

4. **Content Intelligence** (Analysis & generation)
   - POST /api/score/fate — content scoring
   - POST /api/generate/title — title generation
   - POST /api/classify/awareness — awareness level detection

5. **CRMLite** (Contact enrichment)
   - GET /api/contacts/search — contact lookup by name
   - Returns: last_dm_date, platform_handle, crm_status

6. **Blotato** (Social publishing)
   - POST /v2/posts — publish content to platforms
   - GET /v2/accounts — list connected accounts

### Development Status

- ✅ feature_list.json created with 25 features
- ✅ CIOS architecture documented
- ✅ Launch script template created
- ✅ Developer guide with patterns and examples
- 🟡 Awaiting autonomous agent implementation (CIOS-01 onwards)
