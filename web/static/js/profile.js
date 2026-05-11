// Adapt the password card based on whether the current user has a
// password set. SSO-only users (signed up via Google/GitHub/OIDC with
// no password ever assigned) see "Set password" with no "Current"
// field. Users with a password see the standard "Reset password"
// flow that requires the current password as a re-auth step.
let _profileMe = null;
(async function configurePasswordCard() {
  try { _profileMe = await api.get('/me'); } catch (_) { return; }
  if (!_profileMe) return;
  const heading = document.getElementById('password-heading');
  const button  = document.getElementById('password-reset-btn');
  const ssoHint = document.getElementById('password-sso-hint');
  if (_profileMe.has_password) {
    if (heading) heading.textContent = 'Password';
    if (button) button.textContent = 'Reset password';
    if (ssoHint) ssoHint.hidden = true;
  } else {
    if (heading) heading.textContent = 'Set password';
    if (button) button.textContent = 'Set password';
    if (ssoHint) ssoHint.hidden = false;
  }
})();

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

// ----------------------------------------------- password modal flow
//
// Two paths, gated by the server's `password_reset_via_email` setting:
//
//   OFF (default): clicking "Reset password" opens an in-page modal.
//                  Submit POSTs /me/password with current+new password.
//                  Same security model as the old inline form.
//
//   ON:            clicking the button instead POSTs /auth/password-reset
//                  with the current user's identifier. Server emails a
//                  token-bearing link to /reset?token=…. User clicks the
//                  link, lands on the standalone /reset page, enters
//                  new password there. Modal never opens here.
//                  Adds an out-of-band check against session hijack.

const _passwordResetViaEmail = !!window.VITRIOL_PASSWORD_RESET_VIA_EMAIL;
const passwordModal = document.getElementById('password-modal');
const passwordForm  = document.getElementById('password-form');
const passwordTopMsg = document.getElementById('password-msg');

document.getElementById('password-reset-btn').addEventListener('click', async () => {
  if (passwordTopMsg) { passwordTopMsg.hidden = true; passwordTopMsg.className = 'ok'; }
  if (_passwordResetViaEmail) {
    // Email-link path. Use the existing public endpoint — works the
    // same as the forgot-password flow but kicked off from the
    // logged-in profile page. Identifier is the user's username
    // (the endpoint also accepts email).
    if (!_profileMe || !_profileMe.username) return;
    try {
      await api.post('/auth/password-reset', { identifier: _profileMe.username });
      if (passwordTopMsg) {
        passwordTopMsg.textContent = 'Check your email — we sent a password reset link. The link expires in 2 hours.';
        passwordTopMsg.className = 'ok small';
        passwordTopMsg.hidden = false;
      }
    } catch (ex) {
      if (passwordTopMsg) {
        passwordTopMsg.textContent = ex.detail || 'Could not send reset email.';
        passwordTopMsg.className = 'error small';
        passwordTopMsg.hidden = false;
      }
    }
    return;
  }
  // Direct-modal path. Show/hide the "current password" field based
  // on whether the user actually has a password (SSO-only users get
  // the simpler one-field form).
  const currentLabel = document.getElementById('password-current-label');
  const ssoHint = document.getElementById('password-modal-sso-hint');
  if (_profileMe && _profileMe.has_password) {
    if (currentLabel) currentLabel.hidden = false;
    if (ssoHint) ssoHint.hidden = true;
  } else {
    if (currentLabel) currentLabel.hidden = true;
    if (ssoHint) ssoHint.hidden = false;
  }
  // Clear any prior values so reopening doesn't show stale typing.
  passwordForm.reset();
  document.getElementById('password-modal-msg').hidden = true;
  passwordModal.showModal();
});

passwordForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = document.getElementById('password-modal-msg');
  msg.hidden = true;
  msg.className = 'error small';
  const newPw = passwordForm.new_password.value;
  const confirm = passwordForm.confirm_password.value;
  if (newPw !== confirm) {
    msg.textContent = "Passwords don't match.";
    msg.hidden = false;
    return;
  }
  const body = { new_password: newPw };
  // Only ship current_password when the user actually has one (the
  // field is hidden for SSO-only users and an empty string would fail
  // the server's "current password incorrect" check).
  if (_profileMe && _profileMe.has_password) {
    body.current_password = passwordForm.current_password.value;
  }
  try {
    const r = await api.post('/me/password', body);
    passwordModal.close();
    if (passwordTopMsg) {
      passwordTopMsg.textContent = r.message || 'Password updated.';
      passwordTopMsg.className = 'ok small';
      passwordTopMsg.hidden = false;
    }
    passwordForm.reset();
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

  // ---- Alchemy border toggle ----
  // Read/written via `data-show-border` on <html>. border.js checks
  // this attribute on every render and skips drawing when it's "off".
  // The MutationObserver in border.js (already watching data-theme)
  // also picks up data-show-border changes, so the border vanishes /
  // reappears instantly without a page reload.
  const borderToggle = document.getElementById('show-border-toggle');
  if (borderToggle) {
    // Apply initial state to <html> for immediate consistency on first
    // paint. The server-side template already rendered the attribute
    // (see base.html); this is belt-and-suspenders for the case where
    // the user toggles, navigates, then comes back.
    document.documentElement.setAttribute(
      'data-show-border', borderToggle.checked ? 'on' : 'off'
    );
    borderToggle.addEventListener('change', async () => {
      const on = borderToggle.checked;
      document.documentElement.setAttribute('data-show-border', on ? 'on' : 'off');
      msg.hidden = true;
      try {
        await api.patch('/me', { show_border: on });
        msg.textContent = on ? 'Border on.' : 'Border off.';
        msg.hidden = false;
      } catch (ex) {
        // Roll back the visual change if save failed.
        borderToggle.checked = !on;
        document.documentElement.setAttribute('data-show-border', !on ? 'on' : 'off');
        msg.textContent = ex.detail || 'Could not save border preference';
        msg.hidden = false;
      }
    });
  }
})();
