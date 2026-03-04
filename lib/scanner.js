/**
 * Local file scanner — recursive scan of LOCAL_VIDEO_DIRS, SHA256 hash,
 * upsert ci_local_files, match against ci_posts by filename_stem.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { getClient } = require('./supabase');

const VIDEO_EXTENSIONS = new Set([
  '.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.3gp'
]);

const IMAGE_EXTENSIONS = new Set([
  '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.svg'
]);

const MEDIA_EXTENSIONS = new Set([...VIDEO_EXTENSIONS, ...IMAGE_EXTENSIONS]);

function getLocalDirs() {
  const envDirs = process.env.LOCAL_VIDEO_DIRS;
  if (envDirs) {
    return envDirs.split(',').map(d => d.trim()).filter(Boolean);
  }
  return [];
}

function hashFile(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(filePath);
    stream.on('data', chunk => hash.update(chunk));
    stream.on('end', () => resolve(hash.digest('hex')));
    stream.on('error', reject);
  });
}

function getMimeType(ext) {
  const map = {
    '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.avi': 'video/x-msvideo',
    '.mkv': 'video/x-matroska', '.webm': 'video/webm', '.flv': 'video/x-flv',
    '.wmv': 'video/x-ms-wmv', '.m4v': 'video/x-m4v', '.3gp': 'video/3gpp',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.tiff': 'image/tiff', '.svg': 'image/svg+xml'
  };
  return map[ext.toLowerCase()] || 'application/octet-stream';
}

function scanDir(dirPath, files = []) {
  if (!fs.existsSync(dirPath)) return files;

  const entries = fs.readdirSync(dirPath, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dirPath, entry.name);
    if (entry.isDirectory()) {
      // Skip hidden dirs and node_modules
      if (!entry.name.startsWith('.') && entry.name !== 'node_modules') {
        scanDir(fullPath, files);
      }
    } else if (entry.isFile()) {
      const ext = path.extname(entry.name).toLowerCase();
      if (MEDIA_EXTENSIONS.has(ext)) {
        const stat = fs.statSync(fullPath);
        files.push({
          file_path: fullPath,
          filename: entry.name,
          filename_stem: path.basename(entry.name, ext),
          extension: ext,
          size_bytes: stat.size,
          mime_type: getMimeType(ext)
        });
      }
    }
  }
  return files;
}

async function matchAgainstPosts(sb, fileRecord) {
  if (!fileRecord.filename_stem) return null;

  // Match by filename_stem against ci_posts
  const { data: matches } = await sb
    .from('ci_posts')
    .select('id, source, platform, filename_stem')
    .ilike('filename_stem', `%${fileRecord.filename_stem}%`)
    .limit(1);

  if (matches && matches.length > 0) {
    return matches[0].id;
  }
  return null;
}

async function scan() {
  const dirs = getLocalDirs();
  if (dirs.length === 0) {
    return { error: 'No LOCAL_VIDEO_DIRS configured. Set LOCAL_VIDEO_DIRS env var (comma-separated paths).' };
  }

  const sb = getClient();
  const allFiles = [];

  for (const dir of dirs) {
    scanDir(dir, allFiles);
  }

  let indexed = 0;
  let matched = 0;
  let errors = 0;

  for (const file of allFiles) {
    try {
      file.sha256 = await hashFile(file.file_path);
      const matchedPostId = await matchAgainstPosts(sb, file);
      file.matched_post_id = matchedPostId;
      file.is_uploaded = !!matchedPostId;
      file.safe_to_delete = !!matchedPostId;
      file.scanned_at = new Date().toISOString();

      const { error } = await sb
        .from('ci_local_files')
        .upsert(file, { onConflict: 'file_path' });

      if (error) {
        errors++;
      } else {
        indexed++;
        if (matchedPostId) matched++;
      }
    } catch (err) {
      errors++;
    }
  }

  return {
    dirsScanned: dirs.length,
    totalFiles: allFiles.length,
    indexed,
    matched,
    errors,
    totalSizeBytes: allFiles.reduce((sum, f) => sum + f.size_bytes, 0)
  };
}

module.exports = { scan, scanDir, hashFile, getMimeType, getLocalDirs, matchAgainstPosts, MEDIA_EXTENSIONS };
