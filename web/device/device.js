// The phone app: an offline feed. It only knows what it got at the last dock.
(function (BB) {
  const S = BB.store, api = BB.api, esc = BB.esc;
  const ui = { tab: 'feed', profile: null, single: null, peek: {}, sync: null, last: null, profView: 'grid', reefFocus: null };
  const LOCAL = ['localhost', '127.0.0.1', '::1'].includes(location.hostname);
  let root, body;

  const I = {
    feed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="4" y="3.5" width="16" height="11" rx="3"/><path d="M4 18.5h16M4 21h10"/></svg>',
    people: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><circle cx="17" cy="9" r="2.6"/><path d="M17.5 14.5c2.2.3 3.6 2 4 4.5"/></svg>',
    dock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v11M7.5 9.5 12 14l4.5-4.5"/><path d="M4 15v3a2.5 2.5 0 0 0 2.5 2.5h11A2.5 2.5 0 0 0 20 18v-3"/></svg>',
    phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="6" y="2.5" width="12" height="19" rx="3"/><path d="M10.5 5h3"/></svg>',
    house: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M3.5 11 12 4l8.5 7"/><path d="M6 9.5V20h12V9.5"/><rect x="10" y="13.5" width="4" height="6.5"/></svg>',
  };

  const avatar = (r, cls = 'ava') => {
    const seed = r && S.posts.find((p) => p.resident_id === r.id && p.kind === 'seed');
    return `<img class="${cls}" alt="" style="background:${r ? r.hue : '#E8C9A8'}" src="${seed ? S.img(seed.image, true) : ''}">`;
  };
  const feed = () => S.visible().sort((a, b) => ((b.kind === 'seed' ? 0 : b.shift_id) - (a.kind === 'seed' ? 0 : a.shift_id)) || b.id - a.id);

  // ---------- posts ----------
  function postHTML(p) {
    const r = S.resident(p.resident_id) || { id: '', name: '?', handle: '?' }, seed = p.kind === 'seed', peek = ui.peek[p.id];
    const chain = peek ? S.chain(p) : [];
    const state = seed ? '<span class="state">owner’s seed image</span>' : p.queued && p.vote ? '<span class="state q">● queued</span>' : '';
    const link = (c, label) => `<span class="link"><img src="${S.img(c.image, true)}" alt=""><i>${label}</i></span>`;
    return `<article class="post" data-post="${p.id}">
      <div class="post-top"><button data-act="profile" data-res="${r.id}">${avatar(r)}</button>
        <button class="who" data-act="profile" data-res="${r.id}"><b>${esc(r.name)}</b><small>@${esc(r.handle)} · ${BB.ago(p.created_at)}</small></button>${seed ? '<span class="mode seed">seed</span>' : ''}</div>
      <div class="frame" data-act="dbl" data-id="${p.id}"><img class="art" src="${S.img(p.image)}" alt=""></div>
      <div class="acts">${seed ? '' : `<button class="vote up ${p.vote > 0 ? 'on' : ''}" data-act="vote" data-v="1" data-id="${p.id}">↑ bite</button>
        <button class="vote down ${p.vote < 0 ? 'on' : ''}" data-act="vote" data-v="-1" data-id="${p.id}">↓ throw back</button>
        <button class="vote snag" data-act="snag" data-id="${p.id}" title="a broken render: take it out, don't learn from it">✕ snag</button>`}${state}
        <button class="peek ${peek ? 'on' : ''}" data-act="peek" data-id="${p.id}">${peek ? 'hide' : 'prompt'}</button></div>
      ${p.caption ? `<p class="cap"><b>${esc(r.handle)}</b>${esc(p.caption)}</p>` : ''}
      <div class="tags">${(p.tags || []).map((t) => '#' + esc(t)).join(' ')}</div>
      ${peek ? `<div class="promptbox">${chain.length ? `<div class="chain">${chain.length > 4 ? '<span class="more">…</span>' : ''}${chain.slice(-4).map((c) => link(c, (c.kind === 'seed' ? 'seed #' : '#') + c.id) + '<span class="arr">→</span>').join('')}${link(p, 'this')}</div>
        <div class="chain-note">${chain.length} edit${chain.length > 1 ? 's' : ''} from seed #${chain[0].id} · <button class="linkish" data-act="reef-from" data-id="${p.id}">see it in the reef →</button></div>` : ''}<code>${esc(p.prompt)}</code></div>` : ''}
    </article>`;
  }

  function feedHTML() {
    const ps = feed();
    if (!ps.length) return `<div class="end-card"><span class="mark" aria-hidden="true"></span><h3>Nothing here yet.</h3><p>Dock with your Angler to bring the catch in.</p><button class="btn" data-act="tab" data-tab="dock">Go to dock</button></div>`;
    const fresh = new Set(ps.filter((p) => !p.vote && p.kind !== 'seed').map((p) => p.resident_id));
    let h = `<div class="strip">${S.residents.map((r) => `<button data-act="profile" data-res="${r.id}"><div class="ring ${fresh.has(r.id) ? 'fresh' : ''}">${avatar(r, '')}</div><span>${esc(r.handle)}</span></button>`).join('')}</div>`;
    [...new Set(ps.map((p) => (p.kind === 'seed' ? 0 : p.shift_id)))].forEach((n) => {
      const group = ps.filter((p) => (p.kind === 'seed' ? 0 : p.shift_id) === n), open = group.filter((p) => !p.vote && p.kind !== 'seed').length;
      h += `<div class="batch-head"><h3>${n ? `Night shift #${n}` : 'Seed images'}</h3><span>${n ? `${group.length} posts · ${open ? open + ' unvoted' : 'all voted'}` : 'what each resident started from'}</span></div>${group.map(postHTML).join('')}`;
    });
    return h + `<div class="end-card"><span class="mark" aria-hidden="true"></span><h3>That’s the whole catch.</h3><p>baitbook doesn’t scroll forever. The Angler is writing tonight’s posts at home. Dock to send your votes and bring them in.</p><button class="btn" data-act="tab" data-tab="dock">Go to dock</button></div>`;
  }

  // ---------- residents & profile ----------
  function residentsHTML() {
    return `<div class="ph-title"><h2>Residents</h2><p>Nobody here is real. Everyone here is for you.</p></div><div class="res-list">${S.residents.map((r) => {
      const v = S.visible().filter((p) => p.resident_id === r.id && p.vote), b = v.filter((p) => p.vote > 0).length;
      return `<button class="res-row" data-act="profile" data-res="${r.id}">${avatar(r, '')}<div><b>${esc(r.name)}</b><small>${esc(r.bio)}</small></div>${v.length ? `<em>${BB.pct(b / v.length)} bite</em>` : ''}</button>`;
    }).join('') || '<p class="empty">Dock to meet them.</p>'}</div>`;
  }

  function profileHTML() {
    const r = S.resident(ui.profile);
    if (!r) { ui.profile = null; return feedHTML(); }
    const ps = feed().filter((p) => p.resident_id === r.id), voted = ps.filter((p) => p.vote), bites = voted.filter((p) => p.vote > 0).length;
    const nodes = S.visible().filter((p) => p.resident_id === r.id);
    return `<div class="prof"><button class="back" data-act="back">‹ back</button>
      <div class="prof-head">${avatar(r, '')}<div><h2>${esc(r.name)}</h2><small>@${esc(r.handle)}</small></div></div>
      <p class="bio">${esc(r.bio)}</p>
      <div class="stats"><div><b>${ps.length}</b><span>posts</span></div><div><b>${voted.length ? BB.pct(bites / voted.length) : '—'}</b><span>you bite</span></div><div><b>${voted.length}</b><span>votes</span></div></div>
      <p class="sect-label">mood tags · read from the seed images</p><div class="chips" style="margin-bottom:14px">${(r.moods || []).map((m) => `<span class="chip">${esc(m)}</span>`).join('')}</div>
      <div class="seg"><button class="${ui.profView === 'grid' ? 'on' : ''}" data-act="pview" data-v="grid">posts</button><button class="${ui.profView === 'reef' ? 'on' : ''}" data-act="pview" data-v="reef">reef</button></div>
      ${ui.profView === 'reef'
        ? `<div class="reef-scroll compact">${BB.reef.html(nodes, { compact: true, img: S.img, focus: ui.reefFocus, minWidth: Math.max(320, (body ? body.clientWidth : 360) - 16) })}</div><p class="reef-note">seeds on the seabed · every edit grows up from its source · tap an image to open it</p>`
        : `<div class="grid3">${ps.map((p) => `<button data-act="single" data-id="${p.id}"><img class="thumb" src="${S.img(p.image, true)}" alt="">${p.kind === 'seed' ? '<span class="s mode seed">seed</span>' : ''}${p.vote ? `<span class="v ${p.vote > 0 ? 'up' : 'down'}">${p.vote > 0 ? '↑' : '↓'}</span>` : ''}</button>`).join('')}</div>`}</div>`;
  }

  // ---------- dock ----------
  // Over plain http (a LAN address) there's no service worker, so nothing keeps the app on the phone.
  const insecureNote = () => (window.isSecureContext ? '' : `<p class="offline-note"><b>Offline mode needs HTTPS.</b>
    Over plain http, baitbook works only while this phone can reach the Angler. Run <code>baitbook phone</code> on the PC: it sets up
    Tailscale Serve and shows the address to open instead. <a href="${BB.PHONE_GUIDE}" target="_blank" rel="noopener">How it works</a></p>`);

  const STEPS = ['Looking for the Angler', 'Sending votes home', 'Pulling down the catch', 'Tidying up'];
  function dockHTML() {
    const pending = S.pending(), live = ui.sync !== null;
    let h = `<div class="ph-title"><h2>Dock</h2><p>${S.meta.lastDock ? `Last docked ${BB.agoLong(S.meta.lastDock)}` : 'Never docked'} · ${esc(api.base().replace(/^https?:\/\//, ''))}</p></div><div class="dock">
      <div class="dock-art ${live ? 'live' : ''}">${I.phone}<div class="wire"></div><span class="packet up"></span><span class="packet down"></span><span class="packet down"></span>${I.house}<span class="lbl l">this phone</span><span class="lbl r">the Angler</span></div>`;
    if (live) h += `<ul class="steps">${STEPS.map((s, i) => `<li class="${i < ui.sync.step ? 'done' : i === ui.sync.step ? 'doing' : ''}"><i></i>${s}${i === ui.sync.step && ui.sync.detail ? ' · ' + esc(ui.sync.detail) : ''}</li>`).join('')}</ul>`;
    else h += `<div class="ledger-box"><div class="row"><span>Waiting to go home</span><b>${pending.length}</b></div><div class="row"><span>…bites</span><b>${pending.filter((p) => !p.snagged && p.voteQueued !== false && p.vote > 0).length}</b></div><div class="row"><span>…snags</span><b>${pending.filter((p) => p.snagged).length}</b></div><div class="row"><span>New posts at home</span><b style="color:var(--muted)">unknown until docked</b></div></div>
      ${ui.last ? `<div class="ledger-box"><div class="row"><span>Last dock</span><b>↑ ${ui.last.up} votes · ↓ ${ui.last.down} posts</b></div></div>` : ''}
      <button class="btn block" data-act="sync">Dock &amp; sync</button>`;
    h += `<div class="hist"><h4>Dock history</h4>${(S.meta.history || []).map((d) => `<div><span>${BB.agoLong(d.at)}</span><span>↑ ${d.up} votes · ↓ ${d.down} posts</span></div>`).join('') || '<div><span>none yet</span><span></span></div>'}</div>
      ${insecureNote()}${!LOCAL ? `<p class="reef-note" style="margin-top:14px">paired as ${esc(api.deviceName())} · <button class="linkish" data-act="unpair">unpair</button></p>` : ''}</div>`;
    return h;
  }

  function pairHTML() {
    return `<div class="ph-title"><h2>Pair with your Angler</h2><p>Once, so this phone can dock. The PC app shows the code under Settings.</p></div>
      <form class="dock form" id="pair"><label>Angler address<input name="base" value="${esc(location.origin)}"></label>
      <label>Pairing code<input name="code" inputmode="numeric" maxlength="6" placeholder="6 digits" required></label>
      <label>Call this phone<input name="name" value="my phone"></label><button class="btn block">Pair</button>${insecureNote()}</form>`;
  }

  // ---------- frame ----------
  const unpaired = () => !LOCAL && !api.paired();
  function render() {
    const keep = body ? body.scrollTop : 0, pending = S.pending().length;
    const needsPair = unpaired();
    const pill = pending ? `<span class="pill warm"><span class="dot"></span>${pending} queued</span>` : `<span class="pill"><span class="dot"></span>${S.meta.lastDock ? 'docked ' + BB.ago(S.meta.lastDock) : 'not docked'}</span>`;
    const content = needsPair ? pairHTML() : ui.tab === 'dock' ? dockHTML() : ui.tab === 'residents' && !ui.profile && !ui.single ? residentsHTML()
      : ui.single ? `<div class="prof" style="padding:4px 0 0"><button class="back" data-act="back" style="padding-left:18px">‹ back</button></div>${S.post(ui.single) ? postHTML(S.post(ui.single)) : ''}`
      : ui.profile ? profileHTML() : feedHTML();
    root.innerHTML = `<div class="ph-head"><span class="wordmark">bait<i>book</i></span>${pill}</div><div class="ph-body">${content}</div>
      ${needsPair ? '' : `<nav class="ph-nav"><button class="${ui.tab === 'feed' ? 'on' : ''}" data-act="tab" data-tab="feed">${I.feed}Feed</button><button class="${ui.tab === 'residents' ? 'on' : ''}" data-act="tab" data-tab="residents">${I.people}Residents</button><button class="${ui.tab === 'dock' ? 'on' : ''}" data-act="tab" data-tab="dock">${I.dock}Dock${pending ? `<span class="badge">${pending}</span>` : ''}</button></nav>`}`;
    body = root.querySelector('.ph-body');
    body.scrollTop = keep;
  }

  async function sync() {
    ui.sync = { step: 0, detail: '' }; render();
    try {
      ui.last = await S.dock((step, detail) => { ui.sync = { step, detail }; render(); });
      ui.sync = null; ui.tab = 'feed'; ui.profile = null; ui.single = null; render(); body.scrollTop = 0;
      BB.toast(`Docked · ↑ ${ui.last.up} vote${ui.last.up === 1 ? '' : 's'} home · ↓ ${ui.last.down} new post${ui.last.down === 1 ? '' : 's'}`);
      // the Angler answered, so look for a new release of the app while it's here
      navigator.serviceWorker?.getRegistration().then((r) => r && r.update()).catch(() => {});
    } catch (err) {
      ui.sync = null; render();
      BB.toast(err.status === 401 ? 'This phone needs pairing again' : `Couldn’t reach the Angler (${err.message})`);
      if (err.status === 401) { api.forget(); render(); }
    }
  }

  function burst(id) {
    const f = root.querySelector(`.frame[data-id="${id}"]`);
    if (!f) return;
    const b = document.createElement('div'); b.className = 'burst'; b.innerHTML = '<span>bite!</span>';
    f.appendChild(b); setTimeout(() => b.remove(), 950);
  }

  async function onClick(e) {
    const t = e.target.closest('[data-act]');
    if (!t) return;
    const act = t.dataset.act, id = +t.dataset.id;
    if (act === 'vote') { if (!S.post(id)) return; const before = S.post(id).vote, v = +t.dataset.v; await S.vote(id, v); render(); if (before !== v && v > 0) burst(id); }
    else if (act === 'snag') {
      await S.snag(id); if (ui.single === id) ui.single = null; render();
      BB.toast(`Snagged #${id}: gone at the next dock, not a throw-back`, { label: 'undo', fn: async () => {
        if (await S.snag(id, false)) render();
        else BB.toast(`#${id} went home at the dock: the PC app’s Ledger can undo it until the next night shift`);
      } });
    }
    else if (act === 'peek') { ui.peek[id] = !ui.peek[id]; render(); }
    else if (act === 'profile') { ui.profile = t.dataset.res; ui.single = null; render(); body.scrollTop = 0; }
    else if (act === 'single' || act === 'reef-node') { ui.single = id; render(); body.scrollTop = 0; }
    else if (act === 'back') { if (ui.single) ui.single = null; else { ui.profile = null; ui.reefFocus = null; } render(); }
    else if (act === 'pview') { ui.profView = t.dataset.v; render(); }
    else if (act === 'reef-from') { ui.profile = S.post(id).resident_id; ui.single = null; ui.profView = 'reef'; ui.reefFocus = id; render(); body.scrollTop = 0; }
    else if (act === 'tab') { ui.tab = t.dataset.tab; ui.profile = null; ui.single = null; render(); body.scrollTop = 0; }
    else if (act === 'sync' && ui.sync === null) sync();
    else if (act === 'unpair' && confirm('Unpair this phone? Its posts stay until the next pairing.')) { await api.post('/api/unpair').catch(() => {}); api.forget(); render(); }
  }

  async function onSubmit(e) {
    if (e.target.id !== 'pair') return;
    e.preventDefault();
    const f = e.target, base = f.base.value.trim().replace(/\/$/, '');
    try {
      const res = await fetch(base + '/api/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: f.code.value, name: f.name.value }) });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || 'pairing failed');
      api.remember(base, json.token, json.name);
      ui.tab = 'dock'; render(); BB.toast('Paired. Dock to bring the catch in.');
    } catch (err) { BB.toast(err.message); }
  }

  async function start() {
    root = document.getElementById('device');
    root.addEventListener('click', onClick);
    root.addEventListener('submit', onSubmit);
    root.addEventListener('dblclick', async (e) => {
      const f = e.target.closest('.frame[data-act="dbl"]'), p = f && S.post(+f.dataset.id);
      if (!p || p.kind === 'seed') return;
      if (p.vote !== 1) { await S.vote(p.id, 1); render(); }
      burst(p.id);
    });
    await S.load();
    await S.warmImages();
    render();
    // offline mode; over plain http there's none, and the pair and dock screens say so (insecureNote)
    if ('serviceWorker' in navigator && window.isSecureContext) {
      const sw = navigator.serviceWorker, hadController = !!sw.controller;
      sw.register('sw.js').catch(() => {});
      // a new release took over (see sw.js): switch to it the next time the app is out of sight, never mid-dock
      sw.addEventListener('controllerchange', () => {
        if (hadController) document.addEventListener('visibilitychange', () => { if (document.hidden && ui.sync === null) location.reload(); });
      });
    }
    // keep "docked 5m" and the like current, but never under someone typing: a redraw wipes a
    // half-typed pairing code and closes the keyboard (the pair screen has nothing to update anyway)
    setInterval(() => {
      const f = document.activeElement;
      if (unpaired() || (f && root.contains(f) && f.matches('input, textarea, select'))) return;
      render();
    }, 60000);
  }
  start();
})(window.BB);
