// Settings: drop-in models, image size and steps, the phone's window, pairing.
// Everything here writes baitbook.toml on the Angler (comments are kept).
(function (BB) {
  const api = BB.api, app = BB.app, esc = BB.esc;
  let cfg = null;

  const field = (sec, key, label, type = 'text', hint = '') => {
    const v = cfg[sec][key];
    return `<label>${label}${hint ? ` <span class="hint-inline">${hint}</span>` : ''}<input data-cfg="${sec}.${key}" type="${type}" value="${esc(v ?? '')}" ${type === 'number' ? 'step="any"' : ''}></label>`;
  };

  // Where the phone goes: Tailscale Serve's HTTPS address when it passes this Angler on (the app
  // works offline there), else this PC's own address. Over plain http, the way to offline mode.
  const codes = (urls) => urls.map((u) => `<code>${esc(u)}</code>`).join('<br>');
  const phoneWhere = (s) => (s.tailscale_url
    ? `<p style="margin:0 0 6px">On the phone, with Tailscale connected, open:</p>${codes([s.tailscale_url])}`
    : `<p style="margin:0 0 6px">On the phone, open:</p>${codes(s.lan_urls || []) || '<code>(no network address found)</code>'}`);
  const phoneOffline = (s) => {
    const lan = s.lan_urls || [];
    if (s.tailscale_url) return `Added to the home screen, it works offline.${lan.length ? ` On the same Wi-Fi without Tailscale, ${codes(lan)} works too, but needs the network.` : ''} `;
    if (lan.length && lan.every((u) => u.startsWith('https:'))) return '';  // its own certificate: offline already works
    return `Over plain http the phone app only opens while it can reach this PC. For offline mode, run <code>baitbook phone</code> here: it sets up Tailscale Serve (<a href="${BB.PHONE_GUIDE}" target="_blank" rel="noopener">how</a>). `;
  };

  BB.views.settings = {
    async render() {
      cfg = await api.get('/api/config');
      const s = app.status;
      return `<div class="cols">
        <div class="stack">
          <div class="card form models-form"><h2>Language + vision model</h2>
            <p class="sub">Runs in-process with llama-cpp-python. It does the vision read for new residents and writes every night’s prompts and captions. Drop in any Gemma 4 GGUF and its mmproj; other model families aren’t supported.</p>
            ${field('text', 'name', 'Name')}${field('text', 'model', 'GGUF')}${field('text', 'mmproj', 'Vision projector (mmproj)')}
            <div class="row2">${field('text', 'n_ctx', 'Context', 'number')}${field('text', 'temperature', 'Temperature', 'number')}</div></div>
          <div class="card form models-form"><h2>Image model</h2>
            <p class="sub">FLUX.2 [klein] 4B or 9B through ComfyUI, image-to-image only; other image models aren’t supported. Files are looked up in ComfyUI’s model folders. The prompt primer belongs to the image model: point <b>primer file</b> at a text file to swap it (empty = the built-in klein primer).</p>
            ${field('image', 'name', 'Name')}${field('image', 'unet', 'Diffusion model')}${field('image', 'text_encoder', 'Text encoder')}${field('image', 'vae', 'VAE')}
            <div class="row2">${field('image', 'width', 'Width', 'number')}${field('image', 'height', 'Height', 'number')}</div>
            <div class="row2">${field('image', 'steps', 'Steps', 'number', 'distilled: 4')}${field('image', 'cfg', 'CFG', 'number', 'distilled: 1.0')}</div>
            ${field('image', 'primer_file', 'Primer file')}</div>
        </div>
        <div class="stack">
          <div class="card"><h2>Phone</h2>
            <p class="sub">The phone app is offline-first: it keeps the last <b>${cfg.device.keep_nights}</b> nights and docks with the Angler to send votes and fetch new posts.</p>
            <div class="form">${field('device', 'keep_nights', 'Nights to keep on the phone', 'number')}</div>
            <p class="sect-label" style="margin-top:14px">pair a phone</p>
            <div class="pair"><b class="code">${esc(s.pair_code || '—')}</b><div>${phoneWhere(s)}
              <p class="hint" style="margin-top:8px">then enter this code. ${phoneOffline(s)}A PC app on another machine (with <code>[app] angler_url</code> pointing here) asks for the same code and pairs as the owner. The code changes after 5 wrong tries.</p></div></div>
            <p class="sect-label" style="margin-top:14px">paired devices</p>
            ${(s.devices || []).map((d) => `<div class="hist"><div><span>${esc(d.name)}${d.role === 'owner' ? ' · PC app, owner' : ''}</span><span>${d.last_dock_at ? 'docked ' + BB.agoLong(d.last_dock_at) : d.role === 'owner' ? 'paired ' + BB.agoLong(d.created_at) : 'never docked'} · <button class="linkish" data-act="remove-device" data-id="${d.id}" data-name="${esc(d.name)}">remove</button></span></div></div>`).join('') || '<p class="empty">None yet.</p>'}</div>
          <div class="card"><h2>Where things live</h2><p class="sub">Config: <code>${esc(s.paths ? s.paths.config : 'baitbook.toml')}</code>. Database and images: <code>${esc(s.paths ? s.paths.data : 'data/')}</code>. On the Angler's machine, <code>baitbook status</code> shows both and <code>baitbook setup</code> checks the models.</p></div>
        </div></div>`;
    },
    async onChange(e) {
      const k = e.target.dataset.cfg;
      if (!k) return;
      const [sec, key] = k.split('.');
      try { cfg = await api.put('/api/config', { [sec]: { [key]: e.target.value } }); BB.toast(`Saved ${key}`); }
      catch (err) { BB.toast(err.message); }
    },
    async onClick(act, t) {
      if (act !== 'remove-device' || !confirm(`Remove ${t.dataset.name}? Its token stops working; it would have to pair again.`)) return;
      try { await api.del(`/api/devices/${t.dataset.id}`); BB.toast(`Removed ${t.dataset.name}`); await app.poll(); app.render(); }
      catch (err) { BB.toast(err.message); }
    },
  };
})(window.BB);
