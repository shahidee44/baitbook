// Feed: the catch, grouped by night shift, newest first. Same finite feed as
// the phone, but live — votes go straight to the Angler.
(function (BB) {
  const api = BB.api, app = BB.app, esc = BB.esc;
  let posts = [];
  const open = {}; // post id -> chain (or null while loading)

  function sideHTML() {
    const s = app.status, e = s.engine;
    const c = s.counts;
    return `<aside class="feed-side">
      <div class="card"><h2>The Angler</h2>
        <p class="sub">${e.state === 'idle' ? 'Idle.' : e.state === 'error' ? `The last job stopped: ${esc(e.error)}` : esc(e.phase || e.state) + (e.detail ? ' · ' + esc(e.detail) : '')}</p>
        <div class="stats"><div><b>${c.residents}</b><span>residents</span></div><div><b>${c.posts}</b><span>edits</span></div><div><b>${c.votes ? BB.pct(c.bites / c.votes) : '—'}</b><span>bite rate</span></div>
          <div title="Share of every edit made that was snagged as a broken render: a measure of the image model, not of your taste"><b>${c.posts + c.snags ? BB.pct(c.snags / (c.posts + c.snags)) : '—'}</b><span>snag rate</span></div></div>
        <p class="sub" style="margin:0">${e.next_run ? `Next night shift at <b>${e.next_run}</b>.` : 'Scheduled shifts are off.'} <button class="linkish" data-go="shift">night shift →</button></p></div>
      <div class="card side-res"><h2>Residents</h2><div class="res-list">${app.residents.map((r) => `<button class="res-row" data-act="to-res" data-res="${r.id}">${app.avatar(r, '')}<div><b>${esc(r.name)}</b><small>@${esc(r.handle)}${r.status === 'reading' ? ' · reading…' : ''}</small></div>${r.n_votes ? `<em>${BB.pct(r.n_bites / r.n_votes)}</em>` : ''}</button>`).join('') || '<p class="empty">No residents yet. <button class="linkish" data-go="residents">add one →</button></p>'}</div></div>
    </aside>`;
  }

  function feedHTML() {
    if (!posts.length) return `<div class="end-card"><h3>Nothing to see yet.</h3><p>Add a resident with 5 images, then run a night shift.</p><button class="btn" data-go="residents">Add a resident</button></div>`;
    const nights = [...new Set(posts.map((p) => (p.kind === 'seed' ? 0 : p.shift_id)))].sort((a, b) => b - a);
    return nights.map((n) => {
      const ps = posts.filter((p) => (p.kind === 'seed' ? 0 : p.shift_id) === n);
      const unvoted = ps.filter((p) => p.kind !== 'seed' && !p.vote).length;
      return `<div class="batch-head"><h3>${n ? `Night shift #${n}` : 'Seed images'}</h3><span>${n ? `${ps.length} posts · ${unvoted ? unvoted + ' unvoted' : 'all voted'}` : 'what each resident started from'}</span></div>
        ${ps.map((p) => app.postCard(p, p.id in open, open[p.id])).join('')}`;
    }).join('') + `<div class="end-card"><h3>That’s the whole catch.</h3><p>baitbook doesn’t scroll forever. The Angler writes the next one tonight.</p></div>`;
  }

  BB.views.feed = {
    async render() {
      posts = await api.get(`/api/posts?days=7&limit=400`);
      return `<div class="feed-layout"><div class="feed-col" id="feed-col">${feedHTML()}</div>${sideHTML()}</div>`;
    },
    async onClick(act, t) {
      if (act !== 'peek') return;
      const id = +t.dataset.id;
      if (id in open) delete open[id];
      else open[id] = (await api.get(`/api/posts/${id}`)).chain;
      const card = document.querySelector(`article[data-post="${id}"]`);
      const p = posts.find((x) => x.id === id);
      if (card && p) card.outerHTML = app.postCard(p, id in open, open[id]);
    },
    onVote(id, v) { const p = posts.find((x) => x.id === id); if (p) p.vote = v; },
    onSnag(id) { posts = posts.filter((x) => x.id !== id); },
    onStatus() { const side = document.querySelector('.feed-side'); if (side) side.outerHTML = sideHTML(); },
  };
})(window.BB);
