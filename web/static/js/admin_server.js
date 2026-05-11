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

// Generalized size-input widget — same value+unit+display pattern as
// the max-file-size trio above, but allows 0 (= "unlimited"). Used by
// the max_output_size_bytes and max_storage_bytes fields on the Limits
// section. Returns the element refs so the load + save sites can drive
// the widget without re-querying the DOM each call.
function makeSizeWidget(valueId, unitId, displayId) {
  const valueEl = document.getElementById(valueId);
  const unitEl  = document.getElementById(unitId);
  const displayEl = document.getElementById(displayId);
  if (!valueEl || !unitEl) {
    return { valueEl: null, unitEl: null, displayEl: null,
             toBytes: () => 0, fromBytes: () => {}, update: () => {} };
  }
  const toBytes = () => {
    const v = Number(valueEl.value);
    if (!isFinite(v) || v < 0) return 0;
    return v * (unitEl.value === 'GB' ? GB : MB);
  };
  const update = () => {
    if (!displayEl) return;
    const b = toBytes();
    displayEl.textContent = b === 0 ? '= unlimited' : `= ${b.toLocaleString()} bytes`;
  };
  const fromBytes = (bytes) => {
    // 0 stays 0 (= unlimited). Pick GB when the value lands on an
    // exact GB boundary, otherwise MB. Round MB to keep odd values
    // tidy in the input.
    if (!bytes || bytes <= 0) {
      valueEl.value = 0;
      unitEl.value = 'MB';
    } else if (bytes % GB === 0 && bytes >= GB) {
      valueEl.value = bytes / GB;
      unitEl.value = 'GB';
    } else {
      valueEl.value = Math.max(0, Math.round(bytes / MB));
      unitEl.value = 'MB';
    }
    update();
  };
  valueEl.addEventListener('input', update);
  unitEl.addEventListener('change', update);
  return { valueEl, unitEl, displayEl, toBytes, fromBytes, update };
}

const outputSizeWidget = makeSizeWidget(
  'max-output-size-value', 'max-output-size-unit', 'max-output-size-bytes-display'
);
const storageWidget = makeSizeWidget(
  'max-storage-value', 'max-storage-unit', 'max-storage-bytes-display'
);

// Visual placeholder shown when a secret column is populated server-side.
// We never echo plaintext back, so the field stays empty on save — but an
// empty field looks identical to "I never saved this", which is confusing.
// This shows the operator that the slot has a value (and that leaving it
// blank preserves it). 12 bullets is just a visual cue, not the real length.
const SAVED_SECRET_PLACEHOLDER = '•••••••••••• saved — leave blank to keep';
const EMPTY_SECRET_PLACEHOLDER = '(none — paste a value to set)';

// Map each `<field>_set` boolean from the API response to the password
// input that represents it, so a populated DB column shows up as a clear
// "saved" placeholder instead of looking unset.
const SECRET_FIELD_MAP = {
  smtp_password_set:                            'smtp_password',
  oauth_google_secret_set:                      'oauth_google_client_secret',
  oauth_github_secret_set:                      'oauth_github_client_secret',
  ssl_cert_pull_webhook_secret_set:             'ssl_cert_pull_webhook_secret',
  ssl_cert_pull_webhook_header_value_set:       'ssl_cert_pull_webhook_header_value',
  s3_secret_key_set:                            's3_secret_key',
};

// Track which password fields the user has *actually* typed into during
// this page session. We only persist a password field's value on Save
// if this set contains it — defends against browser autofill silently
// dropping a saved credential into a password input on page load and
// then having form-wide submit ship that to the server as the new
// secret. (That was the root cause of "my SMTP password keeps getting
// clobbered after navigating between tabs.")
const _userTypedPasswordFields = new Set();
function _trackPasswordTyping() {
  document.querySelectorAll('#server-form input[type="password"]').forEach(el => {
    if (!el.name) return;
    el.addEventListener('input', () => _userTypedPasswordFields.add(el.name));
  });
}
_trackPasswordTyping();

async function load() {
  const s = await api.get('/server/settings');
  // Each call to load() repaints the form — clear the typing-tracker so
  // a stale "the user typed before" flag from before the reload doesn't
  // cause the next save to send autofill garbage.
  _userTypedPasswordFields.clear();
  for (const [k, v] of Object.entries(s)) {
    const el = form.elements[k];
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!v;
    else if (v != null) el.value = v;
  }
  // Reflect "this secret is saved" visually on every password slot whose
  // matching `<field>_set` flag is true. Field stays empty (so blank =
  // unchanged on next save) — only the placeholder changes, AND a small
  // green "✓ saved" badge becomes visible next to the field's label so
  // it's unambiguous whether the column has a value (the placeholder
  // text alone was missable).
  for (const [setFlag, fieldName] of Object.entries(SECRET_FIELD_MAP)) {
    const el = form.elements[fieldName];
    if (!el) continue;
    const isSet = !!s[setFlag];
    el.placeholder = isSet ? SAVED_SECRET_PLACEHOLDER : EMPTY_SECRET_PLACEHOLDER;
    const mark = document.querySelector(`.saved-mark[data-saved-mark-for="${fieldName}"]`);
    if (mark) mark.hidden = !isSet;
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
  // Populate the two output-side widgets too. Both accept 0 = unlimited
  // (unlike max_file_size which has a min of 1).
  outputSizeWidget.fromBytes(s.max_output_size_bytes || 0);
  storageWidget.fromBytes(s.max_storage_bytes || 0);
  // 3-tier disabled-formats grid — six JSON arrays, decoded into
  // comma-separated strings shown in the matching tier inputs.
  const tierMap = {
    'global,input':  s.disabled_input_formats_json,
    'global,output': s.disabled_output_formats_json,
    'admin,input':   s.disabled_admin_input_formats_json,
    'admin,output':  s.disabled_admin_output_formats_json,
    'user,input':    s.disabled_user_input_formats_json,
    'user,output':   s.disabled_user_output_formats_json,
  };
  document.querySelectorAll('#format-tiers-table input[data-tier]').forEach(el => {
    const k = `${el.dataset.tier},${el.dataset.dir}`;
    let arr = [];
    try { arr = JSON.parse(tierMap[k] || '[]'); } catch (_) { arr = []; }
    el.value = Array.isArray(arr) ? arr.join(',') : '';
  });

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

  // Google / GitHub last-test pills next to their Test buttons. Same
  // shape as the OIDC table's "Last test" cell so the visual is
  // consistent across the SSO section.
  paintProviderLastTest('google-last-test', s.oauth_google_last_test_at, s.oauth_google_last_test_ok);
  paintProviderLastTest('github-last-test', s.oauth_github_last_test_at, s.oauth_github_last_test_ok);

  // Repaint the SSO summary pill now that the Google/GitHub enabled
  // checkboxes are populated. loadOidcProviders() also paints this,
  // but races with us — when it lands first, it reads stale (unchecked)
  // boxes and undercounts. Re-painting here closes the race.
  paintSsoSummaryPill(_lastOidcRows);

  // SMTP / Discord status pills — reflect last-test state so a green
  // "configured" sticks across reloads (vs. the old "configured the
  // moment fields are non-empty" which was misleading after a failed
  // test).
  const smtpConfigured = !!(s.smtp_host && s.smtp_from);
  paintServicePill('smtp-status-pill', smtpConfigured, s.smtp_last_test_ok);
  const discordConfigured = !!s.discord_webhook_url;
  paintServicePill('discord-status-pill', discordConfigured, s.discord_last_test_ok);

  // The password-reset-via-email toggle requires SMTP to be configured
  // AND last-tested ok — otherwise toggling it on would just queue
  // emails that never deliver. Grey out + show the hint until both
  // conditions are met.
  const _prToggle = document.getElementById('password-reset-email-toggle');
  const _prHint   = document.getElementById('password-reset-email-hint');
  if (_prToggle) {
    const smtpReadyForReset = smtpConfigured && s.smtp_last_test_ok === true;
    _prToggle.disabled = !smtpReadyForReset;
    // If SMTP isn't ready but the column is somehow on (e.g. operator
    // toggled it then broke SMTP later), force the visual state off
    // without writing back to the server — they'll see the disabled
    // state + hint and re-enable once SMTP is healthy. The server-side
    // value stays whatever was saved; effectively a runtime gate, not
    // a destructive reset.
    if (!smtpReadyForReset) _prToggle.checked = false;
    if (_prHint) _prHint.hidden = smtpReadyForReset;
    const row = document.getElementById('password-reset-email-row');
    if (row) row.style.opacity = smtpReadyForReset ? '' : '0.55';
  }
  const banner = document.getElementById('smtp-warning');
  if (banner) {
    // Hide the banner when either:
    //   - self sign-up is off (no verification emails to worry about), OR
    //   - SMTP last-test passed (a successful real send also stamps this,
    //     so the banner clears the moment a real email goes through).
    // Show only when sign-up is on, verification is required, AND SMTP
    // is either not fully configured or the last send/test failed.
    const smtpReady = smtpConfigured && s.smtp_last_test_ok === true;
    const showBanner =
      !!s.allow_signup &&
      !!s.require_email_verification &&
      !smtpReady;
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
    } else if (el.type === 'password') {
      // Only ship a password field if the user typed into it *this session*.
      // Defends against browser autofill silently filling a password
      // input with a stored credential on page load; without this guard,
      // clicking Save (or any auto-save flow) would persist that autofill
      // garbage as the new secret.
      if (el.value && _userTypedPasswordFields.has(el.name)) {
        data[el.name] = el.value;
      }
      // Otherwise: skip → server treats as "unchanged".
    } else if (el.value === '') {
      data[el.name] = null;
    } else if (el.type === 'number') {
      data[el.name] = Number(el.value);
    } else {
      data[el.name] = el.value;
    }
  }
  // 3-tier format inputs (no `name` attr — read by data-tier/data-dir).
  // Each tier×direction maps to its own API field.
  const tierField = {
    'global,input':  'disabled_input_formats',
    'global,output': 'disabled_output_formats',
    'admin,input':   'disabled_admin_input_formats',
    'admin,output':  'disabled_admin_output_formats',
    'user,input':    'disabled_user_input_formats',
    'user,output':   'disabled_user_output_formats',
  };
  document.querySelectorAll('#format-tiers-table input[data-tier]').forEach(el => {
    const apiField = tierField[`${el.dataset.tier},${el.dataset.dir}`];
    if (!apiField) return;
    data[apiField] = csv(el.value || '');
  });
  // Drop the old flat names if they snuck in via leftover form serialization.
  delete data.disabled_input_formats_old;
  delete data.disabled_output_formats_old;
  // Max file size — overwrite whatever the form serializer produced (which
  // is nothing, since the inputs have no `name`) with the computed bytes.
  const bytes = readableToBytes();
  if (bytes != null) data.max_file_size_bytes = bytes;
  // Same trick for the two output-side widgets. They allow 0 = unlimited,
  // so we always set the field (no `if non-null` guard) — letting the
  // operator save back to 0 to disable the cap is the whole point.
  if (outputSizeWidget.valueEl) {
    data.max_output_size_bytes = outputSizeWidget.toBytes();
  }
  if (storageWidget.valueEl) {
    data.max_storage_bytes = storageWidget.toBytes();
  }

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

// Module-level cache of the most recently-loaded OIDC rows. Lets
// load() repaint the SSO summary pill *after* it has populated the
// Google/GitHub enabled checkboxes — without that, we had a race:
// loadOidcProviders() (smaller GET, finishes first) called the
// painter while load() (bigger GET) hadn't yet checked the boxes,
// so the count came out missing Google + GitHub.
let _lastOidcRows = [];

async function loadOidcProviders() {
  if (!oidcTbody) return;
  let rows = [];
  try { rows = await api.get('/server/oidc-providers'); } catch (_) { rows = []; }
  _lastOidcRows = Array.isArray(rows) ? rows : [];
  oidcTbody.innerHTML = '';
  // Paint the SSO summary pill regardless of how many providers there
  // are — depends on Google + GitHub + the OIDC enabled count.
  paintSsoSummaryPill(rows);
  if (!Array.isArray(rows) || rows.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="6" class="empty-state">No OIDC providers yet. Click "+ Add OIDC provider" below.</td>';
    oidcTbody.appendChild(tr);
    return;
  }
  for (const p of rows) {
    const tr = document.createElement('tr');
    tr.className = 'user-row clickable';
    tr.dataset.id = p.id;

    // Pull just the hostname out of the issuer URL — the full path
    // bloats the column and isn't useful at a glance. The Edit dialog
    // shows the full thing.
    let issuerHost = p.issuer || '';
    try { issuerHost = new URL(p.issuer).host; } catch (_) { /* leave raw */ }

    // Last-test cell — matches the notification-channels table pattern.
    let lastTest = '<span class="muted small">untested</span>';
    if (p.last_test_at) {
      const when = new Date(p.last_test_at).toLocaleString();
      lastTest = p.last_test_ok
        ? `<span class="status-pill ok">ok</span> <span class="muted small">${when}</span>`
        : `<span class="status-pill failed">failed</span> <span class="muted small">${when}</span>`;
    }

    // Inline enable checkbox: PATCH on change, no need to open the
    // edit form. Inline Test button: opens the SSO test popup using
    // the same `?test=1` flag the in-form Test button uses.
    // stopPropagation on both keeps the row-click handler (which
    // opens Edit) from firing when the operator clicks an action.
    tr.innerHTML = `
      <td>${escapeHtmlSimple(p.display_name)}</td>
      <td><code>${escapeHtmlSimple(p.slug)}</code></td>
      <td class="muted small">${escapeHtmlSimple(issuerHost)}</td>
      <td class="row-toggle-cell"><label class="row-toggle"><input type="checkbox" data-oidc-toggle="${p.id}" ${p.enabled ? 'checked' : ''} /></label></td>
      <td>${lastTest}</td>
      <td class="row-manage-cell" style="white-space: nowrap;">
        <button type="button" class="btn btn-secondary btn-manage" data-oidc-test="${escapeHtmlSimple(p.slug)}">Test</button>
        <button type="button" class="btn btn-secondary btn-manage">Edit</button>
      </td>
    `;
    tr.addEventListener('click', (e) => {
      // Ignore clicks on the toggle or any action button — those have
      // their own handlers and shouldn't also trigger Edit.
      if (e.target.closest('[data-oidc-toggle], .row-toggle, [data-oidc-test]')) return;
      openOidcForm(p);
    });
    const toggle = tr.querySelector('[data-oidc-toggle]');
    toggle.addEventListener('change', async (e) => {
      e.stopPropagation();
      const next = toggle.checked;
      try {
        await api.patch(`/server/oidc-providers/${p.id}`, { enabled: next });
        await loadOidcProviders();
      } catch (ex) {
        toggle.checked = !next;
        alert((ex && ex.detail) || 'Failed to toggle provider.');
      }
    });
    const testBtn = tr.querySelector('[data-oidc-test]');
    if (testBtn) testBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const slug = testBtn.dataset.oidcTest;
      const url = `/api/v1/auth/sso/${encodeURIComponent(slug)}/start?test=1`;
      window.open(url, '_blank', 'noopener,width=600,height=700');
    });
    oidcTbody.appendChild(tr);
  }
}

