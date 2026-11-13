// ---------------------------------------------------------------- Node entry point
// Usage (Node 22+):  SPREAD_URL=... SPREAD_KEY=... node spread-bot.mjs [--level 8] [--seed s] [--board] [--final] [--params '{"f0":0.95}']
import fs from 'node:fs';

const args = process.argv.slice(2);
const flag = n => args.includes('--' + n);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 ? args[i + 1] : d; };

const BASE = process.env.SPREAD_URL || 'https://spread.bahnexpress.com';
const KEY = process.env.SPREAD_KEY;
if (!KEY) { console.error('Set SPREAD_KEY'); process.exit(1); }
const api = new Api(BASE, KEY);

async function loadGeo() {
  const map = await api.map();
  globalThis.P = Object.fromEntries(map.places.map(p => [p.id, p]));
  globalThis.ROAD = {}; for (const r of map.roads) ROAD[r.from + '>' + r.to] = r;
  globalThis.hav = (a, b) => { const R = 6371, t = Math.PI / 180; const dl = (b.lat - a.lat) * t, dn = (b.lon - a.lon) * t; const x = Math.sin(dl / 2) ** 2 + Math.cos(a.lat * t) * Math.cos(b.lat * t) * Math.sin(dn / 2) ** 2; return 2 * R * Math.asin(Math.sqrt(x)); };
  globalThis.S = { steep, rising, sleep };
}

function report(label, s) {
  console.log(`${label.padEnd(9)} margin €${(s.marginCents / 100).toFixed(2).padStart(11)}  ${String(s.wonOrders).padStart(4)} orders  ${(100 * s.onTimeShare).toFixed(1)}% on time  ${(100 * s.undeliveredShare).toFixed(1)}% not delivered`);
}

const params = JSON.parse(opt('params', '{}'));
const makePolicy = () => flag('starter') ? {} : makeV4(params);   // --starter: the kit starter's decisions, to check the desk

// several seeds in parallel (max 8 practice slots): --seeds v-1,v-2,... prints one line per seed and the total
async function sweepSeeds(seeds, level) {
  const out = [];
  let i = 0;
  async function worker() {
    while (i < seeds.length) {
      const seed = seeds[i++];
      const w = await api.create(level, seed);
      try {
        const { score, desk } = await playPractice(api, w.worldId, level, makePolicy());
        out.push({ seed, score, stats: desk.policy.stats });
        console.log(`${seed.padEnd(8)} margin €${(score.marginCents / 100).toFixed(0).padStart(7)}  ${String(score.wonOrders).padStart(4)} orders  ${(100 * score.onTimeShare).toFixed(1)}% on time`);
      } catch (e) { console.error(seed, String(e)); try { await api.del(w.worldId); } catch { } }
    }
  }
  await Promise.all(Array.from({ length: Math.min(8, seeds.length) }, worker));
  const tot = out.reduce((s, r) => s + r.score.marginCents, 0) / 100;
  console.log(`TOTAL €${tot.toFixed(0)}  avg €${(tot / out.length).toFixed(0)}  min on time ${(100 * Math.min(...out.map(r => r.score.onTimeShare))).toFixed(1)}%  ranked ${out.filter(r => r.score.ranked).length}/${out.length}`);
}

async function practice() {
  if (opt('seeds')) return sweepSeeds(opt('seeds').split(','), +opt('level', 8));
  const level = +opt('level', 8);
  const w = await api.create(level, opt('seed'), flag('board'));
  console.log(`${flag('board') ? 'Ranking run' : 'Practice world'} ${w.worldId}, level ${level}`);
  const stop = async () => { if (!flag('board')) { try { await api.del(w.worldId); } catch { } } process.exit(1); };
  process.on('SIGINT', stop);
  try {
    const { score } = await playPractice(api, w.worldId, level, makePolicy());
    report('End', score);
    console.log(score.ranked ? 'Ranked.' : 'Not ranked.');
  } catch (e) { console.error(e); await stop(); }
}

// ---------------------------------------------------------------- the final: poll events, act at once
const STATE = opt('state', 'spread-state.json');
async function nextFinal() {
  let said = false;
  for (;;) {
    try {
      const f = await api.req('GET', '/v1/final');
      const snap = await api.snapshot(f.worldId);
      if (snap.now < snap.endsAt) return f;
    } catch (e) { if (!String(e).includes(' 404 ')) console.error(String(e)); }
    if (!said) { console.log('No final is running yet; waiting (it is set 15 min before a test final, 30 min before the final).'); said = true; }
    await sleep(10000);
  }
}

async function playFinal() {
  const f = await nextFinal();
  const worldId = f.worldId;
  console.log(`Final world ${worldId}, starts ${f.startsAt}`);
  const policy = makeV4(params);
  // restore the policy's learnt state after a restart of this program
  let saved = null;
  try { saved = JSON.parse(fs.readFileSync(STATE, 'utf8')); if (saved.worldId !== worldId) saved = null; } catch { }
  const history = await api.events(worldId, -1);
  const snap = await api.snapshot(worldId);
  const desk = new Desk(api, worldId, 8, snap.now, policy);
  await policy.init?.(desk);
  if (saved) policy.setState(saved.policy);
  desk.rejoin(snap, history, !saved);
  await desk.send();
  let after = history.length ? history[history.length - 1].seq : -1;
  let lastDay = Math.floor(desk.now / 1440), lastSave = 0;
  const seen = new Set(history.map(e => e.seq));
  while (desk.now < snap.endsAt) {
    let evs;
    try { evs = await api.events(worldId, after); }
    catch (e) { console.error('events:', String(e)); await sleep(1000); continue; }
    for (const e of evs) {
      if (seen.has(e.seq)) continue; seen.add(e.seq); after = Math.max(after, e.seq);
      desk.now = Math.max(desk.now, e.at);
      try { await desk.handle(e); } catch (err) { console.error('handle', e.kind, String(err)); }
    }
    try { await desk.send(); } catch (e) { console.error('send:', String(e)); }
    if (!evs.length) { await sleep(300); try { desk.now = Math.max(desk.now, (await api.req('GET', '/v1/final')).now); } catch { } }
    if (Date.now() - lastSave > 2000) { lastSave = Date.now(); fs.writeFileSync(STATE, JSON.stringify({ worldId, policy: policy.getState() })); }
    const d = Math.floor(desk.now / 1440);
    if (d > lastDay) { lastDay = d; try { report(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][(d - 1) % 7], await api.score(worldId)); } catch { } }
  }
  report('End', await api.score(worldId));
}

await loadGeo();
if (flag('final')) {
  for (;;) {
    try { await playFinal(); break; }
    catch (e) { console.error('final loop:', String(e), '- retrying in 5 s'); await sleep(5000); }
  }
} else await practice();
