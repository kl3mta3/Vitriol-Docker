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

  // ---- Output retention (per role) — populate the 3-row grid ----
  const retention = (s.output_retention && typeof s.output_retention === 'object')
    ? s.output_retention
    : {};
  document.querySelectorAll('.retention-table tr[data-role]').forEach(tr => {
    const role = tr.dataset.role;
    const cfg = retention[role] || {};
    const setVal = (field, val) => {
      const el = tr.querySelector(`[data-field="${field}"]`);
      if (!el) return;
      if (el.type === 'checkbox') el.checked = !!val;
      else if (val != null) el.value = val;
    };
    setVal('max_files', cfg.max_files ?? 0);
    setVal('max_age', cfg.max_age ?? 0);
    setVal('age_unit', cfg.age_unit || 'days');
    setVal('delete_on_download', cfg.delete_on_download);
  });

  // ---- SSL cert-pull mode wiring ----
  const sslMode = document.getElementById('ssl-mode-select');
  if (sslMode) {
    sslMode.value = s.ssl_cert_pull_mode || 'webhook';
    updateSslModeBlocks();
    // Script field is a <textarea> with a name; the for-each loop above
    // already populated form.elements['ssl_cert_pull_script']. Make sure
    // empty strings stay empty rather than showing 'null'.
    if (s.ssl_cert_pull_script == null) form.elements['ssl_cert_pull_script'].value = '';
  }
  // Last-run readout + status pill.
  const lastRunEl = document.getElementById('ssl-last-run');
  if (lastRunEl) {
    if (s.ssl_cert_pull_last_run_at) {
      const when = new Date(s.ssl_cert_pull_last_run_at).toLocaleString();
      lastRunEl.textContent = `${when} — ${s.ssl_cert_pull_last_status || '(no status)'}`;
    } else {
      lastRunEl.textContent = 'never';
    }
  }
  const sslPill = document.getElementById('ssl-last-pill');
  if (sslPill) {
    if (!s.ssl_cert_pull_last_run_at) {
      sslPill.textContent = '';
    } else if ((s.ssl_cert_pull_last_status || '').startsWith('failed')) {
      sslPill.textContent = 'last: failed';
      sslPill.className = 'status-pill missing';
    } else {
      sslPill.textContent = 'last: ok';
      sslPill.className = 'status-pill ok';
    }
  }

  // SMTP / Discord status pills — reflect last-test state so a green
  // "configured" sticks across reloads (vs. the old "configured the
  // moment fields are non-empty" which was misleading after a failed
  // test).
  const smtpConfigured = !!(s.smtp_host && s.smtp_from);
  paintServicePill('smtp-status-pill', smtpConfigured, s.smtp_last_test_ok);
  const discordConfigured = !!s.discord_webhook_url;
  paintServicePill('discord-status-pill', discordConfigured, s.discord_last_test_ok);
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

  // Output retention — read the per-role grid into the JSON payload
  // the server expects.
  const retention = {};
  document.querySelectorAll('.retention-table tr[data-role]').forEach(tr => {
    const role = tr.dataset.role;
    const get = (field) => tr.querySelector(`[data-field="${field}"]`);
    retention[role] = {
      max_files: Number(get('max_files').value || 0),
      max_age:   Number(get('max_age').value || 0),
      age_unit:  get('age_unit').value || 'days',
      delete_on_download: get('delete_on_download').checked,
    };
  });
  if (Object.keys(retention).length) data.output_retention = retention;

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

// ---- SSL mode picker — show only the active mode's controls. ----
function updateSslModeBlocks() {
  const sel = document.getElementById('ssl-mode-select');
  const mode = sel ? sel.value : 'webhook';
  const wb = document.getElementById('ssl-mode-webhook');
  const sc = document.getElementById('ssl-mode-script');
  if (wb) wb.hidden = mode !== 'webhook';
  if (sc) sc.hidden = mode !== 'script';
}
const _sslSel = document.getElementById('ssl-mode-select');
if (_sslSel) _sslSel.addEventListener('change', updateSslModeBlocks);

