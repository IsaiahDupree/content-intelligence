/**
 * Blotato account sync — enumerate accounts, paginate last 90 days of posts,
 * upsert metrics into ci_posts.
 */
const { getClient } = require('./supabase');

const BLOTATO_API_URL = process.env.BLOTATO_API_URL || 'https://backend.blotato.com/v2';
const BLOTATO_API_KEY = process.env.BLOTATO_API_KEY || '';

// Known account IDs from ecosystem
const KNOWN_ACCOUNTS = {
  youtube: 228,
  tiktok: 710,
  instagram: 807,
  twitter: 571,
  facebook: 786
};

async function blotatoFetch(path) {
  if (!BLOTATO_API_KEY) {
    throw new Error('BLOTATO_API_KEY env var required');
  }

  const url = `${BLOTATO_API_URL}${path}`;
  const res = await fetch(url, {
    headers: {
      'blotato-api-key': BLOTATO_API_KEY,
      'Content-Type': 'application/json'
    }
  });

  if (!res.ok) {
    throw new Error(`Blotato API ${res.status}: ${await res.text()}`);
  }

  return res.json();
}

async function listAccounts() {
  const data = await blotatoFetch('/accounts');
  return Array.isArray(data) ? data : (data.accounts || data.data || []);
}

async function fetchPostsForAccount(accountId, days = 90) {
  const since = new Date();
  since.setDate(since.getDate() - days);
  const sinceStr = since.toISOString().split('T')[0];

  let allPosts = [];
  let page = 1;
  const pageSize = 100;

  while (true) {
    try {
      const data = await blotatoFetch(
        `/accounts/${accountId}/posts?page=${page}&per_page=${pageSize}&since=${sinceStr}`
      );

      const posts = Array.isArray(data) ? data : (data.posts || data.data || []);
      if (posts.length === 0) break;

      allPosts = allPosts.concat(posts);
      if (posts.length < pageSize) break;
      page++;
    } catch (err) {
      // Some accounts may not support post listing
      break;
    }
  }

  return allPosts;
}

function normalizeBlotatoPost(post, accountId, platform) {
  return {
    source: 'blotato',
    source_id: `blotato_${accountId}_${post.id || post.post_id || post.external_id}`,
    platform: platform || post.platform || 'unknown',
    account_id: String(accountId),
    title: post.title || null,
    caption: post.caption || post.text || post.content || null,
    media_url: post.media_url || post.image_url || post.video_url || null,
    media_type: post.media_type || (post.video_url ? 'video' : 'image'),
    filename: extractFilename(post.media_url || post.image_url || post.video_url),
    filename_stem: extractStem(post.media_url || post.image_url || post.video_url),
    published_at: post.published_at || post.created_at || post.posted_at,
    likes: post.likes || post.like_count || 0,
    comments: post.comments || post.comment_count || 0,
    shares: post.shares || post.share_count || post.retweets || 0,
    views: post.views || post.view_count || post.impressions || 0,
    engagement_rate: calcEngagement(post),
    raw_data: post
  };
}

function calcEngagement(post) {
  const likes = post.likes || post.like_count || 0;
  const comments = post.comments || post.comment_count || 0;
  const shares = post.shares || post.share_count || 0;
  const views = post.views || post.view_count || post.impressions || 1;
  if (views === 0) return 0;
  return parseFloat(((likes + comments + shares) / views).toFixed(6));
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

async function sync() {
  const sb = getClient();
  let accounts;

  try {
    accounts = await listAccounts();
  } catch (err) {
    // Fallback to known accounts
    accounts = Object.entries(KNOWN_ACCOUNTS).map(([platform, id]) => ({
      id, platform, name: platform
    }));
  }

  const results = { accounts: accounts.length, totalPosts: 0, upserted: 0, errors: 0, byPlatform: {} };

  for (const account of accounts) {
    const platform = account.platform || account.type || account.network || 'unknown';
    const posts = await fetchPostsForAccount(account.id);
    const normalized = posts.map(p => normalizeBlotatoPost(p, account.id, platform));

    let accountUpserted = 0;
    for (const post of normalized) {
      const { error } = await sb
        .from('ci_posts')
        .upsert(post, { onConflict: 'source,source_id' });
      if (error) {
        results.errors++;
      } else {
        accountUpserted++;
      }
    }

    results.totalPosts += posts.length;
    results.upserted += accountUpserted;
    results.byPlatform[platform] = (results.byPlatform[platform] || 0) + posts.length;
  }

  return results;
}

module.exports = { sync, listAccounts, fetchPostsForAccount, normalizeBlotatoPost, calcEngagement, extractFilename, extractStem, KNOWN_ACCOUNTS };
