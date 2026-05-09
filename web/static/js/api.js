// Tiny fetch wrapper with cookie-based auth.
window.api = {
  async get(path) {
    const r = await fetch('/api/v1' + path, { credentials: 'same-origin' });
    if (r.status === 401) { location.href = '/signin'; return; }
    if (!r.ok) throw await r.json().catch(() => ({ detail: r.statusText }));
    return r.status === 204 ? null : r.json();
  },
  async post(path, body, isForm = false) {
    const opts = { method: 'POST', credentials: 'same-origin' };
    if (isForm) {
      opts.body = body;
    } else {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = body == null ? null : JSON.stringify(body);
    }
    const r = await fetch('/api/v1' + path, opts);
    if (!r.ok) throw await r.json().catch(() => ({ detail: r.statusText }));
    return r.status === 204 ? null : r.json();
  },
  async patch(path, body) {
    const r = await fetch('/api/v1' + path, {
      method: 'PATCH', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw await r.json().catch(() => ({ detail: r.statusText }));
    return r.status === 204 ? null : r.json();
  },
  async del(path) {
    const r = await fetch('/api/v1' + path, { method: 'DELETE', credentials: 'same-origin' });
    if (!r.ok) throw await r.json().catch(() => ({ detail: r.statusText }));
    return r.status === 204 ? null : r.json();
  },
};

// Global logout button.
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('logout-btn');
  if (btn) btn.addEventListener('click', async () => {
    try { await fetch('/api/v1/auth/logout', { method: 'POST', credentials: 'same-origin' }); } catch (e) {}
    location.href = '/signin';
  });
});
