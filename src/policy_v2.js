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
