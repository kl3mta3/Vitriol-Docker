const form = document.getElementById('server-form');
const msg = document.getElementById('server-msg');

function csv(s) {
  return s ? s.split(',').map(x => x.trim()).filter(Boolean) : [];
}

// ---- Max file size: human units (MB / GB) backed by a bytes column. ----
const MB = 1024 * 1024;
const GB = 1024 * MB;
const sizeValueEl = document.getElementById('max-file-size-value');
const sizeUnitEl  = document.getElementById('max-file-size-unit');
const sizeBytesEl = document.getElementById('max-file-size-bytes-display');

function bytesToReadable(bytes) {
  // Prefer GB only when the value lands on an exact GB boundary — keeps
  // odd byte counts (e.g. legacy 1073741824) showing as the GB they were
  // meant to be, while still showing 100 MB as MB.
  if (bytes && bytes % GB === 0 && bytes >= GB) return { value: bytes / GB, unit: 'GB' };
  return { value: Math.max(1, Math.round((bytes || 0) / MB)), unit: 'MB' };
}

function readableToBytes() {
  const v = Number(sizeValueEl.value);
  if (!isFinite(v) || v <= 0) return null;
  return v * (sizeUnitEl.value === 'GB' ? GB : MB);
}

function updateBytesDisplay() {
  const b = readableToBytes();
  sizeBytesEl.textContent = b == null ? '' : `= ${b.toLocaleString()} bytes`;
}

if (sizeValueEl && sizeUnitEl) {
  sizeValueEl.addEventListener('input', updateBytesDisplay);
  sizeUnitEl.addEventListener('change', updateBytesDisplay);
}

async function load() {
  const s = await api.get('/server/settings');
  for (const [k, v] of Object.entries(s)) {
    const el = form.elements[k];
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!v;
    else if (v != null) el.value = v;
  }
  // Signup default role select takes the encoded form 'builtin:<role>'
  // or 'custom:<id>'. Populate the custom options first, then select
  // whichever option the saved settings point at.
  await populateSignupRoleSelect(s);
  // Max file size — populate the readable picker from the bytes value.
  if (sizeValueEl && sizeUnitEl) {
    const r = bytesToReadable(s.max_file_size_bytes || 0);
    sizeValueEl.value = r.value;
    sizeUnitEl.value = r.unit;
    updateBytesDisplay();
  }
  // List fields stored as JSON.
  try {
    form.elements['disabled_input_formats'].value = JSON.parse(s.disabled_input_formats_json || '[]').join(',');
    form.elements['disabled_output_formats'].value = JSON.parse(s.disabled_output_formats_json || '[]').join(',');
  } catch (e) {}

  // Show the absolute OIDC redirect URI so admins can paste it into their IdP.
  const ru = document.getElementById('oidc-redirect-uri');
  if (ru) {
    const base = (s.public_base_url || location.origin).replace(/\/$/, '');
    ru.textContent = `${base}/api/v1/auth/sso/oidc/callback`;
  }

  // SMTP status pill + signup-without-SMTP warning banner.
  const smtpConfigured = !!(s.smtp_host && s.smtp_from);
  const pill = document.getElementById('smtp-status-pill');
  if (pill) {
    pill.textContent = smtpConfigured ? 'configured' : 'not configured';
    pill.className = 'status-pill ' + (smtpConfigured ? 'ok' : 'missing');
  }
  const banner = document.getElementById('smtp-warning');
  if (banner) {
    // Only nag about SMTP when sign-up is on AND the verification email
    // is required. Operators who explicitly turned verification off don't
    // need SMTP for sign-ups.
    const showBanner = !!s.allow_signup && !smtpConfigured && !!s.require_email_verification;
    banner.hidden = !showBanner;
    if (showBanner) {
      const det = document.getElementById('smtp');
      if (det) det.open = true;
    }
  }
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  msg.hidden = true;
  const data = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === 'checkbox') {
      data[el.name] = el.checked;
    } else if (el.value === '' && el.type === 'password') {
      // blank password = unchanged
      continue;
    } else if (el.value === '') {
      data[el.name] = null;
    } else if (el.type === 'number') {
      data[el.name] = Number(el.value);
    } else {
      data[el.name] = el.value;
    }
  }
  if (data.disabled_input_formats !== undefined) data.disabled_input_formats = csv(data.disabled_input_formats || '');
  if (data.disabled_output_formats !== undefined) data.disabled_output_formats = csv(data.disabled_output_formats || '');
  // Max file size — overwrite whatever the form serializer produced (which
  // is nothing, since the inputs have no `name`) with the computed bytes.
  const bytes = readableToBytes();
  if (bytes != null) data.max_file_size_bytes = bytes;

  // Signup default role — the visible select encodes 'builtin:<role>' or
  // 'custom:<id>'; the API takes them as two separate fields.
  const sel = document.getElementById('signup-role-select');
  if (sel) {
    const [kind, val] = (sel.value || 'builtin:viewer').split(':');
    if (kind === 'builtin') {
      data.signup_default_role = val;
      data.signup_default_custom_role_id = 0;
    } else {
      data.signup_default_custom_role_id = Number(val);
      // base_role gets aligned server-side automatically.
    }
  }
  try {
    await api.patch('/server/settings', data);
    msg.textContent = 'Saved.';
    msg.hidden = false;
  } catch (ex) {
    msg.textContent = ex.detail || 'Save failed';
    msg.hidden = false;
  }
});

