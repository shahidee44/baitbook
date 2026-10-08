// The phone's own store: posts, residents, image blobs and queued votes live in
// IndexedDB, so the feed works with no network at all. Docking is one round
// trip: queued votes go home, the new catch (last N nights) and the current
// votes come down, posts that aged out or were thrown back are dropped.
window.BB = window.BB || {};
(function (BB) {
  const api = BB.api;
  let dbp;

  function open() {
    if (dbp) return dbp;
    dbp = new Promise((resolve, reject) => {
      const r = indexedDB.open('baitbook-device', 1);
      r.onupgradeneeded = () => {
        const d = r.result;
        d.createObjectStore('posts', { keyPath: 'id' });
        d.createObjectStore('residents', { keyPath: 'id' });
        d.createObjectStore('blobs');
        d.createObjectStore('meta');
      };
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
    return dbp;
  }
  const tx = async (store, mode, fn) => {
    const d = await open();
    return new Promise((resolve, reject) => {
      const t = d.transaction(store, mode), s = t.objectStore(store);
      let out;
      Promise.resolve(fn(s)).then((v) => { out = v; });
      t.oncomplete = () => resolve(out);
      t.onerror = () => reject(t.error);
    });
  };
  const req = (r) => new Promise((resolve, reject) => { r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });
  const all = (store) => tx(store, 'readonly', (s) => req(s.getAll()));

  const S = (BB.store = { posts: [], residents: [], meta: {} });
  const urls = new Map(); // blob key -> object URL

  S.load = async () => {
    [S.posts, S.residents] = await Promise.all([all('posts'), all('residents')]);
    S.meta = (await tx('meta', 'readonly', (s) => req(s.get('meta')))) || {};
    return S;
  };
  S.saveMeta = () => tx('meta', 'readwrite', (s) => s.put(S.meta, 'meta'));

  // an image from the device's own copy (object URL), or '' if it isn't here
  S.img = (stem, thumb) => urls.get(stem + (thumb ? '_t' : '')) || '';
  S.warmImages = async () => {
    const keys = await tx('blobs', 'readonly', (s) => req(s.getAllKeys()));
    for (const k of keys) {
      if (urls.has(k)) continue;
      const blob = await tx('blobs', 'readonly', (s) => req(s.get(k)));
      if (blob) urls.set(k, URL.createObjectURL(blob));
    }
  };

  S.resident = (id) => S.residents.find((r) => r.id === id);
  S.post = (id) => S.posts.find((p) => p.id === id);
  S.visible = () => S.posts.filter((p) => !p.snagged);  // what the screens show: snags wait out of sight
  S.pending = () => S.posts.filter((p) => p.queued);
  S.chain = (p) => { const out = []; for (let c = S.post(p.source_id); c && !c.snagged; c = c.source_id ? S.post(c.source_id) : null) out.unshift(c); return out; };

  // vote offline: it waits on the device until the next dock
  S.vote = async (id, v) => {
    const p = S.post(id);
    if (!p || p.kind === 'seed') return;
    p.vote = p.vote === v ? 0 : v;
    p.queued = true;
    p.voteQueued = true;
    p.voted_at = Date.now() / 1000;
    await tx('posts', 'readwrite', (s) => s.put(p));
  };

  // snag offline: a broken render leaves the screens now and the Angler at the next dock. Undone
  // once a dock has taken the snag out (snagSent), the undo has to go home too. False if the post
  // isn't here any more: a dock that came back took it home, and the PC app can still undo it.
  S.snag = async (id, on = true) => {
    const p = S.post(id);
    if (!p || p.kind === 'seed') return false;
    if (p.voteQueued === undefined) p.voteQueued = !!p.queued;  // a vote queued before snags existed
    p.snagged = on;
    p.queued = on || p.voteQueued || !!p.snagSent;
    p.snagged_at = Date.now() / 1000;
    await tx('posts', 'readwrite', (s) => s.put(p));
    return true;
  };

  // A post's vote and snag as they stand. A dock notes this for every post as it
  // leaves; the screens stay live while it's out, so one that no longer matches
  // when it's back was voted on or snagged meanwhile.
  const stamp = (p) => [p.vote, p.voteQueued, !!p.snagged, p.voted_at, p.snagged_at].join('|');
  const byId = (a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);  // the order IndexedDB keeps them in

  // the dock: progress(step, detail) is called as it goes. One at a time: tapping
  // again while one is out gets the same dock back.
  let docking = null;
  S.dock = (progress) => docking || (docking = dock(progress).finally(() => { docking = null; }));

  async function dock(progress) {
    const queued = S.pending(), asSent = new Map(S.posts.map((p) => [p.id, stamp(p)])), now = Date.now() / 1000;
    progress(1, queued.length ? `sending ${queued.length} vote${queued.length > 1 ? 's' : ''} home` : 'no votes to send');
    queued.forEach((p) => { if (p.snagged) p.snagSent = true; });  // from here the Angler may have it, whatever comes back
    const res = await api.post('/api/dock', {
      // a vote only if one was cast here (older queued posts have no voteQueued: those were votes);
      // the vote and the snag each go with their own time, so neither passes for newer than it is
      votes: queued.map((p) => ({ id: p.id, vote: p.voteQueued === false ? null : p.vote, voted_at: p.voted_at || now,
        snag: !!p.snagged, unsnag: !p.snagged && !!p.snagSent, snagged_at: p.snagged_at || now })),
      have: S.posts.map((p) => p.id),
    });
    progress(2, `${res.posts.length} new post${res.posts.length === 1 ? '' : 's'}`);
    // images for every new post, and for any kept post whose download failed last time
    const drop = new Set(res.drop);
    const want = [...res.posts, ...S.posts.filter((p) => !drop.has(p.id) && !p.snagged)].filter((p) => p.image && !(urls.has(p.image) && urls.has(p.image + '_t')));
    let n = 0;
    for (const p of want) {
      for (const thumb of [false, true]) {
        const key = p.image + (thumb ? '_t' : '');
        if (urls.has(key)) continue;
        const blob = await fetch(api.img(p.image, thumb)).then((r) => (r.ok ? r.blob() : null)).catch(() => null);
        if (blob) { await tx('blobs', 'readwrite', (s) => s.put(blob, key)); urls.set(key, URL.createObjectURL(blob)); }
      }
      progress(2, `unpacking ${++n} of ${want.length}`);
    }

    // Settle every post at once, with no await until it's written, so nothing can be voted halfway.
    // A post voted on, snagged or unsnagged since the dock left keeps its own vote and stays queued
    // (and isn't dropped): it goes home next time. Every other post takes the Angler's word. Its vote is
    // the vote now (the Angler has this phone's, and any newer one from elsewhere), and a drop is a drop.
    const votes = res.votes || {}, had = new Set(S.posts.map((p) => p.id));
    const kept = [], gone = [], settled = [];
    for (const p of S.posts) {
      if (p.queued && stamp(p) !== asSent.get(p.id)) kept.push(p);
      else if (drop.has(p.id)) gone.push(p);
      else {
        const vote = p.id in votes ? votes[p.id] : p.vote;
        if (p.queued || vote !== p.vote) { Object.assign(p, { queued: false, voteQueued: false, snagSent: false, vote }); settled.push(p); }
        kept.push(p);
      }
    }
    const fresh = res.posts.filter((p) => !had.has(p.id));
    S.posts = [...kept, ...fresh].sort(byId);
    // only residents whose read is done come down; one being read again (or whose read failed)
    // keeps the card the phone already has while any of their posts are here
    const came = new Set(res.residents.map((r) => r.id)), here = new Set(S.posts.map((p) => p.resident_id));
    S.residents = [...res.residents, ...S.residents.filter((r) => !came.has(r.id) && here.has(r.id))].sort(byId);
    progress(3, 'tidying up');
    await tx('posts', 'readwrite', (s) => {
      fresh.forEach((p) => s.put(p));
      settled.forEach((p) => s.put(p));
      gone.forEach((p) => s.delete(p.id));
    });
    const stems = gone.flatMap((p) => (p.image ? [p.image, p.image + '_t'] : []));
    await tx('blobs', 'readwrite', (s) => stems.forEach((k) => s.delete(k)));
    stems.forEach((k) => { if (urls.has(k)) URL.revokeObjectURL(urls.get(k)); urls.delete(k); });
    await tx('residents', 'readwrite', (s) => { s.clear(); S.residents.forEach((r) => s.put(r)); });
    S.meta.lastDock = Date.now() / 1000;
    S.meta.history = [{ at: S.meta.lastDock, up: queued.length, down: res.posts.length }, ...(S.meta.history || [])].slice(0, 8);
    await S.saveMeta();
    return { up: queued.length, down: res.posts.length, dropped: gone.length };
  }
})(window.BB);
