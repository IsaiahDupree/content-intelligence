-- Add ai_ugc_format column to store detected UGC format and confidence
ALTER TABLE content ADD COLUMN IF NOT EXISTS ai_ugc_format JSONB DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_ugc_format ON content USING gin(ai_ugc_format);
-- Add comment explaining the structure
COMMENT ON COLUMN content.ai_ugc_format IS 'Stores {format: string, confidence: 0-100}';
