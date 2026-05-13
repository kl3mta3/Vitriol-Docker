// Playlist UX — drop zone, per-row controls, websocket-driven progress.
//
// Persistence (so navigating to /files / /profile / /admin and back
// doesn't wipe the user's queue):
//   - Stone + Verify toggle states            → localStorage
//   - Row metadata (filename, ext, jobId, …)  → IndexedDB (rows store)
//   - Original File blobs                     → IndexedDB (blobs store)
//
// Row id is a uuid generated at drop time; the row keeps it in
// row.dataset.id and uses it as the IDB key for both stores. On
// playlist-mutating events we re-snapshot every row so a stale tab
// state can never get out of sync with what's on screen.

const IDB_NAME = `vitriol-playlist-${window.VITRIOL_USER_ID || 0}`;
const IDB_VERSION = 1;
const ROWS_STORE = 'rows';   // {id, filename, srcExt, dstExt, jobId, hasPassword}
const BLOBS_STORE = 'blobs'; // raw File objects keyed by row id

function _openIdb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(IDB_NAME, IDB_VERSION);
    r.onerror = () => reject(r.error);
    r.onsuccess = () => resolve(r.result);
    r.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(ROWS_STORE)) db.createObjectStore(ROWS_STORE);
      if (!db.objectStoreNames.contains(BLOBS_STORE)) db.createObjectStore(BLOBS_STORE);
    };
  });
}
function _idbReq(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbPutRow(id, meta, blob) {
  const db = await _openIdb();
  const tx = db.transaction([ROWS_STORE, BLOBS_STORE], 'readwrite');
  tx.objectStore(ROWS_STORE).put(meta, id);
  if (blob) tx.objectStore(BLOBS_STORE).put(blob, id);
  return new Promise((res, rej) => {
    tx.oncomplete = () => res();
    tx.onerror = () => rej(tx.error);
  });
}
async function idbDeleteRow(id) {
  const db = await _openIdb();
  const tx = db.transaction([ROWS_STORE, BLOBS_STORE], 'readwrite');
  tx.objectStore(ROWS_STORE).delete(id);
  tx.objectStore(BLOBS_STORE).delete(id);
  return new Promise((res, rej) => { tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error); });
}
async function idbAllRows() {
  const db = await _openIdb();
  const tx = db.transaction(ROWS_STORE, 'readonly');
  const store = tx.objectStore(ROWS_STORE);
  const keys = await _idbReq(store.getAllKeys());
  const vals = await _idbReq(store.getAll());
  return keys.map((k, i) => ({ id: k, meta: vals[i] }));
}
async function idbGetBlob(id) {
  const db = await _openIdb();
  return _idbReq(db.transaction(BLOBS_STORE, 'readonly').objectStore(BLOBS_STORE).get(id));
}
async function idbClear() {
  const db = await _openIdb();
  const tx = db.transaction([ROWS_STORE, BLOBS_STORE], 'readwrite');
  tx.objectStore(ROWS_STORE).clear();
  tx.objectStore(BLOBS_STORE).clear();
  return new Promise((res, rej) => { tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error); });
}

