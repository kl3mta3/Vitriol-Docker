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
  if (!data.password) delete data.password;
  if (!data.email) delete data.email;
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

searchInput.addEventListener('input', () => {
  state.search = searchInput.value.trim().toLowerCase();
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
    const haystack = `${u.username || ''} ${u.email || ''}`.toLowerCase();
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

  tr.innerHTML = `
    <td>${escapeHtml(u.username)}</td>
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
  f.role.value = u.role;
  f.new_password.value = '';
  f.stone_enabled.checked = !!u.stone_enabled;
  f.self_compile_enabled.checked = !!u.self_compile_enabled;
  f.daily_conversion_limit.value = u.daily_conversion_limit ?? '';
  f.rate_limit_per_minute.value = u.rate_limit_per_minute ?? '';

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
    role: roleKind === 'builtin' ? roleVal : null,
    custom_role_id: roleKind === 'custom' ? Number(roleVal) : 0,
    new_password: manageForm.new_password.value,
    stone_enabled: manageForm.stone_enabled.checked,
    self_compile_enabled: manageForm.self_compile_enabled.checked,
    daily_conversion_limit: manageForm.daily_conversion_limit.value === ''
      ? null : Number(manageForm.daily_conversion_limit.value),
    rate_limit_per_minute: manageForm.rate_limit_per_minute.value === ''
      ? null : Number(manageForm.rate_limit_per_minute.value),
  };

  const currentCustomId = u.custom_role_id || 0;

  // Diff: PATCH only fields the server route handles directly.
  const patch = {};
  if (formValues.email !== (u.email || '')) patch.email = formValues.email || null;
  if (formValues.role && formValues.role !== u.role) patch.role = formValues.role;
  if (formValues.custom_role_id !== currentCustomId) patch.custom_role_id = formValues.custom_role_id;
  if (formValues.stone_enabled !== !!u.stone_enabled) patch.stone_enabled = formValues.stone_enabled;
  if (formValues.self_compile_enabled !== !!u.self_compile_enabled) patch.self_compile_enabled = formValues.self_compile_enabled;
  if (formValues.daily_conversion_limit !== (u.daily_conversion_limit ?? null))
    patch.daily_conversion_limit = formValues.daily_conversion_limit;
  if (formValues.rate_limit_per_minute !== (u.rate_limit_per_minute ?? null))
    patch.rate_limit_per_minute = formValues.rate_limit_per_minute;

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
  ]) {
    f[flag].checked = !!(cr && cr[flag]);
  }
  f.daily_conversion_limit.value = cr && cr.daily_conversion_limit != null ? cr.daily_conversion_limit : '';
  f.rate_limit_per_minute.value = cr && cr.rate_limit_per_minute != null ? cr.rate_limit_per_minute : '';
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
    can_view_users_tab: roleForm.can_view_users_tab.checked,
    can_create_user: roleForm.can_create_user.checked,
    can_suspend_user: roleForm.can_suspend_user.checked,
    can_ban_user: roleForm.can_ban_user.checked,
    can_approve_pending: roleForm.can_approve_pending.checked,
    can_grant_stone: roleForm.can_grant_stone.checked,
    can_grant_self_compile: roleForm.can_grant_self_compile.checked,
    can_reset_other_creds: roleForm.can_reset_other_creds.checked,
    can_restart_server: roleForm.can_restart_server.checked,
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