function paintSsoSummaryPill(oidcRows) {
  const pill = document.getElementById('sso-status-pill');
  // Read the saved Google/GitHub state from the form-elements that
  // load() just populated. Falls back to false if the elements aren't
  // in the DOM yet (very early call before load completes).
  const googleEn = !!(form.elements['oauth_google_enabled'] && form.elements['oauth_google_enabled'].checked);
  const googleCfg = !!(form.elements['oauth_google_client_id'] && form.elements['oauth_google_client_id'].value);
  const githubEn = !!(form.elements['oauth_github_enabled'] && form.elements['oauth_github_enabled'].checked);
  const githubCfg = !!(form.elements['oauth_github_client_id'] && form.elements['oauth_github_client_id'].value);
  const oidcCount = Array.isArray(oidcRows) ? oidcRows.filter(r => r.enabled).length : 0;
  const active =
    (googleEn && googleCfg ? 1 : 0) +
    (githubEn && githubCfg ? 1 : 0) +
    oidcCount;
  if (pill) {
    if (active === 0) {
      pill.textContent = 'none';
      pill.className = 'status-pill missing';
    } else {
      pill.textContent = `${active} active`;
      pill.className = 'status-pill ok';
    }
  }
  // Side effect: the "Password sign-in enabled" toggle in the Sign-up
  // section is only safe to turn off when at least one SSO method is
  // active — otherwise non-super-admins have no path in at all.
  // Disable the checkbox + show a warning when active === 0. If the
  // operator had already turned it off and then disabled their last
  // SSO provider, re-enable password sign-in defensively so they
  // don't paint themselves into a corner.
  const pwToggle = document.getElementById('password-signin-toggle');
  const pwWarn = document.getElementById('password-signin-warn');
  if (pwToggle) {
    if (active === 0) {
      // Force on, lock the control, surface the warning.
      if (!pwToggle.checked) pwToggle.checked = true;
      pwToggle.disabled = true;
      if (pwWarn) pwWarn.hidden = false;
    } else {
      pwToggle.disabled = false;
      if (pwWarn) pwWarn.hidden = true;
    }
  }
}

