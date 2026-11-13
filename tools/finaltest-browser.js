// Browser harness: run the final-mode polling loop against a practice world whose clock is
// moved by a separate "clock" coroutine, like the real final. Includes a mid-week restart.
window.finalTest = (name, seed, params, opts = {}) => {
  const rec = runs[name] = { status: 'running', t0: Date.now(), restarts: 0, lateQuotes: 0, log: [] };
  let store = null;                       // what spread-state.json would hold
  let clockNow = 0, ended = false;
  const dayMs = opts.dayMs ?? 0, nightStep = opts.nightStep ?? 10;
  async function clock(worldId, endsAt) {
    while (clockNow < endsAt) {
      const h = (clockNow % 1440) / 60, d = Math.floor(clockNow / 1440) % 7;
      const day = d < 5 && h >= 6 && h < 22;
      const r = await api.advance(worldId, { by: day ? 1 : nightStep });
      clockNow = r.now;
      if (day && dayMs) await new Promise(res => setTimeout(res, dayMs));
    }
    ended = true;
  }
  async function bot(worldId, stopAt) {
    const policy = makeV4(params);
    const history = await api.events(worldId, -1);
    const snap = await api.snapshot(worldId);
    const desk = new S.Desk(api, worldId, 8, snap.now, policy);
    await policy.init(desk);
    if (store) policy.setState(JSON.parse(store));
    desk.rejoin(snap, history, !store);
    await desk.send();
    let after = history.length ? history[history.length - 1].seq : -1;
    const seen = new Set(history.map(e => e.seq));
    while (!ended) {
      if (stopAt && clockNow >= stopAt) return 'stopped';
      const evs = await api.events(worldId, after);
      for (const e of evs) {
        if (seen.has(e.seq)) continue; seen.add(e.seq); after = Math.max(after, e.seq);
        desk.now = Math.max(desk.now, e.at);
        await desk.handle(e);
      }
      desk.now = Math.max(desk.now, clockNow);
      // quotes must reach the world before respondBy
      for (const a of desk.outbox) if (a.type === 'quote') { const q = desk.requests[a.requestId]; if (q && clockNow >= q.respondBy) rec.lateQuotes++; }
      await desk.send();
      store = JSON.stringify(policy.getState());
      if (!evs.length) await null;
    }
    return 'ended';
  }
  (async () => {
    try {
      const w = await api.create(8, seed); rec.worldId = w.worldId;
      const snap = await api.snapshot(w.worldId);
      const c = clock(w.worldId, snap.endsAt);
      const restartAt = opts.restartAt ?? (2 * 1440 + 11 * 60);   // Wednesday 11:00
      const first = await bot(w.worldId, restartAt);
      if (first === 'stopped') { rec.restarts++; await bot(w.worldId, null); }
      await c;
      rec.score = await api.score(w.worldId); rec.status = 'done'; rec.secs = (Date.now() - rec.t0) / 1000;
    } catch (e) { rec.status = 'error'; rec.error = String(e.stack || e); }
  })();
  return name;
};
