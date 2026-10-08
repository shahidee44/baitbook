// The Reef: a resident's edit graph drawn as coral. Seed images sit on the
// seabed; every edit grows upward from the image it was made from, one row per
// night. Branches take the colour of the image they lead to. Bites bloom;
// throw-backs bleach (--bleach-line, in theme.css) — thrown-back images are
// never edited again, so a bleached tip is a dead end.
//
// nodes: [{id, source_id, shift_id, kind, vote, image, color}]
// opts.img(stem, thumb) gives each image's URL. It's the Angler's (BB.api.img)
// unless told otherwise; the phone passes its own stored copies, so its Reef
// works with no network.
window.BB = window.BB || {};
(function (BB) {
  const R = (BB.reef = {});
  const SIZE = {
    full: { col: 58, row: 96, node: 46, pad: 96, top: 36, bed: 54 },
    compact: { col: 38, row: 66, node: 30, pad: 22, top: 22, bed: 30 },
  };
  const kind = (n) => (n.kind === 'seed' ? 'seed' : n.vote > 0 ? 'bit' : n.vote < 0 ? 'thrown' : 'open');
  const mix = (hex, to, t) => {
    const a = parseInt(hex.slice(1), 16), b = parseInt(to.slice(1), 16);
    const c = [16, 8, 0].map((s) => Math.round(((a >> s) & 255) + (((b >> s) & 255) - ((a >> s) & 255)) * t));
    return '#' + c.map((v) => v.toString(16).padStart(2, '0')).join('');
  };
  // a branch: its image's colour, a little towards --cocoa (#c9a27a until the image has one)
  const branch = (n) => mix(n.color || '#c9a27a', '#7A6048', 0.15);

  // the nights this resident has posts in, seeds first
  R.rows = (nodes) => [...new Set(nodes.map((n) => (n.kind === 'seed' ? 0 : n.shift_id)))].sort((a, b) => a - b);

  R.layout = (all, opts) => {
    const Z = SIZE[opts.compact ? 'compact' : 'full'];
    const rows = R.rows(all);
    const upTo = opts.upTo == null ? rows.length - 1 : Math.min(opts.upTo, rows.length - 1);
    const rowOf = (n) => rows.indexOf(n.kind === 'seed' ? 0 : n.shift_id);
    const nodes = all.filter((n) => rowOf(n) <= upTo);
    const byId = new Map(nodes.map((n) => [n.id, n])), kids = new Map();
    nodes.forEach((n) => { if (n.source_id && byId.has(n.source_id)) { if (!kids.has(n.source_id)) kids.set(n.source_id, []); kids.get(n.source_id).push(n); } });
    const placed = new Map();
    let slot = 0;
    const place = (n) => {
      const ks = kids.get(n.id) || [];
      ks.forEach(place);
      const desc = ks.reduce((s, k) => s + 1 + placed.get(k.id).desc, 0);
      const at = ks.length ? (placed.get(ks[0].id).slot + placed.get(ks[ks.length - 1].id).slot) / 2 : slot++;
      placed.set(n.id, { n, desc, slot: at, row: rowOf(n) });
    };
    nodes.filter((n) => !n.source_id || !byId.has(n.source_id)).sort((a, b) => a.id - b.id)
      .forEach((n, i) => { if (i) slot += 0.45; place(n); });
    const natural = Z.pad * 2 + Math.max(0, slot - 1) * Z.col;
    const W = Math.max(natural, opts.minWidth || 0), shift = (W - natural) / 2;
    const H = Z.top + upTo * Z.row + Z.node + Z.bed;
    placed.forEach((p) => { p.x = shift + Z.pad + p.slot * Z.col; p.y = Z.top + (upTo - p.row) * Z.row + Z.node / 2; });
    return { Z, placed, kids, W, H, upTo, rows };
  };

  function lineage(L, id) {
    const out = new Set([id]);
    for (let p = L.placed.get(id); p && p.n.source_id && L.placed.has(p.n.source_id); p = L.placed.get(p.n.source_id)) out.add(p.n.source_id);
    const down = [...(L.kids.get(id) || [])];
    while (down.length) { const k = down.pop(); out.add(k.id); down.push(...(L.kids.get(k.id) || [])); }
    return out;
  }

  R.html = (all, opts = {}) => {
    if (!all.length) return '<p class="empty" style="padding:20px">Nothing here yet.</p>';
    const L = R.layout(all, opts), Z = L.Z, compact = !!opts.compact, img = opts.img || BB.api.img;
    const lit = opts.focus && L.placed.has(opts.focus) ? lineage(L, opts.focus) : null;
    let edges = '', nodes = '', extras = '';
    L.placed.forEach((p) => {
      const n = p.n, k = kind(n), dim = lit && !lit.has(n.id) ? ' dim' : '';
      const grow = opts.grow != null && opts.grow === p.row && p.row > 0 ? ' grow' : '';
      const parent = n.source_id && L.placed.get(n.source_id);
      if (parent) {
        const x1 = parent.x, y1 = parent.y, x2 = p.x, y2 = p.y, h = y1 - y2;
        const w = compact ? Math.min(6, 1.6 + Math.sqrt(p.desc) * 1.1) : Math.min(11, 2.6 + Math.sqrt(p.desc) * 2);
        edges += `<path class="redge ${k}${dim}${grow}" pathLength="1" d="M${x1} ${y1} C${x1} ${y1 - h * 0.55} ${x2} ${y2 + h * 0.5} ${x2} ${y2}"
          stroke="${branch(n)}" stroke-width="${w.toFixed(1)}"><title>#${parent.n.id} → #${n.id}</title></path>`;
      }
      const label = `#${n.id} · ${n.kind === 'seed' ? 'seed' : 'night #' + n.shift_id} · ${{ seed: 'seed image', bit: 'bitten', thrown: 'thrown back', open: 'not voted' }[k]}`;
      nodes += `<button class="rnode ${k}${dim}${grow}${opts.focus === n.id ? ' focus' : ''}" style="left:${p.x}px;top:${p.y}px;width:${Z.node}px;height:${Z.node}px;--glow:${mix(n.color || '#c9d8b7', '#ffffff', 0.35)}"
        data-act="reef-node" data-id="${n.id}" title="${label}"><img loading="lazy" src="${img(n.image, true)}" alt=""></button>`;
      if (k === 'bit' && !compact && !dim) for (let b = 0; b < 2; b++) extras += `<span class="bubble" style="left:${p.x + (b ? 9 : -7)}px;top:${p.y - Z.node / 2}px;animation-delay:${((n.id * 0.37 + b * 1.3) % 3).toFixed(2)}s"></span>`;
    });
    let rows = '';
    if (!compact) L.rows.slice(0, L.upTo + 1).forEach((sid, r) => {
      const y = Z.top + (L.upTo - r) * Z.row + Z.node / 2;
      rows += `<span class="rrow" style="top:${y}px">${sid ? 'night #' + sid : 'seeds'}</span><span class="rline" style="top:${y}px"></span>`;
    });
    const bedY = L.H - Z.bed * 0.72;
    const bed = `<path class="rbed" d="M0 ${bedY} Q ${L.W * 0.25} ${bedY - 10} ${L.W * 0.5} ${bedY} T ${L.W} ${bedY} V ${L.H} H 0 Z"/>`;
    return `<div class="reef${compact ? ' compact' : ''}${lit ? ' focusing' : ''}" style="width:${L.W}px;height:${L.H}px">
      <svg class="redges" width="${L.W}" height="${L.H}" viewBox="0 0 ${L.W} ${L.H}">${bed}${edges}</svg>${rows}${extras}${nodes}</div>`;
  };
})(window.BB);