function escapeHtmlSimple(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// Per-kind provisioning help text. Keyed on provision_kind so adding a
// new kind (keycloak, scim, ...) is one entry plus a service handler.
const PROVISION_HELP = {
  authentik:
    'Create a long-lived token under Authentik admin → Directory → Tokens → New token. ' +
    'Identifier: any (e.g. vitriol-provisioning). User: a service account with the ' +
    '"Admin" group. Paste the generated key here.',
};

function applyProvisionSection(p, kindHint) {
  // kindHint comes from the template card on Add; on Edit we use the
  // saved provision_kind. Pass empty string for "no template picked yet".
  const section = document.querySelector('[data-oidc-provision-section]');
  if (!section) return;
  const kind = (p && p.provision_kind) || kindHint || 'none';
  const known = Object.prototype.hasOwnProperty.call(PROVISION_HELP, kind);
  section.hidden = !known;
  section.open = known;
  oidcForm.provision_kind.value = known ? kind : 'none';
  oidcForm.provision_on_approve.checked = !!(p && p.provision_on_approve);
  oidcForm.provision_api_token.value = '';
  const apiTokenMark = document.querySelector('.saved-mark[data-saved-mark-for="provision_api_token"]');
  if (apiTokenMark) apiTokenMark.hidden = !(p && p.provision_api_token_set);
  oidcForm.provision_api_token.placeholder = (p && p.provision_api_token_set)
    ? SAVED_SECRET_PLACEHOLDER
    : (known ? 'IdP admin API token' : '');
  const help = document.getElementById('oidc-provision-help');
  if (help) {
    if (known) {
      help.textContent = PROVISION_HELP[kind];
      help.hidden = false;
    } else {
      help.hidden = true;
      help.textContent = '';
    }
  }
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
  // Match the secret-placeholder convention used in the main form: bullets
  // + "saved" hint when the row has a stored client_secret, plain "(none)"
  // hint when adding fresh, so it's obvious whether the slot is populated.
  if (p && p.client_secret_set) {
    oidcForm.client_secret.placeholder = SAVED_SECRET_PLACEHOLDER;
  } else if (p) {
    oidcForm.client_secret.placeholder = EMPTY_SECRET_PLACEHOLDER;
  } else {
    oidcForm.client_secret.placeholder = 'required for new providers';
  }
  oidcForm.enabled.checked = p ? !!p.enabled : true;
  // Default show_on_signup to true for new providers — same default as
  // the server-side column. Operator can flip it off for IdPs that
  // don't allow self-enrollment (typical closed Authentik setup).
  if (oidcForm.show_on_signup) {
    oidcForm.show_on_signup.checked = p ? (p.show_on_signup !== false) : true;
  }
  document.getElementById('oidc-form-title').textContent = p ? `Edit — ${p.display_name}` : 'Add OIDC provider';
  document.getElementById('oidc-form-delete').hidden = !p;
  // Test button only makes sense for a saved provider — Authlib needs
  // a registered client to dispatch the round-trip, and that only
  // exists after Save. Stash the slug so the click handler knows
  // where to point the popup.
  const testBtn = document.getElementById('oidc-form-test');
  if (testBtn) {
    testBtn.hidden = !p;
    testBtn.dataset.slug = p ? p.slug : '';
  }
  document.getElementById('oidc-form-msg').hidden = true;
  applyProvisionSection(p, '');
  updateOidcRedirectPreview();
  oidcDialog.showModal();
}

// OIDC Test button — opens /api/v1/auth/sso/<slug>/start?test=1 in a
// popup. The start endpoint stamps session["sso_test"] so the callback
// renders the SSO test-result page (no user creation, no session
// cookies) rather than signing the operator in. Same pattern as the
// Google/GitHub Test buttons in the SSO providers section.
const _oidcTestBtn = document.getElementById('oidc-form-test');
if (_oidcTestBtn) {
  _oidcTestBtn.addEventListener('click', () => {
    const slug = _oidcTestBtn.dataset.slug || '';
    if (!slug) return;
    const url = `/api/v1/auth/sso/${encodeURIComponent(slug)}/start?test=1`;
    window.open(url, '_blank', 'noopener,width=600,height=700');
    const msg = document.getElementById('oidc-form-msg');
    msg.textContent = 'Opened sign-in window — complete it there to verify config.';
    msg.className = 'muted small';
    msg.hidden = false;
  });
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

// ---- OIDC provider template catalog -------------------------------------
//
// Sonarr-style picker: clicking "+ Add OIDC provider" first opens a card
// grid of well-known IdPs. Picking one pre-fills the bare add/edit form
// with that provider's known issuer URL pattern, default scopes, and a
// reasonable display name. Picking "Custom OIDC" opens the form blank
// (the historical behaviour). Each entry's `docsUrl` opens in a new tab
// from the provider card's "?" link, so an operator who's never used a
// given IdP has the official setup docs one click away.
//
// Issuer values listed here use placeholders (<your-domain>, <region>,
// <tenant>, <pool-id>, etc.) so the operator knows what to substitute.
// They aren't validated client-side beyond "looks URL-shaped" — Authlib
// hits the .well-known/openid-configuration endpoint at registration
// time and surfaces real errors.
const OIDC_TEMPLATES = [
  {
    id: 'authentik',
    name: 'Authentik',
    description: 'Self-hosted, open-source IdP.',
    displayName: 'Authentik',
    slug: 'authentik',
    issuerTemplate: 'https://auth.your-domain.com/application/o/<app-slug>/',
    scopes: 'openid email profile',
    docsUrl: 'https://goauthentik.io/docs/providers/oauth2/',
    provisionKind: 'authentik',   // Vitriol can auto-create users via Authentik's admin API
  },
  {
    id: 'keycloak',
    name: 'Keycloak',
    description: 'Self-hosted by Red Hat / open-source.',
    displayName: 'Keycloak',
    slug: 'keycloak',
    issuerTemplate: 'https://keycloak.your-domain.com/realms/<realm-name>',
    scopes: 'openid email profile',
    docsUrl: 'https://www.keycloak.org/docs/latest/server_admin/index.html#con-oidc_server_administration_guide',
  },
  {
    id: 'auth0',
    name: 'Auth0',
    description: 'Hosted IdP (an Okta company).',
    displayName: 'Auth0',
    slug: 'auth0',
    issuerTemplate: 'https://<your-tenant>.us.auth0.com/',
    scopes: 'openid email profile',
    docsUrl: 'https://auth0.com/docs/get-started/applications/application-settings',
  },
  {
    id: 'okta',
    name: 'Okta',
    description: 'Enterprise IdP. Use OIE Custom Authorization Server.',
    displayName: 'Okta',
    slug: 'okta',
    issuerTemplate: 'https://<your-org>.okta.com/oauth2/default',
    scopes: 'openid email profile',
    docsUrl: 'https://developer.okta.com/docs/concepts/oauth-openid/',
  },
  {
    id: 'entra',
    name: 'Microsoft Entra ID',
    description: 'Azure AD. Single- or multi-tenant.',
    displayName: 'Microsoft',
    slug: 'microsoft',
    issuerTemplate: 'https://login.microsoftonline.com/<tenant-id>/v2.0',
    scopes: 'openid email profile',
    docsUrl: 'https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc',
  },
  {
    id: 'google',
    name: 'Google Identity',
    description: 'Cloud Identity / Workspace OIDC. (Consumer Google login is a separate built-in.)',
    displayName: 'Google Identity',
    slug: 'google-identity',
    issuerTemplate: 'https://accounts.google.com',
    scopes: 'openid email profile',
    docsUrl: 'https://developers.google.com/identity/openid-connect/openid-connect',
  },
  {
    id: 'gitlab',
    name: 'GitLab',
    description: 'GitLab.com or self-hosted.',
    displayName: 'GitLab',
    slug: 'gitlab',
    issuerTemplate: 'https://gitlab.com',
    scopes: 'openid email profile',
    docsUrl: 'https://docs.gitlab.com/integration/openid_connect_provider/',
  },
  {
    id: 'cognito',
    name: 'Amazon Cognito',
    description: 'AWS user pools.',
    displayName: 'Cognito',
    slug: 'cognito',
    issuerTemplate: 'https://cognito-idp.<region>.amazonaws.com/<userpool-id>',
    scopes: 'openid email profile',
    docsUrl: 'https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-userpools-server-contract-reference.html',
  },
  {
    id: 'apple',
    name: 'Sign in with Apple',
    description: 'Apple ID. Requires a paid Apple Developer membership.',
    displayName: 'Apple',
    slug: 'apple',
    issuerTemplate: 'https://appleid.apple.com',
    scopes: 'openid email name',
    docsUrl: 'https://developer.apple.com/documentation/sign_in_with_apple/sign_in_with_apple_rest_api',
  },
  {
    id: 'zitadel',
    name: 'ZITADEL',
    description: 'Cloud-native open-source IdP.',
    displayName: 'ZITADEL',
    slug: 'zitadel',
    issuerTemplate: 'https://<your-instance>.zitadel.cloud',
    scopes: 'openid email profile',
    docsUrl: 'https://zitadel.com/docs/guides/integrate/login/oidc/login-users',
  },
  {
    id: 'salesforce',
    name: 'Salesforce',
    description: 'Salesforce as an IdP via Connected App.',
    displayName: 'Salesforce',
    slug: 'salesforce',
    issuerTemplate: 'https://login.salesforce.com',
    scopes: 'openid email profile',
    docsUrl: 'https://help.salesforce.com/s/articleView?id=sf.sso_provider_openid_connect.htm',
  },
];

const oidcCatalogDialog = document.getElementById('oidc-catalog-dialog');
const oidcCatalogGrid = document.getElementById('oidc-catalog-grid');

function renderOidcCatalog() {
  if (!oidcCatalogGrid) return;
  oidcCatalogGrid.innerHTML = '';
  for (const tpl of OIDC_TEMPLATES) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'provider-card';
    card.innerHTML = `
      <span class="pc-name">${escapeHtmlSimple(tpl.name)}</span>
      <span class="pc-desc">${escapeHtmlSimple(tpl.description)}</span>
    `;
    card.addEventListener('click', () => {
      oidcCatalogDialog.close();
      openOidcFormFromTemplate(tpl);
    });
    oidcCatalogGrid.appendChild(card);
  }
  // Custom OIDC card — opens the form blank, same as the old Add button.
  const custom = document.createElement('button');
  custom.type = 'button';
  custom.className = 'provider-card pc-custom';
  custom.innerHTML = `
    <span class="pc-name">Custom OIDC</span>
    <span class="pc-desc">Anything not on this list — paste your own issuer URL.</span>
  `;
  custom.addEventListener('click', () => {
    oidcCatalogDialog.close();
    openOidcForm(null);
  });
  oidcCatalogGrid.appendChild(custom);
}

function openOidcFormFromTemplate(tpl) {
  // Pre-fill the existing add form with template values. We pass a
  // pseudo-provider object that isn't yet persisted (no id) so the form
  // treats this as a "create" — the only difference is the fields aren't
  // empty.
  oidcForm.id.value = '';
  oidcForm.display_name.value = tpl.displayName;
  oidcForm.slug.value = tpl.slug;
  oidcForm.issuer.value = tpl.issuerTemplate;
  oidcForm.scopes.value = tpl.scopes;
  oidcForm.client_id.value = '';
  oidcForm.client_secret.value = '';
  oidcForm.client_secret.placeholder = 'required for new providers';
  oidcForm.enabled.checked = true;
  if (oidcForm.show_on_signup) oidcForm.show_on_signup.checked = true;
  document.getElementById('oidc-form-title').textContent = `Add ${tpl.name}`;
  document.getElementById('oidc-form-delete').hidden = true;
  document.getElementById('oidc-form-msg').hidden = true;
  // The catalog card knows whether this kind supports auto-provisioning;
  // pass that hint so the form shows the provisioning section pre-set
  // to that kind. Operators can still uncheck "Auto-provision approved
  // users" if they don't want it.
  applyProvisionSection(null, tpl.provisionKind || 'none');
  updateOidcRedirectPreview();
  oidcDialog.showModal();
}

if (oidcAddBtn) oidcAddBtn.addEventListener('click', () => {
  if (oidcCatalogDialog) {
    renderOidcCatalog();
    oidcCatalogDialog.showModal();
  } else {
    openOidcForm(null);
  }
});

// Generic Cancel/Close handler for any dialog button marked data-close —
// wires up the OIDC modal, export-dialog, import-dialog, etc. so they all
// dismiss without submitting their parent form.
document.querySelectorAll('[data-close]').forEach(btn => {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    const d = btn.closest('dialog');
    if (d) d.close();
  });
});

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
    show_on_signup: oidcForm.show_on_signup ? oidcForm.show_on_signup.checked : true,
    provision_kind: oidcForm.provision_kind.value || 'none',
    provision_on_approve: oidcForm.provision_on_approve.checked,
  };
  if (oidcForm.client_secret.value) payload.client_secret = oidcForm.client_secret.value;
  // Same convention as every other secret in the app: only ship the
  // value if the operator actually typed something. Empty = unchanged.
  if (oidcForm.provision_api_token.value) {
    payload.provision_api_token = oidcForm.provision_api_token.value;
  }
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

// Live-repaint the SSO summary pill (+ password-signin toggle lock)
// whenever the operator toggles Google/GitHub enabled in the form,
// even before they click Save. Pulls the freshest OIDC row state via
// loadOidcProviders so the count stays accurate end-to-end.
['oauth_google_enabled', 'oauth_github_enabled'].forEach(fieldName => {
  const el = form.elements[fieldName];
  if (el) el.addEventListener('change', () => {
    // Reuse the existing reload to get rows AND repaint the pill,
    // which also re-evaluates the password-signin safety lock.
    loadOidcProviders().catch(() => {});
  });
});

