/* Users: kelola akun + matriks peran (read-only). */
(function () {
  "use strict";

  const { escapeHtml, formatDateTime } = window.NMSFormat;

  const VIEWS = {
    users: { label: "Pengguna", desc: "Akun pengguna dan perannya." },
    "user-roles": { label: "Peran pengguna", desc: "Matriks hak akses per peran." },
  };

  const views = document.querySelectorAll("[data-view]");
  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");

  function currentView() {
    const raw = window.location.hash.replace(/^#/, "");
    return Object.prototype.hasOwnProperty.call(VIEWS, raw) ? raw : "users";
  }

  let current = currentView();

  function activateView() {
    current = currentView();
    const meta = VIEWS[current];
    views.forEach((view) => {
      view.hidden = view.dataset.view !== current;
    });
    viewTitle.textContent = meta.label;
    viewDesc.textContent = meta.desc;
    window.NMSFormat.setDocumentTitle(meta.label, "Pengguna");
    if (current === "users") loadUsers();
    else if (current === "user-roles") renderMatrix();
  }

  window.addEventListener("hashchange", activateView);
  document.getElementById("refresh").addEventListener("click", activateView);

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  function isAdmin() {
    return window.__nmsMe && window.__nmsMe.role === "admin";
  }

  const ROLE_LABELS = { admin: "Administrator", operator: "Operator", viewer: "Pengamat" };

  const userRows = document.getElementById("user-rows");
  const userCount = document.getElementById("user-count");
  const userForm = document.getElementById("user-form");
  const userFormTitle = document.getElementById("user-form-title");
  const usrUsername = document.getElementById("usr-username");
  const usrRole = document.getElementById("usr-role");
  const usrPassword = document.getElementById("usr-password");
  const usrEmail = document.getElementById("usr-email");
  const usrTelegramChatId = document.getElementById("usr-telegram-chat-id");
  const usrUsernameWrap = document.getElementById("usr-username-wrap");
  const usrRoleWrap = document.getElementById("usr-role-wrap");
  const usrPasswordWrap = document.getElementById("usr-password-wrap");
  const usrNote = document.getElementById("usr-note");
  const usrSave = document.getElementById("usr-save");

  let editingUserId = null;

  let usersCache = [];

  function renderUsers() {
    userCount.textContent = `${usersCache.length} akun`;
    if (!usersCache.length) {
      userRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada akun pengguna. Tambahkan akun pertama lewat tombol Tambah pengguna.</td></tr>';
      return;
    }
    const me = window.__nmsMe || {};
    userRows.innerHTML = usersCache
      .map((user) => {
        const isSelf = me.username && user.username.toLowerCase() === me.username.toLowerCase();
        const needsChange = !!user.must_change_password;
        const roleCell = isAdmin() && !isSelf
          ? `<select class="role-select" data-role-id="${user.id}" aria-label="Peran ${escapeHtml(user.username)}">
               ${Object.entries(ROLE_LABELS).map(([value, label]) => `<option value="${value}" ${user.role === value ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}
             </select>
             <button class="ack-toggle" type="button" data-action="save-role" data-id="${user.id}">Simpan</button>`
          : `<span class="status ${user.role === "admin" ? "status--ok" : "status--idle"}">${escapeHtml(ROLE_LABELS[user.role] || user.role)}</span>${isSelf ? ' <span class="cell-sub">(Anda)</span>' : ""}`;
        const mfaBadge = user.mfa_enabled ? ' <span class="status status--ok" title="MFA aktif">MFA</span>' : "";
        const contactCell = isAdmin()
          ? `<span class="mono">${escapeHtml(user.email || "-")}</span>${user.telegram_chat_id ? `<br><span class="mono">tg: ${escapeHtml(user.telegram_chat_id)}</span>` : ""}${mfaBadge}`
          : "-";
        const actions = isAdmin()
          ? `<button class="ack-toggle" type="button" data-action="edit-contacts" data-id="${user.id}">Kontak</button>${user.mfa_enabled ? ` <button class="ack-toggle" type="button" data-action="reset-mfa" data-id="${user.id}">Atur ulang MFA</button>` : ""} <button class="ack-toggle" type="button" data-action="reset-password" data-id="${user.id}">Atur ulang kata sandi</button>${isSelf ? "" : ` <button class="ack-toggle" type="button" data-action="delete" data-id="${user.id}">Hapus</button>`}`
          : "-";
        return (
          `<tr>` +
          `<td><strong>${escapeHtml(user.username)}</strong></td>` +
          `<td>${roleCell}</td>` +
          `<td class="mono">${escapeHtml(formatDateTime(user.created_at))}</td>` +
          `<td>${needsChange ? '<span class="status status--warn">Wajib ganti kata sandi</span>' : '<span class="status status--ok">Aktif</span>'}</td>` +
          `<td>${contactCell}</td>` +
          `<td>${actions}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  async function loadUsers() {
    try {
      const data = await window.apiFetch("/api/users");
      usersCache = data.users || [];
    } catch (error) {
      userRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat pengguna: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    renderUsers();
  }

  function openUserForm(user) {
    editingUserId = user ? user.id : null;
    const editing = !!user;
    userFormTitle.textContent = editing ? `Kontak eskalasi: ${user.username}` : "Tambah pengguna";
    usrUsernameWrap.hidden = editing;
    usrRoleWrap.hidden = editing;
    usrPasswordWrap.hidden = editing;
    usrUsername.value = editing ? user.username : "";
    usrRole.value = editing ? user.role : "viewer";
    usrPassword.value = "";
    usrEmail.value = editing ? user.email || "" : "";
    usrTelegramChatId.value = editing ? user.telegram_chat_id || "" : "";
    usrNote.hidden = true;
    window.NMSUi.openSheet(userForm);
    (editing ? usrEmail : usrUsername).focus({ preventScroll: true });
  }

  document.getElementById("user-add")?.addEventListener("click", () => openUserForm(null));

  document.getElementById("usr-cancel")?.addEventListener("click", () => {
    window.NMSUi.closeSheet(userForm);
  });

  userForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (usrSave.disabled) return;
    usrSave.disabled = true;
    usrNote.hidden = true;
    try {
      if (editingUserId) {
        await window.apiFetch(`/api/users/${editingUserId}`, {
          method: "PUT",
          body: { email: usrEmail.value.trim(), telegram_chat_id: usrTelegramChatId.value.trim() },
        });
      } else {
        const username = usrUsername.value.trim().toLowerCase();
        const password = usrPassword.value;
        if (!username) {
          usrNote.textContent = "Nama pengguna wajib diisi.";
          usrNote.hidden = false;
          usrUsername.focus();
          return;
        }
        if (password.length < 10) {
          usrNote.textContent = "Kata sandi minimal 10 karakter.";
          usrNote.hidden = false;
          usrPassword.focus();
          return;
        }
        await window.apiFetch("/api/users", {
          method: "POST",
          body: {
            username,
            role: usrRole.value,
            password,
            email: usrEmail.value.trim(),
            telegram_chat_id: usrTelegramChatId.value.trim(),
          },
        });
      }
      window.NMSUi.closeSheet(userForm);
      editingUserId = null;
      await loadUsers();
    } catch (error) {
      usrNote.textContent = error.message || "Gagal menyimpan pengguna.";
      usrNote.hidden = false;
    } finally {
      usrSave.disabled = false;
    }
  });

  userRows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = Number(button.dataset.id);

    if (button.dataset.action === "save-role") {
      const select = userRows.querySelector(`select[data-role-id="${id}"]`);
      if (!select) return;
      button.disabled = true;
      try {
        await window.apiFetch(`/api/users/${id}`, { method: "PUT", body: { role: select.value } });
        await loadUsers();
      } catch (error) {
        window.NMSUi.toast(error.message || "Gagal mengubah peran.", { kind: "error" });
      } finally {
        button.disabled = false;
      }
      return;
    }

    if (button.dataset.action === "edit-contacts") {
      const user = usersCache.find((item) => item.id === id);
      if (user) openUserForm(user);
      return;
    }

    if (button.dataset.action === "reset-mfa") {
      const user = usersCache.find((item) => item.id === id);
      const password = await window.NMSUi.prompt(
        `MFA untuk "${user ? user.username : id}" akan dihapus. Masukkan kata sandi akun admin Anda untuk konfirmasi.`,
        {
          title: "Atur ulang MFA",
          label: "Kata sandi Anda *",
          type: "password",
          autocomplete: "current-password",
          confirmLabel: "Atur ulang MFA",
          danger: true,
        }
      );
      if (password == null) return;
      try {
        await window.apiFetch(`/api/mfa/reset/${id}`, { method: "POST", body: { password } });
        window.NMSUi.toast("MFA pengguna diatur ulang. Pengguna wajib mengaktifkan ulang MFA saat masuk.");
        await loadUsers();
      } catch (error) {
        window.NMSUi.toast(error.message || "Gagal mengatur ulang MFA.", { kind: "error" });
      }
      return;
    }

    if (button.dataset.action === "reset-password") {
      const user = usersCache.find((item) => item.id === id);
      const password = await window.NMSUi.prompt(
        `Kata sandi baru untuk "${user ? user.username : id}". Pengguna wajib menggantinya saat masuk berikutnya.`,
        {
          title: "Atur ulang kata sandi",
          label: "Kata sandi baru * (min. 10 karakter)",
          type: "password",
          autocomplete: "new-password",
          minLength: 10,
          confirmLabel: "Atur ulang kata sandi",
        }
      );
      if (password == null) return;
      try {
        await window.apiFetch(`/api/users/${id}`, { method: "PUT", body: { password } });
        window.NMSUi.toast("Kata sandi diatur ulang. Pengguna wajib menggantinya saat masuk berikutnya.");
        await loadUsers();
      } catch (error) {
        window.NMSUi.toast(error.message || "Gagal mengatur ulang kata sandi.", { kind: "error" });
      }
      return;
    }

    if (button.dataset.action === "delete") {
      const user = usersCache.find((item) => item.id === id);
      const confirmed = await window.NMSUi.confirm(
        `Hapus akun "${user ? user.username : id}"? Tindakan ini tidak bisa dibatalkan.`,
        { title: "Hapus pengguna", confirmLabel: "Hapus", danger: true }
      );
      if (!confirmed) return;
      try {
        await window.apiFetch(`/api/users/${id}`, { method: "DELETE" });
        window.NMSUi.toast("Pengguna dihapus.");
        await loadUsers();
      } catch (error) {
        window.NMSUi.toast(error.message || "Gagal menghapus pengguna.", { kind: "error" });
      }
    }
  });

  /* =====================================================
     Matriks peran (read-only)
     ===================================================== */

  const CAPABILITIES = [
    ["Melihat dasbor, pemantauan, inventaris, notifikasi, layanan, laporan", true, true, true],
    ["Menambah / mengubah / menghapus perangkat", true, true, false],
    ["Mengakui / membatalkan pengakuan masalah", true, true, false],
    ["Pemeliharaan (jendela & flag perangkat)", true, true, false],
    ["Pemindaian CIDR & penambahan massal", true, true, false],
    ["Uji kirim notifikasi & pengiriman uji kanal", true, true, false],
    ["Mengelola layanan & SLA", true, true, false],
    ["Log audit & unduh CSV audit", true, false, false],
    ["Kelola akun pengguna & peran", true, false, false],
    ["Pengaturan umum, retensi & informasi instalasi", true, false, false],
  ];

  function renderMatrix() {
    const tbody = document.getElementById("roles-matrix");
    tbody.innerHTML = CAPABILITIES.map(
      ([label, admin, operator, viewer]) =>
        `<tr><td>${escapeHtml(label)}</td>` +
        `<td>${admin ? "Ya" : "Tidak"}</td>` +
        `<td>${operator ? "Ya" : "Tidak"}</td>` +
        `<td>${viewer ? "Ya" : "Tidak"}</td></tr>`
    ).join("");
  }

  /* =====================================================
     Boot
     ===================================================== */

  whenReady(() => {
    if (!isAdmin()) {
      const add = document.getElementById("user-add");
      if (add) add.hidden = true;
    }
    activateView();
  });
})();
