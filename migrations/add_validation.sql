-- Add CHECK constraints for data validation and integrity
-- Note: These migrations assume the content table exists in Supabase
-- If it doesn't exist yet, please create it first before running these migrations

-- Validate confidence scores are between 0 and 100
ALTER TABLE content
  ADD CONSTRAINT check_ugc_format_confidence
    CHECK (
      ai_ugc_format IS NULL OR
      (ai_ugc_format->>'confidence')::numeric >= 0 AND
      (ai_ugc_format->>'confidence')::numeric <= 100
    );

-- Validate performance scores are between 0 and 100
ALTER TABLE content
  ADD CONSTRAINT check_performance_score_valid
    CHECK (
      ai_performance_score IS NULL OR
      (ai_performance_score->>'overall_score')::numeric IS NULL OR
      ((ai_performance_score->>'overall_score')::numeric >= 0 AND
       (ai_performance_score->>'overall_score')::numeric <= 100)
    );

-- Ensure repurpose_formats contains an array
ALTER TABLE content
  ADD CONSTRAINT check_repurpose_formats_array
    CHECK (
      ai_repurpose_formats IS NULL OR
      jsonb_typeof(ai_repurpose_formats->'recommendations') = 'array'
    );
