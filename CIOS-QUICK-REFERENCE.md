# CIOS Quick Reference

**For Autonomous Agents**: Use this checklist when implementing CIOS features.

---

## Before You Start

- [ ] Read `feature_list.json` and find your assigned feature
- [ ] Check the acceptance criteria (✅ all must pass)
- [ ] Check dependencies (other features that must be done first)
- [ ] Verify all services are running:
  ```bash
  curl http://localhost:6006/health              # Content Intelligence
  curl http://localhost:5681/health              # TikTok Analytics
  curl http://localhost:5563/api/health          # Media Vault
  curl https://crmlite-...vercel.app/api/health  # CRMLite
  ```

---

## Core Rules

### ✅ Always Do This

1. **Use Real APIs & Databases**
   - Supabase ivhfuhxorppptyuofbgq (shared)
   - Real HTTP calls to external services
   - Real test fixtures in Supabase

2. **Implement /api/health**
   - Every service must expose health endpoint
   - Shows subsystem statuses
   - Returns status + latency_ms

3. **Write Integration Tests**
   - Run against real services (or test fixtures)
   - Use pytest + asyncio
   - Test actual error conditions (timeouts, failures)

4. **Handle Timeouts Gracefully**
   - AIL: 600ms timeout → return empty context
   - TikTok: 300ms timeout → return empty performance
   - Media Vault: 500ms timeout → return empty suggestions
   - **Never fail the pipeline** — fallback and continue

5. **Add Logging**
   - Use loguru: `logger.info()`, `logger.error()`
   - Log at key decision points
   - Include context (request ID, service name)

6. **Commit to GitHub**
   - After each feature completes
   - Clear commit message
   - Include feature ID in message: "feat: Implement CIOS-03 ..."

### ❌ Never Do This

1. **No Mock Code in Production**
   - ❌ `MagicMock()` outside tests
   - ❌ Mock API responses
   - ❌ Stub implementations
   - ❌ Placeholder return values

2. **No Hardcoded Test Data**
   - ❌ Test fixtures as .json files in source
   - ❌ Fake API responses in code
   - ❌ Mock services in production files

3. **No Incomplete Features**
   - ❌ TODO comments with fake returns
   - ❌ Pass statements
   - ❌ Features marked complete when AC not met

4. **No Skipping Tests**
   - ❌ `@pytest.mark.skip`
   - ❌ `if False:`
   - ❌ Disabled tests

---

## Typical Feature Implementation Flow

### 1. Read the Feature

```json
{
  "id": "CIOS-03",
  "title": "Content Brief Pipeline — Parallel Context Retrieval",
  "acceptance_criteria": [
    "Steps 1-3 run concurrently (not sequentially)",
    "Each step has independent timeout with graceful fallback",
    "Combined context passed to Step 4 regardless of individual failures",
    "cios_pipeline_runs.steps records timing"
  ]
}
```

### 2. Check Dependencies

Look at `CIOS-DEVELOPER-GUIDE.md` "Feature Dependencies" table:
- CIOS-03 depends on: CIOS-02 (API scaffold)
- CIOS-03 enables: CIOS-04 (GPT-4o generation)

### 3. Create Bare Minimum Implementation

```python
# media-vault/backend/handlers/content_brief.py

async def context_retrieval(topic: str):
    """CIOS-03: Parallel context retrieval (Steps 1-3)"""

    # All 3 steps in parallel with independent timeouts
    results = await asyncio.gather(
        ail_context(topic),          # 600ms timeout
        tiktok_performance(),        # 300ms timeout
        media_vault_broll(topic),    # 500ms timeout
        return_exceptions=True
    )

    # Combine results (handle timeouts gracefully)
    context = {
        "ail": results[0] or {},
        "tiktok": results[1] or {},
        "vault": results[2] or {}
    }

    return context
```

### 4. Write Integration Test

