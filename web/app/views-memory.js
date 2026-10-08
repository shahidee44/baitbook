// Memory: what the Angler knows about one resident and exactly what it sends.
// Ledger: every prompt it has written, with your verdict.
(function (BB) {
  const api = BB.api, app = BB.app, esc = BB.esc;

  const picker = (act, current) => `<div class="who-pick">${app.residents.map((r) => `<button class="${r.id === current ? 'on' : ''}" data-act="${act}" data-res="${r.id}">${app.avatar(r, '')}${esc(r.name.split(' ')[0])}</button>`).join('')}</div>`;

  // a prompt shows its first lines until asked for; the toggle hides itself when the whole prompt fits (see after)
  const prompt = (text) => `<div class="prompt"><code>${esc(text)}</code><button class="linkish" data-act="prompt" aria-expanded="false">full prompt</button></div>`;

  const list = (posts, picked, empty) => posts.length
    ? `<div class="favs">${[...posts].sort((a, b) => picked.has(b.id) - picked.has(a.id)).slice(0, 14).map((p) => `<div class="fav ${picked.has(p.id) ? '' : 'unpicked'}"><img src="${api.img(p.image, true)}" alt=""><div><b>#${p.id}</b> ${picked.has(p.id) ? '<span class="mode picked">in tonight’s sample</span>' : ''}${prompt(p.prompt)}</div></div>`).join('')}</div>`
    : `<p class="empty">${empty}</p>`;

  BB.views.memory = {
    async render(params) {
      if (!app.residents.length) return '<p class="empty" style="padding:40px">No residents yet.</p>';
      const rid = params.res && app.res(params.res) ? params.res : app.residents[0].id;
      app.params.res = rid;
      const m = await api.get(`/api/residents/${rid}/memory`);
      const r = m.resident, first = esc(r.name.split(' ')[0]);
      const good = new Set(m.good), bad = new Set(m.bad);
      return `${picker('pick', rid)}
        <div class="cols">
          <div class="card"><h2>Where ${first} started</h2><p class="sub">${esc(app.status.models.text)} looked at each seed image and wrote a prompt that could generate it. They never change. Mood tags: ${(r.moods || []).map((t) => `<span class="chip">${esc(t)}</span>`).join(' ')}</p>
            <div class="reads">${r.seeds.map((s) => `<div class="read"><img src="${api.img(s.image, true)}" alt="">${prompt(s.prompt || '…')}</div>`).join('')}</div></div>
          <div class="card"><h2>What the Angler sends</h2><p class="sub">One plain chat per resident per night, no tools. This is ${first}’s system prompt as it would be tonight: the primer, the base prompts, and a fresh random draw of up to ${m.n_good} bites${m.n_bad ? ` and ${m.n_bad} throw-backs` : ' (throw-backs are off)'}. Snags are never in it. Then one turn per post shows the model an old image’s prompt and what to change, followed by a caption.</p>
            <p class="sect-label">system prompt</p><pre class="tpl tall">${esc(m.system)}</pre>
            <div class="shift-actions"><button class="btn soft sm" data-act="reroll">Re-roll the sample</button></div></div>
        </div>
        <div class="cols" style="margin-top:20px">
          <div class="card"><h2>Bites <span class="count">${m.bites.length}</span></h2><p class="sub">Up to ${m.n_good} are picked at random each night as good examples.</p>${list(m.bites, good, 'No bites yet.')}</div>
          <div class="card avoid"><h2>Throw-backs <span class="count">${m.throws.length}</span></h2><p class="sub">${m.n_bad ? `Up to ${m.n_bad} are picked at random each night as bad examples.` : 'Bad examples are off (0 on the Night shift tab), so none are sent.'} Thrown-back images are never edited again either way. A broken render belongs in a snag, not here.</p>${list(m.throws, bad, 'Nothing thrown back yet.')}</div>
        </div>`;
    },
    after() {
      document.querySelectorAll('.prompt').forEach((p) => { const c = p.firstElementChild; p.classList.toggle('fits', c.scrollHeight <= c.clientHeight); });
    },
    onClick(act, t) {
      if (act === 'pick') app.go('memory', { res: t.dataset.res });
      if (act === 'reroll') app.render();
      if (act === 'prompt') {  // in place: a re-render would re-roll tonight's sample
        const open = t.parentElement.classList.toggle('open');
        t.textContent = open ? 'less' : 'full prompt';
        t.setAttribute('aria-expanded', open);
      }
    },
  };

  let filter = { res: '', kind: '' };
  BB.views.ledger = {
    async render() {
      const snags = filter.kind === 'snagged';
      const q = new URLSearchParams({ limit: 400, ...(filter.res ? { resident: filter.res } : {}), ...(snags ? { snagged: true } : filter.kind ? { kind: filter.kind } : {}) });
      const rows = await api.get('/api/posts?' + q);
      const chip = (k, v, label) => `<button class="chip ${filter[k] === v ? 'on' : ''}" data-act="filter" data-k="${k}" data-v="${v}">${label}</button>`;
      const verdict = (p) => (!p.snagged ? app.verdict(p) : p.image ? `${app.verdict(p)}<br><button class="linkish" data-act="unsnag" data-id="${p.id}">undo</button>` : `${app.verdict(p)}<br><small style="color:var(--muted)">images cleared</small>`);
      return `<div class="card"><h2>Prompt ledger</h2><p class="sub">${snags ? 'Snagged posts: broken renders, out of every feed and never learned from. Undo works until the next night shift clears their images.' : 'Every prompt the Angler has written or read, newest first.'}</p>
        <div class="filters">${chip('res', '', 'everyone')}${app.residents.map((r) => chip('res', r.id, '@' + esc(r.handle))).join('')}</div>
        <div class="filters">${chip('kind', '', 'all')}${chip('kind', 'seed', 'seed images')}${chip('kind', 'edit', 'edits')}${chip('kind', 'snagged', 'snagged')}</div>
        <table class="table"><thead><tr><th></th><th>#</th><th>Resident</th><th>Prompt</th><th>Caption</th><th>Verdict</th><th>Night</th></tr></thead><tbody>
        ${rows.map((p) => `<tr><td>${p.image ? `<img src="${api.img(p.image, true)}" alt="">` : ''}</td><td>${p.id}</td><td class="res">@${esc((app.res(p.resident_id) || {}).handle || '?')}</td>
          <td><code title="${esc(p.prompt)}">${esc(p.prompt)}</code>${p.source_id ? `<small style="color:var(--muted)">edited from #${p.source_id}</small>` : ''}</td>
          <td>${esc(p.caption)}</td><td>${verdict(p)}</td><td>${p.shift_id || '—'}</td></tr>`).join('') || `<tr><td colspan="7" class="empty">${snags ? 'Nothing snagged.' : 'Nothing yet.'}</td></tr>`}</tbody></table></div>`;
    },
    onClick(act, t) {
      if (act === 'filter') { filter[t.dataset.k] = t.dataset.v; app.render(); }
      if (act === 'unsnag') app.unsnag(+t.dataset.id);
    },
  };
})(window.BB);
