const state = {
  sources: [], job: null, hits: [], selected: null, cloudModels: [], settings: null,
  outputToken: null, searchRequest: 0, hitRequest: 0, previewRequest: 0, jobEvents: null,
  importDocuments: [], documentMetadata: {}, documents: [], collections: [],
  importPreview: null, sourceFolders: [], selectedDocumentId: null, selectedPage: 0, completedJob: null,
  archiveFilter: 'all', archiveGrid: true, migration: null, totalDocuments: 0,
  archiveSort: 'recent',
  savedSearches: (() => { try { const value = JSON.parse(localStorage.getItem('schriftlotse-saved-searches') || '[]'); return Array.isArray(value) ? value : []; } catch (_error) { return []; } })()
};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
}[char]));

const busyButtons = new WeakMap();
function setButtonBusy(button, busy, label = 'Wird geladen …') {
  if (!button) return;
  if (busy) {
    if (!busyButtons.has(button)) {
      busyButtons.set(button, { html: button.innerHTML, disabled: button.disabled });
    }
    button.disabled = true;
    button.classList.add('is-loading');
    button.setAttribute('aria-busy', 'true');
    button.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${esc(label)}</span>`;
    return;
  }
  const previous = busyButtons.get(button);
  if (!previous) return;
  button.innerHTML = previous.html;
  button.disabled = previous.disabled;
  button.classList.remove('is-loading');
  button.removeAttribute('aria-busy');
  busyButtons.delete(button);
}

async function withButtonBusy(button, label, operation) {
  setButtonBusy(button, true, label);
  try {
    return await operation();
  } finally {
    setButtonBusy(button, false);
  }
}

function loadingMarkup(message, compact = false) {
  return `<div class="loading-state${compact ? ' compact' : ''}"><span class="spinner" aria-hidden="true"></span><span>${esc(message)}</span></div>`;
}

function setInlineStatus(element, message, { busy = false, error = false } = {}) {
  if (!element) return;
  element.hidden = !message;
  element.classList.toggle('is-error', error);
  element.setAttribute('aria-busy', String(busy));
  element.innerHTML = message
    ? `${busy ? '<span class="spinner" aria-hidden="true"></span>' : ''}<span>${esc(message)}</span>`
    : '';
}

async function responseError(response, fallback) {
  try {
    const data = await response.clone().json();
    return data.detail || data.message || fallback;
  } catch (_error) {
    try {
      return (await response.text()).trim() || fallback;
    } catch (_ignored) {
      return fallback;
    }
  }
}

function elapsedLabel(started) {
  const seconds = (performance.now() - started) / 1000;
  return seconds < 1 ? `${Math.max(0.1, seconds).toFixed(1).replace('.', ',')} s` : `${seconds.toFixed(1).replace('.', ',')} s`;
}

let toastTimer = null;
function notify(message, error = false) {
  const toast = $('toast');
  toast.textContent = message;
  toast.classList.toggle('error', error);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4200);
}

function askConfirmation(title, copy) {
  const dialog = $('confirm-dialog');
  $('confirm-title').textContent = title;
  $('confirm-copy').textContent = copy;
  dialog.showModal();
  return new Promise(resolve => {
    dialog.onclose = () => resolve(dialog.returnValue === 'confirm');
  });
}

document.querySelectorAll('.tabs button').forEach(button => {
  button.onclick = () => openTab(button.dataset.tab);
});

function openTab(name) {
  document.querySelectorAll('.tabs button,.tab').forEach(node => node.classList.remove('active'));
  document.querySelector(`.tabs button[data-tab="${name}"]`).classList.add('active');
  $(name).classList.add('active');
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (name === 'models') loadModels();
  if (name === 'search') loadDocuments();
  if (name === 'settings') { loadSettings(); loadSystemStatus(); loadKeyStatus(); }
}

function setupCombobox(root) {
  const trigger = root.querySelector('.combobox-trigger');
  const menu = root.querySelector('.combobox-menu');
  const value = root.querySelector('input[type="hidden"]');
  const options = [...menu.querySelectorAll('[role="option"]')];
  const close = () => {
    root.classList.remove('open');
    trigger.setAttribute('aria-expanded', 'false');
    menu.hidden = true;
  };
  const choose = option => {
    value.value = option.dataset.value;
    trigger.querySelector('span').textContent = option.childNodes[0].textContent.trim();
    options.forEach(item => item.setAttribute('aria-selected', String(item === option)));
    close();
    value.dispatchEvent(new Event('change', { bubbles: true }));
  };
  trigger.onclick = () => {
    const open = menu.hidden;
    document.querySelectorAll('.combobox.open').forEach(item => {
      item.classList.remove('open');
      item.querySelector('.combobox-menu').hidden = true;
      item.querySelector('.combobox-trigger').setAttribute('aria-expanded', 'false');
    });
    if (open) {
      root.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
      menu.hidden = false;
      (options.find(item => item.getAttribute('aria-selected') === 'true') || options[0]).focus();
    }
  };
  options.forEach(option => {
    option.onclick = () => choose(option);
    option.onkeydown = event => {
      const index = options.indexOf(option);
      if (event.key === 'ArrowDown') { event.preventDefault(); options[(index + 1) % options.length].focus(); }
      if (event.key === 'ArrowUp') { event.preventDefault(); options[(index - 1 + options.length) % options.length].focus(); }
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(option); }
      if (event.key === 'Escape') { event.preventDefault(); close(); trigger.focus(); }
    };
  });
  document.addEventListener('click', event => { if (!root.contains(event.target)) close(); });
}
document.querySelectorAll('.combobox').forEach(setupCombobox);

document.querySelectorAll('input[name="quality"]').forEach(input => {
  input.onchange = () => {
    document.querySelectorAll('.profile-option').forEach(label => {
      label.classList.toggle('selected', label.querySelector('input').checked);
    });
    const adaptive = input.value === 'beste_qualitaet' && input.checked;
    $('adaptive-cloud').hidden = !adaptive;
    $('routing-hint').hidden = adaptive;
  };
});

function basename(path) {
  return path.split('/').filter(Boolean).pop() || path;
}

function renderSources(refreshPreview = true) {
  $('source-count').textContent = state.sources.length
    ? `${state.sources.length} ${state.sources.length === 1 ? 'Quelle' : 'Quellen'} ausgewählt`
    : 'Noch nichts ausgewählt';
  $('clear-sources').hidden = !state.sources.length;
  $('sources').innerHTML = state.sources.map((source, index) =>
    `<div class="source-item" title="${esc(source.name)}"><span>${esc(source.name)}</span><button data-remove="${index}" aria-label="Entfernen">×</button></div>`
  ).join('');
  $('sources').querySelectorAll('[data-remove]').forEach(button => {
    button.onclick = () => {
      state.sources.splice(Number(button.dataset.remove), 1);
      renderSources();
    };
  });
  if (refreshPreview) loadImportPreview();
}

async function loadImportPreview() {
  const preview = $('import-preview');
  const requestId = ++state.previewRequest;
  if (!state.sources.length) { preview.hidden = true; preview.textContent = ''; return; }
  preview.hidden = false;
  preview.innerHTML = '<span class="spinner" aria-hidden="true"></span> Import wird geprüft …';
  try {
    const response = await fetch('/api/import-preview', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        sources: state.sources.map(source => source.id),
        group_images_by_folder: $('group-images').checked
      })
    });
    if (!response.ok) throw new Error(await responseError(response, 'Importvorschau fehlgeschlagen.'));
    const data = await response.json();
    if (requestId !== state.previewRequest) return;
    state.importDocuments = data.documents || [];
    state.importPreview = data;
    const series = data.series_suggestions?.length && !$('group-images').checked
      ? ` · ${data.series_suggestions.length} mögliche Bildserie${data.series_suggestions.length === 1 ? '' : 'n'}` : '';
    preview.classList.remove('is-error');
    const reviewLabel = data.title_review_count ? `${data.title_review_count} Titel festlegen` : 'Ordnung & Metadaten prüfen';
    preview.innerHTML = `<span>${data.document_count} Dokument${data.document_count === 1 ? '' : 'e'} · ${data.page_count} Seite${data.page_count === 1 ? '' : 'n'}${esc(series)}${data.folder_trees?.length ? ' · Ordnerstruktur wird übernommen' : ''}</span><button type="button" id="review-import" class="text-button">${esc(reviewLabel)}</button>`;
    $('review-import').onclick = openImportDialog;
  } catch (error) {
    if (requestId === state.previewRequest) {
      preview.textContent = error.message;
      preview.classList.add('is-error');
    }
  }
}

$('group-images').onchange = loadImportPreview;

function openImportDialog() {
  const rows = state.importDocuments;
  $('import-dialog-count').textContent = `${rows.length} Dokument${rows.length === 1 ? '' : 'e'}`;
  $('import-documents').innerHTML = rows.map(document => {
    const saved = state.documentMetadata[document.id] || {};
    const suggested = document.title_needs_review ? `Dokument vom ${new Date().toLocaleDateString('de-DE')}` : document.title;
    return `<tr data-document-id="${esc(document.id)}" class="${document.title_needs_review ? 'title-review-row' : ''}"><td><input class="doc-title" value="${esc(saved.title ?? suggested)}" aria-label="Titel ${esc(document.title)}"><small>${document.pages} Seite${document.pages === 1 ? '' : 'n'} · ${esc(document.files.join(', '))}</small>${document.collection_path?.length ? `<small class="collection-destination">→ ${esc(document.collection_path.join(' / '))}</small>` : ''}${document.title_needs_review ? '<small class="title-warning">Der übermittelte Dateiname war nur ein temporärer Systemname.</small>' : ''}</td><td><input class="doc-year" type="number" min="800" max="2100" value="${esc(saved.year ?? '')}" placeholder="auto" aria-label="Jahr ${esc(document.title)}"></td><td><select class="doc-script" aria-label="Schrift ${esc(document.title)}"><option value="auto">Automatisch</option><option value="handschrift">Handschrift</option><option value="druck">Druck</option><option value="schreibmaschine">Schreibmaschine</option></select></td></tr>`;
  }).join('');
  const trees = state.importPreview?.folder_trees || [];
  $('import-tree-summary').hidden = !trees.length;
  $('import-tree-summary').innerHTML = trees.length ? `<strong>Diese Ordnerstruktur wird als Sammlung übernommen</strong>${trees.map(renderImportTree).join('')}` : '';
  rows.forEach(document => {
    const row = [...$('import-documents').querySelectorAll('tr')].find(item => item.dataset.documentId === document.id);
    if (row) row.querySelector('.doc-script').value = state.documentMetadata[document.id]?.script_hint || 'auto';
  });
  $('import-dialog').showModal();
}

function renderImportTree(node) {
  return `<div class="import-tree-node"><span>▣ ${esc(node.name)} <small>${node.document_count || 0} Dokumente · ${node.file_count || 0} Dateien</small></span>${(node.children || []).length ? `<div>${node.children.map(renderImportTree).join('')}</div>` : ''}</div>`;
}

$('import-dialog').onclose = () => {
  if ($('import-dialog').returnValue !== 'confirm') return;
  $('import-documents').querySelectorAll('tr').forEach(row => {
    const year = row.querySelector('.doc-year').value;
    state.documentMetadata[row.dataset.documentId] = {
      title: row.querySelector('.doc-title').value.trim(),
      year: year ? Number(year) : null,
      script_hint: row.querySelector('.doc-script').value
    };
  });
  notify('Dokumentmetadaten übernommen.');
};

$('clear-sources').onclick = () => { state.sources = []; renderSources(); };

async function loadRecovery() {
  const box = $('recovery');
  let rows = [];
  try {
    const response = await fetch('/api/recovery');
    rows = response.ok ? await response.json() : [];
  } catch (_error) {
    return;
  }
  if (!rows.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = '<strong>Unterbrochene Verarbeitung gefunden</strong>' + rows.map(row =>
    `<p>${esc(row.message || `Auftrag ${row.id.slice(0, 8)}`)} <button data-resume="${row.id}">Sicher fortsetzen</button></p>`
  ).join('');
  box.querySelectorAll('[data-resume]').forEach(button => {
    button.onclick = async () => {
      await withButtonBusy(button, 'Wird fortgesetzt …', async () => {
        try {
          const response = await fetch(`/api/jobs/${button.dataset.resume}/resume`, { method: 'POST' });
          const data = await response.json();
          if (!response.ok) { notify(data.detail || 'Fortsetzen fehlgeschlagen', true); return; }
          state.job = data.id;
          watchJob(data.id);
          box.hidden = true;
        } catch (error) {
          notify(`Fortsetzen fehlgeschlagen: ${error.message}`, true);
        }
      });
    };
  });
}

async function upload(files, button = null) {
  if (!files.length) return;
  const count = files.length;
  const message = `${count} ${count === 1 ? 'Datei wird' : 'Dateien werden'} eingelesen …`;
  $('dropzone').setAttribute('aria-busy', 'true');
  setInlineStatus($('drop-status'), message, { busy: true });
  setButtonBusy(button, true, 'Wird eingelesen …');
  try {
    const body = new FormData();
    [...files].forEach(file => body.append('files', file));
    const response = await fetch('/api/uploads', { method: 'POST', body });
    if (!response.ok) throw new Error(await responseError(response, 'Dateien konnten nicht eingelesen werden.'));
    const data = await response.json();
    state.sources.push(...data.sources.filter(source => !state.sources.some(item => item.id === source.id)));
    renderSources();
  } finally {
    $('dropzone').removeAttribute('aria-busy');
    setInlineStatus($('drop-status'), '');
    setButtonBusy(button, false);
  }
}

let nativePickerAction = null;
window.schriftlotseNativePicked = async paths => {
  if (!paths?.length) return;
  const action = nativePickerAction;
  const button = action === 'folder' ? $('folder') : $('choose-files');
  await withButtonBusy(button, 'Auswahl wird übernommen …', async () => {
    const response = await fetch('/api/native-sources', { method: 'POST', headers: { 'content-type': 'application/json', 'x-schriftlotse-instance': window.__schriftlotseNativeToken || '' }, body: JSON.stringify({ paths }) });
    if (!response.ok) throw new Error(await responseError(response, 'Native Auswahl konnte nicht übernommen werden.'));
    const data = await response.json();
    state.sources.push(...data.sources.filter(source => !state.sources.some(item => item.id === source.id)));
    renderSources(false); await loadImportPreview();
    if (action === 'folder') openImportDialog();
    notify(action === 'folder' ? 'Archivordner wurde übernommen.' : `${data.sources.length} Datei${data.sources.length === 1 ? '' : 'en'} ausgewählt.`);
  }).catch(error => notify(error.message, true));
};

$('choose-files').onclick = () => {
  if (window.webkit?.messageHandlers?.schriftlotsePicker) {
    nativePickerAction = 'files';
    window.webkit.messageHandlers.schriftlotsePicker.postMessage({ action: 'files' });
  } else $('files').click();
};
$('files').onchange = event => {
  upload(event.target.files, $('choose-files'))
    .then(() => { event.target.value = ''; notify('Dateien wurden hinzugefügt.'); })
    .catch(error => notify(error.message, true));
};
const dropzone = $('dropzone');
['dragenter', 'dragover'].forEach(name => dropzone.addEventListener(name, event => {
  event.preventDefault();
  dropzone.classList.add('drag');
}));
['dragleave', 'drop'].forEach(name => dropzone.addEventListener(name, event => {
  event.preventDefault();
  dropzone.classList.remove('drag');
}));
dropzone.addEventListener('drop', event => upload(event.dataTransfer.files)
  .then(() => notify('Dateien wurden hinzugefügt.'))
  .catch(error => notify(error.message, true)));

$('folder').onclick = async () => {
  if (window.webkit?.messageHandlers?.schriftlotsePicker) {
    nativePickerAction = 'folder';
    window.webkit.messageHandlers.schriftlotsePicker.postMessage({ action: 'folder' });
    return;
  }
  await withButtonBusy($('folder'), 'Ordnerdialog geöffnet …', async () => {
    setInlineStatus($('drop-status'), 'Ordner wird ausgewählt und geprüft …', { busy: true });
    try {
      const response = await fetch('/api/folder', { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Ordnerauswahl fehlgeschlagen.');
      if (data.source && !state.sources.some(item => item.id === data.source.id)) {
        state.sources.push(data.source);
        renderSources(false);
        await loadImportPreview();
        openImportDialog();
        notify('Ordner wurde hinzugefügt.');
      }
    } catch (error) {
      notify(`Ordnerauswahl fehlgeschlagen: ${error.message}`, true);
    } finally {
      setInlineStatus($('drop-status'), '');
    }
  });
};

$('start').onclick = async () => {
  if (!state.sources.length) { notify('Bitte zuerst Dateien oder einen Ordner auswählen.', true); return; }
  const unresolvedTitles = state.importDocuments.filter(document => document.title_needs_review && !state.documentMetadata[document.id]?.title);
  if (unresolvedTitles.length) {
    openImportDialog();
    notify('Bitte den temporären Dateinamen vor dem Start durch einen verständlichen Titel ersetzen.', true);
    return;
  }
  const quality = document.querySelector('input[name="quality"]:checked').value;
  const cloud = quality === 'beste_qualitaet';
  const cloudOption = state.cloudModels.find(model => model.key === $('job-cloud-model').value);
  if (cloud) {
    const budget = Number($('job-cloud-budget').value);
    const approved = await askConfirmation(
      'Adaptive Cloud-Prüfung erlauben?',
      `SchriftLotse arbeitet zuerst lokal und darf danach nur unsichere Ausschnitte an ${cloudOption?.label || 'OpenRouter'} senden. Hartes Auftragslimit: ${budget.toFixed(2)} $.`
    );
    if (!approved) return;
  }
  const payload = {
    sources: state.sources.map(source => source.id),
    year: $('year').value ? Number($('year').value) : null,
    script: $('script').value,
    quality,
    cloud,
    cloud_budget_usd: cloud ? Number($('job-cloud-budget').value) : 0,
    cloud_model_profile: $('job-cloud-model').value || 'quality',
    group_images_by_folder: $('group-images').checked,
    document_metadata: state.documentMetadata,
    preserve_folder_structure: true
  };
  await withButtonBusy($('start'), 'Auftrag wird vorbereitet …', async () => {
    $('status-message').textContent = 'Auftrag wird vorbereitet …';
    $('status').classList.add('is-loading');
    $('status').setAttribute('aria-busy', 'true');
    try {
      const response = await fetch('/api/jobs', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Start fehlgeschlagen');
      state.job = data.id;
      $('cancel').hidden = false;
      $('exports').innerHTML = '';
      $('job-summary').innerHTML = '';
      $('job-log').innerHTML = '<div class="event-empty">Der Auftrag wird vorbereitet …</div>';
      $('processing-log-count').textContent = '0 Schritte';
      $('live-preview').hidden = true;
      $('live-stage').textContent = '';
      watchJob(data.id);
    } catch (error) {
      $('status').classList.remove('is-loading');
      $('status').removeAttribute('aria-busy');
      $('status-message').textContent = 'Auftrag konnte nicht gestartet werden';
      notify(error.message, true);
    }
  });
};

function duration(seconds) {
  if (seconds == null) return '';
  if (seconds < 60) return `${seconds} s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours} h ${minutes} min` : `${minutes} min`;
}

function renderJobEvents(job) {
  const events = job.events || [];
  $('processing-log-count').textContent = `${events.length} Schritt${events.length === 1 ? '' : 'e'}`;
  if (!events.length) return;
  $('job-log').innerHTML = events.map((event, index) => {
    const evidence = [...(event.evidence || []), ...(event.period?.evidence || [])];
    const engines = event.engines || [];
    const details = [
      event.reason ? `<p><strong>Entscheidung:</strong> ${esc(event.reason)}</p>` : '',
      evidence.length ? `<p><strong>Indizien:</strong> ${esc(evidence.join(' · '))}</p>` : '',
      engines.length ? `<div class="event-engines">${engines.map(run => `<span class="${run.success ? '' : 'failed'}"><strong>${esc(run.engine)}</strong> · ${esc(run.backend)} · ${Number(run.duration_seconds || 0).toFixed(1)} s${run.success ? '' : ` · ${esc(run.message || 'fehlgeschlagen')}`}</span>`).join('')}</div>` : ''
    ].join('');
    const label = event.stage || event.type || 'fortschritt';
    return `<article class="event-row ${index === events.length - 1 ? 'current' : ''}"><i aria-hidden="true"></i><div><header><strong>${esc(event.message || label)}</strong><span>${Math.round(Number(event.progress || 0) * 100)} %</span></header><small>${esc(label)}${event.model ? ` · ${esc(event.model)}` : ''}${event.script ? ` · ${esc(event.script)}` : ''}</small>${details}</div></article>`;
  }).join('');
  $('job-log').scrollTop = $('job-log').scrollHeight;
}

function watchJob(id) {
  if (state.jobEvents) state.jobEvents.close();
  const events = new EventSource(`/api/jobs/${id}/events`);
  state.jobEvents = events;
  $('status').classList.add('is-loading');
  $('status').setAttribute('aria-busy', 'true');
  events.onmessage = async event => {
    const job = JSON.parse(event.data);
    $('status-message').textContent = job.message;
    const eta = job.estimated_remaining_seconds == null ? '' : ` · ca. ${duration(job.estimated_remaining_seconds)} verbleibend`;
    $('status-meta').textContent = `${job.percent} % · ${duration(job.elapsed_seconds)}${eta}`;
    $('progress').value = job.percent;
    renderJobEvents(job);
    renderLiveJob(job);
    $('status').classList.toggle('failed', job.status === 'fehlgeschlagen');
    if (['fertig', 'fehlgeschlagen', 'abgebrochen'].includes(job.status)) {
      events.close();
      state.jobEvents = null;
      $('status').classList.remove('is-loading');
      $('status').removeAttribute('aria-busy');
      $('cancel').hidden = true;
      renderExports(job.exports || []);
      renderJobSummary(job);
      if (job.status === 'fertig' && state.completedJob !== job.id) {
        state.completedJob = job.id;
        await loadDocuments();
        if ((job.document_ids || []).length === 1) {
          openTab('search');
          await showDocument(job.document_ids[0]);
          notify('Entzifferung abgeschlossen – die Transkription ist geöffnet.');
        } else if ((job.document_ids || []).length > 1) {
          openTab('search');
          const first = state.documents.find(document => document.id === job.document_ids[0]);
          if (first?.collection_ids?.length) state.archiveFilter = `collection:${first.collection_ids[0]}`;
          renderDocumentBrowser();
          notify(`${job.document_ids.length} Dokumente sind fertig und in der Bibliothek abgelegt.`);
        }
      }
    }
  };
  events.onerror = () => {
    $('status-message').textContent = 'Statusverbindung wird wiederhergestellt …';
  };
}

function renderLiveJob(job) {
  const live = job.live || {};
  if (!live.preview_url) return;
  $('live-preview').hidden = false;
  const image = $('live-image');
  const nextSource = `${live.preview_url}?v=${encodeURIComponent(live.stage || '')}`;
  if (image.dataset.source !== nextSource) { image.dataset.source = nextSource; image.src = nextSource; }
  $('live-stage').textContent = [live.document_title, live.page_number ? `Seite ${live.page_number}${live.page_count ? `/${live.page_count}` : ''}` : '', live.stage, live.model].filter(Boolean).join(' · ');
  const overlay = $('live-overlay');
  const width = Number(live.width || 1), height = Number(live.height || 1);
  overlay.setAttribute('viewBox', `0 0 ${width} ${height}`);
  overlay.innerHTML = (live.boxes || []).map(box => `<rect x="${Number(box[0])}" y="${Number(box[1])}" width="${Math.max(1, Number(box[2]) - Number(box[0]))}" height="${Math.max(1, Number(box[3]) - Number(box[1]))}"/>`).join('');
}

function renderJobSummary(job) {
  const summary = job.summary || {};
  if (job.status !== 'fertig' || !summary.documents) { $('job-summary').innerHTML = ''; return; }
  $('job-summary').innerHTML = `<div class="completion-summary"><div><span>Dokumente</span><strong>${summary.documents}</strong></div><div><span>Seiten</span><strong>${summary.pages || 0}</strong></div><div><span>Offene Stellen</span><strong>${summary.uncertain || 0}</strong></div><div><span>Modelle</span><strong>${esc((summary.models || []).join(', ') || '–')}</strong></div></div>`;
}

$('cancel').onclick = async () => {
  if (!state.job) return;
  await withButtonBusy($('cancel'), 'Wird abgebrochen …', async () => {
    try {
      const response = await fetch(`/api/jobs/${state.job}/cancel`, { method: 'POST' });
      if (!response.ok) notify(await responseError(response, 'Abbruch fehlgeschlagen.'), true);
    } catch (error) {
      notify(`Abbruch fehlgeschlagen: ${error.message}`, true);
    }
  });
};

function renderExports(downloads) {
  const preferred = ['schriftlotse-ergebnis.zip', 'schriftlotse.pdf', 'schriftlotse.docx', 'transkription_original.txt', 'lesefassung.txt', 'result.json', 'stapelindex.json'];
  const visible = downloads.filter(download => preferred.includes(download.name));
  if (!visible.length) { $('exports').innerHTML = ''; return; }
  $('exports').innerHTML = '<h3>Fertig — weiterarbeiten</h3><div class="exports-grid">' + visible.map(download =>
    `<a href="/api/output/${encodeURIComponent(download.id)}">${esc(download.name)}</a>`
  ).join('') + '<button id="to-search">Im Archiv prüfen →</button></div>';
  $('to-search').onclick = () => openTab('search');
}

async function loadDocuments() {
  const summary = $('archive-summary');
  summary.setAttribute('aria-busy', 'true');
  $('document-browser').innerHTML = loadingMarkup('Dokumente und Sammlungen werden geladen …');
  try {
    const [documentsResponse, collectionsResponse, migrationResponse, sourceFoldersResponse] = await Promise.all([
      fetch('/api/documents'), fetch('/api/collections'), fetch('/api/library/migration-preview'), fetch('/api/source-folders')
    ]);
    if (!documentsResponse.ok) throw new Error(await responseError(documentsResponse, 'Archiv konnte nicht geladen werden.'));
    state.documents = await documentsResponse.json();
    state.totalDocuments = Number(documentsResponse.headers.get('X-Total-Count') || state.documents.length);
    state.collections = collectionsResponse.ok ? await collectionsResponse.json() : [];
    state.migration = migrationResponse.ok ? await migrationResponse.json() : null;
    state.sourceFolders = sourceFoldersResponse.ok ? await sourceFoldersResponse.json() : [];
    renderCollections();
    renderSourceFolders();
    renderDocumentBrowser();
    renderMigrationNotice();
  } catch (error) {
    summary.innerHTML = `<span class="document-chip error-chip">${esc(error.message)}</span>`;
    $('document-browser').innerHTML = `<div class="loading-state is-error">${esc(error.message)}</div>`;
  } finally {
    summary.removeAttribute('aria-busy');
  }
}

function filteredDocuments() {
  let documents = state.documents;
  if (state.archiveFilter === 'eingang') documents = documents.filter(item => !(item.collection_names || []).length);
  if (state.archiveFilter === 'processing') documents = documents.filter(item => item.document_status === 'in_verarbeitung' || item.document_status === 'fehlgeschlagen');
  if (state.archiveFilter === 'unsicher') documents = documents.filter(item => Number(item.uncertain_count || 0) > 0);
  if (state.archiveFilter === 'verified') documents = documents.filter(item => ['bestaetigt', 'ground_truth'].includes(item.document_status));
  if (state.archiveFilter.startsWith('archive:')) {
    const name = state.archiveFilter.slice('archive:'.length);
    documents = documents.filter(item => item.archive === name);
  }
  if (state.archiveFilter.startsWith('collection:')) {
    const id = state.archiveFilter.slice('collection:'.length);
    const descendants = new Set([id, ...collectionDescendants(id)]);
    documents = documents.filter(item => (item.collection_ids || []).some(collectionId => descendants.has(collectionId)));
  }
  const sorted = [...documents];
  if (state.archiveSort === 'title') sorted.sort((a, b) => a.title.localeCompare(b.title, 'de'));
  if (state.archiveSort === 'year-asc') sorted.sort((a, b) => Number(a.year || 9999) - Number(b.year || 9999));
  if (state.archiveSort === 'year-desc') sorted.sort((a, b) => Number(b.year || 0) - Number(a.year || 0));
  if (state.archiveSort === 'archive') sorted.sort((a, b) => String(a.archive || '').localeCompare(String(b.archive || ''), 'de') || a.title.localeCompare(b.title, 'de'));
  return sorted;
}

function renderDocumentBrowser() {
  const documents = filteredDocuments();
  $('document-browser').classList.toggle('list-view', !state.archiveGrid);
  $('document-browser').hidden = false;
  $('search-results-view').hidden = true;
  $('show-library').classList.add('active');
  $('results-title').textContent = state.archiveFilter.startsWith('collection:')
    ? (state.collections.find(item => item.id === state.archiveFilter.slice('collection:'.length))?.path || 'Sammlung')
    : state.archiveFilter.startsWith('archive:')
    ? state.archiveFilter.slice('archive:'.length)
    : ({ all: 'Alle Dokumente', eingang: 'Eingang', processing: 'In Verarbeitung', unsicher: 'Prüfung erforderlich', verified: 'Verifiziert', dateiprobleme: 'Dateiprobleme', papierkorb: 'Papierkorb' }[state.archiveFilter] || 'Dokumente');
  $('result-count').textContent = String(documents.length);
  $('count-all').textContent = String(state.totalDocuments || state.documents.length);
  $('count-inbox').textContent = String(state.documents.filter(item => !(item.collection_names || []).length).length);
  $('count-review').textContent = String(state.documents.filter(item => Number(item.uncertain_count || 0) > 0).length);
  $('archive-summary').innerHTML = `<span class="document-chip"><strong>${documents.length}</strong>${state.totalDocuments > state.documents.length && state.archiveFilter === 'all' ? ` von ${state.totalDocuments}` : ''} Dokument${documents.length === 1 ? '' : 'e'}</span><span class="document-chip">${state.documents.filter(item => item.managed).length} sicher verwaltet</span>`;
  setInlineStatus($('search-status'), documents.length ? 'Dokument auswählen oder Archiv durchsuchen' : 'Diese Ansicht enthält noch keine Dokumente.');
  if (!documents.length) {
    $('document-browser').innerHTML = '<div class="empty-state"><span class="empty-symbol">▧</span><h3>Noch keine Dokumente hier</h3><p>Neue Scans landen zunächst im Eingang.</p></div>';
    return;
  }
  $('document-browser').innerHTML = documents.map(document => {
    const confidence = document.mean_confidence == null ? null : Math.round(Number(document.mean_confidence) * 100);
    const status = document.document_status === 'in_verarbeitung' ? 'wird verarbeitet' : document.document_status === 'fehlgeschlagen' ? 'Verarbeitung fehlgeschlagen' : Number(document.uncertain_count || 0) ? `${document.uncertain_count} offen` : 'keine offenen Stellen';
    return `<button class="document-card" data-document-id="${esc(document.id)}">
      <span class="document-thumb"><img src="${esc(document.thumbnail_url)}" alt="" loading="lazy"><i class="storage-state ${document.managed ? 'managed' : 'referenced'}" title="${document.managed ? 'Original sicher verwaltet' : 'Noch nur referenziert'}"></i></span>
      <span class="document-card-copy"><strong>${esc(document.title)}</strong><small>${esc((document.collection_paths || []).join(' · ') || 'Eingang · noch nicht abgelegt')}</small><span>${document.year || 'ohne Jahr'} · ${document.page_count || 0} Seite${Number(document.page_count) === 1 ? '' : 'n'}${confidence == null ? '' : ` · ${confidence} %`}</span>${document.title_needs_review ? '<em class="needs-review">Titel prüfen</em>' : `<em class="${Number(document.uncertain_count || 0) || document.document_status === 'fehlgeschlagen' ? 'needs-review' : ''}">${esc(status)}</em>`}</span>
    </button>`;
  }).join('');
  if (state.archiveFilter === 'all' && state.documents.length < state.totalDocuments) {
    $('document-browser').insertAdjacentHTML('beforeend', `<button id="load-more-documents" class="load-more">Weitere Dokumente laden · ${state.totalDocuments - state.documents.length} verbleibend</button>`);
    $('load-more-documents').onclick = loadMoreDocuments;
  }
  $('document-browser').querySelectorAll('.document-card').forEach(button => {
    button.onclick = () => showDocument(button.dataset.documentId);
  });
}

async function loadMoreDocuments() {
  const button = $('load-more-documents');
  await withButtonBusy(button, 'Weitere Dokumente werden geladen …', async () => {
    const response = await fetch(`/api/documents?limit=500&offset=${state.documents.length}`);
    if (!response.ok) throw new Error(await responseError(response, 'Weitere Dokumente konnten nicht geladen werden.'));
    const next = await response.json();
    state.documents.push(...next);
    state.totalDocuments = Number(response.headers.get('X-Total-Count') || state.totalDocuments);
    renderDocumentBrowser();
  }).catch(error => notify(error.message, true));
}

function collectionDescendants(collectionId) {
  const children = state.collections.filter(item => item.parent_id === collectionId);
  return children.flatMap(item => [item.id, ...collectionDescendants(item.id)]);
}

function renderCollections() {
  $('collection-list').innerHTML = state.collections.length ? state.collections.map(collection =>
    `<div class="collection-row" style="--collection-depth:${Number(collection.depth || 0)}"><button data-collection="${esc(collection.id)}" title="${esc(collection.path)}"><span>${collection.kind === 'quellordner' ? '▣ ' : ''}${esc(collection.name)}</span><b>${collection.descendant_document_count || 0}</b></button><button data-edit-collection="${esc(collection.id)}" aria-label="${esc(collection.name)} bearbeiten">•••</button></div>`
  ).join('') : '<p class="side-empty">Noch keine eigenen Sammlungen</p>';
  $('collection-list').querySelectorAll('button').forEach(button => {
    if (button.dataset.editCollection) return;
    button.onclick = () => {
      state.archiveFilter = `collection:${button.dataset.collection}`;
      document.querySelectorAll('#archive-navigation button,.collection-list button').forEach(item => item.classList.remove('active'));
      button.classList.add('active');
      renderDocumentBrowser();
    };
  });
  $('collection-list').querySelectorAll('[data-edit-collection]').forEach(button => button.onclick = event => { event.stopPropagation(); openCollectionDialog(button.dataset.editCollection); });
  const archives = [...new Set(state.documents.map(item => item.archive).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'de'));
  $('archive-list').innerHTML = archives.length ? archives.map(name => `<button data-archive="${esc(name)}"><span>${esc(name)}</span><b>${state.documents.filter(item => item.archive === name).length}</b></button>`).join('') : '<p class="side-empty">Noch keine Archivangaben</p>';
  $('archive-list').querySelectorAll('button').forEach(button => button.onclick = () => {
    state.archiveFilter = `archive:${button.dataset.archive}`;
    document.querySelectorAll('#archive-navigation button,.collection-list button').forEach(item => item.classList.remove('active'));
    button.classList.add('active'); renderDocumentBrowser();
  });
  renderSavedSearches();
}

function renderSourceFolders() {
  $('source-folder-list').innerHTML = state.sourceFolders.length ? state.sourceFolders.map(source =>
    `<button data-source-folder="${esc(source.id)}" title="${esc(source.root_path)}"><span>${source.reachable ? '↻' : '⚠'} ${esc(source.label)}</span><b>${source.file_count || 0}</b></button>`
  ).join('') : '<p class="side-empty">Noch kein Ordner verknüpft</p>';
  $('source-folder-list').querySelectorAll('[data-source-folder]').forEach(button => {
    button.onclick = () => openSourceSync(button.dataset.sourceFolder, button);
  });
}

async function openSourceSync(sourceId, button = null) {
  state.syncSourceId = sourceId;
  $('source-sync-title').textContent = 'Ordneränderungen werden geprüft …';
  $('source-sync-count').textContent = '';
  $('source-sync-content').innerHTML = loadingMarkup('Prüfsummen und Ordnerstruktur werden verglichen …');
  $('source-sync-dialog').showModal();
  setButtonBusy(button, true, 'Prüft …');
  try {
    const response = await fetch(`/api/source-folders/${encodeURIComponent(sourceId)}/diff`);
    if (!response.ok) throw new Error(await responseError(response, 'Ordner konnte nicht geprüft werden.'));
    const data = await response.json();
    $('source-sync-title').textContent = data.reachable ? `${data.label} abgleichen` : `${data.label} ist nicht erreichbar`;
    $('source-sync-count').textContent = data.reachable ? `${data.changes.length} Änderung${data.changes.length === 1 ? '' : 'en'}` : 'offline';
    const labels = { new: 'Neu', changed: 'Geändert', moved: 'Verschoben', missing: 'Quelle fehlt' };
    $('source-sync-content').innerHTML = !data.reachable
      ? '<div class="empty-state"><h3>Ordner nicht gefunden</h3><p>Die verwalteten Originale und Transkriptionen bleiben sicher erhalten.</p></div>'
      : data.changes.length
      ? data.changes.map(change => `<label class="sync-change ${esc(change.kind)}"><input type="checkbox" value="${esc(change.relative_path)}" ${['new','changed','moved'].includes(change.kind) ? 'checked' : 'disabled'}><span><strong>${esc(labels[change.kind] || change.kind)}</strong>${esc(change.relative_path)}${change.previous_path ? `<small>vorher: ${esc(change.previous_path)} · wird ohne neue OCR umgeordnet</small>` : ''}${change.kind === 'missing' ? '<small>Das verwaltete Dokument wird nicht gelöscht.</small>' : ''}</span></label>`).join('')
      : '<div class="empty-state compact"><h3>Alles aktuell</h3><p>Keine neuen oder geänderten Dateien gefunden.</p></div>';
    $('prepare-source-sync').disabled = !data.changes.some(change => ['new', 'changed', 'moved'].includes(change.kind));
  } catch (error) {
    $('source-sync-content').innerHTML = `<div class="loading-state is-error">${esc(error.message)}</div>`;
    $('prepare-source-sync').disabled = true;
  } finally {
    setButtonBusy(button, false);
  }
}

$('source-sync-dialog').addEventListener('close', async () => {
  if ($('source-sync-dialog').returnValue !== 'confirm' || !state.syncSourceId) return;
  const paths = [...$('source-sync-content').querySelectorAll('input:checked')].map(input => input.value);
  const response = await fetch(`/api/source-folders/${encodeURIComponent(state.syncSourceId)}/prepare-sync`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ relative_paths: paths }) });
  if (!response.ok) return notify(await responseError(response, 'Änderungen konnten nicht vorbereitet werden.'), true);
  const data = await response.json();
  state.sources = data.sources || [];
  if (state.sources.length) { openTab('read'); renderSources(); }
  else await loadDocuments();
  notify(`${state.sources.length} Datei${state.sources.length === 1 ? '' : 'en'} zur Verarbeitung, ${data.moved || 0} ohne neue OCR umgeordnet.`);
});

function renderSavedSearches() {
  $('saved-search-list').innerHTML = state.savedSearches.length ? state.savedSearches.map((item, index) => `<div class="saved-search"><button data-saved-search="${index}" title="${esc(item.text)}">${esc(item.text)}</button><button data-remove-search="${index}" aria-label="Gespeicherte Suche entfernen">×</button></div>`).join('') : '<p class="side-empty">Noch keine gespeicherten Suchen</p>';
  $('saved-search-list').querySelectorAll('[data-saved-search]').forEach(button => button.onclick = () => {
    const item = state.savedSearches[Number(button.dataset.savedSearch)];
    $('query').value = item.text; $('search-mode').value = item.mode; $('year-from').value = item.yearFrom || ''; $('year-to').value = item.yearTo || ''; $('fuzziness').value = item.fuzziness; $('fuzziness-value').textContent = `${item.fuzziness} %`; runSearch();
  });
  $('saved-search-list').querySelectorAll('[data-remove-search]').forEach(button => button.onclick = event => {
    event.stopPropagation(); state.savedSearches.splice(Number(button.dataset.removeSearch), 1); localStorage.setItem('schriftlotse-saved-searches', JSON.stringify(state.savedSearches)); renderSavedSearches();
  });
}

$('save-search').onclick = () => {
  const text = $('query').value.trim();
  if (!text) return notify('Zuerst einen Suchbegriff eingeben.', true);
  const saved = { text, mode: $('search-mode').value, yearFrom: $('year-from').value, yearTo: $('year-to').value, fuzziness: Number($('fuzziness').value) };
  state.savedSearches = state.savedSearches.filter(item => item.text !== text || item.mode !== saved.mode);
  state.savedSearches.unshift(saved); state.savedSearches = state.savedSearches.slice(0, 20);
  localStorage.setItem('schriftlotse-saved-searches', JSON.stringify(state.savedSearches)); renderSavedSearches(); notify('Suche gespeichert.');
};

document.querySelectorAll('#archive-navigation button').forEach(button => {
  button.onclick = async () => {
    state.archiveFilter = button.dataset.filter;
    document.querySelectorAll('#archive-navigation button,.collection-list button').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    if (state.archiveFilter === 'jobs') {
      await renderJobHistory();
      return;
    }
    if (state.archiveFilter === 'dateiprobleme') {
      setInlineStatus($('search-status'), 'Letzte Integritätsprüfungen werden geladen …', { busy: true });
      const response = await fetch('/api/documents?status=dateiprobleme');
      const problems = response.ok ? await response.json() : [];
      const original = state.documents;
      state.documents = problems;
      renderDocumentBrowser();
      state.documents = original;
      return;
    }
    if (state.archiveFilter === 'papierkorb') {
      setInlineStatus($('search-status'), 'Papierkorb wird geladen …', { busy: true });
      const response = await fetch('/api/documents?status=papierkorb');
      const deleted = response.ok ? await response.json() : [];
      const original = state.documents;
      state.documents = deleted;
      renderDocumentBrowser();
      state.documents = original;
      return;
    }
    renderDocumentBrowser();
  };
});

async function renderJobHistory() {
  $('document-browser').hidden = false;
  $('search-results-view').hidden = true;
  $('results-title').textContent = 'Verarbeitungsläufe';
  $('document-browser').innerHTML = loadingMarkup('Laufprotokolle werden geladen …');
  const response = await fetch('/api/job-history?limit=100');
  if (!response.ok) {
    $('document-browser').innerHTML = '<div class="loading-state is-error">Laufprotokolle konnten nicht geladen werden.</div>';
    return;
  }
  const jobs = await response.json();
  $('result-count').textContent = String(jobs.length);
  $('archive-summary').innerHTML = '<span class="document-chip">Dauerhaft gespeicherte Aufträge</span>';
  setInlineStatus($('search-status'), jobs.length ? 'Neueste Läufe zuerst' : 'Noch keine Verarbeitungsläufe');
  $('document-browser').innerHTML = jobs.length ? jobs.map(job => `<div class="job-card"><span class="job-state ${job.status}"></span><div><strong>${esc(job.message || 'Verarbeitungsauftrag')}</strong><small>${esc(job.updated_at)} · ${job.document_count || 0} Dokumente · ${job.page_count || 0} Seiten</small><span>${esc(job.status)}${Number(job.cloud_cost || 0) ? ` · Cloud $${Number(job.cloud_cost).toFixed(4)}` : ''}</span></div></div>`).join('') : '<div class="empty-state">Noch keine Verarbeitungsläufe gespeichert.</div>';
}

$('show-library').onclick = () => {
  state.archiveFilter = 'all';
  document.querySelectorAll('#archive-navigation button,.collection-list button').forEach(item => item.classList.remove('active'));
  document.querySelector('#archive-navigation [data-filter="all"]').classList.add('active');
  renderDocumentBrowser();
};
$('archive-grid-toggle').onclick = () => {
  state.archiveGrid = !state.archiveGrid;
  $('archive-grid-toggle').textContent = state.archiveGrid ? '▦' : '☷';
  renderDocumentBrowser();
};
$('archive-sort').onchange = () => { state.archiveSort = $('archive-sort').value; renderDocumentBrowser(); };

async function showDocumentLegacy(documentId) {
  $('viewer-empty').hidden = true;
  $('viewer-content').hidden = true;
  $('document-detail').hidden = false;
  $('document-detail').innerHTML = loadingMarkup('Dokument und technischer Bericht werden geladen …');
  let document;
  try {
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}`);
    if (!response.ok) throw new Error(await responseError(response, 'Dokument konnte nicht geladen werden.'));
    document = await response.json();
  } catch (error) {
    $('document-detail').innerHTML = `<div class="loading-state is-error">${esc(error.message)}</div>`;
    return;
  }
  const pages = document.pages || [];
  const selectedCollections = new Set((document.collections || []).map(item => item.id));
  const diagnostics = pages[0] || {};
  $('document-detail').innerHTML = `
    <div class="detail-hero"><img src="${esc(document.thumbnail_url)}" alt="Vorschau"><div><p class="eyebrow">Dokument</p><h3>${esc(document.title)}</h3><p>${esc([document.archive, document.fonds, document.shelfmark].filter(Boolean).join(' · ') || 'Noch ohne Archivangaben')}</p><span class="managed-badge ${document.library_managed ? '' : 'warning'}">${document.library_managed ? '✓ Original sicher verwaltet' : 'Nur Quelldatei referenziert'}</span></div></div>
    <div class="page-filmstrip">${pages.map(page => `<button data-page="${page.page_index}" title="Seite ${Number(page.page_index) + 1}"><img src="${esc(page.thumbnail_url)}" alt="Seite ${Number(page.page_index) + 1}" loading="lazy"><span>${Number(page.page_index) + 1}</span></button>`).join('')}</div>
    ${pages.length ? `<div class="detail-page-preview"><img id="detail-page-image" src="${esc(pages[0].image_url)}" alt="Ausgewählte Dokumentseite"></div>` : ''}
    <details open><summary>Archivangaben</summary><div class="metadata-grid">
      <label class="wide">Titel<input data-meta="title" value="${esc(document.title)}"></label>
      <label>Jahr<input data-meta="year" type="number" min="800" max="2100" value="${document.year || ''}"></label><label>Archiv<input data-meta="archive" value="${esc(document.archive || '')}"></label>
      <label>Status<select data-document-status><option value="automatisch" ${document.document_status === 'automatisch' ? 'selected' : ''}>Automatisch erkannt</option><option value="in_pruefung" ${['in_pruefung','in_verarbeitung','fehlgeschlagen'].includes(document.document_status) ? 'selected' : ''}>In Prüfung</option><option value="bestaetigt" ${document.document_status === 'bestaetigt' ? 'selected' : ''}>Geprüft</option><option value="ground_truth" ${document.document_status === 'ground_truth' ? 'selected' : ''}>Ground Truth</option></select></label><span></span>
      <label>Bestand<input data-meta="fonds" value="${esc(document.fonds || '')}"></label><label>Signatur<input data-meta="shelfmark" value="${esc(document.shelfmark || '')}"></label>
      <label>Serie<input data-meta="series" value="${esc(document.series || '')}"></label><label>Ort<input data-meta="place" value="${esc(document.place || '')}"></label>
      <label>Urheber / Schreiber<input data-meta="creator" value="${esc(document.creator || '')}"></label><label>Externe Kennung<input data-meta="external_id" value="${esc(document.external_id || '')}"></label>
      <label>Datierung von<input data-meta="date_from" type="number" min="800" max="2100" value="${document.date_from || ''}"></label><label>Datierung bis<input data-meta="date_to" type="number" min="800" max="2100" value="${document.date_to || ''}"></label>
      <label class="wide">Tags <small>mit Komma trennen</small><input data-tags value="${esc((document.tags || []).join(', '))}"></label>
      <label class="wide">Quelllink<input data-meta="source_url" value="${esc(document.source_url || '')}"></label>
      <label class="wide">Beschreibung<textarea data-meta="description" rows="2">${esc(document.description || '')}</textarea></label>
      <label class="wide">Rechte / Nutzung<input data-meta="rights" value="${esc(document.rights || '')}"></label>
      <label class="wide">Notizen<textarea data-meta="notes" rows="2">${esc(document.notes || '')}</textarea></label>
    </div><div class="collection-checks">${state.collections.map(collection => `<label><input type="checkbox" data-collection-id="${collection.id}" ${selectedCollections.has(collection.id) ? 'checked' : ''}>${esc(collection.name)}</label>`).join('') || '<small>Noch keine Sammlung angelegt</small>'}</div><button id="save-document-meta" class="primary">Metadaten speichern</button></details>
    <details><summary>Technischer Bericht · ${pages.length} Seite${pages.length === 1 ? '' : 'n'}</summary>
      <div id="technical-page-report">${technicalPageMarkup(diagnostics)}</div>
    </details>
    <div class="detail-actions"><button id="detail-review">Unsichere prüfen</button><button id="detail-reprocess">Erneut verarbeiten</button><button id="detail-export">Exporte neu erzeugen</button><button id="detail-integrity">Original prüfen</button><button id="detail-repair">Original reparieren</button><button id="detail-cite">Zitat kopieren</button><button id="detail-finder">Im Finder</button>${document.deleted_at ? '<button id="detail-restore" class="primary">Wiederherstellen</button><button id="detail-purge" class="danger-text">Endgültig löschen</button>' : '<button id="detail-trash" class="danger-text">Papierkorb</button>'}</div>`;
  $('document-detail').querySelectorAll('.page-filmstrip button').forEach(button => {
    button.onclick = () => {
      const page = pages.find(item => String(item.page_index) === button.dataset.page);
      if (!page || !$('detail-page-image')) return;
      $('detail-page-image').src = page.image_url;
      $('technical-page-report').innerHTML = technicalPageMarkup(page);
      $('document-detail').querySelectorAll('.page-filmstrip button').forEach(item => item.classList.remove('active'));
      button.classList.add('active');
    };
  });
  $('document-detail').querySelector('.page-filmstrip button')?.classList.add('active');
  $('save-document-meta').onclick = () => saveDocumentMetadata(documentId, document.document_status || 'automatisch');
  $('detail-review').onclick = () => { $('review-queue').click(); };
  $('detail-reprocess').onclick = async () => {
    if (!await askConfirmation('Dokument erneut verarbeiten?', 'Die vorhandene Transkription wird mit dem aktuellen lokalen Standardprofil neu erzeugt. Archivangaben und Sammlungen bleiben erhalten.')) return;
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/reprocess`, { method: 'POST' });
    if (!response.ok) return notify(await responseError(response, 'Neuverarbeitung konnte nicht gestartet werden.'), true);
    const job = await response.json(); state.job = job.id; openTab('read'); $('cancel').hidden = false; watchJob(job.id);
  };
  $('detail-export').onclick = () => exportDocument(documentId, $('detail-export'));
  $('detail-integrity').onclick = () => verifyLibrary($('detail-integrity'));
  $('detail-repair').onclick = async () => {
    if (!await askConfirmation('Verwaltetes Original reparieren?', 'Fehlende oder veränderte Bibliothekskopien werden aus dem unveränderten Prüfsummenobjekt wiederhergestellt.')) return;
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/repair`, { method: 'POST' });
    if (!response.ok) return notify(await responseError(response, 'Original konnte nicht repariert werden.'), true);
    const data = await response.json();
    notify(data.unresolved.length ? `${data.repaired.length} repariert, ${data.unresolved.length} nicht wiederherstellbar.` : `${data.repaired.length} Datei${data.repaired.length === 1 ? '' : 'en'} repariert.`, Boolean(data.unresolved.length));
  };
  $('detail-cite').onclick = async () => {
    const citation = [document.archive, document.fonds, document.shelfmark, document.title, document.year].filter(Boolean).join(', ');
    try { await navigator.clipboard.writeText(citation); notify('Archivzitat kopiert.'); }
    catch (_error) { notify('Zitat konnte nicht in die Zwischenablage kopiert werden.', true); }
  };
  $('detail-finder').onclick = async () => {
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/reveal`, { method: 'POST' });
    if (!response.ok) notify(await responseError(response, 'Original konnte nicht angezeigt werden.'), true);
  };
  if ($('detail-trash')) $('detail-trash').onclick = async () => {
      if (!await askConfirmation('Dokument in den Papierkorb?', 'Originale bleiben erhalten und können wiederhergestellt werden.')) return;
      const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' });
      if (response.ok) { notify('Dokument wurde in den Papierkorb verschoben.'); $('document-detail').hidden = true; $('viewer-empty').hidden = false; loadDocuments(); }
    };
  if ($('detail-restore')) $('detail-restore').onclick = async () => {
      const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/restore`, { method: 'POST' });
      if (response.ok) { notify('Dokument wiederhergestellt.'); $('document-detail').hidden = true; $('viewer-empty').hidden = false; await loadDocuments(); }
    };
  if ($('detail-purge')) $('detail-purge').onclick = async () => {
      if (!await askConfirmation('Dokument endgültig löschen?', 'Originale, Transkriptionen und Exporte werden unwiderruflich entfernt. Diese Aktion kann nicht rückgängig gemacht werden.')) return;
      const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/permanent`, { method: 'DELETE' });
      if (!response.ok) return notify(await responseError(response, 'Dokument konnte nicht gelöscht werden.'), true);
      notify('Dokument und verwaltete Dateien wurden endgültig gelöscht.'); $('document-detail').hidden = true; $('viewer-empty').hidden = false; await loadDocuments();
    };
}

async function showDocument(documentId) {
  state.selectedDocumentId = documentId;
  state.selectedPage = 0;
  document.querySelector('.archive-workspace')?.classList.add('reader-open');
  $('viewer-empty').hidden = true;
  $('viewer-content').hidden = true;
  $('document-detail').hidden = false;
  $('document-detail').innerHTML = loadingMarkup('Scan und vollständige Transkription werden geladen …');
  let documentData;
  let transcript;
  let history = [];
  try {
    const [documentResponse, transcriptResponse] = await Promise.all([
      fetch(`/api/documents/${encodeURIComponent(documentId)}`),
      fetch(`/api/documents/${encodeURIComponent(documentId)}/transcript`)
    ]);
    if (!documentResponse.ok) throw new Error(await responseError(documentResponse, 'Dokument konnte nicht geladen werden.'));
    if (!transcriptResponse.ok) throw new Error(await responseError(transcriptResponse, 'Transkription konnte nicht geladen werden.'));
    documentData = await documentResponse.json();
    transcript = await transcriptResponse.json();
    if (documentData.job_id) {
      const historyResponse = await fetch(`/api/jobs/${encodeURIComponent(documentData.job_id)}/history`);
      if (historyResponse.ok) history = await historyResponse.json();
    }
  } catch (error) {
    $('document-detail').innerHTML = `<div class="loading-state is-error">${esc(error.message)}</div>`;
    return;
  }
  const pages = documentData.pages || [];
  const selectedCollections = new Set((documentData.collections || []).map(item => item.id));
  const inbox = !selectedCollections.size;
  const deleted = Boolean(documentData.deleted_at);
  const cloudSummary = transcript.cloud_summary || { line_count: 0, models: [], cost_usd: 0 };
  const localModelLabels = { 'trocr-kurrent-19': 'TrOCR Kurrent 19. Jh.', 'trocr-kurrent-early': 'TrOCR Kurrent 16.–18. Jh.', 'churro-mlx-8bit': 'CHURRO MLX 8-Bit', 'ub-german-handwriting': 'Kraken · UB deutsche Handschrift', 'party-v4': 'Kraken Party v4', manuell: 'Manuell bestätigte Fassung' };
  const cloudModelLabel = model => state.cloudModels.find(option => option.model === model)?.label || localModelLabels[model] || model;
  const cloudModelNames = (cloudSummary.models || []).map(item => cloudModelLabel(item.model)).join(', ');
  const modelVersions = transcript.model_versions || [];
  const cloudIssueCount = transcript.pages.flatMap(page => page.lines).flatMap(line => line.readings || []).filter(reading => reading.kind === 'cloud' && (reading.quality_issues || []).length).length;
  $('document-detail').innerHTML = `
    <div class="document-reader">
      <header class="reader-toolbar">
        <button id="reader-back" class="text-button">← Bibliothek</button>
        <div class="reader-title"><small>${esc((documentData.collections || []).map(item => item.name).join(' / ') || 'Eingang')}</small><strong>${esc(documentData.title)}</strong></div>
        <span class="reader-status ${Number(documentData.pages?.reduce((sum, page) => sum + Number(page.uncertain_count || 0), 0)) ? 'needs-review' : ''}">${transcript.line_count} Zeilen · ${documentData.pages?.reduce((sum, page) => sum + Number(page.uncertain_count || 0), 0) || 0} offen</span>
        <div class="reader-toolbar-actions">${deleted ? '<button id="reader-restore" class="primary">Wiederherstellen</button><button id="reader-purge" class="danger-text">Endgültig löschen</button>' : '<button id="reader-trash" class="danger-text">In Papierkorb</button><button id="reader-export">Exportieren</button>'}</div>
      </header>
      ${deleted ? `<div class="reader-notice deleted"><strong>Im Papierkorb</strong><span>Gelöscht am ${esc(documentData.deleted_at)}. Du kannst das Dokument wiederherstellen oder endgültig entfernen.</span></div>` : ''}
      ${documentData.title_needs_review || /^temp(?:orary)?image/i.test(documentData.title) ? '<div class="reader-notice warning"><strong>Titel prüfen</strong><span>macOS hat nur einen temporären Bildnamen übermittelt. Unter „Details“ kannst du einen passenden Titel vergeben.</span></div>' : ''}
      ${inbox && !deleted ? '<div class="reader-notice"><strong>Noch im Eingang</strong><span>Die Entzifferung ist fertig. Ordne das Dokument unter „Details“ einer Sammlung zu.</span><button id="reader-file-document">Jetzt ablegen</button></div>' : ''}
      ${cloudSummary.line_count ? `<div class="reader-notice cloud ${cloudIssueCount ? 'warning' : ''}"><strong>Cloud-Zweitprüfung</strong><span>${cloudSummary.line_count} von ${transcript.line_count} Zeilen mit ${esc(cloudModelNames)} geprüft · $${Number(cloudSummary.cost_usd || 0).toFixed(5)}. Die lokale Hauptlesung wurde nicht automatisch ersetzt.${cloudIssueCount ? ` ${cloudIssueCount} ältere Ausgabe${cloudIssueCount === 1 ? '' : 'n'} ist/sind formal auffällig.` : ''}</span><button data-open-cloud-comparison>Vergleichen</button></div>` : ''}
      <nav class="reader-tabs"><button data-reader-tab="read" class="active">Lesen</button><button data-reader-tab="review">Prüfen</button>${cloudSummary.line_count ? `<button data-reader-tab="cloud">Cloud-Vergleich <b>${cloudSummary.line_count}</b></button>` : ''}<button data-reader-tab="details">Details & Technik</button></nav>
      <section id="reader-reading" class="reader-panel active">
        <aside class="reader-pages">${pages.map(page => `<button data-reader-page="${page.page_index}" title="Seite ${Number(page.page_index) + 1}"><img src="${esc(page.thumbnail_url)}" alt=""><span>${Number(page.page_index) + 1}</span></button>`).join('')}</aside>
        <div class="reader-scan"><div class="reader-scan-toolbar"><span id="reader-page-label">Seite 1 von ${pages.length}</span><div><button data-image-view="original" class="active">Original</button><button data-image-view="prepared">Aufbereitet</button></div></div><div class="reader-scan-canvas"><img id="reader-scan-image" alt="Ausgewählte Originalseite"><canvas id="reader-line-overlay"></canvas></div></div>
        <div class="reader-text"><div class="reader-text-toolbar"><div class="reader-view-controls"><button data-text-view="diplomatic" class="active">Transkription</button><button data-text-view="reading">Lesefassung</button><button data-text-view="document">Gesamtdokument</button></div><label id="reader-version-control" class="reader-version-control" hidden>Fassung<select id="reader-version"><option value="">Hauptlesung · ausgewählt</option>${modelVersions.map(version => `<option value="${esc(version.id)}">${esc(cloudModelLabel(version.model))} · ${version.covered_lines}/${version.total_lines}${version.complete ? '' : ' Teilfassung'}${(version.quality_notes || []).length ? ' ⚠' : ''}</option>`).join('')}</select></label><button id="next-uncertain">Nächste unsichere Stelle</button></div><div id="reader-lines" class="reader-lines"></div><div id="reader-editor" class="reader-editor" hidden><label>Zeile korrigieren<textarea id="reader-line-text" rows="3"></textarea></label><div id="reader-alternatives" class="reader-alternatives"></div><div class="button-row"><button id="reader-save-line" class="primary">Korrektur bestätigen</button><button id="reader-cancel-line">Schließen</button></div><div id="reader-edit-status" class="status-copy"></div></div></div>
      </section>
      <section id="reader-details" class="reader-panel">
        <div class="reader-details-grid"><div class="details-form"><h3>Ordnung und Archivangaben</h3><div class="metadata-grid">
          <label class="wide">Titel<input data-meta="title" value="${esc(documentData.title)}"></label><label>Jahr<input data-meta="year" type="number" min="800" max="2100" value="${documentData.year || ''}"></label><label>Archiv<input data-meta="archive" value="${esc(documentData.archive || '')}"></label><label>Bestand<input data-meta="fonds" value="${esc(documentData.fonds || '')}"></label><label>Signatur<input data-meta="shelfmark" value="${esc(documentData.shelfmark || '')}"></label><label>Serie<input data-meta="series" value="${esc(documentData.series || '')}"></label><label>Ort<input data-meta="place" value="${esc(documentData.place || '')}"></label><label class="wide">Tags<input data-tags value="${esc((documentData.tags || []).join(', '))}"></label><label class="wide">Beschreibung<textarea data-meta="description" rows="3">${esc(documentData.description || '')}</textarea></label><label class="wide">Notizen<textarea data-meta="notes" rows="3">${esc(documentData.notes || '')}</textarea></label>
        </div><h4>Sammlungen</h4><div class="collection-checks collection-tree-checks">${state.collections.map(collection => `<label style="--collection-depth:${Number(collection.depth || 0)}"><input type="checkbox" data-collection-id="${collection.id}" ${selectedCollections.has(collection.id) ? 'checked' : ''}>${esc(collection.path)}</label>`).join('') || '<small>Noch keine Sammlung angelegt</small>'}</div><input type="hidden" data-document-status value="${esc(documentData.document_status || 'automatisch')}">${deleted ? '<p class="hint">Zum Bearbeiten das Dokument zuerst wiederherstellen.</p>' : '<button id="save-document-meta" class="primary">Änderungen speichern</button>'}</div>
        <div class="details-technical"><h3>Technischer Bericht</h3><div id="technical-page-report">${technicalPageMarkup(pages[0] || {})}</div><h4>Verarbeitungslauf</h4><div class="stored-event-list">${history.length ? history.map(event => `<div><span>${esc(event.created_at)}</span><strong>${esc(event.message)}</strong>${event.payload?.model ? `<small>${esc(event.payload.model)}</small>` : ''}${event.payload?.reason ? `<small>${esc(event.payload.reason)}</small>` : ''}</div>`).join('') : '<p class="hint">Für diesen älteren Lauf ist kein Detailprotokoll gespeichert.</p>'}</div><div class="detail-actions">${deleted ? '<button id="detail-restore" class="primary">Wiederherstellen</button><button id="detail-purge" class="danger-text">Endgültig löschen</button>' : '<button id="detail-reprocess">Erneut verarbeiten</button><button id="detail-integrity">Original prüfen</button><button id="detail-finder">Im Finder</button><button id="detail-trash" class="danger-text">In Papierkorb</button>'}</div></div></div>
      </section>
    </div>`;

  let pageIndex = 0;
  let textView = 'diplomatic';
  let reviewOnly = false;
  let cloudOnly = false;
  let selectedVersionId = '';
  let imageView = 'original';
  let selectedLine = null;

  const currentPage = () => transcript.pages.find(page => Number(page.page_index) === Number(pageIndex)) || transcript.pages[0];
  function drawReaderLine(line) {
    selectedLine = line;
    const image = $('reader-scan-image');
    const canvas = $('reader-line-overlay');
    if (!line || !image.naturalWidth) return;
    const scaleX = image.clientWidth / image.naturalWidth;
    const scaleY = image.clientHeight / image.naturalHeight;
    canvas.width = image.clientWidth; canvas.height = image.clientHeight;
    canvas.style.width = `${image.clientWidth}px`; canvas.style.height = `${image.clientHeight}px`;
    const context = canvas.getContext('2d');
    const [x1, y1, x2, y2] = line.bbox;
    context.fillStyle = 'rgba(211,157,50,.18)'; context.strokeStyle = '#c58719'; context.lineWidth = 3;
    context.fillRect(x1 * scaleX, y1 * scaleY, (x2 - x1) * scaleX, (y2 - y1) * scaleY);
    context.strokeRect(x1 * scaleX, y1 * scaleY, (x2 - x1) * scaleX, (y2 - y1) * scaleY);
    $('reader-line-text').value = line.text;
    const allReadings = (line.readings || []).filter(reading => reading.text);
    const readings = allReadings.filter(reading => reading.text !== line.text);
    const matchingCloud = allReadings.filter(reading => reading.kind === 'cloud' && reading.text === line.text);
    $('reader-alternatives').innerHTML = readings.length || matchingCloud.length ? `<small>Gespeicherte Zweitlesungen – erst mit „Korrektur bestätigen“ wird eine davon zur Hauptlesung</small>${matchingCloud.map(reading => `<div class="cloud-agreement">✓ ${esc(cloudModelLabel(reading.model))} stimmt mit der lokalen Hauptlesung überein.</div>`).join('')}${readings.map((reading, index) => `<button data-reader-alternative="${index}" class="${reading.kind === 'cloud' ? 'cloud-alternative' : ''} ${(reading.quality_issues || []).length ? 'suspicious' : ''}">${esc(reading.text)} <span>${reading.kind === 'cloud' ? 'Cloud · ' : ''}${esc(cloudModelLabel(reading.model))} · ${Math.round(Number(reading.confidence || 0) * 100)} %${(reading.quality_issues || []).length ? ` · ⚠ ${esc(reading.quality_issues.join('; '))}` : ''}</span></button>`).join('')}` : '<div class="no-alternatives">Für diese Zeile ist keine weitere Modell-Lesung gespeichert.</div>';
    $('reader-alternatives').querySelectorAll('[data-reader-alternative]').forEach(button => button.onclick = event => { event.stopPropagation(); $('reader-line-text').value = readings[Number(button.dataset.readerAlternative)].text; });
    $('reader-editor').hidden = false;
    $('reader-lines').querySelectorAll('[data-line-id]').forEach(button => button.classList.toggle('selected', button.dataset.lineId === line.id));
    image.parentElement.scrollTo({ top: Math.max(0, y1 * scaleY - 80), behavior: 'smooth' });
  }
  const cloudReadingsFor = line => (line.readings || []).filter(reading => reading.kind === 'cloud' && reading.text);
  const selectedVersion = () => modelVersions.find(version => version.id === selectedVersionId) || null;
  function versionNotice(version) {
    if (!version) return '<div class="model-version-note"><strong>Hauptlesung</strong><span>Verwendet die aktuell ausgewählten beziehungsweise bestätigten Zeilen.</span></div>';
    const coverage = `${version.covered_lines} von ${version.total_lines} Zeilen stammen aus ${esc(cloudModelLabel(version.model))}`;
    const fallback = version.complete ? 'Vollständige gespeicherte Modellfassung.' : 'Reine Teilfassung; fehlende Zeilen werden nicht mit anderem Text aufgefüllt.';
    const warning = (version.quality_notes || []).length ? ` · ⚠ ${esc(version.quality_notes.join('; '))}` : '';
    return `<div class="model-version-note ${(version.quality_notes || []).length ? 'warning' : ''}"><strong>${esc(cloudModelLabel(version.model))}${version.complete ? '' : ' · Teilfassung'}</strong><span>${coverage}. ${fallback}${warning}</span></div>`;
  }
  function cloudComparisonMarkup(line) {
    const readings = cloudReadingsFor(line);
    return `<button class="cloud-comparison-card" data-line-id="${esc(line.id)}"><span class="cloud-comparison-head"><strong>Zeile ${Number(line.line_order || 0) + 1}</strong><em>${readings.length} Cloud-Lesung${readings.length === 1 ? '' : 'en'}</em></span><span class="comparison-reading local"><small>Lokale Hauptlesung · ${esc(line.model)}</small><span>${esc(line.text || '‹leer›')}</span></span>${readings.map(reading => `<span class="comparison-reading cloud ${(reading.quality_issues || []).length ? 'suspicious' : ''}"><small>Cloud-Zweitlesung · ${esc(cloudModelLabel(reading.model))}${(reading.quality_issues || []).length ? ' · ⚠ Format auffällig' : ''}</small><span>${esc(reading.text)}</span>${(reading.quality_issues || []).length ? `<em>${esc(reading.quality_issues.join('; '))}</em>` : ''}</span>`).join('')}<span class="comparison-help">Anklicken, um den Ausschnitt zu sehen und eine Lesung bewusst zu übernehmen.</span></button>`;
  }
  function renderReaderPage() {
    const page = currentPage();
    if (!page) { $('reader-lines').innerHTML = '<div class="empty-state">Für dieses Dokument wurde kein Text erkannt.</div>'; return; }
    const pageData = pages.find(item => Number(item.page_index) === Number(page.page_index));
    const version = selectedVersion();
    const versionPage = version?.pages?.find(item => Number(item.page_index) === Number(page.page_index));
    $('reader-page-label').textContent = `Seite ${Number(page.page_index) + 1} von ${pages.length}`;
    $('reader-scan-image').src = `${pageData?.image_url || page.image_url}?view=${imageView}`;
    $('reader-version-control').hidden = cloudOnly || textView === 'diplomatic';
    $('next-uncertain').hidden = cloudOnly || textView !== 'diplomatic';
    const lines = cloudOnly ? page.lines.filter(line => cloudReadingsFor(line).length) : reviewOnly ? page.lines.filter(line => line.review_status === 'unsicher') : page.lines;
    $('reader-lines').classList.toggle('cloud-comparison-list', cloudOnly);
    $('reader-lines').innerHTML = cloudOnly
      ? lines.map(cloudComparisonMarkup).join('') || '<div class="empty-state compact">Auf dieser Seite wurden keine Zeilen mit der Cloud gegengeprüft.</div>'
      : textView === 'document'
      ? `${versionNotice(version)}<div class="readable-document">${(version?.pages || transcript.pages).map((item, index) => `<section><small>Seite ${index + 1}</small><p>${esc(item.reading_text || 'Keine Lesefassung verfügbar.')}</p></section>`).join('')}</div>`
      : textView === 'reading'
      ? `${versionNotice(version)}<div class="readable-text">${esc(versionPage?.reading_text || page.reading_text || 'Keine Lesefassung verfügbar.')}</div>`
      : lines.map(line => { const cloud = cloudReadingsFor(line); const suspicious = cloud.some(reading => (reading.quality_issues || []).length); return `<button class="transcript-line ${line.review_status === 'unsicher' ? 'uncertain' : ''} ${cloud.length ? 'cloud-reviewed' : ''}" data-line-id="${esc(line.id)}"><span>${esc(line.text || '‹leer›')}</span><small>${cloud.length ? `<em>${suspicious ? '⚠ ' : ''}Cloud · ${esc(cloudModelLabel(cloud[0].model))}</em>` : ''}${Math.round(Number(line.confidence || 0) * 100)} % · ${esc(line.model)}</small></button>`; }).join('') || `<div class="empty-state compact">${reviewOnly ? 'Auf dieser Seite sind keine unsicheren Zeilen offen.' : 'Keine Zeilen erkannt.'}</div>`;
    $('reader-lines').querySelectorAll('[data-line-id]').forEach(button => button.onclick = () => drawReaderLine(page.lines.find(line => line.id === button.dataset.lineId)));
    $('reader-editor').hidden = true;
    selectedLine = null;
    $('document-detail').querySelectorAll('[data-reader-page]').forEach(button => button.classList.toggle('active', Number(button.dataset.readerPage) === Number(pageIndex)));
    if ($('technical-page-report')) $('technical-page-report').innerHTML = technicalPageMarkup(pageData || {});
  }
  $('reader-scan-image').onload = () => { if (selectedLine) drawReaderLine(selectedLine); };
  $('document-detail').querySelector('.reader-scan-canvas').onclick = event => {
    const image = $('reader-scan-image');
    const page = currentPage();
    if (!page?.lines?.length || !image.naturalWidth) return;
    const rect = image.getBoundingClientRect();
    const x = (event.clientX - rect.left) * image.naturalWidth / rect.width;
    const y = (event.clientY - rect.top) * image.naturalHeight / rect.height;
    const containing = page.lines.find(line => x >= line.bbox[0] && x <= line.bbox[2] && y >= line.bbox[1] && y <= line.bbox[3]);
    const nearest = containing || page.lines.reduce((best, line) => {
      const distance = Math.abs(y - (line.bbox[1] + line.bbox[3]) / 2);
      return !best || distance < best.distance ? { line, distance } : best;
    }, null)?.line;
    if (nearest) drawReaderLine(nearest);
  };
  renderReaderPage();
  $('document-detail').querySelectorAll('[data-reader-page]').forEach(button => button.onclick = () => { pageIndex = Number(button.dataset.readerPage); renderReaderPage(); });
  $('document-detail').querySelectorAll('[data-text-view]').forEach(button => button.onclick = () => { textView = button.dataset.textView; $('document-detail').querySelectorAll('[data-text-view]').forEach(item => item.classList.toggle('active', item === button)); renderReaderPage(); });
  $('reader-version').onchange = () => { selectedVersionId = $('reader-version').value; renderReaderPage(); };
  $('document-detail').querySelectorAll('[data-image-view]').forEach(button => button.onclick = () => { imageView = button.dataset.imageView; $('document-detail').querySelectorAll('[data-image-view]').forEach(item => item.classList.toggle('active', item === button)); renderReaderPage(); });
  $('document-detail').querySelectorAll('[data-reader-tab]').forEach(button => button.onclick = () => {
    $('document-detail').querySelectorAll('[data-reader-tab]').forEach(item => item.classList.toggle('active', item === button));
    if (button.dataset.readerTab === 'details') { $('reader-reading').classList.remove('active'); $('reader-details').classList.add('active'); return; }
    $('reader-details').classList.remove('active'); $('reader-reading').classList.add('active'); reviewOnly = button.dataset.readerTab === 'review'; cloudOnly = button.dataset.readerTab === 'cloud'; textView = 'diplomatic'; renderReaderPage();
  });
  $('document-detail').querySelector('[data-open-cloud-comparison]')?.addEventListener('click', () => $('document-detail').querySelector('[data-reader-tab="cloud"]')?.click());
  $('next-uncertain').onclick = () => {
    const uncertain = transcript.pages.flatMap(page => page.lines.map(line => ({ page: page.page_index, line })).filter(item => item.line.review_status === 'unsicher'));
    if (!uncertain.length) return notify('Keine unsicheren Stellen mehr offen.');
    const current = uncertain.findIndex(item => selectedLine && item.line.id === selectedLine.id);
    const next = uncertain[(current + 1) % uncertain.length]; pageIndex = Number(next.page); renderReaderPage(); setTimeout(() => drawReaderLine(next.line), 30);
  };
  $('reader-save-line').onclick = async () => {
    if (!selectedLine) return;
    await withButtonBusy($('reader-save-line'), 'Wird gespeichert …', async () => {
      const response = await fetch(`/api/lines/${encodeURIComponent(selectedLine.id)}`, { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: $('reader-line-text').value }) });
      if (!response.ok) throw new Error(await responseError(response, 'Korrektur konnte nicht gespeichert werden.'));
      notify('Korrektur bestätigt und Suchindex aktualisiert.'); await showDocument(documentId);
    }).catch(error => notify(error.message, true));
  };
  $('reader-cancel-line').onclick = () => { $('reader-editor').hidden = true; selectedLine = null; };
  $('reader-back').onclick = () => { document.querySelector('.archive-workspace')?.classList.remove('reader-open'); $('document-detail').hidden = true; $('viewer-empty').hidden = false; state.selectedDocumentId = null; };
  const returnToLibrary = async message => {
    document.querySelector('.archive-workspace')?.classList.remove('reader-open');
    $('document-detail').hidden = true; $('viewer-empty').hidden = false; state.selectedDocumentId = null;
    state.archiveFilter = 'all';
    document.querySelectorAll('#archive-navigation button,.collection-list button').forEach(item => item.classList.remove('active'));
    document.querySelector('#archive-navigation [data-filter="all"]')?.classList.add('active');
    await loadDocuments();
    notify(message);
  };
  const trashCurrentDocument = async () => {
    if (!await askConfirmation('Dokument in den Papierkorb?', 'Das Dokument verschwindet aus der Bibliothek. Verwaltete Originale bleiben erhalten, bis du es im Papierkorb endgültig löschst.')) return;
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' });
    if (!response.ok) return notify(await responseError(response, 'Dokument konnte nicht in den Papierkorb verschoben werden.'), true);
    await returnToLibrary('Dokument wurde in den Papierkorb verschoben.');
  };
  const restoreCurrentDocument = async () => {
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/restore`, { method: 'POST' });
    if (!response.ok) return notify(await responseError(response, 'Dokument konnte nicht wiederhergestellt werden.'), true);
    await returnToLibrary('Dokument wurde wiederhergestellt.');
  };
  const purgeCurrentDocument = async () => {
    if (!await askConfirmation('Dokument endgültig löschen?', 'Verwaltete Originale, Transkriptionen, Suchindex und Exporte werden unwiderruflich entfernt.')) return;
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/permanent`, { method: 'DELETE' });
    if (!response.ok) return notify(await responseError(response, 'Dokument konnte nicht endgültig gelöscht werden.'), true);
    await returnToLibrary('Dokument und verwaltete Dateien wurden endgültig gelöscht.');
  };
  if ($('reader-export')) $('reader-export').onclick = () => exportDocument(documentId, $('reader-export'));
  if ($('reader-trash')) $('reader-trash').onclick = trashCurrentDocument;
  if ($('reader-restore')) $('reader-restore').onclick = restoreCurrentDocument;
  if ($('reader-purge')) $('reader-purge').onclick = purgeCurrentDocument;
  if ($('reader-file-document')) $('reader-file-document').onclick = () => { $('document-detail').querySelector('[data-reader-tab="details"]').click(); setTimeout(() => $('document-detail').querySelector('.collection-tree-checks input')?.focus(), 30); };
  if ($('save-document-meta')) $('save-document-meta').onclick = () => saveDocumentMetadata(documentId, documentData.document_status || 'automatisch');
  if ($('detail-reprocess')) $('detail-reprocess').onclick = async () => { const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/reprocess`, { method: 'POST' }); if (!response.ok) return notify(await responseError(response, 'Neuverarbeitung konnte nicht gestartet werden.'), true); const job = await response.json(); state.job = job.id; state.completedJob = null; openTab('read'); watchJob(job.id); };
  if ($('detail-integrity')) $('detail-integrity').onclick = () => verifyLibrary($('detail-integrity'));
  if ($('detail-finder')) $('detail-finder').onclick = async () => { const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/reveal`, { method: 'POST' }); if (!response.ok) notify(await responseError(response, 'Original konnte nicht angezeigt werden.'), true); };
  if ($('detail-trash')) $('detail-trash').onclick = trashCurrentDocument;
  if ($('detail-restore')) $('detail-restore').onclick = restoreCurrentDocument;
  if ($('detail-purge')) $('detail-purge').onclick = purgeCurrentDocument;
}

function metric(value) { return value == null ? '–' : `${Math.round(Number(value) * 100)} %`; }

function technicalPageMarkup(page) {
  const profile = page.profile || {};
  const engines = page.engine_runs || [];
  return `<div class="technical-grid"><div><span>Seite</span><strong>${Number(page.page_index || 0) + 1}</strong></div><div><span>Erkannte Schrift</span><strong>${esc(profile.script || 'unbekannt')}</strong></div><div><span>Layout</span><strong>${esc(profile.layout || 'unbekannt')}</strong></div><div><span>Modell</span><strong>${esc(page.model || 'noch nicht verarbeitet')}</strong></div><div><span>Bildvariante</span><strong>${esc(page.variant || '–')}</strong></div><div><span>Geschätzte CER</span><strong>${page.expected_cer == null ? '–' : `${(Number(page.expected_cer) * 100).toFixed(1).replace('.', ',')} %`}</strong></div><div><span>Helligkeit</span><strong>${metric(page.brightness)}</strong></div><div><span>Kontrast</span><strong>${metric(page.contrast)}</strong></div><div><span>Schärfe</span><strong>${metric(page.sharpness)}</strong></div><div><span>Schieflage</span><strong>${page.skew_degrees == null ? '–' : `${Number(page.skew_degrees).toFixed(2)}°`}</strong></div><div><span>Unsichere Zeilen</span><strong>${page.uncertain_count || 0}</strong></div><div><span>Erkannte Zeilen</span><strong>${page.line_count || 0}</strong></div></div>${(profile.evidence || []).length ? `<p class="technical-note"><strong>Routing:</strong> ${esc(profile.evidence.join(' · '))}</p>` : ''}${(page.warnings || []).length ? `<p class="technical-note warning"><strong>Hinweise:</strong> ${esc(page.warnings.join(' · '))}</p>` : ''}<div class="engine-list">${engines.map(run => `<span><strong>${esc(run.engine)}</strong> · ${esc(run.backend)} · ${Number(run.duration_seconds).toFixed(1)} s${run.success ? '' : ` · ${esc(run.message)}`}</span>`).join('') || '<span>Noch keine Modellläufe gespeichert.</span>'}</div>`;
}

async function saveDocumentMetadata(documentId, status) {
  const button = $('save-document-meta');
  await withButtonBusy(button, 'Wird gespeichert …', async () => {
    const payload = { document_status: $('document-detail').querySelector('[data-document-status]')?.value || status, collection_ids: [...$('document-detail').querySelectorAll('[data-collection-id]:checked')].map(item => item.dataset.collectionId), tags: ($('document-detail').querySelector('[data-tags]')?.value || '').split(',').map(value => value.trim()).filter(Boolean) };
    $('document-detail').querySelectorAll('[data-meta]').forEach(input => {
      payload[input.dataset.meta] = input.type === 'number' ? (input.value ? Number(input.value) : null) : input.value;
    });
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}`, { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
    if (!response.ok) throw new Error(await responseError(response, 'Metadaten konnten nicht gespeichert werden.'));
    notify('Archivangaben gespeichert.');
    await loadDocuments();
    await showDocument(documentId);
  }).catch(error => notify(error.message, true));
}

