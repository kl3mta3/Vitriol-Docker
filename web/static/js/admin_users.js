// Admin users page — single source of truth for the table is `state.users`,
// which we fetch once and re-render on search/sort changes. Edit lives in a
// dialog that opens on row click (or Manage button) and posts the diff.

const tbody = document.getElementById('users-tbody');
const searchInput = document.getElementById('users-search');
const countLabel = document.getElementById('users-count');
const inviteDialog = document.getElementById('invite-dialog');
const inviteForm = document.getElementById('invite-form');
const manageDialog = document.getElementById('manage-dialog');
const manageForm = document.getElementById('manage-form');

const isSuperAdmin = !!window.VITRIOL_IS_SUPERADMIN;
const actorId = window.VITRIOL_ACTOR_ID;

const state = {
  users: [],
  customRoles: [],      // populated for super admin only
  search: '',
  sortKey: 'username',
  sortDir: 'asc',
  current: null,        // user object being edited in the modal
};

document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => {
  b.closest('dialog').close();
}));

// ---------------- Create-user dialog -----------------------------------

document.getElementById('invite-btn').addEventListener('click', () => inviteDialog.showModal());

inviteForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  // The HTML inputs are named new_first_name / new_last_name to dodge
  // browser autofill (which would silently inject the admin's own name
  // into a "first_name" field on every Create dialog open). Map them
  // back to the API's canonical first_name/last_name here.
  if (data.new_first_name && data.new_first_name.trim()) {
    data.first_name = data.new_first_name.trim();
  }
  if (data.new_last_name && data.new_last_name.trim()) {
    data.last_name = data.new_last_name.trim();
  }
  delete data.new_first_name;
  delete data.new_last_name;
  if (!data.password) delete data.password;
  if (!data.email) delete data.email;
  // Name fields are optional — drop empties so the row goes in with
  // NULL rather than an empty string. Both shapes work, but consistent
  // NULLs make admin-side filtering cleaner.
  if (!data.first_name) delete data.first_name;
  if (!data.last_name) delete data.last_name;
  try {
    await api.post('/users', data);
    inviteDialog.close();
    inviteForm.reset();
    await refresh();
  } catch (ex) {
    alert(formatError(ex, 'Create failed'));
  }
});

// ---------------- Search + sort ----------------------------------------
//
// Two-mode search:
//   * Small lists (default) — client-side filter over the already-fetched
//     state.users array. Instant per-keystroke, no network round-trip.
//   * Big lists (> SERVER_SEARCH_THRESHOLD rows) — debounced server-side
//     `?q=` query. Keeps the UI snappy when fetching the whole user list
//     would itself be slow. The /users endpoint already supports `?q=`
//     server-side, so this is purely a wire-up.
//
// 5,000 is the threshold because that's roughly where serializing every
// UserOut starts to push past a megabyte and the in-memory filter loop
// stops feeling instant on lower-end clients. Operators with very big
// user bases automatically get the better behavior without configuring
// anything.

const SERVER_SEARCH_THRESHOLD = 5000;
let _serverSearchDebounce = null;

function _shouldUseServerSearch() {
  return Array.isArray(state.users) && state.users.length > SERVER_SEARCH_THRESHOLD;
}

async function _runServerSearch(query) {
  // Hit /users?q= and replace state.users with the filtered set. We
  // keep state.search empty so the client-side filter (applySearch)
  // becomes a no-op — the rows we got back are already the result.
  try {
    const params = query ? `?q=${encodeURIComponent(query)}` : '';
    const data = await api.get(`/users${params}`);
    if (!Array.isArray(data)) return;
    state.users = data;
    state.search = '';   // disable client-side re-filter for this view
    render();
  } catch (ex) {
    console.error('server search failed', ex);
  }
}

searchInput.addEventListener('input', () => {
  const raw = searchInput.value.trim();
  if (_shouldUseServerSearch()) {
    if (_serverSearchDebounce) clearTimeout(_serverSearchDebounce);
    _serverSearchDebounce = setTimeout(() => _runServerSearch(raw), 250);
    return;
  }
  state.search = raw.toLowerCase();
  render();
});

document.querySelectorAll('th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const k = th.dataset.sort;
    if (state.sortKey === k) {
      state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      state.sortKey = k;
      state.sortDir = 'asc';
    }
    render();
  });
});

function applySearch(users) {
  if (!state.search) return users;
  return users.filter(u => {
    const haystack = `${u.username || ''} ${u.email || ''} ${u.first_name || ''} ${u.last_name || ''}`.toLowerCase();
    return haystack.includes(state.search);
  });
}

function applySort(users) {
  const k = state.sortKey;
  const dir = state.sortDir === 'asc' ? 1 : -1;
  return [...users].sort((a, b) => {
    const av = (a[k] || '').toString().toLowerCase();
    const bv = (b[k] || '').toString().toLowerCase();
    if (av < bv) return -1 * dir;
    if (av > bv) return  1 * dir;
    return 0;
  });
}

