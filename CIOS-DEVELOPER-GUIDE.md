# CIOS Developer Guide

## Overview

CIOS (Content Intelligence Operating System) is the orchestration and command-center layer for the content intelligence system. It coordinates briefs, pipelines, performance tracking, and autonomous content cycles.

**Status**: Core API scaffolded (1,238 lines in cios_api.py), schema pending, dashboard pending.

**Repository**: `/Users/isaiahdupree/Documents/Software/media-vault/`

## Architecture

```
CIOS Layer (5570/5571)
├── Orchestration API (cios_api.py) — FastAPI, 7 pipelines
├── Dashboard (Next.js, port 5571)
└── Event Bus (Supabase Realtime, cios-events channel)

Integrates with:
├── AIL (semantic search & knowledge base)
├── TikTok Intelligence (performance data)
├── Media Vault (video library)
├── Content Intelligence (scoring, generation)
├── CRMLite (contacts)
└── Blotato (publishing)
```

## Implemented Features (7/25)

### Core Pipelines Ready (But Untested)
- ✅ CIOS-02: FastAPI scaffold with 13 functions (cios_api.py)
- ✅ CIOS-03/04: Content brief pipeline with parallel context retrieval + GPT-4o
- ✅ CIOS-06: Performance sync pipeline
- ✅ CIOS-07: Weekly digest synthesis
- ✅ CIOS-08: Goal alignment scan
- ✅ CIOS-09: Supabase Realtime event bus
- 🟡 CIOS-23: Prospect brief (partial, CRMLite enrichment pending)

## CRITICAL BLOCKER: CIOS-01 Schema Not Created

**STATUS**: 🔴 **MUST IMPLEMENT IMMEDIATELY**

The API code references these tables that **do not exist**:
- `cios_pipeline_runs`
- `cios_content_briefs`
- `cios_insight_log`
- `cios_distribution_queue`

**Next steps**:
1. Create migration file: `008_cios_schema.sql`
2. Define 4 tables with correct columns, types, indexes per CIOS-01 acceptance criteria
3. Apply via: `mcp__supabase__apply_migration("cios-schema", "CREATE TABLE...")`
4. Verify: `curl http://localhost:5570/api/cios/health` should show all subsystems "ok"

See `feature_list.json` for exact schema spec.

## Setup for Development

```bash
# Start content-intelligence AI services (port 6006)
cd /Users/isaiahdupree/Documents/Software/content-intelligence
python app.py

# Start CIOS API
cd /Users/isaiahdupree/Documents/Software/media-vault/backend
source venv/bin/activate
python -m uvicorn cios_api:app --host 0.0.0.0 --port 5570 --reload
```

Test health:
```bash
curl http://localhost:5570/api/cios/health
```

## Pending Implementation (18 Features)

**High Priority (After CIOS-01)**:
- CIOS-05: GEO optimization
- CIOS-10/11: Dashboard command center & brief library
- CIOS-18: Launch script

**Medium Priority**:
- CIOS-12-17: More dashboard pages, autonomous cycles
- CIOS-20-22: History, status workflow, cross-system search

**Lower Priority**:
- CIOS-23: CRMLite enrichment
- CIOS-24: Thompson Sampling scheduler
- CIOS-25: ACD MCP integration

See `feature_list.json` for full specs with acceptance criteria.

## Key Implementation Patterns

### Pipeline Template
```python
async def pipeline_X(req):
    run_id = str(uuid4())
    run_record = {
        "run_id": run_id,
        "run_type": "x",
        "status": "running",
        "input": req.dict(),
        "steps": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # Execute steps, record timing
    for step_name, step_func in steps:
        step_start = time.time()
        result = await step_func()
        run_record["steps"].append({
            "name": step_name,
            "latency_ms": (time.time() - step_start) * 1000,
        })
    
    # Update & broadcast
    sb.table("cios_pipeline_runs").update(run_record).execute()
    await broadcast_event("PIPELINE_COMPLETED", {"run_id": run_id})
    
    return {"run_id": run_id, "status": "completed"}
```

### Parallel Context Retrieval
```python
results = await asyncio.gather(
    _ail_retrieval(topic, namespace),
    _tiktok_analytics(topic, platform),
    _media_vault_search(topic),
    return_exceptions=True
)
```

## Debugging

- **API won't start?** Check Supabase connection, env vars, dependencies
- **Pipeline fails?** Check uvicorn logs, verify schema exists, verify subsystem health
- **Event bus broken?** Check Realtime enabled in Supabase, verify cios-events channel

## External Services

| Service | Base URL | Key Endpoints |
|---------|----------|---|
| AIL | `http://localhost:3200` | /api/query, /api/ingest/social |
| TikTok | `http://localhost:3108` | /api/posts/check-batch, /api/creators/scan-all |
| Media Vault | `http://localhost:5563` | /api/performance/check-all, /api/search/broll |
| Content Intelligence | `http://localhost:6006` | /api/score/fate, /api/generate/title |
| Blotato | `https://backend.blotato.com/v2` | /v2/posts, /v2/accounts |

## Supabase Project

**Project ID**: ivhfuhxorppptyuofbgq
**URL**: https://ivhfuhxorppptyuofbgq.supabase.co

All services share this project.

## Next Agent Action

1. **CRITICAL**: Implement CIOS-01 schema creation
2. Test existing pipelines via curl
3. Build dashboard (CIOS-10 onwards)

See `feature_list.json` for detailed acceptance criteria.
