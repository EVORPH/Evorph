export class ServiceError extends Error {
  constructor(status) { super('Service request could not be completed.'); this.name = 'ServiceError'; this.status = status; }
}

export function createServiceClient({ fetcher = globalThis.fetch, page = globalThis.location, cookies = () => document.cookie } = {}) {
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(page.hostname);
  const example = local && new URLSearchParams(page.search).get('research') === 'service';
  const client = { connected: false, mode: example ? 'example' : 'live' };
  let csrf = null, connecting = null;
  async function request(path, { method = 'GET', body, signal, headers = {} } = {}) {
    const controller = new AbortController();
    let timedOut = false;
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    else signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 12000);
    try {
      const response = await fetcher(path, { method, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: { Accept: 'application/json', ...(method === 'POST' ? { 'Content-Type': 'application/json' } : {}), ...headers },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal });
      if (!response.ok) throw new ServiceError(response.status);
      if (!response.headers.get('content-type')?.includes('application/json')) throw new ServiceError(502);
      const reader = response.body.getReader();
      const chunks = []; let length = 0;
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          length += value.byteLength;
          if (length > 4 * 1024 * 1024) throw new ServiceError(502);
          chunks.push(value);
        }
      } finally { await reader.cancel(); }
      const raw = new Uint8Array(length); let offset = 0;
      for (const chunk of chunks) { raw.set(chunk, offset); offset += chunk.byteLength; }
      return JSON.parse(new TextDecoder().decode(raw));
    } catch (error) {
      if (timedOut) throw new ServiceError(408);
      throw error;
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  }
  function issued(value) {
    if (!value || Object.keys(value).sort().join(',') !== 'contributor_id,csrf_token,recovery' ||
        !['contributor_id', 'csrf_token', 'recovery'].every(key => typeof value[key] === 'string') ||
        !/^[a-zA-Z0-9._:-]{1,120}$/.test(value.contributor_id) ||
        !/^[a-zA-Z0-9_-]{43}$/.test(value.csrf_token) || !/^[a-zA-Z0-9_-]{43}$/.test(value.recovery)) throw new ServiceError(502);
    csrf = value.csrf_token;
    return value;
  }
  function csrfToken() {
    const value = cookies().split(';').map(item => item.trim()).find(item => item.startsWith('evorph_csrf='))?.slice(12);
    return value && /^[a-zA-Z0-9_-]{43}$/.test(value) ? value : csrf;
  }
  client.connect = () => connecting ||= (async () => {
    if (page.protocol !== 'https:') return false;
    try {
      const status = await request('/api/research/status');
      client.connected = status.connected === true && status.mode === client.mode;
    } catch { client.connected = false; }
    return client.connected;
  })();
  client.read = async (path, query = {}, signal) => {
    if (!client.connected) throw new ServiceError(503);
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) if (value !== null && value !== undefined) params.set(key, String(value));
    return request(path + (params.size ? '?' + params : ''), { signal });
  };
  client.session = () => client.read('/api/identities/session');
  client.createIdentity = async () => {
    if (!client.connected) throw new ServiceError(503);
    return issued(await request('/api/identities', { method: 'POST', body: {} }));
  };
  client.recoverIdentity = async recovery => {
    if (!client.connected || !/^[a-zA-Z0-9_-]{43}$/.test(recovery)) throw new ServiceError(400);
    return issued(await request('/api/identities/recovery', { method: 'POST', body: {}, headers: { Authorization: 'Bearer ' + recovery } }));
  };
  client.submit = async payload => {
    if (!client.connected) throw new ServiceError(503);
    const proof = csrfToken();
    if (!proof) throw new ServiceError(401);
    const value = await request('/api/challenges', { method: 'POST', body: payload, headers: { 'X-CSRF-Token': proof } });
    if (typeof value.submission_id !== 'string' || !/^[a-zA-Z0-9._:-]{1,120}$/.test(value.submission_id) || typeof value.state !== 'string') throw new ServiceError(502);
    return value;
  };
  return client;
}
