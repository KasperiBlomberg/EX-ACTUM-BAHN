import fs from 'node:fs';
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
// The desk: the starter's bookkeeping and event answers, with the decisions behind a policy.
// policy.quote(desk, request)            -> {kind: cents} | null (null = starter's list price)
// policy.fees(desk, order, cars, urgent) -> [{at, feeCents}]   (null = starter's fees)
// policy.onEvent(desk, event)            -> optional learning hook
const WAKE_ON = ['price_request', 'order_won', 'order_changed', 'order_cancelled', 'visit_failed', 'gig_expired', 'deadline_close'];

class Desk {
  constructor(api, worldId, level, now, policy) {
    Object.assign(this, { api, worldId, level, now, policy });
    this.outbox = [];
    this.requests = {};   // id -> PriceRequest
    this.orders = {};     // id -> Order (won, not cancelled)
    this.legGigs = {};    // order id -> {leg: gig id}
    this.booked = new Set();
    this.posts = {};      // gig id -> post action
    this.urgent = new Set();
    this.gigInfo = {};    // gig id -> {orderId, postedAt, firstFee, cars}
    this.reposts = {};    // order id -> minutes of reposts after expiry
    this.log = [];
  }

  // ------------------------------------------------------------ rejoin (final / restart)
  rejoin(snapshot, history, replayPolicy = true) {
    const posted = {};
    this.booked = new Set(snapshot.gigs.filter(g => ['booked', 'working', 'done'].includes(g.status)).map(g => g.id));
    for (const gig of snapshot.gigs) {
      (posted[gig.stops[0].orderId] ||= []).push(gig);
      if (['open', 'booked', 'working'].includes(gig.status))
        this.carry({ type: 'post', stops: gig.stops, audience: gig.audience, schedule: gig.schedule }, gig.id);
    }
    for (const e of history) if (e.kind === 'price_request') this.requests[e.request.id] = e.request;
    for (const e of history) if (e.kind === 'deadline_close') this.urgent.add(e.orderId);
    for (const o of snapshot.orders) if (this.requests[o.id] && o.status !== 'cancelled') this.orders[o.id] = o;
    if (replayPolicy) for (const e of history) this.policy.onEvent?.(this, e, true);
    for (const order of Object.values(this.orders))
      if (order.status === 'waiting' && !this.gigsOf(order.id).length) this.post(order, undelivered(order));
    for (const e of history) {
      const order = this.orders[e.orderId];
      if (!order) continue;
      if (e.kind === 'visit_failed' && order.deliveredAt[e.leg] == null) {
        const later = (posted[order.id] || []).filter(g => g.postedAt >= e.at && g.id !== e.gigId);
        if (!later.some(g => g.stops.some(s => s.leg === e.leg))) this.retry(order.id, e.leg, e.reason);
      }
    }
    for (const r of snapshot.openRequests) this.quote(r);
  }

  // ------------------------------------------------------------ events
  async handle(e) {
    this.policy.onEvent?.(this, e, false);
    switch (e.kind) {
      case 'price_request': return this.quote(e.request);
      case 'order_won': return this.win(e.order);
      case 'order_changed': return this.change(e.order);
      case 'order_cancelled': delete this.orders[e.orderId]; delete this.legGigs[e.orderId]; return;
      case 'visit_failed': return this.retry(e.orderId, e.leg, e.reason);
      case 'gig_expired': return this.repost(e.gigId);
      case 'deadline_close': return this.raiseFees(e.orderId);
      case 'gig_booked': this.booked.add(e.gigId); return;
      case 'gig_dropped': case 'no_show': this.booked.delete(e.gigId); this.policy.onUnbooked?.(this, e); return;
      case 'delivered': {
        const lg = this.legGigs[e.orderId]; if (lg) delete lg[e.leg];
        if (this.orders[e.orderId]) this.orders[e.orderId].deliveredAt[e.leg] = e.at;
        return;
      }
    }
  }

  quote(request) {
    this.requests[request.id] = request;
    let prices = this.policy.quote ? this.policy.quote(this, request) : null;
    if (!prices) { const f = request.options[0]; prices = { [f.kind]: f.listPriceCents }; }
    this.outbox.push({ type: 'quote', requestId: request.id, prices });
  }

  async win(order) {
    if (this.requests[order.id] && !this.orders[order.id]) {
      this.orders[order.id] = order;
      await this.post(order, undelivered(order));
    }
  }

  async change(order) {
    if (this.orders[order.id]) {
      this.orders[order.id] = order;
      if (await this.withdraw(order.id)) await this.post(order, undelivered(order));
    }
  }

  async repost(gigId) {
    const action = this.posts[gigId];
    if (!action || !action.stops.some(s => (this.legGigs[s.orderId] || {})[s.leg] === gigId)) return;
    for (const s of action.stops) { const lg = this.legGigs[s.orderId]; if (lg) delete lg[s.leg]; }
    const order = this.orders[action.stops[0].orderId];
    if (!order) return;
    (this.reposts[order.id] ||= []).push(this.now);
    if (this.policy.postExtras) {   // recompute the stop windows from now (note hours roll to the next open day)
      const legs = action.stops.filter(s => s.kind === 'pickup').map(s => s.leg).filter(l => order.deliveredAt[l] == null);
      if (legs.length) await this.post(order, legs, { pickupFrom: this.now, manualLicence: action.audience.manualLicence, plates: action.stops.some(s => s.withPlates) });
      return;
    }
    const stops = withoutWindows(action.stops, Infinity);
    this.outbox.push({ ...action, stops, schedule: this.feesFor(order, pickups(action)) });
  }

  async raiseFees(orderId) {
    const order = this.orders[orderId];
    if (!order) return;
    this.urgent.add(orderId);
    await this.sendPostOf(orderId);
    for (const gigId of this.gigsOf(orderId))
      if (!this.booked.has(gigId))
        this.outbox.push({ type: 'reprice', gigId, schedule: this.feesFor(order, pickups(this.posts[gigId])) });
  }

  feesFor(order, cars) {
    const urgent = this.urgent.has(order.id);
    const custom = this.policy.fees ? this.policy.fees(this, order, cars, urgent) : null;
    if (custom) return custom;
    const fee = this.requests[order.id].referenceFeeCents * cars / order.legs.length;
    return urgent ? steep(this.now, fee, this.level) : rising(this.now, fee, this.level);
  }

  async retry(orderId, leg, reason) {
    const order = this.orders[orderId];
    if (!order) return;
    const lg = this.legGigs[orderId]; if (lg) delete lg[leg];
    const notThere = reason === 'not_ready' || reason === 'closed';
    const pickupFrom = notThere ? retryFrom(order, this.requests[orderId], leg, this.now) : 0;
    await this.post(order, [leg], { pickupFrom, manualLicence: reason === 'licence', plates: reason === 'no_plates' });
  }

