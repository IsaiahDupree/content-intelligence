/**
 * Content association graph — group posts by filename stem across platforms,
 * store in ci_analysis_snapshots.
 */
const { getClient } = require('./supabase');

async function buildGraph() {
  const sb = getClient();

  // Fetch all posts that have a filename_stem
  const { data: posts, error } = await sb
    .from('ci_posts')
    .select('id, source, platform, account_id, title, caption, filename, filename_stem, published_at, likes, comments, shares, views')
    .not('filename_stem', 'is', null)
    .order('filename_stem');

  if (error) throw new Error(`Failed to fetch posts for graph: ${error.message}`);
  if (!posts || posts.length === 0) return { groups: [], totalGroups: 0, totalPosts: 0 };

  // Group by filename_stem
  const groups = {};
  for (const post of posts) {
    const stem = post.filename_stem.toLowerCase();
    if (!groups[stem]) {
      groups[stem] = {
        stem,
        posts: [],
        platforms: new Set(),
        totalLikes: 0,
        totalViews: 0
      };
    }
    groups[stem].posts.push(post);
    groups[stem].platforms.add(post.platform);
    groups[stem].totalLikes += post.likes || 0;
    groups[stem].totalViews += post.views || 0;
  }

  // Convert to array and serialize Sets
  const graphData = Object.values(groups)
    .filter(g => g.posts.length > 0)
    .map(g => ({
      stem: g.stem,
      platforms: [...g.platforms],
      postCount: g.posts.length,
      crossPosted: g.platforms.size > 1,
      totalLikes: g.totalLikes,
      totalViews: g.totalViews,
      posts: g.posts.map(p => ({
        id: p.id,
        platform: p.platform,
        likes: p.likes,
        views: p.views,
        published_at: p.published_at
      }))
    }))
    .sort((a, b) => b.postCount - a.postCount);

  const crossPostedCount = graphData.filter(g => g.crossPosted).length;
  const singlePlatformCount = graphData.filter(g => !g.crossPosted).length;

  const snapshot = {
    snapshot_type: 'content_association_graph',
    data: {
      totalGroups: graphData.length,
      crossPosted: crossPostedCount,
      singlePlatform: singlePlatformCount,
      groups: graphData,
      builtAt: new Date().toISOString()
    }
  };

  // Save to Supabase
  const { error: saveError } = await sb.from('ci_analysis_snapshots').insert(snapshot);
  if (saveError) {
    snapshot.saveError = saveError.message;
  }

  return snapshot.data;
}

module.exports = { buildGraph };
