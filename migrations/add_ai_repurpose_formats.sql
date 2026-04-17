-- Add ai_repurpose_formats column to store repurposing recommendations
ALTER TABLE content ADD COLUMN IF NOT EXISTS ai_repurpose_formats JSONB DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_repurpose_formats ON content USING gin(ai_repurpose_formats);
-- Add comment
COMMENT ON COLUMN content.ai_repurpose_formats IS 'Stores array of {format: string, viability_score: 0-100, rationale: string}';