function updateSortIndicators() {
  document.querySelectorAll('th[data-sort]').forEach(th => {
    const ind = th.querySelector('.sort-ind');
    if (!ind) return;
    if (th.dataset.sort === state.sortKey) {
      ind.textContent = state.sortDir === 'asc' ? '▲' : '▼';
      th.classList.add('sorted');
    } else {
      ind.textContent = '';
      th.classList.remove('sorted');
    }
  });
}

// ---------------- Render -----------------------------------------------
// (render() is defined further down, near refresh())

function buildRow(u) {
  const tr = document.createElement('tr');
  tr.className = 'user-row';
  tr.dataset.userId = u.id;

  const statusText = u.status === 'suspended' && u.suspended_until
    ? `suspended → ${new Date(u.suspended_until).toLocaleString()}`
    : u.status;

  // When a custom role is assigned, show its name with the base ceiling
  // as a small annotation so admins know what powers are at play.
  const roleHtml = u.custom_role_name
    ? `<span class="role-tag role-${u.role}" title="custom role (base: ${u.role})">${escapeHtml(u.custom_role_name)}</span>`
    : `<span class="role-tag role-${u.role}">${u.role}</span>`;

  // Real-world name shown as a small secondary line under the username.
  // Hidden entirely when both first + last are blank, so unfilled rows
  // don't get a stray empty line. Trim coalesces "First " (no last
  // name) and " Last" (no first) to a single visible token.
  const fullName = `${u.first_name || ''} ${u.last_name || ''}`.trim();
  const nameHtml = fullName
    ? `<div class="muted small">${escapeHtml(fullName)}</div>`
    : '';

  tr.innerHTML = `
    <td>${escapeHtml(u.username)}${nameHtml}</td>
    <td>${escapeHtml(u.email || '')}</td>
    <td>${roleHtml}</td>
    <td>${escapeHtml(statusText)}</td>
    <td class="center">${u.stone_enabled ? '✓' : ''}</td>
    <td class="center">${u.self_compile_enabled ? '✓' : ''}</td>
    <td class="row-manage-cell"></td>
  `;

  const cell = tr.querySelector('.row-manage-cell');
  if (canManage(u)) {
    const btn = document.createElement('button');
    // Pending users get a different-styled button that opens the
    // review-and-approve flow instead of the regular manage modal.
    if (u.role === 'pending') {
      btn.className = 'btn btn-primary btn-manage btn-pending';
      btn.textContent = 'Pending — review';
      btn.addEventListener('click', (e) => { e.stopPropagation(); openPending(u); });
      tr.addEventListener('click', () => openPending(u));
    } else {
      btn.className = 'btn btn-secondary btn-manage';
      btn.textContent = 'Manage';
      btn.addEventListener('click', (e) => { e.stopPropagation(); openManage(u); });
      tr.addEventListener('click', () => openManage(u));
    }
    cell.appendChild(btn);
    tr.classList.add('clickable');
  } else {
    const tag = document.createElement('span');
    tag.className = 'muted small';
    tag.textContent = 'view only';
    cell.appendChild(tag);
  }
  return tr;
}

function canManage(u) {
  // The server already filters super admins out of an admin's list, but
  // belt-and-suspenders here in case the API ever changes.
  if (u.role === 'super_admin' && !isSuperAdmin) return false;
  if (u.role === 'admin' && !isSuperAdmin && u.id !== actorId) return false;
  return true;
}

// ---------------- Pending-review dialog --------------------------------

const pendingDialog = document.getElementById('pending-dialog');
const pendingForm = document.getElementById('pending-form');

function openPending(u) {
  state.current = u;
  pendingForm.user_id.value = u.id;
  document.getElementById('pending-username').textContent = u.username || '';
  document.getElementById('pending-email').textContent = u.email || '(none provided)';
  document.getElementById('pending-email-verified').textContent =
    u.email_verified_at ? new Date(u.email_verified_at).toLocaleString() : 'no';
  document.getElementById('pending-created').textContent =
    u.created_at ? new Date(u.created_at).toLocaleString() : '';
  // Hide the Admin role option unless the actor is super admin.
  const adminOpt = pendingForm.role.querySelector('option[data-superadmin-only]');
  if (adminOpt) adminOpt.hidden = !isSuperAdmin;
  pendingForm.role.value = 'user';
  hide(document.getElementById('pending-msg'));
  pendingDialog.showModal();
}

pendingForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const u = state.current;
  if (!u) return;
  const role = pendingForm.role.value;
  const msg = document.getElementById('pending-msg');
  msg.hidden = true;
  try {
    await api.post(`/users/${u.id}/approve`, { role });
    pendingDialog.close();
    await refresh();
  } catch (ex) {
    msg.textContent = formatError(ex, 'Approve failed');
    msg.className = 'error small';
    msg.hidden = false;
  }
});

