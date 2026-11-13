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
