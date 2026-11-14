/* Administration: pengaturan umum, retensi data, informasi instalasi (admin). */
(function () {
  "use strict";

  const { escapeHtml, formatBytes, formatDateTime } = window.NMSFormat;

  const VIEWS = {
    general: { label: "Pengaturan umum", desc: "Organisasi, zona waktu, interval default, dan idle timeout." },
    housekeeping: { label: "Retensi data", desc: "Berapa lama data mentah dan ringkasan disimpan." },
    mikrotik: { label: "MikroTik", desc: "Aksi tulis global dan ambang deteksi masalah modul MikroTik." },
    ingest: { label: "Penerima kejadian", desc: "Syslog, SNMP trap, retensi dan batas laju kejadian." },
    tokens: { label: "Token API & metrik", desc: "Token baca-saja untuk /metrics dan pengaturan metrik Prometheus." },
    installation: { label: "Informasi instalasi", desc: "Versi, uptime server, basis data, dan status poller." },
  };

  const views = document.querySelectorAll("[data-view]");
  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");

  function currentView() {
    const raw = window.location.hash.replace(/^#/, "");
    return Object.prototype.hasOwnProperty.call(VIEWS, raw) ? raw : "general";
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
    document.title = `${meta.label} · Administration · NMS Kopegtel`;
    if (current === "general") loadGeneral();
    else if (current === "housekeeping") loadRetention();
    else if (current === "mikrotik") loadMtSettings();
    else if (current === "ingest") loadIngest();
    else if (current === "tokens") loadTokens();
    else if (current === "installation") loadInstallation();
  }

  window.addEventListener("hashchange", activateView);
  document.getElementById("refresh").addEventListener("click", activateView);

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  /* =====================================================
     Pengaturan umum
     ===================================================== */

  const setOrgName = document.getElementById("set-org-name");
  const setTimezone = document.getElementById("set-timezone");
  const setDefaultInterval = document.getElementById("set-default-interval");
  const setIdleTimeout = document.getElementById("set-idle-timeout");
  const settingsNote = document.getElementById("settings-note");
  const settingsForm = document.getElementById("settings-form");
  const settingsSave = document.getElementById("settings-save");

  async function loadGeneral() {
    try {
      const res = await window.apiFetch("/api/settings");
      const s = res.settings || {};
      setOrgName.value = s.org_name || "";
      setTimezone.value = s.timezone || "Asia/Jakarta";
      setDefaultInterval.value = String(s.default_poll_interval ?? 60);
      setIdleTimeout.value = String(s.idle_timeout_minutes ?? 30);
      let roles = [];
      try {
        roles = JSON.parse(s.mfa_required_roles || "[]");
      } catch (e) {
        roles = [];
      }
      document.querySelectorAll(".mfa-role").forEach((box) => {
        box.checked = Array.isArray(roles) && roles.includes(box.value);
      });
      document.getElementById("mfa-grace-days").value = String(s.mfa_grace_days ?? 7);
    } catch (error) {
      settingsNote.textContent = error.message || "Gagal memuat pengaturan.";
      settingsNote.hidden = false;
    }
  }

  settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (settingsSave.disabled) return;
    const orgName = setOrgName.value.trim();
    if (!orgName) {
      settingsNote.textContent = "Nama organisasi wajib diisi.";
      settingsNote.hidden = false;
      setOrgName.focus();
      return;
    }
    settingsSave.disabled = true;
    settingsNote.hidden = true;
    try {
      await window.apiFetch("/api/settings", {
        method: "PUT",
        body: {
          org_name: orgName,
          timezone: setTimezone.value,
          default_poll_interval: Number(setDefaultInterval.value),
          idle_timeout_minutes: Number(setIdleTimeout.value),
          mfa_required_roles: JSON.stringify(
            Array.from(document.querySelectorAll(".mfa-role:checked")).map((box) => box.value)
          ),
          mfa_grace_days: Number(document.getElementById("mfa-grace-days").value || 0),
        },
      });
      settingsNote.textContent = "Pengaturan tersimpan.";
      settingsNote.hidden = false;
      settingsNote.classList.remove("is-error");
      settingsNote.classList.add("is-ok");
    } catch (error) {
      settingsNote.textContent = error.message || "Gagal menyimpan pengaturan.";
      settingsNote.hidden = false;
      settingsNote.classList.remove("is-ok");
      settingsNote.classList.add("is-error");
    } finally {
      settingsSave.disabled = false;
    }
  });

  /* =====================================================
     Retensi data
     ===================================================== */

  const retRaw = document.getElementById("ret-raw");
  const retChecks = document.getElementById("ret-checks");
  const retHourly = document.getElementById("ret-hourly");
  const retEvents = document.getElementById("ret-events");
  const retAudit = document.getElementById("ret-audit");
  const retentionNote = document.getElementById("retention-note");
  const retentionForm = document.getElementById("retention-form");
  const retentionSave = document.getElementById("retention-save");

  async function loadRetention() {
    try {
      const res = await window.apiFetch("/api/settings");
      const s = res.settings || {};
      const preview = s.retention_preview || {};
      retRaw.value = String(s.retention_raw_days ?? 7);
      retChecks.value = String(s.retention_checks_days ?? 30);
      retHourly.value = String(s.retention_hourly_days ?? 30);
      retEvents.value = String(s.retention_events_days ?? 30);
      retAudit.value = String(s.retention_audit_days ?? 365);
      document.getElementById("ret-checks-preview").textContent = String(preview.checks || 0);
      document.getElementById("ret-raw-preview").textContent = String((preview.metrics || 0) + (preview.interfaces || 0));
      document.getElementById("ret-hourly-preview").textContent = String(preview.hourly || 0);
      document.getElementById("ret-events-preview").textContent = String(preview.events || 0);
      document.getElementById("ret-audit-preview").textContent = String(preview.audit || 0);
      document.getElementById("ret-optical-preview").textContent = String(preview.optical || 0);
      document.getElementById("ret-optical-hourly-preview").textContent = String(preview.optical_hourly || 0);
    } catch (error) {
      retentionNote.textContent = error.message || "Gagal memuat retensi.";
      retentionNote.hidden = false;
    }
  }

  retentionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (retentionSave.disabled) return;
    const values = {
      retention_raw_days: Number(retRaw.value),
      retention_checks_days: Number(retChecks.value),
      retention_hourly_days: Number(retHourly.value),
      retention_events_days: Number(retEvents.value),
      retention_audit_days: Number(retAudit.value),
    };
    for (const [key, value] of Object.entries(values)) {
      if (!Number.isFinite(value) || value < 1 || value > 3650) {
        retentionNote.textContent = "Nilai retensi harus antara 1 dan 3650 hari.";
        retentionNote.hidden = false;
        return;
      }
    }
    retentionSave.disabled = true;
    retentionNote.hidden = true;
    try {
      await window.apiFetch("/api/settings", { method: "PUT", body: values });
      await loadRetention();
      retentionNote.textContent = "Retensi tersimpan. Job harian akan memakai nilai baru.";
      retentionNote.hidden = false;
      retentionNote.classList.remove("is-error");
      retentionNote.classList.add("is-ok");
    } catch (error) {
      retentionNote.textContent = error.message || "Gagal menyimpan retensi.";
      retentionNote.hidden = false;
    } finally {
      retentionSave.disabled = false;
    }
  });

  /* =====================================================
     Modul MikroTik
     ===================================================== */

  const mtWriteGlobal = document.getElementById("mt-write-global");
  const mikrotikForm = document.getElementById("mikrotik-form");
  const mikrotikNote = document.getElementById("mikrotik-note");
  const mikrotikSave = document.getElementById("mikrotik-save");
  const MT_NUMBER_FIELDS = {
    mt_bruteforce_threshold: { el: document.getElementById("mt-bruteforce-threshold"), lo: 2, hi: 1000 },
    mt_bruteforce_window_minutes: { el: document.getElementById("mt-bruteforce-window"), lo: 1, hi: 1440 },
    mt_pool_high_pct: { el: document.getElementById("mt-pool-high"), lo: 50, hi: 100 },
    mt_sessions_drop_pct: { el: document.getElementById("mt-sessions-drop-pct"), lo: 5, hi: 100 },
    mt_sessions_drop_window_minutes: { el: document.getElementById("mt-sessions-drop-window"), lo: 1, hi: 120 },
    mt_time_drift_seconds: { el: document.getElementById("mt-time-drift"), lo: 10, hi: 86400 },
    mt_stale_user_days: { el: document.getElementById("mt-stale-user"), lo: 7, hi: 3650 },
    mt_log_retention_days: { el: document.getElementById("mt-log-retention"), lo: 1, hi: 3650 },
    mt_backup_retention_versions: { el: document.getElementById("mt-backup-retention"), lo: 1, hi: 365 },
  };

  async function loadMtSettings() {
    let s;
    try {
      const res = await window.apiFetch("/api/settings");
      s = res.settings || {};
    } catch (error) {
      mikrotikNote.textContent = error.message || "Gagal memuat pengaturan MikroTik.";
      mikrotikNote.hidden = false;
      return;
    }
    mtWriteGlobal.checked = String(s.mt_write_actions_enabled ?? "0") === "1";
    for (const [key, field] of Object.entries(MT_NUMBER_FIELDS)) {
      field.el.value = String(s[key] ?? "");
    }
  }

  mikrotikForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (mikrotikSave.disabled) return;
    mikrotikNote.hidden = true;
    const body = { mt_write_actions_enabled: mtWriteGlobal.checked };
    for (const [key, field] of Object.entries(MT_NUMBER_FIELDS)) {
      const value = Number(field.el.value);
      if (!Number.isFinite(value) || value < field.lo || value > field.hi) {
        mikrotikNote.textContent = `${key} harus antara ${field.lo} dan ${field.hi}.`;
        mikrotikNote.hidden = false;
        return;
      }
      body[key] = value;
    }
    mikrotikSave.disabled = true;
    try {
      await window.apiFetch("/api/settings", { method: "PUT", body });
      mikrotikNote.textContent = "Pengaturan MikroTik tersimpan.";
      mikrotikNote.hidden = false;
      mikrotikNote.classList.remove("is-error");
      mikrotikNote.classList.add("is-ok");
    } catch (error) {
      mikrotikNote.textContent = error.message || "Gagal menyimpan pengaturan MikroTik.";
      mikrotikNote.hidden = false;
      mikrotikNote.classList.remove("is-ok");
      mikrotikNote.classList.add("is-error");
    } finally {
      mikrotikSave.disabled = false;
    }
  });

  /* =====================================================
     Penerima kejadian (E1)
     ===================================================== */

  const RECEIVER_LABELS = { syslog_udp: "Syslog UDP", syslog_tcp: "Syslog TCP", snmp_trap: "SNMP trap UDP", netflow_udp: "NetFlow UDP", ipfix_udp: "IPFIX UDP" };
  const receiverRows = document.getElementById("ingest-receiver-rows");
  const receiverNote = document.getElementById("ingest-receiver-note");

  function receiverExtraCell(kind, receiver) {
    if (kind !== "snmp_trap") {
      return '<span class="panel__sub">—</span>';
    }
    const communities = Number(receiver.communities_count || 0);
    return `<div class="admin-field">
        <span class="panel__sub">Community tersimpan: <b>${communities}</b>${receiver.allow_v1 ? " · v1 diizinkan" : ""}</span>
        <input type="text" class="ingest-communities" placeholder="ganti daftar, pisahkan koma" autocomplete="off" />
        <label class="panel__sub"><input type="checkbox" class="ingest-clear" /> Hapus semua community (trap ditolak)</label>
        <label class="panel__sub"><input type="checkbox" class="ingest-allow-v1" /> Izinkan trap v1</label>
      </div>`;
  }

  async function loadIngest() {
    if (!receiverRows) return;
    let receivers = [];
    try {
      const data = await window.apiFetch("/api/events/receiver");
      receivers = data.receivers || [];
    } catch (error) {
      receiverNote.textContent = error.message;
      receiverNote.hidden = false;
      return;
    }
    receiverRows.innerHTML = receivers.map((receiver) => {
      const kind = receiver.kind;
      const checked = receiver.enabled ? " checked" : "";
      return `<tr data-kind="${escapeHtml(kind)}">
        <td>${escapeHtml(RECEIVER_LABELS[kind] || kind)}</td>
        <td><input type="checkbox" class="ingest-enabled" aria-label="Aktifkan ${escapeHtml(kind)}"${checked} /></td>
        <td><input type="number" class="ingest-port" min="1024" max="65535" step="1" inputmode="numeric" value="${Number(receiver.port)}" /></td>
        <td><input type="text" class="ingest-bind" maxlength="45" value="${escapeHtml(receiver.bind_address || "0.0.0.0")}" autocomplete="off" /></td>
        <td>${receiverExtraCell(kind, receiver)}</td>
        <td><button type="button" class="btn btn--ghost ingest-save">Simpan</button></td>
      </tr>`;
    }).join("");
    receiverRows.querySelectorAll(".ingest-save").forEach((button) => {
      button.addEventListener("click", () => saveReceiver(button.closest("tr")));
    });
    receiverRows.querySelectorAll(".ingest-allow-v1").forEach((box) => {
      const kind = box.closest("tr").dataset.kind;
      const receiver = receivers.find((item) => item.kind === kind);
      box.checked = !!(receiver && receiver.allow_v1);
    });
    try {
      const res = await window.apiFetch("/api/settings");
      const settings = res.settings || {};
      document.getElementById("ingest-ret-syslog").value = settings.retention_syslog_days || "";
      document.getElementById("ingest-ret-trap").value = settings.retention_trap_days || "";
      document.getElementById("ingest-ret-flow").value = settings.retention_flow_days || "";
      document.getElementById("ingest-ret-flow-hourly").value = settings.retention_flow_hourly_days || "";
      document.getElementById("ingest-topn").value = settings.flow_top_n || "";
      document.getElementById("ingest-rate").value = settings.ingest_rate_limit_per_sec || "";
      document.getElementById("ingest-window").value = settings.ingest_rate_window_seconds || "";
      document.getElementById("ingest-queue").value = settings.ingest_queue_size || "";
      document.getElementById("ingest-batch").value = settings.ingest_batch_size || "";
      document.getElementById("ingest-flush").value = settings.ingest_flush_seconds || "";
      document.getElementById("ingest-maxbytes").value = settings.ingest_max_message_bytes || "";
    } catch (error) {
      document.getElementById("ingest-settings-note").textContent = error.message;
      document.getElementById("ingest-settings-note").hidden = false;
    }
  }

  async function saveReceiver(row) {
    if (!row) return;
    const kind = row.dataset.kind;
    const payload = {
      kind,
      enabled: row.querySelector(".ingest-enabled").checked,
      port: Number(row.querySelector(".ingest-port").value),
      bind_address: row.querySelector(".ingest-bind").value.trim() || "0.0.0.0",
    };
    if (kind === "snmp_trap") {
      const raw = row.querySelector(".ingest-communities").value.trim();
      const clear = row.querySelector(".ingest-clear").checked;
      if (clear) payload.communities = [];
      else if (raw) payload.communities = raw.split(",").map((item) => item.trim()).filter(Boolean);
      payload.allow_v1 = row.querySelector(".ingest-allow-v1").checked;
    }
    receiverNote.hidden = true;
    try {
      const result = await window.apiFetch("/api/events/receiver", { method: "PUT", body: payload });
      receiverNote.textContent = result.warning ? `Tersimpan, tetapi: ${result.warning}` : `Penerima ${kind} tersimpan.`;
      receiverNote.hidden = false;
      receiverNote.classList.remove("is-error");
      receiverNote.classList.add("is-ok");
      await loadIngest();
    } catch (error) {
      receiverNote.textContent = error.message;
      receiverNote.hidden = false;
      receiverNote.classList.add("is-error");
      receiverNote.classList.remove("is-ok");
    }
  }

  const ingestSettingsForm = document.getElementById("ingest-settings-form");
  if (ingestSettingsForm) {
    ingestSettingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const settingsNote = document.getElementById("ingest-settings-note");
      const num = (id) => Number(document.getElementById(id).value);
      const body = {
        retention_syslog_days: num("ingest-ret-syslog"),
        retention_trap_days: num("ingest-ret-trap"),
        retention_flow_days: num("ingest-ret-flow"),
        retention_flow_hourly_days: num("ingest-ret-flow-hourly"),
        flow_top_n: num("ingest-topn"),
        ingest_rate_limit_per_sec: num("ingest-rate"),
        ingest_rate_window_seconds: num("ingest-window"),
        ingest_queue_size: num("ingest-queue"),
        ingest_batch_size: num("ingest-batch"),
        ingest_flush_seconds: num("ingest-flush"),
        ingest_max_message_bytes: num("ingest-maxbytes"),
      };
      settingsNote.hidden = true;
      try {
        await window.apiFetch("/api/settings", { method: "PUT", body });
        settingsNote.textContent = "Pengaturan kejadian tersimpan.";
        settingsNote.hidden = false;
        settingsNote.classList.remove("is-error");
        settingsNote.classList.add("is-ok");
      } catch (error) {
        settingsNote.textContent = error.message;
        settingsNote.hidden = false;
        settingsNote.classList.add("is-error");
        settingsNote.classList.remove("is-ok");
      }
    });
  }

  /* =====================================================
     Token API & metrik Prometheus (E4)
     ===================================================== */

  const $ = (id) => document.getElementById(id);
  const tokenRows = document.getElementById("token-rows");
  const tokenCount = document.getElementById("tokens-count");
  const tokenEmpty = document.getElementById("token-empty");
  const tokenForm = document.getElementById("token-form");
  const tokenNote = document.getElementById("token-note");
  const tokenAdd = document.getElementById("token-add");
  const tokenCancel = document.getElementById("token-cancel");
  const tokenReveal = document.getElementById("token-reveal");
  const tokenRevealValue = document.getElementById("token-reveal-value");
  const tokenRevealNote = document.getElementById("token-reveal-note");

  let tokensCache = [];

  function formatTokenRow(token) {
    const expires = token.expires_at ? escapeHtml(formatDateTime(token.expires_at)) : "Tidak pernah";
    const used = token.last_used_at ? escapeHtml(formatDateTime(token.last_used_at)) : "—";
    const ips = token.allowed_ips && token.allowed_ips.length ? escapeHtml(token.allowed_ips.join(", ")) : "Semua";
    const scopes = escapeHtml((token.scopes || []).join(", "));
    const status = token.revoked ? '<span class="status status--idle">Dicabut</span>' : '<span class="status status--ok">Aktif</span>';
    const action = token.revoked
      ? "-"
      : `<button class="ack-toggle" type="button" data-token-action="revoke" data-id="${token.id}">Cabut</button>`;
    return `<tr>
        <td><strong>${escapeHtml(token.name)}</strong></td>
        <td class="mono">${escapeHtml(token.token_prefix || "")}…</td>
        <td>${scopes}</td>
        <td>${expires}</td>
        <td>${ips}</td>
        <td>${used}</td>
        <td>${status} ${action}</td>
      </tr>`;
  }

  async function loadTokens() {
    if (!tokenRows) return;
    try {
      const data = await window.apiFetch("/api/metrics/tokens");
      tokensCache = data.tokens || [];
    } catch (error) {
      tokenRows.innerHTML = `<tr class="empty-row"><td colspan="7">${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    tokenCount.textContent = `${tokensCache.length} token`;
    tokenEmpty.hidden = tokensCache.length > 0;
    tokenRows.innerHTML = tokensCache.map(formatTokenRow).join("");
    tokenRows.querySelectorAll("[data-token-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!window.confirm("Cabut token ini? Scrape yang memakainya akan gagal.")) return;
        try {
          await window.apiFetch(`/api/metrics/tokens/${button.dataset.id}`, { method: "DELETE" });
          await loadTokens();
        } catch (error) {
          window.alert(error.message);
        }
      });
    });
    // Muat pengaturan metrik.
    try {
      const res = await window.apiFetch("/api/settings");
      const s = res.settings || {};
      document.getElementById("metrics-enabled").checked = ["1", "true", "yes", "on"].includes(String(s.metrics_enabled).toLowerCase());
      document.getElementById("metrics-interfaces").value = s.metrics_interfaces || "important";
      document.getElementById("metrics-series-limit").value = s.metrics_series_limit || "20000";
    } catch (error) {
      document.getElementById("metrics-settings-note").textContent = error.message;
      document.getElementById("metrics-settings-note").hidden = false;
    }
  }

  if (tokenAdd) {
    tokenAdd.addEventListener("click", () => {
      $("token-form").hidden = false;
      document.getElementById("token-name").value = "";
      document.getElementById("token-expires").value = "";
      document.getElementById("token-ips").value = "";
      tokenNote.hidden = true;
      document.getElementById("token-name").focus({ preventScroll: true });
    });
  }
  if (tokenCancel) tokenCancel.addEventListener("click", () => { tokenForm.hidden = true; });

  if (tokenForm) {
    tokenForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      tokenNote.hidden = true;
      const name = document.getElementById("token-name").value.trim();
      const expiresRaw = document.getElementById("token-expires").value.trim();
      const ips = document.getElementById("token-ips").value.split(",").map((s) => s.trim()).filter(Boolean);
      try {
        const body = { name, allowed_ips: ips };
        if (expiresRaw) body.expires_in_days = Number(expiresRaw);
        const data = await window.apiFetch("/api/metrics/tokens", { method: "POST", body });
        tokenForm.hidden = true;
        tokenRevealValue.value = data.token;
        tokenRevealNote.hidden = true;
        tokenReveal.hidden = false;
        await loadTokens();
      } catch (error) {
        tokenNote.textContent = error.message;
        tokenNote.hidden = false;
      }
    });
  }

  const tokenCopy = document.getElementById("token-copy");
  if (tokenCopy) {
    tokenCopy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(tokenRevealValue.value);
        tokenRevealNote.textContent = "Token tersalin ke clipboard.";
        tokenRevealNote.hidden = false;
      } catch {
        tokenRevealValue.select();
        tokenRevealNote.textContent = "Clipboard diblokir — token dipilih; salin manual (Ctrl+C).";
        tokenRevealNote.hidden = false;
      }
    });
  }
  const tokenRevealDone = document.getElementById("token-reveal-done");
  if (tokenRevealDone) {
    tokenRevealDone.addEventListener("click", () => {
      tokenReveal.hidden = true;
      tokenRevealValue.value = "";
    });
  }

  const metricsSettingsForm = document.getElementById("metrics-settings-form");
  if (metricsSettingsForm) {
    metricsSettingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const note = document.getElementById("metrics-settings-note");
      note.hidden = true;
      const payload = {
        metrics_enabled: document.getElementById("metrics-enabled").checked,
        metrics_interfaces: document.getElementById("metrics-interfaces").value,
        metrics_series_limit: Number(document.getElementById("metrics-series-limit").value || 20000),
      };
      try {
        await window.apiFetch("/api/settings", { method: "PUT", body: payload });
        note.textContent = "Pengaturan metrik tersimpan. Efektif pada scrape berikutnya (maks 10 detik).";
        note.classList.remove("is-error");
        note.classList.add("is-ok");
        note.hidden = false;
      } catch (error) {
        note.textContent = error.message;
        note.classList.add("is-error");
        note.classList.remove("is-ok");
        note.hidden = false;
      }
    });
  }

  /* =====================================================
     Informasi instalasi
     ===================================================== */

  const installationList = document.getElementById("installation-list");

  async function loadInstallation() {
    let info;
    try {
      info = await window.apiFetch("/api/reports/system-info");
    } catch (error) {
      installationList.innerHTML = `<div><dt>Gagal memuat</dt><dd>${escapeHtml(error.message)}</dd></div>`;
      return;
    }
    const items = [
      ["Versi aplikasi", info.app_version],
      ["Organisasi", info.org_name],
      ["Zona waktu", info.timezone],
      ["Python", info.python_version],
      ["Flask", info.flask_version],
      ["Uptime server", `${Math.floor((info.server_uptime_s || 0) / 86400)} hari ${Math.floor(((info.server_uptime_s || 0) % 86400) / 3600)} jam`],
      ["Basis data", `${info.db_path} (${formatBytes(info.db_size_bytes)})`],
      ["Poller", info.poller && info.poller.running ? `Berjalan (maks ${info.poller.max_workers} worker)` : "Tidak berjalan"],
      ["Waktu server (UTC)", info.time_utc],
    ];
    installationList.innerHTML = items
      .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value == null ? "—" : String(value))}</dd></div>`)
      .join("");
  }

  /* =====================================================
     Boot
     ===================================================== */

  whenReady(() => {
    const role = window.__nmsMe && window.__nmsMe.role;
    if (role !== "admin") {
      // Non-admin: halaman hanya baca (server juga menolak perubahan).
      ["settings-save", "retention-save", "mikrotik-save", "ingest-save"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) {
          el.hidden = true;
        }
      });
      document.querySelectorAll("#settings-form input, #settings-form select, #retention-form input, #mikrotik-form input, #ingest-settings-form input, #ingest-receiver-rows input, #ingest-receiver-rows button")
        .forEach((el) => { el.disabled = true; });
      ["settings-note", "retention-note", "mikrotik-note", "ingest-settings-note", "ingest-receiver-note"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) {
          el.textContent = "Hanya administrator yang dapat mengubah pengaturan ini.";
          el.hidden = false;
          el.classList.remove("is-error");
          el.classList.add("is-ok");
        }
      });
    }
    activateView();
  });
})();
