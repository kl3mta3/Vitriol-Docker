// Reset-password landing page. Reads the ?token=... query param the
// email link contains, posts it + the new password to
// /auth/password-reset/confirm. Same page handles both flows:
//   - traditional forgot-password (signed-out user from /forgot)
//   - profile-page "reset via email" (signed-in user who triggered
//     the email from their profile)
//
// No token in the URL = the user reached this page some other way
// (typed the URL, bookmark, broken link). Show the "bad token"
// message and a path back to /forgot rather than letting them submit
// against nothing.

(function () {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  const form  = document.getElementById('reset-form');
  const bad   = document.getElementById('reset-bad-token');
  const msg   = document.getElementById('reset-msg');

  if (!token) {
    form.hidden = true;
    bad.hidden = false;
    return;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    msg.hidden = true;
    msg.className = 'error small';
    const newPw  = form.new_password.value;
    const confirm = form.confirm_password.value;
    if (newPw !== confirm) {
      msg.textContent = "Passwords don't match.";
      msg.hidden = false;
      return;
    }
    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    try {
      await api.post('/auth/password-reset/confirm', {
        token,
        new_password: newPw,
      });
      // Success — swap the form for a success message + send to /signin.
      form.hidden = true;
      msg.textContent = 'Password updated. Redirecting to sign in…';
      msg.className = 'ok small';
      msg.hidden = false;
      setTimeout(() => { window.location.href = '/signin'; }, 1500);
    } catch (ex) {
      // Token expired / already-used / invalid all surface here.
      // The server's PasswordResetToken consumer returns a specific
      // message that's worth showing verbatim.
      msg.textContent = ex.detail || 'Reset failed — the link may have expired.';
      msg.hidden = false;
      submitBtn.disabled = false;
    }
  });
})();