document.getElementById('restart-btn').addEventListener('click', async () => {
  if (!confirm('Restart the server now?')) return;
  await api.post('/server/restart', {});
  alert('Restart scheduled.');
});

document.getElementById('refresh-certs').addEventListener('click', async () => {
  try {
    const r = await api.post('/server/refresh-certs', {});
    alert(r.message);
  } catch (ex) { alert(ex.detail || 'Failed'); }
});

// ---------- Server secret key ----------
const skDisplay = document.getElementById('secret-key-display');
const skReveal  = document.getElementById('secret-key-reveal');
const skCopy    = document.getElementById('secret-key-copy');
const skRotate  = document.getElementById('secret-key-rotate');
const skMsg     = document.getElementById('secret-key-msg');
let skRevealed = false;

if (skReveal) skReveal.addEventListener('click', async () => {
  if (skRevealed) {
    // Re-mask.
    skDisplay.value = '••••••••••••••••••••••••••••••••';
    skDisplay.type = 'password';
    skReveal.textContent = 'Reveal';
    skCopy.hidden = true;
    skRevealed = false;
    return;
  }
  try {
    const r = await api.get('/server/secret-key');
    skDisplay.value = r.secret_key;
    skDisplay.type = 'text';
    skReveal.textContent = 'Hide';
    skCopy.hidden = false;
    skRevealed = true;
  } catch (ex) {
    skMsg.textContent = ex.detail || 'Could not load secret key';
    skMsg.className = 'error small'; skMsg.hidden = false;
  }
});

if (skCopy) skCopy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(skDisplay.value);
    skMsg.textContent = 'Copied to clipboard.'; skMsg.className = 'ok small'; skMsg.hidden = false;
    setTimeout(() => { skMsg.hidden = true; }, 2000);
  } catch (e) {
    skDisplay.select();
    skMsg.textContent = 'Press Ctrl+C to copy.'; skMsg.className = 'muted small'; skMsg.hidden = false;
  }
});

if (skRotate) skRotate.addEventListener('click', async () => {
  if (!confirm(
    'Regenerate the server secret key?\n\n' +
    '• All current sessions (including yours) will be signed out.\n' +
    '• Stored SMTP / OAuth / SSL secrets will be re-encrypted with the new key.\n' +
    '• The container will restart immediately.\n\n' +
    'Continue?'
  )) return;
  skRotate.disabled = true;
  skRotate.textContent = 'Rotating…';
  try {
    const r = await api.post('/server/secret-key/rotate', {});
    skMsg.textContent = r.message; skMsg.className = 'ok small'; skMsg.hidden = false;
    // Server is restarting; wait a moment then bounce to /signin.
    setTimeout(() => { location.href = '/signin'; }, 4000);
  } catch (ex) {
    skMsg.textContent = ex.detail || 'Rotation failed'; skMsg.className = 'error small'; skMsg.hidden = false;
    skRotate.disabled = false;
    skRotate.textContent = 'Regenerate';
  }
});

async function populateSignupRoleSelect(s) {
  const sel = document.getElementById('signup-role-select');
  if (!sel) return;
  // Wipe any leftover custom options from a previous load.
  Array.from(sel.querySelectorAll('option[data-custom]')).forEach(o => o.remove());
  let customRoles = [];
  try { customRoles = await api.get('/roles'); } catch (e) { customRoles = []; }
  if (Array.isArray(customRoles) && customRoles.length) {
    const sep = document.createElement('option');
    sep.disabled = true;
    sep.dataset.custom = '';
    sep.textContent = '— custom roles —';
    sel.appendChild(sep);
    for (const cr of customRoles) {
      const opt = document.createElement('option');
      opt.value = `custom:${cr.id}`;
      opt.textContent = `${cr.name}  (base: ${cr.base_role})`;
      opt.dataset.custom = '';
      sel.appendChild(opt);
    }
  }
  sel.value = s.signup_default_custom_role_id
    ? `custom:${s.signup_default_custom_role_id}`
    : `builtin:${s.signup_default_role}`;
}

// Send a test email through the saved SMTP settings — no DB writes.
const testBtn = document.getElementById('smtp-test-btn');
if (testBtn) testBtn.addEventListener('click', async () => {
  const msg = document.getElementById('smtp-test-msg');
  const to = document.getElementById('smtp-test-to').value.trim();
  msg.hidden = true;
  msg.className = 'ok';
  testBtn.disabled = true;
  testBtn.textContent = 'Sending…';
  try {
    const r = await api.post('/server/test-email', { to });
    msg.textContent = r.message;
    msg.className = 'ok';
  } catch (ex) {
    msg.textContent = ex.detail || 'Failed';
    msg.className = 'error';
  } finally {
    msg.hidden = false;
    testBtn.disabled = false;
    testBtn.textContent = 'Send test email';
  }
});

load();
