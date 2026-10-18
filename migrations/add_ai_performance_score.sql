-- Add ai_performance_score column to store performance metrics
ALTER TABLE content ADD COLUMN IF NOT EXISTS ai_performance_score JSONB DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_performance_score ON content USING gin(ai_performance_score);
-- Add comment
COMMENT ON COLUMN content.ai_performance_score IS 'Stores {overall_score: 0-100, engagement_score: 0-100, retention_score: 0-100, conversion_score: 0-100, timestamp: ISO8601}';
