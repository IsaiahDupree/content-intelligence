/**
 * Safe-delete engine — dry-run report of deletable files;
 * --confirm-delete flag for actual deletion.
 */
const fs = require('fs');
const { getClient } = require('./supabase');

async function getDeletableFiles() {
  const sb = getClient();
  const { data, error } = await sb
    .from('ci_local_files')
    .select('*')
    .eq('safe_to_delete', true);

  if (error) throw new Error(`Failed to fetch deletable files: ${error.message}`);
  return data || [];
}

function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(2)} ${units[i]}`;
}

async function dryRun() {
  const files = await getDeletableFiles();
  const totalBytes = files.reduce((sum, f) => sum + (f.size_bytes || 0), 0);

  return {
    mode: 'dry-run',
    deletableFiles: files.length,
    totalSize: formatBytes(totalBytes),
    totalSizeBytes: totalBytes,
    files: files.map(f => ({
      path: f.file_path,
      filename: f.filename,
      size: formatBytes(f.size_bytes || 0),
      sizeBytes: f.size_bytes || 0,
      matchedPostId: f.matched_post_id
    }))
  };
}

async function confirmDelete() {
  const files = await getDeletableFiles();
  const results = { deleted: 0, failed: 0, freedBytes: 0, errors: [] };
  const sb = getClient();

  for (const file of files) {
    try {
      if (fs.existsSync(file.file_path)) {
        fs.unlinkSync(file.file_path);
        results.deleted++;
        results.freedBytes += (file.size_bytes || 0);

        // Update the record
        await sb
          .from('ci_local_files')
          .update({ safe_to_delete: false, is_uploaded: true })
          .eq('id', file.id);
      } else {
        // File already gone
        results.deleted++;
        await sb.from('ci_local_files').delete().eq('id', file.id);
      }
    } catch (err) {
      results.failed++;
      results.errors.push({ path: file.file_path, error: err.message });
    }
  }

  results.freedSize = formatBytes(results.freedBytes);
  return results;
}

module.exports = { dryRun, confirmDelete, getDeletableFiles, formatBytes };