document.getElementById('pending-deny').addEventListener('click', async () => {
  const u = state.current;
  if (!u) return;
  if (!confirm(`Deny and remove the pending sign-up for ${u.username}? This cannot be undone.`)) return;
  try {
    await api.post(`/users/${u.id}/deny`, {});
    pendingDialog.close();
    await refresh();
  } catch (ex) {
    const msg = document.getElementById('pending-msg');
    msg.textContent = formatError(ex, 'Deny failed');
    msg.className = 'error small';
    msg.hidden = false;
  }
});

// ---------------- Manage dialog ----------------------------------------

function openManage(u) {
  state.current = u;
  const f = manageForm;
  f.user_id.value = u.id;
  f.username.value = u.username || '';
  f.email.value = u.email || '';
  // The HTML inputs are named manage_first_name / manage_last_name to
  // dodge browser autofill — see comment in admin_users.html.
  if (f.manage_first_name) f.manage_first_name.value = u.first_name || '';
  if (f.manage_last_name) f.manage_last_name.value = u.last_name || '';
  f.role.value = u.role;
  f.new_password.value = '';
  f.stone_enabled.checked = !!u.stone_enabled;
  f.self_compile_enabled.checked = !!u.self_compile_enabled;
  f.daily_conversion_limit.value = u.daily_conversion_limit ?? '';
  f.rate_limit_per_minute.value = u.rate_limit_per_minute ?? '';
  f.queue_weight_override.value = u.queue_weight_override ?? '';

  // Per-user max-upload-size override: only show this row when the
  // calling admin holds the `set_user_file_size_cap` capability. The
  // backend enforces the same gate; this just keeps the UI honest so
  // an admin who can't change the value also doesn't see a field that
  // appears editable. Capability list is set on the page by base.html
  // (window.VITRIOL_CAPS) — fall back to an empty list if it's missing
  // somehow (older deployments / cached pages).
  const _caps = window.VITRIOL_CAPS || [];
  const _canEditFileSizeCap = _caps.includes('set_user_file_size_cap');
  const fileSizeRow = document.getElementById('manage-file-size-row');
  if (fileSizeRow) fileSizeRow.hidden = !_canEditFileSizeCap;
  if (f.max_file_size_mb) {
    // Server stores bytes; UI works in MB. Round to nearest MB on
    // display; the user can type a fresh value to override.
    f.max_file_size_mb.value = u.max_file_size_bytes
      ? Math.round(u.max_file_size_bytes / (1024 * 1024))
      : '';
  }
  if (f.max_output_size_mb) {
    f.max_output_size_mb.value = u.max_output_size_bytes
      ? Math.round(u.max_output_size_bytes / (1024 * 1024))
      : '';
  }
  if (f.max_storage_mb) {
    f.max_storage_mb.value = u.max_storage_bytes
      ? Math.round(u.max_storage_bytes / (1024 * 1024))
      : '';
  }

  // Per-user retention override — same capability-gated row pattern.
  const _canEditRetention = _caps.includes('set_user_retention');
  const retRow = document.getElementById('manage-retention-row');
  if (retRow) retRow.hidden = !_canEditRetention;
  const ret = u.output_retention || {};
  if (f.ret_max_files) f.ret_max_files.value = (ret.max_files ?? '') === '' ? '' : String(ret.max_files);
  if (f.ret_max_age) f.ret_max_age.value = (ret.max_age ?? '') === '' ? '' : String(ret.max_age);
  if (f.ret_age_unit) f.ret_age_unit.value = ret.age_unit || '';
  if (f.ret_delete_on_download) f.ret_delete_on_download.checked = !!ret.delete_on_download;

  document.getElementById('manage-title').textContent = `Manage — ${u.username}`;
  const roleTag = document.getElementById('manage-role-tag');
  roleTag.textContent = u.custom_role_name || u.role;
  roleTag.className = `role-tag role-${u.role}`;

  // Rebuild role dropdown: built-ins first, then custom roles (super
  // admin only — admins can't see or assign custom roles to keep the
  // user management surface explicit). Selected value encodes either
  // 'builtin:<role>' or 'custom:<id>' so the save handler can route
  // PATCH vs custom_role_id appropriately.
  rebuildManageRoleSelect(u);

  // Show/hide the Admin role option based on actor.
  const adminOption = f.role.querySelector('option[value="builtin:admin"]');
  if (adminOption) adminOption.hidden = !isSuperAdmin;

  // What can this actor actually do?
  const adminEditingAdmin = !isSuperAdmin && u.role === 'admin' && u.id !== actorId;
  const editingSelf = u.id === actorId;

  // Admin → other admin: only suspend/unsuspend allowed.
  const note = document.getElementById('manage-readonly-note');
  if (adminEditingAdmin) {
    note.textContent = 'You can suspend or unsuspend this admin. Other changes require the super admin.';
    note.hidden = false;
  } else {
    note.hidden = true;
  }
  setReadOnly(f, adminEditingAdmin);

  // Hide Delete for self-edit and for admin-on-admin.
  const delBtn = document.getElementById('manage-delete');
  delBtn.hidden = editingSelf || adminEditingAdmin || u.role === 'super_admin';

  // Status section — disable ban/suspend buttons that don't apply.
  const banBtn = manageDialog.querySelector('[data-status-action="ban"]');
  banBtn.hidden = (u.role === 'admin' && !isSuperAdmin) || editingSelf;

  document.getElementById('manage-status-current').textContent =
    `Current: ${u.status}${u.suspended_until ? ' until ' + new Date(u.suspended_until).toLocaleString() : ''}`;
  hide(document.getElementById('manage-status-msg'));

  manageDialog.showModal();
}

