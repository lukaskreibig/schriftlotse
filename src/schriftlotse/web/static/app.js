const state = { sources: [], job: null, hits: [], selected: null, cloudModels: [], settings: null, outputToken: null };
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
}[char]));

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
  input.onchange = () => document.querySelectorAll('.profile-option').forEach(label => {
    label.classList.toggle('selected', label.querySelector('input').checked);
  });
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
}

$('clear-sources').onclick = () => { state.sources = []; renderSources(); };

async function loadRecovery() {
  const response = await fetch('/api/recovery');
  const rows = response.ok ? await response.json() : [];
  const box = $('recovery');
  if (!rows.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = '<strong>Unterbrochene Verarbeitung gefunden</strong>' + rows.map(row =>
    `<p>${esc(row.message || `Auftrag ${row.id.slice(0, 8)}`)} <button data-resume="${row.id}">Sicher fortsetzen</button></p>`
  ).join('');
  box.querySelectorAll('[data-resume]').forEach(button => {
    button.onclick = async () => {
      const response = await fetch(`/api/jobs/${button.dataset.resume}/resume`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) { notify(data.detail || 'Fortsetzen fehlgeschlagen', true); return; }
      state.job = data.id;
      watchJob(data.id);
      box.hidden = true;
    };
  });
}

async function upload(files) {
  if (!files.length) return;
  const body = new FormData();
  [...files].forEach(file => body.append('files', file));
  const response = await fetch('/api/uploads', { method: 'POST', body });
  if (!response.ok) throw new Error(await response.text());
  const data = await response.json();
  state.sources.push(...data.sources.filter(source => !state.sources.some(item => item.id === source.id)));
  renderSources();
}

$('choose-files').onclick = () => $('files').click();
$('files').onchange = event => {
  upload(event.target.files)
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
dropzone.addEventListener('drop', event => upload(event.dataTransfer.files).catch(error => notify(error.message, true)));

$('folder').onclick = async () => {
  try {
    const response = await fetch('/api/folder', { method: 'POST' });
    const data = await response.json();
    if (data.source && !state.sources.some(item => item.id === data.source.id)) {
      state.sources.push(data.source);
      renderSources();
      notify('Ordner wurde hinzugefügt.');
    }
  } catch (error) {
    notify(`Ordnerauswahl fehlgeschlagen: ${error.message}`, true);
  }
};

$('start').onclick = async () => {
  if (!state.sources.length) { notify('Bitte zuerst Dateien oder einen Ordner auswählen.', true); return; }
  const payload = {
    sources: state.sources.map(source => source.id),
    year: $('year').value ? Number($('year').value) : null,
    script: $('script').value,
    quality: document.querySelector('input[name="quality"]:checked').value,
    cloud: false,
    cloud_budget_usd: 1
  };
  const response = await fetch('/api/jobs', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok) { notify(data.detail || 'Start fehlgeschlagen', true); return; }
  state.job = data.id;
  $('cancel').hidden = false;
  $('exports').innerHTML = '';
  watchJob(data.id);
};

function duration(seconds) {
  if (seconds == null) return '';
  if (seconds < 60) return `${seconds} s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours} h ${minutes} min` : `${minutes} min`;
}

function watchJob(id) {
  const events = new EventSource(`/api/jobs/${id}/events`);
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
      $('cancel').hidden = true;
      renderExports(job.exports || []);
      if (job.status === 'fertig') loadDocuments();
    }
  };
  events.onerror = () => {
    $('status-message').textContent = 'Statusverbindung wird wiederhergestellt …';
  };
}

$('cancel').onclick = () => state.job && fetch(`/api/jobs/${state.job}/cancel`, { method: 'POST' });

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
  const response = await fetch('/api/documents');
  if (!response.ok) return;
  const documents = await response.json();
  $('archive-summary').innerHTML = documents.length
    ? `<span class="document-chip"><strong>${documents.length}</strong> Dokument${documents.length === 1 ? '' : 'e'} im lokalen Archiv</span>` + documents.slice(0, 8).map(document =>
      `<span class="document-chip">${esc(document.title)}${document.year ? ` · ${document.year}` : ''}</span>`
    ).join('')
    : '<span class="document-chip">Noch keine Dokumente verarbeitet</span>';
}