// Listen for "test completed" pings from the SSO test popup so the
// OIDC table + Google/GitHub last-test pills refresh without a full
// page reload. The popup posts this message right before window.close().
window.addEventListener('message', (e) => {
  if (e.origin !== location.origin) return;   // only trust our own origin
  if (e.data && e.data.type === 'vitriol-sso-test-complete') {
    // load() repaints Google + GitHub last-test pills from /server/settings;
    // loadOidcProviders() refreshes the OIDC table rows + SSO summary
    // pill count. Both are cheap GETs so we just fire them in parallel.
    load().catch(() => {});
    loadOidcProviders().catch(() => {});
  }
});

// ============================================================
// Notification channels — multi-row replacement for the old singleton
// Discord webhook. Same Sonarr-style catalog → per-kind form pattern as
// OIDC, but the form's visible field set switches based on the chosen
// kind (Discord wants one URL, ntfy wants 4 fields, generic webhook
// wants 5+, etc.).
// ============================================================

const NOTIFICATION_TEMPLATES = [
  { kind: 'discord',         name: 'Discord',         description: 'Channel webhook → posts as a bot.' },
  { kind: 'slack',           name: 'Slack',           description: 'Incoming webhook → posts to a channel.' },
  { kind: 'ntfy',            name: 'ntfy',            description: 'Public ntfy.sh or self-hosted topic.' },
  { kind: 'gotify',          name: 'Gotify',          description: 'Self-hosted push gateway.' },
  { kind: 'telegram',        name: 'Telegram',        description: 'Bot token + chat ID via @BotFather.' },
  { kind: 'generic_webhook', name: 'Generic webhook', description: 'Arbitrary URL/method/headers/body.' },
  { kind: 'script',          name: 'Script',          description: 'Bash with $VITRIOL_MESSAGE in env.' },
  { kind: 'bluesky',         name: 'Bluesky',         description: 'AT Protocol post via app password.' },
];

const notifTbody = document.getElementById('notif-tbody');
const notifAddBtn = document.getElementById('notif-add-btn');
const notifCatalogDialog = document.getElementById('notif-catalog-dialog');
const notifCatalogGrid = document.getElementById('notif-catalog-grid');
const notifFormDialog = document.getElementById('notif-form-dialog');
const notifForm = document.getElementById('notif-form');

async function loadNotificationChannels() {
  if (!notifTbody) return;
  let rows = [];
  try { rows = await api.get('/server/notification-channels'); } catch (_) { rows = []; }
  notifTbody.innerHTML = '';
  if (!Array.isArray(rows) || rows.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5" class="empty-state">No channels yet. Click "+ Add channel" below.</td>';
    notifTbody.appendChild(tr);
    paintNotifSummaryPill(rows);
    return;
  }
  for (const ch of rows) {
    const tr = document.createElement('tr');
    tr.className = 'user-row clickable';
    tr.dataset.id = ch.id;
    let lastTest = '—';
    if (ch.last_test_at) {
      const when = new Date(ch.last_test_at).toLocaleString();
      lastTest = ch.last_test_ok
        ? `<span class="status-pill ok">ok</span> <span class="muted small">${when}</span>`
        : `<span class="status-pill failed">failed</span> <span class="muted small">${when}</span>`;
    }
    // Inline enable checkbox — PATCH on change, no need to open the
    // edit form. stopPropagation on the input keeps the row-click
    // handler from also opening the form when the toggle is clicked.
    tr.innerHTML = `
      <td>${escapeHtmlSimple(ch.name)}</td>
      <td><code>${escapeHtmlSimple(ch.kind)}</code></td>
      <td class="row-toggle-cell"><label class="row-toggle"><input type="checkbox" data-notif-toggle="${ch.id}" ${ch.enabled ? 'checked' : ''} /></label></td>
      <td>${lastTest}</td>
      <td class="row-manage-cell"><button type="button" class="btn btn-secondary btn-manage">Edit</button></td>
    `;
    tr.addEventListener('click', (e) => {
      if (e.target.closest('[data-notif-toggle], .row-toggle')) return;
      openNotifForm(ch);
    });
    const toggle = tr.querySelector('[data-notif-toggle]');
    toggle.addEventListener('change', async (e) => {
      e.stopPropagation();
      const next = toggle.checked;
      try {
        await api.patch(`/server/notification-channels/${ch.id}`, { enabled: next });
        await loadNotificationChannels();
      } catch (ex) {
        toggle.checked = !next;
        alert((ex && ex.detail) || 'Failed to toggle channel.');
      }
    });
    notifTbody.appendChild(tr);
  }
  paintNotifSummaryPill(rows);
}

function paintNotifSummaryPill(rows) {
  const pill = document.getElementById('notif-status-pill');
  if (!pill) return;
  const total = Array.isArray(rows) ? rows.length : 0;
  const enabled = total ? rows.filter(r => r.enabled).length : 0;
  if (total === 0) {
    pill.textContent = 'none';
    pill.className = 'status-pill missing';
    return;
  }
  if (enabled === 0) {
    pill.textContent = `${total} all off`;
    pill.className = 'status-pill warn';
    return;
  }
  pill.textContent = `${enabled} active`;
  pill.className = 'status-pill ok';
}

function renderNotifCatalog() {
  if (!notifCatalogGrid) return;
  notifCatalogGrid.innerHTML = '';
  for (const tpl of NOTIFICATION_TEMPLATES) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'provider-card';
    card.innerHTML = `
      <span class="pc-name">${escapeHtmlSimple(tpl.name)}</span>
      <span class="pc-desc">${escapeHtmlSimple(tpl.description)}</span>
    `;
    card.addEventListener('click', () => {
      notifCatalogDialog.close();
      openNotifFormFromTemplate(tpl);
    });
    notifCatalogGrid.appendChild(card);
  }
}

// Show only the field-group matching the form's current `kind`. Toggling
// happens via the `data-kind-fields` attribute on each container.
function applyNotifKindVisibility(kind) {
  document.querySelectorAll('#notif-form [data-kind-fields]').forEach(div => {
    div.hidden = div.dataset.kindFields !== kind;
  });
}

// Map each kind's UI fields to/from the API config + secret shape that
// services/notifications.py expects. Centralising this here keeps the
// per-kind branching out of the submit handler.
function notifFormToPayload() {
  const kind = notifForm.kind.value;
  const config = {};
  let secret = null;
  const v = (n) => (notifForm.elements[n] || { value: '' }).value;
  switch (kind) {
    case 'discord':
      secret = v('cfg_discord_url') || null;
      break;
    case 'slack':
      secret = v('cfg_slack_url') || null;
      break;
    case 'ntfy':
      config.server_url = v('cfg_ntfy_server');
      config.topic = v('cfg_ntfy_topic');
      config.auth_kind = v('cfg_ntfy_auth_kind') || 'none';
      secret = v('cfg_ntfy_secret') || null;
      break;
    case 'gotify':
      config.server_url = v('cfg_gotify_server');
      secret = v('cfg_gotify_token') || null;
      break;
    case 'telegram':
      config.chat_id = v('cfg_telegram_chat');
      secret = v('cfg_telegram_token') || null;
      break;
    case 'generic_webhook':
      config.url = v('cfg_gw_url');
      config.method = v('cfg_gw_method') || 'POST';
      config.headers_json = v('cfg_gw_headers');
      config.body_template = v('cfg_gw_body');
      secret = v('cfg_gw_secret') || null;
      break;
    case 'script':
      config.script = v('cfg_script_body');
      break;
    case 'bluesky':
      config.handle = v('cfg_bsky_handle');
      config.server = v('cfg_bsky_server') || 'https://bsky.social';
      secret = v('cfg_bsky_password') || null;
      break;
  }
  return {
    name: v('name'),
    enabled: !!notifForm.elements['enabled'].checked,
    config,
    secret,    // null = don't change on edit, "" wouldn't be sent here
  };
}

function notifPayloadToForm(ch) {
  // Reset all per-kind fields. Empty values are correct on Add; on Edit
  // we then fill in what the server gave us.
  for (const el of notifForm.elements) {
    if (el.type === 'checkbox') el.checked = false;
    else if (el.tagName === 'SELECT') el.selectedIndex = 0;
    else el.value = '';
  }
  const cfg = ch?.config || {};
  notifForm.id.value = ch ? ch.id : '';
  notifForm.kind.value = ch ? ch.kind : '';
  notifForm.elements['name'].value = ch ? ch.name : '';
  notifForm.elements['enabled'].checked = ch ? !!ch.enabled : true;

  // For each per-kind input, populate from cfg AND set placeholders for
  // saved-secret slots so the operator can see "this is configured" vs
  // "this is empty" without us echoing plaintext back.
  const setSecretPlaceholder = (fieldName, isSet) => {
    const el = notifForm.elements[fieldName];
    if (!el) return;
    el.placeholder = isSet ? SAVED_SECRET_PLACEHOLDER : EMPTY_SECRET_PLACEHOLDER;
  };

  switch (ch?.kind) {
    case 'discord':
      setSecretPlaceholder('cfg_discord_url', ch.secret_set);
      break;
    case 'slack':
      setSecretPlaceholder('cfg_slack_url', ch.secret_set);
      break;
    case 'ntfy':
      notifForm.elements['cfg_ntfy_server'].value = cfg.server_url || '';
      notifForm.elements['cfg_ntfy_topic'].value = cfg.topic || '';
      notifForm.elements['cfg_ntfy_auth_kind'].value = cfg.auth_kind || 'none';
      setSecretPlaceholder('cfg_ntfy_secret', ch.secret_set);
      break;
    case 'gotify':
      notifForm.elements['cfg_gotify_server'].value = cfg.server_url || '';
      setSecretPlaceholder('cfg_gotify_token', ch.secret_set);
      break;
    case 'telegram':
      notifForm.elements['cfg_telegram_chat'].value = cfg.chat_id || '';
      setSecretPlaceholder('cfg_telegram_token', ch.secret_set);
      break;
    case 'generic_webhook':
      notifForm.elements['cfg_gw_url'].value = cfg.url || '';
      notifForm.elements['cfg_gw_method'].value = cfg.method || 'POST';
      notifForm.elements['cfg_gw_headers'].value = cfg.headers_json || '';
      notifForm.elements['cfg_gw_body'].value = cfg.body_template || '';
      setSecretPlaceholder('cfg_gw_secret', ch.secret_set);
      break;
    case 'script':
      notifForm.elements['cfg_script_body'].value = cfg.script || '';
      break;
    case 'bluesky':
      notifForm.elements['cfg_bsky_handle'].value = cfg.handle || '';
      notifForm.elements['cfg_bsky_server'].value = cfg.server || 'https://bsky.social';
      setSecretPlaceholder('cfg_bsky_password', ch.secret_set);
      break;
  }
}