  async post(order, legs, retry = { pickupFrom: 0, manualLicence: false, plates: false }) {
    const extra = this.policy.postExtras ? this.policy.postExtras(this, order, legs, retry) : {};
    const audience = { experienced: order.legs.some(l => l.car.premium), manualLicence: retry.manualLicence || !!extra.manual };
    const r2 = { ...retry, plates: retry.plates || !!extra.plates };
    // a stop window that ends within 2 hours cannot be met by a new booking: drop it, so the order's own
    // window applies (and, once that has ended, the stop runs to the end of the world)
    let stops = withoutWindows(stopsFor(order, legs, this.requests[order.id].note, r2, extra), this.now + 120);
    const churn = (this.reposts[order.id] || []).filter(t => t > this.now - 120).length;
    if (churn >= 3) stops = withoutWindows(stops, Infinity);   // the windows keep expiring: post without them
    this.outbox.push({ type: 'post', stops, audience, schedule: this.feesFor(order, legs.length) });
    if (legs.length === 1) return;
    await this.send();
    if (!this.gigsOf(order.id).length) for (const leg of legs) await this.post(order, [leg], retry);
  }

  gigsOf(orderId) { return [...new Set(Object.values(this.legGigs[orderId] || {}))]; }

  carry(action, gigId) {
    for (const s of action.stops) (this.legGigs[s.orderId] ||= {})[s.leg] = gigId;
    this.posts[gigId] = action;
    if (!this.gigInfo[gigId]) this.gigInfo[gigId] = { orderId: action.stops[0].orderId, postedAt: this.now, firstFee: action.schedule[0].feeCents, cars: pickups(action) };
  }

  async withdraw(orderId) {
    await this.sendPostOf(orderId);
    const gigs = this.gigsOf(orderId);
    if (gigs.some(g => this.booked.has(g))) return false;
    delete this.legGigs[orderId];
    for (const g of gigs) this.outbox.push({ type: 'withdraw', gigId: g });
    return true;
  }

  async sendPostOf(orderId) {
    if (this.outbox.some(a => a.type === 'post' && a.stops[0].orderId === orderId)) await this.send();
  }

  async send() {
    const actions = this.outbox; this.outbox = [];
    for (let i = 0; i < actions.length; i += 50) {
      const chunk = actions.slice(i, i + 50);
      this.readResults(chunk, await this.api.act(this.worldId, chunk));
    }
  }

  readResults(actions, results) {
    actions.forEach((a, i) => {
      const r = results[i];
      if (a.type === 'post' && r.ok === true) this.carry(a, r.id);
      if (r.ok === false) this.log.push([this.now, a.type, r.reason]);
    });
  }
}

// ------------------------------------------------------------ helpers (ported from the starter)
function retryFrom(order, request, leg, now) {
  const o = request.options.find(o => o.kind === order.option);
  const promised = o ? o.deadline : order.deadline;
  const delay = Math.max(0, order.deadline - promised);
  return Math.max(now + 2 * HOUR, order.legs[leg].pickup.window.from + delay);
}
const undelivered = order => order.legs.map((_, i) => i).filter(i => order.deliveredAt[i] == null);
const pickups = action => action.stops.filter(s => s.kind === 'pickup').length;
function withoutWindows(stops, before) {
  return stops.map(s => ('window' in s && s.window.to < before) ? Object.fromEntries(Object.entries(s).filter(([k]) => k !== 'window')) : s);
}
const roundHalfUp = x => Math.floor(x + 0.5);
function rising(now, fee, level) {
  const n = level === 1 ? 1 : 6;
  return Array.from({ length: n }, (_, k) => ({ at: now + k * HOUR, feeCents: Math.max(1, roundHalfUp(fee * (1 + k / 10))) }));
}
function steep(now, fee, level) {
  const n = level === 1 ? 1 : 5;
  return Array.from({ length: n }, (_, k) => ({ at: now + k * 30, feeCents: Math.max(1, roundHalfUp(fee * (1.5 + k / 4))) }));
}
function stopsFor(order, legs, note, retry, extra = {}) {
  const stops = [];
  for (const leg of legs) {
    const { car, pickup, delivery } = order.legs[leg];
    let noted = windowFromNote(note, pickup.window, 'pickup');
    if (extra.pickupWindow) noted = extra.pickupWindow(leg, noted, pickup.window);
    const start = Math.max(noted.from, retry.pickupFrom);
    const pickupWindow = { from: start, to: Math.max(noted.to, start) };
    const deliveryBase = { from: delivery.window.from, to: Math.max(delivery.window.from, order.deadline) };
    let deliveryWindow = windowFromNote(note, deliveryBase, 'delivery');
    if (extra.deliveryWindow) deliveryWindow = extra.deliveryWindow(leg, deliveryWindow, deliveryBase);
    const lowBattery = car.electric && car.chargePercent != null && car.chargePercent < 30;
    const first = { orderId: order.id, leg, kind: 'pickup', withPlates: retry.plates, withCharging: lowBattery };
    const second = { orderId: order.id, leg, kind: 'delivery' };
    stops.push(narrower(first, pickupWindow, pickup.window), narrower(second, deliveryWindow, deliveryBase));
  }
  return stops;
}
const narrower = (stop, w, base) => (w.from > base.from || w.to < base.to) ? { ...stop, window: w } : stop;

const AFTER = /jälkeen|alkaen|aikaisintaan|\bafter\b|\bfrom\b|earliest|not before|ei ennen|valmis|\bready\b|efter|tidigast|inte före|klar/;
const BEFORE = /ennen|mennessä|viim|\bbefore\b|\bby\b|latest|före|senast/;
const DELIVERY = /toimit|perillä|perille|vastaanot|kohde|deliver|arrive|destination|receiv|leverera|leverans|framme|mottag/;
const CLOCK = /(?:klo|kl\.?)\s*(\d{1,2})(?:[.:](\d{2}))?|\b(\d{1,2})[.:](\d{2})\b/g;
const RANGE = /\b(\d{1,2})(?:[.:](\d{2}))?\s*[-–]\s*(\d{1,2})(?:[.:](\d{2}))?\b/g;
function windowFromNote(note, window, stop) {
  if (!note) return window;
  let text = note.text.toLowerCase();
  if (DELIVERY.test(text) !== (stop === 'delivery')) return window;
  const minutes = (h, m) => (h > 24 || m > 59) ? null : h * HOUR + m;
  let start = window.from, end = window.to;
  for (const m of text.matchAll(RANGE)) {
    const opens = minutes(+m[1], +(m[2] || 0)), closes = minutes(+m[3], +(m[4] || 0));
    if (opens == null || closes == null || opens >= closes) continue;
    start = Math.max(start, Math.floor(start / DAY) * DAY + opens);
    end = Math.min(end, Math.floor(end / DAY) * DAY + closes);
  }
  text = text.replace(RANGE, ' ');
  for (const m of text.matchAll(CLOCK)) {
    const mm = minutes(+(m[1] || m[3]), +(m[2] || m[4] || 0));
    if (mm == null) continue;
    const time = Math.floor(window.from / DAY) * DAY + mm;
    const around = text.slice(Math.max(0, m.index - 16), m.index + m[0].length + 16);
    if (AFTER.test(around)) start = Math.max(start, time);
    else if (BEFORE.test(around)) end = Math.min(end, time);
  }
  return start <= end ? { from: start, to: end } : window;
}