// Persist a row's current state. Called after any field change.
async function persistRow(row) {
  if (!row.dataset.id) return;
  const dstSel = row.querySelector('[data-dst-ext]');
  const dodCb = row.querySelector('[data-dod]');
  const meta = {
    filename: row._file ? row._file.name : (row.querySelector('[data-filename]').textContent || ''),
    srcExt:   row._srcExt,
    dstExt:   dstSel ? dstSel.value : '',
    jobId:    row._jobId || null,
    hasPassword: !!row._password,
    statusKind: (row.querySelector('[data-status]')?.className || '').replace('status-glyph ', '').trim() || 'idle',
    deleteOnDownload: dodCb ? dodCb.checked : false,
  };
  try {
    await idbPutRow(row.dataset.id, meta, row._file && !meta.jobId ? row._file : null);
  } catch (e) { /* IDB errors shouldn't break the UX */ }
}
function _uuid() {
  if (crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'r-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

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

function addFile(file, opts) {
  // opts.id  → reuse an existing IDB key (used during restoreFromIdb)
  // opts.dstExt → seed dst-ext dropdown to the saved selection
  // opts.jobId → row was already submitted before we navigated away
  // opts.deleteOnDownload → restore the Delete-on-DL checkbox state
  opts = opts || {};
  const row = tpl.content.firstElementChild.cloneNode(true);
  row.dataset.id = opts.id || _uuid();
  row._file = file;
  row._srcExt = '.' + (file ? file.name.split('.').pop() : (opts.srcExt || 'bin').replace(/^\./, '')).toLowerCase();
  row._password = '';
  row._jobId = opts.jobId || null;

  row.querySelector('[data-filename]').textContent = file ? file.name : (opts.filename || '(file lost)');
  row.querySelector('[data-src-ext]').textContent = row._srcExt;

  refreshTargets(row);
  if (opts.dstExt) {
    const sel = row.querySelector('[data-dst-ext]');
    if (sel && [...sel.options].some(o => o.value === opts.dstExt)) sel.value = opts.dstExt;
    if (sel) sel.addEventListener('change', () => persistRow(row));
  } else {
    const sel = row.querySelector('[data-dst-ext]');
    if (sel) sel.addEventListener('change', () => persistRow(row));
  }

  // Restore Delete-on-Download checkbox state from IDB.
  const dodCb = row.querySelector('[data-dod]');
  if (dodCb) {
    if (opts.deleteOnDownload) dodCb.checked = true;
    dodCb.addEventListener('change', () => persistRow(row));
  }

  bindRow(row);
  const lock = row.querySelector('[data-lock]');
  if (lock) lock.hidden = !stoneToggle.checked;
  playlist.appendChild(row);
  dz.classList.add('has-files');

  // If we're rehydrating a row whose conversion was already submitted,
  // resync with the server: fetch current job state, then either flip
  // straight to Done (with the Re-transmute / Download buttons) or
  // reopen the websocket for live progress.
  if (opts.jobId) {
    api.get(`/jobs/${opts.jobId}`).then(job => {
      if (!job) return;
      if (job.status === 'done') {
        flipToDone(row, job.id);
      } else if (job.status === 'failed') {
        setStatus(row, 'failed', job.error || 'Failed');
      } else if (job.status === 'cancelled') {
        setStatus(row, 'cancelled', 'Cancelled');
      } else {
        // queued / running — pick up live updates from where the
        // last process left off.
        openWebsocket(row, job.id);
      }
    }).catch(() => {});
  }

  persistRow(row);
}

function bindRow(row) {
  row.querySelector('[data-rm]').addEventListener('click', () => {
    if (row._jobId) api.del('/jobs/' + row._jobId).catch(() => {});
    if (row.dataset.id) idbDeleteRow(row.dataset.id).catch(() => {});
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
    persistRow(row);
    openWebsocket(row, job.id);
  } catch (ex) {
    setStatus(row, 'failed', ex.detail || 'Submit failed');
    row.querySelector('[data-go]').disabled = false;
    persistRow(row);
    // Show limit-hit support modal on 429 (rate or daily limit exceeded)
    if (ex.status === 429 && window.vitriolShowLimitModal) window.vitriolShowLimitModal();
  }
}

function _resetRowToFresh(row) {
  // Restore the row to its pre-conversion "fresh" state. Used after a
  // delete-on-download completes — the file is gone, so the row should
  // look like a newly-dropped file ready for another transmutation.
  row._jobId = null;
  row.querySelector('[data-bar]').style.width = '0%';
  setStatus(row, 'idle', 'Ready');
  row.classList.remove('row-deleted');

  // Remove the Re-transmute button if present.
  const rt = row.querySelector('[data-retransmute]');
  if (rt) rt.remove();

  // Replace whatever the current [data-go] button is (Download) with
  // a fresh Transmute button.
  const dl = row.querySelector('[data-go]');
  const reset = dl.cloneNode(true);
  reset.textContent = 'Transmute';
  reset.disabled = false;
  reset.addEventListener('click', () => convertRow(row));
  dl.parentNode.replaceChild(reset, dl);

  // Re-show the Delete-on-DL checkbox (it was hidden by .row-deleted).
  // Uncheck it since the prior cycle is complete.
  const dodCb = row.querySelector('[data-dod]');
  if (dodCb) dodCb.checked = false;

  persistRow(row);
}

function flipToDone(row, jobId) {
  setStatus(row, 'done', 'Done');
  row.querySelector('[data-bar]').style.width = '100%';

  const dodCb = row.querySelector('[data-dod]');
  const deleteOnDownload = dodCb && dodCb.checked;

  const go = row.querySelector('[data-go]');
  go.textContent = 'Download';
  go.disabled = false;
  // Replace the existing click listener (which calls convertRow) with
  // the download navigation. addEventListener doesn't replace the old
  // listener, so clone-and-replace the node to drop it cleanly.
  const fresh = go.cloneNode(true);
  fresh.addEventListener('click', () => {
    // Build the download URL — append ?delete=1 when the user opted in.
    const deleteFlag = deleteOnDownload ? '?delete=1' : '';
    location.href = `/api/v1/jobs/${jobId}/result${deleteFlag}`;

    if (deleteOnDownload) {
      // The server will delete the file after streaming. Reset this row
      // to "fresh" so the user can re-transmute but can't re-download
      // a file that no longer exists. Small delay so the browser's
      // download navigation fires first.
      setTimeout(() => _resetRowToFresh(row), 600);
    }
  });
  go.parentNode.replaceChild(fresh, go);

  // Re-transmute: convert this row again with whatever the current
  // dst-ext / Stone toggle / password are. Useful for re-doing a row
  // after tweaking output format or password without starting over
  // (or for quickly producing several different targets from the same
  // source file). Only added once — guard against double-flip.
  if (!row.querySelector('[data-retransmute]')) {
    const retransmute = document.createElement('button');
    retransmute.className = 'btn btn-secondary row-retransmute';
    retransmute.dataset.retransmute = '';
    retransmute.title = 'Run this row through transmutation again with the current settings';
    retransmute.textContent = 'Re-transmute';
    retransmute.addEventListener('click', () => {
      // Reset progress + clear the prior jobId so the new job gets its
      // own websocket and download link.
      row._jobId = null;
      row.querySelector('[data-bar]').style.width = '0%';
      // Restore the Transmute button (reverse of flipToDone).
      const dl = row.querySelector('[data-go]');
      const reset = dl.cloneNode(true);
      reset.textContent = 'Transmute';
      reset.disabled = false;
      reset.addEventListener('click', () => convertRow(row));
      dl.parentNode.replaceChild(reset, dl);
      // Drop the Re-transmute button itself; flipToDone will add it
      // back after the new job finishes.
      retransmute.remove();
      convertRow(row);
    });
    fresh.parentNode.insertBefore(retransmute, fresh.nextSibling);
  }
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

// Toggle persistence — sticky across full sessions so a user who
// always works in Stone mode doesn't have to re-flip on every visit.
const TOGGLE_KEY_STONE = 'vitriol_stone_on';
const TOGGLE_KEY_VERIFY = 'vitriol_verify_on';

// Verify Round-Trip only does anything in Stone mode — the engine ignores
// the flag for regular conversions. Mirror that in the UI: disable the
// checkbox when Stone is off, and uncheck it so a stale "on" state can't
// linger after Stone flips off and back on.
function syncVerifyToggle() {
  const stoneOn = stoneToggle.checked && !stoneToggle.disabled;
  verifyToggle.disabled = !stoneOn;
  if (!stoneOn && verifyToggle.checked) {
    verifyToggle.checked = false;
    localStorage.setItem(TOGGLE_KEY_VERIFY, '0');
  }
  // CSS hook: parent label can dim itself + the help-? glyph when disabled.
  const verifyLabel = verifyToggle.closest('label');
  if (verifyLabel) verifyLabel.classList.toggle('disabled', !stoneOn);
}

stoneToggle.addEventListener('change', () => {
  localStorage.setItem(TOGGLE_KEY_STONE, stoneToggle.checked ? '1' : '0');
  applyStoneClass();
  syncVerifyToggle();
});
verifyToggle.addEventListener('change', () => {
  localStorage.setItem(TOGGLE_KEY_VERIFY, verifyToggle.checked ? '1' : '0');
});

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
  const delete_ids = [];
  const dodRows = [];   // rows that had Delete-on-DL checked
  for (const row of playlist.children) {
    if (!row.querySelector('.row-check').checked) continue;
    if (!row._jobId) continue;
    const glyph = row.querySelector('[data-status]');
    if (!glyph || !glyph.classList.contains('done')) continue;
    ids.push(row._jobId);
    const dodCb = row.querySelector('[data-dod]');
    if (dodCb && dodCb.checked) {
      delete_ids.push(row._jobId);
      dodRows.push(row);
    }
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
      body: JSON.stringify({ ids, delete_ids }),
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

    // Reset rows whose files were deleted on the server.
    for (const row of dodRows) _resetRowToFresh(row);
  } catch (ex) {
    statusText.textContent = ex.message || 'Download failed';
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});
document.getElementById('remove-selected').addEventListener('click', () => {
  for (const row of [...playlist.children]) {
    if (row.querySelector('.row-check').checked) {
      if (row.dataset.id) idbDeleteRow(row.dataset.id).catch(() => {});
      row.remove();
    }
  }
  if (!playlist.children.length) dz.classList.remove('has-files');
});
document.getElementById('clear-all').addEventListener('click', () => {
  playlist.innerHTML = '';
  dz.classList.remove('has-files');
  idbClear().catch(() => {});
  syncSelectAllState();
});

// ---------------------------------------------------------- select all
//
// Tri-state master checkbox. Clicking it propagates to every row;
// changes to individual rows propagate back so the master reflects
// "all / some / none" without an explicit re-render. Indeterminate is
// purely visual — the underlying `checked` property is still bool, so
// the next click toggles to a definite state.

const selectAllCheckbox = document.getElementById('select-all');

function syncSelectAllState() {
  if (!selectAllCheckbox) return;
  const rows = playlist.children;
  if (rows.length === 0) {
    selectAllCheckbox.checked = false;
    selectAllCheckbox.indeterminate = false;
    selectAllCheckbox.disabled = true;
    return;
  }
  selectAllCheckbox.disabled = false;
  let checked = 0;
  for (const row of rows) {
    if (row.querySelector('.row-check').checked) checked++;
  }
  if (checked === 0) {
    selectAllCheckbox.checked = false;
    selectAllCheckbox.indeterminate = false;
  } else if (checked === rows.length) {
    selectAllCheckbox.checked = true;
    selectAllCheckbox.indeterminate = false;
  } else {
    selectAllCheckbox.checked = false;
    selectAllCheckbox.indeterminate = true;
  }
}

if (selectAllCheckbox) {
  selectAllCheckbox.addEventListener('change', () => {
    const target = selectAllCheckbox.checked;
    for (const row of playlist.children) {
      const cb = row.querySelector('.row-check');
      if (cb) cb.checked = target;
    }
    selectAllCheckbox.indeterminate = false;
  });
}

// Per-row checkbox clicks bubble up — refresh the master state. Using
// a delegated listener so dynamically-added rows are covered without
// re-binding on every append.
playlist.addEventListener('change', (e) => {
  if (e.target && e.target.classList && e.target.classList.contains('row-check')) {
    syncSelectAllState();
  }
});

// Add/remove via DOM mutation (rehydrate from IDB, drop new files,
// remove-selected) — sync after the microtask so the row count is
// settled before we read it.
const _selectAllObserver = new MutationObserver(() => syncSelectAllState());
_selectAllObserver.observe(playlist, { childList: true });
syncSelectAllState();

// Restore toggle state from localStorage before we touch the playlist
// — ensures Stone/Verify reflect prior state by the time rows render
// (so lock-icon visibility is correct on rehydrated rows).
if (localStorage.getItem(TOGGLE_KEY_STONE) === '1') stoneToggle.checked = true;
if (localStorage.getItem(TOGGLE_KEY_VERIFY) === '1') verifyToggle.checked = true;
applyStoneClass();
// Resolve verify-toggle visibility against Stone state on every boot —
// covers the case where Stone was unchecked last session but Verify
// was still "on" in localStorage (the gating clears the stale flag).
syncVerifyToggle();

// Rehydrate any persisted rows from IndexedDB. Order:
//   1. Wait for /formats so the dst-ext dropdowns can populate.
//   2. Walk the rows store; for each, fetch the saved File blob (if
//      one was stored) and reconstruct the row. addFile handles WS
//      reconnection for jobIds that survived the navigation.
async function restoreFromIdb() {
  await loadFormats();
  let entries = [];
  try { entries = await idbAllRows(); } catch (e) { return; }
  for (const { id, meta } of entries) {
    if (!meta) continue;
    let blob = null;
    try { blob = await idbGetBlob(id); } catch (_) { blob = null; }
    // No blob AND no jobId means the row is unrecoverable — drop it.
    if (!blob && !meta.jobId) {
      idbDeleteRow(id).catch(() => {});
      continue;
    }
    const file = blob
      ? (blob instanceof File ? blob : new File([blob], meta.filename || 'file'))
      : null;
    addFile(file, {
      id,
      filename: meta.filename,
      srcExt: meta.srcExt,
      dstExt: meta.dstExt,
      jobId: meta.jobId || null,
      deleteOnDownload: meta.deleteOnDownload || false,
    });
  }
}

restoreFromIdb();

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
