// Tiny fetch wrapper with cookie-based auth.
//
// On 401: try POST /auth/refresh once (which uses the long-lived
// refresh-token cookie), then retry the original request. Only if the
// retry still 401s do we bounce to /signin. This keeps long-idle browser
// sessions usable — the 15-minute access cookie expires silently and the
// next API call recovers transparently instead of failing.

let _refreshInFlight = null;

async function _refresh() {
  // Coalesce concurrent 401s onto a single refresh round-trip — N requests
  // that all see expired cookies don't trigger N parallel refresh calls.
  if (!_refreshInFlight) {
    _refreshInFlight = (async () => {
      try {
        const r = await fetch('/api/v1/auth/refresh', {
          method: 'POST',
          credentials: 'same-origin',
        });
        return r.ok;
      } catch (e) {
        return false;
      } finally {
        // Hold the result for ~1s so an immediate burst of retries
        // shares the same refresh, but a later 401 starts fresh.
        setTimeout(() => { _refreshInFlight = null; }, 1000);
      }
    })();
  }
  return _refreshInFlight;
}

async function _send(path, opts) {
  const url = '/api/v1' + path;
  let r = await fetch(url, opts);
  if (r.status === 401) {
    const refreshed = await _refresh();
    if (refreshed) {
      r = await fetch(url, opts);
    }
    if (r.status === 401) {
      // Refresh token also dead — full re-auth required.
      location.href = '/signin';
      return;
    }
  }
  if (!r.ok) throw await r.json().catch(() => ({ detail: r.statusText }));
  return r.status === 204 ? null : r.json();
}

window.api = {
  async get(path) {
    return _send(path, { method: 'GET', credentials: 'same-origin' });
  },
  async post(path, body, isForm = false) {
    const opts = { method: 'POST', credentials: 'same-origin' };
    if (isForm) {
      // FormData (with File/Blob entries) is re-readable across fetch
      // calls — the browser streams from each entry on send. So a 401
      // retry on a 5 MB upload safely re-uploads.
      opts.body = body;
    } else {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = body == null ? null : JSON.stringify(body);
    }
    return _send(path, opts);
  },
  async patch(path, body) {
    return _send(path, {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  },
  async del(path) {
    return _send(path, { method: 'DELETE', credentials: 'same-origin' });
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