```python
# tests/test_content_brief.py

@pytest.mark.asyncio
async def test_cios_03_parallel_execution():
    """Verify steps 1-3 run in parallel with timeouts"""

    start = time.time()
    context = await context_retrieval("test topic")
    elapsed = time.time() - start

    # All 3 steps timeout is ~600ms (max of 3), not sum
    assert elapsed < 1.0, "Should run in parallel"

    # Each step should have results (or empty dict on timeout)
    assert isinstance(context["ail"], dict)
    assert isinstance(context["tiktok"], dict)
    assert isinstance(context["vault"], dict)

@pytest.mark.asyncio
async def test_cios_03_handles_timeouts():
    """Verify pipeline continues even if steps timeout"""

    # This would mock one service to timeout, but test real behavior
    context = await context_retrieval("test topic")

    # Pipeline succeeded even if one step timed out
    assert context is not None
    # At least some data should be present (from services that succeeded)
    # Missing data represented as empty dict
```

### 5. Verify with Real Services

```bash
# Start all services
bash harness/launch-cios.sh

# Test your feature
curl -X POST http://localhost:5570/api/cios/pipeline/content-brief \
  -H "Content-Type: application/json" \
  -d '{"topic": "AI automation", "platform": "tiktok"}'

# Check health
curl http://localhost:5570/api/cios/health | jq
```

### 6. Run Tests

```bash
cd media-vault/backend

# All tests
pytest tests/ -v

# Specific feature
pytest tests/test_content_brief.py::test_cios_03_parallel_execution -v

# With output
pytest tests/ -v -s
```

### 7. Commit

```bash
git add .
git commit -m "feat: Implement CIOS-03 parallel context retrieval

- Steps 1-3 run concurrently with independent timeouts
- AIL context (600ms), TikTok analytics (300ms), Media Vault B-roll (500ms)
- Graceful fallback to empty result on timeout
- Pipeline continues even if individual steps fail
- Integration tests verify parallel execution and timeout handling
- Acceptance criteria: All 4 requirements passing"

git push origin main
```

---

## Feature Checklist Template

Use this for each feature:

```markdown
# Feature: [FEATURE_ID] - [TITLE]

## Acceptance Criteria
- [ ] AC1: ...
- [ ] AC2: ...
- [ ] AC3: ...
- [ ] AC4: ...

## Implementation
- [ ] Code written in correct file
- [ ] Integration tests added
- [ ] All AC verified to pass
- [ ] /api/health endpoint working (if applicable)
- [ ] Logging added at key points
- [ ] No mock code in production
- [ ] No hardcoded test data

## Testing
- [ ] `pytest tests/` all pass
- [ ] Real services tested (not mocked)
- [ ] Timeout behavior tested
- [ ] Error handling tested
- [ ] Performance verified (if has SLA)

## Documentation
- [ ] Code comments where logic unclear
- [ ] CAPABILITIES.md updated (if new endpoint)
- [ ] Example curl command documented

## Cleanup & Commit
- [ ] Delete any .pyc, __pycache__
- [ ] Verify no TODO comments left
- [ ] Git commit with feature ID in message
- [ ] `git push origin main`

## Completion
- [ ] Mark `"passes": true` in feature_list.json
- [ ] PR merged to main
- [ ] Dashboard reflects new feature (if applicable)
```

---

## Common Patterns

### Pattern 1: Async Step with Timeout

```python
async def fetch_with_timeout(coro, timeout: float):
    """Execute async function with timeout, return fallback on timeout"""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Timeout after {timeout}s")
        return None  # or {}
```

### Pattern 2: Parallel Execution

```python
results = await asyncio.gather(
    fetch_with_timeout(ail_query(topic), 0.6),
    fetch_with_timeout(tiktok_check(), 0.3),
    fetch_with_timeout(vault_search(topic), 0.5),
    return_exceptions=True  # Don't raise, return exceptions
)

# Handle results
context = {}
for i, result in enumerate(results):
    if isinstance(result, Exception):
        logger.error(f"Step {i} failed: {result}")
    context[f"step_{i}"] = result or {}
```

### Pattern 3: Event Broadcast

```python
from services.event_bus import event_bus

# After pipeline completes
await event_bus.broadcast("PIPELINE_COMPLETED", {
    "run_id": run_id,
    "status": "success",
    "duration_ms": elapsed_time
})
```

### Pattern 4: Health Check

