/* =========================================================================
   PlateVision — camera console
   One screen: live viewfinder, shutter, scan history, PDF export.
   No framework, no build step, no inline handlers (the CSP forbids them).
   ========================================================================= */
(() => {
  'use strict';

  const API = '/api/v1';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const COUNTRIES = [
    { code: 'IN', label: 'INDIA' },
    { code: 'US', label: 'UNITED STATES' },
    { code: 'UK', label: 'UNITED KINGDOM' },
    { code: 'EU', label: 'EUROPE' },
    { code: 'GENERIC', label: 'ANY FORMAT' },
  ];

  const state = {
    user: null,
    config: null,
    country: 'IN',
    auto: true,
    torch: false,
    stream: null,
    track: null,
    captures: [],
    total: 0,
    page: 1,
    pages: 1,
    busy: false,
    recentPlates: new Map(),   // plate -> timestamp, used to de-duplicate live scans
    autoTimer: null,
    lastShotAt: 0,
  };

  /* ------------------------------------------------------------- helpers */
  const fmt = {
    escape(value) {
      return String(value ?? '').replace(/[&<>"']/g, (ch) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
      ));
    },
    plate(capture) {
      return capture.plate_display || capture.plate || 'UNREAD';
    },
    pct(value) {
      const n = Number(value);
      return Number.isFinite(n) ? `${(n * 100).toFixed(0)}%` : '—';
    },
    clock(value) {
      if (!value) return '—';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return '—';
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    },
    ago(value) {
      if (!value) return '—';
      const then = new Date(value).getTime();
      if (Number.isNaN(then)) return '—';
      const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
      if (seconds < 60) return `${seconds}s ago`;
      if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
      if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
      return `${Math.floor(seconds / 86400)}d ago`;
    },
    day(value) {
      if (!value) return '—';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return '—';
      return date.toLocaleDateString([], { day: '2-digit', month: 'short', year: 'numeric' });
    },
  };

  const icon = {
    check: '<svg viewBox="0 0 24 24"><path d="m4 12.5 5 5L20 6.5"/></svg>',
    alert: '<svg viewBox="0 0 24 24"><path d="M12 8v5m0 3.5v.5"/><circle cx="12" cy="12" r="9"/></svg>',
    close: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    download: '<svg viewBox="0 0 24 24"><path d="M12 4v12m0 0 4.5-4.5M12 16l-4.5-4.5M5 19h14"/></svg>',
  };

  function toast(message, kind = 'ok') {
    const root = $('#toasts');
    if (!root) return;
    const node = document.createElement('div');
    node.className = `toast${kind === 'error' ? ' is-error' : ''}`;
    node.innerHTML = `${kind === 'error' ? icon.alert : icon.check}<span>${fmt.escape(message)}</span>`;
    root.appendChild(node);
    setTimeout(() => {
      node.classList.add('is-out');
      setTimeout(() => node.remove(), 260);
    }, kind === 'error' ? 4200 : 2600);
  }

  async function api(path, options = {}) {
    const { body, method = 'GET', headers = {}, raw = false } = options;
    const init = { method, headers: { ...headers }, credentials: 'same-origin' };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await fetch(`${API}${path}`, init);
    if (raw) return response;
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload && payload.error ? payload.error.message : `Request failed (${response.status})`;
      const error = new Error(detail);
      error.status = response.status;
      error.code = payload && payload.error ? payload.error.code : null;
      throw error;
    }
    return payload;
  }

  /* --------------------------------------------------------------- camera */
  function cameraSupported() {
    return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  async function startCamera() {
    const finder = $('#finder');
    if (!cameraSupported()) {
      setFinderState('NO CAMERA');
      $('#finder-hint').innerHTML = '<span class="micro">CAMERA UNAVAILABLE · UPLOAD AN IMAGE</span>';
      finder.classList.add('has-plate');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      });
      state.stream = stream;
      state.track = stream.getVideoTracks()[0] || null;
      const video = $('#cam');
      video.srcObject = stream;
      await video.play().catch(() => {});
      finder.classList.add('is-live');
      finder.classList.remove('has-plate');
      setFinderState('LIVE');
      const settings = state.track && state.track.getSettings ? state.track.getSettings() : {};
      if (settings && settings.width) {
        $('#meta-res').textContent = `${Math.round(settings.height)}p`;
      }
      applyTorch();
      startAuto();
    } catch (error) {
      setFinderState('CAMERA BLOCKED');
      $('#finder-hint').innerHTML = '<span class="micro">ALLOW CAMERA ACCESS · OR UPLOAD A FILE</span>';
      $('#finder').classList.add('has-plate');
    }
  }

  function stopCamera() {
    stopAuto();
    if (state.stream) {
      state.stream.getTracks().forEach((track) => track.stop());
      state.stream = null;
      state.track = null;
    }
    $('#finder').classList.remove('is-live');
    setFinderState('STANDBY');
  }

  function setFinderState(label) {
    const node = $('#finder-state');
    if (node) node.textContent = label;
  }

  async function applyTorch() {
    if (!state.track || !state.track.applyConstraints) return;
    try {
      await state.track.applyConstraints({ advanced: [{ torch: state.torch }] });
    } catch (_) {
      /* many cameras have no torch; the button simply does nothing */
    }
  }

  function toggleTorch() {
    state.torch = !state.torch;
    applyTorch();
    $('#torch-btn').classList.toggle('is-off', !state.torch);
    $('#torch-btn').setAttribute('aria-pressed', String(state.torch));
    $('#meta-flash').textContent = state.torch ? 'ON' : '0.0';
    const meta = $('#meta-flash').closest('.meta-item');
    if (meta) meta.classList.toggle('is-on', state.torch);
  }

  /* -------------------------------------------------------------- capture */
  function grabFrame() {
    const video = $('#cam');
    if (!video || !video.videoWidth) return Promise.resolve(null);
    const canvas = $('#capture-canvas');
    const width = Math.min(video.videoWidth, 1920);
    const height = Math.round((width / video.videoWidth) * video.videoHeight);
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) return Promise.resolve(null);
    context.drawImage(video, 0, 0, width, height);
    return new Promise((resolve) => canvas.toBlob((blob) => resolve(blob), 'image/jpeg', 0.92));
  }

  async function scanBlob(blob, { source = 'stream', silent = false } = {}) {
    if (!blob) {
      if (!silent) toast('No frame available — allow the camera or upload an image', 'error');
      return null;
    }
    if (state.busy) return null;
    state.busy = true;
    const shutter = $('#shutter');
    shutter.dataset.busy = '1';
    $('#finder').classList.add('is-busy');
    setFinderState('READING');

    const form = new FormData();
    form.append('file', blob, `frame-${Date.now()}.jpg`);
    form.append('country', state.country);
    form.append('source', source);

    try {
      const capture = await api('/captures', { method: 'POST', body: form });
      state.captures.unshift(capture);
      state.total += 1;
      if (capture.plate) state.recentPlates.set(capture.plate, Date.now());
      renderStrip();
      renderDock();
      showPlateFlash(capture);
      if (!silent) openResult(capture);
      return capture;
    } catch (error) {
      if (!silent) toast(error.message, 'error');
      if (error.status === 401) showSignin();
      return null;
    } finally {
      state.busy = false;
      shutter.dataset.busy = '0';
      $('#finder').classList.remove('is-busy');
      setFinderState(state.stream ? 'LIVE' : 'STANDBY');
    }
  }

  function showPlateFlash(capture) {
    const node = $('#finder-plate');
    if (!node || !capture || !capture.plate) return;
    $('#plate-read').textContent = fmt.plate(capture);
    $('#plate-conf').textContent = `${fmt.pct(capture.confidence)} CONFIDENCE`;
    node.hidden = false;
    $('#finder').classList.add('has-plate');
    clearTimeout(showPlateFlash.timer);
    showPlateFlash.timer = setTimeout(() => {
      node.hidden = true;
      if (!state.stream) $('#finder').classList.remove('has-plate');
    }, 3600);
  }

  async function shoot() {
    if (!state.stream) {
      toast('Camera is not available — upload an image instead', 'error');
      return;
    }
    const blob = await grabFrame();
    await scanBlob(blob, { source: 'stream' });
  }

  /* ------------------------------------------------------------- auto scan */
  const AUTO_INTERVAL = 3200;
  const DEDUPE_MS = 20000;

  function startAuto() {
    stopAuto();
    if (!state.auto || !state.stream) return;
    state.autoTimer = setInterval(autoTick, AUTO_INTERVAL);
  }

  function stopAuto() {
    if (state.autoTimer) clearInterval(state.autoTimer);
    state.autoTimer = null;
  }

  async function autoTick() {
    if (!state.stream || state.busy || document.hidden || $('#modal-root').children.length) return;
    const blob = await grabFrame();
    if (!blob) return;
    await scanBlob(blob, { source: 'stream', silent: true });
    // De-duplicate: a plate parked in frame should not create a row every tick.
    const now = Date.now();
    for (const [plate, seen] of state.recentPlates) {
      if (now - seen > DEDUPE_MS) state.recentPlates.delete(plate);
    }
  }

  function toggleAuto() {
    state.auto = !state.auto;
    const button = $('#auto-btn');
    button.classList.toggle('is-active', state.auto);
    button.setAttribute('aria-pressed', String(state.auto));
    $('#auto-sub').textContent = state.auto ? 'AUTO SCAN' : 'MANUAL';
    $('#finder').classList.toggle('has-plate', false);
    startAuto();
  }

  /* ------------------------------------------------------------------ data */
  async function loadHistory({ page = 1, append = false } = {}) {
    try {
      const data = await api(`/captures?page=${page}&page_size=12&sort=recent`);
      state.captures = append ? state.captures.concat(data.items) : data.items;
      state.page = data.page;
      state.pages = data.pages;
      state.total = data.total;
      renderStrip();
      renderDock();
      return data;
    } catch (error) {
      if (error.status === 401) showSignin();
      return null;
    }
  }

  /* ------------------------------------------------------------------- ui */
  function renderStrip() {
    const strip = $('#strip');
    if (!strip) return;
    const items = state.captures.slice(0, 6).reverse();
    if (!items.length) {
      strip.innerHTML = '<div class="strip-empty"><span class="micro">NO SCANS YET</span></div>';
      return;
    }
    strip.innerHTML = items.map((capture, index) => {
      const current = index === items.length - 1 ? ' is-current' : '';
      return `<button class="strip-item${current}" data-action="open-capture" data-id="${fmt.escape(capture.id)}">
        <img src="${fmt.escape(capture.thumbnail_url)}" alt="" loading="lazy" />
        <span class="strip-plate">${fmt.escape(fmt.plate(capture))}</span>
      </button>`;
    }).join('');
  }

  function renderDock() {
    const count = $('#scan-count');
    if (count) count.textContent = String(state.total ?? state.captures.length);
    const thumb = $('#last-thumb');
    const latest = state.captures[0];
    if (thumb && latest) thumb.src = latest.thumbnail_url;
  }

  function ensureModalRoot() {
    let root = $('#modal-root');
    if (!root) {
      root = document.createElement('div');
      root.id = 'modal-root';
      document.body.appendChild(root);
    }
    return root;
  }

  function closeModal() {
    const root = ensureModalRoot();
    root.innerHTML = '';
    document.body.style.overflow = '';
  }

  function openSheet({ title, meta = '', body }) {
    const root = ensureModalRoot();
    root.innerHTML = `
      <div class="overlay" data-action="overlay-close">
        <section class="sheet" role="dialog" aria-modal="true" aria-label="${fmt.escape(title)}">
          <header class="sheet-head">
            <span class="sheet-title">${fmt.escape(title)}</span>
            <span class="micro">${fmt.escape(meta)}</span>
            <button class="icon-btn" data-action="close-modal" aria-label="Close">${icon.close}</button>
          </header>
          <div class="sheet-body">${body}</div>
        </section>
      </div>`;
    document.body.style.overflow = 'hidden';
    return root;
  }

  function glyphRow(capture) {
    // Per-glyph confidences live on the best candidate; older captures predate
    // that field, in which case the row still shows the letters without scores.
    const candidate = (capture.candidates && capture.candidates[0]) || {};
    const glyphs = candidate.glyphs || [];
    const letters = (capture.plate || '').split('');
    if (!letters.length) return '';
    return `<div class="glyph-row">${letters.map((char, index) => {
      const item = glyphs[index];
      const confidence = item && typeof item.confidence === 'number' ? item.confidence : null;
      return `<span class="glyph"><b>${fmt.escape(char)}</b><span>${confidence === null ? '—' : fmt.pct(confidence)}</span></span>`;
    }).join('')}</div>`;
  }

  function openResult(capture) {
    const isNew = capture;
    const plate = fmt.plate(capture);
    const body = `
      <div class="result-frame">
        <img src="${fmt.escape(capture.image_url)}" alt="Captured frame" />
      </div>
      <div class="result-main">
        <span class="result-plate">${fmt.escape(plate)}</span>
        <span class="result-conf">${fmt.pct(capture.confidence)}</span>
      </div>
      <p class="result-note">${fmt.escape((capture.region_name || capture.region || 'Unknown jurisdiction'))} · ${fmt.escape((capture.plate_type || 'unknown'))} plate · read in ${Math.round(Number(capture.processing_ms) || 0)} ms</p>
      ${glyphRow(capture)}
      <dl class="kv">
        <div><dt>Captured</dt><dd>${fmt.escape(fmt.day(capture.created_at))} · ${fmt.escape(fmt.clock(capture.created_at))}</dd></div>
        <div><dt>Source</dt><dd>${fmt.escape((capture.source || 'stream').toUpperCase())}</dd></div>
        <div><dt>Camera</dt><dd>${fmt.escape(capture.camera_name || 'This device')}</dd></div>
        <div><dt>Engine</dt><dd>${fmt.escape(capture.engine_version || '—')}</dd></div>
      </dl>
      <div class="actions">
        <button class="btn btn-gold" data-action="download-capture" data-id="${fmt.escape(capture.id)}">${icon.download} PDF</button>
        <button class="btn" data-action="open-teach" data-id="${fmt.escape(capture.id)}">CORRECT</button>
      </div>
      ${isNew ? '<p class="result-note" style="margin-top:14px">Saved to your scan history.</p>' : ''}`;
    openSheet({ title: isNew ? 'Plate read' : 'Scan detail', meta: plate, body });
  }

  function openHistory() {
    const rows = state.captures.map((capture) => `
      <button class="history-item" data-action="open-capture" data-id="${fmt.escape(capture.id)}">
        <img class="history-thumb" src="${fmt.escape(capture.thumbnail_url)}" alt="" loading="lazy" />
        <span class="history-main">
          <span class="history-plate">${fmt.escape(fmt.plate(capture))}</span>
          <span class="history-sub">${fmt.escape(fmt.ago(capture.created_at))} · ${fmt.escape((capture.source || 'stream').toUpperCase())}</span>
        </span>
        <span class="history-score">${fmt.pct(capture.confidence)}</span>
      </button>`).join('');

    const body = `
      <div class="actions" style="margin:0 0 12px">
        <button class="btn btn-solid" data-action="export-pdf">${icon.download} EXPORT ALL · PDF</button>
      </div>
      <div class="history-list" id="history-list">
        ${rows || '<div class="empty">NOTHING SCANNED YET</div>'}
      </div>
      ${state.page < state.pages ? '<button class="btn btn-ghost" data-action="history-more" style="margin-top:14px">LOAD OLDER</button>' : ''}`;
    openSheet({ title: 'Scan history', meta: `${state.total} STORED`, body });
  }

  function openTeach(captureId) {
    const capture = state.captures.find((item) => item.id === captureId);
    if (!capture) return;
    const body = `
      <p class="result-note">Type the plate exactly as printed. The glyphs that were misread become training data for this camera.</p>
      <label class="field">
        <span class="micro muted">CORRECT PLATE</span>
        <input id="teach-input" value="${fmt.escape(capture.plate || '')}" autocomplete="off" spellcheck="false" />
      </label>
      <div class="actions">
        <button class="btn btn-gold" data-action="teach-save" data-id="${fmt.escape(capture.id)}">SAVE · TRAIN</button>
        <button class="btn btn-ghost" data-action="close-modal">CANCEL</button>
      </div>`;
    openSheet({ title: 'Correct reading', meta: fmt.plate(capture), body });
    const input = $('#teach-input');
    if (input) {
      input.focus();
      input.select();
    }
  }

  function openSettings() {
    const user = state.user || {};
    const body = `
      <dl class="kv">
        <div><dt>Signed in</dt><dd>${fmt.escape(user.email || 'demo')}</dd></div>
        <div><dt>Role</dt><dd>${fmt.escape((user.role || 'operator').toUpperCase())}</dd></div>
        <div><dt>Workspace</dt><dd>${fmt.escape(user.organization || 'Demo workspace')}</dd></div>
        <div><dt>Engine</dt><dd>${fmt.escape(state.config ? state.config.version : '—')}</dd></div>
      </dl>
      <p class="result-note" style="margin-top:16px">Every scan is stored on this server. Downloads are generated on request, so no copy of the image leaves the deployment.</p>
      <div class="actions">
        <button class="btn btn-ghost" data-action="logout">SIGN OUT</button>
        <button class="btn btn-solid" data-action="close-modal">DONE</button>
      </div>`;
    openSheet({ title: 'Settings', body });
  }

  /* ------------------------------------------------------------------ pdf */
  function downloadUrl(url, filename) {
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || '';
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    setTimeout(() => link.remove(), 0);
  }

  function downloadCapture(captureId) {
    const capture = state.captures.find((item) => item.id === captureId);
    const stamp = capture ? (capture.plate || 'unread') : 'scan';
    downloadUrl(`${API}/captures/${captureId}/report.pdf`, `platevision-${stamp}.pdf`);
    toast('Preparing PDF…');
  }

  function exportAll() {
    downloadUrl(`${API}/captures/export.pdf`, 'platevision-scans.pdf');
    toast('Exporting scan history…');
  }

  /* ----------------------------------------------------------------- auth */
  function showSignin(message) {
    const root = $('#signin-root');
    if (!root || root.dataset.open === '1') return;
    root.dataset.open = '1';
    root.innerHTML = `
      <div class="signin">
        <form id="signin-form">
          <span class="micro muted">PLATEVISION</span>
          <h1>Sign in</h1>
          <label><span class="micro muted">EMAIL</span>
            <input id="signin-email" type="email" autocomplete="username" value="${fmt.escape((state.config && state.config.demo_email) || '')}" required />
          </label>
          <label><span class="micro muted">PASSWORD</span>
            <input id="signin-password" type="password" autocomplete="current-password" required />
          </label>
          <button class="btn btn-solid" type="submit" style="margin-top:6px">ENTER · CONSOLE</button>
          ${message ? `<p class="result-note" style="text-align:center">${fmt.escape(message)}</p>` : ''}
        </form>
      </div>`;
    const form = $('#signin-form');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        const payload = await api('/auth/login', {
          method: 'POST',
          body: {
            email: $('#signin-email').value.trim(),
            password: $('#signin-password').value,
          },
        });
        state.user = payload.user || null;
        root.innerHTML = '';
        delete root.dataset.open;
        await bootAfterAuth();
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }

  async function bootAfterAuth() {
    if (!state.user) {
      try {
        state.user = await api('/auth/me');
      } catch (_) {
        state.user = null;
      }
    }
    await loadHistory();
    renderCountry();
    startCamera();
  }

  async function boot() {
    try {
      state.config = await api('/config');
    } catch (_) {
      state.config = null;
    }

    // Straight into the camera: the demo workspace is the default identity.
    try {
      state.user = await api('/auth/me');
    } catch (error) {
      if (state.config && state.config.demo_login) {
        try {
          const payload = await api('/auth/demo', { method: 'POST' });
          state.user = payload.user || null;
        } catch (_) {
          state.user = null;
        }
      }
    }

    if (!state.user) {
      showSignin(state.config && state.config.demo_login ? '' : 'Sign in with your operator account.');
      renderCountry();
      await loadHistoryOffline();
      startCamera();
      return;
    }
    await bootAfterAuth();
  }

  async function loadHistoryOffline() {
    renderStrip();
    renderDock();
  }

  async function logout() {
    try {
      await api('/auth/logout', { method: 'POST' });
    } catch (_) {
      /* the cookie may already be gone */
    }
    state.user = null;
    state.captures = [];
    state.total = 0;
    renderStrip();
    renderDock();
    closeModal();
    showSignin('Signed out.');
  }

  /* -------------------------------------------------------------- country */
  function renderCountry() {
    const current = COUNTRIES.find((item) => item.code === state.country) || COUNTRIES[0];
    $('#country-btn').textContent = current.code === 'GENERIC' ? 'ANY' : current.code;
    $('#tray-country').textContent = current.label;
    $$('#country-scale .scale-item').forEach((button) => {
      button.classList.toggle('is-current', button.dataset.country === state.country);
    });
  }

  function setCountry(code) {
    if (!code) return;
    state.country = code;
    renderCountry();
  }

  function cycleCountry(direction) {
    const index = COUNTRIES.findIndex((item) => item.code === state.country);
    const next = (index + direction + COUNTRIES.length) % COUNTRIES.length;
    setCountry(COUNTRIES[next].code);
  }

  /* --------------------------------------------------------------- events */
  document.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action], [data-country]');
    if (!target) return;

    if (target.dataset.country) {
      setCountry(target.dataset.country);
      return;
    }

    if (target === event.target && target.classList.contains('overlay')) {
      closeModal();
      return;
    }

    const action = target.dataset.action;
    switch (action) {
      case 'overlay-close':
        if (event.target === target) closeModal();
        break;

      case 'close-modal':
        closeModal();
        break;

      case 'open-capture': {
        const capture = state.captures.find((item) => item.id === target.dataset.id);
        if (capture) openResult(capture);
        break;
      }

      case 'open-library':
        openHistory();
        break;

      case 'history-more': {
        await loadHistory({ page: state.page + 1, append: true });
        openHistory();
        break;
      }

      case 'open-menu':
      case 'open-settings':
        openSettings();
        break;

      case 'toggle-auto':
        toggleAuto();
        break;

      case 'toggle-torch':
        toggleTorch();
        break;

      case 'cycle-country':
        cycleCountry(1);
        break;

      case 'country-prev':
        cycleCountry(-1);
        break;

      case 'country-next':
        cycleCountry(1);
        break;

      case 'choose-mode':
        toggleAuto();
        break;

      case 'pick-file':
        $('#file-picker').click();
        break;

      case 'download-capture':
        downloadCapture(target.dataset.id);
        break;

      case 'export-pdf':
        exportAll();
        break;

      case 'open-teach':
        openTeach(target.dataset.id);
        break;

      case 'teach-save': {
        const plate = ($('#teach-input').value || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
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
          const added = result.learned ? result.learned.added : 0;
          toast(added > 0 ? `Corrected · ${added} glyph${added === 1 ? '' : 's'} learned` : 'Corrected');
          closeModal();
          renderStrip();
          await loadHistory();
        } catch (error) {
          toast(error.message, 'error');
        }
        break;
      }

      case 'logout':
        await logout();
        break;

      case 'retry-camera':
        startCamera();
        break;

      default:
        break;
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeModal();
    if (event.code === 'Space' && !$('#modal-root').children.length && event.target === document.body) {
      event.preventDefault();
      shoot();
    }
  });

  $('#shutter').addEventListener('click', shoot);

  $('#file-picker').addEventListener('change', async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (file) await scanBlob(file, { source: 'upload' });
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      state.lastShotAt = Date.now();
    }
  });

  window.addEventListener('pagehide', stopCamera);

  /* ----------------------------------------------------------------- boot */
  renderCountry();
  renderStrip();
  boot();
})();