function openNotifFormFromTemplate(tpl) {
  notifPayloadToForm(null);
  notifForm.kind.value = tpl.kind;
  notifForm.elements['name'].value = tpl.name;
  notifForm.elements['enabled'].checked = true;
  applyNotifKindVisibility(tpl.kind);
  document.getElementById('notif-form-title').textContent = `Add ${tpl.name}`;
  document.getElementById('notif-form-delete').hidden = true;
  document.getElementById('notif-form-test').hidden = true;
  document.getElementById('notif-form-msg').hidden = true;
  notifFormDialog.showModal();
}

function openNotifForm(ch) {
  notifPayloadToForm(ch);
  applyNotifKindVisibility(ch.kind);
  document.getElementById('notif-form-title').textContent = `Edit — ${ch.name}`;
  document.getElementById('notif-form-delete').hidden = false;
  document.getElementById('notif-form-test').hidden = false;
  document.getElementById('notif-form-msg').hidden = true;
  notifFormDialog.showModal();
}

if (notifAddBtn) notifAddBtn.addEventListener('click', () => {
  if (notifCatalogDialog) {
    renderNotifCatalog();
    notifCatalogDialog.showModal();
  }
});

if (notifForm) notifForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = notifForm.id.value;
  const msg = document.getElementById('notif-form-msg');
  msg.hidden = true;
  const payload = notifFormToPayload();
  // On create, the server requires the kind. On edit we don't send it
  // — kind is immutable for an existing row.
  if (!id) payload.kind = notifForm.kind.value;
  // Don't send `secret: null` on create — the server treats that as
  // "set to null". We want "no secret yet" to mean "field omitted" so
  // the row goes in with secret_enc=null cleanly.
  if (payload.secret == null && !id) delete payload.secret;
  // On edit, an unchanged password field has secret=null which we
  // *also* want stripped so the existing encrypted value is preserved.
  if (payload.secret == null && id) delete payload.secret;
  try {
    if (id) {
      await api.patch(`/server/notification-channels/${id}`, payload);
    } else {
      await api.post('/server/notification-channels', payload);
    }
    notifFormDialog.close();
    await loadNotificationChannels();
  } catch (ex) {
    msg.textContent = (ex && ex.detail) || 'Save failed';
    msg.className = 'error small';
    msg.hidden = false;
  }
});

const notifFormDelete = document.getElementById('notif-form-delete');
if (notifFormDelete) notifFormDelete.addEventListener('click', async () => {
  const id = notifForm.id.value;
  if (!id) return;
  const name = notifForm.elements['name'].value || 'this channel';
  if (!confirm(`Delete ${name}?`)) return;
  try {
    await api.del(`/server/notification-channels/${id}`);
    notifFormDialog.close();
    await loadNotificationChannels();
  } catch (ex) {
    const msg = document.getElementById('notif-form-msg');
    msg.textContent = (ex && ex.detail) || 'Delete failed';
    msg.className = 'error small';
    msg.hidden = false;
  }
});

const notifFormTest = document.getElementById('notif-form-test');
if (notifFormTest) notifFormTest.addEventListener('click', async () => {
  const id = notifForm.id.value;
  if (!id) return;
  const msg = document.getElementById('notif-form-msg');
  msg.hidden = true;
  notifFormTest.disabled = true;
  const original = notifFormTest.textContent;
  notifFormTest.textContent = 'Testing…';
  try {
    const r = await api.post(`/server/notification-channels/${id}/test`, {});
    msg.textContent = r.message || 'Test sent.';
    msg.className = 'ok small';
    msg.hidden = false;
    await loadNotificationChannels();   // refresh last_test pill
  } catch (ex) {
    msg.textContent = (ex && ex.detail) || 'Test failed';
    msg.className = 'error small';
    msg.hidden = false;
    await loadNotificationChannels();
  } finally {
    notifFormTest.disabled = false;
    notifFormTest.textContent = original;
  }
});

if (notifTbody) loadNotificationChannels();

// ============================================================
// SMTP + Discord pill state — uses last_test_ok/at to drive colour
// ============================================================
//
// Pill states:
//   ok        — host/from set AND last test passed → green "configured"
//   untested  — host/from set, never tested        → amber "untested"
//   missing   — required fields empty              → amber "not configured"
//   failed    — last test failed                   → red   "test failed"

// Render a "last test" indicator next to a Test button — same look as
// the OIDC table's per-row Last test cell. Pass an ISO-ish datetime
// string (or null/undefined) plus the ok boolean.
function paintProviderLastTest(spanId, at, ok) {
  const el = document.getElementById(spanId);
  if (!el) return;
  if (!at) {
    el.innerHTML = '<span class="muted small">untested</span>';
    return;
  }
  const when = new Date(at).toLocaleString();
  el.innerHTML = ok
    ? `<span class="status-pill ok">ok</span> <span class="muted small">${when}</span>`
    : `<span class="status-pill failed">failed</span> <span class="muted small">${when}</span>`;
}

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
    // Persist ONLY the webhook URL — never dispatch a form-wide submit
    // here. A full submit risks browser autofill clobbering unrelated
    // fields (allowed_origin, smtp_*, etc.) silently. The dedicated PATCH
    // below sends a one-key payload, leaving every other column untouched.
    const url = (form.elements['discord_webhook_url'] || {}).value || '';
    if (url) await api.patch('/server/settings', { discord_webhook_url: url });
    const r = await api.post('/server/test-discord', {});
    msg.textContent = r.message || 'Posted.';
    msg.className = 'ok small';
    msg.hidden = false;
    await load();
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

// ============================================================
// Export / Import settings
// ============================================================
//
// Export: collect server config, optionally encrypt with a password,
//         download as a JSON file. Encryption is server-side (Fernet
//         keyed off PBKDF2 of the user's password).
// Import: pick file → if encrypted, prompt for password → server
//         validates + verifies hash → confirmation modal with summary
//         → apply.

const exportDialog = document.getElementById('export-dialog');
const exportForm = document.getElementById('export-form');
const exportEncryptToggle = document.getElementById('export-encrypt');
const exportPasswordRow = document.getElementById('export-password-row');
const exportMsg = document.getElementById('export-msg');

const importDialog = document.getElementById('import-dialog');
const importForm = document.getElementById('import-form');
const importFile = document.getElementById('import-file');
const importPasswordEl = document.getElementById('import-password');
const importMsg = document.getElementById('import-msg');
const importSubmit = document.getElementById('import-submit');

const importStepPick = document.getElementById('import-step-pick');
const importStepPassword = document.getElementById('import-step-password');
const importStepConfirm = document.getElementById('import-step-confirm');

let _importEnvelope = null;
let _importPassword = null;
let _importStep = 'pick';   // 'pick' | 'password' | 'confirm'

// ---- Export ----------------------------------------------------------

if (document.getElementById('export-btn')) {
  document.getElementById('export-btn').addEventListener('click', () => {
    exportEncryptToggle.checked = false;
    document.getElementById('export-password').value = '';
    exportPasswordRow.hidden = true;
    exportMsg.hidden = true;
    exportDialog.showModal();
  });
}

if (exportEncryptToggle) {
  exportEncryptToggle.addEventListener('change', () => {
    exportPasswordRow.hidden = !exportEncryptToggle.checked;
  });
}