function setReadOnly(form, ro) {
  // Lock everything except the Status fieldset when an admin is editing
  // another admin (suspend-only path).
  const sections = form.querySelectorAll('details');
  sections.forEach(d => {
    const isStatus = d.querySelector('.status-actions');
    const inputs = d.querySelectorAll('input, select, button:not([data-status-action]):not([data-close])');
    inputs.forEach(el => {
      // Always allow Close.
      if (el.dataset && el.dataset.close !== undefined) return;
      el.disabled = ro && !isStatus;
    });
  });
  // Submit (Save changes) — disabled in read-only mode.
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = ro;
}

document.getElementById('show-new-password').addEventListener('change', (e) => {
  manageForm.new_password.type = e.target.checked ? 'text' : 'password';
});

function rebuildManageRoleSelect(u) {
  const sel = manageForm.role;
  sel.innerHTML = '';
  // Built-ins.
  const builtins = [
    ['viewer',  'Viewer'],
    ['pending', 'Pending'],
    ['user',    'User'],
    ['admin',   'Admin (built-in)'],
  ];
  for (const [val, label] of builtins) {
    const opt = document.createElement('option');
    opt.value = `builtin:${val}`;
    opt.textContent = label;
    if (val === 'admin') opt.dataset.superadminOnly = '';
    sel.appendChild(opt);
  }
  // Custom roles (super admin only).
  if (isSuperAdmin && state.customRoles.length) {
    const sep = document.createElement('option');
    sep.disabled = true;
    sep.textContent = '— custom roles —';
    sel.appendChild(sep);
    for (const cr of state.customRoles) {
      const opt = document.createElement('option');
      opt.value = `custom:${cr.id}`;
      opt.textContent = `${cr.name}  (base: ${cr.base_role})`;
      sel.appendChild(opt);
    }
  }
  // Pre-select the user's current role.
  sel.value = u.custom_role_id ? `custom:${u.custom_role_id}` : `builtin:${u.role}`;
}

// Status action buttons — call dedicated endpoints.
manageDialog.querySelectorAll('[data-status-action]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const u = state.current;
    if (!u) return;
    const msg = document.getElementById('manage-status-msg');
    msg.hidden = true;
    msg.className = 'ok';
    const action = btn.dataset.statusAction;
    try {
      let r;
      if (action === 'active') {
        if (u.status === 'banned') r = await api.post(`/users/${u.id}/unban`, {});
        else if (u.status === 'suspended') r = await api.post(`/users/${u.id}/unsuspend`, {});
        else r = { message: 'Already active.' };
      } else if (action === 'ban') {
        if (!confirm(`Ban ${u.username}? They lose access immediately.`)) return;
        r = await api.post(`/users/${u.id}/ban`, {});
      } else if (action.startsWith('suspend-')) {
        const duration = action.split('-')[1];
        r = await api.post(`/users/${u.id}/suspend`, { duration });
      }
      msg.textContent = r.message || 'Updated.';
      msg.hidden = false;
      await refresh();
      // Re-open with fresh data so the dialog reflects the new status.
      const updated = state.users.find(x => x.id === u.id);
      if (updated) {
        state.current = updated;
        document.getElementById('manage-status-current').textContent =
          `Current: ${updated.status}${updated.suspended_until ? ' until ' + new Date(updated.suspended_until).toLocaleString() : ''}`;
      }
    } catch (ex) {
      msg.textContent = formatError(ex, 'Status change failed');
      msg.className = 'error';
      msg.hidden = false;
    }
  });
});

