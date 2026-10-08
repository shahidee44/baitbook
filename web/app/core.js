// The PC app: the owner's client for the Angler. Views register themselves on
// BB.views; core does routing, status polling, shared data and event dispatch.
(function (BB) {
  const api = BB.api, esc = BB.esc;
  const TABS = [['feed', 'Feed'], ['residents', 'Residents'], ['shift', 'Night shift'], ['memory', 'Memory'], ['reef', 'Reef'], ['ledger', 'Ledger'], ['settings', 'Settings']];
  const app = (BB.app = { status: null, residents: [], view: 'feed', params: {} });
  BB.views = BB.views || {};
  let root, pollT, lastJob = null, shown = false;

  // ---------- data ----------
  app.res = (id) => app.residents.find((r) => r.id === id);
  app.loadResidents = async () => { app.residents = await api.get('/api/residents'); return app.residents; };
  app.avatar = (r, cls = 'ava') => {
    const s = r && r.seeds && r.seeds[0];
    return `<img class="${cls}" alt="" style="background:${r ? r.hue : '#E8C9A8'}" src="${s ? api.img(s.image, true) : ''}">`;
  };

  // ---------- status polling ----------
  async function poll() {
    clearTimeout(pollT);
    try {
      app.status = await api.get('/api/status');
      const e = app.status.engine, busy = e.state !== 'idle' && e.state !== 'error';
      pill();
      const jobKey = busy ? e.job + (e.shift_id || e.resident || '') : null;
      if (!shown) { await app.loadResidents().catch(() => {}); render(); }  // first contact
      else if (lastJob && !jobKey) { await app.loadResidents(); render(); }  // a job just finished
      else if (BB.views[app.view].onStatus) BB.views[app.view].onStatus(app.status);
      lastJob = jobKey;
      pollT = setTimeout(poll, busy ? 1500 : 8000);
    } catch (err) {
      if (err.status === 401) return pairScreen(api.paired() ? 'This PC app isn’t paired with this Angler any more.' : '');
      document.getElementById('engine-pill').innerHTML = '<span class="pill">Angler unreachable</span>';
      pollT = setTimeout(poll, 4000);
    }
  }
  app.poll = poll;

  // ---------- pairing (a PC app on another machine than the Angler) ----------
  function pairScreen(msg) {
    clearTimeout(pollT);
    document.getElementById('nav').innerHTML = '';
    document.getElementById('engine-pill').innerHTML = '<span class="pill">not paired</span>';
    root.innerHTML = `<form class="card form" id="pairform" style="max-width:480px;margin:48px auto">
      <h2>Pair with this Angler</h2>
      <p class="sub">This PC app is on another machine than the Angler at <code>${esc(api.base())}</code>, so it pairs once, as the owner. The code is on the Angler’s machine: in its PC app under Settings, or in its console when it starts.</p>
      <label>Pairing code<input name="code" inputmode="numeric" maxlength="6" placeholder="6 digits" required autofocus></label>
      <label>Call this PC<input name="name" value="PC"></label>
      ${msg ? `<p class="hint" style="color:var(--clay-deep)">${esc(msg)}</p>` : ''}
      <button class="btn">Pair</button></form>`;
  }

  async function pair(f) {
    try {
      const res = await fetch(api.base() + '/api/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: f.code.value, name: f.name.value, role: 'owner' }) });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || 'pairing failed');
      api.remember(api.base(), json.token, json.name);
      location.reload();
    } catch (err) { BB.toast(err.message); }
  }

  function pill() {
    const e = app.status.engine, m = app.status.models;
    const label = { idle: 'idle', reading: 'reading seeds', writing: `${m.text} writing`, editing: `${m.image} editing`, error: 'last job failed' }[e.state] || e.state;
    // its code changed since it started; opening the app restarts it only if it's idle and in the background
    const stale = app.status.stale ? `<span class="pill warm" title="This Angler started before its code last changed, so it still runs the old code. Run \`baitbook stop\`, then \`baitbook\` (or restart it in its console).">restart the Angler</span>` : '';
    document.getElementById('engine-pill').innerHTML = `${stale}<span class="pill ${e.state === 'idle' ? '' : 'warm'}"><span class="dot"></span>${esc(label)}</span>`;
  }

  // ---------- routing ----------
  function nav() {
    document.getElementById('nav').innerHTML = TABS.map(([k, l]) => `<button class="${app.view === k ? 'on' : ''}" data-go="${k}">${l}</button>`).join('');
  }
  // a view's render waits on the Angler; one that comes back after a newer render started (another
  // tab, say) is dropped, so the page always shows the view the nav says it does
  let renders = 0;
  async function render() {
    const n = ++renders;
    shown = true;
    nav();
    const v = BB.views[app.view];
    root.innerHTML = '<p class="empty" style="padding:40px">…</p>';
    let html;
    try { html = await v.render(app.params); } catch (err) { html = `<div class="card"><h2>Couldn’t load this</h2><p class="sub">${esc(err.message)}</p></div>`; }
    if (n !== renders) return;
    root.innerHTML = html;
    if (v.after) v.after();
  }
  app.render = render;
  app.go = (view, params = {}) => { app.view = view; app.params = params; location.hash = view; render(); };

  // ---------- shared pieces ----------
  app.verdict = (p) => (p.kind === 'seed' ? '<span class="verdict none">seed</span>' : p.snagged ? '<span class="verdict snag">✕ snagged</span>' : p.vote > 0 ? '<span class="verdict up">↑ bite</span>' : p.vote < 0 ? '<span class="verdict down">↓ throw back</span>' : '<span class="verdict none">not voted</span>');

  // a feed post; `open` shows the prompt panel with its edit chain
  app.postCard = (p, open, chain) => {
    const r = app.res(p.resident_id) || { name: '?', handle: '?', hue: '#E8C9A8', seeds: [] };
    const seed = p.kind === 'seed';
    return `<article class="post pc-post" data-post="${p.id}">
      <div class="post-top">
        <button data-act="to-res" data-res="${r.id}">${app.avatar(r)}</button>
        <div class="who"><b>${esc(r.name)}</b><small>@${esc(r.handle)} · ${BB.ago(p.created_at)}${p.shift_id ? ' · night #' + p.shift_id : ''}</small></div>
        ${seed ? '<span class="mode seed">seed</span>' : ''}
      </div>
      <div class="frame" data-act="dbl" data-id="${p.id}"><img class="art" loading="lazy" src="${api.img(p.image)}" alt=""></div>
      <div class="acts">${seed ? '<span class="state">owner’s seed image</span>' : `
        <button class="vote up ${p.vote > 0 ? 'on' : ''}" data-act="vote" data-v="1" data-id="${p.id}">↑ bite</button>
        <button class="vote down ${p.vote < 0 ? 'on' : ''}" data-act="vote" data-v="-1" data-id="${p.id}">↓ throw back</button>
        <button class="vote snag" data-act="snag" data-id="${p.id}" title="A broken render (anatomy, garbled detail): take it out without teaching the Angler anything">✕ snag</button>`}
        <button class="peek ${open ? 'on' : ''}" data-act="peek" data-id="${p.id}">${open ? 'hide' : 'prompt'}</button>
      </div>
      ${p.caption ? `<p class="cap"><b>${esc(r.handle)}</b>${esc(p.caption)}</p>` : ''}
      <div class="tags">${(p.tags || []).map((t) => '#' + esc(t)).join(' ')}</div>
      ${open ? `<div class="promptbox">${chain && chain.length ? `<div class="chain">${chain.length > 5 ? '<span class="more">…</span>' : ''}${chain.slice(-5).map((c) => `<span class="link">${c.snagged ? '<span class="gone">✕</span>' : `<img src="${api.img(c.image, true)}" alt="">`}<i>${c.kind === 'seed' ? 'seed #' : '#'}${c.id}${c.snagged ? ' snag' : ''}</i></span><span class="arr">→</span>`).join('')}<span class="link"><img src="${api.img(p.image, true)}" alt=""><i>this</i></span></div>
        <div class="chain-note">${chain.length} edit${chain.length > 1 ? 's' : ''} from seed #${chain[0].id} · <button class="linkish" data-act="to-reef" data-res="${r.id}" data-id="${p.id}">see it in the reef →</button></div>` : ''}
        <code>${esc(p.prompt || '(the vision read hasn’t written this one yet)')}</code><div class="why"><span>#${p.id}</span></div></div>` : ''}
    </article>`;
  };

  // ---------- events ----------
  async function onClick(e) {
    const go = e.target.closest('[data-go]');
    if (go) return app.go(go.dataset.go);
    const t = e.target.closest('[data-act]');
    if (!t) return;
    const act = t.dataset.act;
    if (act === 'vote') return app.vote(+t.dataset.id, +t.dataset.v, t);
    if (act === 'snag') return app.snag(+t.dataset.id, t);
    if (act === 'to-res') return app.go('residents', { focus: t.dataset.res });
    if (act === 'to-reef') return app.go('reef', { res: t.dataset.res, focus: +t.dataset.id });
    const v = BB.views[app.view];
    if (v.onClick) v.onClick(act, t, e);
  }

  app.vote = async (id, v, btn) => {
    const card = btn && btn.closest('article');
    const cur = card && card.querySelector('.vote.on');
    const next = cur && +cur.dataset.v === v ? 0 : v;
    await api.post('/api/votes', { votes: [{ id, vote: next }] });
    if (card) {
      card.querySelectorAll('.vote').forEach((b) => b.classList.toggle('on', +b.dataset.v === next));
      if (next > 0) burst(card);
    }
    const v2 = BB.views[app.view];
    if (v2.onVote) v2.onVote(id, next);
  };

  // a snag: the post leaves every feed and is never learned from; undo works until the next night shift
  app.snag = async (id, btn) => {
    try { await api.post(`/api/posts/${id}/snag`, { snag: true }); } catch (err) { return BB.toast(err.message); }
    const card = btn && btn.closest('article');
    if (card) { card.classList.add('snagging'); setTimeout(() => card.remove(), 300); }
    const v = BB.views[app.view];
    if (v.onSnag) v.onSnag(id, true);
    BB.toast(`Snagged #${id}: gone, and not a throw-back`, { label: 'undo', fn: () => app.unsnag(id) });
  };
  app.unsnag = async (id) => {
    try { await api.post(`/api/posts/${id}/snag`, { snag: false }); } catch (err) { return BB.toast(err.message); }
    BB.toast(`#${id} is back`);
    app.render();
  };

  function burst(card) {
    const f = card.querySelector('.frame');
    const b = document.createElement('div');
    b.className = 'burst'; b.innerHTML = '<span>bite!</span>';
    f.appendChild(b); setTimeout(() => b.remove(), 950);
  }

  app.start = async () => {
    root = document.getElementById('view');
    document.addEventListener('click', onClick);
    document.addEventListener('dblclick', (e) => {
      const f = e.target.closest('.frame[data-act="dbl"]');
      const card = f && f.closest('article');
      if (card && card.querySelector('.vote.up') && !card.querySelector('.vote.up.on')) app.vote(+f.dataset.id, 1, card.querySelector('.vote.up'));
    });
    ['input', 'change', 'submit'].forEach((type) => document.addEventListener(type, (e) => {
      if (type === 'submit' && e.target.id === 'pairform') { e.preventDefault(); return pair(e.target); }
      const v = BB.views[app.view], fn = v && v['on' + type[0].toUpperCase() + type.slice(1)];
      if (fn) fn(e);
    }));
    const h = location.hash.slice(1);
    if (TABS.some(([k]) => k === h)) app.view = h;
    await poll();  // renders the view once the Angler answers
  };
})(window.BB);
