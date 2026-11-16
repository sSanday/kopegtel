/* Formulir perangkat (<dialog> native) — bisa dibuka dari semua halaman.
 * Memuat grup & uplink dari API; uji koneksi nyata via /api/devices/test.
 * API: window.openDeviceForm({ device?, onSaved? })
 */
(function () {
  "use strict";

  const DEVICE_TYPES = [
    ["mikrotik", "MikroTik"],
    ["router", "Router"],
    ["switch", "Switch"],
    ["access-point", "Access Point"],
    ["server", "Server"],
    ["olt", "OLT"],
    ["ont", "ONT"],
    ["firewall", "Firewall"],
    ["lainnya", "Lainnya"],
  ];

  const IP_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

  let dialog = null;
  let state = { device: null, onSaved: null, submitting: false };

  // Satu sumber escape: window.NMSFormat (format.js). Fallback minimal
  // bila format.js gagal dimuat.
  const esc = window.NMSFormat
    ? window.NMSFormat.escapeHtml
    : (value) => String(value == null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function markup() {
    const typeOptions = DEVICE_TYPES.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
    return `
<dialog class="device-dialog" id="device-dialog" aria-labelledby="device-dialog-title">
  <form class="admin-form" id="device-form" novalidate>
    <div class="modal__head">
      <div>
        <h2 class="panel__title" id="device-dialog-title">Tambah perangkat</h2>
        <p class="panel__sub">Perangkat akan dipantau via ping dan (opsional) SNMP / MikroTik API.</p>
      </div>
      <button class="modal__close" type="button" data-close aria-label="Tutup dialog">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>
      </button>
    </div>

    <div class="admin-form__grid">
      <label class="admin-field">
        <span>Nama perangkat *</span>
        <input id="df-name" name="name" type="text" required autocomplete="off" placeholder="mis. core-sw-01" />
        <span class="field-error" id="df-name-error" hidden></span>
      </label>
      <label class="admin-field">
        <span>Alamat IP *</span>
        <input id="df-ip" name="ip" type="text" required autocomplete="off" inputmode="decimal" placeholder="mis. 192.168.30.2" />
        <span class="field-error" id="df-ip-error" hidden></span>
      </label>
      <label class="admin-field">
        <span>Tipe perangkat</span>
        <select id="df-type" name="type">${typeOptions}</select>
      </label>
      <label class="admin-field">
        <span>Grup</span>
        <select id="df-group" name="group_id"><option value="">Tanpa grup</option></select>
      </label>
      <label class="admin-field">
        <span>Lokasi</span>
        <input id="df-location" name="location" type="text" autocomplete="off" placeholder="mis. Gedung Pusat, Lantai 2" />
      </label>
      <label class="admin-field">
        <span>Uplink (parent)</span>
        <select id="df-parent" name="parent_id"><option value="">Tanpa uplink</option></select>
      </label>
      <label class="admin-field">
        <span>Interval polling (detik, min. 10)</span>
        <input id="df-interval" name="poll_interval" type="number" min="10" max="86400" step="1" value="60" required />
        <span class="field-error" id="df-interval-error" hidden></span>
      </label>
      <label class="admin-field">
        <span>Pemeliharaan</span>
        <span class="check check--inline"><input id="df-maintenance" type="checkbox" /> Perangkat sedang dalam pemeliharaan (problem &amp; notifikasi ditekan)</span>
      </label>
    </div>

    <label class="check"><input id="df-snmp-enabled" type="checkbox" /> Gunakan SNMP (sysName, uptime, CPU, memori, storage, trafik)</label>
    <div class="form-sub" id="df-snmp-fields" hidden>
      <div class="admin-form__grid">
        <label class="admin-field">
          <span>Versi SNMP</span>
          <select id="df-snmp-version" name="snmp_version">
            <option value="v2c" selected>SNMP v2c</option>
            <option value="v3">SNMP v3</option>
          </select>
        </label>
        <label class="admin-field">
          <span>Port SNMP</span>
          <input id="df-snmp-port" name="snmp_port" type="number" min="1" max="65535" step="1" value="161" />
        </label>
      </div>
      <div class="admin-form__grid" id="df-snmp-v2c-fields">
        <label class="admin-field admin-field--full">
          <span>Community *</span>
          <input id="df-snmp-community" name="snmp_community" type="password" autocomplete="off" placeholder="community SNMP" />
          <span class="field-error" id="df-snmp-community-error" hidden></span>
        </label>
      </div>
      <div class="admin-form__grid" id="df-snmp-v3-fields" hidden>
        <label class="admin-field">
          <span>Username v3 *</span>
          <input id="df-snmp-v3-user" name="snmp_v3_user" type="text" autocomplete="off" />
          <span class="field-error" id="df-snmp-v3-user-error" hidden></span>
        </label>
        <label class="admin-field">
          <span>Auth protocol</span>
          <select id="df-snmp-v3-auth-proto" name="snmp_v3_auth_proto">
            <option>MD5</option><option selected>SHA</option><option>SHA-224</option><option>SHA-256</option><option>SHA-384</option><option>SHA-512</option>
          </select>
        </label>
        <label class="admin-field">
          <span>Auth password</span>
          <input id="df-snmp-v3-auth-password" name="snmp_v3_auth_password" type="password" autocomplete="off" placeholder="kosongkan bila tidak ada" />
        </label>
        <label class="admin-field">
          <span>Priv protocol</span>
          <select id="df-snmp-v3-priv-proto" name="snmp_v3_priv_proto"><option selected>AES</option><option>DES</option></select>
        </label>
        <label class="admin-field">
          <span>Priv password</span>
          <input id="df-snmp-v3-priv-password" name="snmp_v3_priv_password" type="password" autocomplete="off" placeholder="kosongkan bila tidak ada" />
        </label>
      </div>
    </div>

    <label class="check"><input id="df-mikrotik-enabled" type="checkbox" /> Gunakan MikroTik RouterOS API (read-only)</label>
    <div class="form-sub" id="df-mikrotik-fields" hidden>
      <div class="admin-form__grid">
        <label class="admin-field">
          <span>Port API</span>
          <input id="df-mikrotik-port" name="mikrotik_port" type="number" min="1" max="65535" step="1" value="8728" />
        </label>
        <label class="admin-field">
          <span>User RouterOS *</span>
          <input id="df-mikrotik-user" name="mikrotik_user" type="text" autocomplete="off" />
          <span class="field-error" id="df-mikrotik-user-error" hidden></span>
        </label>
        <label class="admin-field">
          <span>Password *</span>
          <input id="df-mikrotik-password" name="mikrotik_password" type="password" autocomplete="off" placeholder="password RouterOS" />
          <span class="field-error" id="df-mikrotik-password-error" hidden></span>
        </label>
        <label class="admin-field">
          <span>Mode</span>
          <span class="check check--inline"><input id="df-mikrotik-ssl" type="checkbox" /> Pakai SSL (port 8729)</span>
        </label>
        <label class="admin-field admin-field--full" id="df-mikrotik-fingerprint-field" hidden>
          <span>Fingerprint SHA-256 sertifikat</span>
          <input id="df-mikrotik-ssl-fingerprint" name="mikrotik_ssl_fingerprint" type="text" autocomplete="off" spellcheck="false" placeholder="contoh: AA:BB:CC:… (64 hex)" />
          <span class="field-error" id="df-mikrotik-ssl-fingerprint-error" hidden></span>
          <span class="admin-note">Untuk sertifikat self-signed: isi fingerprint SHA-256 sertifikat perangkat (64 hex, boleh dipisah titik dua). Kosongkan untuk menghapus pinning; tanpa pinning hanya sertifikat tepercaya CA yang diterima.</span>
        </label>
      </div>
    </div>

    <p class="admin-note" id="df-secret-hint" hidden>Kosongkan kolom kata sandi untuk mempertahankan kredensial yang tersimpan.</p>

    <div class="test-results" id="df-test-results" hidden>
      <p class="test-results__title">Hasil uji koneksi</p>
      <ul class="test-results__list" id="df-test-results-list"></ul>
    </div>

    <p class="form-note" id="df-note" role="alert" hidden></p>

    <div class="admin-actions">
      <button class="btn btn--primary" id="df-submit" type="submit">Simpan perangkat</button>
      <button class="btn btn--ghost" id="df-test" type="button">Uji koneksi</button>
      <button class="btn btn--ghost" type="button" data-close>Batal</button>
    </div>
  </form>
</dialog>`;
  }

  function ensureDialog() {
    if (dialog) return dialog;
    const wrapper = document.createElement("div");
    wrapper.innerHTML = markup();
    dialog = wrapper.firstElementChild;
    document.body.appendChild(dialog);

    // Tutup via tombol, klik backdrop (dialog::backdrop), Esc otomatis oleh <dialog>.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
      if (event.target.closest("[data-close]")) dialog.close();
    });

    document.getElementById("df-snmp-enabled").addEventListener("change", syncToggles);
    document.getElementById("df-mikrotik-enabled").addEventListener("change", syncToggles);
    document.getElementById("df-mikrotik-ssl").addEventListener("change", syncToggles);
    document.getElementById("df-snmp-version").addEventListener("change", syncToggles);
    document.getElementById("df-test").addEventListener("click", runTest);
    document.getElementById("device-form").addEventListener("submit", submitForm);
    return dialog;
  }

  function syncToggles() {
    const snmpOn = document.getElementById("df-snmp-enabled").checked;
    const mikrotikOn = document.getElementById("df-mikrotik-enabled").checked;
    document.getElementById("df-snmp-fields").hidden = !snmpOn;
    document.getElementById("df-mikrotik-fields").hidden = !mikrotikOn;
    const sslOn = document.getElementById("df-mikrotik-ssl").checked;
    document.getElementById("df-mikrotik-fingerprint-field").hidden = !(mikrotikOn && sslOn);
    const v3 = document.getElementById("df-snmp-version").value === "v3";
    document.getElementById("df-snmp-v2c-fields").hidden = v3;
    document.getElementById("df-snmp-v3-fields").hidden = !v3;
  }

  function setFieldError(inputId, errorId, message) {
    const input = document.getElementById(inputId);
    const error = document.getElementById(errorId);
    if (!input || !error) return;
    if (message) {
      input.setAttribute("aria-invalid", "true");
      error.textContent = message;
      error.hidden = false;
    } else {
      input.removeAttribute("aria-invalid");
      error.textContent = "";
      error.hidden = true;
    }
  }

  function validateForm() {
    const name = document.getElementById("df-name").value.trim();
    const ip = document.getElementById("df-ip").value.trim();
    const interval = Number(document.getElementById("df-interval").value);

    setFieldError("df-name", "df-name-error", name ? "" : "Nama perangkat wajib diisi.");
    setFieldError("df-interval", "df-interval-error",
      Number.isFinite(interval) && interval >= 10 && interval <= 86400 ? "" : "Interval harus 10–86400 detik.");

    let ipError = "";
    const match = IP_RE.exec(ip);
    if (!match) {
      ipError = "Alamat IP tidak valid (contoh: 192.168.30.2).";
    } else if (match.slice(1).some((part) => Number(part) > 255)) {
      ipError = "Alamat IP tidak valid (oktet > 255).";
    }
    setFieldError("df-ip", "df-ip-error", ipError);

    if (document.getElementById("df-snmp-enabled").checked) {
      const v3 = document.getElementById("df-snmp-version").value === "v3";
      if (v3) {
        const user = document.getElementById("df-snmp-v3-user").value.trim();
        setFieldError("df-snmp-v3-user", "df-snmp-v3-user-error", user ? "" : "Username SNMP v3 wajib diisi.");
      } else {
        const community = document.getElementById("df-snmp-community").value.trim();
        const hasExisting = state.device && state.device.has_snmp_credential;
        setFieldError("df-snmp-community", "df-snmp-community-error", community || hasExisting ? "" : "Community wajib diisi.");
      }
    }
    if (document.getElementById("df-mikrotik-enabled").checked) {
      const user = document.getElementById("df-mikrotik-user").value.trim();
      const hasExisting = state.device && state.device.has_mikrotik_credential;
      setFieldError("df-mikrotik-user", "df-mikrotik-user-error", user ? "" : "User RouterOS wajib diisi.");
      const password = document.getElementById("df-mikrotik-password").value;
      setFieldError("df-mikrotik-password", "df-mikrotik-password-error", password || hasExisting ? "" : "Password wajib diisi.");
      const fingerprint = document.getElementById("df-mikrotik-ssl-fingerprint").value.trim();
      let fingerprintError = "";
      if (fingerprint) {
        const compact = fingerprint.replace(/^\s*sha-?256\s*:?\s*/i, "").replace(/[\s:-]/g, "").toLowerCase();
        if (!/^[0-9a-f]{64}$/.test(compact)) {
          fingerprintError = "Fingerprint harus 64 digit heksadesimal SHA-256 (boleh dipisah titik dua).";
        }
      }
      setFieldError("df-mikrotik-ssl-fingerprint", "df-mikrotik-ssl-fingerprint-error", fingerprintError);
    }

    return document.querySelectorAll("#device-dialog [aria-invalid='true']").length === 0;
  }

  function readPayload() {
    const value = (id) => document.getElementById(id).value.trim();
    return {
      name: value("df-name"),
      ip: value("df-ip"),
      type: value("df-type"),
      group_id: value("df-group") || null,
      location: value("df-location"),
      parent_id: value("df-parent") || null,
      maintenance: document.getElementById("df-maintenance").checked,
      poll_interval: Number(value("df-interval")),
      snmp_enabled: document.getElementById("df-snmp-enabled").checked,
      snmp_version: value("df-snmp-version"),
      snmp_port: Number(value("df-snmp-port") || 161),
      snmp_community: document.getElementById("df-snmp-community").value,
      snmp_v3_user: value("df-snmp-v3-user"),
      snmp_v3_auth_proto: value("df-snmp-v3-auth-proto"),
      snmp_v3_auth_password: document.getElementById("df-snmp-v3-auth-password").value,
      snmp_v3_priv_proto: value("df-snmp-v3-priv-proto"),
      snmp_v3_priv_password: document.getElementById("df-snmp-v3-priv-password").value,
      mikrotik_enabled: document.getElementById("df-mikrotik-enabled").checked,
      mikrotik_port: Number(value("df-mikrotik-port") || 8728),
      mikrotik_user: value("df-mikrotik-user"),
      mikrotik_password: document.getElementById("df-mikrotik-password").value,
      mikrotik_ssl: document.getElementById("df-mikrotik-ssl").checked,
      mikrotik_ssl_fingerprint: value("df-mikrotik-ssl-fingerprint"),
    };
  }

  async function fillSelects(excludeId) {
    const groupSelect = document.getElementById("df-group");
    const parentSelect = document.getElementById("df-parent");
    groupSelect.innerHTML = '<option value="">Tanpa grup</option>';
    parentSelect.innerHTML = '<option value="">Tanpa uplink</option>';
    try {
      const [groupsRes, devicesRes] = await Promise.all([
        window.apiFetch("/api/groups"),
        window.apiFetch("/api/devices?per_page=100"),
      ]);
      for (const group of groupsRes.groups || []) {
        const option = document.createElement("option");
        option.value = String(group.id);
        option.textContent = group.name;
        groupSelect.appendChild(option);
      }
      for (const device of devicesRes.devices || []) {
        if (excludeId && device.id === excludeId) continue;
        const option = document.createElement("option");
        option.value = String(device.id);
        option.textContent = `${device.name} (${device.ip})`;
        parentSelect.appendChild(option);
      }
    } catch {
      /* daftar opsional — biarkan kosong */
    }
  }

  async function runTest() {
    if (!validateForm()) {
      showNote("Periksa kembali field yang ditandai.", true);
      return;
    }
    const button = document.getElementById("df-test");
    button.disabled = true;
    button.classList.add("is-busy");
    try {
      const payload = readPayload();
      if (state.device) payload.id = state.device.id;
      const result = await window.apiFetch("/api/devices/test", { method: "POST", body: payload });
      const list = document.getElementById("df-test-results-list");
      list.innerHTML = "";
      const label = { ping: "Ping", snmp: "SNMP", mikrotik: "MikroTik API" };
      for (const [method, res] of Object.entries(result)) {
        if (!res) continue;
        const ok = !!res.ok;
        const detail = res.detail || res.reason || (ok ? "OK" : "gagal");
        const li = document.createElement("li");
        li.className = `test-results__item test-results__item--${ok ? "ok" : "down"}`;
        li.textContent = `${label[method] || method}: ${ok ? "Berhasil" : `Gagal — ${detail}`}`;
        list.appendChild(li);
      }
      document.getElementById("df-test-results").hidden = false;
    } catch (error) {
      showNote(error.message || "Uji koneksi gagal.", true);
    } finally {
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  function showNote(message, isError) {
    const note = document.getElementById("df-note");
    note.textContent = message || "";
    note.hidden = !message;
    note.classList.toggle("is-error", !!isError);
  }

  async function submitForm(event) {
    event.preventDefault();
    if (state.submitting) return;
    if (!validateForm()) {
      showNote("Periksa kembali field yang ditandai.", true);
      return;
    }
    state.submitting = true;
    const button = document.getElementById("df-submit");
    button.disabled = true;
    button.classList.add("is-busy");
    showNote("", false);
    try {
      const payload = readPayload();
      if (state.device) {
        await window.apiFetch(`/api/devices/${state.device.id}`, { method: "PUT", body: payload });
      } else {
        await window.apiFetch("/api/devices", { method: "POST", body: payload });
      }
      dialog.close();
      window.dispatchEvent(new CustomEvent("nms:devices-changed"));
      if (typeof state.onSaved === "function") state.onSaved();
    } catch (error) {
      showNote(error.message || "Gagal menyimpan perangkat.", true);
    } finally {
      state.submitting = false;
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  function resetForm() {
    document.getElementById("device-form").reset();
    document.getElementById("df-note").hidden = true;
    document.getElementById("df-test-results").hidden = true;
    dialog.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
    dialog.querySelectorAll(".field-error").forEach((el) => {
      el.hidden = true;
      el.textContent = "";
    });
  }

  function fillForm(device) {
    const set = (id, value) => {
      const el = document.getElementById(id);
      if (el && value !== undefined && value !== null) el.value = value;
    };
    set("df-name", device.name);
    set("df-ip", device.ip);
    set("df-type", device.type || "lainnya");
    set("df-location", device.location || "");
    set("df-interval", device.poll_interval || 60);
    set("df-snmp-version", device.snmp_version || "v2c");
    set("df-snmp-port", device.snmp_port || 161);
    set("df-snmp-v3-user", device.snmp_v3_user || "");
    set("df-snmp-v3-auth-proto", device.snmp_v3_auth_proto || "SHA");
    set("df-snmp-v3-priv-proto", device.snmp_v3_priv_proto || "AES");
    set("df-mikrotik-port", device.mikrotik_port || 8728);
    set("df-mikrotik-user", device.mikrotik_user || "");
    document.getElementById("df-snmp-enabled").checked = !!device.snmp_enabled;
    document.getElementById("df-mikrotik-enabled").checked = !!device.mikrotik_enabled;
    document.getElementById("df-mikrotik-ssl").checked = !!device.mikrotik_ssl;
    set("df-mikrotik-ssl-fingerprint", device.mikrotik_ssl_fingerprint || "");
    document.getElementById("df-maintenance").checked = !!device.maintenance;
  }

  async function open(options) {
    const opts = options || {};
    const device = opts.device || null;
    state = { device, onSaved: opts.onSaved || null, submitting: false };

    ensureDialog();
    resetForm();
    document.getElementById("device-dialog-title").textContent = device ? "Ubah perangkat" : "Tambah perangkat";
    document.getElementById("df-secret-hint").hidden = !device;

    await fillSelects(device ? device.id : null);
    if (device) {
      fillForm(device);
      // Pilih grup & uplink setelah opsi dimuat.
      if (device.group_id) document.getElementById("df-group").value = String(device.group_id);
      if (device.parent_id) document.getElementById("df-parent").value = String(device.parent_id);
    } else {
      document.getElementById("df-interval").value = String(window.__nmsDefaultInterval || 60);
    }
    syncToggles();

    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    const nameInput = document.getElementById("df-name");
    if (nameInput) nameInput.focus({ preventScroll: true });
  }

  window.openDeviceForm = open;
})();
