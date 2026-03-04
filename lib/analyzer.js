/**
 * Cross-platform analyzer — top performers, platform breakdown,
 * content gaps, recommendations; upsert ci_analysis_snapshots.
 */
const { getClient } = require('./supabase');

async function fetchAllPosts() {
  const sb = getClient();
  const { data, error } = await sb
    .from('ci_posts')
    .select('*')
    .order('published_at', { ascending: false });

  if (error) throw new Error(`Failed to fetch ci_posts: ${error.message}`);
  return data || [];
}

function platformBreakdown(posts) {
  const platforms = {};
  for (const post of posts) {
    const p = post.platform || 'unknown';
    if (!platforms[p]) {
      platforms[p] = { count: 0, totalLikes: 0, totalComments: 0, totalShares: 0, totalViews: 0 };
    }
    platforms[p].count++;
    platforms[p].totalLikes += post.likes || 0;
    platforms[p].totalComments += post.comments || 0;
    platforms[p].totalShares += post.shares || 0;
    platforms[p].totalViews += post.views || 0;
  }

  // Calc averages
  for (const [, stats] of Object.entries(platforms)) {
    stats.avgLikes = stats.count > 0 ? Math.round(stats.totalLikes / stats.count) : 0;
    stats.avgComments = stats.count > 0 ? Math.round(stats.totalComments / stats.count) : 0;
    stats.avgEngagement = stats.totalViews > 0
      ? parseFloat(((stats.totalLikes + stats.totalComments + stats.totalShares) / stats.totalViews).toFixed(6))
      : 0;
  }

  return platforms;
}

function topPerformers(posts, limit = 10) {
  return [...posts]
    .sort((a, b) => {
      const scoreA = (a.likes || 0) + (a.comments || 0) * 2 + (a.shares || 0) * 3;
      const scoreB = (b.likes || 0) + (b.comments || 0) * 2 + (b.shares || 0) * 3;
      return scoreB - scoreA;
    })
    .slice(0, limit)
    .map(p => ({
      id: p.id,
      platform: p.platform,
      title: p.title || p.caption?.substring(0, 60),
      likes: p.likes,
      comments: p.comments,
      shares: p.shares,
      views: p.views,
      score: (p.likes || 0) + (p.comments || 0) * 2 + (p.shares || 0) * 3,
      published_at: p.published_at
    }));
}

function contentGaps(posts) {
  const gaps = [];
  const platforms = new Set(posts.map(p => p.platform).filter(Boolean));
  const allPlatforms = ['youtube', 'tiktok', 'instagram', 'twitter', 'facebook', 'linkedin', 'threads'];

  // Missing platforms
  for (const p of allPlatforms) {
    if (!platforms.has(p)) {
      gaps.push({ type: 'missing_platform', platform: p, suggestion: `No posts found for ${p}. Consider cross-posting.` });
    }
  }

  // Low-frequency platforms
  const breakdown = platformBreakdown(posts);
  const avgCount = posts.length / Math.max(platforms.size, 1);
  for (const [platform, stats] of Object.entries(breakdown)) {
    if (stats.count < avgCount * 0.3) {
      gaps.push({ type: 'low_frequency', platform, count: stats.count, suggestion: `${platform} has ${stats.count} posts — below average. Increase posting frequency.` });
    }
  }

  // No recent posts (last 7 days)
  const weekAgo = new Date();
  weekAgo.setDate(weekAgo.getDate() - 7);
  for (const platform of platforms) {
    const recentPosts = posts.filter(p => p.platform === platform && p.published_at && new Date(p.published_at) > weekAgo);
    if (recentPosts.length === 0) {
      gaps.push({ type: 'stale', platform, suggestion: `No posts on ${platform} in the last 7 days.` });
    }
  }

  return gaps;
}

function recommendations(posts, breakdown, gaps) {
  const recs = [];

  // Top platform recommendation
  const sortedPlatforms = Object.entries(breakdown).sort((a, b) => b[1].avgEngagement - a[1].avgEngagement);
  if (sortedPlatforms.length > 0) {
    const [topPlatform, stats] = sortedPlatforms[0];
    recs.push(`Double down on ${topPlatform} — highest avg engagement (${(stats.avgEngagement * 100).toFixed(2)}%).`);
  }

  // Cross-post suggestion
  const missingPlatforms = gaps.filter(g => g.type === 'missing_platform');
  if (missingPlatforms.length > 0) {
    recs.push(`Cross-post to: ${missingPlatforms.map(g => g.platform).join(', ')}.`);
  }

  // Posting consistency
  const stalePlatforms = gaps.filter(g => g.type === 'stale');
  if (stalePlatforms.length > 0) {
    recs.push(`Resume posting on: ${stalePlatforms.map(g => g.platform).join(', ')} (no posts in 7 days).`);
  }

  // Content type mix
  const videoCount = posts.filter(p => p.media_type === 'video').length;
  const imageCount = posts.filter(p => p.media_type === 'image').length;
  if (posts.length > 0) {
    const videoRatio = videoCount / posts.length;
    if (videoRatio < 0.3) {
      recs.push(`Only ${Math.round(videoRatio * 100)}% video content — consider increasing video output for higher engagement.`);
    }
  }

  return recs;
}

async function analyze() {
  const sb = getClient();
  const posts = await fetchAllPosts();
  const breakdown = platformBreakdown(posts);
  const top = topPerformers(posts);
  const gaps = contentGaps(posts);
  const recs = recommendations(posts, breakdown, gaps);

  const snapshot = {
    snapshot_type: 'cross_platform_analysis',
    data: {
      totalPosts: posts.length,
      platforms: breakdown,
      topPerformers: top,
      contentGaps: gaps,
      recommendations: recs,
      analyzedAt: new Date().toISOString()
    }
  };

  // Save snapshot to Supabase
  const { error } = await sb.from('ci_analysis_snapshots').insert(snapshot);
  if (error) {
    snapshot.saveError = error.message;
  }

  return snapshot.data;
}

module.exports = { analyze, fetchAllPosts, platformBreakdown, topPerformers, contentGaps, recommendations };
