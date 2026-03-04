/**
 * MPLite DB audit — fetch publish_queue, daily_counters, platforms, activity_log
 * and upsert into ci_posts.
 */
const { getClient } = require('./supabase');

const MPLITE_URL = process.env.MPLITE_URL || 'https://mediaposter-lite-isaiahduprees-projects.vercel.app';

async function fetchMPLitePosts() {
  const sb = getClient();

  // Fetch from publish_queue (the MPLite queue table)
  const { data: queue, error: qErr } = await sb
    .from('publish_queue')
    .select('*')
    .order('created_at', { ascending: false })
    .limit(1000);

  if (qErr) throw new Error(`Failed to fetch publish_queue: ${qErr.message}`);

  return queue || [];
}

async function fetchDailyCounters() {
  const sb = getClient();
  const { data, error } = await sb
    .from('daily_counters')
    .select('*')
    .order('date', { ascending: false })
    .limit(90);

  if (error) {
    // Table may not exist
    if (error.code === '42P01') return [];
    throw new Error(`Failed to fetch daily_counters: ${error.message}`);
  }
  return data || [];
}

async function fetchActivityLog() {
  const sb = getClient();
  const { data, error } = await sb
    .from('activity_log')
    .select('*')
    .order('created_at', { ascending: false })
    .limit(500);

  if (error) {
    if (error.code === '42P01') return [];
    throw new Error(`Failed to fetch activity_log: ${error.message}`);
  }
  return data || [];
}

function normalizeQueuePost(row) {
  return {
    source: 'mplite',
    source_id: `mplite_${row.id}`,
    platform: row.platform || row.account_type || 'unknown',
    account_id: row.account_id ? String(row.account_id) : null,
    title: row.title || null,
    caption: row.caption || row.text || null,
    media_url: row.media_url || row.mediaUrl || null,
    media_type: row.media_type || row.mediaType || 'unknown',
    filename: extractFilename(row.media_url || row.mediaUrl),
    filename_stem: extractStem(row.media_url || row.mediaUrl),
    published_at: row.published_at || row.scheduled_for || row.created_at,
    likes: 0,
    comments: 0,
    shares: 0,
    views: 0,
    engagement_rate: 0,
    raw_data: row
  };
}

function extractFilename(url) {
  if (!url) return null;
  try {
    const pathname = new URL(url).pathname;
    return pathname.split('/').pop() || null;
  } catch {
    return url.split('/').pop() || null;
  }
}

function extractStem(url) {
  const fname = extractFilename(url);
  if (!fname) return null;
  const lastDot = fname.lastIndexOf('.');
  return lastDot > 0 ? fname.substring(0, lastDot) : fname;
}

async function audit() {
  const sb = getClient();
  const posts = await fetchMPLitePosts();
  const dailyCounters = await fetchDailyCounters();
  const activityLog = await fetchActivityLog();

  const normalized = posts.map(normalizeQueuePost);
  let upserted = 0;
  let errors = 0;

  // Batch upsert into ci_posts
  for (const post of normalized) {
    const { error } = await sb
      .from('ci_posts')
      .upsert(post, { onConflict: 'source,source_id' });

    if (error) {
      errors++;
    } else {
      upserted++;
    }
  }

  return {
    source: 'mplite',
    totalFetched: posts.length,
    upserted,
    errors,
    dailyCounters: dailyCounters.length,
    activityLogEntries: activityLog.length
  };
}

module.exports = { audit, fetchMPLitePosts, fetchDailyCounters, fetchActivityLog, normalizeQueuePost, extractFilename, extractStem };
