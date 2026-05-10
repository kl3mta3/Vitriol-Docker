// Files page — searchable / sortable table over /api/v1/files with
// bulk actions: download as zip, add to playlist (replays in / via
// localStorage), delete.

const tbody = document.getElementById('files-tbody');
const searchInput = document.getElementById('files-search');
const countLabel = document.getElementById('files-count');
const selectAll = document.getElementById('files-select-all');

const state = {
  files: [],
  search: '',
  sortKey: 'finished_at',
  sortDir: 'desc',
};

// ---------------- Search + sort ----------------------------------------

searchInput.addEventListener('input', () => {
  state.search = searchInput.value.trim().toLowerCase();
  render();
});

document.querySelectorAll('th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const k = th.dataset.sort;
    if (state.sortKey === k) {
      state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      state.sortKey = k;
      state.sortDir = k === 'finished_at' || k === 'bytes_out' ? 'desc' : 'asc';
    }
    render();
  });
});

selectAll.addEventListener('change', () => {
  document.querySelectorAll('tr.file-row .row-check').forEach(cb => cb.checked = selectAll.checked);
});

function applySearch(files) {
  if (!state.search) return files;
  return files.filter(f => {
    const hay = `${f.dst_filename} ${f.src_filename} ${f.dst_ext} ${f.owner_username}`.toLowerCase();
    return hay.includes(state.search);
  });
}

function applySort(files) {
  const k = state.sortKey;
  const dir = state.sortDir === 'asc' ? 1 : -1;
  return [...files].sort((a, b) => {
    let av = a[k], bv = b[k];
    if (k === 'finished_at') {
      av = av ? Date.parse(av) : 0;
      bv = bv ? Date.parse(bv) : 0;
    } else if (k === 'bytes_out') {
      av = av || 0; bv = bv || 0;
    } else {
      av = (av || '').toString().toLowerCase();
      bv = (bv || '').toString().toLowerCase();
    }
    if (av < bv) return -1 * dir;
    if (av > bv) return  1 * dir;
    return 0;
  });
}

function updateSortIndicators() {
  document.querySelectorAll('th[data-sort]').forEach(th => {
    const ind = th.querySelector('.sort-ind');
    if (!ind) return;
    if (th.dataset.sort === state.sortKey) {
      ind.textContent = state.sortDir === 'asc' ? '▲' : '▼';
      th.classList.add('sorted');
    } else {
      ind.textContent = '';
      th.classList.remove('sorted');
    }
  });
}

// ---------------- Render -----------------------------------------------

function render() {
  updateSortIndicators();
  const filtered = applySort(applySearch(state.files));
  tbody.innerHTML = '';
  if (filtered.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="7" class="empty-state">${state.search ? 'No files match that search.' : 'No converted files retained yet.'}</td>`;
    tbody.appendChild(tr);
  } else {
    for (const f of filtered) tbody.appendChild(buildRow(f));
  }
  countLabel.textContent = state.search
    ? `${filtered.length} of ${state.files.length}`
    : `${state.files.length} file${state.files.length === 1 ? '' : 's'}`;
  selectAll.checked = false;
}

function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function fmtDate(s) {
  if (!s) return '—';
  return new Date(s).toLocaleString();
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function buildRow(f) {
  const tr = document.createElement('tr');
  tr.className = 'file-row clickable';
  tr.dataset.id = f.id;
  tr.innerHTML = `
    <td><input type="checkbox" class="row-check" /></td>
    <td title="from: ${escapeHtml(f.src_filename)}">${escapeHtml(f.dst_filename)}</td>
    <td><code>${escapeHtml(f.dst_ext)}</code></td>
    <td>${fmtBytes(f.bytes_out)}</td>
    <td>${fmtDate(f.finished_at)}</td>
    <td>${f.is_own ? '<em class="muted">you</em>' : escapeHtml(f.owner_username)}</td>
    <td class="row-manage-cell">
      <a class="btn btn-secondary btn-manage" href="/api/v1/jobs/${f.id}/result" target="_blank" rel="noopener">Download</a>
    </td>
  `;
  // Clicking the row toggles its checkbox (but not when the user clicks
  // the inline Download link — we don't want a single click to
  // accidentally tick a hidden box AND open a download tab).
  tr.addEventListener('click', (e) => {
    if (e.target.closest('a, button, input')) return;
    const cb = tr.querySelector('.row-check');
    cb.checked = !cb.checked;
  });
  return tr;
}

function selectedIds() {
  return [...document.querySelectorAll('tr.file-row .row-check:checked')].map(cb => Number(cb.closest('tr').dataset.id));
}

// ---------------- Bulk actions -----------------------------------------

document.getElementById('files-download').addEventListener('click', async () => {
  const ids = selectedIds();
  if (!ids.length) { alert('Select at least one file.'); return; }
  // Re-use the existing zip endpoint — has the same access checks the
  // /files API does.
  const r = await fetch('/api/v1/jobs/download-zip', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  if (!r.ok) {
    let msg = `Download failed (${r.status})`;
    try { const j = await r.json(); if (j && j.detail) msg = j.detail; } catch (_) {}
    alert(msg);
    return;
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'vitriol-files.zip';
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  // After a successful zip download, the server may have applied
  // delete-on-download policies — refresh the list.
  await refresh();
});

document.getElementById('files-replay').addEventListener('click', async () => {
  const ids = selectedIds();
  if (!ids.length) { alert('Select at least one file.'); return; }
  // Stash the chosen job ids; the App page (playlist.js) reads the
  // queue on load and downloads each blob to seed playlist rows as
  // new conversion sources.
  const queue = state.files.filter(f => ids.includes(f.id)).map(f => ({
    id: f.id, name: f.dst_filename, ext: f.dst_ext,
  }));
  localStorage.setItem('vitriol_replay_queue', JSON.stringify(queue));
  location.href = '/';
});

document.getElementById('files-delete').addEventListener('click', async () => {
  const ids = selectedIds();
  if (!ids.length) { alert('Select at least one file.'); return; }
  if (!confirm(`Delete ${ids.length} file${ids.length === 1 ? '' : 's'} permanently?`)) return;
  let failed = 0;
  for (const id of ids) {
    try { await api.del(`/files/${id}`); } catch (_) { failed += 1; }
  }
  if (failed) alert(`${failed} file${failed === 1 ? '' : 's'} could not be deleted.`);
  await refresh();
});

// ---------------- Refresh ----------------------------------------------

async function refresh() {
  try {
    const data = await api.get('/files');
    if (!Array.isArray(data)) return;
    state.files = data;
    render();
  } catch (ex) {
    console.error('Failed to load files:', ex);
    countLabel.textContent = `Failed to load: ${(ex && ex.detail) || 'unknown error'}`;
    countLabel.className = 'error';
    state.files = [];
    render();
  }
}

refresh();
