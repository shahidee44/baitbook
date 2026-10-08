// Night shift: run one now, watch it work, set the schedule, see history + journal.
(function (BB) {
  const api = BB.api, app = BB.app, esc = BB.esc;
  let cfg = null;

  const STAGE = { queued: 'queued', writing: 'writing…', written: 'prompt ready · waiting for the image model', editing: 'editing (i2i)…', packed: 'packed', failed: 'failed', stopped: 'not made: the shift stopped' };
  const VARIATION = { subtle: 'one visible change per edit', bold: 'two changes per edit: a clearly different picture', wild: 'three changes, the style included' };
  const SNAGS = (s) => (s.n_snags ? ` · ${s.n_snags} snagged (${BB.pct(s.n_snags / Math.max(1, s.n_posts))})` : '');
  const ENDED = (s) => (s.status === 'done' ? `${s.n_posts} posts${SNAGS(s)}${s.finished_at ? ' · ' + Math.round(s.finished_at - s.started_at) + 's' : ''}`
    : s.status === 'running' ? 'running' : `${s.status}${s.n_posts ? ` · ${s.n_posts} posts` : ''}`);

  function pipeHTML(e) {
    if (e.job !== 'shift' || !e.items.length) return '<p class="empty">Nothing running. Run a shift, or wait for the schedule.</p>';
    const phase = e.state === 'error' ? `The last shift stopped: ${e.error}` : e.phase + (e.detail ? ' · ' + e.detail : '');
    return `<div class="phase">${esc(phase)}</div>` + e.items.map((it) => {
      const done = it.stage === 'packed';
      return `<div class="job"><div class="thumbbox"><img class="${done ? '' : 'src-ghost'}" src="${api.img(done ? it.image : it.source_image, true)}" alt=""></div><div>
        <div class="job-meta">@${esc(it.handle)} <span class="from">edit ${it.turn} of ${it.turns} · from #${it.source_id}${it.change ? ' · changing ' + esc(it.change) : ''}</span><span class="st ${done ? 'done' : ''}">${STAGE[it.stage] || it.stage}</span></div>
        <code class="${it.stage === 'writing' ? 'caret' : ''}">${esc(it.prompt)}</code>
        ${it.caption ? `<div class="why">“${esc(it.caption)}”</div>` : ''}${it.error ? `<div class="why" style="color:var(--clay-deep)">${esc(it.error)}</div>` : ''}</div></div>`;
    }).join('');
  }

  function controlsHTML() {
    const s = cfg.shift, e = app.status.engine, busy = e.state !== 'idle' && e.state !== 'error';
    return `<div class="card"><h2>Night shift</h2>
      <p class="sub">Every post is an edit of an older one. For each resident, ${esc(app.status.models.text)} gets the prompt primer, their base prompts, up to ${s.good_examples} random bites${+s.bad_examples ? ` and ${s.bad_examples} random throw-backs` : ' (no throw-backs)'}, then writes a prompt and a caption for each new post. It unloads, and ${esc(app.status.models.image)} renders every edit. The two run in series. Snagged posts are never shown to it.</p>
      <div class="knob">Posts / resident<input type="range" min="1" max="8" step="1" value="${s.posts_per_resident}" data-cfg="shift.posts_per_resident"><output>${s.posts_per_resident}</output></div>
      <div class="sched"><span>Examples each night: up to</span><input type="number" min="0" max="10" step="1" value="${esc(s.good_examples)}" data-cfg="shift.good_examples"><span>bites and</span>
        <input type="number" min="0" max="10" step="1" value="${esc(s.bad_examples)}" data-cfg="shift.bad_examples"><span>throw-backs</span></div>
      <div class="sched"><span>Variation</span><select data-cfg="shift.variation">${Object.keys(VARIATION).map((v) => `<option value="${v}" ${s.variation === v ? 'selected' : ''}>${v}</option>`).join('')}</select>
        <small class="hint-inline">${esc(VARIATION[s.variation] || '')}</small></div>
      <div class="sched"><label class="switch"><input type="checkbox" data-cfg="shift.schedule_enabled" ${s.schedule_enabled ? 'checked' : ''}><span></span></label>
        <span>Run every night at</span><input type="time" value="${esc(s.schedule_time)}" data-cfg="shift.schedule_time" ${s.schedule_enabled ? '' : 'disabled'}></div>
      <div class="sched"><span>If the PC slept through it, catch up within</span><input type="number" min="0" max="24" step="1" value="${esc(s.catch_up_hours ?? 6)}" data-cfg="shift.catch_up_hours" ${s.schedule_enabled ? '' : 'disabled'}><span>hours</span></div>
      <div class="shift-actions"><button class="btn" data-act="run" ${busy ? 'disabled' : ''}>${busy ? 'Working…' : 'Run night shift now'}</button>
        <small>${app.residents.filter((r) => r.status === 'ready').length * s.posts_per_resident} posts</small></div>
      <div class="pipe" id="pipe">${pipeHTML(e)}</div></div>`;
  }

  BB.views.shift = {
    async render() {
      cfg = await api.get('/api/config');
      const [shifts, journal] = await Promise.all([api.get('/api/shifts'), api.get('/api/journal')]);
      return `<div class="cols"><div class="stack" id="shift-controls">${controlsHTML()}</div>
        <div class="stack">
          <div class="card"><h2>History</h2>${shifts.length ? `<div class="hist">${shifts.map((s) => `<div title="${esc(s.error || '')}"><span>#${s.id} · ${BB.agoLong(s.started_at)} · ${esc(s.trigger)}</span><span>${esc(ENDED(s))}</span></div>`).join('')}</div>` : '<p class="empty">No shifts yet.</p>'}</div>
          <div class="card"><h2>Angler’s journal</h2><div class="journal">${journal.map((j) => `<div class="entry"><span class="glyph">${{ shift: '✶', resident: '+', dock: '⇅', device: '⌁', error: '!' }[j.kind] || '·'}</span><div>${esc(j.text)}<small>${BB.agoLong(j.at)}</small></div></div>`).join('') || '<p class="empty">Quiet so far.</p>'}</div></div>
        </div></div>`;
    },
    async onClick(act) {
      if (act !== 'run') return;
      const r = await api.post('/api/shift');
      BB.toast(r.ok ? 'Night shift queued' : r.reason);
      app.poll();
    },
    onInput(e) {
      const k = e.target.dataset.cfg;
      if (k && e.target.type === 'range') e.target.nextElementSibling.textContent = e.target.value;
    },
    async onChange(e) {
      const k = e.target.dataset.cfg;
      if (!k) return;
      const [sec, key] = k.split('.');
      const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
      try { cfg = await api.put('/api/config', { [sec]: { [key]: value } }); }
      catch (err) { BB.toast(err.message); return; }
      BB.toast(key === 'schedule_enabled' ? (value ? 'Scheduled shifts on' : 'Scheduled shifts off') : 'Saved');
      const c = document.getElementById('shift-controls');
      if (c) c.innerHTML = controlsHTML();
    },
    onStatus(s) {
      const pipe = document.getElementById('pipe');
      if (pipe) pipe.innerHTML = pipeHTML(s.engine);
      const btn = document.querySelector('[data-act="run"]');
      const busy = s.engine.state !== 'idle' && s.engine.state !== 'error';
      if (btn) { btn.disabled = busy; btn.textContent = busy ? 'Working…' : 'Run night shift now'; }
    },
  };
})(window.BB);
