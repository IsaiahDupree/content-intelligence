/**
 * CLI status dashboard — prints latest snapshot: posts by platform,
 * indexed files, safe-to-delete bytes, top 5 recommendations.
 */
const { getClient } = require('./supabase');
const { formatBytes } = require('./deleter');

async function getStatus() {
  const sb = getClient();

  // Fetch post counts by platform
  const { data: posts } = await sb.from('ci_posts').select('platform');
  const platformCounts = {};
  for (const p of (posts || [])) {
    const plat = p.platform || 'unknown';
    platformCounts[plat] = (platformCounts[plat] || 0) + 1;
  }

  // Fetch local file stats
  const { data: files } = await sb.from('ci_local_files').select('size_bytes, safe_to_delete, is_uploaded');
  const totalFiles = (files || []).length;
  const uploadedFiles = (files || []).filter(f => f.is_uploaded).length;
  const deletableFiles = (files || []).filter(f => f.safe_to_delete).length;
  const deletableBytes = (files || []).filter(f => f.safe_to_delete).reduce((s, f) => s + (f.size_bytes || 0), 0);
  const totalBytes = (files || []).reduce((s, f) => s + (f.size_bytes || 0), 0);

  // Fetch latest analysis snapshot
  const { data: snapshots } = await sb
    .from('ci_analysis_snapshots')
    .select('data, created_at')
    .eq('snapshot_type', 'cross_platform_analysis')
    .order('created_at', { ascending: false })
    .limit(1);

  const latestAnalysis = snapshots?.[0]?.data || null;
  const analysisDate = snapshots?.[0]?.created_at || null;

  return {
    posts: {
      total: (posts || []).length,
      byPlatform: platformCounts
    },
    files: {
      total: totalFiles,
      uploaded: uploadedFiles,
      deletable: deletableFiles,
      deletableSize: formatBytes(deletableBytes),
      totalSize: formatBytes(totalBytes)
    },
    analysis: latestAnalysis ? {
      date: analysisDate,
      topPerformers: (latestAnalysis.topPerformers || []).slice(0, 5),
      recommendations: (latestAnalysis.recommendations || []).slice(0, 5),
      contentGaps: (latestAnalysis.contentGaps || []).length
    } : null
  };
}

function printStatus(status) {
  console.log('\n=== Content Intelligence Status ===\n');

  // Posts
  console.log(`Posts: ${status.posts.total} total`);
  for (const [platform, count] of Object.entries(status.posts.byPlatform)) {
    console.log(`  ${platform}: ${count}`);
  }

  // Files
  console.log(`\nLocal Files: ${status.files.total} indexed (${status.files.totalSize})`);
  console.log(`  Uploaded: ${status.files.uploaded}`);
  console.log(`  Safe to delete: ${status.files.deletable} (${status.files.deletableSize})`);

  // Analysis
  if (status.analysis) {
    console.log(`\nLatest Analysis: ${status.analysis.date}`);
    console.log(`  Content gaps: ${status.analysis.contentGaps}`);

    if (status.analysis.topPerformers?.length > 0) {
      console.log('\n  Top 5 Performers:');
      for (const p of status.analysis.topPerformers) {
        console.log(`    ${p.platform} | ${p.title || '(no title)'} | score: ${p.score}`);
      }
    }

    if (status.analysis.recommendations?.length > 0) {
      console.log('\n  Recommendations:');
      for (const r of status.analysis.recommendations) {
        console.log(`    - ${r}`);
      }
    }
  } else {
    console.log('\nNo analysis yet. Run: node index.js analyze');
  }

  console.log('');
}

module.exports = { getStatus, printStatus };