// Save (Account + Capabilities + optional password reset).
manageForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const u = state.current;
  if (!u) return;

  // Decode the role select. Values come in as 'builtin:<role>' or
  // 'custom:<id>'; the server takes role + custom_role_id separately.
  const [roleKind, roleVal] = (manageForm.role.value || 'builtin:user').split(':');

  const formValues = {
    username: manageForm.username.value.trim(),
    email: manageForm.email.value.trim(),
    // Read from the autofill-resistant input names. Payload key stays
    // first_name/last_name because that's what the server expects.
    first_name: manageForm.manage_first_name ? manageForm.manage_first_name.value.trim() : '',
    last_name: manageForm.manage_last_name ? manageForm.manage_last_name.value.trim() : '',
    role: roleKind === 'builtin' ? roleVal : null,
    custom_role_id: roleKind === 'custom' ? Number(roleVal) : 0,
    new_password: manageForm.new_password.value,
    stone_enabled: manageForm.stone_enabled.checked,
    self_compile_enabled: manageForm.self_compile_enabled.checked,
    daily_conversion_limit: manageForm.daily_conversion_limit.value === ''
      ? null : Number(manageForm.daily_conversion_limit.value),
    rate_limit_per_minute: manageForm.rate_limit_per_minute.value === ''
      ? null : Number(manageForm.rate_limit_per_minute.value),
    queue_weight_override: manageForm.queue_weight_override.value === ''
      ? null : Number(manageForm.queue_weight_override.value),
    // Convert MB → bytes on save; empty or 0 → null = inherit from
    // role/server default. Only meaningful when the actor has the
    // capability (the row is hidden otherwise — and even if the field
    // sneaks in, the backend re-checks the capability and 403s).
    max_file_size_bytes: (() => {
      const el = manageForm.max_file_size_mb;
      if (!el || el.value === '') return null;
      const mb = Number(el.value);
      return mb > 0 ? Math.round(mb * 1024 * 1024) : 0;
    })(),
    max_output_size_bytes: (() => {
      const el = manageForm.max_output_size_mb;
      if (!el || el.value === '') return null;
      const mb = Number(el.value);
      return mb > 0 ? Math.round(mb * 1024 * 1024) : 0;
    })(),
    max_storage_bytes: (() => {
      const el = manageForm.max_storage_mb;
      if (!el || el.value === '') return null;
      const mb = Number(el.value);
      return mb > 0 ? Math.round(mb * 1024 * 1024) : 0;
    })(),
  };

  const currentCustomId = u.custom_role_id || 0;

  // Diff: PATCH only fields the server route handles directly.
  const patch = {};
  if (formValues.email !== (u.email || '')) patch.email = formValues.email || null;
  // Name fields — server treats empty string as "clear", non-empty as
  // "set". Only ship when actually changed so a save that didn't touch
  // them doesn't accidentally null them out.
  if (formValues.first_name !== (u.first_name || '')) patch.first_name = formValues.first_name;
  if (formValues.last_name !== (u.last_name || '')) patch.last_name = formValues.last_name;
  if (formValues.role && formValues.role !== u.role) patch.role = formValues.role;
  if (formValues.custom_role_id !== currentCustomId) patch.custom_role_id = formValues.custom_role_id;
  if (formValues.stone_enabled !== !!u.stone_enabled) patch.stone_enabled = formValues.stone_enabled;
  if (formValues.self_compile_enabled !== !!u.self_compile_enabled) patch.self_compile_enabled = formValues.self_compile_enabled;
  if (formValues.daily_conversion_limit !== (u.daily_conversion_limit ?? null))
    patch.daily_conversion_limit = formValues.daily_conversion_limit;
  if (formValues.rate_limit_per_minute !== (u.rate_limit_per_minute ?? null))
    patch.rate_limit_per_minute = formValues.rate_limit_per_minute;
  if (formValues.queue_weight_override !== (u.queue_weight_override ?? null))
    patch.queue_weight_override = formValues.queue_weight_override;
  // Only ship the file-size field if the actor has the capability AND
  // the value actually changed. Without the capability the backend
  // would 403 — easier to skip the field entirely.
  const _saveCanEditCap = (window.VITRIOL_CAPS || []).includes('set_user_file_size_cap');
  if (_saveCanEditCap
      && formValues.max_file_size_bytes !== (u.max_file_size_bytes ?? null)) {
    patch.max_file_size_bytes = formValues.max_file_size_bytes;
  }
  // Same capability gate for max_output_size_bytes + max_storage_bytes.
  if (_saveCanEditCap
      && formValues.max_output_size_bytes !== (u.max_output_size_bytes ?? null)) {
    patch.max_output_size_bytes = formValues.max_output_size_bytes;
  }
  if (_saveCanEditCap
      && formValues.max_storage_bytes !== (u.max_storage_bytes ?? null)) {
    patch.max_storage_bytes = formValues.max_storage_bytes;
  }

  // Same shape for the retention override. The backend treats an empty
  // dict as "clear the override"; a populated dict has its keys
  // whitelisted server-side.
  const _saveCanEditRetention = (window.VITRIOL_CAPS || []).includes('set_user_retention');
  if (_saveCanEditRetention) {
    const _retOut = {};
    const _mf = manageForm.ret_max_files && manageForm.ret_max_files.value;
    const _ma = manageForm.ret_max_age && manageForm.ret_max_age.value;
    const _au = manageForm.ret_age_unit && manageForm.ret_age_unit.value;
    const _dd = manageForm.ret_delete_on_download && manageForm.ret_delete_on_download.checked;
    if (_mf !== '' && _mf != null) _retOut.max_files = Number(_mf);
    if (_ma !== '' && _ma != null) _retOut.max_age = Number(_ma);
    if (_au) _retOut.age_unit = _au;
    if (_dd) _retOut.delete_on_download = true;
    // Diff against the row we loaded — only ship if the user actually
    // touched something. Stringify-compare keeps it simple for a small
    // dict with stable key order on our side.
    const _existing = u.output_retention || {};
    const _existingNorm = {};
    if (_existing.max_files != null) _existingNorm.max_files = Number(_existing.max_files);
    if (_existing.max_age != null) _existingNorm.max_age = Number(_existing.max_age);
    if (_existing.age_unit) _existingNorm.age_unit = _existing.age_unit;
    if (_existing.delete_on_download) _existingNorm.delete_on_download = true;
    if (JSON.stringify(_retOut) !== JSON.stringify(_existingNorm)) {
      patch.output_retention = _retOut;
    }
  }

  // Username and password go through reset-credentials.
  const resetReq = {};
  if (formValues.username && formValues.username !== u.username) resetReq.new_username = formValues.username;
  if (formValues.new_password) resetReq.new_password = formValues.new_password;

  try {
    if (Object.keys(patch).length) await api.patch(`/users/${u.id}`, patch);
    if (Object.keys(resetReq).length) await api.post(`/users/${u.id}/reset-credentials`, resetReq);
    manageDialog.close();
    await refresh();
  } catch (ex) {
    alert(formatError(ex, 'Save failed'));
  }
});

