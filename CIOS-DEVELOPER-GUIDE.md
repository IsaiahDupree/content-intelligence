# CIOS Developer Guide

**Project**: Content Intelligence Operating System
**Status**: 🟡 In Development (0/25 features complete)
**Last Updated**: 2026-04-13

## Quick Start

### For New Autonomous Agents

1. **Read these files first**:
   - `feature_list.json` — All 25 features with acceptance criteria
   - `CIOS-INITIALIZATION.md` — Architecture and design decisions
   - This file — Developer workflow

2. **Pick a feature to implement**:
   - Start with Tier 1 (critical) features
   - Check feature_list.json for acceptance_criteria
   - Read the "feature dependencies" section below

3. **Follow the rules**:
   - ✅ Use real APIs and databases (no mocks in production)
   - ✅ Write integration tests against real services
   - ✅ Add /api/health endpoints to every service
   - ❌ No mock data, mock providers, or stub implementations
   - ❌ No TODO comments with fake return values

4. **Mark features as complete**:
   - When all acceptance criteria pass, update feature_list.json: `"passes": true`
   - Commit to GitHub with a clear message
   - Next agent picks the next feature

## Feature Dependencies

### Critical Path to MVP (Sessions 1-4)

```
CIOS-01: Supabase Schema
    ↓
CIOS-02: FastAPI Scaffold + Health Check
    ↓ (depends on -02)
CIOS-03: Context Retrieval (AIL, TikTok, Media Vault)
    ↓ (depends on -03)
CIOS-04: Brief Generation (GPT-4o)
    ↓
CIOS-09: Supabase Realtime Event Bus
    ↓
CIOS-10: Dashboard Command Center
```

### Feature Prerequisites Table

| Feature | Depends On | Notes |
|---------|-----------|-------|
| CIOS-01 | None | Pure schema, can start immediately |
| CIOS-02 | CIOS-01 | Needs Supabase tables to exist |
| CIOS-03 | CIOS-02 | Needs FastAPI endpoint structure |
| CIOS-04 | CIOS-03 | Needs context retrieval working |
| CIOS-05 | CIOS-04 | Optional GEO step, non-blocking |
| CIOS-06 | CIOS-02 | Can start after API scaffold |
| CIOS-07 | CIOS-02 | Can start after API scaffold |
| CIOS-08 | CIOS-02 | Can start after API scaffold |
| CIOS-09 | CIOS-02 | Needs API to broadcast events |
| CIOS-10 | CIOS-09 | Needs event bus for live updates |
| CIOS-11 | CIOS-10 | Builds on dashboard foundation |
| CIOS-12 | CIOS-11 | Builds on brief library |
| CIOS-13 | CIOS-11 | Cross-platform analytics |
| CIOS-14 | CIOS-11 | Distribution queue UI |
| CIOS-15 | CIOS-02 | AIL browser, independent |
| CIOS-16 | CIOS-04, CIOS-08 | Morning cycle needs briefs + goals |
| CIOS-17 | CIOS-06 | Evening cycle needs performance sync |
| CIOS-18 | All others | Launch script needs all services |
| CIOS-19 | CIOS-04, CIOS-09 | Viral insights need event bus |
| CIOS-20 | CIOS-02 | Pipeline history, independent |
| CIOS-21 | CIOS-04 | Brief status workflow |
| CIOS-22 | CIOS-02 | Cross-subsystem search |
| CIOS-23 | CIOS-04 | Prospect brief generation |
| CIOS-24 | CIOS-14 | Queue auto-scheduling |
| CIOS-25 | All others | MCP integration, last feature |

## Architecture Patterns

### 1. Async Pipeline Architecture

Every CIOS pipeline follows this pattern:

