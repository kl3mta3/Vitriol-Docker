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
};

async function load() {
  const s = await api.get('/server/settings');
  for (const [k, v] of Object.entries(s)) {
    const el = form.elements[k];
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!v;
    else if (v != null) el.value = v;
  }
  // Reflect "this secret is saved" visually on every password slot whose
  // matching `<field>_set` flag is true. Field stays empty (so blank =
  // unchanged on next save) — only the placeholder changes.
  for (const [setFlag, fieldName] of Object.entries(SECRET_FIELD_MAP)) {
    const el = form.elements[fieldName];
    if (!el) continue;
    el.placeholder = s[setFlag] ? SAVED_SECRET_PLACEHOLDER : EMPTY_SECRET_PLACEHOLDER;
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
  document.getElementById('oidc-form-title').textContent = `Add ${tpl.name}`;
  document.getElementById('oidc-form-delete').hidden = true;
  document.getElementById('oidc-form-msg').hidden = true;
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
    // Persist ONLY the SMTP fields — never dispatch a form-wide submit.
    // (A full submit risks browser autofill silently overwriting unrelated
    // fields like `allowed_origin`.) Empty password = leave saved value
    // alone, matching the convention everywhere else in this form.
    const smtpPatch = {
      smtp_host:    (form.elements['smtp_host']    || {}).value || null,
      smtp_port:    (form.elements['smtp_port']    || {}).value
                       ? Number(form.elements['smtp_port'].value) : null,
      smtp_user:    (form.elements['smtp_user']    || {}).value || null,
      smtp_from:    (form.elements['smtp_from']    || {}).value || null,
      smtp_use_tls: !!(form.elements['smtp_use_tls'] && form.elements['smtp_use_tls'].checked),
    };
    const pw = (form.elements['smtp_password'] || {}).value;
    if (pw) smtpPatch.smtp_password = pw;
    await api.patch('/server/settings', smtpPatch);
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
    // Refresh pill state from server so the banner + pill reflect the
    // new last_test_ok flag persisted by the test endpoint.
    try { await load(); } catch {}
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

load();
