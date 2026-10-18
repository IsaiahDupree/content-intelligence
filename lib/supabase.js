/**
 * Supabase client for Content Intelligence System.
 * Shared project: ivhfuhxorppptyuofbgq
 */
const { createClient } = require('@supabase/supabase-js');

const SUPABASE_URL = process.env.SUPABASE_URL || 'https://ivhfuhxorppptyuofbgq.supabase.co';
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY || process.env.SUPABASE_KEY || '';

let _client = null;

function getClient() {
  if (!_client) {
    if (!SUPABASE_KEY) {
      throw new Error('SUPABASE_SERVICE_KEY or SUPABASE_KEY env var required');
    }
    _client = createClient(SUPABASE_URL, SUPABASE_KEY);
  }
  return _client;
}

module.exports = { getClient, SUPABASE_URL };