if (exportForm) exportForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const password = exportEncryptToggle.checked
    ? document.getElementById('export-password').value
    : '';
  if (exportEncryptToggle.checked && password.length < 8) {
    exportMsg.textContent = 'Password must be at least 8 characters.';
    exportMsg.hidden = false;
    return;
  }
  exportMsg.hidden = true;
  try {
    const envelope = await api.post('/server/export', { password: password || null });
    // Build a blob and trigger a download.
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const filename = `vitriol-settings-${stamp}.json`;
    const blob = new Blob([JSON.stringify(envelope, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    exportDialog.close();
  } catch (ex) {
    exportMsg.textContent = (ex && ex.detail) || 'Export failed.';
    exportMsg.hidden = false;
  }
});

// ---- Import ----------------------------------------------------------

function _importStepShow(step) {
  _importStep = step;
  importStepPick.hidden = step !== 'pick';
  importStepPassword.hidden = step !== 'password';
  importStepConfirm.hidden = step !== 'confirm';
  importMsg.hidden = true;
  if (step === 'pick') {
    importSubmit.textContent = 'Continue';
    importSubmit.disabled = !importFile.files.length;
  } else if (step === 'password') {
    importSubmit.textContent = 'Decrypt';
    importSubmit.disabled = false;
    setTimeout(() => importPasswordEl.focus(), 50);
  } else {
    importSubmit.textContent = 'Apply settings';
    importSubmit.disabled = false;
  }
}

function _renderSummary(summary) {
  const tbody = document.querySelector('#import-summary tbody');
  tbody.innerHTML = '';
  const rows = [
    ['Public base URL', summary.public_base_url],
    ['Allow signup', summary.allow_signup ? 'on' : 'off'],
    ['SMTP host', summary.smtp_host],
    ['SMTP from', summary.smtp_from],
    ['Discord webhook', summary.discord_configured ? 'configured' : '—'],
    ['Google OAuth', summary.google_configured ? 'configured' : '—'],
    ['GitHub OAuth', summary.github_configured ? 'configured' : '—'],
    ['OIDC providers', `${summary.oidc_provider_count}${summary.oidc_slugs.length ? ' (' + summary.oidc_slugs.join(', ') + (summary.oidc_provider_count > summary.oidc_slugs.length ? ', …' : '') + ')' : ''}`],
    ['Custom roles', `${summary.custom_role_count}${summary.custom_role_names.length ? ' (' + summary.custom_role_names.join(', ') + (summary.custom_role_count > summary.custom_role_names.length ? ', …' : '') + ')' : ''}`],
  ];
  for (const [k, v] of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<th>${k}</th><td>${v == null ? '—' : v}</td>`;
    tbody.appendChild(tr);
  }
}

async function _validateImport() {
  // POSTs envelope (+ password if we have one) with confirm=false to
  // get a summary back. Server returns 401 with {needs_password: true}
  // when the file is encrypted and we haven't supplied a password yet.
  const body = { envelope: _importEnvelope, confirm: false };
  if (_importPassword) body.password = _importPassword;
  try {
    const r = await api.post('/server/import', body);
    return { ok: true, summary: r.summary };
  } catch (ex) {
    const detail = ex && ex.detail;
    if (detail && typeof detail === 'object' && detail.needs_password) {
      return { ok: false, needsPassword: true };
    }
    return { ok: false, error: typeof detail === 'string' ? detail : 'Could not read file.' };
  }
}

if (document.getElementById('import-btn')) {
  document.getElementById('import-btn').addEventListener('click', () => {
    _importEnvelope = null;
    _importPassword = null;
    importFile.value = '';
    importPasswordEl.value = '';
    importMsg.hidden = true;
    _importStepShow('pick');
    importDialog.showModal();
  });
}

if (importFile) {
  importFile.addEventListener('change', () => {
    importSubmit.disabled = !importFile.files.length;
  });
}

if (importForm) importForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  importMsg.hidden = true;

  if (_importStep === 'pick') {
    const f = importFile.files[0];
    if (!f) return;
    let parsed;
    try {
      parsed = JSON.parse(await f.text());
    } catch (ex) {
      importMsg.textContent = 'File is not valid JSON.';
      importMsg.hidden = false;
      return;
    }
    if (!parsed || parsed.format !== 'vitriol-settings-v1') {
      importMsg.textContent = 'File doesn\'t look like a Vitriol settings export.';
      importMsg.hidden = false;
      return;
    }
    _importEnvelope = parsed;
    importSubmit.disabled = true;
    importSubmit.textContent = 'Reading…';
    const res = await _validateImport();
    importSubmit.disabled = false;
    if (res.ok) {
      _renderSummary(res.summary);
      _importStepShow('confirm');
    } else if (res.needsPassword) {
      _importStepShow('password');
    } else {
      importMsg.textContent = res.error || 'Could not parse file.';
      importMsg.hidden = false;
      importSubmit.textContent = 'Continue';
    }
    return;
  }

  if (_importStep === 'password') {
    const pw = importPasswordEl.value;
    if (!pw) { importMsg.textContent = 'Enter a password.'; importMsg.hidden = false; return; }
    _importPassword = pw;
    importSubmit.disabled = true;
    importSubmit.textContent = 'Checking…';
    const res = await _validateImport();
    importSubmit.disabled = false;
    if (res.ok) {
      _renderSummary(res.summary);
      _importStepShow('confirm');
    } else if (res.needsPassword) {
      // Shouldn't happen — we just sent a password.
      importMsg.textContent = 'Password rejected.';
      importMsg.hidden = false;
      importSubmit.textContent = 'Decrypt';
    } else {
      importMsg.textContent = res.error || 'Wrong password or corrupted file.';
      importMsg.hidden = false;
      importSubmit.textContent = 'Decrypt';
    }
    return;
  }

  if (_importStep === 'confirm') {
    importSubmit.disabled = true;
    importSubmit.textContent = 'Applying…';
    const body = { envelope: _importEnvelope, confirm: true };
    if (_importPassword) body.password = _importPassword;
    try {
      const r = await api.post('/server/import', body);
      importDialog.close();
      // Re-load form so the freshly-imported values are reflected.
      await load();
      const m = document.getElementById('server-msg');
      if (m) {
        m.textContent = r.message || 'Settings imported.';
        m.className = 'ok';
        m.hidden = false;
      }
    } catch (ex) {
      importMsg.textContent = (ex && ex.detail) || 'Apply failed.';
      importMsg.hidden = false;
      importSubmit.disabled = false;
      importSubmit.textContent = 'Apply settings';
    }
  }
});

// Send a test email using the SMTP settings already saved in the DB.
// No auto-save before the test — that path was the root cause of the
// "password keeps getting clobbered" bug: browser autofill would silently
// drop a stored value into the smtp_password field on page load, and a
// pre-test PATCH would then persist that autofill garbage as the new
// password. If the operator wants to test newly typed credentials, they
// click "Save settings" first, then "Send test email".
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

// ============================================================
// SSO test buttons — opens /auth/sso/<provider>/start in a new tab.
// The flow itself proves the redirect URI + client credentials are
// configured correctly (any misconfig surfaces as a provider error
// page in the popup window).
// ============================================================

function wireSsoTest(btnId, msgId, provider) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.addEventListener('click', () => {
    // No auto-save here. The test flow uses whatever is already in the DB,
    // and dispatching a form-wide submit risks browser autofill (or a
    // stale value in a hidden field) silently overwriting unrelated
    // settings — which is exactly how `allowed_origin` got clobbered with
    // "masterlocke" the first time. If the operator wants to test newly
    // typed credentials, they Save first, then click Test.
    const msg = document.getElementById(msgId);
    const url = `/api/v1/auth/sso/${provider}/start?test=1`;
    window.open(url, '_blank', 'noopener,width=600,height=700');
    msg.textContent = 'Opened sign-in window — complete it there to verify config. Save the page first if you just typed a new client ID/secret.';
    msg.className = 'muted small';
    msg.hidden = false;
  });
}

wireSsoTest('google-test-btn', 'google-test-msg', 'google');
wireSsoTest('github-test-btn', 'github-test-msg', 'github');

// ============================================================
//   Storage section — config edits are saved on regular Save;
//   activating S3 (or reverting to local) is an explicit second
//   step that requires a passing test + a typed confirmation.
// ============================================================

function paintStoragePill(s) {
  const pill = document.getElementById('storage-status-pill');
  if (!pill) return;
  if (s.storage_backend !== 's3') {
    pill.textContent = 'local';
    pill.className = 'status-pill ok';
    return;
  }
  pill.textContent = `s3: ${s.s3_bucket || '?'}`;
  pill.className = 'status-pill ok';
}

function paintStorageActiveReadout(s) {
  const el = document.getElementById('storage-active-readout');
  if (!el) return;
  if (s.storage_backend === 's3') {
    el.textContent = `S3 (${s.s3_bucket || '?'})`;
    el.className = 'role-tag role-admin';
  } else {
    el.textContent = 'Local disk (/data)';
    el.className = 'role-tag role-user';
  }
}

function paintS3LastTest(s) {
  const el = document.getElementById('s3-last-test');
  if (!el) return;
  if (!s.s3_last_test_at) { el.textContent = ''; return; }
  const when = new Date(s.s3_last_test_at).toLocaleString();
  el.textContent = `Last test: ${when} — ${s.s3_last_test_ok === true ? 'ok' : 'failed'}`;
}

function updateActivateButtons(s) {
  const activateBtn = document.getElementById('s3-activate-btn');
  const revertBtn = document.getElementById('s3-revert-btn');
  const hint = document.getElementById('s3-activate-hint');
  if (!activateBtn || !revertBtn) return;
  const isS3 = s.storage_backend === 's3';
  activateBtn.hidden = isS3;
  revertBtn.hidden = !isS3;
  const cfgComplete = !!(s.s3_bucket && s.s3_access_key && s.s3_secret_key_set);
  const testOk = s.s3_last_test_ok === true;
  activateBtn.disabled = !(cfgComplete && testOk);
  if (hint) {
    if (isS3) {
      hint.textContent = 'S3 is active. Click "Revert to local" to send new writes back to /data.';
    } else if (!cfgComplete) {
      hint.textContent = 'Fill in bucket, access key, and secret key (then Save) before testing.';
    } else if (!testOk) {
      hint.textContent = 'Run "Test connection" successfully to unlock Activate S3.';
    } else {
      hint.textContent = 'Test passed — Activate S3 is unlocked. Activation requires a typed confirmation.';
    }
  }
}

const s3TestBtn = document.getElementById('s3-test-btn');
if (s3TestBtn) {
  s3TestBtn.addEventListener('click', async () => {
    const out = document.getElementById('s3-last-test');
    if (out) { out.textContent = 'Testing…'; }
    try {
      const r = await api.post('/server/test-s3', {});
      if (out) out.textContent = r.message || 'ok';
    } catch (ex) {
      if (out) out.textContent = ex.detail || 'Test failed';
    }
    await refreshStorageSection();
  });
}

// Confirmation modal — typed-phrase gate. Same dialog handles both
// "enable s3" and "revert to local" by swapping the title + phrase.
const storageConfirmDialog = document.getElementById('storage-confirm-dialog');
const storageConfirmInput = document.getElementById('storage-confirm-input');
const storageConfirmSubmit = document.getElementById('storage-confirm-submit');
const storageConfirmPhraseEl = document.getElementById('storage-confirm-phrase');
let _storageConfirmMode = null;   // 's3' or 'local'

function openStorageConfirm(targetBackend) {
  if (!storageConfirmDialog) return;
  _storageConfirmMode = targetBackend;
  const title = document.getElementById('storage-confirm-title');
  const body = document.getElementById('storage-confirm-body');
  if (targetBackend === 's3') {
    title.textContent = 'Activate S3 storage?';
    body.textContent = 'New uploads and conversion outputs will go to the configured S3 bucket from this moment forward. Files already on local disk remain downloadable until they hit their retention window — this switch does not move or delete them.';
    storageConfirmPhraseEl.textContent = 'enable s3';
  } else {
    title.textContent = 'Revert to local storage?';
    body.textContent = 'New uploads and conversion outputs will go back to /data. Files already in S3 remain downloadable as long as the bucket creds stay valid — this switch does not migrate them off.';
    storageConfirmPhraseEl.textContent = 'use local';
  }
  storageConfirmInput.value = '';
  storageConfirmSubmit.disabled = true;
  document.getElementById('storage-confirm-msg').hidden = true;
  storageConfirmDialog.showModal();
}

if (storageConfirmInput) {
  storageConfirmInput.addEventListener('input', () => {
    storageConfirmSubmit.disabled =
      storageConfirmInput.value.trim().toLowerCase() !== storageConfirmPhraseEl.textContent;
  });
}

if (document.getElementById('storage-confirm-form')) {
  document.getElementById('storage-confirm-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const target = _storageConfirmMode;
    const m = document.getElementById('storage-confirm-msg');
    try {
      await api.patch('/server/settings', { storage_backend: target });
      storageConfirmDialog.close();
      await refreshStorageSection();
    } catch (ex) {
      m.textContent = ex.detail || 'Switch failed';
      m.hidden = false;
    }
  });
}

const s3ActivateBtn = document.getElementById('s3-activate-btn');
if (s3ActivateBtn) s3ActivateBtn.addEventListener('click', () => openStorageConfirm('s3'));
const s3RevertBtn = document.getElementById('s3-revert-btn');
if (s3RevertBtn) s3RevertBtn.addEventListener('click', () => openStorageConfirm('local'));

// One-shot painters called after load() (which we trigger from the
// bottom of this file).
async function refreshStorageSection() {
  try {
    const s = await api.get('/server/settings');
    paintStoragePill(s);
    paintStorageActiveReadout(s);
    paintS3LastTest(s);
    updateActivateButtons(s);
  } catch (_) { /* ignore */ }
}

// ============================================================
//   Database providers — list + dialog (test / create-db / init).
// ============================================================

const dbProviderDialog = document.getElementById('db-provider-dialog');
const dbProviderForm = document.getElementById('db-provider-form');
const dbProvidersTbody = document.getElementById('db-providers-tbody');

let _dbProviders = [];

async function reloadDbProviders() {
  if (!dbProvidersTbody) return;
  try {
    _dbProviders = await api.get('/server/db-providers');
  } catch (_) {
    _dbProviders = [];
  }
  renderDbProvidersTable();
  paintDbProvidersPill();
  await refreshDbPendingBanner();
}

let _dbActiveState = { pending: false, target_redacted: null, active_provider_id: null, active_provider_name: null };

async function refreshDbPendingBanner() {
  const banner = document.getElementById('db-pending-banner');
  if (!banner) return;
  try {
    _dbActiveState = await api.get('/server/db-providers/active/state');
  } catch (_) {
    _dbActiveState = { pending: false };
  }
  if (!_dbActiveState.pending) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  const tgt = document.getElementById('db-pending-target');
  if (tgt) {
    const name = _dbActiveState.active_provider_name
      ? `${_dbActiveState.active_provider_name} — `
      : '';
    tgt.textContent = `${name}${_dbActiveState.target_redacted || '(unknown)'} — restart to apply`;
  }
}

function renderDbProvidersTable() {
  if (!dbProvidersTbody) return;
  dbProvidersTbody.innerHTML = '';
  if (!_dbProviders.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="6" class="muted small empty-state">No database providers yet — click "+ Add provider" to define one.</td>`;
    dbProvidersTbody.appendChild(tr);
    return;
  }
  for (const p of _dbProviders) {
    const tr = document.createElement('tr');
    const lastTest = p.last_test_at
      ? `${new Date(p.last_test_at).toLocaleString()} — ${p.last_test_ok ? 'ok' : 'failed'}`
      : 'never';
    const initd = p.last_init_at ? new Date(p.last_init_at).toLocaleDateString() : '—';
    const migrated = p.last_migrate_at
      ? `${new Date(p.last_migrate_at).toLocaleDateString()} — ${escapeHtml(p.last_migrate_status || '?')}`
      : '—';
    const activeBadge = p.is_active ? ' <span class="role-tag role-admin">active</span>' : '';
    tr.innerHTML = `
      <td>${escapeHtml(p.display_name)}${activeBadge} <em class="muted small">(${escapeHtml(p.slug)})</em></td>
      <td>${escapeHtml(p.kind)}</td>
      <td class="small">${escapeHtml(lastTest)}</td>
      <td class="small">${escapeHtml(initd)}</td>
      <td class="small">${migrated}</td>
      <td><button type="button" class="btn btn-secondary btn-manage" data-edit-id="${p.id}">Edit</button></td>
    `;
    dbProvidersTbody.appendChild(tr);
  }
  dbProvidersTbody.querySelectorAll('[data-edit-id]').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = Number(btn.dataset.editId);
      const row = _dbProviders.find(x => x.id === id);
      if (row) openDbProviderDialog(row);
    });
  });
}

