-- Add strategic indexes for performance queries on format data
-- Note: These migrations assume the content table exists in Supabase
-- If it doesn't exist yet, please create it first before running these migrations

-- Index on JSON fields for filter performance
CREATE INDEX IF NOT EXISTS idx_ai_performance_score ON content USING gin(ai_performance_score);
CREATE INDEX IF NOT EXISTS idx_ai_repurpose_formats ON content USING gin(ai_repurpose_formats);

-- Composite indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_content_user_ai_ugc_format ON content(user_id) WHERE ai_ugc_format IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_content_user_ai_performance ON content(user_id) WHERE ai_performance_score IS NOT NULL;
