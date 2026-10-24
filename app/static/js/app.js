/* =========================================================================
   PlateVision — iOS-style camera console
   Dark viewfinder in both themes; sheets, lists and grids follow the system
   appearance. Dependency-free, no inline handlers (the CSP forbids them).
   ========================================================================= */
(() => {
  'use strict';

  const API = '/api/v1';
  const $ = (sel, root = document) => root.querySelector(sel);

  const COUNTRIES = ['IN', 'US', 'UK', 'EU', 'GENERIC'];
  const SCAN_INTERVAL_FLOOR = 2600;   // ms — never poll faster than this
  const SCENE_THRESHOLD = 6;          // mean luma delta that counts as a new scene

  const state = {
    user: null,
    config: null,
    country: 'IN',
    mode: 'scan',            // photo | scan | import
    facing: 'environment',
    stream: null,
    track: null,
    torch: false,
    captures: [],
    total: 0,
    page: 1,
    pages: 1,
    busy: false,
    lastReadMs: 0,
    autoTimer: null,
    lastSignature: null,
    historyFilter: 'all',    // all | today | week
  };

  /* ─────────────────────────────────────────────────────────── helpers ── */
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));

  const plateOf = (capture) => capture.plate_display || capture.plate || 'Unread';

  const pct = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? `${(n * 100).toFixed(0)}%` : '—';
  };

  const timeOf = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? '—'
      : date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  };

  const dayKey = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'unknown' : date.toDateString();
  };

  const dayLabel = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Earlier';
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    if (date.toDateString() === today.toDateString()) return 'Today';
    if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
    return date.toLocaleDateString([], { day: 'numeric', month: 'long', year: 'numeric' });
  };

  const icons = {
    check: '<svg viewBox="0 0 24 24"><path d="m4.5 12.5 5 5L20 6.5"/></svg>',
    alert: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.2v.3"/></svg>',
    down: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
    share: '<svg viewBox="0 0 24 24"><path d="M12 15V3m0 0L8 7m4-4 4 4"/><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/></svg>',
    chevron: '<svg viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></svg>',
    photos: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="1.6"/><path d="m4 17 5-4 3.5 3 3-2.5L20 17"/></svg>',
    car: '<svg viewBox="0 0 24 24"><path d="M5 16h14M7 16v2M17 16v2"/><path d="M4.5 16 6 9.5A2 2 0 0 1 8 8h8a2 2 0 0 1 2 1.5L19.5 16z"/></svg>',
    clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
    chip: '<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3"/></svg>',
  };

  /* ───────────────────────────────────────────────────────── requests ── */
  async function api(path, options = {}) {
    const { body, method = 'GET', headers = {} } = options;
    const init = { method, headers: { ...headers }, credentials: 'same-origin' };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await fetch(`${API}${path}`, init);
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      const message = payload?.error?.message || `Request failed (${response.status})`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  /* ─────────────────────────────────────────────────────────── toasts ── */
  function toast(message, kind = 'ok') {
    const layer = $('#toast-layer');
    if (!layer) return;
    const node = document.createElement('div');
    node.className = `toast${kind === 'error' ? ' is-error' : ''}`;
    node.innerHTML = `${kind === 'error' ? icons.alert : icons.check}<span>${esc(message)}</span>`;
    layer.appendChild(node);
    setTimeout(() => {
      node.classList.add('is-out');
      setTimeout(() => node.remove(), 260);
    }, kind === 'error' ? 4000 : 2400);
  }

  /* ────────────────────────────────────────────────────────── camera ── */
  function setStatus(text) {
    const node = $('#status-text');
    if (node) node.textContent = text;
  }

  async function startCamera() {
    const camera = $('#camera');
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus('NO CAMERA');
      $('#vf-hint').innerHTML = '<span>Camera unavailable — tap IMPORT</span>';
      return;
    }
    stopCamera();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: state.facing },
          width: { ideal: 1920 },
          height: { ideal: 1080 },
        },
        audio: false,
      });
      state.stream = stream;
      state.track = stream.getVideoTracks()[0] || null;
      const video = $('#cam');
      video.srcObject = stream;
      await video.play().catch(() => {});
      camera.classList.add('is-live');
      camera.classList.remove('has-plate');
      setStatus('LIVE');
      applyTorch();
      scheduleAuto();
    } catch (error) {
      setStatus('CAMERA OFF');
      $('#vf-hint').innerHTML = '<span>Allow camera access, or tap IMPORT</span>';
    }
  }

  function stopCamera() {
    clearTimeout(state.autoTimer);
    state.autoTimer = null;
    if (state.stream) {
      state.stream.getTracks().forEach((track) => track.stop());
      state.stream = null;
      state.track = null;
    }
    $('#camera')?.classList.remove('is-live');
    setStatus('STANDBY');
  }

  async function flipCamera() {
    state.facing = state.facing === 'environment' ? 'user' : 'environment';
    state.torch = false;
    $('#torch-btn').classList.remove('is-on');
    await startCamera();
  }

  async function applyTorch() {
    if (!state.track?.applyConstraints) return;
    try {
      await state.track.applyConstraints({ advanced: [{ torch: state.torch }] });
    } catch (_) {
      /* most front cameras have no torch — the button simply does nothing */
    }
  }

  function toggleTorch() {
    state.torch = !state.torch;
    applyTorch();
    const button = $('#torch-btn');
    button.classList.toggle('is-on', state.torch);
    button.setAttribute('aria-pressed', String(state.torch));
  }

  /* ───────────────────────────────────────────────────────── capture ── */
  function grabFrame() {
    const video = $('#cam');
    if (!video || !video.videoWidth) return Promise.resolve(null);
    const canvas = $('#snapshot');
    const width = Math.min(video.videoWidth, 1920);
    const height = Math.round((width / video.videoWidth) * video.videoHeight);
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) return Promise.resolve(null);
    context.drawImage(video, 0, 0, width, height);
    return new Promise((resolve) => canvas.toBlob((blob) => resolve(blob), 'image/jpeg', 0.92));
  }

  async function scanBlob(blob, { source = 'stream', quiet = false } = {}) {
    if (!blob) {
      if (!quiet) toast('No frame — allow the camera or use IMPORT', 'error');
      return null;
    }
    if (state.busy) return null;

    state.busy = true;
    const started = performance.now();
    $('#shutter').dataset.busy = '1';
    $('#vf-progress').hidden = false;
    setStatus('READING');

    const form = new FormData();
    form.append('file', blob, `frame-${Date.now()}.jpg`);
    form.append('country', state.country);
    form.append('source', source);

    try {
      const capture = await api('/captures', { method: 'POST', body: form });
      state.lastReadMs = performance.now() - started;
      state.captures.unshift(capture);
      state.total += 1;
      renderThumb();
      flashReadout(capture);
      if (!quiet) openResult(capture);
      return capture;
    } catch (error) {
      if (!quiet) toast(error.message, 'error');
      if (error.status === 401) showGate();
      return null;
    } finally {
      state.busy = false;
      $('#shutter').dataset.busy = '0';
      $('#vf-progress').hidden = true;
      setStatus(state.stream ? 'LIVE' : 'STANDBY');
      scheduleAuto();
    }
  }

  function flashReadout(capture) {
    if (!capture?.plate) return;
    const node = $('#vf-readout');
    $('#readout-plate').textContent = plateOf(capture);
    $('#readout-conf').textContent = `${pct(capture.confidence)} confidence`;
    node.hidden = false;
    $('#camera').classList.add('has-plate');
    clearTimeout(flashReadout.timer);
    flashReadout.timer = setTimeout(() => {
      node.hidden = true;
      $('#camera').classList.remove('has-plate');
    }, 3800);
  }

  /* ──────────────────────────────────────────────────── auto scanning ── */
  /**
   * Interval adapts to how long a read actually takes. On Render's free CPU a
   * single read can run over ten seconds; a fixed 3 s poll would stack frames
   * faster than the engine drains them, so wait for the last read plus headroom.
   */
  function nextInterval() {
    return Math.max(SCAN_INTERVAL_FLOOR, Math.round(state.lastReadMs * 1.5));
  }

  function scheduleAuto() {
    clearTimeout(state.autoTimer);
    state.autoTimer = null;
    if (state.mode !== 'scan' || !state.stream) return;
    state.autoTimer = setTimeout(autoTick, nextInterval());
  }

  /**
   * Cheap perceptual fingerprint of the current frame: 32x18 grayscale.
   *
   * Auto-scan must not re-read a scene that has not changed. A parked car would
   * otherwise be recognised every tick — 10 s of CPU on a free instance — and
   * bury the history in duplicate rows. Returns null when the platform gives no
   * pixel access (jsdom, some embedded webviews), in which case scanning simply
   * proceeds.
   */
  function sceneSignature() {
    const video = $('#cam');
    if (!video?.videoWidth) return null;
    try {
      const canvas = document.createElement('canvas');
      canvas.width = 32;
      canvas.height = 18;
      const context = canvas.getContext('2d', { willReadFrequently: true });
      if (!context?.getImageData) return null;
      context.drawImage(video, 0, 0, 32, 18);
      const { data } = context.getImageData(0, 0, 32, 18);
      const gray = new Float32Array(32 * 18);
      for (let i = 0; i < gray.length; i += 1) {
        const o = i * 4;
        gray[i] = data[o] * 0.299 + data[o + 1] * 0.587 + data[o + 2] * 0.114;
      }
      return gray;
    } catch (_) {
      return null;
    }
  }

  function sceneChanged(signature) {
    if (!signature || !state.lastSignature) return true;
    let delta = 0;
    for (let i = 0; i < signature.length; i += 1) {
      delta += Math.abs(signature[i] - state.lastSignature[i]);
    }
    return delta / signature.length > SCENE_THRESHOLD;
  }

  async function autoTick() {
    const sheetOpen = $('#sheet-layer').hidden === false;
    if (state.mode !== 'scan' || !state.stream || state.busy || document.hidden || sheetOpen) {
      scheduleAuto();
      return;
    }
    const signature = sceneSignature();
    if (!sceneChanged(signature)) {
      scheduleAuto();
      return;
    }
    const blob = await grabFrame();
    if (!blob) {
      scheduleAuto();
      return;
    }
    await scanBlob(blob, { source: 'stream', quiet: true });
    state.lastSignature = signature;
    scheduleAuto();
  }

  async function shutterPress() {
    if (state.mode === 'import') {
      $('#file-picker').click();
      return;
    }
    if (!state.stream) {
      toast('No camera — tap IMPORT to scan a photo', 'error');
      return;
    }
    const blob = await grabFrame();
    await scanBlob(blob, { source: 'stream' });
  }

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll('.mode').forEach((button) => {
      const active = button.dataset.mode === mode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', String(active));
    });
    if (mode === 'import') $('#file-picker').click();
    scheduleAuto();
  }

  /* ───────────────────────────────────────────────────────────── data ── */
  async function loadHistory() {
    try {
      const data = await api('/captures?page=1&page_size=48&sort=recent');
      state.captures = data.items || [];
      state.total = data.total ?? state.captures.length;
      state.page = data.page;
      state.pages = data.pages;
      renderThumb();
      return data;
    } catch (error) {
      if (error.status === 401) showGate();
      return null;
    }
  }

  function renderThumb() {
    const badge = $('#scan-count');
    if (badge) {
      badge.textContent = String(state.total);
      badge.hidden = state.total === 0;
    }
    const thumb = $('#last-thumb');
    const latest = state.captures[0];
    if (thumb && latest) thumb.src = latest.thumbnail_url;
  }

  /* ──────────────────────────────────────────────────────────── sheets ── */
  const layer = () => $('#sheet-layer');
  const sheet = () => $('#sheet');

  function openSheet(html, { tight = false } = {}) {
    const node = sheet();
    node.innerHTML = `
      <div class="sheet-grabber" aria-hidden="true"></div>
      ${html}`;
    node.classList.toggle('is-tight', tight);
    layer().hidden = false;
    document.body.style.overflow = 'hidden';
    const body = $('.sheet-body', node);
    if (body) body.scrollTop = 0;
  }

  function closeSheet() {
    layer().hidden = true;
    sheet().innerHTML = '';
    document.body.style.overflow = '';
    scheduleAuto();
  }

  function navBar(title, { left = '', right = '' } = {}) {
    return `
      <nav class="sheet-nav">
        ${left || '<span class="nav-btn" aria-hidden="true"></span>'}
        <span class="nav-title">${esc(title)}</span>
        ${right || '<span class="nav-btn" aria-hidden="true"></span>'}
      </nav>`;
  }

  /* ---------------------------------------------------------- result */
  function glyphStrip(capture) {
    const glyphs = capture.candidates?.[0]?.glyphs || [];
    const letters = (capture.plate || '').split('');
    if (!letters.length) return '';
    return `<div class="glyph-row">${letters.map((char, index) => {
      const confidence = glyphs[index]?.confidence;
      return `<span class="glyph"><b>${esc(char)}</b><span>${
        typeof confidence === 'number' ? pct(confidence) : '—'
      }</span></span>`;
    }).join('')}</div>`;
  }

  function openResult(capture, { fresh = false } = {}) {
    const rows = [
      ['Confidence', pct(capture.confidence)],
      ['Captured', `${dayLabel(capture.created_at)} · ${timeOf(capture.created_at)}`],
      ['Source', (capture.source || 'stream').replace(/^./, (c) => c.toUpperCase())],
      ['Camera', capture.camera_name || 'This device'],
      ['Jurisdiction', capture.region_name || capture.region || '—'],
      ['Engine', capture.engine_version || '—'],
    ];

    openSheet(`
      ${navBar(fresh ? 'Plate read' : 'Scan', {
        right: '<button class="nav-btn is-strong" data-action="close-sheet">Done</button>',
      })}
      <div class="sheet-body is-tight">
        <div class="result-frame">
          <img src="${esc(capture.image_url)}" alt="Captured frame" />
        </div>
        <div class="plate-head">
          <span class="plate-number">${esc(plateOf(capture))}</span>
          <span class="plate-conf">${pct(capture.confidence)}</span>
        </div>
        <p class="plate-sub">${esc((capture.plate_type || 'Unknown').replace(/^./, (c) => c.toUpperCase()))} plate · read in ${Math.round(Number(capture.processing_ms) || 0)} ms</p>
        ${glyphStrip(capture)}

        <div class="section-label">Details</div>
        <div class="card">
          ${rows.map(([label, value]) => `
            <div class="row">
              <span class="row-label">${esc(label)}</span>
              <span class="row-value">${esc(value)}</span>
            </div>`).join('')}
        </div>

        <div class="button-stack">
          <button class="ios-btn is-filled" data-action="download-capture" data-id="${esc(capture.id)}">
            ${icons.share} Download PDF
          </button>
          <button class="ios-btn" data-action="open-correct" data-id="${esc(capture.id)}">
            Correct plate
          </button>
        </div>
      </div>`);
  }

  /* --------------------------------------------------------- history */
  function filteredHistory() {
    const now = Date.now();
    return state.captures.filter((capture) => {
      if (state.historyFilter === 'all') return true;
      const then = new Date(capture.created_at).getTime();
      if (Number.isNaN(then)) return false;
      const window = state.historyFilter === 'today' ? 864e5 : 7 * 864e5;
      return now - then <= window;
    });
  }

  function openHistory() {
    const items = filteredHistory();
    const groups = [];
    for (const capture of items) {
      const key = dayKey(capture.created_at);
      let group = groups.find((entry) => entry.key === key);
      if (!group) {
        group = { key, label: dayLabel(capture.created_at), items: [] };
        groups.push(group);
      }
      group.items.push(capture);
    }

    const body = groups.length
      ? groups.map((group) => `
          <div class="grid-head">${esc(group.label)}</div>
          <div class="photo-grid">
            ${group.items.map((capture, index) => `
              <button class="photo-cell" data-action="open-capture" data-id="${esc(capture.id)}"
                      style="animation-delay:${Math.min(index * 18, 220)}ms" aria-label="${esc(plateOf(capture))}">
                <img src="${esc(capture.thumbnail_url)}" alt="" loading="lazy" />
                <span class="photo-plate">${esc(plateOf(capture))}</span>
                ${capture.status === 'flagged' || capture.watchlist_hit ? '<span class="photo-flag"></span>' : ''}
              </button>`).join('')}
          </div>`).join('')
      : `<div class="empty-state">
           ${icons.photos}
           <h3>No scans yet</h3>
           <p>Point the camera at a plate and tap the shutter. Everything you capture lands here.</p>
         </div>`;

    openSheet(`
      ${navBar('Scan history', {
        left: '<button class="nav-btn is-strong" data-action="close-sheet">Done</button>',
        right: '<button class="nav-btn is-strong" data-action="export-pdf">Export</button>',
      })}
      <div class="sheet-body is-tight">
        <div class="segmented" role="tablist">
          <button class="${state.historyFilter === 'all' ? 'is-active' : ''}" data-filter="all" role="tab">All</button>
          <button class="${state.historyFilter === 'today' ? 'is-active' : ''}" data-filter="today" role="tab">Today</button>
          <button class="${state.historyFilter === 'week' ? 'is-active' : ''}" data-filter="week" role="tab">This week</button>
        </div>
        ${body}
      </div>`);
  }

  /* --------------------------------------------------------- correct */
  function openCorrect(captureId) {
    const capture = state.captures.find((item) => item.id === captureId);
    if (!capture) return;
    openSheet(`
      ${navBar('Correct plate', {
        left: '<button class="nav-btn is-strong" data-action="close-sheet">Cancel</button>',
        right: '<button class="nav-btn is-strong" data-action="save-correct" data-id="' + esc(capture.id) + '">Save</button>',
      })}
      <div class="sheet-body">
        <p class="plate-sub" style="padding:6px 4px 14px">
          Type the plate exactly as printed. The characters the reader got wrong become
          training data for this camera.
        </p>
        <div class="card">
          <label class="field">
            <span class="field-label">Plate</span>
            <input id="correct-input" value="${esc(capture.plate || '')}"
                   autocomplete="off" autocapitalize="characters" spellcheck="false" />
          </label>
        </div>
      </div>`);
    const input = $('#correct-input');
    if (input) {
      input.focus();
      input.select();
    }
  }

  /* -------------------------------------------------------- settings */
  function openSettings() {
    const user = state.user || {};
    openSheet(`
      ${navBar('Settings', {
        right: '<button class="nav-btn is-strong" data-action="close-sheet">Done</button>',
      })}
      <div class="sheet-body is-tight">
        <div class="large-title">Account</div>
        <div class="card">
          <div class="row">
            <span class="row-icon">${icons.car}</span>
            <span class="row-label">${esc(user.full_name || user.email || 'Demo operator')}</span>
          </div>
          <div class="row">
            <span class="row-label">Role</span>
            <span class="row-value">${esc((user.role || 'operator').replace(/^./, (c) => c.toUpperCase()))}</span>
          </div>
          <div class="row">
            <span class="row-label">Workspace</span>
            <span class="row-value">${esc(user.organization || 'Demo workspace')}</span>
          </div>
        </div>

        <div class="section-label">Reader</div>
        <div class="card">
          <div class="row">
            <span class="row-icon is-green">${icons.chip}</span>
            <span class="row-label">Engine</span>
            <span class="row-value">${esc(state.config?.version || '1.0.0')}</span>
          </div>
          <div class="row">
            <span class="row-icon is-gold">${icons.clock}</span>
            <span class="row-label">Last read</span>
            <span class="row-value">${state.lastReadMs ? `${(state.lastReadMs / 1000).toFixed(1)} s` : '—'}</span>
          </div>
        </div>

        <div class="button-stack">
          <button class="ios-btn is-destructive" data-action="sign-out">Sign out</button>
        </div>
      </div>`);
  }

  /* --------------------------------------------------------- sign in */
  function showGate(message) {
    if ($('#gate')) return;
    const gate = document.createElement('div');
    gate.id = 'gate';
    gate.className = 'gate';
    gate.innerHTML = `
      <form class="gate-card" id="gate-form">
        <h1>Sign in</h1>
        <p>${esc(message || 'Use your operator account to continue.')}</p>
        <div class="card">
          <label class="field">
            <span class="field-label">Email</span>
            <input id="gate-email" type="email" autocomplete="username"
                   value="${esc(state.config?.demo_email || '')}" required />
          </label>
          <label class="field">
            <span class="field-label">Password</span>
            <input id="gate-password" type="password" autocomplete="current-password" required />
          </label>
        </div>
        <div class="button-stack">
          <button class="ios-btn is-filled" type="submit">Sign in</button>
        </div>
      </form>`;
    document.body.appendChild(gate);
    $('#gate-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        const payload = await api('/auth/login', {
          method: 'POST',
          body: {
            email: $('#gate-email').value.trim(),
            password: $('#gate-password').value,
          },
        });
        state.user = payload.user || null;
        gate.remove();
        await afterSignIn();
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }

  async function signOut() {
    try {
      await api('/auth/logout', { method: 'POST' });
    } catch (_) {
      /* cookie may already be gone */
    }
    state.user = null;
    state.captures = [];
    state.total = 0;
    closeSheet();
    renderThumb();
    showGate('Signed out.');
  }

  /* ----------------------------------------------------------- export */
  function download(url, filename) {
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    setTimeout(() => link.remove(), 0);
  }

  /* ─────────────────────────────────────────────────────────── events ── */
  document.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action], [data-country], [data-mode], [data-filter]');
    if (!target) return;

    if (target.dataset.country) {
      state.country = target.dataset.country;
      document.querySelectorAll('.zoom-pill').forEach((pill) => {
        pill.classList.toggle('is-active', pill.dataset.country === state.country);
      });
      return;
    }

    if (target.dataset.mode) {
      setMode(target.dataset.mode);
      return;
    }

    if (target.dataset.filter) {
      state.historyFilter = target.dataset.filter;
      openHistory();
      return;
    }

    switch (target.dataset.action) {
      case 'close-sheet':
        closeSheet();
        break;

      case 'toggle-torch':
        toggleTorch();
        break;

      case 'flip-camera':
        await flipCamera();
        break;

      case 'open-history':
        openHistory();
        break;

      case 'open-settings':
        openSettings();
        break;

      case 'open-capture': {
        const capture = state.captures.find((item) => item.id === target.dataset.id);
        if (capture) openResult(capture);
        break;
      }

      case 'download-capture': {
        const capture = state.captures.find((item) => item.id === target.dataset.id);
        download(`${API}/captures/${target.dataset.id}/report.pdf`,
          `platevision-${capture?.plate || 'scan'}.pdf`);
        toast('Preparing PDF…');
        break;
      }

      case 'export-pdf':
        download(`${API}/captures/export.pdf`, 'platevision-scans.pdf');
        toast('Exporting history…');
        break;

      case 'open-correct':
        openCorrect(target.dataset.id);
        break;

      case 'save-correct': {
        const input = $('#correct-input');
        const plate = (input?.value || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
        if (plate.length < 3) {
          toast('Enter the plate as printed', 'error');
          break;
        }
        try {
          const result = await api(`/captures/${target.dataset.id}/teach`, {
            method: 'POST',
            body: { plate },
          });
          const capture = state.captures.find((item) => item.id === target.dataset.id);
          if (capture) {
            capture.plate = result.capture.plate;
            capture.plate_display = result.capture.plate_display;
          }
          const learned = result.learned?.added || 0;
          toast(learned ? `Saved · ${learned} character${learned === 1 ? '' : 's'} learned` : 'Saved');
          closeSheet();
          renderThumb();
          await loadHistory();
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      case 'sign-out':
        await signOut();
        break;

      default:
        break;
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && $('#sheet-layer').hidden === false) closeSheet();
    if (event.code === 'Space' && event.target === document.body) {
      event.preventDefault();
      shutterPress();
    }
  });

  $('#shutter').addEventListener('click', shutterPress);

  $('#file-picker').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (file) await scanBlob(file, { source: 'upload' });
  });

  window.addEventListener('pagehide', stopCamera);

  /* ───────────────────────────────────────────────────────────── boot ── */
  async function afterSignIn() {
    await loadHistory();
    startCamera();
  }

  async function boot() {
    document.querySelector(`.zoom-pill[data-country="${state.country}"]`)?.classList.add('is-active');

    try {
      state.config = await api('/config');
    } catch (_) {
      state.config = null;
    }

    try {
      state.user = await api('/auth/me');
    } catch (_) {
      if (state.config?.demo_login) {
        try {
          const payload = await api('/auth/demo', { method: 'POST' });
          state.user = payload.user || null;
        } catch (_) {
          state.user = null;
        }
      }
    }

    if (!state.user) {
      showGate(state.config?.demo_login ? '' : 'Sign in with your operator account.');
      startCamera();
      return;
    }
    await afterSignIn();
  }

  boot();
})();
