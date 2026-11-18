/* Inventory: overview, hosts (filter server-side + pagination), detail perangkat. */
(function () {
  "use strict";

  const {
    escapeHtml,
    timeAgo,
    formatPercent,
    formatBytes,
    formatBps,
    formatUptime,
    formatDateTime,
    STATUS_META,
    TYPE_LABELS,
    availabilityClass,
    SEVERITY_META,
  } = window.NMSFormat;

  const VIEWS = {
    overview: { label: "Ringkasan", desc: "Ringkasan aset perangkat dan infrastruktur." },
    hosts: { label: "Daftar Host", desc: "Daftar host terdaftar beserta konfigurasi dan statusnya." },
    host: { label: "Detail perangkat", desc: "Metrik sumber daya dan trafik perangkat." },
  };

  const views = document.querySelectorAll("[data-view]");
  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");

  let currentDetailId = null;
  let current = "overview";

  function parseHash(raw) {
    const value = String(raw || "").replace(/^#/, "");
    if (value.startsWith("host/")) {
      const id = Number(value.slice(5));
      if (Number.isInteger(id) && id > 0) return { name: "host", id };
      return { name: "hosts", id: null };
    }
    return { name: Object.prototype.hasOwnProperty.call(VIEWS, value) ? value : "overview", id: null };
  }

  function activateView() {
    const parsed = parseHash(window.location.hash);
    current = parsed.name;
    const meta = VIEWS[parsed.name];

    views.forEach((view) => {
      view.hidden = view.dataset.view !== parsed.name;
    });
    viewTitle.textContent = meta.label;
    viewDesc.textContent = meta.desc;
    window.NMSFormat.setDocumentTitle(meta.label, "Inventaris");

    if (parsed.name === "overview") loadOverview();
    else if (parsed.name === "hosts") showHosts();
    else if (parsed.name === "host" && parsed.id) {
      currentDetailId = parsed.id;
      window.setTimeout(() => loadDeviceDetail(), 0);
    }
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

  function canEdit() {
    const role = window.__nmsMe && window.__nmsMe.role;
    return role === "admin" || role === "operator";
  }

  function tierAvail(pct, warn, crit) {
    if (pct == null) return null;
    if (pct >= crit) return { cls: "avail avail--down", label: `${pct.toFixed(0)}%` };
    if (pct >= warn) return { cls: "avail avail--warn", label: `${pct.toFixed(0)}%` };
    return { cls: "avail avail--ok", label: `${pct.toFixed(0)}%` };
  }

  /* =====================================================
     Overview
     ===================================================== */

  const invTotal = document.getElementById("inv-total");
  const invTotalMeta = document.getElementById("inv-total-meta");
  const invOnline = document.getElementById("inv-online");
  const invOnlinePct = document.getElementById("inv-online-pct");
  const invAttention = document.getElementById("inv-attention");
  const invMaintenance = document.getElementById("inv-maintenance");
  const bytypeRows = document.getElementById("bytype-rows");
  const attentionRows = document.getElementById("attention-rows");
  const attentionCount = document.getElementById("attention-count");
  const moduleRows = document.getElementById("module-rows");
  const modulesCount = document.getElementById("modules-count");

  async function loadModules() {
    let data;
    try {
      data = await window.apiFetch("/api/optical/modules");
    } catch (error) {
      moduleRows.innerHTML = `<tr class="empty-row"><td colspan="8">Gagal memuat modul optik: ${escapeHtml(error.message)}</td></tr>`;
      modulesCount.textContent = "-";
      return;
    }
    const modules = data.modules || [];
    modulesCount.textContent = modules.length === 0 ? "Tidak ada" : `${modules.length} modul`;
    moduleRows.innerHTML = modules.length
      ? modules
          .map((mod) => (
            `<tr><td><a class="cell-link" href="inventory.html#host/${mod.device_id}">${escapeHtml(mod.device_name)}</a></td>` +
            `<td class="mono">${escapeHtml(mod.if_name)}</td>` +
            `<td>${escapeHtml(mod.vendor || "-")}</td>` +
            `<td>${escapeHtml(mod.part_no || "-")}</td>` +
            `<td class="mono">${escapeHtml(mod.serial || "-")}</td>` +
            `<td class="mono">${mod.wavelength_nm == null ? "-" : `${mod.wavelength_nm} nm`}</td>` +
            `<td>${escapeHtml(mod.type || "-")}</td>` +
            `<td class="mono">${escapeHtml(timeAgo(mod.last_seen))}</td></tr>`
          ))
          .join("")
      : '<tr class="empty-row"><td colspan="8">Belum ada modul optik terdeteksi. Modul SFP dengan DDM yang terpantau di perangkat akan tampil di sini.</td></tr>';
  }

  async function loadOverview() {
    loadModules(); // modul optik dimuat independen dari ringkasan perangkat
    let data;
    try {
      data = await window.apiFetch("/api/inventory/summary");
    } catch (error) {
      bytypeRows.innerHTML = `<tr class="empty-row"><td colspan="7">Gagal memuat ringkasan: ${escapeHtml(error.message)}</td></tr>`;
      attentionRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat daftar.</td></tr>`;
      return;
    }
    const byType = data.by_type || [];
    const attention = data.attention || [];
    const total = data.total || 0;
    const online = byType.reduce((sum, row) => sum + (row.online || 0), 0);
    const maintenance = byType.reduce((sum, row) => sum + (row.maintenance || 0), 0);

    invTotal.textContent = String(total);
    invTotalMeta.textContent = `${byType.length} tipe perangkat`;
    invOnline.textContent = String(online);
    invOnlinePct.textContent = total ? formatPercent((online / total) * 100, 1) : "-";
    invAttention.textContent = String(attention.length);
    invMaintenance.textContent = String(maintenance);

    bytypeRows.innerHTML = byType.length
      ? byType
          .map((row) => {
            const attentionCountRow = (row.degraded || 0) + (row.offline || 0) + (row.unknown || 0);
            return (
              `<tr><td><strong>${escapeHtml(TYPE_LABELS[row.type] || row.type)}</strong></td>` +
              `<td class="mono">${row.total}</td>` +
              `<td class="mono">${row.online}</td>` +
              `<td class="mono">${row.degraded}</td>` +
              `<td class="mono">${row.offline}</td>` +
              `<td class="mono">${row.unknown}</td>` +
              `<td class="mono">${row.maintenance}</td></tr>`
            );
          })
          .join("")
      : '<tr class="empty-row"><td colspan="7">Belum ada perangkat. Tambahkan perangkat pertama lewat menu Perangkat.</td></tr>';

    attentionCount.textContent = attention.length === 0 ? "Tidak ada" : `${attention.length} perangkat`;
    attentionRows.innerHTML = attention.length
      ? attention
          .map((device) => {
            const meta = STATUS_META[device.status] || STATUS_META.unknown;
            return (
              `<tr><td><a class="cell-link" href="inventory.html#host/${device.id}">${escapeHtml(device.name)}</a></td>` +
              `<td>${escapeHtml(TYPE_LABELS[device.type] || device.type)}</td>` +
              `<td class="mono">${escapeHtml(device.ip)}</td>` +
              `<td>${escapeHtml(device.location || "-")}</td>` +
              `<td><span class="status ${escapeHtml(meta.cls)}">${escapeHtml(meta.label)}</span></td>` +
              `<td class="mono">${escapeHtml(timeAgo(device.last_seen))}</td></tr>`
            );
          })
          .join("")
      : '<tr class="empty-row"><td colspan="6">Semua perangkat tersambung. Tidak ada yang perlu perhatian.</td></tr>';
  }

  /* =====================================================
     Hosts
     ===================================================== */

  const hostRows = document.getElementById("host-rows");
  const hostCount = document.getElementById("host-count");
  const hostError = document.getElementById("host-error");
  const hostSearch = document.getElementById("host-search");
  const hostStatus = document.getElementById("host-status");
  const hostGroup = document.getElementById("host-group");
  const hostType = document.getElementById("host-type");
  const hostRouteros = document.getElementById("host-routeros");
  const hostModel = document.getElementById("host-model");
  const hostPrev = document.getElementById("host-prev");
  const hostNext = document.getElementById("host-next");
  const hostPageInfo = document.getElementById("host-page-info");

  const hostState = { page: 1, total: 0, pages: 1 };

  async function loadGroupOptions(selectEl) {
    try {
      const data = await window.apiFetch("/api/groups");
      for (const group of data.groups || []) {
        const option = document.createElement("option");
        option.value = String(group.id);
        option.textContent = group.name;
        selectEl.appendChild(option);
      }
    } catch {
      /* filter grup opsional */
    }
  }

  function loadTypeOptions(selectEl) {
    for (const [value, label] of Object.entries(TYPE_LABELS)) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      selectEl.appendChild(option);
    }
  }

  async function loadMtFilterOptions() {
    try {
      const data = await window.apiFetch("/api/mikrotik/filter-options");
      for (const version of data.versions || []) {
        const option = document.createElement("option");
        option.value = version;
        option.textContent = version;
        hostRouteros.appendChild(option);
      }
      for (const model of data.models || []) {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model;
        hostModel.appendChild(option);
      }
    } catch {
      /* filter MikroTik opsional */
    }
  }

  function metricCell(pct, warn, crit) {
    if (pct == null) return '<td class="mono">-</td>';
    const tier = tierAvail(pct, warn, crit);
    return `<td><span class="${escapeHtml(tier.cls)}" title="${pct.toFixed(1)}% (ambang peringatan ${warn}%, kritis ${crit}%)">${escapeHtml(tier.label)}</span></td>`;
  }

  async function loadHosts() {
    hostError.hidden = true;
    const params = new URLSearchParams({ page: String(hostState.page), per_page: "10" });
    if (hostSearch.value.trim()) params.set("search", hostSearch.value.trim());
    if (hostStatus.value) params.set("status", hostStatus.value);
    if (hostGroup.value) params.set("group", hostGroup.value);
    if (hostType.value) params.set("type", hostType.value);
    if (hostRouteros.value) params.set("routeros_version", hostRouteros.value);
    if (hostModel.value) params.set("mikrotik_model", hostModel.value);

    let data;
    try {
      data = await window.apiFetch(`/api/devices?${params.toString()}`);
    } catch (error) {
      hostError.hidden = false;
      hostRows.innerHTML = `<tr class="empty-row"><td colspan="12">Gagal memuat perangkat: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }

    hostState.total = data.total || 0;
    hostState.pages = data.pages || 1;
    hostCount.textContent = `${hostState.total} perangkat`;

    document.getElementById("hosts-stat-total").textContent = String(hostState.total);
    const devices = data.devices || [];

    // Stats diambil dari ringkasan (seluruh perangkat, bukan halaman ini).
    window.apiFetch("/api/summary")
      .then((summary) => {
        document.getElementById("hosts-stat-online").textContent = String(summary.online || 0);
        document.getElementById("hosts-stat-offline").textContent = String(summary.offline || 0);
        document.getElementById("hosts-stat-degraded").textContent = String(summary.degraded || 0);
      })
      .catch(() => {});

    if (devices.length === 0) {
      const filtered = hostSearch.value.trim() || hostStatus.value || hostGroup.value || hostType.value || hostRouteros.value || hostModel.value;
      hostRows.innerHTML = filtered
        ? '<tr class="empty-row"><td colspan="12">Tidak ada perangkat yang cocok dengan filter saat ini.</td></tr>'
        : '<tr class="empty-row"><td colspan="12">Belum ada perangkat. Tambahkan perangkat pertama lewat tombol Tambah perangkat.</td></tr>';
    } else {
      const editable = canEdit();
      hostRows.innerHTML = devices
        .map((device) => {
          const meta = STATUS_META[device.status] || STATUS_META.unknown;
          const cpu = metricCell(device.mikrotik_enabled || device.snmp_enabled ? device.cpu_pct : null, 70, 90);
          const mem = metricCell(device.mikrotik_enabled || device.snmp_enabled ? device.mem_pct : null, 70, 90);
          return (
            `<tr data-status="${escapeHtml(device.status)}">` +
            `<td><a class="cell-link" href="inventory.html#host/${device.id}">${escapeHtml(device.name)}</a><span class="cell-sub">${escapeHtml(TYPE_LABELS[device.type] || device.type)}</span></td>` +
            `<td class="mono">${escapeHtml(device.ip)}</td>` +
            `<td>${escapeHtml(TYPE_LABELS[device.type] || device.type)}</td>` +
            `<td class="mono">${escapeHtml(device.routeros_version || "-")}</td>` +
            `<td>${escapeHtml(device.mikrotik_model || "-")}</td>` +
            `<td>${escapeHtml(device.group_name || "-")}</td>` +
            `<td>${escapeHtml(device.location || "-")}</td>` +
            `<td class="mono">${device.latency_ms == null ? "-" : `${Number(device.latency_ms).toFixed(0)} ms`}</td>` +
            cpu + mem +
            `<td><span class="status ${escapeHtml(meta.cls)}">${escapeHtml(meta.label)}</span></td>` +
            `<td>${editable
              ? `<button class="ack-toggle" type="button" data-action="edit" data-id="${device.id}">Ubah</button> ` +
                `<button class="ack-toggle" type="button" data-action="delete" data-id="${device.id}">Hapus</button>`
              : "-"}</td>` +
            `</tr>`
          );
        })
        .join("");
    }
    hostPageInfo.textContent = `Halaman ${data.page} dari ${hostState.pages}`;
    hostPrev.disabled = data.page <= 1;
    hostNext.disabled = data.page >= hostState.pages;
  }

  function showHosts() {
    loadHosts();
  }

  hostSearch.addEventListener("input", debounce(() => { hostState.page = 1; loadHosts(); }, 250));
  hostStatus.addEventListener("change", () => { hostState.page = 1; loadHosts(); });
  hostGroup.addEventListener("change", () => { hostState.page = 1; loadHosts(); });
  hostType.addEventListener("change", () => { hostState.page = 1; loadHosts(); });
  hostRouteros.addEventListener("change", () => { hostState.page = 1; loadHosts(); });
  hostModel.addEventListener("change", () => { hostState.page = 1; loadHosts(); });
  hostPrev.addEventListener("click", () => { if (hostState.page > 1) { hostState.page -= 1; loadHosts(); } });
  hostNext.addEventListener("click", () => { if (hostState.page < hostState.pages) { hostState.page += 1; loadHosts(); } });
  document.getElementById("host-retry").addEventListener("click", loadHosts);
  document.getElementById("add-device-btn").addEventListener("click", () =>
    window.openDeviceForm({ onSaved: () => { window.NMSStore.invalidate(); loadHosts(); } })
  );

  hostRows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = Number(button.dataset.id);
    if (button.dataset.action === "edit") {
      try {
        const res = await window.apiFetch(`/api/devices/${id}`);
        window.openDeviceForm({ device: res.device, onSaved: () => { window.NMSStore.invalidate(); loadHosts(); } });
      } catch (error) {
        await window.NMSUi.alert(error.message || "Gagal memuat perangkat.");
      }
      return;
    }
    if (button.dataset.action === "delete") {
      if (!(await window.NMSUi.confirm("Hapus perangkat ini? Riwayat pemeriksaan dan metriknya ikut terhapus.", {
        title: "Hapus perangkat",
        confirmLabel: "Hapus",
        danger: true,
      }))) return;
      try {
        await window.apiFetch(`/api/devices/${id}`, { method: "DELETE" });
        window.NMSStore.invalidate();
        loadHosts();
      } catch (error) {
        await window.NMSUi.alert(error.message || "Gagal menghapus perangkat.");
      }
    }
  });

  /* =====================================================
     Detail perangkat (#host/<id>)
     ===================================================== */

  const detailEls = {
    title: document.getElementById("host-detail-title"),
    sub: document.getElementById("detail-sub"),
    status: document.getElementById("detail-status"),
    refresh: document.getElementById("detail-refresh"),
    updated: document.getElementById("detail-updated"),
    availability: document.getElementById("detail-availability"),
    error: document.getElementById("detail-error"),
    errorText: document.getElementById("detail-error-text"),
    retry: document.getElementById("detail-retry"),
    empty: document.getElementById("detail-empty"),
    emptyDesc: document.getElementById("detail-empty-desc"),
    body: document.getElementById("detail-body"),
    cpu: document.getElementById("d-cpu"),
    cpuSub: document.getElementById("d-cpu-sub"),
    mem: document.getElementById("d-mem"),
    memSub: document.getElementById("d-mem-sub"),
    storage: document.getElementById("d-storage"),
    storageSub: document.getElementById("d-storage-sub"),
    temp: document.getElementById("d-temp"),
    tempSub: document.getElementById("d-temp-sub"),
    voltage: document.getElementById("d-voltage"),
    voltageSub: document.getElementById("d-voltage-sub"),
    uptime: document.getElementById("d-uptime"),
    uptimeSub: document.getElementById("d-uptime-sub"),
    barCpu: document.getElementById("bar-cpu"),
    barMem: document.getElementById("bar-mem"),
    barStorage: document.getElementById("bar-storage"),
    chipCpu: document.getElementById("chip-cpu"),
    chipMem: document.getElementById("chip-mem"),
    chipStorage: document.getElementById("chip-storage"),
    chipTemp: document.getElementById("chip-temp"),
    chipVoltage: document.getElementById("chip-voltage"),
    chipUptime: document.getElementById("chip-uptime"),
    ifaceRows: document.getElementById("detail-interfaces"),
    trafficIf: document.getElementById("traffic-if"),
    chartCpu: document.getElementById("chart-cpu"),
    chartMem: document.getElementById("chart-mem"),
    chartTraffic: document.getElementById("chart-traffic"),
    chartCpuSummary: document.getElementById("chart-cpu-summary"),
    chartMemSummary: document.getElementById("chart-mem-summary"),
    trafficRxSummary: document.getElementById("traffic-rx-summary"),
    trafficTxSummary: document.getElementById("traffic-tx-summary"),
    segButtons: Array.from(document.querySelectorAll(".seg__btn[data-range]")),
    thresholdsForm: document.getElementById("thresholds-form"),
    thCpuWarn: document.getElementById("th-cpu-warn"),
    thCpuCrit: document.getElementById("th-cpu-crit"),
    thMemWarn: document.getElementById("th-mem-warn"),
    thMemCrit: document.getElementById("th-mem-crit"),
    thStorageWarn: document.getElementById("th-storage-warn"),
    thStorageCrit: document.getElementById("th-storage-crit"),
    thTempWarn: document.getElementById("th-temp-warn"),
    thTempCrit: document.getElementById("th-temp-crit"),
  };

  const METRICS_ERROR_TEXT = {
    api_disabled: "RouterOS API belum dikonfigurasi atau dimatikan (api_disabled).",
    auth_failed: "Kredensial API ditolak oleh perangkat (auth_failed).",
    timeout: "Koneksi ke API RouterOS timeout (timeout).",
    unreachable: "Port API RouterOS tidak terjangkau (unreachable).",
    tls_error: "Verifikasi TLS ke RouterOS gagal. Periksa sertifikat dan fingerprint perangkat (tls_error).",
    tls_fingerprint_mismatch: "Fingerprint sertifikat RouterOS tidak cocok dengan pengaturan perangkat (tls_fingerprint_mismatch).",
    snmp_no_response: "RouterOS API gagal dan SNMP tidak merespons (snmp_no_response).",
    error: "Terjadi galat saat membaca metrik (error).",
  };

  function fmtAgo(iso) {
    return timeAgo(iso);
  }

  function chipClassFor(value, warn, crit) {
    if (value == null) return { text: "Tanpa data", cls: "chip--muted", bar: "is-ok" };
    if (value >= crit) return { text: "Kritis", cls: "chip--down", bar: "is-down" };
    if (value >= warn) return { text: "Peringatan", cls: "chip--warn", bar: "is-warn" };
    return { text: "Normal", cls: "chip--ok", bar: "is-ok" };
  }

  function setPresenceChip(chipEl, hasData, okText) {
    if (!chipEl) return;
    chipEl.textContent = hasData ? okText : "Tanpa data";
    chipEl.className = `metric-chip ${hasData ? "chip--ok" : "chip--muted"}`;
  }

  function setResourceCard(valueEl, barEl, chipEl, value, warn, crit) {
    const tier = chipClassFor(value, warn, crit);
    valueEl.textContent = value == null ? "-" : value.toFixed(0);
    if (barEl) {
      barEl.style.width = `${Math.max(0, Math.min(100, value ?? 0))}%`;
      barEl.className = tier.bar;
    }
    if (chipEl) {
      chipEl.textContent = tier.text;
      chipEl.className = `metric-chip ${tier.cls}`;
    }
  }

  function renderLineChart(svgEl, lines, opts) {
    const W = 600, H = 170, PAD = 16;
    const titleText = (svgEl.querySelector("title") || {}).textContent || "Grafik";
    svgEl.innerHTML = "";
    const n = lines.reduce((acc, line) => Math.max(acc, line.values.length), 0);
    const allValues = lines.flatMap((line) => line.values).filter((v) => v != null);
    if (n < 2 || allValues.length < 2) {
      svgEl.innerHTML = `<title>${escapeHtml(titleText)}</title><text class="chart__empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">Belum cukup data untuk grafik.</text>`;
      return null;
    }
    const fixedMin = opts && opts.min != null ? opts.min : Math.min(...allValues);
    const fixedMax = opts && opts.max != null ? opts.max : Math.max(...allValues);
    const min = fixedMin;
    const max = fixedMax - min < 1e-9 ? fixedMin + 1 : fixedMax;
    const xAt = (index) => PAD + ((W - 2 * PAD) * index) / (n - 1);
    const yAt = (value) => PAD + (H - 2 * PAD) * (1 - (value - min) / (max - min));

    const parts = [`<title>${escapeHtml(titleText)}</title>`];
    [0, 0.5, 1].forEach((fraction) => {
      const y = yAt(min + (max - min) * fraction);
      parts.push(`<line class="chart__grid" x1="${PAD}" y1="${y.toFixed(1)}" x2="${W - PAD}" y2="${y.toFixed(1)}" />`);
    });
    lines.forEach((line, lineIndex) => {
      const coords = [];
      line.values.forEach((value, index) => {
        if (value == null) return;
        coords.push([xAt(index), yAt(value)]);
      });
      if (coords.length < 2) return;
      const pointString = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
      if (lineIndex === 0 && !(opts && opts.noArea)) {
        parts.push(`<polygon class="chart__area" points="${PAD},${(H - PAD).toFixed(1)} ${pointString} ${(W - PAD).toFixed(1)},${(H - PAD).toFixed(1)}" />`);
      }
      const lineClass = lineIndex === 0 ? "chart__line" : "chart__line chart__line--tx";
      parts.push(`<polyline class="${lineClass}" points="${pointString}" />`);
    });
    svgEl.innerHTML = parts.join("");
    return { min, max };
  }

  function chartSummary(values, format) {
    const valid = values.filter((v) => v != null);
    if (valid.length === 0) return "min - · rata-rata - · maks -";
    const min = Math.min(...valid);
    const max = Math.max(...valid);
    const avg = valid.reduce((sum, v) => sum + v, 0) / valid.length;
    return `Min ${format(min)} · Rata-rata ${format(avg)} · Maks ${format(max)}`;
  }

  let detailRange = "1h";
  let detailMetricsData = null;

  function showDetailError(message) {
    detailEls.errorText.textContent = message || "Gagal memuat metrik perangkat.";
    detailEls.error.hidden = false;
  }

  function renderInterfaces(interfaces) {
    detailEls.ifaceRows.innerHTML = "";
    if (!interfaces || interfaces.length === 0) {
      detailEls.ifaceRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada data interface. Tunggu poll pertama.</td></tr>';
      return;
    }
    for (const iface of interfaces) {
      const status = iface.disabled
        ? { label: "Dinonaktifkan", cls: "avail avail--warn" }
        : iface.running
          ? { label: "Berjalan", cls: "avail avail--ok" }
          : { label: "Tidak aktif", cls: "avail avail--down" };
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${escapeHtml(iface.if_name)}</td>` +
        `<td><span class="${status.cls}">${escapeHtml(status.label)}</span></td>` +
        `<td class="mono">${escapeHtml(formatBps(iface.rx_bps))}</td>` +
        `<td class="mono">${escapeHtml(formatBps(iface.tx_bps))}</td>` +
        `<td class="mono">${escapeHtml(formatBytes(iface.rx_bytes))}</td>` +
        `<td class="mono">${escapeHtml(formatBytes(iface.tx_bytes))}</td>`;
      detailEls.ifaceRows.appendChild(tr);
    }
  }

  function renderTrafficChart() {
    const selected = detailEls.trafficIf.value;
    const series = (detailMetricsData && detailMetricsData.interfaces) || [];
    const current2 = series.find((item) => item.if_name === selected) || series[0];
    const rxValues = current2 ? current2.points.map((p) => p.rx_bps) : [];
    const txValues = current2 ? current2.points.map((p) => p.tx_bps) : [];
    renderLineChart(detailEls.chartTraffic, [{ values: rxValues }, { values: txValues }], { min: 0, noArea: true });
    const fmt = (v) => formatBps(v);
    const peak = (values) => (values.filter((v) => v != null).length ? fmt(Math.max(...values.filter((v) => v != null))) : "-");
    detailEls.trafficRxSummary.textContent = peak(rxValues);
    detailEls.trafficTxSummary.textContent = peak(txValues);
  }

  function syncTrafficSelect() {
    const series = (detailMetricsData && detailMetricsData.interfaces) || [];
    const current2 = detailEls.trafficIf.value;
    detailEls.trafficIf.innerHTML = "";
    if (series.length === 0) {
      detailEls.trafficIf.innerHTML = '<option value="">Tidak ada interface</option>';
      return;
    }
    for (const item of series) {
      const option = document.createElement("option");
      option.value = item.if_name;
      option.textContent = item.if_name;
      detailEls.trafficIf.appendChild(option);
    }
    detailEls.trafficIf.value = series.some((item) => item.if_name === current2) ? current2 : series[0].if_name;
  }

  function renderDeviceDetail(snapshot, metricsData, interfacesData) {
    const device = (snapshot && snapshot.device) || null;
    const metrics = (snapshot && snapshot.metrics) || null;
    detailMetricsData = metricsData;
    if (!device) return;

    detailEls.title.textContent = device.name;
    const model = device.mikrotik_model || device.mikrotik_board || "model tidak tersedia";
    const version = device.routeros_version ? `RouterOS ${device.routeros_version}` : "RouterOS -";
    detailEls.sub.textContent = `${device.ip} · ${model} · ${version}${device.mikrotik_serial ? ` · SN ${device.mikrotik_serial}` : ""}`;

    const meta = STATUS_META[device.status] || STATUS_META.unknown;
    detailEls.status.textContent = meta.label;
    detailEls.status.className = `status ${meta.cls}`;

    const avail = device.availability || {};
    const fmtAvail = (a) => (a && a.pct != null ? formatPercent(a.pct) : "-");
    if (detailEls.availability) {
      detailEls.availability.textContent = `Ketersediaan 24 jam: ${fmtAvail(avail.h24)} · 7 hari: ${fmtAvail(avail.h7d)} · 30 hari: ${fmtAvail(avail.h30d)}`;
    }

    if (device.metrics_error) {
      showDetailError(METRICS_ERROR_TEXT[device.metrics_error] || `Gagal membaca metrik (${device.metrics_error}).`);
    } else {
      detailEls.error.hidden = true;
    }

    if (!metrics) {
      detailEls.empty.hidden = false;
      detailEls.body.hidden = true;
      detailEls.emptyDesc.textContent = device.mikrotik_enabled || device.snmp_enabled
        ? "Metrik akan tampil setelah poller berhasil menghubungi perangkat minimal satu kali."
        : "Perangkat ini belum memiliki sumber metrik. Aktifkan MikroTik API atau SNMP pada pengaturan perangkat.";
      detailEls.updated.textContent = "Terakhir diperbarui -";
      return;
    }
    detailEls.empty.hidden = true;
    detailEls.body.hidden = false;

    const thresholds = device.thresholds || {};
    setResourceCard(detailEls.cpu, detailEls.barCpu, detailEls.chipCpu, metrics.cpu_pct, thresholds.cpu_warn, thresholds.cpu_crit);
    setResourceCard(detailEls.mem, detailEls.barMem, detailEls.chipMem, metrics.mem_pct, thresholds.mem_warn, thresholds.mem_crit);
    setResourceCard(detailEls.storage, detailEls.barStorage, detailEls.chipStorage, metrics.storage_pct, thresholds.storage_warn, thresholds.storage_crit);

    const cpuCores = device.cpu_count ? `${device.cpu_count} core` : "inti tidak diketahui";
    const cpuFreq = device.cpu_frequency_mhz ? ` · ${device.cpu_frequency_mhz} MHz` : "";
    detailEls.cpuSub.textContent = `${cpuCores}${cpuFreq}`;
    detailEls.memSub.textContent = metrics.mem_total != null
      ? `${formatBytes(metrics.mem_used)} / ${formatBytes(metrics.mem_total)} terpakai`
      : "-";
    detailEls.storageSub.textContent = metrics.disk_total != null
      ? `${formatBytes(metrics.disk_used)} / ${formatBytes(metrics.disk_total)} terpakai`
      : "-";

    const tempTier = chipClassFor(metrics.temp_c, thresholds.temp_warn, thresholds.temp_crit);
    detailEls.temp.textContent = metrics.temp_c == null ? "-" : metrics.temp_c.toFixed(1);
    detailEls.chipTemp.textContent = tempTier.text;
    detailEls.chipTemp.className = `metric-chip ${tempTier.cls}`;
    detailEls.tempSub.textContent = metrics.cpu_temp_c != null ? `Suhu CPU ${metrics.cpu_temp_c.toFixed(1)} °C` : "Suhu CPU -";

    detailEls.voltage.textContent = metrics.voltage == null ? "-" : metrics.voltage.toFixed(1);
    detailEls.voltageSub.textContent = "Tegangan masuk";
    setPresenceChip(detailEls.chipVoltage, metrics.voltage != null, "Normal");
    detailEls.uptime.textContent = formatUptime(metrics.uptime_s);
    detailEls.uptimeSub.textContent = metrics.ping_ms != null ? `Ping ${Math.round(metrics.ping_ms)} ms` : "Sejak mulai ulang terakhir";
    setPresenceChip(detailEls.chipUptime, metrics.uptime_s != null, "Aktif");
    detailEls.updated.textContent = `Terakhir diperbarui ${fmtAgo(metrics.ts)}`;

    const cpuValues = (metricsData && metricsData.points.map((p) => p.cpu_pct)) || [];
    const memValues = (metricsData && metricsData.points.map((p) => p.mem_pct)) || [];
    renderLineChart(detailEls.chartCpu, [{ values: cpuValues }], { min: 0, max: 100 });
    renderLineChart(detailEls.chartMem, [{ values: memValues }], { min: 0, max: 100 });
    detailEls.chartCpuSummary.textContent = chartSummary(cpuValues, (v) => `${v.toFixed(0)}%`);
    detailEls.chartMemSummary.textContent = chartSummary(memValues, (v) => `${v.toFixed(0)}%`);

    syncTrafficSelect();
    renderTrafficChart();
    renderInterfaces(interfacesData ? interfacesData.interfaces : null);

    if (!detailEls.thresholdsForm.contains(document.activeElement)) {
      detailEls.thCpuWarn.value = device.threshold_cpu_warn ?? 70;
      detailEls.thCpuCrit.value = device.threshold_cpu_crit ?? 90;
      detailEls.thMemWarn.value = device.threshold_mem_warn ?? 70;
      detailEls.thMemCrit.value = device.threshold_mem_crit ?? 90;
      detailEls.thStorageWarn.value = device.threshold_storage_warn ?? 70;
      detailEls.thStorageCrit.value = device.threshold_storage_crit ?? 90;
      detailEls.thTempWarn.value = device.threshold_temp_warn ?? 60;
      detailEls.thTempCrit.value = device.threshold_temp_crit ?? 75;
    }
  }

  async function loadDeviceDetail() {
    const id = currentDetailId;
    if (!id) return;

    detailEls.error.hidden = true;
    let snapshot = null;
    try {
      snapshot = await window.apiFetch(`/api/devices/${id}/snapshot`);
    } catch (error) {
      detailEls.title.textContent = "Detail perangkat";
      detailEls.sub.textContent = "Gagal memuat informasi perangkat.";
      detailEls.errorText.textContent = error.message || "Perangkat tidak dapat dimuat.";
      detailEls.error.hidden = false;
      detailEls.empty.hidden = true;
      detailEls.body.hidden = true;
      return;
    }

    const results = await Promise.allSettled([
      window.apiFetch(`/api/devices/${id}/metrics?range=${detailRange}`),
      window.apiFetch(`/api/devices/${id}/interfaces`),
    ]);
    const metricsData = results[0].status === "fulfilled" ? results[0].value : null;
    const interfacesData = results[1].status === "fulfilled" ? results[1].value : null;
    if (results[0].status === "rejected") showDetailError(results[0].reason.message || "Grafik riwayat tidak dapat dimuat.");
    if (results[1].status === "rejected") showDetailError(results[1].reason.message || "Daftar antarmuka tidak dapat dimuat.");

    renderDeviceDetail(snapshot, metricsData, interfacesData);
  }

  detailEls.refresh.addEventListener("click", async () => {
    if (!currentDetailId) return;
    detailEls.refresh.disabled = true;
    detailEls.refresh.classList.add("is-busy");
    try {
      await window.apiFetch(`/api/devices/${currentDetailId}/poll`, { method: "POST" });
    } catch (error) {
      showDetailError(error.message || "Gagal memicu poll.");
    } finally {
      detailEls.refresh.disabled = false;
      detailEls.refresh.classList.remove("is-busy");
      await loadDeviceDetail();
    }
  });

  detailEls.retry.addEventListener("click", () => loadDeviceDetail());
  detailEls.segButtons.forEach((button) => {
    button.addEventListener("click", () => {
      detailRange = button.dataset.range;
      detailEls.segButtons.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      loadDeviceDetail();
    });
  });
  detailEls.trafficIf.addEventListener("change", renderTrafficChart);

  detailEls.thresholdsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!currentDetailId) return;
    const read = (input) => Number(input.value);
    const pairs = [
      [detailEls.thCpuWarn, detailEls.thCpuCrit, "CPU"],
      [detailEls.thMemWarn, detailEls.thMemCrit, "Memori"],
      [detailEls.thStorageWarn, detailEls.thStorageCrit, "Penyimpanan"],
      [detailEls.thTempWarn, detailEls.thTempCrit, "Suhu"],
    ];
    for (const [warnInput, critInput, label] of pairs) {
      if (read(warnInput) > read(critInput) || warnInput.value === "" || critInput.value === "") {
        showDetailError(`Ambang ${label}: peringatan tidak boleh melebihi kritis, dan keduanya wajib diisi.`);
        return;
      }
    }
    const submitBtn = document.getElementById("thresholds-save");
    submitBtn.disabled = true;
    try {
      await window.apiFetch(`/api/devices/${currentDetailId}/thresholds`, {
        method: "PUT",
        body: {
          cpu_warn: read(detailEls.thCpuWarn),
          cpu_crit: read(detailEls.thCpuCrit),
          mem_warn: read(detailEls.thMemWarn),
          mem_crit: read(detailEls.thMemCrit),
          storage_warn: read(detailEls.thStorageWarn),
          storage_crit: read(detailEls.thStorageCrit),
          temp_warn: read(detailEls.thTempWarn),
          temp_crit: read(detailEls.thTempCrit),
        },
      });
      detailEls.error.hidden = true;
      await loadDeviceDetail();
    } catch (error) {
      showDetailError(error.message || "Gagal menyimpan ambang.");
    } finally {
      submitBtn.disabled = false;
    }
  });

  /* =====================================================
     Boot
     ===================================================== */

  whenReady(() => {
    loadGroupOptions(hostGroup);
    loadTypeOptions(hostType);
    loadMtFilterOptions();
    const role = (window.__nmsMe || {}).role;
    if (role !== "admin" && role !== "operator") {
      const add = document.getElementById("add-device-btn");
      if (add) add.hidden = true;
    }
    activateView();
    window.NMSStore.startAutoRefresh(() => {
      if (current === "overview") loadOverview();
      else if (current === "hosts") loadHosts();
      else if (current === "host") loadDeviceDetail();
    }, current === "host" ? 10000 : 15000);
    window.addEventListener("nms:devices-changed", () => {
      window.NMSStore.invalidate();
      activateView();
    });
  });
})();
