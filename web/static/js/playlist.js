// Playlist UX — drop zone, per-row controls, websocket-driven progress.

const dz = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');
const playlist = document.getElementById('playlist');
const tpl = document.getElementById('row-template');
const stoneToggle = document.getElementById('toggle-stone');
const verifyToggle = document.getElementById('toggle-verify');
const stoneDialog = document.getElementById('stone-dialog');
const stonePassword = document.getElementById('stone-password');
const stoneShow = document.getElementById('stone-show');
const statusText = document.getElementById('status-text');
const appShell = document.querySelector('.app-shell');

let formats = null;
let activeRowForDialog = null;

const STATUS_GLYPH = {
  idle: '·', running: '◔', done: '✓', failed: '✗', cancelled: '·', queued: '◌',
};

async function loadFormats() {
  try { formats = await api.get('/formats'); } catch (e) { formats = { inputs: [], outputs: [], targets_for: {} }; }
}

function targetsFor(srcExt) {
  if (!formats) return [];
  const key = srcExt + (stoneToggle.checked ? '+stone' : '');
  return formats.targets_for[key] || formats.targets_for[srcExt] || [];
}

function applyStoneClass() {
  const on = stoneToggle.checked;
  appShell.classList.toggle('stone-on', on);
  // Lock icon + per-row password only make sense for Stone conversions
  // — the non-Stone code paths in the engine ignore the password byte
  // string entirely. Hide the lock when Stone is off and clear any
  // stashed password on each row so a stale value can't sneak through
  // if the user later flips Stone back on for an unrelated file.
  for (const row of playlist.children) {
    refreshTargets(row);
    const lock = row.querySelector('[data-lock]');
    if (lock) lock.hidden = !on;
    if (!on) {
      row._password = '';
      if (lock) {
        lock.textContent = '🔓';
        lock.classList.remove('locked');
      }
    }
  }
}

function refreshTargets(row) {
  const select = row.querySelector('[data-dst-ext]');
  const current = select.value;
  const srcExt = row._srcExt;
  const targets = targetsFor(srcExt);
  select.innerHTML = '';
  for (const ext of targets) {
    const opt = document.createElement('option');
    opt.value = ext;
    opt.textContent = ext;
    select.appendChild(opt);
  }
  if (targets.includes(current)) select.value = current;
}

function addFile(file) {
  const row = tpl.content.firstElementChild.cloneNode(true);
  row._file = file;
  row._srcExt = '.' + file.name.split('.').pop().toLowerCase();
  row._password = '';
  row._jobId = null;

  row.querySelector('[data-filename]').textContent = file.name;
  row.querySelector('[data-src-ext]').textContent = row._srcExt;

  refreshTargets(row);
  bindRow(row);
  // If Stone isn't on at row-create time, hide the lock icon up front
  // — applyStoneClass keeps it in sync from then on.
  const lock = row.querySelector('[data-lock]');
  if (lock) lock.hidden = !stoneToggle.checked;
  playlist.appendChild(row);
  dz.classList.add('has-files');
}

function bindRow(row) {
  row.querySelector('[data-rm]').addEventListener('click', () => {
    if (row._jobId) api.del('/jobs/' + row._jobId).catch(() => {});
    row.remove();
    if (!playlist.children.length) dz.classList.remove('has-files');
  });
  row.querySelector('[data-go]').addEventListener('click', () => convertRow(row));
  row.querySelector('[data-lock]').addEventListener('click', () => openStoneDialog(row));
}

function openStoneDialog(row) {
  activeRowForDialog = row;
  stonePassword.value = row._password || '';
  stoneShow.checked = false;
  stonePassword.type = 'password';
  stoneDialog.showModal();
}

stoneDialog.addEventListener('close', () => {
  if (stoneDialog.returnValue === 'set' && activeRowForDialog) {
    activeRowForDialog._password = stonePassword.value;
    const lock = activeRowForDialog.querySelector('[data-lock]');
    lock.textContent = stonePassword.value ? '🔒' : '🔓';
    lock.classList.toggle('locked', !!stonePassword.value);
  }
  activeRowForDialog = null;
});
stoneShow.addEventListener('change', () => {
  stonePassword.type = stoneShow.checked ? 'text' : 'password';
});