// ------------------------------------------------------------ practice loop
async function playPractice(api, worldId, level, policy, opts = {}) {
  const snap = await api.snapshot(worldId);
  const desk = new Desk(api, worldId, level, snap.now, policy);
  await policy.init?.(desk);
  const endsAt = snap.endsAt;
  const events = [];
  while (desk.now < endsAt) {
    const midnight = (Math.floor(desk.now / DAY) + 1) * DAY;
    if (desk.outbox.length > 50) { const keep = desk.outbox.splice(desk.outbox.length - 50); await desk.send(); desk.outbox = keep; }
    const actions = desk.outbox; desk.outbox = [];
    const wake = policy.nextWake ? policy.nextWake(desk) : Infinity;
    const body = { untilEvent: policy.wakeOn || WAKE_ON, max: Math.min(endsAt, midnight, Math.max(desk.now + 1, wake)) };
    if (actions.length) body.actions = actions;
    const step = await api.advance(worldId, body);
    if (body.actions) desk.readResults(body.actions, step.results);
    desk.now = step.now;
    for (const e of step.events) { if (opts.keepEvents) events.push(e); await desk.handle(e); }
    await policy.tick?.(desk);
  }
  const score = await api.score(worldId);
  return { desk, score, events };
}

// ------------------------------------------------------------ browser runner helpers
if (typeof window !== 'undefined') {
  window.api = new Api(location.origin, window.KEY);
  window.runs = window.runs || {};
  window.startRun = (name, level, seed, policy, opts = {}) => {
    const rec = window.runs[name] = { status: 'running', t0: Date.now() };
    (async () => {
      const w = await api.create(level, seed); rec.worldId = w.worldId;
      try { const r = await playPractice(api, w.worldId, level, policy, opts); Object.assign(rec, r, { status: 'done', secs: (Date.now() - rec.t0) / 1000 }); }
      catch (e) { rec.status = 'error'; rec.error = String(e.stack || e); try { await api.del(w.worldId); } catch (_) { } }
    })();
    return name;
  };
}
// Dealer-note reader (fi/sv/en), JS port of spreadcore/notes.py. Follows RULES.md:
// days count from arrival; weekday names mean 2..6 days ahead; digits are 24h unless am/pm;
// hours in words are working hours 9..17 ("two" = 14:00, "half three" = 14:30); corrections win;
// denied times ("not 3pm") are ignored.
const N_DAY = 1440;
const WEEKDAYS_RX = [
  ['maanantai\\w*|måndag\\w*|monday|mån|mon', 0], ['tiistai\\w*|tisdag\\w*|tuesday|tues|tue|tis', 1],
  ['keskiviik\\w*|onsdag\\w*|wednesday|wed|ons', 2], ['torstai\\w*|trostai\\w*|torsdag\\w*|thursday|thurs|thur|thu|tors', 3],
  ['perjantai\\w*|fredag\\w*|friday|fri|fre', 4], ['lauantai\\w*|lördag\\w*|saturday|sat|lör', 5],
  ['sunnuntai\\w*|söndag\\w*|sunday|sun|sön', 6],
];
const FI_SHORT = { ma: 0, ti: 1, ke: 2, to: 3, pe: 4, la: 5, su: 6 };
const REL_DAYS = [
  ['day after tomorrow|ylihuomen\\w*|i\\s?övermorgon|overmorrow', 2],
  ['huo?m+en+a\\p{L}*|hom+en+a\\p{L}*|huomena|i\\s?morgon|tomorrow|tomorow|tommorow|tmrw|tmrrw|tmr|2moro', 1],
  ['tänään|idag|i dag|today|tdy', 0],
];
const NUM_WORDS = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
  yksi: 1, yhdeltä: 1, yhteen: 1, kaksi: 2, kahdelta: 2, kahteen: 2, kolme: 3, kolmelta: 3, kolmeen: 3, neljä: 4, neljältä: 4, neljään: 4,
  viisi: 5, viideltä: 5, viiteen: 5, yhdeksän: 9, yhdeksältä: 9, kymmenen: 10, kymmeneltä: 10, kymmeneen: 10, yksitoista: 11,
  yhdeltätoista: 11, kaksitoista: 12, kahdeltatoista: 12, yhdestä: 1, kahdesta: 2, kolmesta: 3, neljästä: 4, viidestä: 5, kuusi: 6, kuudelta: 6, kuuteen: 6, kuudesta: 6, seitsemän: 7, seitsemältä: 7, seitsemään: 7, seitsemästä: 7, kahdeksan: 8, kahdeksalta: 8, kahdeksaan: 8, kahdeksasta: 8, yhdeksään: 9, yhdeksästä: 9, kymmenestä: 10, yhdestätoista: 11, kahdestatoista: 12, sju: 7, åtta: 8, ett: 1, två: 2, tre: 3, fyra: 4, fem: 5, nio: 9, tio: 10, elva: 11, tolv: 12 };