function paintDbProvidersPill() {
  const pill = document.getElementById('db-providers-status-pill');
  if (!pill) return;
  if (!_dbProviders.length) { pill.textContent = ''; pill.className = 'status-pill'; return; }
  const ok = _dbProviders.filter(p => p.last_test_ok === true).length;
  pill.textContent = `${ok}/${_dbProviders.length} tested ok`;
  pill.className = ok === _dbProviders.length ? 'status-pill ok' : 'status-pill';
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

const dbProviderAddBtn = document.getElementById('db-provider-add-btn');
if (dbProviderAddBtn) {
  dbProviderAddBtn.addEventListener('click', () => openDbProviderDialog(null));
}

function openDbProviderDialog(p) {
  if (!dbProviderForm) return;
  const f = dbProviderForm;
  f.id.value = p ? p.id : '';
  f.display_name.value = p ? p.display_name : '';
  f.slug.value = p ? p.slug : '';
  f.slug.disabled = !!p;   // slug is immutable once saved
  f.kind.value = p ? p.kind : 'postgres';
  const cfg = (p && p.config) || {};
  for (const name of ['host', 'port', 'user', 'db_name', 'sslmode', 'db_path']) {
    if (f[name]) f[name].value = cfg[name] ?? '';
  }
  f.secret.value = '';
  const hint = document.getElementById('db-provider-secret-set-hint');
  if (hint) hint.textContent = p && p.secret_set ? '✓ saved' : '';
  document.getElementById('db-provider-title').textContent = p ? `Edit — ${p.display_name}` : 'New database provider';
  document.getElementById('db-provider-delete-btn').hidden = !p;
  // Test / create / init are only meaningful for saved rows.
  document.getElementById('db-provider-test-btn').hidden = !p;
  document.getElementById('db-provider-create-btn').hidden = !p;
  document.getElementById('db-provider-init-btn').hidden = !p;
  // Migrate + Set Active appear on saved rows. Both are gated:
  // migration needs schema initialized; set-active needs both schema
  // initialized AND last_test_ok=true. The backend re-checks too.
  const migrateBtn = document.getElementById('db-provider-migrate-btn');
  const setActiveBtn = document.getElementById('db-provider-setactive-btn');
  if (migrateBtn) {
    migrateBtn.hidden = !p;
    migrateBtn.disabled = !(p && p.last_test_ok && p.last_init_at);
    migrateBtn.title = migrateBtn.disabled
      ? 'Test the connection and initialize schema first'
      : 'Copy every row from the running DB into this target. Source stays online.';
  }
  if (setActiveBtn) {
    setActiveBtn.hidden = !p;
    setActiveBtn.disabled = !(p && p.last_test_ok && p.last_init_at) || (p && p.is_active);
    setActiveBtn.textContent = (p && p.is_active) ? 'Already active' : 'Set as active';
    setActiveBtn.title = setActiveBtn.disabled
      ? (p && p.is_active
          ? 'This provider is already the pending / active target'
          : 'Test the connection and initialize schema first')
      : 'Use this DB on next boot (does not switch until you Restart).';
  }
  applyDbProviderKindVisibility();
  document.getElementById('db-provider-msg').hidden = true;
  dbProviderDialog.showModal();
}

function applyDbProviderKindVisibility() {
  if (!dbProviderForm) return;
  const k = dbProviderForm.kind.value;
  dbProviderForm.querySelectorAll('[data-kind-only]').forEach(el => {
    el.hidden = el.dataset.kindOnly !== k;
  });
  dbProviderForm.querySelectorAll('[data-kind-not]').forEach(el => {
    el.hidden = el.dataset.kindNot === k;
  });
}
if (dbProviderForm) {
  dbProviderForm.kind.addEventListener('change', applyDbProviderKindVisibility);
}

function _readDbProviderForm() {
  const f = dbProviderForm;
  const kind = f.kind.value;
  const cfg = {};
  if (kind === 'sqlite') {
    cfg.db_path = (f.db_path.value || '').trim();
  } else {
    cfg.host = (f.host.value || '').trim();
    if (f.port.value) cfg.port = Number(f.port.value);
    cfg.user = (f.user.value || '').trim();
    cfg.db_name = (f.db_name.value || '').trim();
    if (kind === 'postgres' && f.sslmode.value) cfg.sslmode = f.sslmode.value;
  }
  return {
    display_name: f.display_name.value.trim(),
    slug: f.slug.value.trim(),
    kind,
    config: cfg,
    secret: f.secret.value || null,
  };
}

if (dbProviderForm) {
  dbProviderForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = dbProviderForm.id.value;
    const msgEl = document.getElementById('db-provider-msg');
    msgEl.hidden = true;
    const payload = _readDbProviderForm();
    try {
      if (id) {
        // PATCH — slug is locked, so omit it
        const { slug, ...rest } = payload;
        await api.patch(`/server/db-providers/${id}`, rest);
      } else {
        await api.post('/server/db-providers', payload);
      }
      dbProviderDialog.close();
      await reloadDbProviders();
    } catch (ex) {
      msgEl.textContent = ex.detail || 'Save failed';
      msgEl.className = 'error small';
      msgEl.hidden = false;
    }
  });

  document.getElementById('db-provider-delete-btn').addEventListener('click', async () => {
    const id = dbProviderForm.id.value;
    if (!id) return;
    if (!confirm('Delete this provider? Saved connection details will be lost.')) return;
    try {
      await api.del(`/server/db-providers/${id}`);
      dbProviderDialog.close();
      await reloadDbProviders();
    } catch (ex) {
      const m = document.getElementById('db-provider-msg');
      m.textContent = ex.detail || 'Delete failed';
      m.className = 'error small';
      m.hidden = false;
    }
  });

  async function _runAction(path, busyLabel) {
    const id = dbProviderForm.id.value;
    if (!id) return;
    const m = document.getElementById('db-provider-msg');
    m.textContent = busyLabel + '…';
    m.className = 'muted small';
    m.hidden = false;
    try {
      await api.post(`/server/db-providers/${id}/${path}`, {});
      m.textContent = busyLabel + ' ok';
      m.className = 'ok small';
      await reloadDbProviders();
    } catch (ex) {
      m.textContent = ex.detail || (busyLabel + ' failed');
      m.className = 'error small';
    }
  }
  document.getElementById('db-provider-test-btn').addEventListener('click', () => _runAction('test', 'Test'));
  document.getElementById('db-provider-create-btn').addEventListener('click', () => _runAction('create-db', 'Create DB'));
  document.getElementById('db-provider-init-btn').addEventListener('click', () => _runAction('initialize-schema', 'Initialize schema'));

  // Set Active — requires a typed-phrase confirmation since it's the
  // commit-point for the DB switch. Same UX as the storage activation.
  document.getElementById('db-provider-setactive-btn').addEventListener('click', async () => {
    const id = dbProviderForm.id.value;
    if (!id) return;
    const p = _dbProviders.find(x => x.id === Number(id));
    const name = p ? p.display_name : 'this provider';
    const ans = prompt(
      `Set "${name}" as the next-boot database?\n\n` +
      `This writes /data/active_db.url. The live DB keeps running until ` +
      `you click "Restart server" on the section banner. If the new DB ` +
      `is unreachable at boot, the app falls back to the current one ` +
      `(so you stay reachable).\n\n` +
      `Type "set active" to confirm:`
    );
    if (ans == null) return;
    if (ans.trim().toLowerCase() !== 'set active') {
      const m = document.getElementById('db-provider-msg');
      m.textContent = 'Confirmation phrase did not match — switch cancelled.';
      m.className = 'error small';
      m.hidden = false;
      return;
    }
    await _runAction('set-active', 'Set as active');
    await refreshDbPendingBanner();
  });

  // Try migrate — opens the progress dialog and kicks off the worker.
  // The route returns immediately; we poll for status until done.
  document.getElementById('db-provider-migrate-btn').addEventListener('click', async () => {
    const id = dbProviderForm.id.value;
    if (!id) return;
    if (!confirm(
      'Copy every row from the running database into this target?\n\n' +
      'The source stays online and read-only access is the only thing ' +
      'happening on it. This can take several minutes on a large DB. ' +
      'Target must be empty.'
    )) return;
    const m = document.getElementById('db-provider-msg');
    m.textContent = 'Starting migration…';
    m.className = 'muted small';
    m.hidden = false;
    try {
      await api.post(`/server/db-providers/${id}/migrate`, {});
      openMigrateDialog();
    } catch (ex) {
      m.textContent = ex.detail || 'Migration could not start';
      m.className = 'error small';
    }
  });
}

