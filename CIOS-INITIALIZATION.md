# Content Intelligence Operating System (CIOS) — Initialization

**Date**: 2026-04-13
**Project**: content-intelligence-os
**Version**: 1.0.0
**Target Path**: /Users/isaiahdupree/Documents/Software/media-vault

## Project Overview

CIOS is a comprehensive orchestration system that integrates content intelligence, analytics, and autonomous pipeline execution. It serves as the command-and-control center for the entire ACTP ecosystem.

### Core Components

1. **Content Intelligence API** (content-intelligence/port 6006)
   - FATE scoring, awareness classification, sentiment analysis
   - Vision analysis, title/caption generation
   - Narrative planning, hypothesis generation

2. **CIOS Orchestration API** (media-vault/backend, port 5570)
   - Manages 5 core pipelines: content-brief, performance-sync, weekly-digest, goal-align, prospect-brief
   - Parallel step execution with graceful fallbacks
   - Event bus for real-time updates

3. **CIOS Dashboard** (media-vault/frontend)
   - Command center with pipeline launchers
   - Content brief library with filters
   - Intelligence insight log with actionable recommendations
   - Analytics and performance tracking
   - Distribution queue management

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CIOS Orchestration API                       │
│                     (media-vault/backend, port 5570)                │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│ ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                │
│ │ Content Brief│  │  Performance │  │Weekly Digest │  ...           │
│ │   Pipeline   │  │   Sync       │  │  Pipeline    │                │
│ └──────────────┘  └──────────────┘  └──────────────┘                │
│                                                                       │
│ ┌─────────────────────────────────────────────────────────┐         │
│ │         Supabase Realtime Event Bus (cios-events)      │         │
│ └─────────────────────────────────────────────────────────┘         │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
         ↓          ↓           ↓           ↓           ↓
    ┌─────────┐  ┌──────────┐  ┌─────────┐ ┌────────┐ ┌──────────┐
    │   AIL   │  │ TikTok   │  │ Media   │ │ Blotato│ │ CRMLite  │
    │ (Query) │  │Analytics │  │ Vault   │ │(Publish)│ │(Enrich)  │
    └─────────┘  └──────────┘  └─────────┘ └────────┘ └──────────┘
         ↓          ↓           ↓           ↓           ↓
    ┌─────────────────────────────────────────────────────────┐
    │          Supabase Database (ivhfuhxorppptyuofbgq)       │
    │                                                           │
    │ cios_pipeline_runs       cios_content_briefs            │
    │ cios_insight_log         cios_distribution_queue        │
    │                                                           │
    │ (plus 10+ other shared tables)                          │
    └─────────────────────────────────────────────────────────┘
         ↑                            ↑
    ┌─────────────────────────────────────────────────────────┐
    │            CIOS Dashboard (Next.js Frontend)            │
    │              (media-vault/frontend)                     │
    └─────────────────────────────────────────────────────────┘
