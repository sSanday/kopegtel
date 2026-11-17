/* Data collection: host groups, profil per tipe, hosts, maintenance, discovery CIDR. */
(function () {
  "use strict";

  const { escapeHtml, formatDateTime, TYPE_LABELS, STATUS_META, timeAgo } = window.NMSFormat;

  const VIEWS = {
    "host-groups": { label: "Grup perangkat", desc: "Kelompok logis perangkat untuk filter dan pemeliharaan." },
    profiles: { label: "Profil pemantauan", desc: "Interval polling dan ambang peringatan per tipe perangkat." },
    hosts: { label: "Perangkat", desc: "Daftar perangkat yang dipantau." },
    maintenance: { label: "Pemeliharaan", desc: "Jendela waktu pemeliharaan yang menekan masalah dan notifikasi." },
    discovery: { label: "Penemuan", desc: "Pemindaian CIDR untuk menemukan perangkat baru." },
  };

  const views = document.querySelectorAll("[data-view]");
  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");
  const canEdit = () => {
    const role = window.__nmsMe && window.__nmsMe.role;
    return role === "admin" || role === "operator";
  };

  function currentView() {
    const raw = window.location.hash.replace(/^#/, "");
    return Object.prototype.hasOwnProperty.call(VIEWS, raw) ? raw : "host-groups";
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
    window.NMSFormat.setDocumentTitle(meta.label, "Pengumpulan data");
    if (current === "host-groups") loadGroups();
    else if (current === "profiles") loadProfiles();
    else if (current === "hosts") loadHosts();
    else if (current === "maintenance") loadMaintenance();
    else if (current === "discovery") { loadScanHistory(); loadGroupOptionsForScan(); }
  }

  window.addEventListener("hashchange", activateView);
  document.getElementById("refresh").addEventListener("click", activateView);

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  function editButtons(kind, id) {
    return canEdit()
      ? `<button class="ack-toggle" type="button" data-kind="${kind}" data-action="edit" data-id="${id}">Ubah</button> ` +
        `<button class="ack-toggle" type="button" data-kind="${kind}" data-action="delete" data-id="${id}">Hapus</button>`
      : "—";
  }

  /* =====================================================
     Host groups
     ===================================================== */

  const groupRows = document.getElementById("group-rows");
  const groupCount = document.getElementById("group-count");
  const groupForm = document.getElementById("group-form");
  const groupFormTitle = document.getElementById("group-form-title");
  const grpName = document.getElementById("grp-name");
  const grpDescription = document.getElementById("grp-description");
  const grpNote = document.getElementById("grp-note");
  const grpSave = document.getElementById("grp-save");

  let groupsCache = [];
  let editingGroupId = null;

  function renderGroups() {
    groupCount.textContent = `${groupsCache.length} grup terdaftar`;
    if (groupsCache.length === 0) {
      groupRows.innerHTML = '<tr class="empty-row"><td colspan="4">Belum ada grup. Buat grup untuk mengelompokkan perangkat.</td></tr>';
      return;
    }
    groupRows.innerHTML = groupsCache
      .map((group) => (
        `<tr>` +
        `<td><strong>${escapeHtml(group.name)}</strong></td>` +
        `<td>${escapeHtml(group.description || "—")}</td>` +
        `<td class="mono">${group.device_count}</td>` +
        `<td>${editButtons("group", group.id)}</td>` +
        `</tr>`
      ))
      .join("");
  }

  async function loadGroups() {
    try {
      const data = await window.apiFetch("/api/groups");
      groupsCache = data.groups || [];
    } catch (error) {
      groupRows.innerHTML = `<tr class="empty-row"><td colspan="4">Gagal memuat grup: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    renderGroups();
  }

  function openGroupForm(group) {
    editingGroupId = group ? group.id : null;
    groupFormTitle.textContent = group ? "Ubah grup" : "Tambah grup";
    grpName.value = group ? group.name : "";
    grpDescription.value = group ? group.description || "" : "";
    grpNote.hidden = true;
    groupForm.hidden = false;
    grpName.focus({ preventScroll: true });
  }

  document.getElementById("group-add").addEventListener("click", () => openGroupForm(null));
  document.getElementById("grp-cancel").addEventListener("click", () => {
    groupForm.hidden = true;
    editingGroupId = null;
  });

  groupForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (grpSave.disabled) return;
    const name = grpName.value.trim();
    if (!name) {
      grpNote.textContent = "Nama grup wajib diisi.";
      grpNote.hidden = false;
      grpName.focus();
      return;
    }
    grpSave.disabled = true;
    grpNote.hidden = true;
    try {
      const payload = { name, description: grpDescription.value.trim() };
      if (editingGroupId) {
        await window.apiFetch(`/api/groups/${editingGroupId}`, { method: "PUT", body: payload });
      } else {
        await window.apiFetch("/api/groups", { method: "POST", body: payload });
      }
      groupForm.hidden = true;
      editingGroupId = null;
      await loadGroups();
    } catch (error) {
      grpNote.textContent = error.message || "Gagal menyimpan grup.";
      grpNote.hidden = false;
    } finally {
      grpSave.disabled = false;
    }
  });

  /* =====================================================
     Profil pemantauan per tipe
     ===================================================== */

  const profileRows = document.getElementById("profile-rows");
  const profileNote = document.getElementById("profile-note");
  const PROFILE_FIELDS = [
    ["cpu_warn", "CPU peringatan"],
    ["cpu_crit", "CPU kritis"],
    ["mem_warn", "Memori peringatan"],
    ["mem_crit", "Memori kritis"],
    ["storage_warn", "Penyimpanan peringatan"],
    ["storage_crit", "Penyimpanan kritis"],
    ["temp_warn", "Suhu peringatan"],
    ["temp_crit", "Suhu kritis"],
  ];

  async function loadProfiles() {
    let data;
    try {
      data = await window.apiFetch("/api/profiles");
    } catch (error) {
      profileRows.innerHTML = `<tr class="empty-row"><td colspan="7">Gagal memuat profil: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const profiles = data.profiles || [];
    const types = data.types || [];
    const byType = new Map(profiles.map((p) => [p.device_type, p]));
    profileRows.innerHTML = types
      .map((type) => {
        const profile = byType.get(type) || {
          interval_seconds: 60,
          thresholds: { cpu_warn: 70, cpu_crit: 90, mem_warn: 70, mem_crit: 90, storage_warn: 70, storage_crit: 90, temp_warn: 60, temp_crit: 75 },
        };
        const t = profile.thresholds || {};
        const input = (key) => `<input type="number" min="1" max="${key.startsWith("temp") ? 150 : 100}" step="1" value="${Number(t[key] ?? 70)}" data-key="${key}" aria-label="${escapeHtml(TYPE_LABELS[type] || type)} ${key}" />`;
        return (
          `<tr data-type="${escapeHtml(type)}">` +
          `<td><strong>${escapeHtml(TYPE_LABELS[type] || type)}</strong></td>` +
          `<td><input type="number" min="10" max="86400" step="1" value="${Number(profile.interval_seconds || 60)}" data-key="interval_seconds" aria-label="Interval detik" /></td>` +
          `<td>${input("cpu_warn")} ${input("cpu_crit")}</td>` +
          `<td>${input("mem_warn")} ${input("mem_crit")}</td>` +
          `<td>${input("storage_warn")} ${input("storage_crit")}</td>` +
          `<td>${input("temp_warn")} ${input("temp_crit")}</td>` +
          `<td>${canEdit() ? `<button class="ack-toggle" type="button" data-save-profile="${escapeHtml(type)}">Simpan</button>` : "—"}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  profileRows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-save-profile]");
    if (!button) return;
    const type = button.dataset.saveProfile;
    const row = button.closest("tr");
    const payload = { interval_seconds: Number(row.querySelector("[data-key='interval_seconds']").value), thresholds: {} };
    row.querySelectorAll("input[data-key]").forEach((input) => {
      const key = input.dataset.key;
      if (key !== "interval_seconds") payload.thresholds[key] = Number(input.value);
    });
    button.disabled = true;
    button.textContent = "Menyimpan…";
    try {
      await window.apiFetch(`/api/profiles/${encodeURIComponent(type)}`, { method: "PUT", body: payload });
      profileNote.textContent = `Profil ${TYPE_LABELS[type] || type} tersimpan dan diterapkan ke perangkat terkait.`;
      profileNote.hidden = false;
      profileNote.classList.remove("is-error");
    } catch (error) {
      profileNote.textContent = error.message || "Gagal menyimpan profil.";
      profileNote.hidden = false;
      profileNote.classList.add("is-error");
    } finally {
      button.disabled = false;
      button.textContent = "Simpan";
    }
  });

  /* =====================================================
     Hosts
     ===================================================== */

  const dcHostRows = document.getElementById("dc-host-rows");
  const dcHostCount = document.getElementById("dc-host-count");

  async function loadHosts() {
    let data;
    try {
      data = await window.apiFetch("/api/devices?per_page=100");
    } catch (error) {
      dcHostRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat perangkat: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const devices = data.devices || [];
    dcHostCount.textContent = `${devices.length} perangkat`;
    if (devices.length === 0) {
      dcHostRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada perangkat. Tambahkan lewat tombol “Tambah perangkat”.</td></tr>';
      return;
    }
    dcHostRows.innerHTML = devices
      .map((device) => {
        const meta = STATUS_META[device.status] || STATUS_META.unknown;
        return (
          `<tr>` +
          `<td><a class="cell-link" href="inventory.html#host/${device.id}">${escapeHtml(device.name)}</a></td>` +
          `<td>${escapeHtml(TYPE_LABELS[device.type] || device.type)}</td>` +
          `<td class="mono">${escapeHtml(device.ip)}</td>` +
          `<td>${escapeHtml(device.group_name || "—")}</td>` +
          `<td><span class="status ${escapeHtml(meta.cls)}">${escapeHtml(meta.label)}</span></td>` +
          `<td>${editButtons("host", device.id)}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  document.getElementById("dc-host-add").addEventListener("click", () =>
    window.openDeviceForm({ onSaved: () => { window.NMSStore.invalidate(); loadHosts(); } })
  );

  /* =====================================================
     Maintenance
     ===================================================== */

  const maintenanceRows = document.getElementById("maintenance-rows");
  const maintenanceCount = document.getElementById("maintenance-count");
  const maintenanceForm = document.getElementById("maintenance-form");
  const maintenanceFormTitle = document.getElementById("maintenance-form-title");
  const mntName = document.getElementById("mnt-name");
  const mntScope = document.getElementById("mnt-scope");
  const mntDeviceWrap = document.getElementById("mnt-device-wrap");
  const mntGroupWrap = document.getElementById("mnt-group-wrap");
  const mntDevice = document.getElementById("mnt-device");
  const mntGroup = document.getElementById("mnt-group");
  const mntStart = document.getElementById("mnt-start");
  const mntEnd = document.getElementById("mnt-end");
  const mntNote = document.getElementById("mnt-note");
  const mntNoteError = document.getElementById("mnt-note-error");
  const mntSave = document.getElementById("mnt-save");

  /** datetime-local (WIB, UTC+7) → 'YYYY-MM-DDTHH:MM:SSZ'. */
  function wibToUtcIso(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || "");
    if (!match) return null;
    const [, y, mo, d, h, mi] = match.map(Number);
    return new Date(Date.UTC(y, mo - 1, d, h - 7, mi)).toISOString().slice(0, 19) + "Z";
  }

  function scopeLabelOf(windowItem) {
    if (windowItem.device_id) return `Perangkat: ${windowItem.device_name || windowItem.device_id}`;
    if (windowItem.group_id) return `Grup: ${windowItem.group_name || windowItem.group_id}`;
    return "Semua perangkat";
  }

  function renderMaintenance(windows) {
    maintenanceCount.textContent = `${windows.length} jadwal`;
    if (windows.length === 0) {
      maintenanceRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada jadwal pemeliharaan.</td></tr>';
      return;
    }
    maintenanceRows.innerHTML = windows
      .map((w) => (
        `<tr>` +
        `<td><strong>${escapeHtml(w.name)}</strong></td>` +
        `<td>${escapeHtml(scopeLabelOf(w))}</td>` +
        `<td class="mono">${escapeHtml(formatDateTime(w.starts_at))}</td>` +
        `<td class="mono">${escapeHtml(formatDateTime(w.ends_at))}</td>` +
        `<td>${escapeHtml(w.note || "—")}</td>` +
        `<td>${canEdit() ? `<button class="ack-toggle" type="button" data-kind="maintenance" data-action="delete" data-id="${w.id}">Hapus</button>` : "—"}</td>` +
        `</tr>`
      ))
      .join("");
  }

  async function loadMaintenance() {
    try {
      const data = await window.apiFetch("/api/maintenance");
      renderMaintenance(data.windows || []);
    } catch (error) {
      maintenanceRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat jadwal: ${escapeHtml(error.message)}</td></tr>`;
    }
    // Isi opsi perangkat/grup untuk formulir.
    try {
      const [devicesRes, groupsRes] = await Promise.all([
        window.apiFetch("/api/devices?per_page=100"),
        window.apiFetch("/api/groups"),
      ]);
      mntDevice.innerHTML = '<option value="">Pilih perangkat…</option>' + (devicesRes.devices || []).map((d) => `<option value="${d.id}">${escapeHtml(d.name)} (${escapeHtml(d.ip)})</option>`).join("");
      mntGroup.innerHTML = '<option value="">Pilih grup…</option>' + (groupsRes.groups || []).map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`).join("");
    } catch {
      /* opsi formulir opsional */
    }
  }

  mntScope.addEventListener("change", () => {
    mntDeviceWrap.hidden = mntScope.value !== "device";
    mntGroupWrap.hidden = mntScope.value !== "group";
  });

  document.getElementById("maint-add").addEventListener("click", () => {
    maintenanceFormTitle.textContent = "Tambah jadwal pemeliharaan";
    mntName.value = "";
    mntScope.value = "device";
    mntScope.dispatchEvent(new Event("change"));
    mntStart.value = "";
    mntEnd.value = "";
    mntNote.value = "";
    mntNoteError.hidden = true;
    maintenanceForm.hidden = false;
    mntName.focus({ preventScroll: true });
  });

  document.getElementById("mnt-cancel").addEventListener("click", () => {
    maintenanceForm.hidden = true;
  });

  maintenanceForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (mntSave.disabled) return;
    const name = mntName.value.trim();
    const startsAt = wibToUtcIso(mntStart.value);
    const endsAt = wibToUtcIso(mntEnd.value);
    if (!name || !startsAt || !endsAt) {
      mntNoteError.textContent = "Nama, waktu mulai, dan waktu selesai wajib diisi.";
      mntNoteError.hidden = false;
      return;
    }
    if (startsAt >= endsAt) {
      mntNoteError.textContent = "Waktu mulai harus sebelum waktu selesai.";
      mntNoteError.hidden = false;
      return;
    }
    mntSave.disabled = true;
    mntNoteError.hidden = true;
    try {
      const payload = {
        name,
        starts_at: startsAt,
        ends_at: endsAt,
        note: mntNote.value.trim(),
        device_id: mntScope.value === "device" ? Number(mntDevice.value) || null : null,
        group_id: mntScope.value === "group" ? Number(mntGroup.value) || null : null,
      };
      if (mntScope.value === "device" && !payload.device_id) {
        throw new Error("Pilih perangkat untuk skop ini.");
      }
      if (mntScope.value === "group" && !payload.group_id) {
        throw new Error("Pilih grup untuk skop ini.");
      }
      await window.apiFetch("/api/maintenance", { method: "POST", body: payload });
      maintenanceForm.hidden = true;
      await loadMaintenance();
    } catch (error) {
      mntNoteError.textContent = error.message || "Gagal menyimpan jadwal.";
      mntNoteError.hidden = false;
    } finally {
      mntSave.disabled = false;
    }
  });

  /* =====================================================
     Discovery
     ===================================================== */

  const scanForm = document.getElementById("scan-form");
  const scanCidr = document.getElementById("scan-cidr");
  const scanCommunity = document.getElementById("scan-community");
  const scanStart = document.getElementById("scan-start");
  const scanNote = document.getElementById("scan-note");
  const scanProgress = document.getElementById("scan-progress");
  const scanProgressText = document.getElementById("scan-progress-text");
  const scanProgressPct = document.getElementById("scan-progress-pct");
  const scanProgressBar = document.getElementById("scan-progress-bar");
  const scanResultsWrap = document.getElementById("scan-results-wrap");
  const scanResultRows = document.getElementById("scan-result-rows");
  const scanBulkPanel = document.getElementById("scan-bulk-panel");
  const scanBulkType = document.getElementById("scan-bulk-type");
  const scanBulkGroup = document.getElementById("scan-bulk-group");
  const scanAddSelected = document.getElementById("scan-add-selected");
  const scanBulkNote = document.getElementById("scan-bulk-note");
  const scanHistoryList = document.getElementById("scan-history-list");

  let activeScanTimer = null;

  function loadTypeOptionsForScan() {
    scanBulkType.innerHTML = Object.entries(TYPE_LABELS)
      .map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`)
      .join("");
  }

  async function loadGroupOptionsForScan() {
    try {
      const data = await window.apiFetch("/api/groups");
      scanBulkGroup.innerHTML = '<option value="">Tanpa grup</option>' + (data.groups || []).map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`).join("");
    } catch {
      /* grup opsional */
    }
    loadTypeOptionsForScan();
  }

  async function loadScanHistory() {
    try {
      const data = await window.apiFetch("/api/discovery/scans");
      const scans = data.scans || [];
      scanHistoryList.innerHTML = scans.length
        ? scans.map((scan) => `<li><span class="mono">${escapeHtml(scan.cidr)}</span> — ${scan.status === "running" ? "berjalan" : scan.status === "error" ? "gagal" : "selesai"} · ${scan.found} hidup dari ${scan.total} alamat · ${escapeHtml(formatDateTime(scan.created_at))}</li>`).join("")
        : "<li>Belum ada pemindaian.</li>";
    } catch {
      scanHistoryList.innerHTML = "<li>Gagal memuat riwayat pemindaian.</li>";
    }
  }

  function stopScanPolling() {
    if (activeScanTimer) {
      window.clearInterval(activeScanTimer);
      activeScanTimer = null;
    }
  }

  function renderScanResults(job) {
    const results = job.results || [];
    scanResultsWrap.hidden = results.length === 0;
    scanBulkPanel.hidden = results.length === 0;
    scanResultRows.innerHTML = results
      .map((entry) => (
        `<tr>` +
        `<td><input type="checkbox" class="scan-pick" value="${escapeHtml(entry.ip)}" aria-label="Pilih ${escapeHtml(entry.ip)}" /></td>` +
        `<td class="mono">${escapeHtml(entry.ip)}</td>` +
        `<td class="mono">${entry.latency_ms == null ? "—" : `${Number(entry.latency_ms).toFixed(0)} ms`}</td>` +
        `<td>${escapeHtml(entry.sysname || "—")}</td>` +
        `<td>${escapeHtml(entry.type_hint || "—")}</td>` +
        `</tr>`
      ))
      .join("");
  }

  function pollScanJob(jobId) {
    stopScanPolling();
    scanProgress.hidden = false;
    activeScanTimer = window.setInterval(async () => {
      try {
        const data = await window.apiFetch(`/api/discovery/scans/${jobId}`);
        const job = data.scan;
        const pct = job.total > 0 ? Math.round((job.completed / job.total) * 100) : 0;
        scanProgressBar.style.width = `${pct}%`;
        scanProgressPct.textContent = `${pct}%`;
        scanProgressText.textContent = job.status === "running"
          ? `Memindai ${job.completed}/${job.total} alamat — ${job.found} hidup`
          : job.status === "error"
            ? "Pemindaian gagal — periksa log server."
            : `Selesai: ${job.found} perangkat hidup dari ${job.total} alamat`;
        if (job.status !== "running") {
          stopScanPolling();
          renderScanResults(job);
          loadScanHistory();
        }
      } catch (error) {
        scanProgressText.textContent = error.message || "Gagal memantau pemindaian.";
        stopScanPolling();
      }
    }, 1000);
  }

  scanForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (scanStart.disabled) return;
    const cidr = scanCidr.value.trim();
    if (!cidr) {
      scanNote.textContent = "Rentang CIDR wajib diisi.";
      scanNote.hidden = false;
      scanCidr.focus();
      return;
    }
    scanStart.disabled = true;
    scanNote.hidden = true;
    scanBulkNote.hidden = true;
    scanResultsWrap.hidden = true;
    scanBulkPanel.hidden = true;
    try {
      const res = await window.apiFetch("/api/discovery/scan", {
        method: "POST",
        body: {
          cidr,
          community: scanCommunity.value.trim() || null,
        },
      });
      scanProgress.hidden = false;
      scanProgressBar.style.width = "0%";
      scanProgressPct.textContent = "0%";
      scanProgressText.textContent = "Memulai pemindaian…";
      pollScanJob(res.scan.id);
      // Bersihkan kredensial dari formulir setelah dikirim (tidak disimpan).
      scanCommunity.value = "";
    } catch (error) {
      scanNote.textContent = error.message || "Gagal memulai pemindaian.";
      scanNote.hidden = false;
    } finally {
      scanStart.disabled = false;
    }
  });

  scanAddSelected.addEventListener("click", async () => {
    const picked = Array.from(scanResultRows.querySelectorAll("input.scan-pick:checked")).map((input) => input.value);
    if (picked.length === 0) {
      scanBulkNote.textContent = "Pilih minimal satu perangkat.";
      scanBulkNote.hidden = false;
      return;
    }
    scanAddSelected.disabled = true;
    scanBulkNote.hidden = true;
    const names = {};
    scanResultRows.querySelectorAll("tr").forEach((row) => {
      const checkbox = row.querySelector("input.scan-pick");
      const ip = row.children[1] && row.children[1].textContent.trim();
      const sysname = row.children[3] && row.children[3].textContent.trim();
      if (checkbox && checkbox.checked && ip && sysname && sysname !== "—") {
        names[ip] = sysname;
      }
    });
    try {
      const res = await window.apiFetch("/api/discovery/bulk-add", {
        method: "POST",
        body: {
          ips: picked,
          type: scanBulkType.value,
          group_id: scanBulkGroup.value ? Number(scanBulkGroup.value) : null,
          names,
        },
      });
      const created = res.created || [];
      const skipped = res.skipped || [];
      scanBulkNote.textContent = `${created.length} perangkat ditambahkan${skipped.length ? `, ${skipped.length} dilewati (${skipped.map((s) => s.ip).join(", ")})` : ""}.`;
      scanBulkNote.hidden = false;
      window.NMSStore.invalidate();
      loadScanHistory();
    } catch (error) {
      scanBulkNote.textContent = error.message || "Gagal menambahkan perangkat.";
      scanBulkNote.hidden = false;
    } finally {
      scanAddSelected.disabled = false;
    }
  });

  /* =====================================================
     Delegasi klik: edit/hapus perangkat & grup
     ===================================================== */

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-kind]");
    if (!button) return;
    const kind = button.dataset.kind;
    const id = Number(button.dataset.id);

    if (button.dataset.action === "edit") {
      if (kind === "group") {
        const group = groupsCache.find((g) => g.id === id);
        if (group) openGroupForm(group);
        return;
      }
      if (kind === "host") {
        try {
          const res = await window.apiFetch(`/api/devices/${id}`);
          window.openDeviceForm({ device: res.device, onSaved: () => { window.NMSStore.invalidate(); loadHosts(); } });
        } catch (error) {
          await window.NMSUi.alert(error.message || "Gagal memuat perangkat.");
        }
        return;
      }
    }

    if (button.dataset.action === "delete") {
      const label = { group: "grup", host: "perangkat", maintenance: "jadwal pemeliharaan" }[kind] || "item";
      if (!(await window.NMSUi.confirm(`Hapus ${label} ini?`, {
        title: "Hapus item",
        confirmLabel: "Hapus",
        danger: true,
      }))) return;
      const paths = { group: `/api/groups/${id}`, host: `/api/devices/${id}`, maintenance: `/api/maintenance/${id}` };
      try {
        await window.apiFetch(paths[kind], { method: "DELETE" });
        if (kind === "group") await loadGroups();
        else if (kind === "host") await loadHosts();
        else if (kind === "maintenance") await loadMaintenance();
      } catch (error) {
        if (kind === "group" && error.status === 409) {
          await window.NMSUi.alert(error.message); // "Grup masih dipakai N perangkat"
        } else {
          await window.NMSUi.alert(error.message || "Gagal menghapus.");
        }
      }
    }
  });

  /* =====================================================
     Boot
     ===================================================== */

  whenReady(() => {
    if (!canEdit()) {
      ["group-add", "dc-host-add", "maint-add", "scan-start", "scan-add-selected"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.hidden = true;
      });
    }
    activateView();
  });
})();
