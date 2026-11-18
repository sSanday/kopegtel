/* Services: CRUD layanan + ketersediaan & laporan SLA (semua dari API). */
(function () {
  "use strict";

  const { escapeHtml, formatPercent, availabilityClass } = window.NMSFormat;

  const VIEWS = {
    services: { label: "Layanan", desc: "Layanan bisnis dan perangkat penopangnya." },
    sla: { label: "SLA", desc: "Ketersediaan tiap layanan terhadap target SLA." },
    "sla-report": { label: "Laporan SLA", desc: "Laporan SLA per periode beserta unduhan CSV." },
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
    return Object.prototype.hasOwnProperty.call(VIEWS, raw) ? raw : "services";
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
    window.NMSFormat.setDocumentTitle(meta.label, "Layanan");
    if (current === "services") loadServices();
    else if (current === "sla") loadSla();
    else if (current === "sla-report") loadSlaReport();
  }

  window.addEventListener("hashchange", activateView);
  document.getElementById("refresh").addEventListener("click", activateView);

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  function debounce(fn, ms) {
    let timer = null;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), ms);
    };
  }

  /* =====================================================
     Services — daftar & CRUD
     ===================================================== */

  const serviceRows = document.getElementById("service-rows");
  const serviceCount = document.getElementById("service-count");
  const serviceSearch = document.getElementById("service-search");
  const serviceAdd = document.getElementById("service-add");
  const serviceFormPanel = document.getElementById("service-form-panel");
  const serviceFormTitle = document.getElementById("service-form-title");
  const svcName = document.getElementById("svc-name");
  const svcDivision = document.getElementById("svc-division");
  const svcSla = document.getElementById("svc-sla");
  const svcMode = document.getElementById("svc-mode");
  const svcDescription = document.getElementById("svc-description");
  const svcDevices = document.getElementById("svc-devices");
  const svcDevicesEmpty = document.getElementById("svc-devices-empty");
  const svcNote = document.getElementById("svc-form-note");
  const svcSave = document.getElementById("svc-save");
  const svcCancel = document.getElementById("svc-cancel");

  let servicesCache = [];
  let devicesCache = [];
  let editingId = null;
  let slaDays = 1;
  let slaReportDays = 7;

  async function loadDevicesCache() {
    if (devicesCache.length) return devicesCache;
    const data = await window.apiFetch("/api/devices?per_page=100");
    devicesCache = data.devices || [];
    return devicesCache;
  }

  function serviceStatusMeta(service) {
    if (service.device_count === 0) return { cls: "status status--idle", label: "Tanpa perangkat" };
    return null; // status terperinci dihitung di tampilan SLA
  }

  function renderServices() {
    const query = serviceSearch.value.trim().toLowerCase();
    const visible = servicesCache.filter((service) => {
      if (!query) return true;
      return `${service.name} ${service.division}`.toLowerCase().includes(query);
    });

    serviceCount.textContent = `${servicesCache.length} layanan terdaftar`;
    document.getElementById("svc-total").textContent = String(servicesCache.length);

    if (visible.length === 0) {
      serviceRows.innerHTML = servicesCache.length === 0
        ? '<tr class="empty-row"><td colspan="7">Belum ada layanan. Tambahkan layanan pertama Anda.</td></tr>'
        : '<tr class="empty-row"><td colspan="7">Tidak ada layanan yang cocok dengan pencarian.</td></tr>';
      return;
    }

    const editable = canEdit();
    serviceRows.innerHTML = visible
      .map((service) => {
        const devices = service.devices || [];
        const anyDown = devices.some((d) => d.status === "offline");
        const allUp = devices.length > 0 && devices.every((d) => d.status === "online" || d.status === "degraded");
        const modeLabel = service.mode === "all" ? "Semua up" : "Minimal satu up";
        let statusCls = "status status--idle";
        let statusLabel = "Belum ada data";
        if (devices.length > 0) {
          const up = service.mode === "all" ? allUp : devices.some((d) => d.status === "online" || d.status === "degraded");
          statusCls = up ? "status status--ok" : "status status--down";
          statusLabel = up ? "Operasional" : "Gangguan";
        }
        const deviceList = devices.length
          ? devices.map((d) => d.name).join(", ")
          : "—";
        return (
          `<tr>` +
          `<td><strong>${escapeHtml(service.name)}</strong></td>` +
          `<td>${escapeHtml(service.division || "—")}</td>` +
          `<td class="mono">${escapeHtml(formatPercent(service.sla_target, 2))}</td>` +
          `<td>${escapeHtml(modeLabel)}</td>` +
          `<td title="${escapeHtml(deviceList)}">${devices.length} perangkat</td>` +
          `<td><span class="${statusCls}">${escapeHtml(statusLabel)}</span></td>` +
          `<td>${editable
            ? `<button class="ack-toggle" type="button" data-action="edit" data-id="${service.id}">Ubah</button> ` +
              `<button class="ack-toggle" type="button" data-action="delete" data-id="${service.id}">Hapus</button>`
            : "—"}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  async function loadServices() {
    try {
      const data = await window.apiFetch("/api/services");
      servicesCache = data.services || [];
    } catch (error) {
      serviceRows.innerHTML = `<tr class="empty-row"><td colspan="7">Gagal memuat layanan: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    renderServices();
    updateSummaryStats();
  }

  async function updateSummaryStats() {
    // Statistik tercapai/tidak berbasis target vs 24 jam terakhir.
    try {
      const report = await window.apiFetch("/api/services/sla-report?period=1");
      const rows = report.rows || [];
      const met = rows.filter((r) => r.met === true).length;
      const missed = rows.filter((r) => r.met === false).length;
      const nodata = rows.filter((r) => r.met === null).length;
      document.getElementById("svc-met").textContent = String(met);
      document.getElementById("svc-missed").textContent = String(missed);
      document.getElementById("svc-nodata").textContent = String(nodata);
    } catch {
      document.getElementById("svc-met").textContent = "—";
    }
  }

  function openForm(service) {
    editingId = service ? service.id : null;
    serviceFormTitle.textContent = service ? "Ubah layanan" : "Tambah layanan";
    svcName.value = service ? service.name : "";
    svcDivision.value = service ? service.division || "" : "";
    svcSla.value = service ? String(service.sla_target) : "99.9";
    svcMode.value = service ? service.mode : "all";
    svcDescription.value = service ? service.description || "" : "";
    svcNote.hidden = true;
    window.NMSUi.openSheet(serviceFormPanel);
    renderDevicePicker(service ? service.devices || [] : []);
    svcName.focus({ preventScroll: true });
  }

  function closeForm() {
    window.NMSUi.closeSheet(serviceFormPanel);
    editingId = null;
  }

  async function renderDevicePicker(selected) {
    await loadDevicesCache();
    const selectedIds = new Set((selected || []).map((d) => String(d.id)));
    svcDevicesEmpty.hidden = devicesCache.length !== 0;
    svcDevices.innerHTML = devicesCache
      .map((device) => {
        const checked = selectedIds.has(String(device.id)) ? " checked" : "";
        return `<label class="picker__item"><input type="checkbox" value="${device.id}"${checked} /> ${escapeHtml(device.name)} <span class="cell-sub mono">${escapeHtml(device.ip)}</span></label>`;
      })
      .join("");
  }

  function selectedDeviceIds() {
    return Array.from(svcDevices.querySelectorAll("input[type='checkbox']:checked")).map((input) => Number(input.value));
  }

  serviceSearch.addEventListener("input", debounce(renderServices, 200));
  serviceAdd.addEventListener("click", () => openForm(null));
  svcCancel.addEventListener("click", closeForm);

  serviceRows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = Number(button.dataset.id);
    const service = servicesCache.find((item) => item.id === id);
    if (!service) return;

    if (button.dataset.action === "edit") {
      openForm(service);
      return;
    }
    if (button.dataset.action === "delete") {
      if (!(await window.NMSUi.confirm(`Hapus layanan "${service.name}"? Perangkat penopangnya tidak ikut terhapus.`, {
        title: "Hapus layanan",
        confirmLabel: "Hapus",
        danger: true,
      }))) return;
      try {
        await window.apiFetch(`/api/services/${id}`, { method: "DELETE" });
        await loadServices();
      } catch (error) {
        await window.NMSUi.alert(error.message || "Gagal menghapus layanan.");
      }
    }
  });

  serviceFormPanel.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (svcSave.disabled) return;
    const name = svcName.value.trim();
    const sla = Number(svcSla.value);
    if (!name) {
      svcNote.textContent = "Nama layanan wajib diisi.";
      svcNote.hidden = false;
      svcName.focus();
      return;
    }
    if (!Number.isFinite(sla) || sla < 0 || sla > 100) {
      svcNote.textContent = "Target SLA harus antara 0 dan 100.";
      svcNote.hidden = false;
      return;
    }
    svcSave.disabled = true;
    svcNote.hidden = true;
    try {
      const payload = {
        name,
        division: svcDivision.value.trim(),
        description: svcDescription.value.trim(),
        sla_target: sla,
        mode: svcMode.value,
      };
      let serviceId = editingId;
      if (editingId) {
        await window.apiFetch(`/api/services/${editingId}`, { method: "PUT", body: payload });
      } else {
        const res = await window.apiFetch("/api/services", { method: "POST", body: payload });
        serviceId = res.service.id;
      }
      await window.apiFetch(`/api/services/${serviceId}/devices`, {
        method: "PUT",
        body: { device_ids: selectedDeviceIds() },
      });
      closeForm();
      await loadServices();
    } catch (error) {
      svcNote.textContent = error.message || "Gagal menyimpan layanan.";
      svcNote.hidden = false;
    } finally {
      svcSave.disabled = false;
    }
  });

  /* =====================================================
     SLA per layanan
     ===================================================== */

  const slaRows = document.getElementById("sla-rows");

  async function loadSla() {
    let services;
    try {
      const data = await window.apiFetch("/api/services");
      services = data.services || [];
    } catch (error) {
      slaRows.innerHTML = `<tr class="empty-row"><td colspan="5">Gagal memuat layanan: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    if (services.length === 0) {
      slaRows.innerHTML = '<tr class="empty-row"><td colspan="5">Belum ada layanan. Buat layanan di tab Layanan.</td></tr>';
      return;
    }
    const results = await Promise.allSettled(
      services.map((service) => window.apiFetch(`/api/services/${service.id}/availability?days=${slaDays}`))
    );
    slaRows.innerHTML = results
      .map((result, index) => {
        const service = services[index];
        if (result.status === "rejected") {
          return `<tr><td><strong>${escapeHtml(service.name)}</strong></td><td colspan="4">Gagal memuat: ${escapeHtml(result.reason.message)}</td></tr>`;
        }
        const stats = result.value;
        const pct = stats.availability_pct;
        const met = pct != null && pct >= service.sla_target;
        return (
          `<tr>` +
          `<td><strong>${escapeHtml(service.name)}</strong></td>` +
          `<td><span class="${availabilityClass(pct)}">${pct == null ? "—" : formatPercent(pct, 2)}</span></td>` +
          `<td class="mono">${escapeHtml(formatPercent(service.sla_target, 2))}</td>` +
          `<td>${pct == null ? '<span class="status status--idle">Tanpa data</span>' : met ? '<span class="status status--ok">Tercapai</span>' : '<span class="status status--down">Tidak tercapai</span>'}</td>` +
          `<td class="mono">${stats.cycles}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  document.querySelectorAll(".seg__btn[data-days]").forEach((button) => {
    button.addEventListener("click", () => {
      slaDays = Number(button.dataset.days);
      document.querySelectorAll(".seg__btn[data-days]").forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      loadSla();
    });
  });

  /* =====================================================
     SLA report + CSV
     ===================================================== */

  const slaReportRows = document.getElementById("sla-report-rows");
  const slaPeriod = document.getElementById("sla-period");
  const slaCsv = document.getElementById("sla-csv");

  async function loadSlaReport() {
    slaCsv.href = `/api/services/sla-report.csv?period=${slaReportDays}`;
    let report;
    try {
      report = await window.apiFetch(`/api/services/sla-report?period=${slaReportDays}`);
    } catch (error) {
      slaReportRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat laporan: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const rows = report.rows || [];
    if (rows.length === 0) {
      slaReportRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada layanan.</td></tr>';
      return;
    }
    slaReportRows.innerHTML = rows
      .map((row) => {
        const pct = row.availability_pct;
        const met = row.met;
        const downtime = row.downtime_minutes == null ? "—" : `${Math.round(row.downtime_minutes)} menit`;
        return (
          `<tr>` +
          `<td><strong>${escapeHtml(row.service)}</strong></td>` +
          `<td>${escapeHtml(row.division || "—")}</td>` +
          `<td class="mono">${escapeHtml(formatPercent(row.sla_target, 2))}</td>` +
          `<td><span class="${availabilityClass(pct)}">${pct == null ? "—" : formatPercent(pct, 2)}</span></td>` +
          `<td class="mono">${escapeHtml(downtime)}</td>` +
          `<td>${met == null ? '<span class="status status--idle">Tanpa data</span>' : met ? '<span class="status status--ok">Tercapai</span>' : '<span class="status status--down">Tidak tercapai</span>'}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  slaPeriod.addEventListener("change", () => {
    slaReportDays = Number(slaPeriod.value);
    loadSlaReport();
  });

  /* =====================================================
     Boot
     ===================================================== */

  whenReady(() => {
    if (!canEdit()) {
      serviceAdd.hidden = true;
    }
    activateView();
    window.NMSStore.startAutoRefresh(activateView, 15000);
  });
})();
