const state = {
  sources: [], job: null, hits: [], selected: null, cloudModels: [], settings: null,
  outputToken: null, searchRequest: 0, hitRequest: 0, previewRequest: 0, jobEvents: null,
  importDocuments: [], documentMetadata: {}
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

function renderSources() {
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
  loadImportPreview();
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
    const series = data.series_suggestions?.length && !$('group-images').checked
      ? ` · ${data.series_suggestions.length} mögliche Bildserie${data.series_suggestions.length === 1 ? '' : 'n'}` : '';
    preview.classList.remove('is-error');
    preview.innerHTML = `<span>${data.document_count} Dokument${data.document_count === 1 ? '' : 'e'} · ${data.page_count} Seite${data.page_count === 1 ? '' : 'n'}${esc(series)}</span><button type="button" id="review-import" class="text-button">Prüfen & Metadaten</button>`;
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
    return `<tr data-document-id="${esc(document.id)}"><td><input class="doc-title" value="${esc(saved.title ?? document.title)}" aria-label="Titel ${esc(document.title)}"><small>${document.pages} Seite${document.pages === 1 ? '' : 'n'} · ${esc(document.files.join(', '))}</small></td><td><input class="doc-year" type="number" min="800" max="2100" value="${esc(saved.year ?? '')}" placeholder="auto" aria-label="Jahr ${esc(document.title)}"></td><td><select class="doc-script" aria-label="Schrift ${esc(document.title)}"><option value="auto">Automatisch</option><option value="handschrift">Handschrift</option><option value="druck">Druck</option><option value="schreibmaschine">Schreibmaschine</option></select></td></tr>`;
  }).join('');
  rows.forEach(document => {
    const row = [...$('import-documents').querySelectorAll('tr')].find(item => item.dataset.documentId === document.id);
    if (row) row.querySelector('.doc-script').value = state.documentMetadata[document.id]?.script_hint || 'auto';
  });
  $('import-dialog').showModal();
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

$('choose-files').onclick = () => $('files').click();
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
  await withButtonBusy($('folder'), 'Ordnerdialog geöffnet …', async () => {
    setInlineStatus($('drop-status'), 'Ordner wird ausgewählt und geprüft …', { busy: true });
    try {
      const response = await fetch('/api/folder', { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Ordnerauswahl fehlgeschlagen.');
      if (data.source && !state.sources.some(item => item.id === data.source.id)) {
        state.sources.push(data.source);
        renderSources();
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
    document_metadata: state.documentMetadata
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

function watchJob(id) {
  if (state.jobEvents) state.jobEvents.close();
  const events = new EventSource(`/api/jobs/${id}/events`);
  state.jobEvents = events;
  $('status').classList.add('is-loading');
  $('status').setAttribute('aria-busy', 'true');
  events.onmessage = event => {
    const job = JSON.parse(event.data);
    $('status-message').textContent = job.message;
    const eta = job.estimated_remaining_seconds == null ? '' : ` · ca. ${duration(job.estimated_remaining_seconds)} verbleibend`;
    $('status-meta').textContent = `${job.percent} % · ${duration(job.elapsed_seconds)}${eta}`;
    $('progress').value = job.percent;
    $('job-log').textContent = (job.history || []).join('\n');
    $('status').classList.toggle('failed', job.status === 'fehlgeschlagen');
    if (['fertig', 'fehlgeschlagen', 'abgebrochen'].includes(job.status)) {
      events.close();
      state.jobEvents = null;
      $('status').classList.remove('is-loading');
      $('status').removeAttribute('aria-busy');
      $('cancel').hidden = true;
      renderExports(job.exports || []);
      if (job.status === 'fertig') loadDocuments();
    }
  };
  events.onerror = () => {
    $('status-message').textContent = 'Statusverbindung wird wiederhergestellt …';
  };
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
  if (!summary.querySelector('.document-chip:not(.loading-chip)')) {
    summary.innerHTML = '<span class="document-chip loading-chip"><span class="spinner" aria-hidden="true"></span> Archiv wird geladen …</span>';
  }
  try {
    const response = await fetch('/api/documents');
    if (!response.ok) throw new Error(await responseError(response, 'Archiv konnte nicht geladen werden.'));
    const documents = await response.json();
    summary.innerHTML = documents.length
      ? `<span class="document-chip"><strong>${documents.length}</strong> Dokument${documents.length === 1 ? '' : 'e'} im lokalen Archiv</span>` + documents.slice(0, 8).map(document =>
        `<span class="document-chip">${esc(document.title)}${document.year ? ` · ${document.year}` : ''}</span>`
      ).join('')
      : '<span class="document-chip">Noch keine Dokumente verarbeitet</span>';
  } catch (error) {
    summary.innerHTML = `<span class="document-chip error-chip">${esc(error.message)}</span>`;
  } finally {
    summary.removeAttribute('aria-busy');
  }
}

async function runSearch() {
  const text = $('query').value.trim();
  if (!text) {
    setInlineStatus($('search-status'), 'Bitte einen Suchbegriff eingeben.', { error: true });
    $('query').focus();
    return;
  }
  if ($('search-button').getAttribute('aria-busy') === 'true') return;
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
      showHit(hits[Number(row.dataset.index)]);
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
    ['Ausgabe', data.output],
    ['Datenbank', data.database],
    ['Version', data.version]
  ];
  $('system-status').innerHTML = rows.map(([label, value, warning]) =>
    `<div class="system-row"><span>${esc(label)}</span><strong title="${esc(value)}">${warning !== undefined ? `<i class="status-dot ${warning ? 'warn' : ''}"></i>` : ''}${esc(value)}</strong></div>`
  ).join('');
}
$('refresh-system').onclick = () => loadSystemStatus($('refresh-system'));

async function initialize() {
  renderSources();
  await Promise.all([loadRecovery(), loadDocuments(), loadSettings(), loadCloudModels(), loadSystemStatus()]);
  const initialTab = location.hash.slice(1);
  if (['read', 'search', 'models', 'settings'].includes(initialTab)) openTab(initialTab);
}
initialize();