document.getElementById('refresh-certs').addEventListener('click', async () => {
  const btn = document.getElementById('refresh-certs');
  const msg = document.getElementById('refresh-certs-msg');
  btn.disabled = true;
  const original = btn.textContent;
  if (msg) { msg.hidden = true; msg.className = 'ok small'; }
  try {
    // Auto-save the current form state first — otherwise users who
    // changed mode/url/script in the form but didn't click "Save"
    // hit "webhook URL not set" or worse. Saving on every pull is
    // cheap and matches the user's mental model ("the form is the
    // source of truth").
    btn.textContent = 'Saving…';
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    // submit is async — give it a tick to land. The handler is
    // async/await, so we have to wait for the patch round-trip
    // explicitly. Use a tiny poll on the message element instead
    // of restructuring the submit handler.
    await new Promise(r => setTimeout(r, 350));
    btn.textContent = 'Pulling…';
    const r = await api.post('/server/refresh-certs', {});
    if (msg) {
      msg.textContent = r.message || 'Done.';
      msg.className = 'ok small';
      msg.hidden = false;
    }
    await load();
  } catch (ex) {
    if (msg) {
      msg.textContent = ex.detail || 'Pull failed';
      msg.className = 'error small';
      msg.hidden = false;
    } else {
      alert(ex.detail || 'Failed');
    }
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
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

// ============================================================
// OIDC providers — list + click-to-edit + delete
// ============================================================
//
// Same pattern as the custom-roles list: fetch, render rows, clicking a
// row (or the inline Edit-style icon) opens a modal pre-populated with
// that provider's fields. The "+ Add" button opens the same dialog
// blank. Save POSTs (new) or PATCHes (existing); Delete only shows when
// editing an existing row.

const oidcTbody = document.getElementById('oidc-tbody');
const oidcDialog = document.getElementById('oidc-form-dialog');
const oidcForm = document.getElementById('oidc-form');
const oidcAddBtn = document.getElementById('oidc-add-btn');
const oidcRedirectPreview = document.getElementById('oidc-form-redirect');

async function loadOidcProviders() {
  if (!oidcTbody) return;
  let rows = [];
  try { rows = await api.get('/server/oidc-providers'); } catch (_) { rows = []; }
  oidcTbody.innerHTML = '';
  if (!Array.isArray(rows) || rows.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5" class="empty-state">No OIDC providers yet. Click "+ Add OIDC provider" below.</td>';
    oidcTbody.appendChild(tr);
    return;
  }
  for (const p of rows) {
    const tr = document.createElement('tr');
    tr.className = 'user-row clickable';
    tr.dataset.id = p.id;
    tr.innerHTML = `
      <td>${escapeHtmlSimple(p.display_name)}</td>
      <td><code>${escapeHtmlSimple(p.slug)}</code></td>
      <td class="muted small" style="max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtmlSimple(p.issuer)}</td>
      <td>${p.enabled ? '<span class="status-pill ok">on</span>' : '<span class="status-pill missing">off</span>'}</td>
      <td class="row-manage-cell"><button type="button" class="btn btn-secondary btn-manage">Edit</button></td>
    `;
    tr.addEventListener('click', () => openOidcForm(p));
    oidcTbody.appendChild(tr);
  }
}

function escapeHtmlSimple(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function openOidcForm(p) {
  // p === null → blank dialog for "Add". p === object → populated for "Edit".
  oidcForm.id.value = p ? p.id : '';
  oidcForm.display_name.value = p ? p.display_name : '';
  oidcForm.slug.value = p ? p.slug : '';
  oidcForm.issuer.value = p ? p.issuer : '';
  oidcForm.scopes.value = p ? (p.scopes || 'openid email profile') : 'openid email profile';
  oidcForm.client_id.value = p ? p.client_id : '';
  oidcForm.client_secret.value = '';
  oidcForm.enabled.checked = p ? !!p.enabled : true;
  document.getElementById('oidc-form-title').textContent = p ? `Edit — ${p.display_name}` : 'Add OIDC provider';
  document.getElementById('oidc-form-delete').hidden = !p;
  document.getElementById('oidc-form-msg').hidden = true;
  updateOidcRedirectPreview();
  oidcDialog.showModal();
}

function updateOidcRedirectPreview() {
  if (!oidcRedirectPreview) return;
  const slug = (oidcForm.slug.value || '').trim() ||
               (oidcForm.display_name.value || '').toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') ||
               '<slug>';
  const base = (form.elements['public_base_url'] && form.elements['public_base_url'].value) || location.origin;
  oidcRedirectPreview.textContent = `${base.replace(/\/+$/, '')}/api/v1/auth/sso/${slug}/callback`;
}
if (oidcForm) {
  oidcForm.slug.addEventListener('input', updateOidcRedirectPreview);
  oidcForm.display_name.addEventListener('input', updateOidcRedirectPreview);
}

if (oidcAddBtn) oidcAddBtn.addEventListener('click', () => openOidcForm(null));

if (oidcForm) oidcForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = oidcForm.id.value;
  const msg = document.getElementById('oidc-form-msg');
  msg.hidden = true;
  // Build payload — only include client_secret if the user actually
  // typed something (blank means "don't change" on edit; required on
  // create — server validates).
  const payload = {
    slug: oidcForm.slug.value.trim() || null,
    display_name: oidcForm.display_name.value.trim(),
    issuer: oidcForm.issuer.value.trim(),
    client_id: oidcForm.client_id.value.trim(),
    scopes: oidcForm.scopes.value.trim() || 'openid email profile',
    enabled: oidcForm.enabled.checked,
  };
  if (oidcForm.client_secret.value) payload.client_secret = oidcForm.client_secret.value;
  try {
    if (id) {
      await api.patch(`/server/oidc-providers/${id}`, payload);
    } else {
      // New providers REQUIRE a secret — surface inline rather than 400.
      if (!payload.client_secret) {
        msg.textContent = 'Client secret is required for new providers.';
        msg.className = 'error small';
        msg.hidden = false;
        return;
      }
      await api.post('/server/oidc-providers', payload);
    }
    oidcDialog.close();
    await loadOidcProviders();
  } catch (ex) {
    msg.textContent = (ex && ex.detail) || 'Save failed';
    msg.className = 'error small';
    msg.hidden = false;
  }
});

if (document.getElementById('oidc-form-delete')) {
  document.getElementById('oidc-form-delete').addEventListener('click', async () => {
    const id = oidcForm.id.value;
    if (!id) return;
    const name = oidcForm.display_name.value || 'this provider';
    if (!confirm(`Delete ${name}? Users who signed in via it will lose that link.`)) return;
    try {
      await api.del(`/server/oidc-providers/${id}`);
      oidcDialog.close();
      await loadOidcProviders();
    } catch (ex) {
      const msg = document.getElementById('oidc-form-msg');
      msg.textContent = (ex && ex.detail) || 'Delete failed';
      msg.className = 'error small';
      msg.hidden = false;
    }
  });
}

// Kick off the initial load when the SSO section is in the DOM.
if (oidcTbody) loadOidcProviders();

// ============================================================
// SMTP + Discord pill state — uses last_test_ok/at to drive colour
// ============================================================
//
// Pill states:
//   ok        — host/from set AND last test passed → green "configured"
//   untested  — host/from set, never tested        → amber "untested"
//   missing   — required fields empty              → amber "not configured"
//   failed    — last test failed                   → red   "test failed"

function paintServicePill(pillId, configured, lastTestOk, untestedLabel = 'untested') {
  const pill = document.getElementById(pillId);
  if (!pill) return;
  if (!configured) {
    pill.textContent = 'not configured';
    pill.className = 'status-pill missing';
    return;
  }
  if (lastTestOk === true) {
    pill.textContent = 'configured';
    pill.className = 'status-pill ok';
    return;
  }
  if (lastTestOk === false) {
    pill.textContent = 'last test failed';
    pill.className = 'status-pill failed';
    return;
  }
  pill.textContent = untestedLabel;
  pill.className = 'status-pill warn';
}

// ============================================================
// Discord test button — same shape as SMTP test
// ============================================================

const discordTestBtn = document.getElementById('discord-test-btn');
if (discordTestBtn) discordTestBtn.addEventListener('click', async () => {
  const msg = document.getElementById('discord-test-msg');
  msg.hidden = true; msg.className = 'ok small';
  discordTestBtn.disabled = true;
  const original = discordTestBtn.textContent;
  discordTestBtn.textContent = 'Posting…';
  try {
    // Auto-save first so a freshly-pasted webhook URL is persisted
    // before we try to use it.
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    await new Promise(r => setTimeout(r, 350));
    const r = await api.post('/server/test-discord', {});
    msg.textContent = r.message || 'Posted.';
    msg.className = 'ok small';
    msg.hidden = false;
    await load();   // refresh pill state
  } catch (ex) {
    msg.textContent = (ex && ex.detail) || 'Failed';
    msg.className = 'error small';
    msg.hidden = false;
    await load();
  } finally {
    discordTestBtn.disabled = false;
    discordTestBtn.textContent = original;
  }
});

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