```python
# cios_api.py
from fastapi import FastAPI
from typing import Dict, Any
import asyncio
from datetime import datetime
import uuid

app = FastAPI()

class PipelineRunner:
    async def run_content_brief_pipeline(self, topic: str, platform: str):
        run_id = str(uuid.uuid4())

        # Record pipeline start
        await supabase.table("cios_pipeline_runs").insert({
            "run_id": run_id,
            "run_type": "content-brief",
            "input": {"topic": topic, "platform": platform},
            "steps": [],
            "status": "running",
            "started_at": datetime.utcnow().isoformat()
        })

        try:
            # Execute steps with timing
            step_results = {}

            # STEP 1-3: Parallel execution with timeout
            results = await asyncio.gather(
                self._step_1_ail_query(topic),
                self._step_2_tiktok_performance(platform),
                self._step_3_media_vault_broll(topic),
                return_exceptions=True
            )

            # STEP 4: Sequential (depends on steps 1-3)
            brief = await self._step_4_gpt4o_generate(results)

            # STEP 5: Optional GEO optimization
            if geo_optimize:
                brief = await self._step_5_geo_optimize(brief)

            # Record completion
            await supabase.table("cios_pipeline_runs").update({
                "status": "completed",
                "output": brief,
                "completed_at": datetime.utcnow().isoformat()
            }).eq("run_id", run_id)

            # Broadcast event
            await self.event_bus.broadcast("PIPELINE_COMPLETED", {
                "run_id": run_id,
                "status": "success"
            })

            return brief

        except Exception as e:
            await supabase.table("cios_pipeline_runs").update({
                "status": "failed",
                "error": str(e)
            }).eq("run_id", run_id)

            await self.event_bus.broadcast("PIPELINE_FAILED", {
                "run_id": run_id,
                "error": str(e)
            })

            raise

@app.post("/api/cios/pipeline/content-brief")
async def content_brief(topic: str, platform: str):
    runner = PipelineRunner()
    brief = await runner.run_content_brief_pipeline(topic, platform)
    return brief
```

### 2. Integration Client Pattern

Every external service gets a dedicated client:

```python
# services/ail_client.py
class AILClient:
    def __init__(self, base_url: str = "http://localhost:5666"):
        self.base_url = base_url
        self.timeout = 0.6  # 600ms timeout

    async def semantic_query(self, query: str, namespace: str = "all") -> Dict:
        """Query AIL for semantic matches"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/query",
                    json={"query": query, "namespace": namespace}
                )
                response.raise_for_status()
                return response.json()
        except asyncio.TimeoutError:
            return {"results": [], "error": "timeout"}
        except Exception as e:
            return {"results": [], "error": str(e)}

# Usage
ail = AILClient()
context = await ail.semantic_query("content pillar: AI automation")
```

### 3. Event Bus Pattern

Supabase Realtime for live updates:

```python
# services/event_bus.py
class EventBus:
    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self.channel = None

    async def connect(self):
        """Subscribe to cios-events channel"""
        self.channel = self.supabase.realtime.channel("cios-events")
        self.channel.subscribe()

    async def broadcast(self, event_type: str, payload: Dict):
        """Publish event to subscribers"""
        self.channel.send(
            type="broadcast",
            event=event_type,
            payload=payload
        )

# In handlers
await event_bus.broadcast("PIPELINE_COMPLETED", {
    "run_id": run_id,
    "pipeline_type": "content-brief",
    "duration_ms": elapsed_time
})
```

### 4. Health Check Pattern

Every service must expose health endpoint:

```python
@app.get("/api/cios/health")
async def health_check():
    """Aggregate health of all subsystems"""

    subsystems = {
        "ail": await check_service("http://localhost:5666/health"),
        "tiktok": await check_service("http://localhost:5681/health"),
        "vault": await check_service("http://localhost:5563/api/health"),
        "blotato": await check_service("https://backend.blotato.com/v2/health"),
        "crmlite": await check_service("https://crmlite-...vercel.app/api/health"),
        "content-intel": await check_service("http://localhost:6006/health")
    }

    return {
        "status": "ok" if all(s["status"] == "ok" for s in subsystems.values()) else "degraded",
        "subsystems": subsystems,
        "timestamp": datetime.utcnow().isoformat()
    }

async def check_service(url: str, timeout: float = 2.0) -> Dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            return {"status": "ok", "latency_ms": response.elapsed.total_seconds() * 1000}
    except Exception as e:
        return {"status": "down", "error": str(e)}
```

## Testing Strategy

### Unit Tests (Fast, No Mocks)

❌ **Don't do this**:
```python
# BAD: Mock test
def test_pipeline():
    mock_ail = MagicMock()
    mock_ail.query.return_value = {"results": [...]}
    pipeline = Pipeline(ail_client=mock_ail)
    result = pipeline.run(...)
    assert result is not None
```

✅ **Do this instead**:
```python
# GOOD: Function unit test (no external calls)
def test_ail_context_parsing():
    """Test parsing of AIL response format"""
    ail_response = {
        "results": [
            {"title": "...", "similarity": 0.95, "content": "..."}
        ]
    }
    context = parse_ail_context(ail_response)
    assert context["title"] == "..."
    assert context["relevance"] == "high"

# GOOD: Integration test (real services)
async def test_ail_integration():
    """Test against real AIL service"""
    ail = AILClient("http://localhost:5666")
    result = await ail.semantic_query("test query")
    assert "results" in result  # Real response structure
    assert isinstance(result["results"], list)
```