```

## Feature Breakdown (25 Total)

### Critical (Tier 1) - 5 features
- **CIOS-01**: Supabase Schema (4 tables + indexes)
- **CIOS-02**: Orchestration API scaffold (port 5570, health check)
- **CIOS-03**: Content Brief Pipeline Steps 1-3 (parallel context retrieval)
- **CIOS-04**: Content Brief Pipeline Step 4 (GPT-4o brief generation)
- **CIOS-18**: Launch script (harness/launch-cios.sh)

### High Priority (Tier 2) - 11 features
- **CIOS-05**: GEO optimization (TikTok specific)
- **CIOS-06**: Performance sync pipeline
- **CIOS-07**: Weekly digest pipeline
- **CIOS-08**: Goal alignment scan
- **CIOS-09**: Supabase Realtime event bus
- **CIOS-10**: Dashboard command center
- **CIOS-11**: Content brief library
- **CIOS-19**: Auto-insight generation for viral events
- **CIOS-21**: Content brief production status workflow
- **CIOS-22**: Cross-subsystem search endpoint

### Medium Priority (Tier 3) - 6 features
- **CIOS-12**: Intelligence insight log UI
- **CIOS-13**: Cross-platform performance analytics
- **CIOS-14**: Distribution queue management
- **CIOS-15**: AIL memory browser
- **CIOS-16**: Autonomous morning cycle
- **CIOS-17**: Autonomous evening cycle
- **CIOS-23**: Prospect brief + CRMLite integration
- **CIOS-24**: Distribution queue auto-scheduler
- **CIOS-20**: Pipeline run history and replay

### Low Priority (Tier 4) - 1 feature
- **CIOS-25**: ACD MCP tool integration

## Development Strategy

### Session Structure
Each autonomous agent session targets 2-4 features in dependency order:

**Session 1-2**: Schema + API Foundation
- CIOS-01: Database setup
- CIOS-02: FastAPI scaffold
- CIOS-18: Launch script

**Session 3-5**: Content Brief Pipeline
- CIOS-03: Parallel context retrieval
- CIOS-04: GPT-4o brief generation
- CIOS-05: GEO optimization

**Session 6-8**: Dashboard + Real-time
- CIOS-09: Event bus
- CIOS-10: Command center
- CIOS-11: Content brief library

**Session 9-12**: Advanced Pipelines
- CIOS-06: Performance sync
- CIOS-07: Weekly digest
- CIOS-08: Goal alignment
- CIOS-19: Viral event auto-insights

**Session 13+**: UI + Autonomous Features
- CIOS-12 through CIOS-25

### Quality Standards

- **Zero Mock Code**: All integrations use real Supabase, real APIs, real data flows
- **Integration Tests**: Every feature tested against real services (or test fixtures)
- **Health Checks**: All services expose `/api/health` endpoints
- **Error Handling**: Graceful fallbacks for subsystem failures
- **Performance**: Pipeline operations < 20 seconds (except data aggregation)
- **Documentation**: Every endpoint documented with curl examples

## Environment Setup Checklist

- [ ] Supabase project confirmed (ivhfuhxorppptyuofbgq)
- [ ] All API keys in ~/.env (OPENAI_API_KEY, GROQ_API_KEY, BLOTATO_API_KEY)
- [ ] media-vault/backend scaffolded with FastAPI
- [ ] media-vault/frontend scaffolded with Next.js
- [ ] Local services ready: content-intelligence on :6006, media-vault on :5563-:5564
- [ ] TikTok analytics service accessible
- [ ] AIL service accessible for queries
- [ ] CRMLite accessible for lookups

## Running CIOS

```bash
# Terminal 1: Start content-intelligence API
cd /Users/isaiahdupree/Documents/Software/content-intelligence
python3 app.py  # port 6006

# Terminal 2: Start CIOS orchestration + dashboard
cd /Users/isaiahdupree/Documents/Software/media-vault
bash harness/launch-cios.sh  # launches backend (5570) + frontend (5571)

# Check health
curl http://localhost:5570/api/cios/health
open http://localhost:5571  # dashboard
```

## Key Files (Will Be Created)

### content-intelligence/
- `CIOS-INITIALIZATION.md` — This file
- Updated `CAPABILITIES.md` — Document CIOS endpoints

### media-vault/backend/
- `cios_api.py` — Main FastAPI orchestration app
- `migrations/` — Supabase schema migrations
- `handlers/` — Pipeline handlers (content-brief, performance-sync, etc.)
- `services/` — Integration clients (AIL, TikTok, CRMLite, Blotato)

### media-vault/frontend/
- `app/` — Next.js app directory structure
- `app/page.tsx` — Command center dashboard
- `app/briefs/page.tsx` — Content brief library
- `app/intelligence/page.tsx` — Insight log
- Components for real-time updates via Supabase

### media-vault/
- `harness/launch-cios.sh` — Service startup orchestration

## Success Criteria

✅ **CIOS is successful when:**
1. All 25 features passing in feature_list.json
2. Dashboard loads at http://localhost:5571 with zero latency
3. Content brief pipeline generates briefs < 15 seconds
4. Performance sync runs hourly autonomously
5. Viral content triggers auto-insights within 2 minutes
6. All subsystems healthy (green dots on dashboard)
7. Zero manual intervention needed for morning/evening cycles

## References

- **Business Goals**: /Users/isaiahdupree/Documents/Software/business-goals.json
- **ACTP Worker**: /Users/isaiahdupree/Documents/Software/actp-worker
- **Safari Automation**: /Users/isaiahdupree/Documents/Software/Safari Automation
- **Media Vault**: /Users/isaiahdupree/Documents/Software/media-vault
- **CRMLite**: https://crmlite-isaiahduprees-projects.vercel.app
- **Blotato API**: https://backend.blotato.com/v2

---

**Initializer**: Claude Haiku 4.5
**Session**: INITIALIZER
**Status**: ✅ Environment initialized, ready for autonomous development
