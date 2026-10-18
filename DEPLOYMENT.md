# Content Intelligence — Deployment Guide

Complete guide for deploying Content Intelligence service to production.

## Pre-Deployment Checklist

- [ ] All tests passing (`pytest tests/ -v`)
- [ ] Environment variables configured (.env file)
- [ ] Supabase credentials verified
- [ ] Git changes committed
- [ ] Dependencies frozen in requirements.txt
- [ ] Health endpoint responds correctly
- [ ] Database migrations reviewed
- [ ] Security headers verified

## Step 1: Local Verification

```bash
# Test imports
python3 -c "import app; print('✅ App imports successfully')"

# Run full test suite
python3 -m pytest tests/ -v

# Test health endpoint
python3 app.py &
sleep 2
curl -s http://localhost:6006/health | python3 -m json.tool
```

## Step 2: Database Migrations

Apply migrations to Supabase:

```bash
# Migration files location: migrations/
# - add_ai_ugc_format.sql
# - add_ai_repurpose_formats.sql
# - add_ai_performance_score.sql

# Run via Supabase CLI or SQL editor in dashboard
# OR via MCP tool:
# supabase_apply_migration(name="add_ai_ugc_format", query="<sql_content>")
```

### Migration SQL

Each migration creates:
1. JSONB column on `content` table
2. GIN index for performance
3. Comment describing schema

All migrations are idempotent (safe to run multiple times).

## Step 3: Environment Setup

### Production Environment Variables

```bash
# Service
PORT=6006
FLASK_ENV=production
DEBUG=false

# Supabase
SUPABASE_URL=https://ivhfuhxorppptyuofbgq.supabase.co
SUPABASE_SERVICE_KEY=<your_service_key>

# Optional AI Providers (fallback to placeholders if not set)
GROQ_API_KEY=<optional>
OPENAI_API_KEY=<optional>
ANTHROPIC_API_KEY=<optional>
```

## Step 4: Deployment Options

### Option A: Vercel (Recommended)

1. **Push to GitHub**
```bash
git push origin main
```

2. **Deploy from GitHub**
```bash
vercel --prod --yes
```

3. **Configure Environment**
```bash
vercel env add SUPABASE_URL
vercel env add SUPABASE_SERVICE_KEY
vercel env add GROQ_API_KEY (optional)
```

4. **Redeploy**
```bash
vercel --prod --yes
```

### Option B: Docker

1. **Create Dockerfile**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 6006
CMD ["python3", "app.py"]
```

2. **Build and Run**
```bash
docker build -t content-intelligence .
docker run -p 6006:6006 \
  -e SUPABASE_URL=... \
  -e SUPABASE_SERVICE_KEY=... \
  content-intelligence
```

### Option C: Gunicorn Production

1. **Install**
```bash
pip install gunicorn
```

2. **Run**
```bash
gunicorn -w 4 -b 0.0.0.0:6006 \
  --timeout 30 \
  --access-logfile logs/access.log \
  --error-logfile logs/error.log \
  app:app
```

## Step 5: Verification

### Health Check
```bash
curl -s https://your-deployment-url/health | python3 -m json.tool
```

Expected response:
```json
{
  "status": "healthy",
  "service": "content-intelligence",
  "version": "1.0.0",
  "timestamp": "2026-04-17T18:15:00+00:00"
}
```

### Test Classification
```bash
curl -X POST https://your-url/api/classify \
  -H "Content-Type: application/json" \
  -d '{"content": "I love this product!"}'
```

### Monitor Logs
```bash
# Vercel
vercel logs

# Local
tail -f logs/api.log
```

## Step 6: Monitoring

### Health Monitoring
- Set up cron job to check /health every 5 minutes
- Alert if response != 200 or latency > 5s

### Performance Monitoring
- Monitor endpoint latencies
- Track error rates
- Monitor Supabase query performance

### Logging
- Logs stored in JSON format
- Structure: {timestamp, method, path, status, duration_ms}
- Retention: 30 days (configure in settings)

## Step 7: Rollback Plan

If issues occur:

### Vercel Rollback
```bash
vercel rollback
```

### Git Rollback
```bash
git revert <commit>
git push origin main
```

### Database Rollback
- Restore from Supabase backup (last 7 days)
- Or manually delete new columns if needed

## Security Checklist

- [ ] Environment variables not in git
- [ ] Supabase Row-Level Security (RLS) enabled
- [ ] API rate limiting configured
- [ ] CORS headers allow only expected origins
- [ ] CSP headers configured correctly
- [ ] Access logs monitored for suspicious activity
- [ ] Backups automated and tested

## Performance Targets

| Endpoint | Target | Actual |
|----------|--------|--------|
| /health | <100ms | ~10ms |
| /api/classify | <200ms | ~50ms |
| /api/score | <100ms | ~30ms |
| /api/repurpose | <100ms | ~25ms |
| /api/analyze | <300ms | ~150ms |

## Scaling

Current deployment handles:
- ~1000 requests/day (comfortable)
- ~100 requests/min (peak)

To scale:
1. Increase worker processes
2. Use load balancer (Vercel handles automatically)
3. Consider Redis for distributed caching

## Maintenance

### Weekly
- [ ] Check error logs
- [ ] Verify health endpoint
- [ ] Monitor latencies

### Monthly
- [ ] Review usage metrics
- [ ] Update dependencies if needed
- [ ] Test disaster recovery

### Quarterly
- [ ] Security audit
- [ ] Performance optimization
- [ ] Capacity planning

## Support

### Common Issues

**Supabase connection fails:**
- Verify SUPABASE_URL and SUPABASE_SERVICE_KEY
- Check network connectivity
- Review Supabase service status

**Classification returns all zeros:**
- Check if classifier service initialized
- Verify content is not empty
- Check cache TTL (24 hours default)

**High latency:**
- Check Supabase query performance
- Monitor cache hit rate
- Consider Redis for caching layer

## Deployment Verification Script

```bash
#!/bin/bash
set -e

echo "🔍 Running deployment verification..."

# Check health
echo "1. Checking health endpoint..."
HEALTH=$(curl -s -X GET https://$DEPLOY_URL/health)
if echo $HEALTH | grep -q '"status":"healthy"'; then
  echo "✅ Health check passed"
else
  echo "❌ Health check failed"
  exit 1
fi

# Check classification
echo "2. Testing classification..."
CLASS=$(curl -s -X POST https://$DEPLOY_URL/api/classify \
  -H "Content-Type: application/json" \
  -d '{"content":"Test content"}')
if echo $CLASS | grep -q '"format"'; then
  echo "✅ Classification working"
else
  echo "❌ Classification failed"
  exit 1
fi

# Check scoring
echo "3. Testing scoring..."
SCORE=$(curl -s -X POST https://$DEPLOY_URL/api/score \
  -H "Content-Type: application/json" \
  -d '{"metrics":{"views":1000}}')
if echo $SCORE | grep -q '"overall_score"'; then
  echo "✅ Scoring working"
else
  echo "❌ Scoring failed"
  exit 1
fi

echo "✅ All verification checks passed!"
echo "🚀 Deployment successful!"
```

---

Last Updated: 2026-04-17
