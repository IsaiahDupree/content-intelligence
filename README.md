# Content Intelligence

Two systems in one repo:

1. **AI Services** (Python/Flask, port 6006) — content analysis, generation, FATE scoring, awareness classification
2. **Content Intelligence System** (Node.js CLI) — audits MPLite DB, syncs Blotato accounts, indexes local files, deduplicates, cross-platform analysis, uploads to Supabase

## Content Intelligence CLI

### Setup

```bash
cp .env.example .env   # fill in SUPABASE_SERVICE_KEY, BLOTATO_API_KEY, LOCAL_VIDEO_DIRS
npm install
```

### Commands

```bash
node index.js migrate       # Create Supabase tables (ci_posts, ci_local_files, ci_analysis_snapshots)
node index.js audit          # MPLite DB audit → ci_posts
node index.js sync           # Blotato account sync → ci_posts
node index.js scan           # Local file scanner → ci_local_files (SHA256 + post matching)
node index.js delete         # Safe-delete dry-run report
node index.js delete --confirm-delete  # Actually delete matched files
node index.js analyze        # Cross-platform analysis → ci_analysis_snapshots
node index.js graph          # Content association graph → ci_analysis_snapshots
node index.js status         # Dashboard: posts by platform, files, recommendations
```

### Tests

```bash
node --test tests/test_content_intelligence.js
```

### Structure

```
lib/
├── supabase.js    # Supabase client (shared project ivhfuhxorppptyuofbgq)
├── migrate.js     # Schema migration
├── mplite.js      # MPLite DB audit
├── blotato.js     # Blotato account sync
├── scanner.js     # Local file scanner + SHA256 hashing
├── deleter.js     # Safe-delete engine
├── analyzer.js    # Cross-platform analyzer
├── graph.js       # Content association graph
└── dashboard.js   # CLI status dashboard
```

## AI Services (Python)

### Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py  # starts on port 6006
```

### Key Endpoints

- `POST /api/score/fate` — FATE framework scoring
- `POST /api/classify/awareness` — Awareness level classification
- `POST /api/analyze/sentiment` — Sentiment analysis
- `POST /api/generate/title` — AI title generation (Groq/OpenAI)
- `POST /api/generate/caption` — AI caption generation
- `GET /health` — Health check

## Environment Variables

See `.env.example` for all configuration options.

## CIOS (Content Intelligence Operating System)

CIOS is the orchestration layer being built on top of this content-intelligence service. See **CAPABILITIES.md** for full details.

### Quick Start for CIOS Development

```bash
# 1. Start content-intelligence AI services (this repo)
python app.py  # port 6006

# 2. Set up CIOS backend in media-vault
cd /Users/isaiahdupree/Documents/Software/media-vault/backend
source venv/bin/activate
# Agents will implement features CIOS-01 onwards

# 3. CIOS dashboard will run on port 5571
# Agents will build this as a Next.js app
```

### CIOS Implementation Order

**Phase 1 (Critical)** — Required for basic operation:
1. CIOS-01: Supabase schema (4 tables)
2. CIOS-02: FastAPI scaffold on port 5570
3. CIOS-03: Parallel context retrieval
4. CIOS-04: GPT-4o brief generation

**Phase 2 (High Priority)** — Completes core pipelines:
- CIOS-05 to CIOS-18: Additional pipelines, event bus, launch script

**Phase 3 (Medium)** — Dashboard and autonomous cycles:
- CIOS-12 to CIOS-17, CIOS-21-24: UI, analytics, autonomous operations

**Phase 4 (Polish)** — Integration and extras:
- CIOS-20, CIOS-25: History/replay, ACD integration

See `feature_list.json` for full feature specifications with acceptance criteria.

## Related Repos

- MediaPoster — Scheduling, publishing
- Safari Automation — Browser-based social automation
- MPLite — Organic publish queue (Vercel)
- Media Vault — CIOS target path
- CRMLite — Contact management and enrichment
