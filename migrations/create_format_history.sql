-- Create format_history table to store historical format and score changes
CREATE TABLE IF NOT EXISTS format_history (
  id BIGSERIAL PRIMARY KEY,
  content_id UUID NOT NULL,
  format VARCHAR(50) NOT NULL,
  confidence NUMERIC(3, 2) CHECK (confidence >= 0 AND confidence <= 1),
  all_scores JSONB DEFAULT '{}',
  timestamp TIMESTAMPTZ DEFAULT NOW(),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes for query performance
CREATE INDEX IF NOT EXISTS idx_format_history_content_id ON format_history(content_id);
CREATE INDEX IF NOT EXISTS idx_format_history_timestamp ON format_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_format_history_format ON format_history(format);
