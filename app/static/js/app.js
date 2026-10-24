/* ============================================================================
   PlateVision console — vanilla ES2020, no build step, no external runtime.
   Talks to the FastAPI service over relative URLs so the same bundle works on
   any host (localhost, Fly, Render, a toll-plaza edge box).
   ========================================================================== */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const API = '/api/v1';

  const state = {
    user: null,
    view: 'overview',
    range: '7d',
    overview: null,
    library: { page: 1, status: 'all', risk: null, sort: 'recent', total: 0, pages: 1 },
    watchlist: [],
    cameras: [],
    samples: [],
    selectedCapture: null,
    scanning: false,
    lastResult: null,
  };

  const SCENE_IMAGES = {
    dusk: '/static/img/hero-dusk.jpg',
    night: '/static/img/scene-highway.jpg',
    tunnel: '/static/img/scene-bridge.jpg',
    highway: '/static/img/scene-airport.jpg',
  };

  /* ------------------------------------------------------------------ utils */
  const fmt = {
    int: (n) => new Intl.NumberFormat('en-IN').format(Number(n || 0)),
    pct: (n, digits = 1) => `${Number(n || 0).toFixed(digits)}%`,
    plate: (p) => {
      if (!p) return 'UNREADABLE';
      const clean = String(p).replace(/[^A-Z0-9]/gi, '').toUpperCase();
      if (clean.length >= 9 && /^[A-Z]{2}/.test(clean)) {
        return `${clean.slice(0, 2)} ${clean.slice(2, 4)} ${clean.slice(4, -4)} ${clean.slice(-4)}`.replace(/\s+/g, ' ').trim();
      }
      return clean;
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
    clock: (iso) => {
      const d = iso ? new Date(iso) : new Date();
      return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    },
    escape: (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  };

  const icons = {
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 13 4 4L19 7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 4 3.5 19h17L12 4Z" stroke-linejoin="round"/><path d="M12 10v4m0 2.5v.5" stroke-linecap="round"/></svg>',
    flag: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 20V5m0 0h9l-1.5 4L18 9H6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3.8 5.5 6.6v4.8c0 3.9 2.7 7.1 6.5 8.8 3.8-1.7 6.5-4.9 6.5-8.8V6.6L12 3.8Z" stroke-linejoin="round"/></svg>',
    arrowUp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 19V5m0 0-6 6m6-6 6 6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    arrowDown: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14m0 0 6-6m-6 6-6-6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l11-6.5-11-6.5Z"/></svg>',
    prev: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M15 5.5 8 12l7 6.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    next: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 5.5 16 12l-7 6.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    deny: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 6l12 12M18 6 6 18" stroke-linecap="round"/></svg>',
    scan: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8M20 16v2.5a1.5 1.5 0 0 1-1.5 1.5H16M8 20H5.5A1.5 1.5 0 0 1 4 18.5V16" stroke-linecap="round"/></svg>',
    spinner: '<svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 4a8 8 0 1 0 8 8" stroke-linecap="round"/></svg>',
  };

  /* -------------------------------------------------------------------- api */
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
        const body = await res.json();
        message = body?.error?.message || message;
      }
      throw new Error(message);
    }
    if (type.includes('application/json')) return res.json();
    return res;
  }

  /* ----------------------------------------------------------------- toasts */
  function toast(message, kind = 'ok') {
    const host = $('#toasts');
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `${kind === 'error' ? icons.alert : icons.check}<span>${fmt.escape(message)}</span>`;
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .3s, transform .3s';
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      setTimeout(() => el.remove(), 320);
    }, 3200);
  }

  /* ------------------------------------------------------------------ auth */
  function showAuth() {
    $('#auth-screen').classList.remove('hidden');
    $('#app-shell').classList.add('hidden');
  }

  async function boot() {
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
    renderIdentity();
    await Promise.all([loadOverview(), loadWatchlist(), loadCameras(), loadSamples()]);
  }

  function renderIdentity() {
    const user = state.user;
    if (!user) return;
    $('#avatar').textContent = user.avatar_initials || 'PV';
    const hour = new Date().getHours();
    const part = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
    $('#greeting').textContent = `${part}, ${(user.full_name || '').split(' ')[0]}`;
    $('#hero-meta').textContent = `${user.organization || 'Meridian Traffic Authority'} · ${(user.plan || 'enterprise').replace(/^./, (c) => c.toUpperCase())}`;
    $('#hero-clock').textContent = fmt.clock();
    $('#avatar').title = `${user.full_name} · ${user.role}`;
  }

  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('#login-btn');
    button.disabled = true;
    button.innerHTML = `${icons.spinner} Signing in…`;
    try {
      const result = await api('/auth/login', {
        method: 'POST',
        body: { email: $('#email').value.trim(), password: $('#password').value },
      });
      state.user = result.user;
      await enterApp();
      toast('Welcome back');
    } catch (error) {
      toast(error.message || 'Sign-in failed', 'error');
    } finally {
      button.disabled = false;
      button.textContent = 'Enter workspace';
    }
  });

  $('#logout-btn').addEventListener('click', async () => {
    try { await api('/auth/logout', { method: 'POST' }); } catch { /* ignore */ }
    state.user = null;
    showAuth();
  });

  /* ------------------------------------------------------------- navigation */
  function switchView(view) {
    state.view = view;
    $$('.view').forEach((el) => el.classList.toggle('active', el.id === `view-${view}`));
    $$('[data-view]').forEach((el) => el.classList.toggle('active', el.dataset.view === view));
    $$('.tool').forEach((el) => el.classList.toggle('active', el.dataset.view === view));
    window.scrollTo({ top: 0, behavior: 'smooth' });
    if (view === 'analytics') loadAnalytics();
    if (view === 'library') loadLibrary();
    if (view === 'watchlist') loadWatchlist();
  }

  document.addEventListener('click', (event) => {
    const target = event.target.closest('[data-view]');
    if (!target) return;
    event.preventDefault();
    switchView(target.dataset.view);
  });

  /* --------------------------------------------------------------- overview */
  async function loadOverview() {
    try {
      const data = await api(`/analytics/overview?range=${state.range}&tz_offset=${-new Date().getTimezoneOffset()}`);
      state.overview = data;
      renderOverview(data);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function renderOverview(data) {
    const k = data.kpis;
    const trend = k.trend || {};
    const kpis = [
      { label: 'Scans', value: fmt.int(k.scans), delta: trend.scans, foot: 'vs previous period', suffix: '' },
      { label: 'Unique plates', value: fmt.int(k.unique_plates), foot: `${fmt.int(k.watchlist_hits)} watchlist hits` },
      { label: 'Accuracy', value: fmt.pct(k.accuracy), delta: trend.accuracy, foot: `mean confidence ${fmt.pct(k.avg_confidence)}` },
      { label: 'Needs review', value: fmt.int(k.manual_review), foot: `${k.avg_processing_ms} ms mean latency` },
    ];
    $('#kpi-row').innerHTML = kpis.map((item) => {
      const delta = typeof item.delta === 'number' && item.delta !== 0
        ? `<span class="delta ${item.delta > 0 ? 'up' : 'down'}">${item.delta > 0 ? icons.arrowUp : icons.arrowDown}${Math.abs(item.delta)}%</span>`
        : '';
      return `
        <article class="card kpi">
          <p class="kpi-label">${item.label}</p>
          <p class="kpi-value">${item.value}</p>
          <div class="kpi-foot">${delta}<span>${item.foot}</span></div>
        </article>`;
    }).join('');

    $('#chart-total').textContent = fmt.int(k.scans);
    $('#chart-flagged').textContent = fmt.int(k.flagged);
    $('#chart-conf').textContent = fmt.pct(k.avg_confidence);
    drawChart(data.series || []);

    const heroSub = $('#hero-sub');
    if (data.cameras_total) {
      heroSub.textContent = `${data.cameras_online} of ${data.cameras_total} cameras reporting. Every plate read, scored and archived in under a second.`;
    }

    $('#overview-cards').innerHTML = (data.recent || []).slice(0, 4).map(captureCard).join('')
      || emptyState('No captures yet', 'Run your first scan to populate the corridor.');

    $('#recent-list').innerHTML = (data.recent || []).slice(0, 6).map((item) => `
      <div class="list-row" data-capture="${item.id}">
        <img class="list-thumb" src="${item.thumbnail_url}" alt="" loading="lazy" />
        <div class="grow">
          <div class="list-plate">${fmt.escape(fmt.plate(item.plate))}</div>
          <div class="list-sub">${fmt.escape(item.camera_name || 'Manual upload')} · ${fmt.escape(item.plate_type)}</div>
        </div>
        <div class="list-place">${fmt.escape(item.region_name || '')}</div>
        <span class="status-pill ${item.status}">${item.status}</span>
        <div class="list-time right">${fmt.when(item.created_at)}</div>
      </div>`).join('') || emptyState('Nothing logged yet', 'Captures appear here the moment they are read.');

    renderWatchMini(data.watchlist || []);
  }

  function renderWatchMini(entries) {
    const host = $('#watchlist-mini');
    if (!entries.length) {
      host.innerHTML = emptyState('Watchlist is empty', 'Add a plate to start enforcing.');
      return;
    }
    host.innerHTML = entries.slice(0, 5).map((entry) => `
      <div class="spread" style="padding:9px 0;border-bottom:1px solid var(--line)">
        <div class="grow">
          <div class="list-plate" style="font-size:12.5px">${fmt.escape(fmt.plate(entry.plate))}</div>
          <div class="list-sub">${fmt.escape(entry.label || entry.category)}</div>
        </div>
        <span class="plate-badge risk-${entry.severity}">${entry.severity}</span>
      </div>`).join('');
  }

  function drawChart(series) {
    const svg = $('#chart');
    if (!svg || !series.length) return;
    const W = 640, H = 150, padX = 8, padTop = 12, padBottom = 24;
    const values = series.map((p) => p.scans);
    const max = Math.max(1, ...values);
    const stepX = series.length > 1 ? (W - padX * 2) / (series.length - 1) : 0;
    const y = (v) => H - padBottom - (v / max) * (H - padTop - padBottom);
    const points = series.map((p, i) => [padX + i * stepX, y(p.scans)]);
    const line = points.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    const area = `${line} L${points.at(-1)[0].toFixed(1)},${H - padBottom} L${points[0][0].toFixed(1)},${H - padBottom} Z`;
    $('#chart-line').setAttribute('d', line);
    $('#chart-area').setAttribute('d', area);

    const grid = [];
    for (let i = 0; i <= 3; i += 1) {
      const gy = padTop + ((H - padTop - padBottom) / 3) * i;
      grid.push(`<line class="chart-grid" x1="0" y1="${gy}" x2="${W}" y2="${gy}" />`);
    }
    $('#chart-grid').innerHTML = grid.join('');

    const axis = $('#chart-axis');
    if (axis) {
      const stride = series.length > 10 ? Math.ceil(series.length / 6) : 1;
      axis.innerHTML = series
        .map((p, i) => (i % stride === 0 || i === series.length - 1 ? `<span>${fmt.escape(p.label)}</span>` : ''))
        .join('');
    }

    $('#chart-dots').innerHTML = points.map((p, i) => {
      const flagged = series[i].flagged > 0;
      return `<circle class="chart-dot" cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="${flagged ? 3.4 : 2.2}" ${flagged ? 'style="stroke:var(--danger)"' : ''} />`;
    }).join('');
  }

  $$('#range-chips .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      $$('#range-chips .chip').forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      state.range = chip.dataset.range;
      loadOverview();
    });
  });

  /* ------------------------------------------------------------------ cards */
  function captureCard(item) {
    const plate = fmt.plate(item.plate);
    const conf = Math.round((item.confidence || 0) * 100);
    return `
      <article class="plate-card" data-capture="${item.id}">
        <div class="plate-thumb" style="background-image:url('${SCENE_IMAGES[(item.diagnostics && item.diagnostics.scene) || 'dusk']}')">
          <img src="${item.thumbnail_url}" alt="Capture ${fmt.escape(plate)}" loading="lazy" />
          <div class="plate-thumb-top">
            <span class="plate-badge risk-${item.risk_level}">${item.risk_level}</span>
            ${item.watchlist_hit ? `<span class="plate-badge" style="border-color:rgba(255,107,107,.5);color:#ffd3d3">Watchlist</span>` : ''}
          </div>
          <div class="plate-thumb-plate">
            <p class="plate-read">${fmt.escape(plate)}</p>
            <p class="plate-meta-line">${fmt.escape(item.camera_name || 'Manual upload')} · ${fmt.when(item.created_at)}</p>
          </div>
        </div>
        <div class="plate-foot">
          <span class="place">${fmt.escape(item.region_name || item.country)}</span>
          <span class="conf-meter"><span class="conf-bar"><i style="width:${conf}%"></i></span>${conf}%</span>
        </div>
      </article>`;
  }

  function emptyState(title, copy) {
    return `<div class="empty" style="grid-column:1/-1"><h4>${fmt.escape(title)}</h4><p>${fmt.escape(copy)}</p></div>`;
  }

  /* ---------------------------------------------------------------- library */
  async function loadLibrary() {
    const { page, status, risk, sort } = state.library;
    const params = new URLSearchParams({ page, page_size: 8, sort, status });
    if (risk) params.set('risk', risk);
    const q = $('#global-search').value.trim();
    if (q) params.set('q', q);
    try {
      const data = await api(`/captures?${params}`);
      state.library.total = data.total;
      state.library.pages = data.pages;
      $('#library-sub').textContent = `${fmt.int(data.total)} recognition events · page ${data.page} of ${data.pages}`;
      const grid = $('#library-grid');
      grid.innerHTML = data.items.length
        ? data.items.map(captureCard).join('')
        : emptyState('No captures match', 'Adjust the filters or run a new scan.');
      const pager = $('#library-pager');
      pager.innerHTML = data.pages > 1
        ? `<button class="btn btn-ghost btn-sm" data-page="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>Previous</button>
           <span>Page ${data.page} / ${data.pages}</span>
           <button class="btn btn-ghost btn-sm" data-page="${data.page + 1}" ${data.page >= data.pages ? 'disabled' : ''}>Next</button>`
        : '';
      $$('#library-pager [data-page]').forEach((button) => {
        button.addEventListener('click', () => {
          state.library.page = Number(button.dataset.page);
          loadLibrary();
        });
      });
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  $$('#library-filters .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      if (chip.dataset.status) {
        $$('#library-filters .chip[data-status]').forEach((c) => c.classList.remove('active'));
        state.library.status = chip.dataset.status;
        chip.classList.add('active');
      } else {
        const next = state.library.risk === chip.dataset.risk ? null : chip.dataset.risk;
        $$('#library-filters .chip[data-risk]').forEach((c) => c.classList.remove('active'));
        state.library.risk = next;
        if (next) chip.classList.add('active');
      }
      state.library.page = 1;
      loadLibrary();
    });
  });

  $('#sort-select').addEventListener('change', (event) => {
    state.library.sort = event.target.value;
    state.library.page = 1;
    loadLibrary();
  });

  let searchTimer = null;
  $('#global-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      if (state.view !== 'library') switchView('library');
      else { state.library.page = 1; loadLibrary(); }
    }, 380);
  });

  $('#export-btn').addEventListener('click', async () => {
    try {
      const res = await fetch(`${API}/captures/export.csv`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error('Export failed');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `platevision-captures-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      toast('CSV exported');
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  /* ----------------------------------------------------------------- drawer */
  document.addEventListener('click', (event) => {
    const card = event.target.closest('[data-capture]');
    if (!card) return;
    openCapture(card.dataset.capture);
  });

  async function openCapture(id) {
    $('#drawer').classList.add('open');
    $('#drawer-scrim').classList.add('open');
    $('#drawer-body').innerHTML = `<div class="drawer-section"><div class="skeleton" style="height:220px"></div></div>`;
    try {
      const item = await api(`/captures/${id}`);
      state.selectedCapture = item;
      renderCaptureDrawer(item);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function renderCaptureDrawer(item) {
    $('#drawer-title').textContent = `Capture · ${fmt.when(item.created_at)}`;
    const conf = Math.round((item.confidence || 0) * 100);
    const diag = item.diagnostics || {};
    const glyphs = (item.candidates && item.candidates[0] && item.candidates[0].glyphs) || [];
    const alternates = item.alternates || [];

    $('#drawer-body').innerHTML = `
      <div class="drawer-media">
        <img src="${item.image_url}" alt="Annotated capture" />
      </div>
      <div class="drawer-section">
        <div class="spread">
          <div>
            <p class="result-caption">${item.watchlist_hit ? 'Watchlist match' : 'Recognition'}</p>
            <p class="drawer-plate">${fmt.escape(fmt.plate(item.plate))}</p>
            <p class="drawer-sub">${fmt.escape(diag.region_name || item.region_name || '')} · ${fmt.escape(item.plate_type)} · ${fmt.escape(String(item.country))}</p>
          </div>
          <span class="status-pill ${item.status}">${item.status}</span>
        </div>
        <div class="result-stats">
          <div>
            <div class="result-stat-label">Confidence</div>
            <div class="result-stat-value" style="color:${conf >= 80 ? 'var(--mint)' : conf >= 60 ? 'var(--amber)' : 'var(--danger)'}">${conf}%</div>
          </div>
          <div>
            <div class="result-stat-label">Risk</div>
            <div class="result-stat-value">${item.risk_score} <span class="muted" style="font-size:11px">${item.risk_level}</span></div>
          </div>
          <div>
            <div class="result-stat-label">Latency</div>
            <div class="result-stat-value">${Math.round(item.processing_ms)} ms</div>
          </div>
          <div>
            <div class="result-stat-label">Engine</div>
            <div class="result-stat-value" style="font-size:12px">${fmt.escape(item.engine_version)}</div>
          </div>
        </div>
      </div>

      <div class="drawer-section">
        <div class="card-head"><h4 class="card-title">Review</h4><span class="muted" style="font-size:10.5px">${item.reviewed_by ? `by ${fmt.escape(item.reviewed_by)}` : 'unreviewed'}</span></div>
        <div class="controls">
          <button class="ctrl" data-action="prev" title="Previous">${icons.prev}</button>
          <button class="ctrl" data-action="denied" title="Deny">${icons.deny}</button>
          <button class="ctrl ctrl-main" data-action="verified" title="Verify">${icons.check}</button>
          <button class="ctrl" data-action="flagged" title="Flag">${icons.flag}</button>
          <button class="ctrl" data-action="next" title="Next">${icons.next}</button>
        </div>
        <div class="row" style="justify-content:center;gap:8px;margin-top:4px">
          <span class="muted" style="font-size:10.5px">Confidence</span>
          <span class="conf-bar" style="width:120px"><i style="width:${conf}%"></i></span>
          <span class="muted" style="font-size:10.5px">${conf}%</span>
        </div>
      </div>

      ${glyphs.length ? `
      <div class="drawer-section">
        <div class="card-head"><h4 class="card-title">Character read</h4><span class="muted" style="font-size:10.5px">${fmt.escape(diag.format || item.diagnostics?.format || '')}</span></div>
        <div class="glyph-strip">
          ${glyphs.map((g) => {
            const c = Math.round((g.confidence || 0) * 100);
            return `<div class="glyph-chip ${c < 55 ? 'vlow' : c < 75 ? 'low' : ''}"><b>${fmt.escape(g.label)}</b><i>${c}</i></div>`;
          }).join('')}
        </div>
      </div>` : ''}

      <div class="drawer-section">
        <div class="card-head"><h4 class="card-title">Details</h4></div>
        <div class="kv">
          <div class="kv-row"><span class="kv-label">Camera</span><span class="kv-value">${fmt.escape(item.camera_name || 'Manual upload')}</span></div>
          <div class="kv-row"><span class="kv-label">Corridor</span><span class="kv-value">${fmt.escape(item.corridor || '—')}</span></div>
          <div class="kv-row"><span class="kv-label">Captured</span><span class="kv-value">${new Date(item.created_at).toLocaleString('en-GB')}</span></div>
          <div class="kv-row"><span class="kv-label">Source</span><span class="kv-value">${fmt.escape(item.source)}</span></div>
          <div class="kv-row"><span class="kv-label">Resolution</span><span class="kv-value mono">${item.image?.width || 0} × ${item.image?.height || 0}</span></div>
          <div class="kv-row"><span class="kv-label">Operator</span><span class="kv-value">${fmt.escape(item.operator || '—')}</span></div>
        </div>
      </div>

      ${alternates.length ? `
      <div class="drawer-section">
        <div class="card-head"><h4 class="card-title">Alternate reads</h4></div>
        <div class="stack" style="gap:6px">
          ${alternates.map((alt) => `
            <div class="spread" style="font-size:12px">
              <span class="mono">${fmt.escape(fmt.plate(alt.plate))}</span>
              <span class="muted">${Math.round((alt.confidence || 0) * 100)}%</span>
            </div>`).join('')}
        </div>
      </div>` : ''}

      <div class="drawer-section">
        <div class="card-head"><h4 class="card-title">Operator note</h4></div>
        <textarea class="note-input" id="note-input" placeholder="Add context for the audit trail…">${fmt.escape(item.notes || '')}</textarea>
        <button class="btn btn-ghost btn-sm" id="save-note" style="margin-top:10px">Save note</button>
      </div>
    `;

    $('#save-note').addEventListener('click', async () => {
      try {
        await api(`/captures/${item.id}`, { method: 'PATCH', body: { notes: $('#note-input').value } });
        toast('Note saved to the audit trail');
      } catch (error) {
        toast(error.message, 'error');
      }
    });

    $$('#drawer-body [data-action]').forEach((button) => {
      button.addEventListener('click', async () => {
        const action = button.dataset.action;
        if (action === 'prev' || action === 'next') {
          stepCapture(action);
          return;
        }
        try {
          const updated = await api(`/captures/${item.id}`, { method: 'PATCH', body: { status: action } });
          state.selectedCapture = updated;
          renderCaptureDrawer(updated);
          toast(`Marked ${action}`);
          loadOverview();
          if (state.view === 'library') loadLibrary();
        } catch (error) {
          toast(error.message, 'error');
        }
      });
    });
  }

  async function stepCapture(direction) {
    const items = state.overview?.recent || [];
    if (!items.length) return;
    const index = items.findIndex((c) => c.id === state.selectedCapture?.id);
    const next = items[(index + (direction === 'next' ? 1 : -1) + items.length) % items.length];
    if (next) openCapture(next.id);
  }

  function closeDrawer() {
    $('#drawer').classList.remove('open');
    $('#drawer-scrim').classList.remove('open');
  }
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeDrawer();
      $('#modal-scrim').classList.remove('open');
    }
  });

  /* ------------------------------------------------------------------ watch */
  async function loadWatchlist() {
    try {
      const [entries, stats] = await Promise.all([api('/watchlist'), api('/watchlist/stats')]);
      state.watchlist = entries;
      const grid = $('#watchlist-grid');
      const scenes = Object.values(SCENE_IMAGES);
      grid.innerHTML = entries.length
        ? entries.map((entry, index) => `
          <article class="card wl-card">
            <div class="wl-photo">
              <img src="${scenes[index % scenes.length]}" alt="" loading="lazy" />
              <div class="wl-flag">${icons.flag}</div>
            </div>
            <div class="wl-body">
              <p class="wl-plate">${fmt.escape(fmt.plate(entry.plate))}</p>
              <p class="wl-cat">${fmt.escape(entry.category)} · ${fmt.escape(entry.severity)} · ${entry.hits} hits</p>
              <p class="wl-reason">${fmt.escape(entry.label || entry.reason || 'No description')}</p>
            </div>
          </article>`).join('')
        : emptyState('No watchlist entries', 'Add a plate to start enforcing.');

      const mix = entries.reduce((acc, entry) => {
        acc[entry.category] = (acc[entry.category] || 0) + 1;
        return acc;
      }, {});
      const total = entries.length || 1;
      $('#watch-mix').innerHTML = Object.entries(mix).map(([label, value]) => `
        <div>
          <div class="spread" style="font-size:11.5px;margin-bottom:5px">
            <span>${fmt.escape(label.replace(/^./, (c) => c.toUpperCase()))}</span>
            <span class="muted">${value}</span>
          </div>
          <div class="conf-bar" style="width:100%;height:4px"><i style="width:${(value / total) * 100}%;background:var(--lilac)"></i></div>
        </div>`).join('') || '<p class="muted" style="font-size:12px">No categories yet.</p>';

      const matches = await api('/watchlist/matches?limit=5');
      $('#watch-matches').innerHTML = matches.length
        ? matches.map((match) => `
          <div class="spread" style="padding:8px 0;border-bottom:1px solid var(--line)">
            <div class="grow">
              <div class="list-plate" style="font-size:12.5px">${fmt.escape(fmt.plate(match.plate))}</div>
              <div class="list-sub">${fmt.escape(match.camera || 'Manual')} · ${fmt.when(match.created_at)}</div>
            </div>
            <span class="plate-badge risk-${match.severity}">${fmt.escape(match.category)}</span>
          </div>`).join('')
        : '<p class="muted" style="font-size:12px">No matches recorded yet.</p>';

      renderWatchMini(entries);
      if (stats && stats.active !== undefined) {
        const label = $('#view-watchlist .section-sub');
        if (label) label.textContent = `${stats.active} active plates · ${stats.hits} lifetime hits`;
      }
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  $('#add-watch-btn').addEventListener('click', () => $('#modal-scrim').classList.add('open'));
  $('#modal-cancel').addEventListener('click', () => $('#modal-scrim').classList.remove('open'));
  $('#modal-scrim').addEventListener('click', (event) => {
    if (event.target.id === 'modal-scrim') $('#modal-scrim').classList.remove('open');
  });

  $('#watch-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api('/watchlist', {
        method: 'POST',
        body: {
          plate: $('#wl-plate').value,
          category: $('#wl-category').value,
          severity: $('#wl-severity').value,
          label: $('#wl-label').value,
          reason: $('#wl-reason').value,
        },
      });
      $('#modal-scrim').classList.remove('open');
      $('#watch-form').reset();
      toast('Plate added to the watchlist');
      loadWatchlist();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  /* ------------------------------------------------------------------- scan */
  async function loadSamples() {
    try {
      const data = await api('/recognize/samples?limit=6');
      state.samples = data.items;
      const scenes = Object.values(SCENE_IMAGES);
      $('#sample-grid').innerHTML = data.items.slice(0, 6).map((sample, index) => `
        <article class="plate-card" data-sample="${sample.id}">
          <div class="plate-thumb" style="background-image:url('${scenes[index % scenes.length]}')">
            <img src="/api/v1/recognize/samples/${sample.id}.jpg" alt="${fmt.escape(sample.label)}" loading="lazy" />
            <div class="plate-thumb-top"><span class="plate-badge">${fmt.escape(sample.scene)}</span></div>
            <div class="plate-thumb-plate">
              <p class="plate-read" style="font-size:19px">${fmt.escape(fmt.plate(sample.plate))}</p>
              <p class="plate-meta-line">${fmt.escape(sample.kind)} plate</p>
            </div>
          </div>
        </article>`).join('');
      $$('#sample-grid [data-sample]').forEach((card) => {
        card.addEventListener('click', () => runSample(card.dataset.sample));
      });
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  const dropzone = $('#dropzone');
  const fileInput = $('#file-input');
  $('#browse-btn').addEventListener('click', (event) => {
    event.stopPropagation();
    fileInput.click();
  });
  dropzone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    if (fileInput.files && fileInput.files[0]) runUpload(fileInput.files[0]);
  });
  ['dragenter', 'dragover'].forEach((type) => dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.add('dragging');
  }));
  ['dragleave', 'drop'].forEach((type) => dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.remove('dragging');
  }));
  dropzone.addEventListener('drop', (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) runUpload(file);
  });

  function setPreview(url, scanning) {
    const stage = $('#preview-stage');
    stage.innerHTML = `<img src="${url}" alt="Frame under analysis" />${scanning ? '<div class="scan-progress"><i></i></div>' : ''}`;
  }

  async function runSample(sampleId) {
    if (state.scanning) return;
    switchView('scan');
    state.scanning = true;
    const url = `/api/v1/recognize/samples/${sampleId}.jpg`;
    setPreview(url, true);
    $('#result-detail').innerHTML = `<div class="card card-pad"><div class="skeleton" style="height:96px"></div></div>`;
    try {
      const result = await api(`/recognize/samples/${sampleId}`, { method: 'POST' });
      state.lastResult = result;
      renderResult(result);
      if (result.diagnostics?.overlay_b64) {
        setPreview(result.diagnostics.overlay_b64, false);
      }
      toast('Plate read in ' + Math.round(result.processing_ms) + ' ms');
    } catch (error) {
      toast(error.message, 'error');
      setPreview(url, false);
    } finally {
      state.scanning = false;
    }
  }

  async function runUpload(file) {
    if (state.scanning) return;
    if (file.size > 12 * 1024 * 1024) {
      toast('Frame is larger than 12 MB', 'error');
      return;
    }
    switchView('scan');
    state.scanning = true;
    const localUrl = URL.createObjectURL(file);
    setPreview(localUrl, true);
    $('#result-detail').innerHTML = `<div class="card card-pad"><div class="skeleton" style="height:96px"></div></div>`;
    const form = new FormData();
    form.append('file', file);
    form.append('include_overlay', 'true');
    try {
      const result = await api('/recognize', { method: 'POST', body: form });
      state.lastResult = result;
      renderResult(result);
      if (result.diagnostics?.overlay_b64) setPreview(result.diagnostics.overlay_b64, false);
      toast('Plate read');
      loadOverview();
    } catch (error) {
      toast(error.message, 'error');
      setPreview(localUrl, false);
    } finally {
      state.scanning = false;
      URL.revokeObjectURL(localUrl);
    }
  }

  function renderResult(result) {
    const best = result.best;
    if (!best) {
      $('#result-detail').innerHTML = `
        <div class="card card-pad">
          <div class="card-head"><h4 class="card-title">No plate found</h4></div>
          <p class="muted" style="font-size:12.5px;line-height:1.6">
            The engine scanned ${result.image.width}×${result.image.height} in ${Math.round(result.processing_ms)} ms and
            could not isolate a plate. Try a frame where the plate spans at least 8% of the width.
          </p>
        </div>`;
      return;
    }
    const conf = Math.round(best.confidence * 100);
    const plate = fmt.plate(best.plate);
    const glyphs = best.glyphs || [];
    const alternates = best.alternates || [];
    $('#result-detail').innerHTML = `
      <div class="result-hero">
        <p class="result-caption">${fmt.escape(best.format || 'read')} · ${fmt.escape(best.format_valid ? 'format valid' : 'low confidence')}</p>
        <p class="result-plate">${fmt.escape(plate.slice(0, -4))}<span>${fmt.escape(plate.slice(-4))}</span></p>
        <p class="muted" style="font-size:11.5px;margin:0">
          ${fmt.escape(best.country)} · ${fmt.escape(best.diagnostics?.region_name || '')} · ${fmt.escape(best.plate_type)}
        </p>
        <div class="result-stats">
          <div><div class="result-stat-label">Confidence</div><div class="result-stat-value" style="color:${conf >= 80 ? 'var(--mint)' : conf >= 60 ? 'var(--amber)' : 'var(--danger)'}">${conf}%</div></div>
          <div><div class="result-stat-label">Latency</div><div class="result-stat-value">${Math.round(result.processing_ms)} ms</div></div>
          <div><div class="result-stat-label">Plates</div><div class="result-stat-value">${result.plates.length}</div></div>
          <div><div class="result-stat-label">Quality</div><div class="result-stat-value">${Math.round((result.diagnostics?.quality || 0) * 100)}%</div></div>
        </div>
      </div>

      <div class="card card-pad">
        <div class="card-head"><h4 class="card-title">Character read</h4><span class="muted" style="font-size:10.5px">${glyphs.length} glyphs</span></div>
        <div class="glyph-strip">
          ${glyphs.map((g) => {
            const c = Math.round(g.confidence * 100);
            return `<div class="glyph-chip ${c < 55 ? 'vlow' : c < 75 ? 'low' : ''}"><b>${fmt.escape(g.label)}</b><i>${c}</i></div>`;
          }).join('') || '<p class="muted" style="font-size:12px">Glyph strip unavailable.</p>'}
        </div>
      </div>

      ${alternates.length ? `
      <div class="card card-pad">
        <div class="card-head"><h4 class="card-title">Alternates</h4></div>
        <div class="stack" style="gap:6px">
          ${alternates.map((alt) => `<div class="spread" style="font-size:12px"><span class="mono">${fmt.escape(fmt.plate(alt.plate))}</span><span class="muted">${Math.round(alt.confidence * 100)}%</span></div>`).join('')}
        </div>
      </div>` : ''}

      <div class="row" style="gap:10px">
        <button class="btn btn-primary btn-block" id="save-capture">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 12.5 9 16.5 19 6.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          Save to library
        </button>
        <button class="btn btn-ghost btn-sm" id="rescan-sample">Try another sample</button>
      </div>
    `;
    $('#save-capture')?.addEventListener('click', () => toast('Capture already archived by the recognition service', 'ok'));
    $('#rescan-sample')?.addEventListener('click', () => {
      const index = Math.floor(Math.random() * Math.min(6, state.samples.length));
      if (state.samples[index]) runSample(state.samples[index].id);
    });
  }

  $('#api-link').addEventListener('click', () => window.open('/docs', '_blank'));

  /* -------------------------------------------------------------- analytics */
  async function loadAnalytics() {
    if (!state.overview) await loadOverview();
    const data = state.overview;
    if (!data) return;
    const blocks = [
      { title: 'Plate type', items: data.plate_types },
      { title: 'Risk mix', items: data.risk_mix },
      { title: 'Review status', items: data.status_mix },
      { title: 'Corridors', items: data.corridors },
    ];
    $('#breakdown-row').innerHTML = blocks.slice(0, 3).map((block) => `
      <div class="card card-pad">
        <div class="card-head"><h3 class="card-title">${block.title}</h3></div>
        <div class="stack" style="gap:9px">
          ${(block.items || []).slice(0, 5).map((item) => `
            <div>
              <div class="spread" style="font-size:11.5px;margin-bottom:5px">
                <span>${fmt.escape(item.label)}</span><span class="muted">${item.value} · ${item.share}%</span>
              </div>
              <div class="conf-bar" style="width:100%;height:4px"><i style="width:${Math.min(100, item.share)}%"></i></div>
            </div>`).join('') || '<p class="muted" style="font-size:12px">No data for this period.</p>'}
        </div>
      </div>`).join('');

    const cameras = state.cameras.length ? state.cameras : await api('/cameras');
    state.cameras = cameras;
    $('#fleet-sub').textContent = `${cameras.filter((c) => c.status === 'online').length} of ${cameras.length} cameras online`;
    $('#camera-list').innerHTML = cameras.map((camera) => `
      <div class="list-row" style="grid-template-columns:auto minmax(0,1fr) auto auto">
        <span class="plate-badge">${fmt.escape(camera.code)}</span>
        <div class="grow">
          <div class="list-plate" style="font-size:12.5px">${fmt.escape(camera.name)}</div>
          <div class="list-sub">${fmt.escape(camera.location || camera.corridor)} · ${camera.lanes} lanes · ${camera.fps} fps</div>
        </div>
        <span class="list-place">${fmt.escape(camera.firmware)}</span>
        <span class="status-pill ${camera.status === 'online' ? 'verified' : camera.status === 'degraded' ? 'pending' : 'flagged'}">${camera.status}</span>
      </div>`).join('');
  }

  async function loadCameras() {
    try {
      state.cameras = await api('/cameras');
    } catch (error) {
      /* non-fatal */
    }
  }

  /* ------------------------------------------------------------------- boot */
  $('#refresh-btn').addEventListener('click', async () => {
    await Promise.all([loadOverview(), loadWatchlist()]);
    if (state.view === 'library') loadLibrary();
    toast('Workspace refreshed');
  });

  // Tiny clock in the hero, matching the reference's status bar.
  setInterval(() => {
    const clock = $('#hero-clock');
    if (clock) clock.textContent = fmt.clock();
  }, 30000);

  // Auto-refresh the overview while the console is idle.
  setInterval(() => {
    if (state.view === 'overview' && state.user && document.visibilityState === 'visible') loadOverview();
  }, 60000);

  boot();
})();