async function convertRow(row) {
  const dstExt = row.querySelector('[data-dst-ext]').value;
  if (!dstExt) {
    setStatus(row, 'failed', 'No target format');
    return;
  }
  const fd = new FormData();
  fd.append('file', row._file);
  fd.append('dst_ext', dstExt);
  fd.append('stone', stoneToggle.checked ? 'true' : 'false');
  fd.append('verify_round_trip', verifyToggle.checked ? 'true' : 'false');
  if (row._password) fd.append('password', row._password);
  // Self-compile targets are inferred from the chosen .py / .exe target with stone on.
  if (stoneToggle.checked && (dstExt === '.py' || dstExt === '.exe')) {
    fd.append('self_compile_target', dstExt.slice(1));
  }
  setStatus(row, 'queued', 'Queued');
  row.querySelector('[data-go]').disabled = true;
  try {
    const job = await api.post('/convert', fd, true);
    row._jobId = job.id;
    openWebsocket(row, job.id);
  } catch (ex) {
    setStatus(row, 'failed', ex.detail || 'Submit failed');
    row.querySelector('[data-go]').disabled = false;
  }
}

function flipToDone(row, jobId) {
  setStatus(row, 'done', 'Done');
  row.querySelector('[data-bar]').style.width = '100%';
  const go = row.querySelector('[data-go]');
  go.textContent = 'Download';
  go.disabled = false;
  // Replace the existing click listener (which calls convertRow) with
  // the download navigation. addEventListener doesn't replace the old
  // listener, so clone-and-replace the node to drop it cleanly.
  const fresh = go.cloneNode(true);
  fresh.addEventListener('click', () => { location.href = `/api/v1/jobs/${jobId}/result`; });
  go.parentNode.replaceChild(fresh, go);
}

function openWebsocket(row, jobId) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/jobs/${jobId}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'snapshot') {
      // The snapshot is the *current* job state at the moment the WS
      // opened — for fast conversions (small images, etc.) the engine
      // can finish before the browser's WS handshake completes, so a
      // snapshot with a terminal status is normal and must be treated
      // as the matching terminal event. Without this branch the UI
      // would be stuck at "100% running" forever waiting for a `done`
      // event that already fired before anyone subscribed.
      const pct = msg.progress || 0;
      row.querySelector('[data-bar]').style.width = pct + '%';
      if (msg.status === 'done') { flipToDone(row, jobId); ws.close(); return; }
      if (msg.status === 'failed') {
        setStatus(row, 'failed', 'Failed');
        row.querySelector('[data-go]').disabled = false;
        ws.close(); return;
      }
      if (msg.status === 'cancelled') {
        setStatus(row, 'cancelled', 'Cancelled');
        row.querySelector('[data-go]').disabled = false;
        ws.close(); return;
      }
      setStatus(row, 'running', `${pct}%`);
    } else if (msg.type === 'progress') {
      const pct = msg.progress || 0;
      row.querySelector('[data-bar]').style.width = pct + '%';
      setStatus(row, 'running', `${pct}%`);
    } else if (msg.type === 'done') {
      flipToDone(row, jobId);
      ws.close();
    } else if (msg.type === 'failed') {
      setStatus(row, 'failed', msg.error || 'Failed');
      row.querySelector('[data-go]').disabled = false;
      ws.close();
    } else if (msg.type === 'cancelled') {
      setStatus(row, 'cancelled', 'Cancelled');
      row.querySelector('[data-go]').disabled = false;
      ws.close();
    }
  };
  ws.onerror = () => setStatus(row, 'failed', 'WS error');
}

function setStatus(row, kind, text) {
  const g = row.querySelector('[data-status]');
  g.textContent = STATUS_GLYPH[kind] || '·';
  g.className = 'status-glyph ' + kind;
  if (text) statusText.textContent = `${row.querySelector('[data-filename]').textContent}: ${text}`;
}