// Delete user.
document.getElementById('manage-delete').addEventListener('click', async () => {
  const u = state.current;
  if (!u) return;
  if (!confirm(`Permanently delete ${u.username}? This cannot be undone.`)) return;
  try {
    await api.del(`/users/${u.id}`);
    manageDialog.close();
    await refresh();
  } catch (ex) {
    alert(formatError(ex, 'Delete failed'));
  }
});

// ---------------- Helpers ----------------------------------------------

function formatError(ex, fallback) {
  if (!ex) return fallback;
  const d = ex.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map(e => {
      const field = Array.isArray(e.loc) ? e.loc.filter(p => p !== 'body').join('.') : '';
      return field ? `${field}: ${e.msg}` : e.msg;
    }).join('; ');
  }
  return ex.message || fallback;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function hide(el) { if (el) el.hidden = true; }

// ---------------- Manage roles dialog ----------------------------------

const rolesDialog = document.getElementById('roles-dialog');
const rolesTbody  = document.getElementById('roles-tbody');
const roleFormDialog = document.getElementById('role-form-dialog');
const roleForm = document.getElementById('role-form');

const rolesBtn = document.getElementById('manage-roles-btn');
if (rolesBtn) rolesBtn.addEventListener('click', async () => {
  await loadRoles();
  rolesDialog.showModal();
});

async function loadRoles() {
  if (!isSuperAdmin) return;
  try {
    const data = await api.get('/roles');
    if (!Array.isArray(data)) return;
    state.customRoles = data;
    renderRolesTable();
  } catch (ex) {
    console.error('roles load failed', ex);
    state.customRoles = [];
  }
}

