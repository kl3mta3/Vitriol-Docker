// One round-trip pulls everything the sign-in page needs to show
// dynamically: SSO buttons + whether to expose the Sign up link.
(async function loadPolicy() {
  let policy = { allow_signup: false, providers: [] };
  try { policy = await api.get('/auth/policy'); } catch (e) {}

  const row = document.getElementById('sso-row');
  if (row && policy.providers && policy.providers.length) {
    row.hidden = false;
    for (const p of policy.providers) {
      const a = document.createElement('a');
      a.className = 'btn btn-secondary';
      a.href = `/api/v1/auth/sso/${p.id}/start`;
      a.textContent = p.label;
      row.appendChild(a);
    }
  }

  const signupLink = document.getElementById('signup-link');
  if (signupLink && policy.allow_signup) signupLink.hidden = false;
})();

function formatError(ex) {
  if (!ex) return 'Sign in failed';
  const d = ex.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map(e => {
      const field = Array.isArray(e.loc) ? e.loc.filter(p => p !== 'body').join('.') : '';
      return field ? `${field}: ${e.msg}` : e.msg;
    }).join('; ');
  }
  return ex.message || 'Sign in failed';
}

// Super admin escape hatch — when password sign-in is master-disabled,
// the signin template renders the form with [hidden] and surfaces a
// small "Use password (super admin)" link. Clicking it reveals the
// form so the operator can sign in with their password (the backend
// at /auth/signin allows this for super admin even when the master
// toggle is off — see auth.py for the carve-out).
const revealBtn = document.getElementById('reveal-password-form');
if (revealBtn) {
  revealBtn.addEventListener('click', (e) => {
    e.preventDefault();
    const form = document.getElementById('signin-form');
    if (form) form.hidden = false;
    // The link is single-use — hide it once the form is showing so it
    // doesn't compete visually with the form's Sign in button.
    revealBtn.closest('p').hidden = true;
    // Also hide the "Password sign-in is disabled" paragraph above it.
    const disabledNotice = revealBtn.closest('p').previousElementSibling;
    if (disabledNotice && disabledNotice.tagName === 'P') disabledNotice.hidden = true;
    // Move focus into the form so the user can start typing immediately.
    const ident = form && form.querySelector('input[name="identifier"]');
    if (ident) ident.focus();
  });
}

document.getElementById('signin-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  const err = document.getElementById('signin-error');
  err.hidden = true;
  try {
    await api.post('/auth/signin', data);
    location.href = '/';
  } catch (ex) {
    err.textContent = formatError(ex);
    err.hidden = false;
  }
});
