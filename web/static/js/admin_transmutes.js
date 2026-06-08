// Active Transmutes admin tab — polls the server every 2s for the live
// queue snapshot and re-renders. Buttons (skip / pause / resume / stop)
// call the corresponding /admin/transmutes/{id}/* endpoint. Done /
// failed / cancelled jobs naturally drop out of the next poll's response
// (the server only lists running/queued/held), so there is nothing to
// manually remove on this side.

const POLL_MS = 2000;

const WINDOW_LABELS = {
  today: 'Daily transmutes',
  weekly: 'Last 7 days',
  monthly: 'Last 30 days',
  '3mo': 'Last 3 months',
  '6mo': 'Last 6 months',
  yearly: 'Last year',
  all: 'All-time transmutes',
};

let _pollTimer = null;
let _currentWindow = 'today';

async function refreshStats() {
  try {
    const s = await window.api.get(`/admin/transmutes/stats?window=${encodeURIComponent(_currentWindow)}`);
    document.getElementById('stats-total').textContent = s.total.toLocaleString();
    document.getElementById('stats-stone').textContent = s.stone.toLocaleString();
    document.getElementById('stats-non-stone').textContent = s.non_stone.toLocaleString();
    document.getElementById('stats-window-label').textContent = WINDOW_LABELS[s.window] || 'Transmutes';
  } catch (e) {
    // Non-fatal — just leave the previous numbers up.
    console.warn('stats refresh failed', e);
  }
}

function fmtAgo(iso) {
  // Server returns naive UTC; treat it as such so the clock math is right.
  const t = new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime();
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function rowHtml(item) {
  const stateLabel = {
    running: '<span class="state-pill state-running">running</span>',
    queued: `<span class="state-pill state-queued">queued${item.position != null ? ` #${item.position + 1}` : ''}</span>`,
    held: '<span class="state-pill state-held">held</span>',
  }[item.state] || item.state;

  const conv = `${item.src_ext} → ${item.dst_ext}`;
  const stone = item.stone ? '✓' : '';
  const progress = item.state === 'running' ? `${item.progress}%` : '—';

  // Action buttons depend on state. Stop always shown. Skip + Pause only on
  // queued. Resume only on held. Buttons carry data-action + data-id so a
  // single delegated click handler covers them all.
  let actions = '';
  if (item.state === 'queued') {
    actions += `<button class="btn btn-ghost btn-xs" data-action="skip" data-id="${item.job_id}" title="Move to end of queue">Skip</button>`;
    actions += `<button class="btn btn-ghost btn-xs" data-action="pause" data-id="${item.job_id}" title="Hold out of rotation">Pause</button>`;
  } else if (item.state === 'held') {
    actions += `<button class="btn btn-ghost btn-xs" data-action="resume" data-id="${item.job_id}" title="Return to queue">Resume</button>`;
  }
  actions += `<button class="btn btn-ghost btn-xs danger" data-action="stop" data-id="${item.job_id}" title="Stop — user sees an unknown error">Stop</button>`;

  return `<tr data-id="${item.job_id}">
    <td>${escapeHtml(item.username)}</td>
    <td>${stateLabel}</td>
    <td><code>${escapeHtml(conv)}</code></td>
    <td class="center">${stone}</td>
    <td>${progress}</td>
    <td class="muted small">${fmtAgo(item.created_at)}</td>
    <td class="row-actions">${actions}</td>
  </tr>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

async function refreshActive() {
  try {
    const items = await window.api.get('/admin/transmutes/active');
    const tbody = document.getElementById('active-tbody');
    if (!items.length) {
      tbody.innerHTML = '';
      document.getElementById('active-count').textContent = 'No active transmutes.';
    } else {
      tbody.innerHTML = items.map(rowHtml).join('');
      document.getElementById('active-count').textContent =
        `${items.length} active transmute${items.length === 1 ? '' : 's'}.`;
    }
  } catch (e) {
    console.warn('active refresh failed', e);
  }
}

async function onActionClick(ev) {
  const btn = ev.target.closest('button[data-action]');
  if (!btn) return;
  const id = btn.dataset.id;
  const action = btn.dataset.action;
  // Confirm Stop — it's the only one users see (an "unknown error" on
  // their side). Skip / Pause / Resume are silent for the user.
  if (action === 'stop' && !confirm('Stop this transmute? The user will see it as a failed conversion.')) {
    return;
  }
  btn.disabled = true;
  try {
    await window.api.post(`/admin/transmutes/${id}/${action}`);
    // Optimistically refresh both panels — the row will reflect its new
    // state (or disappear, for stop) on the next render.
    await refreshActive();
    if (action === 'stop') await refreshStats();
  } catch (e) {
    alert(e.detail || 'Action failed.');
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('stats-window').addEventListener('change', (ev) => {
    _currentWindow = ev.target.value;
    refreshStats();
  });
  document.getElementById('active-tbody').addEventListener('click', onActionClick);
  refreshStats();
  refreshActive();
  _pollTimer = setInterval(() => {
    refreshActive();
    // Stats refresh on the same tick — cheap (single COUNT) and keeps
    // the top card moving as Done jobs accumulate.
    refreshStats();
  }, POLL_MS);
});

window.addEventListener('beforeunload', () => {
  if (_pollTimer) clearInterval(_pollTimer);
});