async function runSearch() {
  const text = $('query').value.trim();
  if (!text) return;
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
  if (!response.ok) { notify('Suche fehlgeschlagen.', true); return; }
  renderHits(await response.json(), `Treffer für „${text}“`);
}

function renderHits(hits, title) {
  state.hits = hits;
  $('results-title').textContent = title;
  $('result-count').textContent = String(hits.length);
  $('empty-results').hidden = Boolean(hits.length);
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
  const response = await fetch('/api/review-queue?limit=200');
  if (!response.ok) { notify('Prüfliste konnte nicht geladen werden.', true); return; }
  renderHits(await response.json(), 'Unsichere Stellen — niedrigste Sicherheit zuerst');
};

async function showHit(hit) {
  state.selected = hit;
  $('viewer-empty').hidden = true;
  $('viewer-content').hidden = false;
  $('scan').src = hit.image_url;
  $('hit-title').textContent = `${hit.document_title} · Seite ${Number(hit.page_index) + 1}`;
  $('hit-confidence').textContent = `${Math.round(Number(hit.confidence) * 100)} % Sicherheit`;
  $('correction').value = hit.text;
  $('correction-status').textContent = '';
  $('scan').onload = () => drawBox(hit.bbox);
  const response = await fetch(`/api/lines/${hit.line_id}`);
  if (!response.ok) return;
  const details = await response.json();
  const unique = [];
  const seen = new Set();
  details.readings.forEach(reading => {
    const key = `${reading.model}\n${reading.text}`;
    if (!seen.has(key)) { seen.add(key); unique.push(reading); }
  });
  $('readings').innerHTML = unique.length > 1 ? '<strong>Andere Modell-Lesungen</strong>' + unique.map((reading, index) =>
    `<button class="reading" data-reading="${index}"><span>${esc(reading.text)}<small>${esc(reading.model)} · ${esc(reading.kind)}</small></span><span>${Math.round(Number(reading.confidence) * 100)} %</span></button>`
  ).join('') : '';
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
  const response = await fetch(`/api/lines/${state.selected.line_id}`, {
    method: 'PATCH', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text: $('correction').value })
  });
  $('correction-status').textContent = response.ok
    ? 'Bestätigt und im Suchindex aktualisiert.'
    : 'Speichern fehlgeschlagen.';
};

$('export-current').onclick = async () => {
  if (!state.selected) return;
  const button = $('export-current');
  button.disabled = true;
  $('correction-status').textContent = 'PDF, DOCX, Text und Austauschformate werden aktualisiert …';
  const response = await fetch(`/api/documents/${state.selected.document_id}/export`, { method: 'POST' });
  const data = await response.json();
  button.disabled = false;
  if (!response.ok) { $('correction-status').textContent = data.detail || 'Export fehlgeschlagen.'; return; }
  $('correction-status').innerHTML = 'Aktuelle Fassung exportiert: ' + data.downloads.map(download =>
    `<a href="/api/output/${encodeURIComponent(download.id)}">${esc(download.name)}</a>`
  ).join(' · ');
};

async function loadCloudModels() {
  const response = await fetch('/api/cloud-models');
  if (!response.ok) return;
  state.cloudModels = await response.json();
  $('cloud-model').innerHTML = state.cloudModels.map(option =>
    `<option value="${esc(option.key)}">${esc(option.label)} — ${esc(option.model)}</option>`
  ).join('');
  $('setting-cloud-model').innerHTML = state.cloudModels.map(option =>
    `<option value="${esc(option.key)}">${esc(option.label)}</option>`
  ).join('');
  if (state.settings?.openrouter_profile) {
    $('cloud-model').value = state.settings.openrouter_profile;
    $('setting-cloud-model').value = state.settings.openrouter_profile;
  }
  renderCloudHelp();
  $('cloud-catalog').innerHTML = state.cloudModels.map(option =>
    `<div class="cloud-model-card ${option.recommended ? 'recommended' : ''}"><strong>${esc(option.label)}</strong><code>${esc(option.model)}</code><small>${esc(option.description)} ${esc(option.price_hint)}</small></div>`
  ).join('');
}