async function exportDocument(documentId, button) {
  await withButtonBusy(button, 'Exporte werden erzeugt …', async () => {
    const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/export`, { method: 'POST' });
    if (!response.ok) throw new Error(await responseError(response, 'Export fehlgeschlagen.'));
    const data = await response.json(); renderExports(data.downloads || []); notify('Aktuelle Exporte wurden erzeugt.');
  }).catch(error => notify(error.message, true));
}

function renderMigrationNotice() {
  const notice = $('migration-notice');
  const pending = state.migration?.pending || 0;
  notice.hidden = !pending;
  if (pending) $('migration-copy').textContent = `${pending} Dokument${pending === 1 ? '' : 'e'} liegen noch außerhalb der verwalteten Bibliothek.`;
}

$('open-migration').onclick = () => {
  const migration = state.migration;
  if (!migration) return;
  $('migration-size').textContent = `${(Number(migration.bytes || 0) / 1024 / 1024).toFixed(1).replace('.', ',')} MB`;
  $('migration-list').innerHTML = migration.documents.filter(item => !item.managed).map(item => `<div class="migration-item"><input class="migration-select" type="checkbox" value="${item.id}" checked><span><strong>${esc(item.title)}</strong><small>${item.reachable}/${item.sources} Quellen erreichbar${item.output_available ? '' : ' · Ausgaben fehlen'}${item.grouping_review ? ' · Gruppierung prüfen' : ''}</small>${item.grouping_review ? `<label class="split-option"><input type="checkbox" data-split-id="${item.id}" checked> Lose Bilder als einzelne Dokumente übernehmen</label>` : ''}</span></div>`).join('');
  $('migration-dialog').showModal();
};

$('migration-dialog').addEventListener('close', async () => {
  if ($('migration-dialog').returnValue !== 'confirm') return;
  const ids = [...$('migration-list').querySelectorAll('.migration-select:checked')].map(input => input.value);
  const splitIds = [...$('migration-list').querySelectorAll('[data-split-id]:checked')].map(input => input.dataset.splitId).filter(id => ids.includes(id));
  setInlineStatus($('search-status'), 'Originale werden kopiert und geprüft …', { busy: true });
  const response = await fetch('/api/library/migrate', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ document_ids: ids, split_document_ids: splitIds }) });
  const data = await response.json();
  if (!response.ok || data.errors?.length) notify(`${data.migrated?.length || 0} übernommen, ${data.errors?.length || 0} mit Problemen.`, Boolean(data.errors?.length));
  else notify(`${data.migrated.length} Dokumente sicher übernommen.`);
  await loadDocuments();
});

function openCollectionDialog(collectionId = null) {
  state.editCollectionId = collectionId;
  const collection = state.collections.find(item => item.id === collectionId);
  $('collection-dialog-title').textContent = collection ? 'Sammlung bearbeiten' : 'Neue Sammlung';
  $('save-collection').textContent = collection ? 'Speichern' : 'Anlegen';
  $('delete-collection').hidden = !collection;
  $('collection-name').value = collection?.name || ''; $('collection-description').value = collection?.description || '';
  $('collection-parent').innerHTML = '<option value="">Bibliothek (oberste Ebene)</option>' + state.collections.filter(item => item.id !== collectionId && !collectionDescendants(collectionId || '').includes(item.id)).map(item => `<option value="${esc(item.id)}">${esc(item.path)}</option>`).join('');
  $('collection-parent').value = collection?.parent_id || '';
  $('collection-dialog').showModal(); setTimeout(() => $('collection-name').focus(), 50);
}
$('new-collection').onclick = () => openCollectionDialog();
$('collection-dialog').addEventListener('close', async () => {
  if ($('collection-dialog').returnValue !== 'confirm' || !$('collection-name').value.trim()) return;
  const editing = state.editCollectionId;
  const response = await fetch(editing ? `/api/collections/${encodeURIComponent(editing)}` : '/api/collections', { method: editing ? 'PATCH' : 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: $('collection-name').value.trim(), description: $('collection-description').value.trim(), parent_id: $('collection-parent').value || null, update_parent: true }) });
  if (!response.ok) return notify(await responseError(response, 'Sammlung konnte nicht angelegt werden.'), true);
  notify(editing ? 'Sammlung gespeichert.' : 'Sammlung angelegt.'); state.editCollectionId = null; await loadDocuments();
});
$('delete-collection').onclick = async () => {
  const collectionId = state.editCollectionId;
  if (!collectionId || !await askConfirmation('Sammlung löschen?', 'Dokumente werden nicht gelöscht. Unterordner rücken eine Ebene nach oben; unzugeordnete Dokumente erscheinen im Eingang.')) return;
  const response = await fetch(`/api/collections/${encodeURIComponent(collectionId)}`, { method: 'DELETE' });
  if (!response.ok) return notify(await responseError(response, 'Sammlung konnte nicht gelöscht werden.'), true);
  $('collection-dialog').close('cancel'); state.editCollectionId = null; notify('Sammlung gelöscht; Dokumente bleiben erhalten.'); await loadDocuments();
};

async function verifyLibrary(button = $('verify-library')) {
  await withButtonBusy(button, 'Prüfsummen laufen …', async () => {
    const response = await fetch('/api/library/integrity', { method: 'POST' });
    if (!response.ok) throw new Error(await responseError(response, 'Bibliothek konnte nicht geprüft werden.'));
    const data = await response.json();
    notify(data.problems.length ? `${data.problems.length} Dateiproblem${data.problems.length === 1 ? '' : 'e'} gefunden.` : `${data.files} Dateien erfolgreich geprüft.`, Boolean(data.problems.length));
    await loadDocuments();
  }).catch(error => notify(error.message, true));
}
$('verify-library').onclick = () => verifyLibrary();

async function runSearch() {
  const text = $('query').value.trim();
  if (!text) {
    setInlineStatus($('search-status'), 'Bitte einen Suchbegriff eingeben.', { error: true });
    $('query').focus();
    return;
  }
  if ($('search-button').getAttribute('aria-busy') === 'true') return;
  $('document-browser').hidden = true;
  $('search-results-view').hidden = false;
  $('show-library').classList.remove('active');
  const request = ++state.searchRequest;
  const started = performance.now();
  setButtonBusy($('search-button'), true, 'Suche läuft …');
  setInlineStatus($('search-status'), 'Lokales Archiv wird durchsucht …', { busy: true });
  $('result-count').textContent = '…';
  $('empty-results').hidden = true;
  $('results').innerHTML = `<tr class="loading-row"><td colspan="4">${loadingMarkup('Volltext, ähnliche Lesarten und Bedeutung werden verglichen …', true)}</td></tr>`;
  try {
    const response = await fetch('/api/search', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({
        text,
        mode: $('search-mode').value,
        fuzziness: Number($('fuzziness').value) / 100,
        year_from: $('year-from').value ? Number($('year-from').value) : null,
        year_to: $('year-to').value ? Number($('year-to').value) : null,
        limit: 100
      })
    });
    if (!response.ok) throw new Error(await responseError(response, 'Suche fehlgeschlagen.'));
    const hits = await response.json();
    if (request !== state.searchRequest) return;
    renderHits(hits, `Treffer für „${text}“`, `Keine Treffer für „${text}“. Versuche die Namenssuche oder eine geringere Ähnlichkeit.`);
    setInlineStatus($('search-status'), `${hits.length} ${hits.length === 1 ? 'Treffer' : 'Treffer'} in ${elapsedLabel(started)}`);
  } catch (error) {
    if (request !== state.searchRequest) return;
    renderHits([], `Treffer für „${text}“`, 'Die Suche konnte nicht ausgeführt werden. Bitte erneut versuchen.');
    setInlineStatus($('search-status'), error.message, { error: true });
    notify(error.message, true);
  } finally {
    if (request === state.searchRequest) setButtonBusy($('search-button'), false);
  }
}

function renderHits(hits, title, emptyMessage = 'Keine passenden Fundstellen gefunden.') {
  state.hits = hits;
  $('results-title').textContent = title;
  $('result-count').textContent = String(hits.length);
  $('empty-results').hidden = Boolean(hits.length);
  $('empty-results').textContent = emptyMessage;
  $('results').innerHTML = hits.map((hit, index) =>
    `<tr data-index="${index}" tabindex="0"><td>${esc(hit.document_title)}</td><td>${Number(hit.page_index) + 1}${hit.year ? `<br><small>${hit.year}</small>` : ''}</td><td>${esc(hit.matched_form || hit.text)}${hit.matched_form && hit.matched_form !== hit.text ? `<br><small>Aktuelle Fassung: ${esc(hit.text)}</small>` : ''}</td><td>${esc(hit.reason)}</td></tr>`
  ).join('');
  $('results').querySelectorAll('tr').forEach(row => {
    row.onclick = () => {
      $('results').querySelectorAll('tr').forEach(item => item.classList.remove('selected'));
      row.classList.add('selected');
      const hit = hits[Number(row.dataset.index)];
      if (hit.line_id) showHit(hit); else showDocument(hit.document_id);
    };
    row.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); row.click(); } };
  });
}

$('search-button').onclick = runSearch;
$('query').onkeydown = event => { if (event.key === 'Enter') runSearch(); };
$('fuzziness').oninput = () => { $('fuzziness-value').textContent = `${$('fuzziness').value} %`; };
$('review-queue').onclick = async () => {
  const button = $('review-queue');
  if (button.getAttribute('aria-busy') === 'true') return;
  $('document-browser').hidden = true;
  $('search-results-view').hidden = false;
  $('show-library').classList.remove('active');
  const request = ++state.searchRequest;
  const started = performance.now();
  setButtonBusy(button, true, 'Prüfliste wird geladen …');
  setInlineStatus($('search-status'), 'Unsichere Modell-Lesungen werden sortiert …', { busy: true });
  $('result-count').textContent = '…';
  $('empty-results').hidden = true;
  $('results').innerHTML = `<tr class="loading-row"><td colspan="4">${loadingMarkup('Unsichere Stellen werden vorbereitet …', true)}</td></tr>`;
  try {
    const response = await fetch('/api/review-queue?limit=200');
    if (!response.ok) throw new Error(await responseError(response, 'Prüfliste konnte nicht geladen werden.'));
    const hits = await response.json();
    if (request !== state.searchRequest) return;
    renderHits(hits, 'Unsichere Stellen — niedrigste Sicherheit zuerst', 'Keine unsicheren Stellen vorhanden. Das Archiv ist vollständig geprüft.');
    setInlineStatus($('search-status'), `${hits.length} ${hits.length === 1 ? 'Stelle' : 'Stellen'} in ${elapsedLabel(started)}`);
  } catch (error) {
    if (request !== state.searchRequest) return;
    renderHits([], 'Unsichere Stellen', 'Die Prüfliste konnte nicht geladen werden.');
    setInlineStatus($('search-status'), error.message, { error: true });
    notify(error.message, true);
  } finally {
    if (request === state.searchRequest) setButtonBusy(button, false);
  }
};

async function showHit(hit) {
  const request = ++state.hitRequest;
  state.selected = hit;
  $('viewer-empty').hidden = true;
  $('document-detail').hidden = true;
  $('viewer-content').hidden = false;
  setInlineStatus($('scan-status'), 'Scan-Ausschnitt wird geladen …', { busy: true });
  $('scan-wrap').setAttribute('aria-busy', 'true');
  $('scan').onload = () => {
    if (request !== state.hitRequest) return;
    setInlineStatus($('scan-status'), '');
    $('scan-wrap').removeAttribute('aria-busy');
    drawBox(hit.bbox);
  };
  $('scan').onerror = () => {
    if (request !== state.hitRequest) return;
    setInlineStatus($('scan-status'), 'Scan konnte nicht geladen werden.', { error: true });
    $('scan-wrap').removeAttribute('aria-busy');
  };
  $('scan').src = hit.image_url;
  $('hit-title').textContent = `${hit.document_title} · Seite ${Number(hit.page_index) + 1}`;
  $('hit-confidence').textContent = `${Math.round(Number(hit.confidence) * 100)} % Sicherheit`;
  $('correction').value = hit.text;
  $('correction-status').textContent = '';
  $('readings').setAttribute('aria-busy', 'true');
  $('readings').innerHTML = loadingMarkup('Modell-Lesungen werden geladen …', true);
  let details;
  try {
    const response = await fetch(`/api/lines/${hit.line_id}`);
    if (!response.ok) throw new Error(await responseError(response, 'Modell-Lesungen konnten nicht geladen werden.'));
    details = await response.json();
  } catch (error) {
    if (request === state.hitRequest) $('readings').innerHTML = `<div class="loading-state compact is-error">${esc(error.message)}</div>`;
    return;
  } finally {
    if (request === state.hitRequest) $('readings').removeAttribute('aria-busy');
  }
  if (request !== state.hitRequest) return;
  const unique = [];
  const seen = new Set();
  details.readings.forEach(reading => {
    const key = `${reading.model}\n${reading.text}`;
    if (!seen.has(key)) { seen.add(key); unique.push(reading); }
  });
  const successfulRuns = (details.engine_runs || []).filter(run => run.success);
  const runSummary = successfulRuns.length
    ? `<small class="engine-summary">Seitenläufe: ${successfulRuns.map(run => `${esc(run.engine)} · ${esc(run.backend)} · ${Number(run.duration_seconds).toFixed(1)} s`).join(' / ')}</small>` : '';
  $('readings').innerHTML = (unique.length > 1 ? '<strong>Andere Modell-Lesungen</strong>' + unique.map((reading, index) =>
    `<button class="reading" data-reading="${index}"><span>${esc(reading.text)}<small>${esc(reading.model)} · ${esc(reading.kind)}</small></span><span>${Math.round(Number(reading.confidence) * 100)} %</span></button>`
  ).join('') : '') + runSummary;
  $('readings').querySelectorAll('[data-reading]').forEach(button => {
    button.onclick = () => { $('correction').value = unique[Number(button.dataset.reading)].text; };
  });
}

function drawBox(box) {
  const image = $('scan');
  const canvas = $('overlay');
  const scaleX = image.clientWidth / image.naturalWidth;
  const scaleY = image.clientHeight / image.naturalHeight;
  canvas.width = image.clientWidth;
  canvas.height = image.clientHeight;
  canvas.style.width = `${image.clientWidth}px`;
  canvas.style.height = `${image.clientHeight}px`;
  const context = canvas.getContext('2d');
  context.fillStyle = 'rgba(222, 150, 38, .17)';
  context.strokeStyle = '#d58b1d';
  context.lineWidth = 3;
  const x = box[0] * scaleX;
  const y = box[1] * scaleY;
  const width = (box[2] - box[0]) * scaleX;
  const height = (box[3] - box[1]) * scaleY;
  context.fillRect(x, y, width, height);
  context.strokeRect(x, y, width, height);
  requestAnimationFrame(() => image.parentElement.scrollTo({ top: Math.max(0, y - 80), behavior: 'smooth' }));
}

$('save-correction').onclick = async () => {
  if (!state.selected) return;
  await withButtonBusy($('save-correction'), 'Wird gespeichert …', async () => {
    setInlineStatus($('correction-status'), 'Korrektur und Suchindex werden aktualisiert …', { busy: true });
    try {
      const response = await fetch(`/api/lines/${state.selected.line_id}`, {
        method: 'PATCH', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text: $('correction').value })
      });
      if (!response.ok) throw new Error(await responseError(response, 'Speichern fehlgeschlagen.'));
      setInlineStatus($('correction-status'), 'Bestätigt und im Suchindex aktualisiert.');
    } catch (error) {
      setInlineStatus($('correction-status'), error.message, { error: true });
    }
  });
};

$('export-current').onclick = async () => {
  if (!state.selected) return;
  await withButtonBusy($('export-current'), 'Export läuft …', async () => {
    setInlineStatus($('correction-status'), 'PDF, DOCX, Text und Austauschformate werden aktualisiert …', { busy: true });
    try {
      const response = await fetch(`/api/documents/${state.selected.document_id}/export`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Export fehlgeschlagen.');
      $('correction-status').classList.remove('is-error');
      $('correction-status').removeAttribute('aria-busy');
      $('correction-status').innerHTML = 'Aktuelle Fassung exportiert: ' + data.downloads.map(download =>
        `<a href="/api/output/${encodeURIComponent(download.id)}">${esc(download.name)}</a>`
      ).join(' · ');
    } catch (error) {
      setInlineStatus($('correction-status'), error.message, { error: true });
    }
  });
};

async function loadCloudModels() {
  $('cloud-catalog').setAttribute('aria-busy', 'true');
  $('cloud-catalog').innerHTML = loadingMarkup('Kuratiertes Modellangebot wird geladen …', true);
  $('cloud-model').disabled = true;
  $('setting-cloud-model').disabled = true;
  $('cloud-model').innerHTML = '<option>Wird geladen …</option>';
  $('setting-cloud-model').innerHTML = '<option>Wird geladen …</option>';
  try {
    const response = await fetch('/api/cloud-models');
    if (!response.ok) throw new Error(await responseError(response, 'Cloud-Modellauswahl konnte nicht geladen werden.'));
    state.cloudModels = await response.json();
  } catch (error) {
    $('cloud-catalog').innerHTML = `<div class="loading-state compact is-error">${esc(error.message)}</div>`;
    $('cloud-model').innerHTML = '<option>Nicht verfügbar</option>';
    $('setting-cloud-model').innerHTML = '<option>Nicht verfügbar</option>';
    return;
  } finally {
    $('cloud-catalog').removeAttribute('aria-busy');
  }
  $('cloud-model').innerHTML = state.cloudModels.map(option =>
    `<option value="${esc(option.key)}">${esc(option.label)} — ${esc(option.model)}</option>`
  ).join('');
  $('setting-cloud-model').innerHTML = state.cloudModels.map(option =>
    `<option value="${esc(option.key)}">${esc(option.label)}</option>`
  ).join('');
  $('job-cloud-model').innerHTML = state.cloudModels.map(option =>
    `<option value="${esc(option.key)}">${esc(option.label)}</option>`
  ).join('');
  $('cloud-model').disabled = false;
  $('setting-cloud-model').disabled = false;
  if (state.settings?.openrouter_profile) {
    $('cloud-model').value = state.settings.openrouter_profile;
    $('setting-cloud-model').value = state.settings.openrouter_profile;
    $('job-cloud-model').value = state.settings.openrouter_profile;
  }
  renderCloudHelp();
  $('cloud-catalog').innerHTML = state.cloudModels.map(option =>
    `<div class="cloud-model-card ${option.recommended ? 'recommended' : ''}"><strong>${esc(option.label)}</strong><code>${esc(option.model)}</code><small>${esc(option.best_for)} · ${option.zdr ? 'ZDR verfügbar' : 'kein ZDR'}${option.experimental ? ' · experimentell' : ''}</small></div>`
  ).join('');
}

function renderCloudHelp() {
  const option = state.cloudModels.find(model => model.key === $('cloud-model').value);
  $('cloud-model-help').textContent = option ? `${option.model} · ${option.description} ${option.price_hint}.` : '';
  $('cloud-privacy').textContent = option
    ? `${option.zdr ? 'ZDR wird verlangt' : 'Kein ZDR-Endpunkt verfügbar'} · Datensammlung wird abgelehnt${option.experimental ? ' · experimentelles Profil' : ''}`
    : 'Datenschutzprofil nicht verfügbar';
}
$('cloud-model').onchange = renderCloudHelp;

$('cloud-review').onclick = async () => {
  if (!state.selected) return;
  const option = state.cloudModels.find(model => model.key === $('cloud-model').value);
  const budget = Number($('cloud-budget').value);
  const approved = await askConfirmation(
    'Cloud-Zweitprüfung starten?',
    `Nur dieser markierte Ausschnitt wird an ${option?.label || 'OpenRouter'} (${option?.model || 'Modell'}) gesendet. Kostenlimit: ${budget.toFixed(2)} $.`
  );
  if (!approved) return;
  await withButtonBusy($('cloud-review'), 'Cloud prüft …', async () => {
    setInlineStatus($('correction-status'), 'OpenRouter prüft den ausgewählten Ausschnitt …', { busy: true });
    try {
      const response = await fetch(`/api/lines/${state.selected.line_id}/cloud-review`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ budget_usd: budget, profile: $('cloud-model').value })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Prüfung fehlgeschlagen.');
      $('correction').value = data.text;
      setInlineStatus($('correction-status'), `Unbestätigter Vorschlag von ${data.model} · $${Number(data.cost_usd).toFixed(4)}. Zum Übernehmen bitte bestätigen.`);
    } catch (error) {
      setInlineStatus($('correction-status'), error.message, { error: true });
    }
  });
};

async function loadModels() {
  const list = $('model-list');
  list.setAttribute('aria-busy', 'true');
  if (!list.querySelector('.model')) list.innerHTML = loadingMarkup('Lokale Modelle werden geprüft …');
  let rows;
  try {
    const response = await fetch('/api/models');
    if (!response.ok) throw new Error(await responseError(response, 'Modellbibliothek konnte nicht geladen werden.'));
    rows = await response.json();
  } catch (error) {
    list.innerHTML = `<div class="loading-state is-error">${esc(error.message)}</div>`;
    return;
  } finally {
    list.removeAttribute('aria-busy');
  }
  $('model-list').innerHTML = rows.map(model =>
    `<div class="model"><div><strong>${esc(model.name)}</strong><div class="muted">${esc(model.license)}</div></div><div>${esc(model.purpose)}</div><div>${model.estimated_size_mb ? `${model.estimated_size_mb} MB` : ''}</div><button data-key="${model.key}" ${model.installed ? 'disabled' : ''}>${model.installed ? 'Installiert' : 'Installieren'}</button></div>`
  ).join('');
  $('model-list').querySelectorAll('.model button:not([disabled])').forEach(button => {
    button.onclick = () => installModel(button.dataset.key, button);
  });
}

async function installModel(key, button) {
  let accept = true;
  if (key === 'churro-mlx-8bit') {
    accept = await askConfirmation(
      'CHURRO lokal installieren?',
      'CHURRO benötigt etwa 4,4 GB und steht unter der Qwen Research License. Die Installation ist für deine Forschungs-/nichtkommerzielle Nutzung vorgesehen.'
    );
  }
  if (!accept) return;
  setButtonBusy(button, true, 'Wird gestartet …');
  try {
    const response = await fetch(`/api/models/${key}/install`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ accept_license: accept })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Installation fehlgeschlagen');
    setButtonBusy(button, true, 'Installation läuft …');
    watchModelInstall(data.id, button);
  } catch (error) {
    setButtonBusy(button, false);
    notify(error.message, true);
  }
}

async function watchModelInstall(id, button = null) {
  const box = $('model-install-status');
  box.hidden = false;
  box.classList.remove('is-error');
  box.setAttribute('aria-busy', 'true');
  box.innerHTML = '<span class="spinner" aria-hidden="true"></span> Installation wird vorbereitet …';
  const tick = async () => {
    try {
      const response = await fetch(`/api/model-installs/${id}`);
      if (!response.ok) throw new Error(await responseError(response, 'Installationsstatus nicht erreichbar.'));
      const data = await response.json();
      box.innerHTML = `${data.status === 'läuft' ? '<span class="spinner" aria-hidden="true"></span> ' : ''}${esc(data.message)} · ${esc(duration(data.elapsed_seconds))}`;
      if (data.status === 'läuft') {
        setTimeout(tick, 1000);
      } else {
        box.removeAttribute('aria-busy');
        setButtonBusy(button, false);
        await loadModels();
      }
    } catch (error) {
      box.removeAttribute('aria-busy');
      box.classList.add('is-error');
      box.textContent = error.message;
      setButtonBusy(button, false);
    }
  };
  tick();
}

$('save-openrouter-key').onclick = async () => {
  const key = $('openrouter-key').value.trim();
  if (!key) { notify('Bitte zuerst einen API-Schlüssel einfügen.', true); return; }
  await withButtonBusy($('save-openrouter-key'), 'Schlüssel wird geprüft …', async () => {
    setInlineStatus($('key-status'), 'Schlüssel wird bei OpenRouter geprüft …', { busy: true });
    try {
      const response = await fetch('/api/openrouter-key', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ key, validate: true })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Prüfung fehlgeschlagen.');
      setInlineStatus($('key-status'), `Sicher gespeichert und bestätigt${data.label ? ` · ${data.label}` : ''}.`);
      $('openrouter-key').value = '';
      loadSystemStatus();
    } catch (error) {
      setInlineStatus($('key-status'), error.message, { error: true });
      notify(error.message, true);
    }
  });
};

$('validate-openrouter-key').onclick = () => loadKeyStatus(true, $('validate-openrouter-key'));
$('delete-openrouter-key').onclick = async () => {
  const approved = await askConfirmation('API-Schlüssel entfernen?', 'Der OpenRouter-Schlüssel wird aus dem macOS-Schlüsselbund gelöscht.');
  if (!approved) return;
  await withButtonBusy($('delete-openrouter-key'), 'Wird entfernt …', async () => {
    setInlineStatus($('key-status'), 'Schlüssel wird aus dem macOS-Schlüsselbund entfernt …', { busy: true });
    try {
      const response = await fetch('/api/openrouter-key', { method: 'DELETE' });
      if (!response.ok) throw new Error(await responseError(response, 'Entfernen fehlgeschlagen.'));
      setInlineStatus($('key-status'), 'Kein OpenRouter-Schlüssel gespeichert.');
      loadSystemStatus();
    } catch (error) {
      setInlineStatus($('key-status'), error.message, { error: true });
    }
  });
};

function setScriptCombobox(value) {
  const root = $('script-combobox');
  const option = root.querySelector(`[role="option"][data-value="${value}"]`);
  if (!option) return;
  $('script').value = value;
  root.querySelector('.combobox-trigger span').textContent = option.childNodes[0].textContent.trim();
  root.querySelectorAll('[role="option"]').forEach(item => item.setAttribute('aria-selected', String(item === option)));
}

async function loadSettings() {
  setInlineStatus($('settings-status'), 'Einstellungen werden geladen …', { busy: true });
  let settings;
  try {
    const response = await fetch('/api/settings');
    if (!response.ok) throw new Error(await responseError(response, 'Einstellungen konnten nicht geladen werden.'));
    settings = await response.json();
  } catch (error) {
    setInlineStatus($('settings-status'), error.message, { error: true });
    notify(error.message, true);
    return;
  }
  state.settings = settings;
  $('setting-quality').value = settings.default_quality;
  $('setting-script').value = settings.default_script;
  $('setting-output').value = settings.output_dir || '';
  $('setting-tesseract').value = settings.tesseract_command;
  $('setting-budget').value = settings.cloud_budget_usd;
  $('setting-advanced').checked = settings.advanced_models;
  $('setting-semantic').checked = settings.semantic_search;
  $('setting-preprocessing').checked = settings.show_preprocessing;
  if (state.cloudModels.length) $('setting-cloud-model').value = settings.openrouter_profile;

  const quality = document.querySelector(`input[name="quality"][value="${settings.default_quality}"]`);
  if (quality && !state.job) {
    quality.checked = true;
    quality.dispatchEvent(new Event('change', { bubbles: true }));
    setScriptCombobox(settings.default_script);
  }
  $('cloud-budget').value = [...$('cloud-budget').options].some(item => Number(item.value) === Number(settings.cloud_budget_usd))
    ? String(settings.cloud_budget_usd)
    : $('cloud-budget').value;
  setInlineStatus($('settings-status'), 'Einstellungen geladen.');
}

$('save-settings').onclick = async () => {
  const payload = {
    advanced_models: $('setting-advanced').checked,
    semantic_search: $('setting-semantic').checked,
    cloud_budget_usd: Number($('setting-budget').value || 0),
    output_dir: $('setting-output').value.trim() || null,
    output_token: state.outputToken,
    tesseract_command: $('setting-tesseract').value.trim() || 'tesseract',
    default_quality: $('setting-quality').value,
    default_script: $('setting-script').value,
    openrouter_profile: $('setting-cloud-model').value,
    show_preprocessing: $('setting-preprocessing').checked
  };
  await withButtonBusy($('save-settings'), 'Wird gespeichert …', async () => {
    setInlineStatus($('settings-status'), 'Einstellungen werden sicher gespeichert …', { busy: true });
    try {
      const response = await fetch('/api/settings', {
        method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Speichern fehlgeschlagen.');
      state.settings = data;
      state.outputToken = null;
      setInlineStatus($('settings-status'), 'Gespeichert. Gilt für neue Aufträge.');
      setScriptCombobox(data.default_script);
      notify('Einstellungen wurden gespeichert.');
      loadSystemStatus();
    } catch (error) {
      setInlineStatus($('settings-status'), error.message, { error: true });
      notify(error.message, true);
    }
  });
};

$('pick-output').onclick = async () => {
  await withButtonBusy($('pick-output'), 'Ordnerdialog geöffnet …', async () => {
    setInlineStatus($('settings-status'), 'Ausgabeordner wird ausgewählt …', { busy: true });
    try {
      const response = await fetch('/api/settings/output-folder', { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Ordnerauswahl fehlgeschlagen.');
      if (data.path) {
        $('setting-output').value = data.path;
        state.outputToken = data.token;
        setInlineStatus($('settings-status'), 'Ordner ausgewählt. Zum Übernehmen Einstellungen speichern.');
      } else {
        setInlineStatus($('settings-status'), 'Ordnerauswahl abgebrochen.');
      }
    } catch (error) {
      setInlineStatus($('settings-status'), error.message, { error: true });
    }
  });
};

async function loadKeyStatus(validate = false, button = null) {
  setButtonBusy(button, true, validate ? 'Verbindung wird geprüft …' : 'Wird geprüft …');
  setInlineStatus($('key-status'), validate ? 'Verbindung zu OpenRouter wird geprüft …' : 'Schlüsselbund wird geprüft …', { busy: true });
  try {
    const response = await fetch(`/api/openrouter-key?validate=${validate}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Prüfung fehlgeschlagen.');
    setInlineStatus($('key-status'), data.configured
      ? data.validated
        ? `Schlüssel bestätigt${data.label ? ` · ${data.label}` : ''}${data.limit_remaining != null ? ` · Restlimit $${Number(data.limit_remaining).toFixed(2)}` : ''}.`
        : 'API-Schlüssel ist sicher im macOS-Schlüsselbund gespeichert.'
      : 'Kein OpenRouter-Schlüssel gespeichert.');
  } catch (error) {
    setInlineStatus($('key-status'), error.message, { error: true });
  } finally {
    setButtonBusy(button, false);
  }
}

async function loadSystemStatus(button = null) {
  const region = $('system-status');
  setButtonBusy(button, true, 'Wird geprüft …');
  region.setAttribute('aria-busy', 'true');
  region.innerHTML = loadingMarkup('Lokale Komponenten werden geprüft …', true);
  let data;
  try {
    const response = await fetch('/api/system');
    if (!response.ok) throw new Error(await responseError(response, 'Systemstatus konnte nicht geladen werden.'));
    data = await response.json();
  } catch (error) {
    region.innerHTML = `<div class="loading-state compact is-error">${esc(error.message)}</div>`;
    $('local-badge').classList.remove('is-loading');
    $('local-badge').classList.add('is-error');
    $('local-badge').innerHTML = '<span></span> Lokaler Dienst nicht erreichbar';
    return;
  } finally {
    region.removeAttribute('aria-busy');
    setButtonBusy(button, false);
  }
  $('local-badge').classList.remove('is-loading', 'is-error');
  $('local-badge').innerHTML = '<span></span> Lokal bereit · ' + esc(data.models_installed) + '/' + esc(data.models_total) + ' Modelle';
  const rows = [
    ['Archiv', `${data.documents} Dokumente · ${data.pages} Seiten · ${data.lines} Zeilen`],
    ['Lokale Modelle', `${data.models_installed} von ${data.models_total} installiert`],
    ['Tesseract', data.tesseract_available ? `bereit · ${data.tesseract_path || 'automatisch gefunden'}` : 'nicht gefunden', !data.tesseract_available],
    ['OpenRouter', data.openrouter_configured ? 'Schlüssel gespeichert' : 'nicht eingerichtet', !data.openrouter_configured],
    ['Bestätigte Referenz', `${data.ground_truth?.verified_lines || 0} Zeilen in ${data.ground_truth?.documents || 0} Dokumenten`],
    ['Cloud-Nutzung', `${data.cloud_usage?.requests || 0} Prüfungen · $${Number(data.cloud_usage?.cost_usd || 0).toFixed(4)}`],
    ['Bibliothek', `${data.library || data.output || 'lokal'}${data.library_pending ? ` · ${data.library_pending} noch zu übernehmen` : ' · vollständig verwaltet'}`],
    ['Ausgabe', data.output],
    ['Datenbank', data.database],
    ['Version', data.version]
  ];
  $('system-status').innerHTML = rows.map(([label, value, warning]) =>
    `<div class="system-row"><span>${esc(label)}</span><strong title="${esc(value)}">${warning !== undefined ? `<i class="status-dot ${warning ? 'warn' : ''}"></i>` : ''}${esc(value)}</strong></div>`
  ).join('');
}
$('refresh-system').onclick = () => loadSystemStatus($('refresh-system'));

$('guide-file').onclick = () => { $('first-run-guide').hidden = true; $('choose-files').click(); };
$('guide-folder').onclick = () => { $('first-run-guide').hidden = true; $('folder').click(); };
$('guide-library').onclick = () => { $('first-run-guide').hidden = true; openTab('search'); };
$('dismiss-guide').onclick = () => { $('first-run-guide').hidden = true; localStorage.setItem('schriftlotse-guide-dismissed', '1'); };

async function initialize() {
  renderSources();
  await Promise.all([loadRecovery(), loadDocuments(), loadSettings(), loadCloudModels(), loadSystemStatus()]);
  $('first-run-guide').hidden = Boolean(state.documents.length || localStorage.getItem('schriftlotse-guide-dismissed'));
  const initialTab = location.hash.slice(1);
  if (['read', 'search', 'models', 'settings'].includes(initialTab)) openTab(initialTab);
}
initialize();
