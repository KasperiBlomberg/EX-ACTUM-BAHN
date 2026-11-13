// v5 = v4 + direct offers to our own drivers where they finish.
// A driver who just delivered one of our cars at X is not notified of gigs near X (notifications go by
// home town). Offer them our unbooked gigs that start near X: their trip to the pickup is short, so a
// fee that the app's drivers turn down can pay them. Also used after a deadline warning.
function makeV5(params = {}) {
  const p = Object.assign({ offerKm: 35, offerLeadMax: 240, offerFeeMult: 1.0, offerUrgentKm: 80 }, params);
  const base = makeV4(params);
  const gigDriver = {};               // gig -> driver who booked it
  const lastSeen = {};                // driver -> {place, at}
  const offersToday = {};             // driver -> {day, n}
  const openOffers = {};              // gig -> count of offers waiting
  const offered = new Set();          // gig|driver pairs already tried
  const stats = { offers: 0, accepted: 0, declined: 0, expired: 0 };
  const currentFee = (desk, gigId) => (desk.posts[gigId]?.schedule || []).filter(s => s.at <= desk.now).reduce((m, s) => Math.max(m, s.feeCents), desk.posts[gigId]?.schedule?.[0]?.feeCents || 0);
  function canOffer(desk, driver, gigId) {
    const d = Math.floor(desk.now / 1440), o = offersToday[driver];
    if (o && o.day === d && o.n >= 3) return false;
    if ((openOffers[gigId] || 0) >= 3) return false;
    if (offered.has(gigId + '|' + driver)) return false;
    const h = (desk.now % 1440) / 60; if (h < 7 || h >= 22) return false;
    return true;
  }
  function offer(desk, driver, gigId, fee) {
    const d = Math.floor(desk.now / 1440);
    const o = offersToday[driver]; offersToday[driver] = (o && o.day === d) ? { day: d, n: o.n + 1 } : { day: d, n: 1 };
    openOffers[gigId] = (openOffers[gigId] || 0) + 1; offered.add(gigId + '|' + driver);
    desk.outbox.push({ type: 'offer', gigId, driverId: driver, feeCents: Math.round(fee) });
    stats.offers++;
  }
  function openGigsNear(desk, place, km) {
    const out = [];
    for (const [oid, legs] of Object.entries(desk.legGigs)) {
      for (const gigId of new Set(Object.values(legs))) {
        if (desk.booked.has(gigId)) continue;
        const post = desk.posts[gigId]; if (!post) continue;
        const first = post.stops[0]; const order = desk.orders[first.orderId]; if (!order) continue;
        const pick = order.legs[first.leg].pickup;
        const from = first.window ? first.window.from : pick.window.from;
        if (from > desk.now + p.offerLeadMax) continue;           // not for a later day
        if (hav(P[place], P[pick.placeId]) > km) continue;
        out.push({ gigId, order, dist: hav(P[place], P[pick.placeId]) });
      }
    }
    return out.sort((a, b) => a.dist - b.dist);
  }
  return Object.assign(Object.create(base), base, {
    offerStats: stats,
    onEvent(desk, e, replay) {
      base.onEvent(desk, e, replay);
      if (replay) return;
      switch (e.kind) {
        case 'gig_booked': gigDriver[e.gigId] = e.driverId; break;
        case 'offer_answered':
          openOffers[e.gigId] = Math.max(0, (openOffers[e.gigId] || 1) - 1);
          stats[e.answer] = (stats[e.answer] || 0) + 1;
          if (e.answer === 'accepted') gigDriver[e.gigId] = e.driverId;
          break;
        case 'delivered': {
          const drv = gigDriver[e.gigId]; if (!drv) break;
          lastSeen[drv] = { place: e.placeId, at: e.at };
          // only once this driver's gig has no more stops for us: a gig's last delivery
          const post = desk.posts[e.gigId];
          const lastStop = post && post.stops[post.stops.length - 1];
          if (lastStop && !(lastStop.orderId === e.orderId && lastStop.leg === e.leg)) break;
          for (const g of openGigsNear(desk, e.placeId, p.offerKm).slice(0, 2)) {
            if (!canOffer(desk, drv, g.gigId)) continue;
            offer(desk, drv, g.gigId, currentFee(desk, g.gigId) * p.offerFeeMult);
            break;
          }
          break;
        }
        case 'deadline_close': {
          const order = desk.orders[e.orderId]; if (!order) break;
          const gigs = desk.gigsOf(order.id).filter(g => !desk.booked.has(g)); if (!gigs.length) break;
          const pick = order.legs[desk.posts[gigs[0]]?.stops[0].leg ?? 0].pickup.placeId;
          const cands = Object.entries(lastSeen).filter(([d, s]) => desk.now - s.at < 360 && hav(P[s.place], P[pick]) <= p.offerUrgentKm)
            .sort((a, b) => hav(P[a[1].place], P[pick]) - hav(P[b[1].place], P[pick]));
          let n = 0;
          for (const [drv] of cands) { if (n >= 3) break; if (!canOffer(desk, drv, gigs[0])) continue; offer(desk, drv, gigs[0], currentFee(desk, gigs[0]) * 1.1); n++; }
          break;
        }
      }
    },
    getState() { return { ...base.getState(), v5: { gigDriver, lastSeen, offersToday } }; },
    setState(st) { base.setState(st); if (st.v5) { Object.assign(gigDriver, st.v5.gigDriver); Object.assign(lastSeen, st.v5.lastSeen); Object.assign(offersToday, st.v5.offersToday); } },
  });
}
