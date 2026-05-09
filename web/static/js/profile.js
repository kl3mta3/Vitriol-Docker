document.getElementById('profile-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  const msg = document.getElementById('profile-msg');
  msg.hidden = true;
  try {
    await api.patch('/me', data);
    msg.textContent = 'Saved.';
    msg.hidden = false;
  } catch (ex) {
    msg.textContent = ex.detail || 'Save failed';
    msg.hidden = false;
  }
});

document.getElementById('password-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  const msg = document.getElementById('password-msg');
  msg.hidden = true;
  try {
    const r = await api.post('/me/password', data);
    msg.textContent = r.message;
    msg.hidden = false;
    e.target.reset();
  } catch (ex) {
    msg.textContent = ex.detail || 'Failed';
    msg.hidden = false;
  }
});

async function refreshKeys() {
  const list = document.getElementById('apikey-list');
  list.innerHTML = '';
  const keys = await api.get('/me/api-keys');
  for (const k of keys) {
    const li = document.createElement('li');
    const tag = document.createElement('span');
    tag.textContent = `${k.name} — ${k.prefix} ${k.revoked_at ? '(revoked)' : ''}`;
    li.appendChild(tag);
    if (!k.revoked_at) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-ghost';
      btn.textContent = 'Revoke';
      btn.onclick = async () => { await api.del(`/me/api-keys/${k.id}`); refreshKeys(); };
      li.appendChild(btn);
    }
    list.appendChild(li);
  }
}

document.getElementById('apikey-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  const r = await api.post('/me/api-keys', data);
  const out = document.getElementById('apikey-secret');
  out.textContent = `Save this — it won't be shown again: ${r.secret}`;
  out.hidden = false;
  e.target.reset();
  refreshKeys();
});

const reqBtn = document.getElementById('request-access');
if (reqBtn) reqBtn.addEventListener('click', async () => {
  const msg = document.getElementById('request-msg');
  try {
    const r = await api.post('/me/request-access', {});
    msg.textContent = r.message; msg.hidden = false;
  } catch (ex) {
    msg.textContent = ex.detail || 'Failed'; msg.hidden = false;
  }
});

refreshKeys();

// ---------- Theme picker ----------
(function () {
  const picker = document.getElementById('theme-picker');
  if (!picker) return;
  const msg = document.getElementById('theme-msg');
  const current = picker.dataset.current || 'default';
  const swatches = picker.querySelectorAll('.theme-swatch');

  function markActive(theme) {
    swatches.forEach(s => s.classList.toggle('active', s.dataset.theme === theme));
  }
  function applyTheme(theme) {
    if (theme && theme !== 'default') {
      document.documentElement.setAttribute('data-theme', theme);
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
  }

  markActive(current);

  swatches.forEach(s => {
    s.addEventListener('click', async (e) => {
      e.preventDefault();
      const theme = s.dataset.theme;
      // Optimistic — flip the look immediately, then persist.
      applyTheme(theme);
      markActive(theme);
      msg.hidden = true;
      try {
        await api.patch('/me', { theme });
        msg.textContent = 'Theme saved.';
        msg.hidden = false;
      } catch (ex) {
        applyTheme(current);
        markActive(current);
        msg.textContent = ex.detail || 'Could not save theme';
        msg.hidden = false;
      }
    });
  });
})();
