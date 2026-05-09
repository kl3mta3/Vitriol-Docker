document.getElementById('setup-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const err = document.getElementById('setup-error');
  err.hidden = true;

  const data = Object.fromEntries(new FormData(f));
  if (data.password !== data.password_confirm) {
    err.textContent = 'Passwords do not match.';
    err.hidden = false;
    return;
  }
  delete data.password_confirm;
  if (!data.email) delete data.email;

  try {
    await api.post('/auth/setup', data);
    location.href = '/admin/server';
  } catch (ex) {
    let msg = 'Setup failed';
    if (ex && typeof ex.detail === 'string') msg = ex.detail;
    else if (Array.isArray(ex && ex.detail)) {
      msg = ex.detail.map(e => {
        const field = Array.isArray(e.loc) ? e.loc.filter(p => p !== 'body').join('.') : '';
        return field ? `${field}: ${e.msg}` : e.msg;
      }).join('; ');
    }
    err.textContent = msg;
    err.hidden = false;
  }
});