const WB = '(?<![\\p{L}\\d])', WE = '(?![\\p{L}\\d])';     // unicode-aware word boundaries
const TIME_CUES = 'klo|kl\\.?|klockan|at|@|by|before|after|until|till|til|from|ennen|jälkeen|asti|saakka|mennessä|alkaen|lähtien|viimeistään|aikaisintaan|före|efter|innan|från|senast|tidigast|not|ei|inte|ej|but|vaan|utan|men|or|tai|eller|around|noin|ca\\.?|cirka|ready|valmis|klar';
const DAY_CUES = 'huo?m+en+a|tänään|ylihuomenna|tomorrow|today|idag|imorgon|morgon|maanantaina|tiistaina|keskiviikkona|torstaina|perjantaina|på måndag|på tisdag|på onsdag|på torsdag|på fredag|monday|tuesday|wednesday|thursday|friday|vasta|först|alkaen';
const CUE_BEFORE = new RegExp(`${WB}(?:${TIME_CUES}|${DAY_CUES})\\s*$`, 'u');
const UNITS = /^\s*(?:km|%|€|eur|e\b|autoa|auto|cars?|bilar|bil|kpl|st\b|min|h\b|tuntia|timmar|hours?|pv|days?|päivä)/u;
const NEG = new RegExp(`${WB}(?:not|ei|inte|ej|no)\\s+(?:klo\\s+|kl\\.?\\s+|klockan\\s+|at\\s+|on\\s+|på\\s+)?$`, 'u');
const K = {
  payment: 'maksu\\p{L}*|maksa\\p{L}*|maksettu|payment|paid|pay(?![\\p{L}])|betalning|betald|betalat|invoice|lasku',
  ready: 'valmi\\p{L}*|ready|klar(?![\\p{L}])|färdig|available|saatavilla|noudettavissa|vapautuu|frigörs',
  pickup: 'nouto\\p{L}*|nouda\\p{L}*|noudet\\p{L}*|haku|haett?av\\p{L}*|hakea|pick\\s?-?up|pick\\s+up|collect\\p{L}*|hämt\\p{L}*|avhämt\\p{L}*|luovutus\\p{L}*|överlämn\\p{L}*|handover|hand-over',
  site: 'vastaanot\\p{L}*|receiv\\p{L}*|reception|deliveries\\s+(?:until|till)|mottag\\p{L}*|open(?![\\p{L}])|auki|öppe\\p{L}*|close\\p{L}*|closing|suljet\\p{L}*|sulke\\p{L}*|sulkeu\\p{L}*|stäng\\p{L}*|kiinni',
  deadline: 'perill\\p{L}*|perille|destination|framme|deliver(?:ed|y)?\\s+by|must\\s+be|oltava|pitää\\s+olla|täytyy|needed|tarvitaan|behövs|deadline|latest|viimeistään|senast|mennessä|arrive\\p{L}*|saapu\\p{L}*|toimit\\p{L}*|lever\\p{L}*',
};
const DEST = /kohde|vastaanot|receiv|mottag|destination|perill|perille|leverans|framme|määränpää/u;
const KRX = Object.fromEntries(Object.entries(K).map(([k, v]) => [k, new RegExp(v, 'gu')]));
const REL_BEFORE = /before|until|till|by|ennen|asti|saakka|mennessä|viimeistään|före|innan|senast|latest/gu;
const REL_AFTER = /after|from|jälkeen|alkaen|lähtien|aikaisintaan|earliest|efter|från|tidigast|not\s+before|ei\s+ennen|inte\s+före|ej\s+före|först/gu;
const F_MANUAL = /manuaali\p{L}*|käsivaiht\p{L}*|manual(?![\p{L}])|manuell\p{L}*|stick\s?shift|kopplingspedal/u;
const F_AUTO_NEG = /(?:not|ei|inte|ej)\s+(?:a\s+)?(?:manuaali|manual|manuell)/u;
const F_PLATES = /no\s+(?:licen[cs]e\s+)?pla+tes|trade\s+pla+tes|pla+tes\s+removed|no\s+reg(?:istration)?\s+pla+tes|without\s+plates|plates?\s+missing|needs?\s+(?:transfer\s+)?plates|unregistered|ei\s+kilpi\p{L}*|ilman\s+kilpi\p{L}*|kilvet\p{L}*|kilpiä\s+ei|kilvet\s+puuttu\p{L}*|rekisteröimät\p{L}*|siirtokilp\p{L}*|koekilp\p{L}*|koenumer\p{L}*|inga\s+skyltar|utan\s+skyltar|saknar\s+skyltar|avställd|provskylt\p{L}*|transfer\s+plates/u;
const F_SUMMER = /kesärenkailla|kesärenkaat\s+alla|kesärenkaat\s+vielä|vielä\s+kesärenkaat|(?:on|still\s+(?:has|on|with))\s+summer\s+t[iy]res|summer\s+t[iy]res\s+(?:on|still|fitted)|på\s+sommardäck|har\s+sommardäck|sommardäck\s+på|fortfarande\s+sommardäck/u;
const DIGIT_RE = /(?<![\d.:])(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?:\s*-\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?)?(?!\d)/gu;
const WORD_RE = new RegExp(`${WB}(?:(half|puoli|halv)\\s+)?(${Object.keys(NUM_WORDS).sort((a, b) => b.length - a.length).join('|')})${WE}`, 'gu');

const wordHour = n => (n >= 9 && n <= 12) ? n : n + 12;
const ampm = (h, a) => { if (!a) return h; a = a.replace(/\./g, ''); if (a === 'am') return h === 12 ? 0 : h; return h === 12 ? h : h + 12; };
function lastPos(rx, s) { let pos = -1; rx.lastIndex = 0; for (const m of s.matchAll(rx)) pos = m.index; return pos; }

