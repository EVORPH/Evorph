import { mergeRecords, validateExperimentPage, validateRecordPage } from '../research-contract.js?v=public-research-v2-1';

const LIMIT = 100;
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;
const REQUEST_TIMEOUT_MS = 12000;
const MAX_PAGES = 500;

export class ArchiveReadError extends Error {
  constructor(message, cause) {
    super(message);
    this.name = 'ArchiveReadError';
    if (cause !== undefined) this.cause = cause;
  }
}

/** Read-only archive access. Every page is validated with the site's own public
 * contract, and a cursor is followed to its end before a history is called complete. */
export function createArchiveReader({ fetcher = globalThis.fetch, now = () => new Date().toISOString() } = {}) {
  async function readJSON(path, params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined) query.set(key, String(value));
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetcher(path + (query.size ? '?' + query.toString() : ''), {
        method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error',
        headers: { Accept: 'application/json' }, signal: controller.signal,
      });
      if (!response.ok) throw new ArchiveReadError('The archive answered ' + response.status + ' for ' + path + '.');
      if (!response.headers.get('content-type')?.includes('application/json')) {
        throw new ArchiveReadError('The archive answered with a non-JSON document for ' + path + '.');
      }
      const source = response.body.getReader();
      const chunks = [];
      let length = 0;
      try {
        for (;;) {
          const { value, done } = await source.read();
          if (done) break;
          length += value.byteLength;
          if (length > MAX_RESPONSE_BYTES) throw new ArchiveReadError('The archive response exceeded the read bound for ' + path + '.');
          chunks.push(value);
        }
      } finally {
        await source.cancel();
      }
      const raw = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) { raw.set(chunk, offset); offset += chunk.byteLength; }
      try {
        return JSON.parse(new TextDecoder().decode(raw));
      } catch (error) {
        throw new ArchiveReadError('The archive returned unparseable JSON for ' + path + '.', error);
      }
    } catch (error) {
      if (error instanceof ArchiveReadError) throw error;
      if (error?.name === 'AbortError') {
        throw new ArchiveReadError('The archive did not answer within ' + REQUEST_TIMEOUT_MS + ' ms for ' + path + '.', error);
      }
      throw new ArchiveReadError('The archive could not be reached at ' + path + '.', error);
    } finally {
      clearTimeout(timer);
    }
  }

  async function followPages(path, validate, collect) {
    const seen = new Set();
    const collected = [];
    let cursor = null;
    let pages = 0;
    for (;;) {
      if (cursor !== null) {
        if (seen.has(cursor)) throw new ArchiveReadError('Pagination repeated a cursor for ' + path + '.');
        seen.add(cursor);
      }
      const value = await readJSON(path, { limit: LIMIT, cursor });
      try {
        validate(value);
      } catch (error) {
        throw new ArchiveReadError('The archive answered outside the public data contract for ' + path + '.', error);
      }
      pages += 1;
      collect(value, collected);
      cursor = typeof value.next_cursor === 'string' && value.next_cursor ? value.next_cursor : null;
      if (cursor === null) return { pages, collected };
      if (pages >= MAX_PAGES) throw new ArchiveReadError('Pagination did not terminate for ' + path + '.');
    }
  }

  function merged(collected) {
    try {
      return mergeRecords([], collected);
    } catch (error) {
      throw new ArchiveReadError('The archive returned contradictory public records.', error);
    }
  }

  return {
    async readStatus() {      const value = await readJSON('/api/research/status', {});
      if (value?.connected !== true || !['live', 'example'].includes(value?.mode)) {
        throw new ArchiveReadError('The archive did not report a connected public mode.');
      }
      return { connected: true, mode: value.mode, read_at: now() };
    },

    async listExperiments(mode) {
      const { pages, collected } = await followPages('/api/research/experiments',
        value => validateExperimentPage(value, mode),
        (value, out) => out.push(...value.experiments));
      const ids = collected.map(item => item.experiment_id);
      if (new Set(ids).size !== ids.length) throw new ArchiveReadError('The archive listed one experiment twice.');
      return { experiments: collected, pages };
    },

    async readRecords(experimentId, mode) {
      const path = '/api/research/experiments/' + encodeURIComponent(experimentId) + '/records';
      let experiment = null;
      const { pages, collected } = await followPages(path,
        value => validateRecordPage(value, experimentId, mode),
        (value, out) => { experiment = value.experiment; out.push(...value.records); });
      return { experiment, records: merged(collected), pages };
    },
  };
}