// ============================================================
//   Migration progress poller + dialog.
// ============================================================

const dbMigrateDialog = document.getElementById('db-migrate-dialog');
let _migratePollHandle = null;

function openMigrateDialog() {
  if (!dbMigrateDialog) return;
  dbMigrateDialog.showModal();
  // Begin polling. Cleanup happens when state leaves 'running'.
  if (_migratePollHandle) clearInterval(_migratePollHandle);
  paintMigrationProgress({ state: 'running', percent: 0 });
  _migratePollHandle = setInterval(pollMigrationStatus, 2000);
  pollMigrationStatus();   // immediate kick
}

async function pollMigrationStatus() {
  try {
    const s = await api.get('/server/db-providers/migrate/status');
    paintMigrationProgress(s);
    if (s.state === 'done' || s.state === 'failed' || s.state === 'idle') {
      if (_migratePollHandle) {
        clearInterval(_migratePollHandle);
        _migratePollHandle = null;
      }
      // Refresh row badges so the "Last migration" cell + status pill
      // reflect the new state without a full page reload.
      await reloadDbProviders();
    }
  } catch (_) { /* network hiccup — keep polling */ }
}

function paintMigrationProgress(s) {
  const bar = document.getElementById('db-migrate-bar');
  const pct = document.getElementById('db-migrate-pct');
  const counts = document.getElementById('db-migrate-counts');
  const tables = document.getElementById('db-migrate-tables');
  const current = document.getElementById('db-migrate-current');
  const title = document.getElementById('db-migrate-title');
  const msg = document.getElementById('db-migrate-msg');
  const percent = Number(s.percent || 0);
  if (bar) bar.style.width = `${percent}%`;
  if (pct) pct.textContent = `${percent.toFixed(1)}%`;
  if (counts) counts.textContent = `${s.rows_copied || 0} / ${s.rows_total || 0} rows`;
  if (tables) tables.textContent = `${s.tables_done || 0} / ${s.tables_total || 0} tables`;
  if (current) current.textContent = s.current_table || '—';
  if (title) {
    if (s.state === 'done') title.textContent = 'Migration complete';
    else if (s.state === 'failed') title.textContent = 'Migration failed';
    else if (s.state === 'idle') title.textContent = 'No migration running';
    else title.textContent = 'Migrating data…';
  }
  if (msg) {
    if (s.state === 'failed' && s.error) {
      msg.textContent = s.error;
      msg.className = 'error small';
      msg.hidden = false;
    } else if (s.state === 'done') {
      msg.textContent = 'All rows copied successfully. You can now Set the target as active and Restart.';
      msg.className = 'ok small';
      msg.hidden = false;
    } else {
      msg.hidden = true;
    }
  }
  // Per-table breakdown.
  const tbody = document.querySelector('#db-migrate-pertable tbody');
  if (tbody) {
    tbody.innerHTML = '';
    const pt = s.per_table || {};
    for (const [name, info] of Object.entries(pt)) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${escapeHtml(name)}</td><td>${info.copied || 0}</td><td>${info.total || 0}</td>`;
      tbody.appendChild(tr);
    }
  }
}

// Snapshot button — one-click SQLite backup of the running DB. Creates
// a new SQLite provider, initializes its schema, and starts a copy.
// Reuses the same progress dialog as Try Migrate.
const dbSnapshotBtn = document.getElementById('db-snapshot-btn');
if (dbSnapshotBtn) {
  dbSnapshotBtn.addEventListener('click', async () => {
    if (!confirm(
      'Create a SQLite backup of the running database?\n\n' +
      'The file will land at /data/vitriol-snapshot-<timestamp>.db. ' +
      'The running DB stays online (read-only access only). ' +
      'After it finishes you can keep the file as a backup, or click ' +
      '"Set as active" on the new row + Restart to promote it.'
    )) return;
    try {
      await api.post('/server/db-providers/snapshot', {});
      await reloadDbProviders();
      openMigrateDialog();
    } catch (ex) {
      alert(ex.detail || 'Snapshot failed to start.');
    }
  });
}

// Pending-switch banner buttons.
const dbPendingCancelBtn = document.getElementById('db-pending-cancel-btn');
if (dbPendingCancelBtn) {
  dbPendingCancelBtn.addEventListener('click', async () => {
    if (!confirm('Cancel the pending DB switch? Next boot will use the current database.')) return;
    try {
      await api.post('/server/db-providers/active/clear', {});
      await reloadDbProviders();
    } catch (ex) {
      alert(ex.detail || 'Could not clear pending switch.');
    }
  });
}
const dbPendingRestartBtn = document.getElementById('db-pending-restart-btn');
if (dbPendingRestartBtn) {
  dbPendingRestartBtn.addEventListener('click', async () => {
    if (!confirm(
      'Restart the server now to apply the pending DB switch?\n\n' +
      'If the new DB is unreachable, the app will fall back to the ' +
      'current one and log the failure (you stay reachable).'
    )) return;
    try {
      await api.post('/server/restart', {});
      alert('Restart requested. Give it a few seconds, then reload the page.');
    } catch (ex) {
      alert(ex.detail || 'Restart failed.');
    }
  });
}

// ============================================================
//   Branding logo — upload + reset.
// ============================================================
//
// Separate from the form-driven settings PATCH because file uploads
// need multipart/form-data, not JSON. The "Reset to default" button
// is only visible when an operator upload exists; toggled by a
// painter that runs after load() so its initial state matches the
// settings response.

const logoFileInput = document.getElementById('logo-file-input');
const logoUploadBtn = document.getElementById('logo-upload-btn');
const logoResetBtn  = document.getElementById('logo-reset-btn');
const logoPreview   = document.getElementById('logo-preview');
const logoMsg       = document.getElementById('logo-msg');

function _setLogoMsg(text, isError) {
  if (!logoMsg) return;
  logoMsg.textContent = text;
  logoMsg.className = (isError ? 'error small' : 'ok small');
  logoMsg.hidden = !text;
}

function _bustLogoCache() {
  // The GET endpoint sets Cache-Control: public, max-age=60 — to make
  // a new upload visible immediately, append a fresh cache-bust query
  // param to both the preview img on this page and the nav-logo img
  // up top. Other pages will pick up the new logo on next navigation.
  if (logoPreview) {
    logoPreview.src = `/api/v1/server/branding/logo?_=${Date.now()}`;
  }
  const navLogo = document.querySelector('.nav-logo');
  if (navLogo) navLogo.src = `/api/v1/server/branding/logo?_=${Date.now()}`;
}

function paintLogoResetVisibility(s) {
  if (!logoResetBtn) return;
  logoResetBtn.hidden = !s.brand_logo_set;
}

if (logoUploadBtn && logoFileInput) {
  logoUploadBtn.addEventListener('click', async () => {
    const f = logoFileInput.files && logoFileInput.files[0];
    if (!f) {
      _setLogoMsg('Pick a file first.', true);
      return;
    }
    // Quick client-side size check so a 50 MB attempt doesn't waste
    // a round trip. Server enforces the same 1 MB cap.
    if (f.size > 1024 * 1024) {
      _setLogoMsg(`File too large (${(f.size / 1024 / 1024).toFixed(2)} MB). Max 1 MB.`, true);
      return;
    }
    _setLogoMsg('Uploading…', false);
    logoUploadBtn.disabled = true;
    try {
      const fd = new FormData();
      fd.append('file', f);
      const r = await fetch('/api/v1/server/branding/logo', {
        method: 'POST', body: fd, credentials: 'same-origin',
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || `Upload failed (${r.status}).`);
      }
      _setLogoMsg('Logo uploaded.', false);
      logoFileInput.value = '';
      _bustLogoCache();
      const s = await api.get('/server/settings');
      paintLogoResetVisibility(s);
    } catch (ex) {
      _setLogoMsg(ex.message || 'Upload failed.', true);
    } finally {
      logoUploadBtn.disabled = false;
    }
  });
}

if (logoResetBtn) {
  logoResetBtn.addEventListener('click', async () => {
    if (!confirm('Reset to the bundled default logo? Your uploaded image will be deleted.')) return;
    _setLogoMsg('Resetting…', false);
    logoResetBtn.disabled = true;
    try {
      await api.del('/server/branding/logo');
      _setLogoMsg('Reverted to default.', false);
      _bustLogoCache();
      const s = await api.get('/server/settings');
      paintLogoResetVisibility(s);
    } catch (ex) {
      _setLogoMsg(ex.detail || 'Reset failed.', true);
    } finally {
      logoResetBtn.disabled = false;
    }
  });
}

// Kick everything off. load() populates the bulk of the form;
// refreshStorageSection + reloadDbProviders paint the new sections
// that need post-load wiring (visibility, pills, table render).
(async () => {
  await load();
  await refreshStorageSection();
  await reloadDbProviders();
  // Initial visibility for the "Reset to default" button.
  try {
    const s = await api.get('/server/settings');
    paintLogoResetVisibility(s);
  } catch (_) { /* ignore */ }
})();