```python
@app.get("/api/cios/health")
async def health_check():
    subsystems = {
        "ail": await check_service("http://localhost:5666/health"),
        "tiktok": await check_service("http://localhost:5681/health"),
        # ... more services
    }

    return {
        "status": "ok" if all(s["ok"] for s in subsystems.values()) else "degraded",
        "subsystems": subsystems
    }
```

---

## Debugging Checklist

| Problem | Solution |
|---------|----------|
| Test fails locally | Verify all 6 services running (curl health) |
| Service timeout | Increase timeout or check service logs |
| Supabase insert fails | Check table exists (`SELECT * FROM cios_*`) |
| Mock code detected | Search for `MagicMock`, `Mock`, `patch` in source |
| Test data not cleaned up | Add fixture cleanup (`yield` then delete) |
| Port already in use | `lsof -i :5570` and kill process |
| Import errors | Verify requirements.txt installed |

---

## Performance Budgets

Keep features within these limits:

| Operation | Budget | How to Verify |
|-----------|--------|--------------|
| Content brief pipeline | < 15s | Time end-to-end request |
| Individual API call | 300-600ms | Check logs for latency_ms |
| Dashboard load | < 2s | Check network tab in browser |
| Real-time event | < 500ms | Supabase Realtime latency |
| Database query | < 100ms | Check Supabase logs |

---

## Files to Know

| File | Purpose |
|------|---------|
| `feature_list.json` | Your source of truth for what to build |
| `CIOS-INITIALIZATION.md` | Architecture and design decisions |
| `CIOS-DEVELOPER-GUIDE.md` | Code patterns and testing |
| `CAPABILITIES.md` | API endpoints reference |
| `harness/launch-cios.sh` | Start all services |

---

## Example: Implementing CIOS-01 (Supabase Schema)

### 1. Read Feature
```json
{
  "id": "CIOS-01",
  "title": "Supabase Schema",
  "acceptance_criteria": [
    "All 4 tables created",
    "Partial index on insight_log",
    "Enums correct",
    "JSONB columns"
  ]
}
```

### 2. Create Migration File
```bash
touch media-vault/backend/migrations/001_cios_schema.sql
```

### 3. Write SQL
```sql
-- Create cios_pipeline_runs table
CREATE TABLE IF NOT EXISTS cios_pipeline_runs (
  run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  input JSONB,
  output JSONB,
  steps JSONB,
  started_at TIMESTAMP,
  completed_at TIMESTAMP
);

-- Add index
CREATE INDEX idx_cios_pipeline_runs_type_date
  ON cios_pipeline_runs(run_type, started_at DESC);

-- ... more tables
```

### 4. Apply Migration
```python
# cios_api.py
from lib.migrate import apply_migration

await apply_migration("cios_schema", open("migrations/001_cios_schema.sql").read())
```

### 5. Test It
```python
def test_cios_01_schema():
    # Verify tables exist
    result = supabase.table("cios_pipeline_runs").select("*").limit(1).execute()
    assert result is not None  # Table exists

    # Verify columns
    # (Supabase doesn't expose schema easily, so test by inserting)
    test_run = {
        "run_id": uuid.uuid4(),
        "run_type": "test",
        "status": "running",
        "input": {"topic": "test"},
        "steps": []
    }
    result = supabase.table("cios_pipeline_runs").insert(test_run).execute()
    assert result.data[0]["run_id"] == test_run["run_id"]
```

### 6. Verify & Commit
```bash
pytest tests/test_schema.py -v
git add media-vault/backend/migrations/001_cios_schema.sql
git commit -m "feat: CIOS-01 create Supabase schema (4 tables)"
git push origin main
```

---

## Support

**Questions?**
1. Check `CIOS-DEVELOPER-GUIDE.md` for patterns
2. Look at existing feature implementations for examples
3. Check `feature_list.json` acceptance criteria again
4. Review error messages in service logs

**Stuck?**
- Verify all services running: `bash harness/launch-cios.sh`
- Check Supabase: https://ivhfuhxorppptyuofbgq.supabase.co
- Review feature dependencies: May need to complete prerequisite feature first

---

**Ready? Pick a feature from feature_list.json and start coding!** 🚀