### Integration Tests

All integration tests run against **real services**:

```python
# tests/test_pipelines.py
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_content_brief_pipeline_e2e():
    """Full pipeline: topic → brief in < 20 seconds"""

    async with AsyncClient(app=app, base_url="http://localhost:5570") as client:
        response = await client.post(
            "/api/cios/pipeline/content-brief",
            json={
                "topic": "AI automation for startups",
                "platform": "tiktok"
            },
            timeout=20.0
        )

    assert response.status_code == 200
    brief = response.json()

    # Verify ContentBrief structure
    assert "brief_id" in brief
    assert "topic" in brief
    assert "platform" in brief
    assert "hook" in brief
    assert "script_outline" in brief
    assert "hook_alternatives" in brief
    assert len(brief["hook_alternatives"]) == 2
    assert "hashtags" in brief
    assert isinstance(brief["hashtags"], list)

@pytest.mark.asyncio
async def test_event_bus_broadcasts():
    """Verify Supabase Realtime broadcasts"""

    event_bus = EventBus(supabase_client)
    events_received = []

    def on_event(payload):
        events_received.append(payload)

    channel = supabase_client.realtime.channel("cios-events")
    channel.on("broadcast", on_event).subscribe()

    # Trigger pipeline
    await event_bus.broadcast("TEST_EVENT", {"data": "test"})

    # Wait for event
    await asyncio.sleep(0.5)

    assert len(events_received) > 0
    assert events_received[0]["event"] == "TEST_EVENT"
```

### Test Data Fixtures

Keep test data in real databases (not JSON files):

```python
# tests/conftest.py
@pytest.fixture
async def test_content_brief():
    """Create a real test brief in Supabase"""
    result = await supabase.table("cios_content_briefs").insert({
        "topic": "Test topic",
        "platform": "tiktok",
        "brief": {...},
        "status": "draft"
    }).execute()

    brief_id = result.data[0]["id"]

    yield result.data[0]

    # Cleanup after test
    await supabase.table("cios_content_briefs").delete().eq("id", brief_id).execute()
```

## Environment Setup

### Required Services (Must Be Running)

```bash
# 1. Supabase (always on)
# Check: curl https://ivhfuhxorppptyuofbgq.supabase.co/health

# 2. Content Intelligence API (port 6006)
cd /Users/isaiahdupree/Documents/Software/content-intelligence
python3 app.py

# 3. AIL Service (port 5666)
# Check your ail service startup

# 4. TikTok Analytics (port 5681)
# Check your tiktok-analytics service startup

# 5. Media Vault (port 5563)
cd /Users/isaiahdupree/Documents/Software/media-vault
source backend/venv/bin/activate
uvicorn main:app --port 5563

# 6. CIOS (port 5570)
cd /Users/isaiahdupree/Documents/Software/media-vault/backend
source venv/bin/activate
uvicorn cios_api:app --port 5570
```

### Environment Variables

Create `.env` in media-vault/backend:

```bash
# Database
SUPABASE_URL=https://ivhfuhxorppptyuofbgq.supabase.co
SUPABASE_SERVICE_KEY=<from 1Password>

# AI APIs
OPENAI_API_KEY=<from 1Password>
GROQ_API_KEY=<from 1Password>

# Internal Services
AIL_URL=http://localhost:5666
TIKTOK_ANALYTICS_URL=http://localhost:5681
MEDIA_VAULT_URL=http://localhost:5563
CONTENT_INTELLIGENCE_URL=http://localhost:6006
CRMLITE_URL=https://crmlite-isaiahduprees-projects.vercel.app

# Publishing
BLOTATO_API_KEY=<from 1Password>

# Feature Flags
ENABLE_GEO_OPTIMIZATION=true
AUTO_INSIGHT_GENERATION=true
AUTONOMOUS_CYCLES_ENABLED=false  # Enable in CIOS-16/17
```

## Code Organization

### File Structure

