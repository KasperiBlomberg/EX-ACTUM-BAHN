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
