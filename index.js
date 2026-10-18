#!/usr/bin/env node
/**
 * Content Intelligence System CLI
 *
 * Commands:
 *   migrate  - Create Supabase tables (ci_posts, ci_local_files, ci_analysis_snapshots)
 *   audit    - MPLite DB audit: fetch publish_queue → upsert ci_posts
 *   sync     - Blotato account sync: enumerate accounts → paginate posts → upsert ci_posts
 *   scan     - Local file scanner: recursive scan → SHA256 → match against ci_posts
 *   delete   - Safe-delete engine: dry-run (or --confirm-delete for actual deletion)
 *   analyze  - Cross-platform analyzer: top performers, gaps, recommendations
 *   graph    - Content association graph: group posts by filename stem
 *   status   - CLI dashboard: latest snapshot summary
 */
require('dotenv').config();

const command = process.argv[2];
const flags = process.argv.slice(3);

async function main() {
  try {
    switch (command) {
      case 'migrate': {
        const { migrate, checkTables } = require('./lib/migrate');
        console.log('Running schema migration...');
        const results = await migrate();
        console.log('Migration results:', JSON.stringify(results, null, 2));
        const status = await checkTables();
        console.log('Table status:', JSON.stringify(status, null, 2));
        break;
      }

      case 'audit': {
        const { audit } = require('./lib/mplite');
        console.log('Auditing MPLite DB...');
        const result = await audit();
        console.log('Audit complete:', JSON.stringify(result, null, 2));
        break;
      }

      case 'sync': {
        const { sync } = require('./lib/blotato');
        console.log('Syncing Blotato accounts...');
        const result = await sync();
        console.log('Sync complete:', JSON.stringify(result, null, 2));
        break;
      }

      case 'scan': {
        const { scan } = require('./lib/scanner');
        console.log('Scanning local files...');
        const result = await scan();
        console.log('Scan complete:', JSON.stringify(result, null, 2));
        break;
      }

      case 'delete': {
        const { dryRun, confirmDelete } = require('./lib/deleter');
        if (flags.includes('--confirm-delete')) {
          console.log('DELETING files marked safe-to-delete...');
          const result = await confirmDelete();
          console.log('Delete complete:', JSON.stringify(result, null, 2));
        } else {
          console.log('Dry-run: showing files safe to delete...');
          const result = await dryRun();
          console.log('Dry-run result:', JSON.stringify(result, null, 2));
          console.log('\nTo actually delete, run: node index.js delete --confirm-delete');
        }
        break;
      }

      case 'analyze': {
        const { analyze } = require('./lib/analyzer');
        console.log('Running cross-platform analysis...');
        const result = await analyze();
        console.log('Analysis:', JSON.stringify(result, null, 2));
        break;
      }

      case 'graph': {
        const { buildGraph } = require('./lib/graph');
        console.log('Building content association graph...');
        const result = await buildGraph();
        console.log('Graph:', JSON.stringify(result, null, 2));
        break;
      }

      case 'status': {
        const { getStatus, printStatus } = require('./lib/dashboard');
        const status = await getStatus();
        printStatus(status);
        break;
      }

      default:
        console.log(`Content Intelligence System v1.0.0

Usage: node index.js <command>

Commands:
  migrate           Create Supabase tables
  audit             MPLite DB audit → ci_posts
  sync              Blotato account sync → ci_posts
  scan              Local file scanner → ci_local_files
  delete            Safe-delete report (add --confirm-delete to execute)
  analyze           Cross-platform analysis → ci_analysis_snapshots
  graph             Content association graph → ci_analysis_snapshots
  status            Dashboard summary
`);
        break;
    }
  } catch (err) {
    console.error('Error:', err.message);
    process.exit(1);
  }
}

main();
