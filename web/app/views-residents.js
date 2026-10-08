// Residents: who lives here, their 5 seed images and mood tags, editing who they
// are, and the new-resident flow (upload 5 images → the vision read → edit the tags).
(function (BB) {
  const api = BB.api, app = BB.app, esc = BB.esc;
  let files = [];          // the new resident's images, in order
  let watching = null;     // resident id whose vision read we're showing
  let editing = null;      // resident id whose name, handle and bio are open for editing

  const chips = (tags) => `<div class="chips">${(tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join('') || '<span class="chip">no tags yet</span>'}</div>`;

  function readHTML(r) {
    const e = app.status.engine;
    const live = e.job === 'read' && e.resident === r.id;
    const items = live ? e.items : null;
    const T = app.status.models.text;
    if (r.status !== 'reading') return '';
    return `<p class="sect-label" style="margin-top:12px">${esc(T)} · one conversation: ${r.seeds.length} images, then mood tags</p>
      <div class="reads">${r.seeds.map((s, i) => {
        const it = items && items[i];
        const stage = it ? it.stage : (live ? 'waiting' : 'queued');
        return `<div class="read ${stage === 'done' ? '' : 'pending'}"><img src="${api.img(s.image, true)}" alt=""><code class="${stage === 'writing' ? 'caret' : ''}">${esc(it && it.prompt ? it.prompt : stage === 'queued' ? 'queued behind another job' : '')}</code></div>`;
      }).join('')}
      <div class="read tagsread ${items && items[items.length - 1].stage === 'done' ? '' : 'pending'}"><span class="stack">${r.seeds.map((s) => `<img src="${api.img(s.image, true)}" alt="">`).join('')}</span>
        <div class="chips ${items && items[items.length - 1].stage === 'writing' ? 'caret' : ''}"></div></div></div>`;
  }

  // who they are: as shown, or open for editing
  const who = (r) => editing === r.id ? `<form class="form resform" data-res="${r.id}">
      <label>Name<input name="name" required value="${esc(r.name)}"></label>
      <label>Handle<input name="handle" required value="${esc(r.handle)}"></label>
      <label>Bio<input name="bio" value="${esc(r.bio)}"></label>
      <p class="hint">The next night shift writes with these; the phone gets them when it next docks.</p>
      <div class="edit-actions"><button class="btn sm">Save</button><button type="button" class="btn soft sm" data-act="edit-cancel" data-res="${r.id}">Cancel</button></div></form>`
    : `<div class="top">${app.avatar(r, '')}<div><b>${esc(r.name)}</b><small>@${esc(r.handle)}</small></div><button class="linkish" data-act="edit" data-res="${r.id}">edit</button></div>
      <p class="bio">${esc(r.bio)}</p>`;

  function card(r) {
    const rate = (r.n_votes ? BB.pct(r.n_bites / r.n_votes) + ' bite' : 'no votes yet') + (r.n_snags ? ` · ${BB.pct(r.n_snags / (r.n_posts + r.n_snags))} snagged` : '');
    return `<div class="card res-card ${app.params.focus === r.id ? 'focus-card' : ''}" id="res-${r.id}">
      ${who(r)}
      <p class="sect-label">${r.seeds.length} seed images</p><div class="seeds">${r.seeds.map((s) => `<img src="${api.img(s.image, true)}" title="${esc(s.prompt)}" alt="">`).join('')}</div>
      ${r.status === 'reading' ? readHTML(r) : r.status === 'failed' ? `<p class="sect-label">the vision read failed</p>
        <p class="hint">It stopped before writing every base prompt; the Night shift tab’s journal says why. The seed images are all here, and night shifts skip @${esc(r.handle)} until a read finishes.</p>
        <button class="btn soft sm" data-act="reread" data-res="${r.id}">Read again</button>` : `<p class="sect-label">mood tags</p>
        <form class="tagform" data-res="${r.id}"><input name="moods" value="${esc((r.moods || []).join(', '))}" title="3–5 tags, comma-separated"><button class="btn soft sm">save</button></form>`}
      <div class="foot"><span>${r.n_posts} edits · ${rate}</span>
        <span><button class="linkish" data-act="mem" data-res="${r.id}">memory →</button> · <button class="linkish" data-act="reef" data-res="${r.id}">reef →</button>${r.status === 'ready' ? ` · <button class="linkish" data-act="reread" data-res="${r.id}" data-ask="1">re-read</button>` : ''} · <button class="linkish" data-act="del" data-res="${r.id}">remove</button></span></div></div>`;
  }

  function newHTML() {
    return `<form class="card new-card nr form" id="newres"><h2>New resident</h2>
      <p class="sub">Give them a name and 5 images. ${esc(app.status.models.text)} looks at each image and writes a ${esc(app.status.models.image)} prompt that could generate it (their base prompts), then gives 3–5 mood tags for all five together. After that, every post is an edit of one of these images or a later post.</p>
      <div class="row2"><label>Name<input name="name" required placeholder="Wren Ashby"></label><label>Handle<input name="handle" required placeholder="wren.draws"></label></div>
      <label>Bio<input name="bio" placeholder="sketchbook in every bag"></label>
      <label class="drop" id="drop">Seed images · ${files.length}/5<input type="file" name="images" accept="image/*" multiple class="visually-hidden">
        <span class="drop-hint">${files.length ? 'click or drop to replace' : 'click or drop 5 images here'}</span>
        <div class="picker">${files.map((f, i) => `<div class="pick on"><img src="${URL.createObjectURL(f)}" alt=""><span class="num">${i + 1}</span></div>`).join('')}</div></label>
      <div class="shift-actions"><button class="btn" ${files.length === 5 ? '' : 'disabled'}>Add resident &amp; read with ${esc(app.status.models.text)}</button><small>the read takes a few minutes; it runs on the Angler</small></div>
    </form>`;
  }

  function redraw(rid) {
    const r = app.res(rid), el = document.getElementById('res-' + rid);
    if (r && el) el.outerHTML = card(r);
  }

  BB.views.residents = {
    async render() {
      editing = null;
      await app.loadResidents();
      return `<div class="res-grid">${app.residents.map(card).join('')}${newHTML()}</div>`;
    },
    after() {
      const f = app.params.focus && document.getElementById('res-' + app.params.focus);
      if (f) f.scrollIntoView({ block: 'center' });
      const drop = document.getElementById('drop');
      if (!drop) return;
      ['dragover', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
        e.preventDefault();
        if (ev === 'drop') take([...e.dataTransfer.files]);
      }));
    },
    onChange(e) { if (e.target.name === 'images') take([...e.target.files]); },
    async onSubmit(e) {
      e.preventDefault();
      const f = e.target;
      if (f.classList.contains('tagform')) {
        await api.patch(`/api/residents/${f.dataset.res}`, { moods: f.moods.value });
        BB.toast('Mood tags saved');
        return;
      }
      if (f.classList.contains('resform')) {
        f.querySelector('.btn').disabled = true;
        try {
          const r = await api.patch(`/api/residents/${f.dataset.res}`, { name: f.name.value, handle: f.handle.value, bio: f.bio.value });
          app.residents = app.residents.map((x) => (x.id === r.id ? r : x));
          editing = null; redraw(r.id);
          BB.toast(`@${r.handle} saved`);
        } catch (err) { BB.toast(err.message); f.querySelector('.btn').disabled = false; }
        return;
      }
      if (f.id !== 'newres' || files.length !== 5) return;
      const fd = new FormData();
      ['name', 'handle', 'bio'].forEach((k) => fd.append(k, f[k].value));
      files.forEach((file) => fd.append('images', file));
      f.querySelector('.btn').disabled = true;
      try {
        const r = await api.req('POST', '/api/residents', fd);
        files = []; watching = r.id;
        BB.toast(`@${r.handle} added — the vision read is running`);
        app.params = { focus: r.id };
        await app.render(); app.poll();
      } catch (err) { BB.toast(err.message); f.querySelector('.btn').disabled = false; }
    },
    async onClick(act, t) {
      const rid = t.dataset.res;
      if (act === 'mem') app.go('memory', { res: rid });
      if (act === 'reef') app.go('reef', { res: rid });
      if (act === 'edit' || act === 'edit-cancel') {
        editing = act === 'edit' ? rid : null;
        redraw(rid);
        if (editing) document.querySelector(`#res-${rid} .resform`).name.focus();
      }
      if (act === 'del' && confirm('Remove this resident and every image they have? This can’t be undone.')) {
        await api.del(`/api/residents/${rid}`); BB.toast('Removed'); app.render();
      }
      if (act === 'reread' && (!t.dataset.ask || confirm('Read the seed images again? This rewrites the base prompts and the mood tags (your own tag edits too). Posts already made stay as they are.'))) {
        try { await api.post(`/api/residents/${rid}/read`); BB.toast('Vision read queued'); app.params = { focus: rid }; await app.render(); app.poll(); }
        catch (err) { BB.toast(err.message); }
      }
    },
    onStatus(s) {
      const e = s.engine;
      if (e.job === 'read' && e.resident && e.resident !== editing) redraw(e.resident);  // not over what's being typed
    },
  };

  function take(list) {
    files = list.filter((f) => f.type.startsWith('image/')).slice(0, 5);
    const form = document.getElementById('newres');
    const keep = form ? { name: form.name.value, handle: form.handle.value, bio: form.bio.value } : {};
    const refocus = form && document.activeElement === form.images;  // chosen from the keyboard: stay on the picker
    if (form) form.outerHTML = newHTML();
    const f2 = document.getElementById('newres');
    Object.entries(keep).forEach(([k, v]) => { f2[k].value = v; });
    if (refocus) f2.images.focus();
    BB.views.residents.after();
  }
})(window.BB);
