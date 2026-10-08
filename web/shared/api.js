// Talking to the Angler. Both clients use this. On the Angler's own machine no
// token is needed; anywhere else the device pairs once and keeps a token.
window.BB = window.BB || {};
(function (BB) {
  const KEY = 'baitbook.angler';
  const saved = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; } };
  const A = (BB.api = {
    // the Angler's address: this page's origin unless the device was pointed elsewhere
    base: () => (saved().base || location.origin).replace(/\/$/, ''),
    token: () => saved().token || '',
    remember: (base, token, name) => localStorage.setItem(KEY, JSON.stringify({ base, token, name })),
    forget: () => localStorage.removeItem(KEY),
    paired: () => !!saved().token,
    deviceName: () => saved().name || '',
  });

  A.req = async (method, path, body, opts = {}) => {
    const headers = {};
    if (A.token()) headers['X-Baitbook-Token'] = A.token();
    let data = body;
    if (body && !(body instanceof FormData)) { headers['Content-Type'] = 'application/json'; data = JSON.stringify(body); }
    const res = await fetch(A.base() + path, { method, headers, body: data, signal: opts.signal });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(json.error || res.statusText), { status: res.status });
    return json;
  };
  A.get = (p) => A.req('GET', p);
  A.post = (p, b) => A.req('POST', p, b);
  A.put = (p, b) => A.req('PUT', p, b);
  A.patch = (p, b) => A.req('PATCH', p, b);
  A.del = (p) => A.req('DELETE', p);

  // image URLs: full JPEG for the feed, square thumbnail for grids/reef
  A.img = (stem, thumb) => stem ? `${A.base()}/media/${stem}${thumb ? '_t' : ''}.jpg${A.token() ? `?t=${encodeURIComponent(A.token())}` : ''}` : '';

  // small shared helpers
  BB.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  BB.ago = (ts) => {
    const s = Math.max(1, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return 'just now';
    const m = Math.round(s / 60); if (m < 60) return m + 'm';
    const h = Math.round(m / 60); if (h < 24) return h + 'h';
    return Math.round(h / 24) + 'd';
  };
  BB.agoLong = (ts) => { const a = BB.ago(ts); return a === 'just now' ? a : a + ' ago'; };
  BB.pct = (v) => Math.round(v * 100) + '%';
  // the README's steps for putting the phone app on a phone, offline mode included
  BB.PHONE_GUIDE = 'https://github.com/shahidee44/baitbook#put-it-on-your-phone';
  // a message for a few seconds; with `action` ({label, fn}) it carries a button, e.g. undo
  BB.toast = (msg, action) => {
    let t = document.getElementById('toast');
    if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = msg;
    t.classList.toggle('has-action', !!action);
    if (action) {
      const b = document.createElement('button');
      b.textContent = action.label;
      b.onclick = () => { t.classList.remove('show'); action.fn(); };
      t.appendChild(b);
    }
    t.classList.add('show');
    clearTimeout(BB._toastT); BB._toastT = setTimeout(() => t.classList.remove('show'), action ? 7000 : 3200);
  };
})(window.BB);
