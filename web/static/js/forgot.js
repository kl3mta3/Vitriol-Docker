// Forgot-password page. Posts the identifier (username or email) to the
// public reset-password endpoint, which generates a token + emails a
// /reset?token=... link. Server response is deliberately the same
// whether the identifier matched a real account or not — prevents
// account enumeration. We mirror that in the UI by showing the same
// "if a matching account exists, we sent it" message in both cases.

document.getElementById('forgot-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  const msg = document.getElementById('forgot-msg');
  msg.hidden = true;
  msg.className = 'muted small';
  const submitBtn = e.target.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  try {
    await api.post('/auth/password-reset', data);
    msg.textContent = "If a matching account exists, we just sent a reset link. Check your email — the link expires in 2 hours.";
    msg.className = 'ok small';
    msg.hidden = false;
    // Disable the form on success so a confused user doesn't smash the
    // submit button and trigger a flood of identical reset emails.
    e.target.querySelector('input[name="identifier"]').disabled = true;
  } catch (ex) {
    // Server-side errors here are infrastructure problems (SMTP down,
    // DB unreachable, etc.), not user errors — surface verbatim so the
    // operator sees what's happening.
    msg.textContent = ex.detail || "Couldn't send the reset email — please try again in a moment.";
    msg.className = 'error small';
    msg.hidden = false;
    submitBtn.disabled = false;
  }
});