function findTimes(s) {
  const out = [];
  for (const m of s.matchAll(DIGIT_RE)) {
    const [_, h0, mi, ap, h2, mi2, ap2] = m; const h = +h0; const prefix = s.slice(0, m.index);
    const cue = CUE_BEFORE.test(prefix.slice(-25));
    if (UNITS.test(s.slice(m.index + m[0].length)) && !mi && !ap) continue;
    if (!(mi || ap || cue || h2)) continue;
    if (h > 23 || (mi && +mi > 59)) continue;
    const tod = ampm(h, ap || ap2) * 60 + (mi ? +mi : 0);
    let tod2 = null; if (h2 != null) { if (+h2 > 23) continue; tod2 = ampm(+h2, ap2) * 60 + (mi2 ? +mi2 : 0); }
    out.push({ start: m.index, end: m.index + m[0].length, tod, tod2, denied: NEG.test(prefix) });
  }
  for (const m of s.matchAll(WORD_RE)) {
    const [_, half, word] = m; const prefix = s.slice(0, m.index);
    if (!half && !CUE_BEFORE.test(prefix.slice(-25))) continue;
    if (UNITS.test(s.slice(m.index + m[0].length))) continue;
    const n = NUM_WORDS[word];
    const tod = half ? (wordHour(n) - 1) * 60 + 30 : wordHour(n) * 60;
    out.push({ start: m.index, end: m.index + m[0].length, tod, tod2: null, denied: NEG.test(prefix) });
  }
  return out.sort((a, b) => a.start - b.start);
}
function findDays(s, arrivalDay) {
  const found = [], taken = [];
  const free = i => !taken.some(([a, b]) => a <= i && i < b);
  const wAbs = w => { let d = ((w - arrivalDay % 7) % 7 + 7) % 7; if (d < 2) d += 7; return arrivalDay + d; };
  for (const [rx, delta] of REL_DAYS) for (const m of s.matchAll(new RegExp(`${WB}(?:${rx})`, 'gu'))) { if (!free(m.index)) continue; taken.push([m.index, m.index + m[0].length]); found.push([m.index, arrivalDay + delta, NEG.test(s.slice(0, m.index))]); }
  for (const [rx, w] of WEEKDAYS_RX) for (const m of s.matchAll(new RegExp(`${WB}(?:${rx})${WE}\\.?`, 'gu'))) { if (!free(m.index)) continue; taken.push([m.index, m.index + m[0].length]); found.push([m.index, wAbs(w), NEG.test(s.slice(0, m.index))]); }
  for (const m of s.matchAll(/(?<![\p{L}\d])(ma|ti|ke|to|pe|la|su)\.?(?=\s*(?:klo|kl\b|$|,)|\.\s*\d)/gu)) { if (!free(m.index)) continue; found.push([m.index, wAbs(FI_SHORT[m[1]]), NEG.test(s.slice(0, m.index))]); }
  return found.sort((a, b) => a[0] - b[0]);
}
function sentences(t) {
  t = t.replace(/(\d)\.(\d)/g, '$1§$2').replace(/(?<![\p{L}\d])(ma|ti|ke|to|pe|la|su)\./gu, '$1¤');
  return t.split(/[.!?;\n]+/).map(p => p.replace(/§/g, '.').replace(/¤/g, '.').trim()).filter(Boolean);
}
function classify(sentence, tm) {
  const pre = sentence.slice(0, tm.start), post = sentence.slice(tm.end, tm.end + 20), window = pre + ' ' + post;
  let obj = null, best = -1;
  for (const [name, rx] of Object.entries(KRX)) {
    let p = lastPos(rx, pre);
    if (p < 0) p = lastPos(rx, post) < 0 ? -1 : 0;
    if (p > best) { best = p; obj = name; }
  }
  KRX.payment.lastIndex = 0; if (KRX.payment.test(sentence)) obj = 'payment';
  const pb = lastPos(REL_BEFORE, window), pa = lastPos(REL_AFTER, window);
  const rel = (pa >= 0 && pa >= pb) ? 'after' : pb >= 0 ? 'before' : null;
  return [obj, rel];
}
function parseNote(text, arrivalMinute) {
  const f = { ready_at: null, release_at: null, deadline_at: null, pickup_open: null, pickup_close: null, delivery_close: null, manual: false, plates: false, summer: false, trace: [] };
  if (!text) return f;
  const t = text.toLowerCase().replace(/[–—]/g, '-').replace(/’/g, "'").replace(/\s+/g, ' ')
    .replace(/(?<![\p{L}])viim\./gu, 'viimeistään').replace(/(?<![\p{L}])huom\./gu, 'huom').replace(/(?<![\p{L}])n\.(?=\s*\d)/gu, 'noin').replace(/(?<![\p{L}])esim\./gu, 'esim').replace(/klo\./g, 'klo');
  f.manual = F_MANUAL.test(t) && !F_AUTO_NEG.test(t); f.plates = F_PLATES.test(t); f.summer = F_SUMMER.test(t);
  const aDay = Math.floor(arrivalMinute / N_DAY);
  let last = null;
  for (const s of sentences(t)) {
    const days = findDays(s, aDay).filter(d => !d[2]);
    const times = findTimes(s);
    for (const tm of times) {
      if (tm.denied) continue;
      const before = days.filter(d => d[0] <= tm.start), after = days.filter(d => d[0] > tm.start);
      const explicitDay = before.length ? before[before.length - 1][1] : after.length ? after[0][1] : null;
      const [obj, rel] = classify(s, tm);
      const dest = DEST.test(s);
      let fld;
      if (tm.tod2 != null) fld = dest ? 'delivery_close' : 'pickup_close';              // a range is opening hours
      else if (obj === 'site') fld = dest ? 'delivery_close' : 'pickup_close';          // "liike kiinni klo 16.30"
      else fld = obj === 'payment' ? 'release_at' : obj === 'ready' ? 'ready_at' : obj === 'pickup' ? (rel === 'after' ? 'ready_at' : 'pickup_close')
        : obj === 'deadline' ? 'deadline_at' : rel === 'after' ? 'ready_at' : last;
      if (!fld) continue;
      const inherited = explicitDay == null && fld === last && f[fld] != null && fld.endsWith('_at') ? Math.floor(f[fld] / N_DAY) : null;
      const day = explicitDay ?? inherited ?? aDay;
      const abs = day * N_DAY + tm.tod;
      if (fld === 'pickup_close' || fld === 'delivery_close') {
        if (tm.tod2 != null) { if (fld === 'pickup_close') f.pickup_open = tm.tod; f[fld] = tm.tod2; } else f[fld] = tm.tod;
      } else f[fld] = abs;
      f.trace.push(`${fld}<-${s}`); last = fld;
    }
    if (!times.length && days.length) {
      const d = days[days.length - 1][1];
      KRX.payment.lastIndex = 0; KRX.ready.lastIndex = 0; KRX.deadline.lastIndex = 0;
      if (KRX.ready.test(s) || KRX.payment.test(s)) { f.ready_at = d * N_DAY + 8 * 60; last = 'ready_at'; }
      else if (KRX.deadline.test(s)) { f.deadline_at = d * N_DAY + 20 * 60; last = 'deadline_at'; }
    }
  }
  return f;
}
// v2 policy: v1's selection + live Bayesian competitor model (world level x day factor, price-war prior)
// + dealer trust + expected-margin-optimal quotes.
function makeCompModel(o = {}) {
  const c = Object.assign({ lo: 1.05, hi: 1.95, nL: 37, dLo: -0.40, dHi: 0.35, nD: 31, daySd: 0.12, war: 0.15, k: 11, temper: 0.8 }, o);
  const L = Array.from({ length: c.nL }, (_, i) => Math.log(c.lo) + (Math.log(c.hi) - Math.log(c.lo)) * i / (c.nL - 1));
  const D = Array.from({ length: c.nD }, (_, i) => c.dLo + (c.dHi - c.dLo) * i / (c.nD - 1));
  let logL = L.map(() => 0), logJ = null, day = null, warSeen = false;
  const dayPrior = () => { const w = warSeen ? 0.02 : c.war; return D.map(d => Math.log((1 - w) * Math.exp(-0.5 * (d / c.daySd) ** 2) / c.daySd + w * Math.exp(-0.5 * ((d + 0.223) / 0.04) ** 2) / 0.04)); };
  const norm = a => { const m = Math.max(...a.flat()); return a.map(r => r.map(v => v - m)); };
  function at(minute) {
    const d = Math.floor(minute / 1440);
    if (d === day) return;
    if (logJ) {
      // carry the world level; note whether today looked like the price war
      const dm = dayMarginal();
      const war = D.reduce((s, dv, j) => s + (dv < -0.15 ? dm[j] : 0), 0) > 0.6;
      if (war) warSeen = true;   // a price-war day says little about the usual level: keep the old one
      else logL = logJ.map(r => { const m = Math.max(...r); return c.temper * (m + Math.log(r.reduce((s, v) => s + Math.exp(v - m), 0))); });
    }
    day = d; const dp = dayPrior();
    logJ = norm(L.map((_, i) => D.map((_, j) => logL[i] + dp[j])));
  }
  function weights() { let s = 0; const w = logJ.map(r => r.map(v => { const e = Math.exp(v); s += e; return e; })); return w.map(r => r.map(v => v / s)); }
  function dayMarginal() { const w = weights(); return D.map((_, j) => w.reduce((s, r) => s + r[j], 0)); }
  const sig = x => 1 / (1 + Math.exp(-x));
  return {
    observe(minute, x, won) {
      at(minute);
      logJ = norm(logJ.map((r, i) => r.map((v, j) => { const p = Math.min(1 - 1e-6, Math.max(1e-6, sig(c.k * (L[i] + D[j] - x)))); return v + Math.log(won ? p : 1 - p); })));
    },
    pWin(minute, xs) { // xs: array of log(price/list); returns P(win) for each
      at(minute); const w = weights();
      return xs.map(x => { let p = 0; for (let i = 0; i < L.length; i++) for (let j = 0; j < D.length; j++) if (w[i][j] > 1e-7) p += w[i][j] * sig(c.k * (L[i] + D[j] - x)); return p; });
    },
    getState() { return { logL, logJ, day, warSeen }; },
    setState(st) { ({ logL, logJ, day, warSeen } = st); },
    summary(minute) {
      at(minute); const w = weights(); let el = 0, ed = 0;
      w.forEach((r, i) => r.forEach((v, j) => { el += v * L[i]; ed += v * D[j]; }));
      const dm = dayMarginal();
      return { level: +Math.exp(el).toFixed(3), day: +Math.exp(ed).toFixed(3), pWar: +D.reduce((s, dv, j) => s + (dv < -0.15 ? dm[j] : 0), 0).toFixed(2), warSeen };
    },
  };
}