function renderCloudHelp() {
  const option = state.cloudModels.find(model => model.key === $('cloud-model').value);
  $('cloud-model-help').textContent = option ? `${option.model} · ${option.description} ${option.price_hint}.` : '';
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
  const button = $('cloud-review');
  button.disabled = true;
  $('correction-status').textContent = 'OpenRouter prüft den ausgewählten Ausschnitt …';
  const response = await fetch(`/api/lines/${state.selected.line_id}/cloud-review`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ budget_usd: budget, profile: $('cloud-model').value })
  });
  const data = await response.json();
  button.disabled = false;
  if (!response.ok) { $('correction-status').textContent = data.detail || 'Prüfung fehlgeschlagen.'; return; }
  $('correction').value = data.text;
  $('correction-status').textContent = `Unbestätigter Vorschlag von ${data.model} · $${Number(data.cost_usd).toFixed(4)}. Zum Übernehmen bitte bestätigen.`;
};

async function loadModels() {
  const response = await fetch('/api/models');
  if (!response.ok) return;
  const rows = await response.json();
  $('model-list').innerHTML = rows.map(model =>
    `<div class="model"><div><strong>${esc(model.name)}</strong><div class="muted">${esc(model.license)}</div></div><div>${esc(model.purpose)}</div><div>${model.estimated_size_mb ? `${model.estimated_size_mb} MB` : ''}</div><button data-key="${model.key}" ${model.installed ? 'disabled' : ''}>${model.installed ? 'Installiert' : 'Installieren'}</button></div>`
  ).join('');
  $('model-list').querySelectorAll('.model button:not([disabled])').forEach(button => {
    button.onclick = () => installModel(button.dataset.key);
  });
}

async function installModel(key) {
  let accept = true;
  if (key === 'churro-mlx-8bit') {
    accept = await askConfirmation(
      'CHURRO lokal installieren?',
      'CHURRO benötigt etwa 4,4 GB und steht unter der Qwen Research License. Die Installation ist für deine Forschungs-/nichtkommerzielle Nutzung vorgesehen.'
    );
  }
  if (!accept) return;
  const response = await fetch(`/api/models/${key}/install`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ accept_license: accept })
  });
  const data = await response.json();
  if (!response.ok) { notify(data.detail || 'Installation fehlgeschlagen', true); return; }
  watchModelInstall(data.id);
}

async function watchModelInstall(id) {
  const box = $('model-install-status');
  box.hidden = false;
  const tick = async () => {
    const response = await fetch(`/api/model-installs/${id}`);
    const data = await response.json();
    box.textContent = `${data.message} · ${duration(data.elapsed_seconds)}`;
    if (data.status === 'läuft') setTimeout(tick, 1000); else loadModels();
  };
  tick();
}

$('save-openrouter-key').onclick = async () => {
  const key = $('openrouter-key').value.trim();
  if (!key) { notify('Bitte zuerst einen API-Schlüssel einfügen.', true); return; }
  $('key-status').textContent = 'Schlüssel wird bei OpenRouter geprüft …';
  const response = await fetch('/api/openrouter-key', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ key, validate: true })
  });
  const data = await response.json();
  if (!response.ok) {
    $('key-status').textContent = data.detail || 'Prüfung fehlgeschlagen.';
    notify($('key-status').textContent, true);
    return;
  }
  $('key-status').textContent = `Sicher gespeichert und bestätigt${data.label ? ` · ${data.label}` : ''}.`;
  $('openrouter-key').value = '';
  loadSystemStatus();
};

