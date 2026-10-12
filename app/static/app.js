// Burnarr - frontend vanilla JS

(() => {
  'use strict';

  // ---------- Stato ----------
  const state = {
    drive: null,
    job: null,
    // selected.kind === 'iso'     -> { kind, path, name, size, source }
    // selected.kind === 'staging' -> { kind, staging_id }   (dettagli in state.staging)
    selected: null,
    files: [],
    sse: null,
    rawLog: '',
    activeTab: 'upload',
    staging: null,   // { id, files: [{name,size}], total_size, uploading: Map(name -> {progress}) }
  };

  // ---------- Utility DOM ----------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ---------- Utility format ----------
  const fmtSize = (b) => {
    if (b == null) return '';
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(0)} KB`;
    if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(0)} MB`;
    return `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`;
  };
  const fmtDuration = (s) => {
    if (s == null) return '';
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${m}m ${sec}s`;
  };
  const fmtEta = (s) => {
    if (s == null || s <= 0) return '';
    if (s < 60) return `~${s}s rimanenti`;
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `~${m}m ${sec}s rimanenti`;
  };
  const fmtDate = (iso) => {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      return d.toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' });
    } catch { return iso; }
  };

  // ---------- Toast ----------
  let toastTimer = null;
  function toast(msg, kind = '') {
    const el = $('#toast');
    el.textContent = msg;
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
  }

  // ---------- API ----------
  async function api(path, opts = {}) {
    const resp = await fetch(path, opts);
    let body = null;
    try { body = await resp.json(); } catch { /* niente */ }
    if (!resp.ok) {
      const msg = (body && body.error) || `Errore ${resp.status}`;
      throw new Error(msg);
    }
    return body;
  }

  // ---------- Drive status ----------
  async function refreshStatus() {
    try {
      const data = await api('/api/status');
      state.drive = data.drive;
      // Solo aggiorna il job se non c'è SSE attivo (lo SSE è la fonte autorevole)
      if (!state.job) state.job = data.job;
      renderDriveState();
      renderBurnButton();
      maybeRefreshBrowse();
      // Se c'è un job attivo e non siamo connessi all'SSE, connettiti
      if (state.job && isJobActive(state.job) && !state.sse) {
        connectSSE();
      }
    } catch (e) {
      $('#state-icon').className = 'state-icon is-error';
      $('#state-icon').textContent = '⚠';
      $('#state-title').textContent = 'Impossibile contattare il server';
      $('#state-sub').textContent = e.message;
    }
  }

  function renderDriveState() {
    const icon = $('#state-icon');
    const title = $('#state-title');
    const sub = $('#state-sub');

    if (state.job && isJobActive(state.job)) {
      icon.className = 'state-icon is-burning';
      icon.textContent = '⚙';
      title.textContent = ({
        'verifying': 'Verifica del disco in corso',
        'building': 'Creazione ISO in corso',
        'preparing': 'Preparazione...',
      })[state.job.status] || 'Masterizzazione in corso';
      const label = state.job.source_label
        || (state.job.source_path ? state.job.source_path.split('/').pop() : '');
      sub.textContent = label;
      return;
    }

    const d = state.drive || {};
    switch (d.state) {
      case 'empty':
        icon.className = 'state-icon is-empty';
        icon.textContent = '◌';
        title.textContent = 'Drive vuoto';
        sub.textContent = 'Inserisci un CD o DVD vergine.';
        break;
      case 'blank':
        icon.className = 'state-icon is-blank';
        icon.textContent = '●';
        title.textContent = d.message || 'Disco vuoto inserito';
        sub.textContent = d.capacity_bytes ? `Capacità: ${fmtSize(d.capacity_bytes)}` : '';
        break;
      case 'has_data':
        icon.className = 'state-icon is-data';
        icon.textContent = '◉';
        title.textContent = d.message || 'Disco con dati inserito';
        sub.textContent = 'Per masterizzare serve un disco vergine.';
        break;
      case 'error':
        icon.className = 'state-icon is-error';
        icon.textContent = '⚠';
        title.textContent = 'Errore';
        sub.textContent = d.message || '';
        break;
      default:
        icon.className = 'state-icon is-empty';
        icon.textContent = '?';
        title.textContent = d.message || 'Stato sconosciuto';
        sub.textContent = '';
    }
  }

  // ---------- Tabs ----------
  $$('.tab').forEach(t => {
    t.addEventListener('click', () => {
      $$('.tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      const tab = t.dataset.tab;
      state.activeTab = tab;
      $('#panel-upload').hidden = (tab !== 'upload');
      $('#panel-library').hidden = (tab !== 'library');
      $('#panel-files').hidden = (tab !== 'files');
      if (tab === 'library') refreshFiles();
      if (tab === 'files') ensureStagingSession();
      // Quando si cambia tab, svuota la "selezione" se il tipo non combacia
      if (state.selected) {
        if (tab === 'files' && state.selected.kind !== 'staging') clearSelectedSilent();
        else if (tab !== 'files' && state.selected.kind === 'staging') clearSelectedSilent();
      }
      renderBurnButton();
    });
  });

  function clearSelectedSilent() {
    state.selected = null;
    $('#selected').hidden = true;
    renderFileList();
  }

  // ---------- File library ----------
  async function refreshFiles() {
    try {
      const data = await api('/api/files');
      state.files = data.files || [];
      renderFileList();
    } catch (e) {
      toast('Impossibile caricare la lista file: ' + e.message, 'error');
    }
  }

  function renderFileList() {
    const list = $('#file-list');
    list.innerHTML = '';
    if (state.files.length === 0) {
      list.innerHTML = '<div class="empty">Nessun file ISO trovato sul server.</div>';
      return;
    }
    state.files.forEach(f => {
      const div = document.createElement('div');
      div.className = 'file-item' + (state.selected && state.selected.path === f.path ? ' selected' : '');
      const badge = f.source === 'uploads' ? '<span class="badge upload">caricato</span>' : '<span class="badge">libreria</span>';
      div.innerHTML = `
        <span class="name">${escapeHtml(f.name)}</span>
        <span class="meta">${fmtSize(f.size)}</span>
        ${badge}
      `;
      div.addEventListener('click', () => selectSource(f));
      list.appendChild(div);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  // ---------- Selection ----------
  function selectSource(f) {
    state.selected = { kind: 'iso', ...f };
    const sel = $('#selected');
    sel.hidden = false;
    $('#selected-name').textContent = `${f.name} (${fmtSize(f.size)})`;
    renderFileList();
    renderBurnButton();
  }

  $('#clear-selection').addEventListener('click', () => {
    state.selected = null;
    $('#selected').hidden = true;
    renderFileList();
    renderBurnButton();
  });

  // ---------- Upload ----------
  const dropzone = $('#dropzone');
  const fileInput = $('#file-input');

  ['dragenter', 'dragover'].forEach(ev =>
    dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add('drag'); })
  );
  ['dragleave', 'drop'].forEach(ev =>
    dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove('drag'); })
  );
  dropzone.addEventListener('drop', e => {
    const f = e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) uploadFile(fileInput.files[0]);
  });

  function uploadFile(file) {
    const ext = file.name.toLowerCase().split('.').pop();
    if (ext !== 'iso' && ext !== 'img') {
      toast('Estensione non supportata. Usa .iso o .img', 'error');
      return;
    }
    const progEl = $('#upload-progress');
    const fillEl = $('#upload-bar-fill');
    const infoEl = $('#upload-info');
    progEl.hidden = false;
    fillEl.style.width = '0%';
    infoEl.textContent = `Caricamento di ${file.name}...`;

    const fd = new FormData();
    fd.append('file', file);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.upload.addEventListener('progress', e => {
      if (e.lengthComputable) {
        const pct = (e.loaded * 100 / e.total);
        fillEl.style.width = pct.toFixed(1) + '%';
        infoEl.textContent = `${pct.toFixed(0)}% — ${fmtSize(e.loaded)} / ${fmtSize(e.total)}`;
      }
    });
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const data = JSON.parse(xhr.responseText);
        infoEl.textContent = `Caricato: ${data.name}`;
        selectSource({ path: data.path, name: data.name, size: data.size, source: 'uploads' });
        refreshFiles();
        setTimeout(() => { progEl.hidden = true; }, 2000);
      } else {
        let msg = `Errore ${xhr.status}`;
        try { msg = JSON.parse(xhr.responseText).error || msg; } catch {}
        infoEl.textContent = msg;
        toast('Upload fallito: ' + msg, 'error');
      }
    };
    xhr.onerror = () => {
      infoEl.textContent = 'Errore di rete durante l\'upload.';
      toast('Errore di rete', 'error');
    };
    xhr.send(fd);
  }

  // ---------- Burn button ----------
  function hasValidSource() {
    if (!state.selected) {
      // Sul tab files, considera valido anche solo avere file in staging
      // (l'utente potrebbe non aver ancora visto la card "selezionato").
      if (state.activeTab === 'files') {
        return state.staging && state.staging.files.length > 0;
      }
      return false;
    }
    if (state.selected.kind === 'iso') return !!state.selected.path;
    if (state.selected.kind === 'staging') {
      return state.staging && state.staging.files.length > 0;
    }
    return false;
  }

  function renderBurnButton() {
    const btn = $('#burn-btn');
    const hint = $('#burn-hint');
    const busy = state.job && isJobActive(state.job);
    const driveReady = state.drive && state.drive.state === 'blank';
    const valid = hasValidSource();
    const uploading = state.staging
      && state.staging.uploading
      && state.staging.uploading.size > 0;

    btn.disabled = busy || !driveReady || !valid || uploading;

    if (busy) {
      hint.textContent = 'Masterizzazione in corso...';
    } else if (uploading) {
      hint.textContent = 'Caricamento file in corso, attendi...';
    } else if (!valid) {
      hint.textContent = state.activeTab === 'files'
        ? 'Trascina dei file da masterizzare.'
        : 'Seleziona o carica un file ISO.';
    } else if (!driveReady) {
      hint.textContent = state.drive && state.drive.state === 'empty'
        ? 'Inserisci un CD o DVD vergine nel drive.'
        : (state.drive ? state.drive.message : 'Drive non pronto.');
    } else {
      hint.textContent = state.activeTab === 'files'
        ? 'Tutto pronto. Premi Masterizza per creare e scrivere il disco.'
        : 'Tutto pronto. Premi Masterizza per iniziare.';
    }
  }

  $('#burn-btn').addEventListener('click', async () => {
    const isStaging = state.activeTab === 'files'
      || (state.selected && state.selected.kind === 'staging');
    if (!hasValidSource()) return;

    const common = {
      speed: $('#opt-speed').value,
      verify: $('#opt-verify').checked,
      eject: $('#opt-eject').checked,
    };

    let payload, url;
    if (isStaging) {
      url = '/api/burn-from-files';
      payload = {
        staging_id: state.staging.id,
        label: ($('#iso-label').value || 'BURNARR').trim(),
        ...common,
      };
    } else {
      url = '/api/burn';
      payload = { source_path: state.selected.path, ...common };
    }

    try {
      const data = await api(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      state.job = data.job;
      state.rawLog = '';
      // Dopo l'avvio, la sessione staging "sparirà" dal server (cleanup in finalize)
      // ma teniamo l'oggetto client finché il job non termina.
      renderBurnButton();
      renderDriveState();
      showProgressCard();
      connectSSE();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  $('#cancel-btn').addEventListener('click', async () => {
    if (!confirm('Annullare la masterizzazione in corso?')) return;
    try {
      await api('/api/burn/cancel', { method: 'POST' });
      toast('Annullamento richiesto.');
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // ---------- Staging (Crea disco dai file) ----------

  async function ensureStagingSession() {
    if (state.staging && state.staging.id) return;
    try {
      const data = await api('/api/staging/new', { method: 'POST' });
      state.staging = {
        id: data.staging_id,
        files: [],
        total_size: 0,
        uploading: new Map(),
      };
      // Pre-popola l'etichetta con default basato sulla data
      const labelInput = $('#iso-label');
      if (labelInput && !labelInput.value) {
        const d = new Date();
        const yyyy = d.getFullYear();
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        labelInput.value = `BURNARR_${yyyy}${mm}${dd}`;
      }
      renderStaging();
    } catch (e) {
      toast('Errore creando la sessione di staging: ' + e.message, 'error');
    }
  }

  function renderStaging() {
    const wrap = $('#staging-list');
    const itemsBox = $('#staging-items');
    const labelRow = $('#label-row');
    const count = $('#staging-count');
    const sizeEl = $('#staging-size');
    const uploadsBox = $('#staging-uploads');

    if (!state.staging) {
      wrap.hidden = true;
      labelRow.hidden = true;
      return;
    }

    const nFiles = state.staging.files.length;
    const nUploading = state.staging.uploading.size;

    if (nFiles === 0 && nUploading === 0) {
      wrap.hidden = true;
      labelRow.hidden = true;
      state.selected = null;
      $('#selected').hidden = true;
      return;
    }

    wrap.hidden = false;
    labelRow.hidden = false;

    count.textContent = nFiles === 1 ? '1 file' : `${nFiles} file`;
    sizeEl.textContent = fmtSize(state.staging.total_size);

    itemsBox.innerHTML = '';
    state.staging.files.forEach(f => {
      const div = document.createElement('div');
      div.className = 'staging-item';
      div.innerHTML = `
        <span class="name">${escapeHtml(f.name)}</span>
        <span class="size">${fmtSize(f.size)}</span>
        <button class="remove" title="Rimuovi" aria-label="Rimuovi">×</button>
      `;
      div.querySelector('.remove').addEventListener('click', () => removeStagingFile(f.name));
      itemsBox.appendChild(div);
    });

    uploadsBox.innerHTML = '';
    state.staging.uploading.forEach((info, key) => {
      const div = document.createElement('div');
      div.className = 'staging-upload-row';
      const pct = info.progress != null ? info.progress.toFixed(0) : '0';
      div.innerHTML = `
        <span class="name">${escapeHtml(key)} — ${pct}%</span>
        <div class="bar"><div class="bar-fill" style="width: ${pct}%"></div></div>
      `;
      uploadsBox.appendChild(div);
    });

    // Aggiorna la selezione "virtuale" così il bottone Masterizza si abilita.
    if (nFiles > 0) {
      state.selected = { kind: 'staging', staging_id: state.staging.id };
      const sel = $('#selected');
      sel.hidden = false;
      const label = ($('#iso-label').value || 'BURNARR').trim();
      $('#selected-name').textContent = `${label} · ${nFiles} file · ${fmtSize(state.staging.total_size)}`;
    }
  }

  async function refreshStaging() {
    if (!state.staging) return;
    try {
      const data = await api(`/api/staging/${state.staging.id}`);
      state.staging.files = data.files || [];
      state.staging.total_size = data.total_size || 0;
      renderStaging();
      renderBurnButton();
    } catch (e) {
      // Se la sessione non esiste più (es. server riavviato), ricrea
      if (/non trovata/i.test(e.message)) {
        state.staging = null;
        ensureStagingSession();
      }
    }
  }

  async function removeStagingFile(name) {
    if (!state.staging) return;
    try {
      const data = await api(
        `/api/staging/${state.staging.id}/file/${encodeURI(name)}`,
        { method: 'DELETE' }
      );
      state.staging.files = data.files || [];
      state.staging.total_size = data.total_size || 0;
      renderStaging();
      renderBurnButton();
    } catch (e) {
      toast('Rimozione fallita: ' + e.message, 'error');
    }
  }

  $('#staging-clear').addEventListener('click', async () => {
    if (!state.staging) return;
    if (!confirm('Svuotare la lista di file?')) return;
    try {
      await api(`/api/staging/${state.staging.id}`, { method: 'DELETE' });
    } catch {}
    state.staging = null;
    state.selected = null;
    $('#selected').hidden = true;
    renderStaging();
    renderBurnButton();
    ensureStagingSession();
  });

  $('#iso-label').addEventListener('input', () => {
    if (state.staging && state.staging.files.length > 0) {
      renderStaging();  // aggiorna il testo "selezionato"
    }
  });

  // ----- File drop / picker -----

  const dzFiles = $('#dropzone-files');
  ['dragenter', 'dragover'].forEach(ev =>
    dzFiles.addEventListener(ev, e => { e.preventDefault(); dzFiles.classList.add('drag'); })
  );
  ['dragleave', 'drop'].forEach(ev =>
    dzFiles.addEventListener(ev, e => { e.preventDefault(); dzFiles.classList.remove('drag'); })
  );
  dzFiles.addEventListener('drop', async (e) => {
    await ensureStagingSession();
    const items = e.dataTransfer.items;
    const pickedFiles = [];
    if (items && items.length && items[0].webkitGetAsEntry) {
      const entries = [];
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry();
        if (entry) entries.push(entry);
      }
      for (const entry of entries) {
        await walkEntry(entry, '', pickedFiles);
      }
    } else {
      for (const f of e.dataTransfer.files) {
        pickedFiles.push({ file: f, relpath: f.name });
      }
    }
    enqueueUploads(pickedFiles);
  });

  $('#files-input').addEventListener('change', async (e) => {
    await ensureStagingSession();
    const list = [];
    for (const f of e.target.files) list.push({ file: f, relpath: f.name });
    enqueueUploads(list);
    e.target.value = '';
  });

  $('#folder-input').addEventListener('change', async (e) => {
    await ensureStagingSession();
    const list = [];
    for (const f of e.target.files) {
      list.push({ file: f, relpath: f.webkitRelativePath || f.name });
    }
    enqueueUploads(list);
    e.target.value = '';
  });

  function walkEntry(entry, prefix, out) {
    return new Promise((resolve) => {
      if (entry.isFile) {
        entry.file(file => {
          out.push({ file, relpath: prefix + entry.name });
          resolve();
        }, () => resolve());
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        const all = [];
        const readBatch = () => {
          reader.readEntries(async (entries) => {
            if (entries.length === 0) {
              for (const child of all) await walkEntry(child, prefix + entry.name + '/', out);
              resolve();
            } else {
              all.push(...entries);
              readBatch();
            }
          }, () => resolve());
        };
        readBatch();
      } else {
        resolve();
      }
    });
  }

  // Upload seriali per non saturare la banda LAN — la maggior parte degli
  // homelab ha 1 Gbps e l'overhead di N upload paralleli non porta vantaggi.
  let _uploadQueue = Promise.resolve();
  function enqueueUploads(list) {
    list.forEach(item => {
      _uploadQueue = _uploadQueue.then(() => uploadStagingFile(item.file, item.relpath));
    });
  }

  function uploadStagingFile(file, relpath) {
    return new Promise((resolve) => {
      if (!state.staging) return resolve();
      const key = relpath;
      state.staging.uploading.set(key, { progress: 0 });
      renderStaging();
      renderBurnButton();

      const fd = new FormData();
      fd.append('file', file, file.name);
      fd.append('relpath', relpath);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/staging/${state.staging.id}/file`);
      xhr.upload.addEventListener('progress', (e) => {
        if (!e.lengthComputable) return;
        const info = state.staging && state.staging.uploading.get(key);
        if (info) {
          info.progress = e.loaded * 100 / e.total;
          renderStaging();
        }
      });
      xhr.onload = () => {
        state.staging && state.staging.uploading.delete(key);
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            const data = JSON.parse(xhr.responseText);
            if (state.staging) {
              state.staging.files = data.files || state.staging.files;
              state.staging.total_size = data.total_size || state.staging.total_size;
            }
          } catch {}
        } else {
          let msg = `Errore ${xhr.status}`;
          try { msg = JSON.parse(xhr.responseText).error || msg; } catch {}
          toast(`Upload di "${relpath}" fallito: ${msg}`, 'error');
        }
        renderStaging();
        renderBurnButton();
        resolve();
      };
      xhr.onerror = () => {
        state.staging && state.staging.uploading.delete(key);
        toast(`Errore di rete su "${relpath}"`, 'error');
        renderStaging();
        renderBurnButton();
        resolve();
      };
      xhr.send(fd);
    });
  }

  // ---------- SSE progress ----------
  function isJobActive(j) {
    return j && ['queued', 'preparing', 'building', 'burning', 'verifying'].includes(j.status);
  }

  function connectSSE() {
    if (state.sse) return;
    const es = new EventSource('/api/burn/progress');
    state.sse = es;
    es.onmessage = (e) => {
      try {
        const job = JSON.parse(e.data);
        state.job = job;
        renderProgress(job);
        renderBurnButton();
        renderDriveState();
        if (!isJobActive(job)) {
          handleJobEnd(job);
          es.close();
          state.sse = null;
        }
      } catch {}
    };
    es.onerror = () => {
      es.close();
      state.sse = null;
      // Riproveremo al prossimo poll se il job è ancora attivo
    };
  }

  function showProgressCard() {
    $('#progress-card').hidden = false;
  }

  function renderProgress(job) {
    showProgressCard();
    const pct = job.progress != null ? job.progress : 0;
    $('#progress-fill').style.width = pct.toFixed(1) + '%';
    $('#progress-percent').textContent = pct.toFixed(1) + '%';
    $('#progress-speed').textContent = job.speed_current ? `Velocità: ${job.speed_current}` : '';
    $('#progress-eta').textContent = fmtEta(job.eta_seconds);
    $('#progress-title').textContent = ({
      'verifying': 'Verifica del disco',
      'preparing': 'Preparazione...',
      'building': 'Creazione ISO dai file...',
    })[job.status] || 'Masterizzazione in corso';
    if (job.message) {
      const log = $('#raw-log-content');
      const line = `[${new Date().toLocaleTimeString('it-IT')}] ${job.message}\n`;
      if (!state.rawLog.endsWith(line)) {
        state.rawLog += line;
        log.textContent = state.rawLog;
        log.scrollTop = log.scrollHeight;
      }
    }
  }

  function handleJobEnd(job) {
    if (job.status === 'done') {
      toast('Masterizzazione completata!', 'success');
      $('#progress-title').textContent = 'Completata ✓';
    } else if (job.status === 'cancelled') {
      toast('Masterizzazione annullata.');
      $('#progress-title').textContent = 'Annullata';
    } else {
      toast(job.error || 'Masterizzazione fallita.', 'error');
      $('#progress-title').textContent = 'Errore';
    }
    refreshHistory();
    // Nascondi la card progresso dopo 8s
    setTimeout(() => {
      $('#progress-card').hidden = true;
      state.job = null;
      renderBurnButton();
      renderDriveState();
    }, 8000);
  }

  // ---------- Sfoglia disco ----------

  const browseState = {
    loaded: false,
    loadedFor: null,  // signature dello stato disco per evitare reload inutili
    files: [],
    volume: null,
    filter: '',
  };

  function browseSignature(d) {
    if (!d) return null;
    return `${d.state}|${d.media_type || ''}`;
  }

  async function maybeRefreshBrowse() {
    const browseMode = $('#browse-mode');
    const burnMode = $('#burn-mode');
    const d = state.drive;
    const busy = state.job && isJobActive(state.job);
    const hasData = d && d.state === 'has_data' && !busy;

    // Toggle delle due modalità: o burn o browse, mai entrambi.
    browseMode.hidden = !hasData;
    burnMode.hidden = hasData;

    if (!hasData) {
      browseState.loaded = false;
      browseState.loadedFor = null;
      return;
    }
    const sig = browseSignature(d);
    if (browseState.loaded && browseState.loadedFor === sig) return;
    await loadBrowse();
    browseState.loadedFor = sig;
  }

  async function loadBrowse() {
    const list = $('#browse-list');
    list.innerHTML = '<div class="empty">Lettura del disco in corso…</div>';
    try {
      const data = await api('/api/disc/browse');
      browseState.files = data.files || [];
      browseState.volume = data.volume || null;
      browseState.loaded = true;
      renderBrowseVolume(data);
      renderBrowse();
    } catch (e) {
      list.innerHTML = `<div class="empty">Impossibile leggere il disco: ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderBrowseVolume(data) {
    const v = data.volume || {};
    const total = data.total_size || 0;
    const meta = $('#browse-meta');
    const volEl = $('#browse-volume');
    const id = v.volume_id || '(senza etichetta)';
    volEl.innerHTML = `Etichetta: <span class="volume-id">${escapeHtml(id)}</span>`;
    meta.textContent = `${data.total_files} file · ${fmtSize(total)}`;
  }

  function renderBrowse() {
    const list = $('#browse-list');
    const q = browseState.filter.trim().toLowerCase();
    const files = q
      ? browseState.files.filter(f => f.path.toLowerCase().includes(q))
      : browseState.files;

    if (files.length === 0) {
      list.innerHTML = q
        ? '<div class="empty">Nessun file corrisponde alla ricerca.</div>'
        : '<div class="empty">Disco vuoto o non leggibile.</div>';
      return;
    }

    list.innerHTML = '';
    // Limita a 500 elementi visibili per non saturare il DOM con dischi enormi
    const cap = 500;
    files.slice(0, cap).forEach(f => {
      const div = document.createElement('div');
      div.className = 'browse-item';
      const dir = f.parent && f.parent !== '/' ? `<span class="dir-prefix">${escapeHtml(f.parent)}/</span>` : '';
      const href = `/api/disc/file?path=${encodeURIComponent(f.path)}`;
      div.innerHTML = `
        <span class="name">${dir}${escapeHtml(f.name)}</span>
        <span class="size">${fmtSize(f.size)}</span>
        <a class="download" href="${href}" title="Scarica" download="${escapeHtml(f.name)}">⬇</a>
      `;
      list.appendChild(div);
    });
    if (files.length > cap) {
      const more = document.createElement('div');
      more.className = 'empty';
      more.textContent = `… altri ${files.length - cap} file (usa la ricerca per filtrare).`;
      list.appendChild(more);
    }
  }

  $('#browse-refresh').addEventListener('click', () => {
    browseState.loaded = false;
    browseState.loadedFor = null;
    maybeRefreshBrowse();
  });
  $('#browse-filter').addEventListener('input', (e) => {
    browseState.filter = e.target.value;
    renderBrowse();
  });
  $('#browse-eject').addEventListener('click', async () => {
    const btn = $('#browse-eject');
    btn.disabled = true;
    btn.textContent = 'Espulsione...';
    try {
      await api('/api/disc/eject', { method: 'POST' });
      toast('Disco espulso.', 'success');
      // Forza un refresh immediato dello stato per far apparire la modalità burn
      browseState.loaded = false;
      browseState.loadedFor = null;
      await refreshStatus();
    } catch (e) {
      toast('Espulsione fallita: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Espelli disco';
    }
  });

  // ---------- History ----------
  async function refreshHistory() {
    try {
      const data = await api('/api/history');
      renderHistory(data.burns || []);
    } catch (e) {
      // silenzioso
    }
  }

  function renderHistory(burns) {
    const list = $('#history-list');
    list.innerHTML = '';
    if (burns.length === 0) {
      list.innerHTML = '<div class="empty">Nessuna masterizzazione registrata.</div>';
      return;
    }
    burns.forEach(b => {
      const div = document.createElement('div');
      div.className = 'history-item';
      let iconChar = '✓', iconClass = 'ok';
      if (b.status === 'error') { iconChar = '✗'; iconClass = 'ko'; }
      else if (b.status === 'cancelled') { iconChar = '⊘'; iconClass = 'cancel'; }
      else if (b.status !== 'done') { iconChar = '⋯'; iconClass = 'cancel'; }
      const fileName = (b.source_file || '').split('/').pop();
      const meta = [
        fmtDate(b.started_at),
        b.duration_sec ? `durata ${fmtDuration(b.duration_sec)}` : null,
        b.media_type,
      ].filter(Boolean).join(' · ');
      div.innerHTML = `
        <span class="icon ${iconClass}">${iconChar}</span>
        <div class="info">
          <div class="file">${escapeHtml(fileName)}</div>
          <div class="meta">${escapeHtml(meta)}${b.error ? ' — ' + escapeHtml(b.error) : ''}</div>
        </div>
        <a class="link" href="/api/history/${b.id}/log" target="_blank">log</a>
      `;
      list.appendChild(div);
    });
  }

  $('#refresh-history').addEventListener('click', refreshHistory);

  // ---------- Theme toggle ----------
  const html = document.documentElement;
  const savedTheme = localStorage.getItem('burnarr-theme');
  if (savedTheme) html.dataset.theme = savedTheme;
  $('#theme-toggle').addEventListener('click', () => {
    const cur = html.dataset.theme === 'dark' ? 'dark' : 'light';
    const next = cur === 'dark' ? 'light' : 'dark';
    html.dataset.theme = next;
    localStorage.setItem('burnarr-theme', next);
  });

  // ---------- Init ----------
  refreshStatus();
  refreshFiles();
  refreshHistory();
  setInterval(refreshStatus, 4000);

})();