function makeV2(params = {}) {
  const p = Object.assign({
    maxKm: 400, minSlack: 480, minSlackPriv: 600, tradeinPriv: false,
    f0: 1.0, f1: 1.30, f1Lead: 360,
    costMult: 1.08,     // expected fee paid / ref for a car we take (incl. occasional raises)
    lateLoss: 1.2,      // expected loss of a late order, in ref units (refund + steep fees + trust)
    pLate: 0.04,
    uMin: 1.0, uMax: 2.3, minEv: 0,
    k: 11, war: 0.15, daySd: 0.12,
    declineMult: 5,
  }, params);
  const road = (a, b) => ROAD[a + '>' + b];
  const comp = makeCompModel({ k: p.k, war: p.war, daySd: p.daySd });
  const trust = {};            // dealer -> on-time count (0..25)
  const quoted = {};           // request id -> x (log price/list, trust-adjusted)
  const grid = []; for (let u = p.uMin; u <= p.uMax + 1e-9; u += 0.02) grid.push(u);
  function legMinutes(req) { let m = 0; req.legs.forEach((l, i) => { m += road(l.pickup.placeId, l.delivery.placeId).minutes; if (i > 0) m += road(req.legs[i - 1].delivery.placeId, l.pickup.placeId).minutes; }); return m; }
  function effDeadline(req, deadline) { const h = req.legs[req.legs.length - 1].delivery.hours; if (!h) return deadline; return Math.min(deadline, Math.floor(deadline / 1440) * 1440 + h.closes); }
  function slackOf(req, opt) {
    const start = Math.max(req.respondBy, req.legs[0].pickup.window.from);
    let s = effDeadline(req, opt.deadline) - legMinutes(req) - 30 - start;
    const pw = req.legs[0].pickup; if (pw.privateAddress) s = Math.min(s, pw.window.to - start);
    return s;
  }
  const pol = {
    params: p, comp, trust, stats: { quoted: 0, declined: 0, evNeg: 0 },
    quote(desk, req) {
      const km = Math.max(...req.legs.map(l => road(l.pickup.placeId, l.delivery.placeId).km));
      const priv = req.legs.some(l => l.pickup.privateAddress || l.delivery.privateAddress);
      const okOpts = req.options.filter(o => km <= p.maxKm && slackOf(req, o) >= (priv ? p.minSlackPriv : p.minSlack) && !(req.legs.length > 1 && priv && !p.tradeinPriv));
      const prices = {}; for (const o of req.options) prices[o.kind] = Math.round(o.listPriceCents * p.declineMult / 100) * 100;
      if (!okOpts.length) { this.stats.declined++; return prices; }
      // one markup u for the accepted options, chosen on the first accepted option's list price
      const o = okOpts[0], list = o.listPriceCents, ref = req.referenceFeeCents;
      const t = 1 + 0.01 * (trust[req.dealerId] || 0);
      const xs = grid.map(u => Math.log(u / t));
      const pw = comp.pWin(req.arrivedAt, xs);
      const cost = p.costMult * ref + p.pLate * p.lateLoss * ref;
      let best = -1, bu = null;
      grid.forEach((u, i) => { const ev = pw[i] * (u * list - cost); if (ev > best) { best = ev; bu = u; } });
      if (best <= p.minEv) { this.stats.evNeg++; return prices; }
      for (const oo of okOpts) prices[oo.kind] = Math.round(oo.listPriceCents * bu / 100) * 100;
      quoted[req.id] = Math.log(bu / t);
      this.stats.quoted++;
      return prices;
    },
    onEvent(desk, e, replay) {
      if (e.kind === 'order_won' && e.order.id in quoted) { comp.observe(desk.requests[e.order.id]?.arrivedAt ?? e.at, quoted[e.order.id], true); delete quoted[e.order.id]; }
      else if (e.kind === 'order_lost' && e.requestId in quoted) { comp.observe(desk.requests[e.requestId]?.arrivedAt ?? e.at, quoted[e.requestId], false); delete quoted[e.requestId]; }
      else if (e.kind === 'delivered') {
        const o = desk.orders[e.orderId]; if (!o) return;
        const all = o.deliveredAt.map((d, i) => i === e.leg ? e.at : d);
        if (all.every(d => d != null)) {
          const onTime = all.every(d => d <= o.deadline);
          const t = trust[o.dealerId] || 0;
          trust[o.dealerId] = onTime ? Math.min(25, t + 1) : Math.floor(t / 2);
        }
      }
    },
    fees(desk, order, cars, urgent) {
      const ref = desk.requests[order.id].referenceFeeCents * cars / order.legs.length;
      if (urgent) return S.steep(desk.now, ref, 8);
      return [{ at: desk.now, feeCents: Math.round(ref * p.f0) },
              { at: Math.max(desk.now + 1, order.lastPickupAt - p.f1Lead), feeCents: Math.round(ref * p.f1) }];
    },
  };
  return pol;
}
// v3 = v2 + per-pickup-town booking success (learnt live, prior from the roster) priced into quotes
// and opening fees + a global lateness shadow price + earlier two-step escalation.
// Needs makeCompModel (policy_v2.js), ROAD, P, hav, S.
function makeV4(params = {}) {
  const p = Object.assign({
    maxKm: 450, minSlack: 360, minSlackPriv: 600, tradeinPriv: false,
    f0: 0.92, f0Risk: 0.30,          // opening fee = (f0 + f0Risk * pFail_town) * ref
    f1: 1.25, f1Lead: 420, f2: 1.5, f2Lead: 270,
    costMult: 1.0,                   // fee / ref paid on a smooth booking
    failRef: 1.0, failPrice: 0.15,   // extra cost when a car gets no driver before the warning
    priorW: 4,
    targetLate: 0.06, shadowK: 25,
    earlyFail: 240, backlogB: 10, warnW: 0.4, pendW: 0.15, hardLate: 0.075, hardPFail: 0.15,
    riskSlack: [[480, 0.20], [720, 0.07], [1e9, 0.02]], riskKm: [[200, 0], [300, 0.04], [1e9, 0.14]], optRiskGap: 0.08,   // structural risk by slack and distance     // unbooked for 4 h counts as a failure; own unbooked gigs in a town raise its risk
    uMin: 1.0, uMax: 2.3, minEv: 0, k: 11, war: 0.15, daySd: 0.12, declineMult: 5,
  }, params);
  const road = (a, b) => ROAD[a + '>' + b];
  const comp = makeCompModel({ k: p.k, war: p.war, daySd: p.daySd });
  const trust = {}, quoted = {}, town = {};          // town: place -> {ok, fail}
  const orderTown = {}, resolved = {};               // order -> pickup place; order -> 'ok'|'fail'
  let homes = null, lateOrders = 0, doneOrders = 0, lastNow = 0;
  const openWarned = new Set(), openOrders = new Set();
  const grid = []; for (let u = p.uMin; u <= p.uMax + 1e-9; u += 0.02) grid.push(u);
  const legMinutes = req => { let m = 0; req.legs.forEach((l, i) => { m += road(l.pickup.placeId, l.delivery.placeId).minutes; if (i > 0) m += road(req.legs[i - 1].delivery.placeId, l.pickup.placeId).minutes; }); return m; };
  const effDeadline = (req, d) => { const h = req.legs[req.legs.length - 1].delivery.hours; return h ? Math.min(d, Math.floor(d / 1440) * 1440 + h.closes) : d; };
  const notes = {};                                   // request id -> parsed note
  const noteOf = req => notes[req.id] ||= parseNote(req.note ? req.note.text : null, req.arrivedAt);
  function slackOf(req, opt) {
    const f = noteOf(req);
    const stated = req.legs[0].pickup.window.from;
    const earliest = Math.max(stated, f.ready_at || 0, f.release_at || 0);
    const shift = Math.max(0, Math.floor(earliest / 1440) - Math.floor(stated / 1440)) * 1440;  // a later ready day moves the deadline
    let deadline = effDeadline(req, opt.deadline + shift);
    if (f.deadline_at != null) deadline = Math.min(deadline, f.deadline_at);
    if (f.delivery_close != null) deadline = Math.min(deadline, Math.floor(deadline / 1440) * 1440 + f.delivery_close);
    const start = Math.max(req.respondBy, earliest);
    let last = deadline - legMinutes(req) - 30;
    if (f.pickup_close != null) { const dayClose = Math.floor(last / 1440) * 1440 + f.pickup_close; if (dayClose < last) last = dayClose; }
    let s = last - start;
    const pw = req.legs[0].pickup; if (pw.privateAddress) s = Math.min(s, pw.window.to - start);
    return s;
  }
  // stop windows from the note, inside the order's windows; one day of pickup hours at a time
  function pickupWindowFor(order, leg, base, retryFrom) {
    const f = noteOf(order.__req);
    let from = Math.max(base.from, f.ready_at || 0, f.release_at || 0, retryFrom || 0);
    let to = base.to;
    if (f.pickup_close != null) {
      const day = Math.floor(from / 1440);
      if (f.pickup_open != null) from = Math.max(from, day * 1440 + f.pickup_open);
      let close = day * 1440 + f.pickup_close;
      if (close <= from + 30) { const nd = day + 1 + (((day + 1) % 7) >= 5 ? (7 - ((day + 1) % 7)) : 0); from = Math.max(from, nd * 1440 + (f.pickup_open ?? 8 * 60)); close = nd * 1440 + f.pickup_close; }
      to = Math.min(to, close);
    }
    return to > from ? { from, to } : base;
  }
  function deliveryWindowFor(order, base) {
    const f = noteOf(order.__req);
    let to = base.to;
    if (f.deadline_at != null && f.deadline_at > base.from) to = Math.min(to, f.deadline_at);
    if (f.delivery_close != null) { const c = Math.floor(to / 1440) * 1440 + f.delivery_close; if (c > base.from && c < to) to = c; }
    return { from: base.from, to };
  }
  function priorOk(place) {
    if (!homes) return 0.8;
    const n = Object.entries(homes).reduce((s, [h, c]) => s + (hav(P[place], P[h]) <= 30 ? c : 0), 0);
    return 0.55 + 0.40 * (1 - Math.exp(-n / 15));
  }
  const pending = {};
  const unbooked = {};                               // order id -> minute since it has had no driver                                // order id -> minute its first gig was posted (until booked)
  const backlog = place => Object.keys(pending).reduce((n, oid) => n + (orderTown[oid] === place ? 1 : 0), 0);
  function pFail(place, withBacklog = true) {
    const t = town[place] || { ok: 0, fail: 0 };
    const pf = 1 - (t.ok + p.priorW * priorOk(place)) / (t.ok + t.fail + p.priorW);
    return withBacklog ? 1 - (1 - pf) * Math.exp(-backlog(place) / p.backlogB) : pf;
  }
  function sweepPending(now) {
    for (const [oid, t0] of Object.entries(pending)) {
      const h = (now % 1440) / 60;
      if (now - t0 > p.earlyFail && h >= 9 && h <= 20) { resolve(oid, 'fail'); delete pending[oid]; }
    }
  }
  // projected late share: known late + open orders already warned + open orders unbooked for hours
  function lateShare() {
    const n = doneOrders + openOrders.size;
    if (n < 15) return 0;
    const now = lastNow, oldPending = Object.values(unbooked).filter(t0 => now - t0 > 180).length;
    return (lateOrders + p.warnW * openWarned.size + p.pendW * oldPending) / n;
  }
  function shadow() { return 1 + p.shadowK * Math.max(0, lateShare() - p.targetLate) * 10; }
  const resolve = (oid, how) => {
    if (resolved[oid]) return; resolved[oid] = how;
    const pl = orderTown[oid]; if (!pl) return;
    const t = town[pl] ||= { ok: 0, fail: 0 }; t[how]++;
  };
  return {
    params: p, comp, trust, town, stats: { quoted: 0, declined: 0, evNeg: 0 }, pFail, shadow, lateShare,
    getState() { return { comp: comp.getState(), trust, quoted, town, orderTown, resolved, pending, unbooked, lastNow, lateOrders, doneOrders, openWarned: [...openWarned], openOrders: [...openOrders] }; },
    setState(st) {
      comp.setState(st.comp); Object.assign(trust, st.trust); Object.assign(quoted, st.quoted); Object.assign(town, st.town);
      Object.assign(orderTown, st.orderTown); Object.assign(resolved, st.resolved); Object.assign(pending, st.pending || {}); Object.assign(unbooked, st.unbooked || {}); lastNow = st.lastNow || 0; lateOrders = st.lateOrders; doneOrders = st.doneOrders;
      st.openWarned.forEach(x => openWarned.add(x)); st.openOrders.forEach(x => openOrders.add(x));
    },
    async init(desk) { const roster = await desk.api.roster(desk.worldId); homes = {}; for (const d of roster) homes[d.homePlaceId] = (homes[d.homePlaceId] || 0) + 1; },
    quote(desk, req) {
      const km = Math.max(...req.legs.map(l => road(l.pickup.placeId, l.delivery.placeId).km));
      const priv = req.legs.some(l => l.pickup.privateAddress || l.delivery.privateAddress);
      const step = (tab, v) => tab.find(([lim]) => v < lim)[1];
      const structural = o => 1 - (1 - step(p.riskSlack, slackOf(req, o))) * (1 - step(p.riskKm, km));
      let okOpts = req.options.filter(o => km <= p.maxKm && slackOf(req, o) >= (priv ? p.minSlackPriv : p.minSlack) && !(req.legs.length > 1 && priv && !p.tradeinPriv));
      if (okOpts.length > 1) { const minR = Math.min(...okOpts.map(structural)); okOpts = okOpts.filter(o => structural(o) <= minR + p.optRiskGap); }   // offer a fast option only if it is not much riskier
      const prices = {}; for (const o of req.options) prices[o.kind] = Math.round(o.listPriceCents * p.declineMult / 100) * 100;
      if (!okOpts.length) { this.stats.declined++; return prices; }
      const o = okOpts[0], list = o.listPriceCents, ref = req.referenceFeeCents;
      const t = 1 + 0.01 * (trust[req.dealerId] || 0);
      const pw = comp.pWin(req.arrivedAt, grid.map(u => Math.log(u / t)));
      const pf = 1 - (1 - Math.max(...req.legs.map(l => pFail(l.pickup.placeId)))) * (1 - Math.max(...okOpts.map(structural)));
      const ls = lateShare();
      if (ls >= p.hardLate && pf > p.hardPFail) { this.stats.braked = (this.stats.braked || 0) + 1; return prices; }   // brake: only safe cars, which also dilute the late share   // hard brake
      const lam = shadow();
      const f0 = p.f0 + p.f0Risk * pf;
      let best = -1, bu = null;
      grid.forEach((u, i) => {
        const price = u * list;
        const cost = p.costMult * f0 * ref + pf * lam * (p.failRef * ref + p.failPrice * price);
        const ev = pw[i] * (price - cost); if (ev > best) { best = ev; bu = u; }
      });
      if (best <= p.minEv) { this.stats.evNeg++; return prices; }
      for (const oo of okOpts) prices[oo.kind] = Math.round(oo.listPriceCents * bu / 100) * 100;
      quoted[req.id] = Math.log(bu / t);
      this.stats.quoted++;
      return prices;
    },
    onEvent(desk, e) {
      lastNow = Math.max(lastNow, e.at);
      sweepPending(e.at);
      switch (e.kind) {
        case 'order_won':
          if (e.order.id in quoted) { comp.observe(desk.requests[e.order.id]?.arrivedAt ?? e.at, quoted[e.order.id], true); delete quoted[e.order.id]; }
          orderTown[e.order.id] = e.order.legs[0].pickup.placeId; openOrders.add(e.order.id); pending[e.order.id] = e.at; unbooked[e.order.id] = e.at; break;
        case 'order_lost':
          if (e.requestId in quoted) { comp.observe(desk.requests[e.requestId]?.arrivedAt ?? e.at, quoted[e.requestId], false); delete quoted[e.requestId]; }
          break;
        case 'gig_booked': { const oid = desk.gigInfo[e.gigId]?.orderId; if (oid) { delete pending[oid]; delete unbooked[oid]; if (!openWarned.has(oid)) resolve(oid, 'ok'); } break; }
        case 'deadline_close': resolve(e.orderId, 'fail'); delete pending[e.orderId]; if (openOrders.has(e.orderId)) openWarned.add(e.orderId); break;
        case 'order_cancelled': openOrders.delete(e.orderId); openWarned.delete(e.orderId); delete pending[e.orderId]; delete unbooked[e.orderId]; break;
        case 'gig_dropped': case 'no_show': { const oid = desk.gigInfo[e.gigId]?.orderId; if (oid && openOrders.has(oid)) unbooked[oid] = e.at; break; }
        case 'delivered': {
          const o = desk.orders[e.orderId]; if (!o) return;
          const all = o.deliveredAt.map((d, i) => i === e.leg ? e.at : d);
          if (all.every(d => d != null)) {
            const onTime = all.every(d => d <= o.deadline);
            const t = trust[o.dealerId] || 0; trust[o.dealerId] = onTime ? Math.min(25, t + 1) : Math.floor(t / 2);
            openOrders.delete(e.orderId); openWarned.delete(e.orderId); delete unbooked[e.orderId]; doneOrders++; if (!onTime) lateOrders++;
          }
        }
      }
    },
    postExtras(desk, order, legs, retry) {
      order.__req = desk.requests[order.id];
      const f = noteOf(order.__req);
      return {
        manual: f.manual, plates: f.plates,
        pickupWindow: (leg, noted, base) => pickupWindowFor(order, leg, base, retry.pickupFrom),
        deliveryWindow: (leg, noted, base) => deliveryWindowFor(order, base),
      };
    },
    onUnbooked(desk, e) {         // a drop or a no-show: the old fee did not hold a driver, so go up a step now
      const info = desk.gigInfo[e.gigId]; if (!info) return;
      const order = desk.orders[info.orderId]; if (!order || desk.urgent.has(order.id)) return;
      const ref = desk.requests[order.id].referenceFeeCents * info.cars / order.legs.length;
      const cur = (desk.posts[e.gigId]?.schedule || []).filter(s => s.at <= desk.now).reduce((m, s) => Math.max(m, s.feeCents), 0);
      const f1 = Math.max(cur * 1.15, ref * p.f1);
      const t2 = Math.max(desk.now + 1, order.lastPickupAt - p.f2Lead);
      const schedule = [{ at: desk.now, feeCents: Math.round(f1) }, { at: t2, feeCents: Math.round(Math.max(f1, ref * p.f2)) }];
      desk.outbox.push({ type: 'reprice', gigId: e.gigId, schedule });
      if (desk.posts[e.gigId]) desk.posts[e.gigId] = { ...desk.posts[e.gigId], schedule };
    },
    fees(desk, order, cars, urgent) {
      const ref = desk.requests[order.id].referenceFeeCents * cars / order.legs.length;
      if (urgent) return S.steep(desk.now, ref, 8);
      const pf = pFail(order.legs[0].pickup.placeId, false);
      const f0 = p.f0 + p.f0Risk * pf;
      const steps = [{ at: desk.now, feeCents: Math.round(ref * f0) }];
      const t1 = Math.max(desk.now + 1, order.lastPickupAt - p.f1Lead), t2 = Math.max(t1 + 1, order.lastPickupAt - p.f2Lead);
      steps.push({ at: t1, feeCents: Math.round(ref * Math.max(f0, p.f1)) }, { at: t2, feeCents: Math.round(ref * Math.max(f0, p.f2)) });
      return steps;
    },
  };
}
// ---------------------------------------------------------------- Node entry point
// Usage (Node 22+):  SPREAD_URL=... SPREAD_KEY=... node spread-bot.mjs [--level 8] [--seed s] [--board] [--final] [--params '{"f0":0.95}']

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
