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

document.getElementById('signup-form').addEventListener('submit', async (e) => {
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
