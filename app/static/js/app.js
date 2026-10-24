/* ============================================================================
   PlateVision console — a small, dependency-free SPA.
   Paper & ink design language: cream stock, hairline rules, black pill actions.
   Talks to the FastAPI service over same-origin relative URLs.
   ========================================================================== */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const API = '/api/v1';

  const state = {
    user: null,
    config: null,
    view: 'overview',
    range: '7d',
    overview: null,
    library: { page: 1, status: 'all', sort: 'recent', query: '', total: 0, pages: 1, items: [] },
    watchlist: [],
    watchStats: { total: 0, active: 0, hits: 0 },
    cameras: [],
    samples: [],
    scan: { file: null, preview: null, result: null, busy: false, storing: false, meta: null, source: null },
    selected: null,
    drawerOpen: false,
    busy: {},
  };

  /* ------------------------------------------------------------------ svg */
  const svg = {
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="11" cy="11" r="6.2"/><path d="m15.6 15.6 4.4 4.4" stroke-linecap="round"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 6l12 12M18 6 6 18" stroke-linecap="round"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m5 12.5 4.2 4.2L19 7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    flag: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 20V4m0 0h10l-1.6 4.2L16 12H6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M5 7h14M9.5 7V4.8h5V7M7 7l.9 12.2h8.2L17 7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    left: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M15 5.5 8 12l7 6.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    right: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 5.5 16 12l-7 6.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 16V5m0 0-4.5 4.5M12 5l4.5 4.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M4.5 15.5v2.2A2.3 2.3 0 0 0 6.8 20h10.4a2.3 2.3 0 0 0 2.3-2.3v-2.2" stroke-linecap="round"/></svg>',
    image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="3.5" y="5" width="17" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m5 17 5-5 4.2 4.2L17 13l2.5 2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    spinner: '<svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 4a8 8 0 1 0 8 8" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/></path></svg>',
  };

  /* ------------------------------------------------------------------ fmt */
  const fmt = {
    int: (n) => new Intl.NumberFormat('en-IN').format(Number(n || 0)),
    pct: (n, d = 1) => `${Number(n || 0).toFixed(d)}%`,
    escape: (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    plate: (item) => (item && (item.plate_display || item.plate)) || 'UNREADABLE',
    date: () => new Date().toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' }),
    short: (iso) => {
      if (!iso) return '—';
      const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`);
      return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
    },
    clock: (iso) => {
      const d = iso ? new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`) : new Date();
      return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    },
    when: (iso) => {
      if (!iso) return '—';
      const then = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`);
      const diff = (Date.now() - then.getTime()) / 1000;
      if (diff < 60) return 'just now';
      if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
      if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
      if (diff < 172800) return 'yesterday';
      return then.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    },
    bytes: (n) => (n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`),
  };

  /* ------------------------------------------------------------------ api */
  async function api(path, options = {}) {
    const opts = { credentials: 'same-origin', headers: {}, ...options };
    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(`${API}${path}`, opts);
    if (res.status === 401) {
      showAuth();
      throw new Error('Session expired');
    }
    const type = res.headers.get('content-type') || '';
    if (!res.ok) {
      let message = `Request failed (${res.status})`;
      if (type.includes('application/json')) {
        const body = await res.json().catch(() => null);
        message = body?.error?.message || message;
      }
      throw new Error(message);
    }
    if (type.includes('application/json')) return res.json();
    return res;
  }

  /* ---------------------------------------------------------------- toast */
  function toast(message, kind = 'ok') {
    const host = $('#toasts');
    if (!host) return;
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `${kind === 'error' ? svg.flag : svg.check}<span>${fmt.escape(message)}</span>`;
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .25s, transform .25s';
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      setTimeout(() => el.remove(), 280);
    }, 3400);
  }

  /* ----------------------------------------------------------------- auth */
  async function loadPublicConfig() {
    try {
      state.config = await fetch('/api/v1/config').then((r) => (r.ok ? r.json() : null));
    } catch {
      state.config = null;
    }
    const cfg = state.config;
    const hint = $('#auth-hint');
    if (hint) {
      if (cfg && cfg.demo_login) {
        hint.classList.remove('hidden');
        $('#auth-hint-copy').innerHTML = `Demo workspace available${cfg.demo_email ? ` · <code>${fmt.escape(cfg.demo_email)}</code>` : ''}`;
      } else {
        hint.classList.add('hidden');
      }
    }
    if (cfg?.app_name) document.title = `${cfg.app_name} · Plate Intelligence`;
    if (cfg?.version) {
      const v = $('#foot-version');
      if (v) v.textContent = cfg.version;
    }
  }

  function showAuth() {
    state.user = null;
    $('#app-shell').classList.add('hidden');
    $('#auth-screen').classList.remove('hidden');
    closeModal();
  }

  async function boot() {
    await loadPublicConfig();
    try {
      state.user = await api('/auth/me');
      await enterApp();
    } catch {
      showAuth();
    }
  }

  async function enterApp() {
    $('#auth-screen').classList.add('hidden');
    $('#app-shell').classList.remove('hidden');
    const user = state.user || {};
    $('#user-label').textContent = `${(user.full_name || 'Operator').toUpperCase()} · ${(user.role || 'operator').toUpperCase()}`;
    const env = $('#foot-env');
    if (env) env.textContent = `${(state.config?.environment || 'production').toUpperCase()} · LOCAL INFERENCE`;
    const corridor = $('#corridor-label');
    if (corridor && user.organization) corridor.textContent = String(user.organization).toUpperCase();
    render();
    await Promise.all([loadOverview(), loadSamples(), loadCameras(), loadWatchlist()]);
    render();
  }

  $('#login-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('#login-btn');
    button.disabled = true;
    button.innerHTML = `${svg.spinner} SIGNING IN`;
    try {
      const result = await api('/auth/login', {
        method: 'POST',
        body: { email: $('#email').value.trim(), password: $('#password').value },
      });
      $('#password').value = '';
      state.user = result.user;
      await enterApp();
      toast('Welcome back');
    } catch (error) {
      toast(error.message || 'Sign-in failed', 'error');
    } finally {
      button.disabled = false;
      button.textContent = 'ENTER · WORKSPACE';
    }
  });

  $('#demo-login-btn')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = 'OPENING…';
    try {
      const result = await api('/auth/demo', { method: 'POST' });
      state.user = result.user;
      await enterApp();
      toast('Demo workspace ready');
    } catch (error) {
      toast(error.message || 'Demo access is disabled', 'error');
    } finally {
      button.disabled = false;
      button.textContent = 'USE · DEMO WORKSPACE';
    }
  });

  /* ------------------------------------------------------------ navigation */
  function setView(view) {
    if (state.view === view) return;
    state.view = view;
    $$('.tab').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.view === view));
    state.library.page = 1;
    render();
    loadFor(view);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function loadFor(view) {
    if (view === 'overview') loadOverview();
    if (view === 'library') loadLibrary();
    if (view === 'watchlist') loadWatchlist();
    if (view === 'scan') loadSamples();
  }

  /* ---------------------------------------------------------------- render */
  const views = {
    overview: renderOverview,
    scan: renderScan,
    library: renderLibrary,
    watchlist: renderWatchlist,
  };

  function render() {
    if (!state.user) return;
    const active = document.activeElement;
    const focusId = active && active.id ? active.id : null;
    const caret = active && typeof active.selectionStart === 'number' ? active.selectionStart : null;
    $('#view').innerHTML = (views[state.view] || renderOverview)();
    if (focusId) {
      const el = document.getElementById(focusId);
      if (el) {
        el.focus();
        if (caret !== null && el.setSelectionRange) {
          try { el.setSelectionRange(caret, caret); } catch { /* not a text input */ }
        }
      }
    }
  }

  /* ------------------------------------------------------------- overview */
  async function loadOverview() {
    try {
      state.overview = await api(`/analytics/overview?range=${state.range}&tz_offset=${-new Date().getTimezoneOffset()}`);
    } catch (error) {
      state.overview = null;
      toast(error.message, 'error');
    }
    render();
  }

  function statusTag(status) {
    const map = { verified: 'is-verified', flagged: 'is-flagged', pending: 'is-pending', denied: 'is-flagged', archived: '' };
    return `<span class="tag ${map[status] || ''}">${fmt.escape(status || 'pending')}</span>`;
  }

  function riskDot(level) {
    const cls = level === 'critical' || level === 'high' ? '' : level === 'medium' ? 'is-amber' : 'is-muted';
    return `<span class="dot ${cls}"></span>`;
  }

  function renderOverview() {
    const data = state.overview;
    const k = data?.kpis || {};
    const trend = k.trend || {};
    const delta = (value, invert = false) => {
      if (value === undefined || value === null) return '<span class="delta is-flat">—</span>';
      const up = Number(value) >= 0;
      const cls = up ? (invert ? 'is-down' : '') : (invert ? '' : 'is-down');
      return `<span class="delta ${cls}">${up ? '▲' : '▼'} ${Math.abs(Number(value)).toFixed(1)}%</span>`;
    };
    const latest = data?.recent?.[0];
    const watch = data?.watchlist || [];

    const kpis = [
      { label: 'SCANS', value: fmt.int(k.scans), foot: delta(trend.scans) + '<span class="micro muted">VS PREVIOUS</span>' },
      { label: 'UNIQUE PLATES', value: fmt.int(k.unique_plates), foot: `<span class="micro muted">${fmt.int(k.watchlist_hits)} WATCHLIST HITS</span>` },
      { label: 'READ ACCURACY', value: fmt.pct(k.accuracy), foot: `<span class="micro muted">MEAN ${fmt.pct(k.avg_confidence)}</span>` },
      { label: 'NEEDS REVIEW', value: fmt.int(k.manual_review), foot: `<span class="micro muted">${Number(k.avg_processing_ms || 0).toFixed(0)} MS MEAN</span>` },
    ];

    const rows = (data?.recent || []).slice(1, 7).map((c) => `
      <button class="row" data-action="open-capture" data-id="${c.id}">
        <span class="row-plate">${fmt.escape(fmt.plate(c))}</span>
        <span class="row-meta">${fmt.escape(c.camera_name || 'Unassigned')} · ${fmt.escape(c.corridor || '—')}</span>
        <span class="row-time">${fmt.when(c.created_at)}</span>
        ${statusTag(c.status)}
      </button>`).join('');

    const watchItems = watch.length ? watch.slice(0, 5).map((w) => `
      <div class="rail-item">
        <div>
          <div class="rail-plate">${fmt.escape(w.plate)}</div>
          <div class="rail-sub">${fmt.escape((w.category || 'other').toUpperCase())} · ${fmt.escape(w.reason || 'Watchlist entry')}</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">${riskDot(w.severity)}<span class="micro muted">${fmt.int(w.hits)}</span></div>
      </div>`).join('') : '<div class="empty">No active watchlist entries</div>';

    return `
      <header class="page-head">
        <div>
          <div class="eyebrow"><span class="dot"></span>OPERATIONS · ${fmt.date().toUpperCase()}</div>
          <h1 class="display" style="margin-top:14px">Corridor <em>overview.</em></h1>
        </div>
        <div class="page-head-meta">
          <div class="segmented" data-action="range">
            ${['24h', '7d', '30d'].map((r) => `<button data-range="${r}" class="${state.range === r ? 'is-active' : ''}">${r.toUpperCase()}</button>`).join('')}
          </div>
          <span class="micro muted">${fmt.int(data?.cameras_online || 0)} / ${fmt.int(data?.cameras_total || 0)} CAMERAS ONLINE</span>
        </div>
      </header>

      <section class="kpis">
        ${kpis.map((item) => `
          <div class="kpi">
            <span class="kpi-label micro muted">${item.label}</span>
            <div class="kpi-value">${item.value}</div>
            <div class="kpi-foot">${item.foot}</div>
          </div>`).join('')}
      </section>

      <div class="overview-grid">
        <div class="stack">
          ${latest ? `
          <figure class="photo-card">
            <div class="photo-head">
              <span class="micro"><span class="dot"></span>LIVE · ${fmt.escape((latest.camera_name || 'FRAME').toUpperCase())}</span>
              <span class="micro muted">${fmt.escape(fmt.plate(latest))}</span>
            </div>
            <div class="photo-body">
              <img src="${latest.image_url}" alt="Latest capture" loading="lazy" />
              <span class="badge">${riskDot(latest.risk_level)}${fmt.escape((latest.status || 'pending').toUpperCase())} · ${fmt.pct(latest.confidence * 100, 1)}</span>
            </div>
            <div class="photo-foot">
              <span class="micro">${Number(latest.processing_ms || 0).toFixed(0)} MS · ${fmt.escape(latest.engine_version || 'pv-alpr')}</span>
              <span class="micro muted">${fmt.when(latest.created_at)}</span>
            </div>
          </figure>` : `
          <figure class="photo-card">
            <div class="photo-head"><span class="micro"><span class="dot is-muted"></span>NO FRAMES YET</span><span class="micro muted">—</span></div>
            <div class="photo-body is-empty"><div class="photo-empty">${svg.image}<span class="micro">CAPTURE A FRAME TO BEGIN</span></div></div>
            <div class="photo-foot"><span class="micro">—</span><span class="micro muted">—</span></div>
          </figure>`}

          <div>
            <div class="section-head">
              <h2>Recent captures</h2>
              <button class="micro muted" data-view="library" style="letter-spacing:.18em">OPEN EVIDENCE →</button>
            </div>
            <div class="rows">${rows || '<div class="empty">Nothing captured in this window</div>'}</div>
          </div>
        </div>

        <div class="stack">
          <div class="card rail-card">
            <div class="rail-head">
              <span class="micro"><span class="dot"></span>WATCHLIST</span>
              <button class="micro muted" data-view="watchlist">MANAGE →</button>
            </div>
            <div class="rail-list">${watchItems}</div>
          </div>

          <div class="card rail-card">
            <div class="rail-head"><span class="micro">FLEET</span><span class="micro muted">${fmt.int(data?.cameras_total || 0)} UNITS</span></div>
            <div class="fleet">
              <div class="fleet-item"><span class="fleet-num numeral">${fmt.int(data?.cameras_online || 0)}</span><span class="micro muted">ONLINE</span></div>
              <div class="fleet-item"><span class="fleet-num numeral">${fmt.int(k.scans)}</span><span class="micro muted">SCANS</span></div>
              <div class="fleet-item"><span class="fleet-num numeral">${fmt.int(k.watchlist_hits)}</span><span class="micro muted">HITS</span></div>
            </div>
          </div>
        </div>
      </div>`;
  }

  /* ----------------------------------------------------------------- scan */
  async function loadSamples() {
    if (state.samples.length) return;
    try {
      const data = await api('/recognize/samples?limit=6');
      state.samples = data.items || [];
      if (state.view === 'scan') render();
    } catch { /* non-fatal */ }
  }

  async function loadCameras() {
    try {
      state.cameras = await api('/cameras');
    } catch {
      state.cameras = [];
    }
  }

  function scanFile(file, source = 'upload') {
    if (!file) return;
    if (state.scan.preview) URL.revokeObjectURL(state.scan.preview);
    state.scan = {
      file,
      preview: URL.createObjectURL(file),
      result: null,
      busy: false,
      storing: false,
      meta: null,
      source,
    };
    const img = new Image();
    img.onload = () => {
      state.scan.meta = `${img.naturalWidth} × ${img.naturalHeight} · ${fmt.bytes(file.size)}`;
      if (state.view === 'scan') render();
    };
    img.src = state.scan.preview;
    render();
  }

  async function analyzeScan() {
    if (!state.scan.file || state.scan.busy) return;
    state.scan.busy = true;
    render();
    try {
      const form = new FormData();
      form.append('file', state.scan.file, state.scan.file.name || 'frame.jpg');
      form.append('country', state.config?.countries?.[0] || 'IN');
      const result = await api('/recognize', { method: 'POST', body: form });
      state.scan.result = result;
      const best = (result.plates || [])[0];
      toast(best ? `${best.plate} · ${Math.round((best.confidence || 0) * 100)}%` : 'No plate found');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      state.scan.busy = false;
      render();
    }
  }

  async function storeScan() {
    if (!state.scan.file || state.scan.storing) return;
    state.scan.storing = true;
    render();
    try {
      const form = new FormData();
      form.append('file', state.scan.file, state.scan.file.name || 'frame.jpg');
      form.append('country', state.config?.countries?.[0] || 'IN');
      if (state.cameras[0]) form.append('camera_id', state.cameras[0].id);
      const capture = await api('/captures', { method: 'POST', body: form });
      toast(`Stored · ${capture.plate_display || capture.plate}`);
      state.scan.result = { ...(state.scan.result || {}), stored: capture };
      await Promise.all([loadOverview(), loadLibrary()]);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      state.scan.storing = false;
      render();
    }
  }

  function renderScan() {
    const scan = state.scan;
    const best = scan.result?.plates?.[0];
    const conf = best ? Number(best.confidence || 0) : 0;
    const confClass = conf >= 0.72 ? '' : conf >= 0.5 ? 'is-mid' : 'is-low';

    const samples = state.samples.map((s) => `
      <button class="sample" data-action="use-sample" data-id="${fmt.escape(s.id)}">
        <img src="/api/v1/recognize/samples/${encodeURIComponent(s.id)}.jpg" alt="" loading="lazy" />
        <span>${fmt.escape(s.label || s.id)}</span>
      </button>`).join('');

    const glyphs = (best?.glyphs || []).map((g) => `
      <div class="glyph"><b>${fmt.escape(g.label || '·')}</b><span>${Math.round((g.confidence || 0) * 100)}</span></div>`).join('');

    const alternates = (best?.alternates || []).map((a) => `<span class="alt">${fmt.escape(a.plate)}</span>`).join('');

    const result = best ? `
      <div class="eyebrow"><span class="dot ${conf >= 0.72 ? 'is-green' : 'is-amber'}"></span>READ COMPLETE</div>
      <div class="result-plate">${fmt.escape(best.plate || 'UNREADABLE')}</div>
      <div class="result-format">
        <span class="tag ${best.format_valid ? 'is-verified' : ''}">${fmt.escape(best.format || 'GENERIC')}</span>
        <span class="micro muted">${fmt.escape(best.diagnostics?.region_name || best.region || 'REGION UNKNOWN')}</span>
      </div>

      <div class="meter">
        <div class="meter-head">
          <span class="micro muted">CONFIDENCE</span>
          <span class="micro">${fmt.pct(conf * 100)}</span>
        </div>
        <div class="meter-track"><div class="meter-fill ${confClass}" style="width:${Math.max(2, Math.round(conf * 100))}%"></div></div>
      </div>

      ${glyphs ? `<div class="glyphs">${glyphs}</div>` : ''}
      ${alternates ? `<div class="alt-row">${alternates}</div>` : ''}

      <dl class="meta-grid">
        <div><dt class="micro muted">PROCESSING</dt><dd>${Number(scan.result.processing_ms || 0).toFixed(0)} ms</dd></div>
        <div><dt class="micro muted">ENGINE</dt><dd>${fmt.escape(scan.result.engine_version || '—')}</dd></div>
        <div><dt class="micro muted">CANDIDATES</dt><dd>${fmt.int((scan.result.plates || []).length)}</dd></div>
        <div><dt class="micro muted">QUALITY</dt><dd>${fmt.pct((best.diagnostics?.quality || 0) * 100, 0)}</dd></div>
      </dl>

      <div class="result-actions">
        <button class="pill pill-dark" data-action="store">${scan.storing ? `${svg.spinner} STORING` : 'STORE · EVIDENCE'}</button>
        <button class="pill pill-ghost" data-action="reset-scan">NEW FRAME</button>
      </div>
      ${scan.result?.stored ? `<p class="micro muted" style="margin-top:14px">SAVED AS <button class="micro" data-action="open-capture" data-id="${scan.result.stored.id}" style="text-decoration:underline">${fmt.escape(fmt.plate(scan.result.stored))}</button></p>` : ''}
    ` : `
      <div class="result-empty">
        <span class="micro muted">AWAITING FRAME</span>
        <div class="display">No read yet.</div>
        <p class="lede" style="max-width:280px">Drop a photograph on the left and press analyze. The reader returns a plate, a confidence and the per-character breakdown.</p>
      </div>`;

    return `
      <header class="page-head">
        <div>
          <div class="eyebrow"><span class="dot"></span>FRAME → PLATE</div>
          <h1 class="display" style="margin-top:14px">Read a <em>plate.</em></h1>
        </div>
        <div class="page-head-meta"><span class="micro muted">JPG · PNG · WEBP · ≤ 12 MB</span></div>
      </header>

      <div class="scan-grid">
        <div>
          <figure class="photo-card scan-card">
            <div class="photo-head">
              <span class="micro"><span class="dot"></span>${scan.preview ? 'READY' : 'DROP A FRAME'} · 01</span>
              <span class="micro muted">${scan.file ? fmt.escape((scan.file.name || 'FRAME').toUpperCase().slice(0, 22)) : 'VC-SNAP-0421'}</span>
            </div>
            <div class="photo-body ${scan.preview ? '' : 'is-empty'}" data-action="pick-file" id="drop-zone">
              ${scan.preview
                ? `<img src="${scan.preview}" alt="Frame to analyse" />`
                : `<div class="photo-empty">${svg.upload}<span class="micro">TAP TO CHOOSE · OR DROP AN IMAGE</span></div>`}
            </div>
            <div class="photo-foot">
              <span class="micro">${scan.meta || 'NO FRAME LOADED'}</span>
              <span class="micro muted">${scan.result ? `${Number(scan.result.processing_ms || 0).toFixed(0)} MS` : 'READY'}</span>
            </div>
          </figure>

          <div class="scan-actions">
            <button class="pill pill-dark" data-action="analyze" ${!scan.file || scan.busy ? 'disabled' : ''}>
              ${scan.busy ? `${svg.spinner} ANALYZING` : 'ANALYZE · PLATE'}
            </button>
            <button class="pill pill-ghost" data-action="pick-file">CHOOSE · FILE</button>
          </div>

          ${samples ? `
          <div class="samples">
            <div class="section-head" style="margin:26px 0 10px"><h2 style="font-size:16px">Sample frames</h2><span class="micro muted">SYNTHETIC · SAFE TO USE</span></div>
            <div class="samples-track">${samples}</div>
          </div>` : ''}
        </div>

        <div class="card card-pad result-card">${result}</div>
      </div>`;
  }

  /* -------------------------------------------------------------- library */
  async function loadLibrary() {
    const { page, status, sort, query } = state.library;
    const params = new URLSearchParams({ page: String(page), page_size: '12', sort });
    if (status !== 'all') params.set('status', status);
    if (query) params.set('query', query);
    try {
      const data = await api(`/captures?${params}`);
      state.library = { ...state.library, ...data, items: data.items };
    } catch (error) {
      toast(error.message, 'error');
    }
    render();
  }

  function renderLibrary() {
    const lib = state.library;
    const chips = [
      ['all', 'ALL'],
      ['pending', 'NEEDS REVIEW'],
      ['verified', 'VERIFIED'],
      ['flagged', 'FLAGGED'],
    ];

    const cards = (lib.items || []).map((c) => `
      <button class="lib-card" data-action="open-capture" data-id="${c.id}">
        <div class="photo-body">
          <img src="${c.thumbnail_url || c.image_url}" alt="" loading="lazy" />
          ${c.watchlist_hit ? '<span class="badge"><span class="dot"></span>WATCHLIST</span>' : ''}
        </div>
        <div class="lib-meta">
          <span class="lib-plate">${fmt.escape(fmt.plate(c))}</span>
          <span class="lib-sub">${fmt.pct((c.confidence || 0) * 100, 0)}</span>
        </div>
        <div class="lib-foot">
          <span class="lib-sub">${fmt.escape(c.camera_name || 'UNASSIGNED')} · ${fmt.escape(fmt.when(c.created_at).toUpperCase())}</span>
          ${statusTag(c.status)}
        </div>
      </button>`).join('');

    return `
      <header class="page-head">
        <div>
          <div class="eyebrow"><span class="dot"></span>EVIDENCE</div>
          <h1 class="display" style="margin-top:14px">The <em>record.</em></h1>
        </div>
        <div class="page-head-meta">
          <span class="micro muted">${fmt.int(lib.total || 0)} CAPTURES ON FILE</span>
          <a class="pill pill-ghost pill-sm" href="/api/v1/captures/export.csv" download>EXPORT · CSV</a>
        </div>
      </header>

      <div class="toolbar">
        <div class="toolbar-group">
          ${chips.map(([key, label]) => `<button class="chip ${lib.status === key ? 'is-active' : ''}" data-action="filter-status" data-status="${key}">${label}</button>`).join('')}
        </div>
        <div class="toolbar-group">
          <label class="search">
            ${svg.search}
            <input id="library-search" type="search" placeholder="SEARCH PLATE" value="${fmt.escape(lib.query)}" maxlength="16" autocomplete="off" />
          </label>
          <select class="chip" id="library-sort" aria-label="Sort captures">
            <option value="recent" ${lib.sort === 'recent' ? 'selected' : ''}>NEWEST</option>
            <option value="confidence" ${lib.sort === 'confidence' ? 'selected' : ''}>CONFIDENCE</option>
            <option value="risk" ${lib.sort === 'risk' ? 'selected' : ''}>RISK</option>
          </select>
        </div>
      </div>

      ${cards ? `<div class="lib-grid">${cards}</div>` : '<div class="empty">No captures match this filter</div>'}

      <div class="pager">
        <button data-action="page" data-page="${Math.max(1, lib.page - 1)}" ${lib.page <= 1 ? 'disabled' : ''}>${svg.left} PREV</button>
        <span class="micro">PAGE ${fmt.int(lib.page)} / ${fmt.int(lib.pages)}</span>
        <button data-action="page" data-page="${Math.min(lib.pages, lib.page + 1)}" ${lib.page >= lib.pages ? 'disabled' : ''}>NEXT ${svg.right}</button>
      </div>`;
  }

  /* ------------------------------------------------------------ watchlist */
  async function loadWatchlist() {
    try {
      const [entries, stats] = await Promise.all([api('/watchlist'), api('/watchlist/stats')]);
      state.watchlist = entries;
      state.watchStats = stats;
    } catch (error) {
      toast(error.message, 'error');
    }
    render();
  }

  function renderWatchlist() {
    const entries = state.watchlist;
    const stats = state.watchStats || {};

    const items = entries.map((w) => `
      <div class="watch-item">
        <div>
          <div class="watch-plate">${fmt.escape(w.plate)}</div>
          <div class="watch-reason">${fmt.escape(w.reason || w.label || 'Watchlist entry')}</div>
          <div class="rail-sub" style="margin-top:8px;display:flex;align-items:center;gap:8px">
            ${riskDot(w.severity)}${fmt.escape((w.category || 'other').toUpperCase())} · ${fmt.int(w.hits)} HITS · ${w.active ? 'ACTIVE' : 'PAUSED'}
          </div>
        </div>
        <div class="watch-actions">
          <button class="chip ${w.active ? 'is-active' : ''}" data-action="toggle-watch" data-id="${w.id}">${w.active ? 'ON' : 'OFF'}</button>
          <button class="icon-btn is-danger" data-action="delete-watch" data-id="${w.id}" title="Remove">${svg.trash}</button>
        </div>
      </div>`).join('');

    return `
      <header class="page-head">
        <div>
          <div class="eyebrow"><span class="dot"></span>ENFORCEMENT</div>
          <h1 class="display" style="margin-top:14px">The <em>watchlist.</em></h1>
        </div>
        <div class="page-head-meta"><span class="micro muted">MATCHED ON EVERY FRAME</span></div>
      </header>

      <section class="stat-strip">
        <div class="stat"><span class="micro muted">ENTRIES</span><div class="stat-num">${fmt.int(stats.total)}</div></div>
        <div class="stat"><span class="micro muted">ACTIVE</span><div class="stat-num">${fmt.int(stats.active)}</div></div>
        <div class="stat"><span class="micro muted">HITS RECORDED</span><div class="stat-num">${fmt.int(stats.hits)}</div></div>
      </section>

      <div class="watch-grid">
        <div class="card card-pad">
          <div class="rail-head" style="margin-bottom:6px"><span class="micro">REGISTERED</span><span class="micro muted">${fmt.int(entries.length)} SHOWN</span></div>
          <div style="margin-top:8px">${items || '<div class="empty">No entries yet — add the first plate on the right</div>'}</div>
        </div>

        <form class="card card-pad" id="watch-form">
          <div class="eyebrow"><span class="dot"></span>NEW ENTRY</div>
          <label class="field">
            <span class="micro">PLATE</span>
            <input name="plate" id="watch-plate" maxlength="16" placeholder="MH 12 AB 3456" required />
          </label>
          <label class="field">
            <span class="micro">CATEGORY</span>
            <select name="category" id="watch-category">
              <option value="stolen">Stolen</option>
              <option value="banned">Banned</option>
              <option value="vip">VIP</option>
              <option value="permit">Permit</option>
              <option value="unpaid">Unpaid</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label class="field">
            <span class="micro">NOTE</span>
            <input name="reason" id="watch-reason" maxlength="200" placeholder="Reason for the entry" />
          </label>
          <button class="pill pill-dark pill-block" type="submit" style="margin-top:26px">ADD · WATCHLIST</button>
        </form>
      </div>`;
  }

  /* ---------------------------------------------------------------- modal */
  function closeModal() {
    state.drawerOpen = false;
    $('#modal-root').innerHTML = '';
    document.body.style.overflow = '';
  }

  async function openCapture(id) {
    try {
      const item = await api(`/captures/${id}`);
      state.selected = item;
      state.drawerOpen = true;
      renderDrawer(item);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function renderDrawer(c) {
    const glyphs = (c.candidates || []).slice(0, 12).map((g) => `
      <div class="glyph"><b>${fmt.escape(g.label || '·')}</b><span>${Math.round((g.confidence || 0) * 100)}</span></div>`).join('');

    $('#modal-root').innerHTML = `
      <div class="overlay" data-action="close-modal">
        <aside class="drawer" role="dialog" aria-modal="true" aria-label="Capture detail">
          <div class="drawer-head">
            <span class="micro"><span class="dot"></span>CAPTURE · ${fmt.escape(c.id.slice(0, 8).toUpperCase())}</span>
            <button class="icon-btn" data-action="close-modal" aria-label="Close">${svg.close}</button>
          </div>

          <figure class="photo-card">
            <div class="photo-head">
              <span class="micro"><span class="dot ${c.watchlist_hit ? '' : 'is-green'}"></span>${c.watchlist_hit ? 'WATCHLIST HIT' : 'EVIDENCE'}</span>
              <span class="micro muted">${fmt.escape(fmt.plate(c))}</span>
            </div>
            <div class="photo-body"><img src="${c.image_url}" alt="Evidence frame" /></div>
            <div class="photo-foot">
              <span class="micro">${fmt.escape(c.camera_name || 'UNASSIGNED')} · ${fmt.clock(c.created_at)}</span>
              <span class="micro muted">${Number(c.processing_ms || 0).toFixed(0)} MS</span>
            </div>
          </figure>

          <div class="result-plate" style="font-size:44px;margin-top:24px">${fmt.escape(fmt.plate(c))}</div>
          <div class="result-format">
            ${statusTag(c.status)}
            <span class="micro muted">${fmt.pct((c.confidence || 0) * 100)} CONFIDENCE</span>
            ${c.watchlist_hit ? `<span class="tag is-flagged">${fmt.escape(c.watchlist_label || 'WATCHLIST')}</span>` : ''}
          </div>

          ${glyphs ? `<div class="glyphs">${glyphs}</div>` : ''}

          <dl class="kv" style="margin-top:26px">
            <div><dt class="micro muted">JURISDICTION</dt><dd>${fmt.escape(c.region_name || c.region || '—')}</dd></div>
            <div><dt class="micro muted">PLATE TYPE</dt><dd>${fmt.escape((c.plate_type || 'unknown').toUpperCase())}</dd></div>
            <div><dt class="micro muted">RISK</dt><dd>${fmt.escape((c.risk_level || '—').toUpperCase())} · ${Number(c.risk_score || 0).toFixed(0)}</dd></div>
            <div><dt class="micro muted">SOURCE</dt><dd>${fmt.escape(c.source || '—')}</dd></div>
            <div><dt class="micro muted">CAPTURED</dt><dd>${fmt.escape(fmt.short(c.created_at))} · ${fmt.clock(c.created_at)}</dd></div>
            <div><dt class="micro muted">OPERATOR</dt><dd>${fmt.escape(c.operator || 'Automated ingest')}</dd></div>
          </dl>

          <label class="field" style="margin-top:26px">
            <span class="micro">CORRECT PLATE · TRAINS THIS CAMERA</span>
            <input id="teach-input" value="${fmt.escape(c.plate || '')}" autocomplete="off" spellcheck="false" />
          </label>
          <button class="pill pill-ghost pill-block" data-action="teach" data-id="${c.id}" style="margin-top:12px">SAVE · TRAIN</button>

          <label class="field" style="margin-top:26px">
            <span class="micro">REVIEW NOTE</span>
            <textarea id="note-input" placeholder="Add context for the audit trail">${fmt.escape(c.notes || '')}</textarea>
          </label>
          <button class="pill pill-ghost pill-block" data-action="save-note" data-id="${c.id}" style="margin-top:12px">SAVE · NOTE</button>

          <div class="modal-actions" style="margin-top:22px">
            <button class="pill pill-dark" data-action="review" data-status="verified" data-id="${c.id}">VERIFY</button>
            <button class="pill pill-ghost" data-action="review" data-status="flagged" data-id="${c.id}">FLAG</button>
          </div>
          <div class="modal-actions" style="margin-top:10px">
            <button class="pill pill-ghost" data-action="delete-capture" data-id="${c.id}" style="border-color:var(--red);color:var(--red)">DELETE · EVIDENCE</button>
          </div>
        </aside>
      </div>`;
    document.body.style.overflow = 'hidden';
  }

  /* --------------------------------------------------------------- events */
  document.addEventListener('click', async (event) => {
    const viewBtn = event.target.closest('[data-view]');
    if (viewBtn) {
      event.preventDefault();
      setView(viewBtn.dataset.view);
      return;
    }

    const range = event.target.closest('[data-range]');
    if (range) {
      state.range = range.dataset.range;
      await loadOverview();
      return;
    }

    const action = event.target.closest('[data-action]');
    if (!action) return;
    const kind = action.dataset.action;

    if (kind === 'close-modal') {
      if (event.target === action || action.classList.contains('icon-btn')) closeModal();
      return;
    }

    switch (kind) {
      case 'logout':
        try { await api('/auth/logout', { method: 'POST' }); } catch { /* ignore */ }
        showAuth();
        break;

      case 'pick-file':
        $('#file-picker').click();
        break;

      case 'analyze':
        analyzeScan();
        break;

      case 'store':
        storeScan();
        break;

      case 'reset-scan':
        if (state.scan.preview) URL.revokeObjectURL(state.scan.preview);
        state.scan = { file: null, preview: null, result: null, busy: false, storing: false, meta: null, source: null };
        render();
        break;

      case 'use-sample': {
        const id = action.dataset.id;
        try {
          const res = await fetch(`/api/v1/recognize/samples/${encodeURIComponent(id)}.jpg`, { credentials: 'same-origin' });
          if (!res.ok) throw new Error('Sample unavailable');
          const blob = await res.blob();
          scanFile(new File([blob], `${id}.jpg`, { type: blob.type || 'image/jpeg' }), 'sample');
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      case 'open-capture':
        openCapture(action.dataset.id);
        break;

      case 'filter-status':
        state.library.status = action.dataset.status;
        state.library.page = 1;
        await loadLibrary();
        break;

      case 'page':
        state.library.page = Number(action.dataset.page);
        await loadLibrary();
        break;

      case 'review': {
        const id = action.dataset.id;
        try {
          await api(`/captures/${id}`, { method: 'PATCH', body: { status: action.dataset.status } });
          toast(action.dataset.status === 'verified' ? 'Capture verified' : 'Capture flagged');
          closeModal();
          await Promise.all([loadLibrary(), loadOverview()]);
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      case 'teach': {
        const id = action.dataset.id;
        const plate = ($('#teach-input')?.value ?? '').toUpperCase().replace(/[^A-Z0-9]/g, '');
        if (plate.length < 3) {
          toast('Enter the plate as printed', 'error');
          break;
        }
        try {
          const result = await api(`/captures/${id}/teach`, { method: 'POST', body: { plate } });
          const added = result?.learned?.added ?? 0;
          toast(added > 0 ? `Corrected · ${added} glyphs learned` : 'Corrected');
          closeModal();
          await Promise.all([loadLibrary(), loadOverview()]);
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      case 'save-note': {
        const id = action.dataset.id;
        const note = $('#note-input')?.value ?? '';
        try {
          await api(`/captures/${id}`, { method: 'PATCH', body: { notes: note } });
          toast('Note saved');
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      case 'delete-capture': {
        const id = action.dataset.id;
        if (!window.confirm('Delete this capture and its evidence image?')) return;
        try {
          await api(`/captures/${id}`, { method: 'DELETE' });
          toast('Evidence deleted');
          closeModal();
          await Promise.all([loadLibrary(), loadOverview()]);
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      case 'toggle-watch': {
        const id = action.dataset.id;
        try {
          await api(`/watchlist/${id}`, { method: 'PATCH', body: { active: !action.classList.contains('is-active') } });
          await loadWatchlist();
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      case 'delete-watch': {
        const id = action.dataset.id;
        if (!window.confirm('Remove this watchlist entry?')) return;
        try {
          await api(`/watchlist/${id}`, { method: 'DELETE' });
          toast('Entry removed');
          await loadWatchlist();
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      default:
        break;
    }
  });

  /* inputs */
  document.addEventListener('change', async (event) => {
    if (event.target.id === 'library-sort') {
      state.library.sort = event.target.value;
      state.library.page = 1;
      await loadLibrary();
    }
  });

  let searchTimer = null;
  document.addEventListener('input', (event) => {
    if (event.target.id !== 'library-search') return;
    clearTimeout(searchTimer);
    const value = event.target.value.trim();
    searchTimer = setTimeout(async () => {
      state.library.query = value;
      state.library.page = 1;
      await loadLibrary();
    }, 320);
  });

  document.addEventListener('submit', async (event) => {
    if (event.target.id !== 'watch-form') return;
    event.preventDefault();
    const plate = $('#watch-plate').value.trim();
    if (!plate) return;
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api('/watchlist', {
        method: 'POST',
        body: {
          plate,
          category: $('#watch-category').value,
          reason: $('#watch-reason').value.trim(),
          severity: 'high',
        },
      });
      event.target.reset();
      toast('Added to watchlist');
      await Promise.all([loadWatchlist(), loadOverview()]);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
    }
  });

  $('#file-picker')?.addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    if (file) {
      scanFile(file);
      if (state.view !== 'scan') setView('scan');
    }
    event.target.value = '';
  });

  document.addEventListener('dragover', (event) => {
    const zone = event.target.closest?.('#drop-zone, .scan-card');
    if (!zone) return;
    event.preventDefault();
    const body = $('.photo-body', zone);
    if (body) body.classList.add('is-dragging');
  });

  document.addEventListener('dragleave', (event) => {
    const zone = event.target.closest?.('#drop-zone, .scan-card');
    if (!zone) return;
    const body = $('.photo-body', zone);
    if (body) body.classList.remove('is-dragging');
  });

  document.addEventListener('drop', (event) => {
    const zone = event.target.closest?.('#drop-zone, .scan-card');
    if (!zone) return;
    event.preventDefault();
    const body = $('.photo-body', zone);
    if (body) body.classList.remove('is-dragging');
    const file = event.dataTransfer?.files?.[0];
    if (file) {
      scanFile(file);
      if (state.view !== 'scan') setView('scan');
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.drawerOpen) closeModal();
  });

  boot();
})();