function renderRolesTable() {
  if (!rolesTbody) return;
  rolesTbody.innerHTML = '';
  if (!state.customRoles.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="6" class="empty-state">No custom roles yet — click "+ New role" to create one.</td>`;
    rolesTbody.appendChild(tr);
    return;
  }
  for (const cr of state.customRoles) {
    const tr = document.createElement('tr');
    tr.className = 'user-row clickable';
    tr.innerHTML = `
      <td>${escapeHtml(cr.name)}</td>
      <td><span class="role-tag role-${cr.base_role}">${cr.base_role}</span></td>
      <td class="center">${cr.stone_enabled ? '✓' : ''}</td>
      <td class="center">${cr.self_compile_enabled ? '✓' : ''}</td>
      <td>${cr.user_count}</td>
      <td class="row-manage-cell"></td>
    `;
    const cell = tr.querySelector('.row-manage-cell');
    const btn = document.createElement('button');
    btn.className = 'btn btn-secondary btn-manage';
    btn.textContent = 'Edit';
    btn.addEventListener('click', (e) => { e.stopPropagation(); openRoleForm(cr); });
    cell.appendChild(btn);
    tr.addEventListener('click', () => openRoleForm(cr));
    rolesTbody.appendChild(tr);
  }
}

if (document.getElementById('roles-new-btn')) {
  document.getElementById('roles-new-btn').addEventListener('click', () => openRoleForm(null));
}

function openRoleForm(cr) {
  const f = roleForm;
  f.id.value = cr ? cr.id : '';
  document.getElementById('role-form-title').textContent = cr ? `Edit role — ${cr.name}` : 'New role';
  f.name.value = cr ? cr.name : '';
  f.description.value = cr && cr.description ? cr.description : '';
  f.base_role.value = cr ? cr.base_role : 'user';
  for (const flag of [
    'stone_enabled', 'self_compile_enabled',
    'can_view_users_tab', 'can_create_user', 'can_suspend_user', 'can_ban_user',
    'can_approve_pending', 'can_grant_stone', 'can_grant_self_compile',
    'can_reset_other_creds', 'can_restart_server',
    // Files-tab caps — own_files works at any base; the *_others ones
    // are admin-tier and are forced off when base != admin (see
    // updateRoleAdminBlockVisibility below).
    'can_view_own_files', 'can_view_others_files',
    'can_download_others_files', 'can_delete_others_files',
    // Per-user file-size-cap edit permission — also admin-tier.
    'can_set_user_file_size_cap',
    // Per-user retention edit permission — also admin-tier.
    'can_set_user_retention',
  ]) {
    if (f[flag]) f[flag].checked = !!(cr && cr[flag]);
  }
  f.daily_conversion_limit.value = cr && cr.daily_conversion_limit != null ? cr.daily_conversion_limit : '';
  f.rate_limit_per_minute.value = cr && cr.rate_limit_per_minute != null ? cr.rate_limit_per_minute : '';
  f.queue_weight.value = cr && cr.queue_weight != null ? cr.queue_weight : '';
  // Role-level upload cap + sibling output/storage caps, stored as
  // bytes server-side, edited as MB. Same blank-or-zero-=-inherit
  // semantics as the per-user fields on the manage-user dialog.
  if (f.max_file_size_mb) {
    f.max_file_size_mb.value = (cr && cr.max_file_size_bytes)
      ? Math.round(cr.max_file_size_bytes / (1024 * 1024))
      : '';
  }
  if (f.max_output_size_mb) {
    f.max_output_size_mb.value = (cr && cr.max_output_size_bytes)
      ? Math.round(cr.max_output_size_bytes / (1024 * 1024))
      : '';
  }
  if (f.max_storage_mb) {
    f.max_storage_mb.value = (cr && cr.max_storage_bytes)
      ? Math.round(cr.max_storage_bytes / (1024 * 1024))
      : '';
  }
  // Role-level retention override. Empty fields = inherit from server
  // default for the base role.
  const _crRet = (cr && cr.output_retention) || {};
  if (f.ret_max_files) f.ret_max_files.value = (_crRet.max_files ?? '') === '' ? '' : String(_crRet.max_files);
  if (f.ret_max_age) f.ret_max_age.value = (_crRet.max_age ?? '') === '' ? '' : String(_crRet.max_age);
  if (f.ret_age_unit) f.ret_age_unit.value = _crRet.age_unit || '';
  if (f.ret_delete_on_download) f.ret_delete_on_download.checked = !!_crRet.delete_on_download;
  document.getElementById('role-delete-btn').hidden = !cr;
  document.getElementById('role-form-msg').hidden = true;
  updateRoleAdminBlockVisibility();
  roleFormDialog.showModal();
}

function updateRoleAdminBlockVisibility() {
  const block = document.querySelector('.role-admin-block');
  if (!block) return;
  const isAdmin = roleForm.base_role.value === 'admin';
  block.style.opacity = isAdmin ? '1' : '0.5';
  // Disable admin-tier flag inputs when base != admin so they can't be
  // toggled on by mistake; the API would reject them anyway, but the
  // UI hint is clearer than a 400 error.
  block.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.disabled = !isAdmin;
    if (!isAdmin) cb.checked = false;
  });
  document.getElementById('role-base-hint').textContent = isAdmin
    ? 'Admin base — admin-tier capabilities below are unlocked.'
    : `Base "${roleForm.base_role.value}" — admin-tier capabilities are disabled. Switch base to Admin to enable them.`;
}
// Everything below operates on the role-form dialog, which only exists
// in the DOM when the actor is a super admin (the template hides it for
// everyone else). Guard the whole block so admin / custom-role users
// don't blow up on load.
if (roleForm) {
roleForm.base_role.addEventListener('change', updateRoleAdminBlockVisibility);

roleForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = roleForm.id.value;
  const msg = document.getElementById('role-form-msg');
  msg.hidden = true;
  const data = {
    name: roleForm.name.value.trim(),
    description: roleForm.description.value.trim() || null,
    base_role: roleForm.base_role.value,
    stone_enabled: roleForm.stone_enabled.checked,
    self_compile_enabled: roleForm.self_compile_enabled.checked,
    daily_conversion_limit: roleForm.daily_conversion_limit.value === '' ? null : Number(roleForm.daily_conversion_limit.value),
    rate_limit_per_minute: roleForm.rate_limit_per_minute.value === '' ? null : Number(roleForm.rate_limit_per_minute.value),
    queue_weight: roleForm.queue_weight.value === '' ? null : Number(roleForm.queue_weight.value),
    can_view_users_tab: roleForm.can_view_users_tab.checked,
    can_create_user: roleForm.can_create_user.checked,
    can_suspend_user: roleForm.can_suspend_user.checked,
    can_ban_user: roleForm.can_ban_user.checked,
    can_approve_pending: roleForm.can_approve_pending.checked,
    can_grant_stone: roleForm.can_grant_stone.checked,
    can_grant_self_compile: roleForm.can_grant_self_compile.checked,
    can_reset_other_creds: roleForm.can_reset_other_creds.checked,
    can_restart_server: roleForm.can_restart_server.checked,
    can_view_own_files: roleForm.can_view_own_files ? roleForm.can_view_own_files.checked : false,
    can_view_others_files: roleForm.can_view_others_files ? roleForm.can_view_others_files.checked : false,
    can_download_others_files: roleForm.can_download_others_files ? roleForm.can_download_others_files.checked : false,
    can_delete_others_files: roleForm.can_delete_others_files ? roleForm.can_delete_others_files.checked : false,
    can_set_user_file_size_cap: roleForm.can_set_user_file_size_cap ? roleForm.can_set_user_file_size_cap.checked : false,
    can_set_user_retention: roleForm.can_set_user_retention ? roleForm.can_set_user_retention.checked : false,
    max_file_size_bytes: (() => {
      const el = roleForm.max_file_size_mb;
      if (!el || el.value === '') return null;
      const mb = Number(el.value);
      return mb > 0 ? Math.round(mb * 1024 * 1024) : 0;
    })(),
    max_output_size_bytes: (() => {
      const el = roleForm.max_output_size_mb;
      if (!el || el.value === '') return null;
      const mb = Number(el.value);
      return mb > 0 ? Math.round(mb * 1024 * 1024) : 0;
    })(),
    max_storage_bytes: (() => {
      const el = roleForm.max_storage_mb;
      if (!el || el.value === '') return null;
      const mb = Number(el.value);
      return mb > 0 ? Math.round(mb * 1024 * 1024) : 0;
    })(),
    output_retention: (() => {
      const out = {};
      const mf = roleForm.ret_max_files && roleForm.ret_max_files.value;
      const ma = roleForm.ret_max_age && roleForm.ret_max_age.value;
      const au = roleForm.ret_age_unit && roleForm.ret_age_unit.value;
      const dd = roleForm.ret_delete_on_download && roleForm.ret_delete_on_download.checked;
      if (mf !== '' && mf != null) out.max_files = Number(mf);
      if (ma !== '' && ma != null) out.max_age = Number(ma);
      if (au) out.age_unit = au;
      if (dd) out.delete_on_download = true;
      return out;   // empty dict = clear the role-level override
    })(),
  };
  try {
    if (id) await api.patch(`/roles/${id}`, data);
    else    await api.post('/roles', data);
    roleFormDialog.close();
    await loadRoles();
    await refresh();   // user rows show role names — refresh in case any changed
  } catch (ex) {
    msg.textContent = formatError(ex, 'Save failed');
    msg.className = 'error small';
    msg.hidden = false;
  }
});

document.getElementById('role-delete-btn').addEventListener('click', async () => {
  const id = roleForm.id.value;
  if (!id) return;
  if (!confirm('Delete this role? Any users currently assigned will fall back to their built-in base role.')) return;
  try {
    await api.del(`/roles/${id}`);
    roleFormDialog.close();
    await loadRoles();
    await refresh();
  } catch (ex) {
    const msg = document.getElementById('role-form-msg');
    msg.textContent = formatError(ex, 'Delete failed');
    msg.className = 'error small';
    msg.hidden = false;
  }
});
} // end if (roleForm) — see comment above the block

async function refresh() {
  try {
    const data = await api.get('/users');
    // api.get returns undefined when the call hit 401 (redirecting). Don't
    // overwrite users with undefined — leave whatever was there and bail
    // so render() doesn't crash on a non-array.
    if (!Array.isArray(data)) return;
    state.users = data;
    if (isSuperAdmin) await loadRoles();
    render();
  } catch (ex) {
    console.error('Failed to load users:', ex);
    countLabel.textContent = `Failed to load: ${formatError(ex, 'unknown error')}`;
    countLabel.className = 'error';
    state.users = [];
    render();
  }
}

function render() {
  updateSortIndicators();
  const filtered = applySort(applySearch(state.users || []));
  tbody.innerHTML = '';
  if (filtered.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="7" class="empty-state">${state.search ? 'No users match that search.' : 'No users yet.'}</td>`;
    tbody.appendChild(tr);
  } else {
    for (const u of filtered) tbody.appendChild(buildRow(u));
  }
  countLabel.textContent = state.search
    ? `${filtered.length} of ${state.users.length}`
    : `${state.users.length} user${state.users.length === 1 ? '' : 's'}`;
}

refresh();
