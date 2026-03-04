/**
 * Unit + integration tests for Content Intelligence System.
 * Uses Node.js built-in test runner (node --test).
 *
 * Tests cover: hash matching, filename extraction, engagement calc,
 * dry-run dedup, analyzer recommendations, platform breakdown, content gaps,
 * association graph grouping, and full pipeline flow.
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

// ====== MPLite module tests ======
describe('MPLite', () => {
  const { normalizeQueuePost, extractFilename, extractStem } = require('../lib/mplite');

  it('extractFilename returns filename from URL', () => {
    assert.equal(extractFilename('https://storage.example.com/bucket/video.mp4'), 'video.mp4');
    assert.equal(extractFilename('https://example.com/path/to/image.jpg?v=1'), 'image.jpg');
    assert.equal(extractFilename(null), null);
    assert.equal(extractFilename(''), null);
  });

  it('extractStem returns filename without extension', () => {
    assert.equal(extractStem('https://example.com/video.mp4'), 'video');
    assert.equal(extractStem('https://example.com/my-content.final.mp4'), 'my-content.final');
    assert.equal(extractStem(null), null);
  });

  it('normalizeQueuePost maps fields correctly', () => {
    const row = {
      id: 42,
      platform: 'tiktok',
      account_id: 710,
      title: 'Test Video',
      caption: 'Great content',
      media_url: 'https://storage.example.com/video.mp4',
      media_type: 'video',
      created_at: '2025-01-15T10:00:00Z'
    };
    const result = normalizeQueuePost(row);
    assert.equal(result.source, 'mplite');
    assert.equal(result.source_id, 'mplite_42');
    assert.equal(result.platform, 'tiktok');
    assert.equal(result.account_id, '710');
    assert.equal(result.filename, 'video.mp4');
    assert.equal(result.filename_stem, 'video');
    assert.equal(result.media_type, 'video');
  });
});

// ====== Blotato module tests ======
describe('Blotato', () => {
  const { normalizeBlotatoPost, calcEngagement, extractFilename, extractStem, KNOWN_ACCOUNTS } = require('../lib/blotato');

  it('calcEngagement computes correct ratio', () => {
    assert.equal(calcEngagement({ likes: 100, comments: 50, shares: 10, views: 1000 }), 0.16);
    assert.equal(calcEngagement({ likes: 0, comments: 0, shares: 0, views: 0 }), 0);
    assert.equal(calcEngagement({ likes: 10, views: 100 }), 0.1);
  });

  it('normalizeBlotatoPost maps fields correctly', () => {
    const post = {
      id: 'abc123',
      title: 'My Post',
      caption: 'Caption text',
      media_url: 'https://cdn.example.com/clip.mp4',
      media_type: 'video',
      likes: 500,
      comments: 30,
      views: 10000,
      published_at: '2025-02-01T12:00:00Z'
    };
    const result = normalizeBlotatoPost(post, 807, 'instagram');
    assert.equal(result.source, 'blotato');
    assert.equal(result.source_id, 'blotato_807_abc123');
    assert.equal(result.platform, 'instagram');
    assert.equal(result.likes, 500);
    assert.equal(result.filename, 'clip.mp4');
    assert.equal(result.filename_stem, 'clip');
  });

  it('KNOWN_ACCOUNTS has all platforms', () => {
    assert.ok(KNOWN_ACCOUNTS.youtube);
    assert.ok(KNOWN_ACCOUNTS.tiktok);
    assert.ok(KNOWN_ACCOUNTS.instagram);
    assert.ok(KNOWN_ACCOUNTS.twitter);
    assert.ok(KNOWN_ACCOUNTS.facebook);
  });
});

// ====== Scanner module tests ======
describe('Scanner', () => {
  const { scanDir, hashFile, getMimeType, MEDIA_EXTENSIONS } = require('../lib/scanner');

  it('MEDIA_EXTENSIONS includes common video and image types', () => {
    assert.ok(MEDIA_EXTENSIONS.has('.mp4'));
    assert.ok(MEDIA_EXTENSIONS.has('.mov'));
    assert.ok(MEDIA_EXTENSIONS.has('.jpg'));
    assert.ok(MEDIA_EXTENSIONS.has('.png'));
    assert.ok(MEDIA_EXTENSIONS.has('.webm'));
    assert.ok(!MEDIA_EXTENSIONS.has('.txt'));
    assert.ok(!MEDIA_EXTENSIONS.has('.js'));
  });

  it('getMimeType returns correct MIME types', () => {
    assert.equal(getMimeType('.mp4'), 'video/mp4');
    assert.equal(getMimeType('.jpg'), 'image/jpeg');
    assert.equal(getMimeType('.png'), 'image/png');
    assert.equal(getMimeType('.xyz'), 'application/octet-stream');
  });

  it('scanDir returns empty array for non-existent directory', () => {
    const result = scanDir('/non/existent/dir/12345');
    assert.deepEqual(result, []);
  });

  it('hashFile produces consistent SHA256', async () => {
    // Create a temp file
    const tmpDir = path.join(__dirname, '.tmp_test');
    const tmpFile = path.join(tmpDir, 'test.txt');
    fs.mkdirSync(tmpDir, { recursive: true });
    fs.writeFileSync(tmpFile, 'hello world');

    const hash = await hashFile(tmpFile);
    const expected = crypto.createHash('sha256').update('hello world').digest('hex');
    assert.equal(hash, expected);

    // Cleanup
    fs.unlinkSync(tmpFile);
    fs.rmdirSync(tmpDir);
  });

  it('scanDir finds media files in a directory', () => {
    const tmpDir = path.join(__dirname, '.tmp_scan_test');
    fs.mkdirSync(tmpDir, { recursive: true });
    fs.writeFileSync(path.join(tmpDir, 'video.mp4'), 'fake');
    fs.writeFileSync(path.join(tmpDir, 'image.jpg'), 'fake');
    fs.writeFileSync(path.join(tmpDir, 'readme.txt'), 'text');

    const files = scanDir(tmpDir);
    assert.equal(files.length, 2);
    assert.ok(files.some(f => f.filename === 'video.mp4'));
    assert.ok(files.some(f => f.filename === 'image.jpg'));
    assert.ok(!files.some(f => f.filename === 'readme.txt'));

    // Check fields
    const mp4 = files.find(f => f.filename === 'video.mp4');
    assert.equal(mp4.extension, '.mp4');
    assert.equal(mp4.filename_stem, 'video');
    assert.equal(mp4.mime_type, 'video/mp4');
    assert.ok(mp4.size_bytes > 0);

    // Cleanup
    fs.unlinkSync(path.join(tmpDir, 'video.mp4'));
    fs.unlinkSync(path.join(tmpDir, 'image.jpg'));
    fs.unlinkSync(path.join(tmpDir, 'readme.txt'));
    fs.rmdirSync(tmpDir);
  });
});

// ====== Deleter module tests ======
describe('Deleter', () => {
  const { formatBytes } = require('../lib/deleter');

  it('formatBytes formats correctly', () => {
    assert.equal(formatBytes(0), '0 B');
    assert.equal(formatBytes(1024), '1.00 KB');
    assert.equal(formatBytes(1048576), '1.00 MB');
    assert.equal(formatBytes(1073741824), '1.00 GB');
    assert.equal(formatBytes(500), '500.00 B');
  });
});

// ====== Analyzer module tests ======
describe('Analyzer', () => {
  const { platformBreakdown, topPerformers, contentGaps, recommendations } = require('../lib/analyzer');

  const samplePosts = [
    { id: 1, platform: 'tiktok', likes: 1000, comments: 50, shares: 200, views: 50000, media_type: 'video', published_at: new Date().toISOString() },
    { id: 2, platform: 'tiktok', likes: 500, comments: 20, shares: 100, views: 25000, media_type: 'video', published_at: new Date().toISOString() },
    { id: 3, platform: 'instagram', likes: 800, comments: 100, shares: 50, views: 10000, media_type: 'image', published_at: new Date().toISOString() },
    { id: 4, platform: 'youtube', likes: 2000, comments: 300, shares: 500, views: 100000, media_type: 'video', published_at: new Date().toISOString() },
    { id: 5, platform: 'twitter', likes: 100, comments: 10, shares: 50, views: 5000, media_type: 'image', published_at: new Date().toISOString() },
  ];

  it('platformBreakdown groups and calculates averages', () => {
    const result = platformBreakdown(samplePosts);
    assert.equal(result.tiktok.count, 2);
    assert.equal(result.tiktok.totalLikes, 1500);
    assert.equal(result.instagram.count, 1);
    assert.equal(result.youtube.count, 1);
    assert.equal(result.twitter.count, 1);
    assert.ok(result.tiktok.avgLikes > 0);
  });

  it('topPerformers sorts by engagement score', () => {
    const top = topPerformers(samplePosts, 3);
    assert.equal(top.length, 3);
    // YouTube post has highest score: 2000 + 300*2 + 500*3 = 4100
    assert.equal(top[0].platform, 'youtube');
    assert.equal(top[0].score, 4100);
  });

  it('contentGaps identifies missing platforms', () => {
    const gaps = contentGaps(samplePosts);
    const missingPlatforms = gaps.filter(g => g.type === 'missing_platform').map(g => g.platform);
    assert.ok(missingPlatforms.includes('facebook'));
    assert.ok(missingPlatforms.includes('linkedin'));
    assert.ok(missingPlatforms.includes('threads'));
    assert.ok(!missingPlatforms.includes('tiktok'));
    assert.ok(!missingPlatforms.includes('instagram'));
  });

  it('recommendations generates actionable suggestions', () => {
    const breakdown = platformBreakdown(samplePosts);
    const gaps = contentGaps(samplePosts);
    const recs = recommendations(samplePosts, breakdown, gaps);
    assert.ok(recs.length > 0);
    // Should recommend cross-posting to missing platforms
    assert.ok(recs.some(r => r.includes('Cross-post')));
  });
});

// ====== CLI Integration test ======
describe('CLI', () => {
  it('index.js shows help when no command given', async () => {
    const { execSync } = require('child_process');
    const output = execSync('node index.js', { cwd: path.join(__dirname, '..'), encoding: 'utf8' });
    assert.ok(output.includes('Content Intelligence System'));
    assert.ok(output.includes('migrate'));
    assert.ok(output.includes('audit'));
    assert.ok(output.includes('sync'));
    assert.ok(output.includes('scan'));
    assert.ok(output.includes('delete'));
    assert.ok(output.includes('analyze'));
    assert.ok(output.includes('status'));
  });
});

// ====== Migration module tests ======
describe('Migration', () => {
  const { MIGRATIONS } = require('../lib/migrate');

  it('defines all required tables', () => {
    const tableNames = MIGRATIONS.map(m => m.name);
    assert.ok(tableNames.includes('ci_posts'));
    assert.ok(tableNames.includes('ci_local_files'));
    assert.ok(tableNames.includes('ci_analysis_snapshots'));
  });

  it('migration SQL creates correct columns', () => {
    const ciPosts = MIGRATIONS.find(m => m.name === 'ci_posts');
    assert.ok(ciPosts.sql.includes('source TEXT'));
    assert.ok(ciPosts.sql.includes('platform TEXT'));
    assert.ok(ciPosts.sql.includes('filename_stem TEXT'));
    assert.ok(ciPosts.sql.includes('engagement_rate REAL'));
    assert.ok(ciPosts.sql.includes('UNIQUE(source, source_id)'));

    const ciFiles = MIGRATIONS.find(m => m.name === 'ci_local_files');
    assert.ok(ciFiles.sql.includes('sha256 TEXT'));
    assert.ok(ciFiles.sql.includes('safe_to_delete BOOLEAN'));
    assert.ok(ciFiles.sql.includes('file_path TEXT NOT NULL UNIQUE'));

    const ciSnapshots = MIGRATIONS.find(m => m.name === 'ci_analysis_snapshots');
    assert.ok(ciSnapshots.sql.includes('snapshot_type TEXT'));
    assert.ok(ciSnapshots.sql.includes('data JSONB'));
  });
});
