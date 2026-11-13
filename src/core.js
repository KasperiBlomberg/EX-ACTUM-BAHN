// Spread bot core: API client + starter-equivalent desk. Runs in a browser page on the Spread
// origin or in Node 22 (global fetch). No dependencies.
const HOUR = 60, DAY = 1440;

class Api {
  constructor(base, key) { this.base = base; this.key = key; this.calls = 0; }
  async req(method, path, body) {
    for (let attempt = 0; ; attempt++) {
      let r;
      try {
        r = await fetch(this.base + path, {
          method, headers: { Authorization: 'Bearer ' + this.key, 'Content-Type': 'application/json' },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
      } catch (e) {
        if (attempt > 20) throw e;
        await sleep(1000); continue;
      }
      this.calls++;
      if (r.status === 429) { await sleep(+(r.headers.get('Retry-After') || 200)); continue; }
      if (r.status >= 502 && r.status <= 504) { if (attempt > 60) throw new Error('unreachable'); await sleep(2000); continue; }
      if (r.status === 204) return null;
      const j = await r.json();
      if (!r.ok) throw new Error(`${method} ${path} ${r.status} ${JSON.stringify(j)}`);
      return j;
    }
  }
  create(level, seed, board) { const b = { level }; if (seed) b.seed = seed; if (board) b.board = true; return this.req('POST', '/v1/worlds', b); }
  advance(id, body) { return this.req('POST', `/v1/worlds/${id}/advance`, body); }
  act(id, actions) { return this.req('POST', `/v1/worlds/${id}/actions`, actions); }
  snapshot(id) { return this.req('GET', `/v1/worlds/${id}`); }
  events(id, after = -1) { return this.req('GET', `/v1/worlds/${id}/events?after=${after}`); }
  score(id) { return this.req('GET', `/v1/worlds/${id}/score`); }
  roster(id) { return this.req('GET', `/v1/worlds/${id}/drivers`); }
  reveal(id) { return this.req('GET', `/v1/worlds/${id}/reveal`); }
  del(id) { return this.req('DELETE', `/v1/worlds/${id}`); }
  map() { return this.req('GET', '/v1/map'); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
