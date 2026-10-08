// Reef: a resident's edit graph as coral, with a night-by-night replay.
(function (BB) {
  const api = BB.api, app = BB.app, esc = BB.esc;
  let nodes = [], st = { res: null, t: null, focus: null, grow: null, playing: false };

  function cardHTML() {
    const n = st.focus && nodes.find((x) => x.id === st.focus);
    if (!n) return `<div class="card reef-card"><h3>Tap any image</h3><p class="sub" style="margin:0">Its whole lineage lights up: everything it was edited from, and everything that grew from it.</p></div>`;
    const src = n.source_id && nodes.find((x) => x.id === n.source_id);
    let depth = 0; for (let c = n; c && c.source_id; c = nodes.find((x) => x.id === c.source_id)) depth++;
    const grew = nodes.filter((x) => { for (let c = x; c && c.source_id; c = nodes.find((y) => y.id === c.source_id)) if (c.source_id === n.id) return true; return false; }).length;
    const verdict = n.kind === 'seed' ? 'seed image' : n.vote > 0 ? 'bitten' : n.vote < 0 ? 'thrown back (a dead end)' : 'not voted yet';
    return `<div class="card reef-card"><img class="art" src="${api.img(n.image)}" alt="">
      <h3>#${n.id} · ${n.kind === 'seed' ? 'seed' : 'night #' + n.shift_id}</h3>
      <div class="meta">${verdict} · ${depth ? depth + ' edit' + (depth > 1 ? 's' : '') + ' from its seed' : 'a root of the reef'} · ${grew} grew from it</div>
      ${src ? `<p class="sect-label">edited from #${src.id}</p><div class="src"><img src="${api.img(src.image, true)}" alt=""><span>${esc((src.prompt || '').slice(0, 90))}…</span></div>` : ''}
      <p class="sect-label">prompt</p><code>${esc(n.prompt || '')}</code>
      <div class="shift-actions"><button class="btn soft sm" data-act="clear">Clear</button></div></div>`;
  }

  function stage() {
    return `<div class="reef-grid"><div class="reef-scroll">${BB.reef.html(nodes, { upTo: st.t, focus: st.focus, grow: st.grow, minWidth: 620 })}</div>${cardHTML()}</div>`;
  }

  function paint() {
    const top = BB.reef.rows(nodes).length - 1, t = st.t == null ? top : st.t;
    const s = document.getElementById('reef-stage'), o = document.getElementById('reef-night'), sl = document.querySelector('[data-reef-t]');
    if (s) s.innerHTML = stage();
    if (o) o.textContent = `night ${t} of ${top}`;
    if (sl) sl.value = t;
    const box = document.querySelector('.reef-scroll'), f = box && box.querySelector('.rnode.focus');
    if (f) { box.scrollLeft = f.offsetLeft - box.clientWidth / 2; box.scrollTop = f.offsetTop - box.clientHeight / 2; }
  }

  BB.views.reef = {
    async render(params) {
      if (!app.residents.length) return '<p class="empty" style="padding:40px">No residents yet.</p>';
      const rid = params.res && app.res(params.res) ? params.res : (st.res && app.res(st.res) ? st.res : app.residents[0].id);
      if (rid !== st.res) st = { res: rid, t: null, focus: null, grow: null, playing: false };
      if (params.focus) st.focus = params.focus;
      nodes = await api.get(`/api/residents/${rid}/graph`);
      const r = app.res(rid), top = BB.reef.rows(nodes).length - 1, t = st.t == null ? top : st.t;
      return `<div class="who-pick">${app.residents.map((x) => `<button class="${x.id === rid ? 'on' : ''}" data-act="pick" data-res="${x.id}">${app.avatar(x, '')}${esc(x.name.split(' ')[0])}</button>`).join('')}</div>
        <p class="sub">${esc(r.name.split(' ')[0])}’s edit graph, grown like coral. Seed images sit on the seabed and every edit grows up from the image it was made from, one row per night. Branches take the colour of the image they lead to. Bites bloom; throw-backs bleach, and since thrown-back images are never edited again, a bleached tip is where a branch died.</p>
        <div class="reef-legend"><span><i class="l-seed"></i>seed</span><span><i class="l-bit"></i>bitten</span><span><i class="l-thrown"></i>thrown back</span><span><i class="l-open"></i>not voted</span></div>
        <div class="reef-tools"><button class="btn sm" data-act="play">▶ Grow it</button>
          <input type="range" min="0" max="${top}" step="1" value="${t}" data-reef-t><output id="reef-night">night ${t} of ${top}</output></div>
        <div id="reef-stage">${stage()}</div>`;
    },
    after: paint,
    onClick(act, t) {
      if (act === 'pick') app.go('reef', { res: t.dataset.res });
      if (act === 'reef-node') { const id = +t.dataset.id; st.focus = st.focus === id ? null : id; st.grow = null; paint(); }
      if (act === 'clear') { st.focus = null; paint(); }
      if (act === 'play' && !st.playing) {
        const top = BB.reef.rows(nodes).length - 1;
        st.playing = true; st.t = 0; st.grow = null; paint();
        const step = () => {
          if (app.view !== 'reef' || !st.playing) { st.playing = false; return; }
          if (st.t >= top) { st.playing = false; st.grow = null; st.t = null; paint(); return; }
          st.t++; st.grow = st.t; paint(); setTimeout(step, 1300);
        };
        setTimeout(step, 700);
      }
    },
    onInput(e) { if (e.target.dataset.reefT !== undefined) { st.t = +e.target.value; st.grow = null; paint(); } },
  };
})(window.BB);