// Drop zone behavior.
dz.addEventListener('click', () => fileInput.click());
dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', (e) => {
  e.preventDefault();
  dz.classList.remove('drag');
  for (const f of e.dataTransfer.files) addFile(f);
});
fileInput.addEventListener('change', () => {
  for (const f of fileInput.files) addFile(f);
  fileInput.value = '';
});

stoneToggle.addEventListener('change', applyStoneClass);

document.getElementById('convert-all').addEventListener('click', () => {
  for (const row of playlist.children) convertRow(row);
});
document.getElementById('convert-selected').addEventListener('click', () => {
  for (const row of playlist.children) {
    if (row.querySelector('.row-check').checked) convertRow(row);
  }
});

document.getElementById('download-selected').addEventListener('click', async () => {
  // Collect job ids for rows that are both checked AND finished. Rows
  // without a job_id (never converted) and rows still running / failed
  // are silently skipped — the same set the server would accept anyway.
  const ids = [];
  for (const row of playlist.children) {
    if (!row.querySelector('.row-check').checked) continue;
    if (!row._jobId) continue;
    const glyph = row.querySelector('[data-status]');
    if (!glyph || !glyph.classList.contains('done')) continue;
    ids.push(row._jobId);
  }
  if (!ids.length) {
    statusText.textContent = 'No completed conversions selected.';
    return;
  }
  const btn = document.getElementById('download-selected');
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = `Zipping ${ids.length}…`;
  try {
    const r = await fetch('/api/v1/jobs/download-zip', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    if (!r.ok) {
      let msg = `Download failed (${r.status})`;
      try { const j = await r.json(); if (j && j.detail) msg = j.detail; } catch (_) {}
      throw new Error(msg);
    }
    // Stream the response into a Blob and trigger an anchor click — the
    // browser handles the actual save dialog.
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'vitriol-conversions.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    statusText.textContent = `Downloaded ${ids.length} file${ids.length === 1 ? '' : 's'} as zip.`;
  } catch (ex) {
    statusText.textContent = ex.message || 'Download failed';
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});
document.getElementById('remove-selected').addEventListener('click', () => {
  for (const row of [...playlist.children]) {
    if (row.querySelector('.row-check').checked) row.remove();
  }
  if (!playlist.children.length) dz.classList.remove('has-files');
});
document.getElementById('clear-all').addEventListener('click', () => {
  playlist.innerHTML = '';
  dz.classList.remove('has-files');
});

loadFormats();

// ---------------- Replay queue from /files page ------------------------
//
// The Files page stashes selected jobs in localStorage and redirects
// here. We pull each output file's bytes back, wrap it as a File so the
// rest of the playlist code treats it identically to a drag-dropped
// upload, and add it as a new playlist row ready for the user to pick a
// new target format and convert.

(async function replayFromFilesTab() {
  const raw = localStorage.getItem('vitriol_replay_queue');
  if (!raw) return;
  localStorage.removeItem('vitriol_replay_queue');
  let queue = [];
  try { queue = JSON.parse(raw); } catch (_) { return; }
  if (!Array.isArray(queue) || !queue.length) return;

  // Wait for the formats catalogue to land before adding rows so the
  // dst-ext dropdowns populate immediately.
  let waited = 0;
  while (!formats && waited < 30) { await new Promise(r => setTimeout(r, 100)); waited++; }

  for (const item of queue) {
    try {
      const r = await fetch(`/api/v1/jobs/${item.id}/result`, { credentials: 'same-origin' });
      if (!r.ok) {
        statusText.textContent = `Skipped ${item.name}: ${r.status}`;
        continue;
      }
      const blob = await r.blob();
      // File preserves the filename so the playlist renders + uploads
      // the same way as a fresh drag-drop. Type from blob.
      const file = new File([blob], item.name, { type: blob.type });
      addFile(file);
    } catch (ex) {
      statusText.textContent = `Replay failed for ${item.name}: ${ex.message || ex}`;
    }
  }
  if (queue.length) statusText.textContent = `Loaded ${queue.length} file${queue.length === 1 ? '' : 's'} from Files tab.`;
})();