```
media-vault/
├── backend/
│   ├── cios_api.py              # Main FastAPI app
│   ├── migrations/
│   │   └── cios_schema.sql      # Supabase schema
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── content_brief.py     # CIOS-03, 04, 05
│   │   ├── performance_sync.py  # CIOS-06
│   │   ├── weekly_digest.py     # CIOS-07
│   │   ├── goal_align.py        # CIOS-08
│   │   ├── prospect_brief.py    # CIOS-23
│   │   └── search.py            # CIOS-22
│   ├── services/
│   │   ├── __init__.py
│   │   ├── ail_client.py        # AIL integration
│   │   ├── tiktok_client.py     # TikTok analytics
│   │   ├── vault_client.py      # Media vault
│   │   ├── blotato_client.py    # Publishing
│   │   ├── crmlite_client.py    # CRM
│   │   ├── event_bus.py         # Supabase Realtime
│   │   └── openai_client.py     # GPT-4o
│   ├── models/
│   │   ├── __init__.py
│   │   ├── pipeline_run.py      # Pipeline schema
│   │   ├── content_brief.py     # Brief schema
│   │   ├── insight_log.py       # Insight schema
│   │   └── distribution_queue.py
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── timeout.py           # Timeout helpers
│   │   ├── validation.py        # Input validation
│   │   └── health.py            # Health check aggregation
│   ├── tests/
│   │   ├── conftest.py
│   │   ├── test_cios_api.py
│   │   ├── test_content_brief.py
│   │   ├── test_performance_sync.py
│   │   └── test_event_bus.py
│   ├── harness/
│   │   └── launch-cios.sh
│   ├── requirements.txt
│   └── .env.example
│
└── frontend/
    ├── app/
    │   ├── layout.tsx
    │   ├── page.tsx              # Command center (CIOS-10)
    │   ├── briefs/page.tsx       # Brief library (CIOS-11)
    │   ├── intelligence/page.tsx # Insights (CIOS-12)
    │   ├── performance/page.tsx  # Analytics (CIOS-13)
    │   ├── queue/page.tsx        # Distribution (CIOS-14)
    │   ├── memory/page.tsx       # AIL browser (CIOS-15)
    │   └── components/
    │       ├── HealthDots.tsx
    │       ├── PipelineMonitor.tsx
    │       ├── BriefCard.tsx
    │       ├── InsightFeed.tsx
    │       ├── PerformanceCharts.tsx
    │       └── QueueTable.tsx
    ├── lib/
    │   ├── realtime.ts
    │   ├── api.ts
    │   └── hooks.ts
    ├── tests/
    │   └── e2e/
    │       └── dashboard.spec.ts
    ├── package.json
    ├── next.config.js
    └── tailwind.config.js
```

## Common Patterns

### Making API Calls with Timeout + Fallback

```python
async def fetch_with_fallback(url: str, timeout: float, default: Any = None):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
    except (asyncio.TimeoutError, httpx.HTTPError):
        return default or {}
```

### Logging for Debugging

```python
from loguru import logger

logger.add("logs/cios-{time}.log", rotation="500 MB")

@app.post("/api/cios/pipeline/content-brief")
async def content_brief(topic: str):
    logger.info(f"Starting content brief pipeline: {topic}")

    try:
        brief = await runner.run(topic)
        logger.success(f"Content brief completed: {brief['brief_id']}")
        return brief
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        raise
```

## Debugging

### Check Service Status

```bash
# All CIOS services
curl http://localhost:5570/api/cios/health | jq

# Individual services
curl http://localhost:6006/health
curl http://localhost:5681/health
curl http://localhost:5563/api/health
curl https://backend.blotato.com/v2/health
```

### View Database

```python
# Check Supabase tables
supabase.table("cios_pipeline_runs").select("*").limit(5).execute()
supabase.table("cios_content_briefs").select("*").limit(5).execute()
supabase.table("cios_insight_log").select("*").limit(5).execute()
```

### Tail Logs

```bash
tail -f /tmp/cios-logs/*.log
```

## Deployment

### Vercel Deployment (Frontend)

```bash
cd media-vault/frontend
npx vercel --prod
```

### Production Backend (Railway/Heroku)

```bash
cd media-vault/backend
# Create Procfile
echo "web: gunicorn -w 4 -b 0.0.0.0:\$PORT cios_api:app" > Procfile

# Deploy
git push heroku main
```

## Useful Commands

```bash
# Run tests
pytest tests/ -v

# Run specific test
pytest tests/test_content_brief.py::test_content_brief_pipeline_e2e -v

# Check dependencies
pip list | grep -i fastapi

# Format code
black . --line-length 100

# Type checking
mypy . --ignore-missing-imports

# Lint
ruff check . --select=E,W
```

---

**Questions?** Check:
- `feature_list.json` for feature details
- `CIOS-INITIALIZATION.md` for architecture
- Existing features' code for patterns
- Integration tests for usage examples
