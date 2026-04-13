# Agent Implementation Checklist

This document is your quick reference for implementing CIOS features in priority order.

## Pre-Implementation Setup

- [ ] Read `CIOS-DEVELOPER-GUIDE.md` (5 min)
- [ ] Review `feature_list.json` for your assigned feature (10 min)
- [ ] Understand feature's acceptance criteria (5 min)
- [ ] Check dependencies on other features in feature_list.json
- [ ] Verify Supabase project access: https://ivhfuhxorppptyuofbgq.supabase.co
- [ ] Start content-intelligence AI services: `python app.py` (port 6006)
- [ ] Check CIOS API health: `curl http://localhost:5570/api/cios/health`

## Feature Implementation

### Step 1: Code
- [ ] Write/modify code in `/Users/isaiahdupree/Documents/Software/media-vault/backend/`
- [ ] Follow patterns from CIOS-DEVELOPER-GUIDE.md
- [ ] No mock data — use real API calls, real Supabase tables
- [ ] All functions have timeout handling and graceful fallback
- [ ] Log all steps via loguru logger

### Step 2: Test
- [ ] Write unit tests in `/backend/tests/`
- [ ] Write integration tests using real Supabase
- [ ] Test with `node --test` or pytest
- [ ] All tests pass before commit

### Step 3: Verify Acceptance Criteria
For each acceptance criterion in feature_list.json:
- [ ] Manually test or write test case
- [ ] Document test results
- [ ] Ensure all criteria pass before marking complete

### Step 4: Commit
```bash
cd /Users/isaiahdupree/Documents/Software/media-vault
git add backend/
git commit -m "feat: CIOS-XX — [feature title]"
git push
```

### Step 5: Update Progress
Update `/Users/isaiahdupree/Documents/Software/content-intelligence/claude-progress.txt`:
- [ ] Mark feature as ✅ in "Implemented Features"
- [ ] Add notes on any issues or decisions
- [ ] Update expected next steps

## Feature Implementation Order (Prioritized)

### Critical (Must Have)
- [ ] **CIOS-01**: Supabase schema (4 tables) — **BLOCKER, DO THIS FIRST**
- [ ] **CIOS-05**: GEO optimization (after 01 passes)
- [ ] **CIOS-18**: Launch script (after APIs working)

### High Priority (Core Functionality)
- [ ] **CIOS-10**: Command center dashboard page
- [ ] **CIOS-11**: Brief library dashboard page
- [ ] **CIOS-12**: Intelligence/insights page
- [ ] **CIOS-13**: Performance analytics page
- [ ] **CIOS-14**: Distribution queue page
- [ ] **CIOS-15**: AIL memory browser page
- [ ] **CIOS-16**: Morning autonomous cycle
- [ ] **CIOS-17**: Evening autonomous cycle
- [ ] **CIOS-19**: Auto-viral event insights

### Medium Priority (Polish)
- [ ] **CIOS-20**: Pipeline history/replay
- [ ] **CIOS-21**: Brief status workflow
- [ ] **CIOS-22**: Cross-system search
- [ ] **CIOS-23**: CRMLite enrichment for prospect brief
- [ ] **CIOS-24**: Thompson Sampling auto-scheduler

### Low Priority (Optional)
- [ ] **CIOS-25**: ACD MCP integration

## Debugging Commands

```bash
# Check API health
curl http://localhost:5570/api/cios/health

# Test content-brief pipeline
curl -X POST http://localhost:5570/api/cios/pipeline/content-brief \
  -H "Content-Type: application/json" \
  -d '{"topic":"AI","platform":"tiktok","geo_optimize":false}'

# Check Supabase tables
# Use Supabase Studio: https://ivhfuhxorppptyuofbgq.supabase.co

# View logs
tail -f /tmp/cios.log
```

## Common Issues & Fixes

| Issue | Fix |
|-------|-----|
| "Table does not exist" error | CIOS-01 schema not applied. Run migration first. |
| API won't start | Check env vars: SUPABASE_URL, SUPABASE_SERVICE_KEY, OPENAI_API_KEY |
| Pipeline hangs | Check subsystem health. One service might be down. |
| Timeout errors | Increase timeouts in asyncio.gather() or reduce service response times |
| Event bus not working | Verify Realtime enabled in Supabase project settings |

## When You're Done

1. Commit to GitHub with clear commit message
2. Update `claude-progress.txt` with what you did
3. Update this checklist to mark your feature(s) as complete
4. Document any blockers or edge cases for the next agent
5. Note any environment setup needed for next features

## Key Files

| File | Purpose |
|------|---------|
| `feature_list.json` | Full 25-feature spec with acceptance criteria |
| `CIOS-DEVELOPER-GUIDE.md` | Setup, patterns, subsystem integration |
| `CAPABILITIES.md` | API endpoints and architecture overview |
| `README.md` | Project overview and quick start |
| `claude-progress.txt` | Session log and next steps |

## Contact / Help

- All CIOS code lives in: `/Users/isaiahdupree/Documents/Software/media-vault/backend/`
- All content-intelligence services in: `/Users/isaiahdupree/Documents/Software/content-intelligence/`
- Supabase project: ivhfuhxorppptyuofbgq
- Business goals: `/Users/isaiahdupree/Documents/Software/business-goals.json`

