/**
 * Supabase schema migration — creates ci_posts, ci_local_files, ci_analysis_snapshots tables.
 */
const { getClient } = require('./supabase');

const MIGRATIONS = [
  {
    name: 'ci_posts',
    sql: `
      CREATE TABLE IF NOT EXISTS ci_posts (
        id BIGSERIAL PRIMARY KEY,
        source TEXT NOT NULL,
        source_id TEXT,
        platform TEXT,
        account_id TEXT,
        title TEXT,
        caption TEXT,
        media_url TEXT,
        media_type TEXT,
        filename TEXT,
        filename_stem TEXT,
        published_at TIMESTAMPTZ,
        likes INTEGER DEFAULT 0,
        comments INTEGER DEFAULT 0,
        shares INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        engagement_rate REAL DEFAULT 0,
        raw_data JSONB DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(source, source_id)
      );
    `
  },
  {
    name: 'ci_local_files',
    sql: `
      CREATE TABLE IF NOT EXISTS ci_local_files (
        id BIGSERIAL PRIMARY KEY,
        file_path TEXT NOT NULL UNIQUE,
        filename TEXT NOT NULL,
        filename_stem TEXT,
        extension TEXT,
        size_bytes BIGINT DEFAULT 0,
        sha256 TEXT,
        mime_type TEXT,
        matched_post_id BIGINT REFERENCES ci_posts(id),
        is_uploaded BOOLEAN DEFAULT FALSE,
        safe_to_delete BOOLEAN DEFAULT FALSE,
        scanned_at TIMESTAMPTZ DEFAULT NOW(),
        created_at TIMESTAMPTZ DEFAULT NOW()
      );
    `
  },
  {
    name: 'ci_analysis_snapshots',
    sql: `
      CREATE TABLE IF NOT EXISTS ci_analysis_snapshots (
        id BIGSERIAL PRIMARY KEY,
        snapshot_type TEXT NOT NULL,
        data JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );
    `
  }
];

async function migrate() {
  const sb = getClient();
  const results = [];

  for (const m of MIGRATIONS) {
    const { error } = await sb.rpc('exec_sql', { sql: m.sql }).maybeSingle();
    if (error) {
      // If rpc doesn't exist, try direct table check
      const { error: checkErr } = await sb.from(m.name).select('id').limit(1);
      if (checkErr && checkErr.code === '42P01') {
        // Table doesn't exist and we can't create it via RPC — report
        results.push({ table: m.name, status: 'needs_manual_creation', error: 'exec_sql RPC not available; run SQL in Supabase dashboard' });
      } else if (!checkErr) {
        results.push({ table: m.name, status: 'already_exists' });
      } else {
        results.push({ table: m.name, status: 'error', error: checkErr.message });
      }
    } else {
      results.push({ table: m.name, status: 'created' });
    }
  }

  return results;
}

async function checkTables() {
  const sb = getClient();
  const tables = ['ci_posts', 'ci_local_files', 'ci_analysis_snapshots'];
  const status = {};

  for (const t of tables) {
    const { error } = await sb.from(t).select('id').limit(1);
    status[t] = error ? (error.code === '42P01' ? 'missing' : 'error') : 'exists';
  }

  return status;
}

module.exports = { migrate, checkTables, MIGRATIONS };