$('validate-openrouter-key').onclick = () => loadKeyStatus(true);
$('delete-openrouter-key').onclick = async () => {
  const approved = await askConfirmation('API-Schlüssel entfernen?', 'Der OpenRouter-Schlüssel wird aus dem macOS-Schlüsselbund gelöscht.');
  if (!approved) return;
  const response = await fetch('/api/openrouter-key', { method: 'DELETE' });
  $('key-status').textContent = response.ok ? 'Kein OpenRouter-Schlüssel gespeichert.' : 'Entfernen fehlgeschlagen.';
  loadSystemStatus();
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
  const response = await fetch('/api/settings');
  if (!response.ok) { notify('Einstellungen konnten nicht geladen werden.', true); return; }
  const settings = await response.json();
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
  $('settings-status').textContent = 'Wird gespeichert …';
  const response = await fetch('/api/settings', {
    method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok) {
    $('settings-status').textContent = data.detail || 'Speichern fehlgeschlagen.';
    notify($('settings-status').textContent, true);
    return;
  }
  state.settings = data;
  state.outputToken = null;
  $('settings-status').textContent = 'Gespeichert. Gilt für neue Aufträge.';
  setScriptCombobox(data.default_script);
  notify('Einstellungen wurden gespeichert.');
  loadSystemStatus();
};

$('pick-output').onclick = async () => {
  const response = await fetch('/api/settings/output-folder', { method: 'POST' });
  const data = await response.json();
  if (data.path) {
    $('setting-output').value = data.path;
    state.outputToken = data.token;
  }
};
$('setting-output').oninput = () => { state.outputToken = null; };

async function loadKeyStatus(validate = false) {
  $('key-status').textContent = validate ? 'Verbindung zu OpenRouter wird geprüft …' : 'Schlüsselbund wird geprüft …';
  const response = await fetch(`/api/openrouter-key?validate=${validate}`);
  const data = await response.json();
  if (!response.ok) {
    $('key-status').textContent = data.detail || 'Prüfung fehlgeschlagen.';
    return;
  }
  $('key-status').textContent = data.configured
    ? data.validated
      ? `Schlüssel bestätigt${data.label ? ` · ${data.label}` : ''}${data.limit_remaining != null ? ` · Restlimit $${Number(data.limit_remaining).toFixed(2)}` : ''}.`
      : 'API-Schlüssel ist sicher im macOS-Schlüsselbund gespeichert.'
    : 'Kein OpenRouter-Schlüssel gespeichert.';
}

async function loadSystemStatus() {
  const response = await fetch('/api/system');
  if (!response.ok) return;
  const data = await response.json();
  $('local-badge').innerHTML = '<span></span> Lokal bereit · ' + esc(data.models_installed) + '/' + esc(data.models_total) + ' Modelle';
  const rows = [
    ['Archiv', `${data.documents} Dokumente · ${data.pages} Seiten · ${data.lines} Zeilen`],
    ['Lokale Modelle', `${data.models_installed} von ${data.models_total} installiert`],
    ['Tesseract', data.tesseract_available ? 'bereit' : 'nicht gefunden', !data.tesseract_available],
    ['OpenRouter', data.openrouter_configured ? 'Schlüssel gespeichert' : 'nicht eingerichtet', !data.openrouter_configured],
    ['Ausgabe', data.output],
    ['Datenbank', data.database],
    ['Version', data.version]
  ];
  $('system-status').innerHTML = rows.map(([label, value, warning]) =>
    `<div class="system-row"><span>${esc(label)}</span><strong title="${esc(value)}">${warning !== undefined ? `<i class="status-dot ${warning ? 'warn' : ''}"></i>` : ''}${esc(value)}</strong></div>`
  ).join('');
}
$('refresh-system').onclick = loadSystemStatus;

async function initialize() {
  renderSources();
  await Promise.all([loadRecovery(), loadDocuments(), loadSettings(), loadCloudModels(), loadSystemStatus()]);
  const initialTab = location.hash.slice(1);
  if (['read', 'search', 'models', 'settings'].includes(initialTab)) openTab(initialTab);
}
initialize();
