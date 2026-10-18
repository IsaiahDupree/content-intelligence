-- Enable Row Level Security and create policies for format fields
-- Note: RLS policies assume a user_id column on the content table

-- Create policies for format_history table (if it exists)
CREATE TABLE IF NOT EXISTS format_history (
  id BIGSERIAL PRIMARY KEY,
  content_id UUID NOT NULL,
  format VARCHAR(50) NOT NULL,
  confidence NUMERIC(3, 2),
  all_scores JSONB,
  timestamp TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE format_history ENABLE ROW LEVEL SECURITY;

-- Policy to allow service role to insert records
CREATE POLICY IF NOT EXISTS "service_role_insert_history" ON format_history
  FOR INSERT WITH CHECK (auth.role() = 'service_role');

-- Policy to allow service role to read all records
CREATE POLICY IF NOT EXISTS "service_role_read_history" ON format_history
  FOR SELECT USING (auth.role() = 'service_role');

-- Index for performance
CREATE INDEX IF NOT EXISTS idx_format_history_content_id ON format_history(content_id);
