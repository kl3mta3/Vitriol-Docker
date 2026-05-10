// Pull SSO providers from the same policy endpoint /signin uses and
// render "Sign up with <provider>" buttons. Clicking one kicks off
// the normal SSO start flow; the callback creates a Vitriol account
// for the IdP-supplied email + name if no match exists (gated on the
// admin's allow_signup toggle, same as password signup).
(async function loadSsoProviders() {
  let policy = { providers: [] };
  try { policy = await api.get('/auth/policy'); } catch (e) {}
  const row = document.getElementById('sso-row');
  if (!row || !policy.providers || !policy.providers.length) return;
  row.hidden = false;
  // Reveal the "— or sign up with —" divider when both a password
  // form AND SSO buttons are visible. (When password-signup is off,
  // the template skips the form entirely and we just show the SSO
  // buttons — no divider needed.)
  const divider = document.getElementById('signup-divider');
  const formStillThere = !!document.getElementById('signup-form');
  if (divider && formStillThere) divider.hidden = false;
  for (const p of policy.providers) {
    const a = document.createElement('a');
    a.className = 'btn btn-secondary';
    a.href = `/api/v1/auth/sso/${p.id}/start`;
    // Replace "Continue with X" → "Sign up with X" on this page; the
    // start endpoint is the same in either case but the verb signals
    // intent to the user.
    a.textContent = (p.label || '').replace(/^Continue with /, 'Sign up with ') || `Sign up with ${p.id}`;
    row.appendChild(a);
  }
})();

// FastAPI returns 422 with `detail` as an array of {loc, msg, ...} — flatten
// so the user sees "email: not a valid address" instead of "[object Object]".
function formatError(ex) {
  if (!ex) return 'Sign up failed';
  const d = ex.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map(e => {
      const field = Array.isArray(e.loc) ? e.loc.filter(p => p !== 'body').join('.') : '';
      return field ? `${field}: ${e.msg}` : e.msg;
    }).join('; ');
  }
  return ex.message || 'Sign up failed';
}

// The password-signup form is conditionally rendered (only when the
// admin has `password_signin_enabled = true`). When the form isn't
// in the DOM, skip the submit-listener wiring — the SSO buttons are
// the only path forward.
const _signupFormEl = document.getElementById('signup-form');
if (_signupFormEl) _signupFormEl.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  const err = document.getElementById('signup-error');
  const ok = document.getElementById('signup-ok');
  err.hidden = ok.hidden = true;
  try {
    const r = await api.post('/auth/signup', data);
    ok.textContent = r.message;
    ok.hidden = false;
    // If the message says they can sign in immediately, bounce them there
    // after a brief pause so they can read it. Pending / verification
    // messages stay on the page so the user reads them carefully.
    if (/sign in now/i.test(r.message)) {
      setTimeout(() => { location.href = '/signin'; }, 2500);
    }
  } catch (ex) {
    err.textContent = formatError(ex);
    err.hidden = false;
  }
});
