/* Traffic: NetFlow/IPFIX. Ringkasan, talkers, aplikasi, percakapan, status. */
(function () {
  "use strict";

  const { escapeHtml, formatBytes, formatBps } = window.NMSFormat;

  const TABS = {
    summary: { title: "Ringkasan", desc: "Total trafik per eksporter dan trafik teratas." },
    talkers: { title: "Trafik teratas", desc: "IP yang paling banyak mengirim/menerima." },
    apps: { title: "Aplikasi / Port", desc: "Pemetaan protokol dan port lokal." },
    conversations: { title: "Percakapan", desc: "Pasangan IP sumber > tujuan." },
    status: { title: "Status eksporter", desc: "Berjalan/berhenti, paket, flow diterima/dibuang, template." },
  };

  let currentTab = (window.location.hash || "#summary").slice(1);
  if (!TABS[currentTab]) currentTab = "summary";
  let exporters = [];
  let currentExporter = "";
  let currentRange = "1h";
  let currentInterface = "";
  const canViewDetail = () => window.__nmsMe && ["admin", "operator"].includes(window.__nmsMe.role);
  const isViewer = () => window.__nmsMe && window.__nmsMe.role === "viewer";
  const RANGE_LABELS = { "1h": "1 jam terakhir", "24h": "24 jam terakhir", "7d": "7 hari terakhir" };
  const counts = (value) => (Number(value) || 0).toLocaleString("id-ID");
  const rangeLabel = () => RANGE_LABELS[currentRange] || currentRange;

  const $ = (id) => document.getElementById(id);

  function queryString() {
    const params = new URLSearchParams();
    if (currentExporter) params.set("exporter_id", currentExporter);
    params.set("range", currentRange);
    return params.toString();
  }

  const tabButtons = Array.from(document.querySelectorAll("[data-tab]"));

  function activate(tab) {
    currentTab = TABS[tab] ? tab : "summary";
    document.querySelectorAll("[data-view]").forEach((view) => {
      view.hidden = view.dataset.view !== currentTab;
    });
    tabButtons.forEach((button) => {
      const active = button.dataset.tab === currentTab;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    const meta = TABS[currentTab];
    $("view-title").textContent = meta.title;
    $("view-desc").textContent = meta.desc;
    window.NMSFormat.setDocumentTitle(meta.title, "Lalu lintas");
    if (currentTab === "summary") loadSummary();
    if (currentTab === "talkers") loadTalkers();
    if (currentTab === "apps") loadApps();
    if (currentTab === "conversations") loadConversations();
    if (currentTab === "status") loadStatus();
  }

  function selectTab(tab) {
    if (!TABS[tab]) return;
    // Muat hanya lewat satu jalur: set hash → hashchange → activate().
    if (window.location.hash === `#${tab}`) {
      activate(tab);
      return;
    }
    window.location.hash = tab;
  }

  function focusTabSoon(button) {
    // nav.js memindahkan fokus ke judul pada hashchange; kembalikan fokus ke tab
    // agar navigasi panah tetap sesuai pola ARIA tablist.
    window.setTimeout(() => button.focus({ preventScroll: true }), 0);
  }

  tabButtons.forEach((button) => {
    button.addEventListener("click", () => {
      selectTab(button.dataset.tab);
      focusTabSoon(button);
    });
  });
  document.querySelector("[role='tablist']").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const enabled = tabButtons.filter((button) => !button.hidden);
    if (!enabled.length) return;
    event.preventDefault();
    const currentIndex = Math.max(0, enabled.indexOf(document.activeElement));
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % enabled.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + enabled.length) % enabled.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = enabled.length - 1;
    const next = enabled[nextIndex];
    next.focus();
    selectTab(next.dataset.tab);
    focusTabSoon(next);
  });
  window.addEventListener("hashchange", () => activate(window.location.hash.slice(1)));
  $("refresh").addEventListener("click", () => activate(currentTab));

  $("flow-exporter").addEventListener("change", async (event) => {
    currentExporter = event.target.value;
    // Opsi interface milik eksporter lama. Muat ulang agar tidak menyesatkan.
    currentInterface = "";
    await loadInterfaceOptions();
    activate(currentTab);
  });
  $("flow-range").addEventListener("change", async (event) => {
    currentRange = event.target.value;
    currentInterface = "";
    await loadInterfaceOptions();
    activate(currentTab);
  });
  $("flow-interface").addEventListener("change", (event) => {
    currentInterface = event.target.value;
    activate(currentTab);
  });

  function renderBars(containerId, rows, emptyText) {
    const container = $(containerId);
    container.setAttribute("aria-busy", "false");
    if (!rows.length) {
      container.innerHTML = `<p class="flow-bars__state">${escapeHtml(emptyText || "Belum ada data pada rentang ini.")}</p>`;
      return;
    }
    const max = Math.max(...rows.map((row) => Number(row.bytes) || 0), 1);
    container.innerHTML = rows.map((row, index) => `
      <div class="flow-bar">
        <span class="flow-bar__rank">${String(index + 1).padStart(2, "0")}</span>
        <span class="flow-bar__label mono" title="${escapeHtml(row.key)}">${escapeHtml(row.key)}</span>
        <span class="flow-bar__track" aria-hidden="true"><span class="flow-bar__fill" style="width: ${Math.max(2, Math.round(100 * (Number(row.bytes) || 0) / max))}%"></span></span>
        <span class="flow-bar__value mono">${formatBytes(Number(row.bytes) || 0)}</span>
      </div>`).join("");
  }

  function renderBarsError(containerId, message) {
    const container = $(containerId);
    container.setAttribute("aria-busy", "false");
    container.innerHTML = `<p class="flow-bars__state flow-bars__state--error">${escapeHtml(message || "Data gagal dimuat.")}</p>`;
  }

  function lastRate(exporters, grain) {
    const period = grain === "raw" ? 300 : 3600;
    return exporters.reduce((total, exporter) => {
      const series = exporter.series_bps || exporter.series || [];
      const last = series[series.length - 1];
      if (!last) return total;
      return total + (last.bps != null ? Number(last.bps) : Math.round((Number(last.bytes) || 0) * 8 / period));
    }, 0);
  }

  function renderStats(data) {
    const exporters = data.exporters || [];
    const grainLabel = data.grain === "raw" ? "Bucket terakhir · 5 menit" : "Bucket terakhir · 1 jam";
    $("flow-stat-bytes").textContent = formatBytes(Number(data.total_bytes) || 0);
    $("flow-stat-bytes-meta").textContent = `${counts(exporters.length)} eksporter aktif`;
    $("flow-stat-rate").textContent = formatBps(lastRate(exporters, data.grain));
    $("flow-stat-rate-meta").textContent = grainLabel;
    $("flow-stat-flows").textContent = counts(data.total_flows);
    $("flow-stat-packets").textContent = counts(data.total_packets);
  }

  function renderExporters(data) {
    const exporters = data.exporters || [];
    const tableWrap = $("flow-summary-table-wrap");
    const empty = $("flow-summary-empty");
    const grainLabel = data.grain === "raw" ? "bucket 5 menit" : "agregat per jam";

    renderStats(data);
    $("flow-summary-error").hidden = true;

    if (!exporters.length) {
      $("flow-exporter-rows").innerHTML = "";
      tableWrap.hidden = true;
      empty.hidden = false;
      $("flow-summary-total").textContent = `Tidak ada flow pada ${rangeLabel()}.`;
    } else {
      tableWrap.hidden = false;
      empty.hidden = true;
      $("flow-summary-total").textContent =
        `${counts(exporters.length)} eksporter · ${rangeLabel()} · ${grainLabel}`;
      $("flow-exporter-rows").innerHTML = exporters.map((exporter) => {
        const series = exporter.series_bps || exporter.series || [];
        const last = series[series.length - 1];
        const period = data.grain === "raw" ? 300 : 3600;
        const bps = last
          ? (last.bps != null ? Number(last.bps) : Math.round((Number(last.bytes) || 0) * 8 / period))
          : null;
        return `
          <tr>
            <td>
              <strong>${escapeHtml(exporter.name || exporter.device_id)}</strong>
              <span class="cell-sub mono">${escapeHtml(exporter.ip || "IP tidak diketahui")}</span>
            </td>
            <td class="num">${formatBytes(Number(exporter.bytes) || 0)}</td>
            <td class="num">${counts(exporter.packets)}</td>
            <td class="num">${counts(exporter.flows)}</td>
            <td class="num mono">${bps == null ? "-" : formatBps(bps)}</td>
          </tr>`;
      }).join("");
    }

    const chart = $("flow-chart");
    const selected = exporters.find((exporter) => String(exporter.device_id) === String(currentExporter));
    $("flow-chart-grain").textContent = data.grain === "raw" ? "Bucket 5 menit" : "Agregat per jam";
    if (selected && selected.series_bps && selected.series_bps.length > 1) {
      const values = selected.series_bps.map((point) => Number(point.bps) || 0);
      const name = selected.name || selected.ip || "eksporter terpilih";
      $("flow-chart-title").textContent = `Tren laju · ${name}`;
      $("flow-chart-sub").textContent =
        `Puncak ${formatBps(Math.max(...values))} · ${rangeLabel()}`;
      chart.hidden = false;
      drawChart($("flow-chart-svg"), values, name);
    } else {
      chart.hidden = true;
    }
  }

  function drawChart(svg, values, name) {
    const width = 600, height = 140, padX = 12, padY = 22, baseY = height - padY;
    const max = Math.max(...values, 1);
    const step = (width - padX * 2) / Math.max(values.length - 1, 1);
    const points = values.map((value, index) => {
      const x = padX + index * step;
      const y = baseY - (value / max) * (baseY - 20);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const area = `${padX},${baseY} ${points.join(" ")} ${width - padX},${baseY}`;
    const grid = [0.25, 0.5, 0.75].map((part) => {
      const y = (baseY - (baseY - 20) * part).toFixed(1);
      return `<line class="flow-chart__grid" x1="${padX}" y1="${y}" x2="${width - padX}" y2="${y}" />`;
    }).join("");
    svg.setAttribute("aria-label", `Grafik laju ${name}. Puncak ${formatBps(max)}.`);
    svg.innerHTML = `
      ${grid}
      <polygon class="flow-chart__area" points="${area}" />
      <polyline class="flow-chart__line" points="${points.join(" ")}" />
      <line class="flow-chart__baseline" x1="${padX}" y1="${baseY}" x2="${width - padX}" y2="${baseY}" />
      <text class="flow-chart__label" x="${padX}" y="13">Puncak ${escapeHtml(formatBps(max))}</text>`;
  }

  function renderInterfaceRows(entries) {
    const filtered = entries.filter((entry) => !currentInterface || entry.interface === currentInterface);
    if (!filtered.length) {
      $("flow-if-rows").innerHTML = '<tr class="empty-row"><td colspan="5">Tidak ada data antarmuka pada rentang ini.</td></tr>';
    } else {
      $("flow-if-rows").innerHTML = filtered.map((entry) => `
        <tr>
          <td class="mono">${escapeHtml(entry.interface)}</td>
          <td>${escapeHtml((entry.direction || "").toUpperCase() || "-")}</td>
          <td class="num">${formatBytes(Number(entry.bytes) || 0)}</td>
          <td class="num">${counts(entry.packets)}</td>
          <td class="num">${counts(entry.flows)}</td>
        </tr>`).join("");
    }
    $("flow-if-panel").hidden = isViewer();
  }

  async function loadInterfaceRows() {
    if (isViewer()) { $("flow-if-panel").hidden = true; return; }
    try {
      const data = await window.apiFetch(`/api/flow/interfaces?${queryString()}`);
      renderInterfaceRows(data.entries || []);
    } catch (error) {
      $("flow-if-rows").innerHTML = `<tr class="empty-row"><td colspan="5">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  function resetStats() {
    ["flow-stat-bytes", "flow-stat-rate", "flow-stat-flows", "flow-stat-packets"].forEach((id) => {
      $(id).textContent = "-";
    });
    $("flow-stat-bytes-meta").textContent = "Menunggu data";
    $("flow-stat-rate-meta").textContent = rangeLabel();
  }

  function showSummaryError(error) {
    $("flow-summary-error-text").textContent = error.message || "Ringkasan flow gagal dimuat.";
    $("flow-summary-error").hidden = false;
    $("flow-summary-table-wrap").hidden = true;
    $("flow-summary-empty").hidden = true;
    $("flow-chart").hidden = true;
    $("flow-summary-total").textContent = "Ringkasan flow gagal dimuat.";
    resetStats();
  }

  async function loadSummary() {
    $("flow-summary-error").hidden = true;
    try {
      const data = await window.apiFetch(`/api/flow/summary?${queryString()}`);
      renderExporters(data);
    } catch (error) {
      showSummaryError(error);
      return;
    }

    if (isViewer()) {
      $("flow-detail-grid").hidden = true;
      $("flow-if-panel").hidden = true;
      return;
    }

    $("flow-detail-grid").hidden = false;
    await loadInterfaceRows();

    const [talkers, apps] = await Promise.allSettled([
      window.apiFetch(`/api/flow/talkers?kind=src&limit=10&${queryString()}`),
      window.apiFetch(`/api/flow/apps?limit=10&${queryString()}`),
    ]);
    if (talkers.status === "fulfilled") renderBars("flow-talkers-bar", talkers.value.entries || []);
    else renderBarsError("flow-talkers-bar", talkers.reason && talkers.reason.message);
    if (apps.status === "fulfilled") renderBars("flow-apps-bar", apps.value.apps || []);
    else renderBarsError("flow-apps-bar", apps.reason && apps.reason.message);
  }

  async function loadTalkers() {
    if (isViewer()) { renderDetailForbidden("flow-talkers-rows", 6); return; }
    const dir = $("flow-talkers-dir").value;
    try {
      const data = await window.apiFetch(`/api/flow/talkers?kind=${dir}&limit=100&${queryString()}`);
      const entries = data.entries || [];
      $("flow-talkers-rows").innerHTML = entries.length
        ? entries.map((entry, index) => `
          <tr>
            <td class="flow-rank">${index + 1}</td>
            <td class="mono">${escapeHtml(entry.key)}</td>
            <td>${entry.host ? escapeHtml(entry.host) : "-"}</td>
            <td class="num">${formatBytes(Number(entry.bytes) || 0)}</td>
            <td class="num">${counts(entry.packets)}</td>
            <td class="num">${counts(entry.flows)}</td>
          </tr>`).join("")
        : '<tr class="empty-row"><td colspan="6">Tidak ada trafik pada rentang ini.</td></tr>';
    } catch (error) {
      $("flow-talkers-rows").innerHTML = `<tr class="empty-row"><td colspan="6">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  function renderFlowRows(entries, emptyText) {
    if (!entries.length) {
      return `<tr class="empty-row"><td colspan="5">${escapeHtml(emptyText)}</td></tr>`;
    }
    return entries.map((entry, index) => {
      const bytes = Number(entry.bytes) || 0;
      return `
        <tr>
          <td class="flow-rank">${index + 1}</td>
          <td class="flow-key">${escapeHtml(entry.key || "-")}</td>
          <td class="flow-num">${formatBytes(bytes)}</td>
          <td class="flow-num">${(Number(entry.packets) || 0).toLocaleString("id-ID")}</td>
          <td class="flow-num">${(Number(entry.flows) || 0).toLocaleString("id-ID")}</td>
        </tr>`;
    }).join("");
  }

  async function loadApps() {
    const count = $("flow-apps-count");
    $("flow-apps-error").hidden = true;
    $("flow-apps-empty").hidden = true;
    $("flow-apps-tables").hidden = true;
    $("flow-apps-export").disabled = true;
    $("flow-apps-tables").setAttribute("aria-busy", "true");
    count.textContent = "Memuat data aplikasi…";

    if (isViewer()) {
      count.textContent = "Detail dibatasi untuk pengamat.";
      $("flow-apps-empty-title").textContent = "Detail dibatasi";
      $("flow-apps-empty-desc").textContent = "Akun pengamat hanya dapat melihat ringkasan total trafik.";
      $("flow-apps-setup").hidden = true;
      $("flow-apps-empty-actions").hidden = true;
      $("flow-apps-empty").hidden = false;
      $("flow-apps-export").hidden = true;
      $("flow-apps-tables").removeAttribute("aria-busy");
      return;
    }

    $("flow-apps-export").hidden = false;
    $("flow-apps-setup").hidden = false;
    $("flow-apps-empty-actions").hidden = false;
    $("flow-apps-empty-cta").hidden = false;
    $("flow-apps-empty-retry").hidden = false;
    $("flow-apps-empty-title").textContent = "Belum ada data aplikasi";
    $("flow-apps-empty-desc").textContent = "Aplikasi dan protokol akan muncul setelah penerima NetFlow/IPFIX menerima flow dari router.";

    try {
      const data = await window.apiFetch(`/api/flow/apps?limit=100&${queryString()}`);
      const apps = data.apps || [];
      const protos = data.protos || [];
      const hasData = apps.length > 0 || protos.length > 0;
      const appsLabel = apps.length >= 100 ? "100 aplikasi teratas" : `${apps.length.toLocaleString("id-ID")} aplikasi`;
      count.textContent = hasData
        ? `${appsLabel} · ${protos.length.toLocaleString("id-ID")} protokol`
        : "Tidak ada flow pada rentang ini.";
      $("flow-apps-export").disabled = !hasData;
      $("flow-apps-tables").hidden = !hasData;
      $("flow-apps-empty").hidden = hasData;
      $("flow-apps-rows").innerHTML = renderFlowRows(apps, "Tidak ada aplikasi pada rentang ini.");
      $("flow-protos-rows").innerHTML = renderFlowRows(protos, "Tidak ada protokol pada rentang ini.");
    } catch (error) {
      count.textContent = "Gagal memuat data aplikasi.";
      $("flow-apps-error-text").textContent = error.message || "Data aplikasi gagal dimuat.";
      $("flow-apps-error").hidden = false;
    } finally {
      $("flow-apps-tables").removeAttribute("aria-busy");
    }
  }

  async function loadConversations() {
    if (isViewer()) { renderDetailForbidden("flow-conv-rows", 5); return; }
    try {
      const data = await window.apiFetch(`/api/flow/conversations?limit=100&${queryString()}`);
      const entries = data.entries || [];
      $("flow-conv-rows").innerHTML = entries.length
        ? entries.map((entry, index) => `
          <tr>
            <td class="flow-rank">${index + 1}</td>
            <td class="mono">${escapeHtml(entry.key)}</td>
            <td class="num">${formatBytes(Number(entry.bytes) || 0)}</td>
            <td class="num">${counts(entry.packets)}</td>
            <td class="num">${counts(entry.flows)}</td>
          </tr>`).join("")
        : '<tr class="empty-row"><td colspan="5">Tidak ada percakapan pada rentang ini.</td></tr>';
    } catch (error) {
      $("flow-conv-rows").innerHTML = `<tr class="empty-row"><td colspan="5">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  function renderDetailForbidden(id, colspan) {
    $(id).innerHTML = `<tr class="empty-row"><td colspan="${colspan}">Detail hanya untuk operator/admin. Pengamat hanya melihat total.</td></tr>`;
  }

  async function loadStatus() {
    try {
      const data = await window.apiFetch("/api/flow/status");
      const labels = { netflow_udp: "NetFlow UDP", ipfix_udp: "IPFIX UDP" };
      const rows = Object.entries(data.receivers || {}).filter(([kind]) => kind !== "templates" && kind !== "bucket");
      $("flow-status-rows").innerHTML = rows.length
        ? rows.map(([kind, item]) => {
          const running = Boolean(item && item.running);
          return `
            <tr>
              <td>${escapeHtml(labels[kind] || kind)}</td>
              <td><span class="status ${running ? "status--ok" : "status--down"}">${running ? "Berjalan" : "Berhenti"}</span></td>
              <td class="mono">${escapeHtml(item.bind_address || "-")}:${escapeHtml(item.port || "-")}</td>
              <td>${item.last_packet_at ? escapeHtml(window.NMSFormat.formatDateTime(item.last_packet_at)) : "-"}</td>
              <td class="num">${counts(item.flows)}</td>
              <td class="num">${escapeHtml(formatBytes(item.bytes || 0))}</td>
              <td>${Number(item.dropped_unknown || 0)} / ${Number(item.dropped_rate || 0)} / ${Number(item.dropped_invalid || 0)} / ${Number(item.dropped_no_template || 0)}</td>
              <td class="num">${Number((data.receivers.templates && Object.values(data.receivers.templates).reduce((a, b) => a + b, 0)) || 0)}</td>
            </tr>`;
        }).join("")
        : '<tr class="empty-row"><td colspan="8">Belum ada penerima flow yang dikonfigurasi. Aktifkan NetFlow/IPFIX di Administrasi.</td></tr>';
      const totals = data.counts || {};
      const anyRunning = rows.some(([, item]) => item && item.running);
      $("flow-status-empty").hidden = anyRunning;
      $("flow-status-note").textContent = `Bucket tersimpan: ${counts(totals.flow_buckets)} · per jam: ${counts(totals.flow_buckets_hourly)} · menunggu flush: ${counts(data.receivers.bucket && data.receivers.bucket.pending)}`;
    } catch (error) {
      $("flow-status-rows").innerHTML = `<tr class="empty-row"><td colspan="8">${escapeHtml(error.message)}</td></tr>`;
      $("flow-status-note").textContent = "Status penerima flow gagal dimuat.";
    }
  }

  async function loadInterfaceOptions() {
    try {
      const data = await window.apiFetch(`/api/flow/interfaces?${queryString()}`);
      const names = [...new Set((data.entries || []).map((entry) => entry.interface))].sort();
      $("flow-interface").innerHTML = '<option value="">Semua antarmuka</option>' + names
        .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
      $("flow-interface-wrap").hidden = names.length === 0;
    } catch (error) {
      $("flow-interface-wrap").hidden = true;
    }
  }

  async function loadExporters() {
    try {
      const data = await window.apiFetch("/api/flow/status");
      exporters = data.exporters || [];
      $("flow-exporter").innerHTML = '<option value="">Semua eksporter</option>'
        + exporters.map((exporter) => `<option value="${exporter.id}">${escapeHtml(exporter.name)}</option>`).join("");
    } catch (error) {
      $("flow-exporter").innerHTML = '<option value="">Semua eksporter</option>';
    }
  }

  $("flow-talkers-dir").addEventListener("change", loadTalkers);
  $("flow-apps-retry").addEventListener("click", loadApps);
  $("flow-apps-empty-retry").addEventListener("click", loadApps);
  $("flow-summary-retry").addEventListener("click", loadSummary);
  $("flow-summary-error-retry").addEventListener("click", loadSummary);
  document.querySelectorAll("#flow-talkers-export, #flow-apps-export, #flow-conv-export").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      const kind = button.id === "flow-talkers-export" ? $("flow-talkers-dir").value : null;
      const target = button.id === "flow-talkers-export"
        ? `/api/flow/talkers.csv?kind=${kind}&${queryString()}`
        : button.id === "flow-apps-export"
          ? `/api/flow/apps.csv?${queryString()}`
          : `/api/flow/conversations.csv?${queryString()}`;
      window.location.href = target;
    });
  });

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  whenReady(async () => {
    if (isViewer()) {
      document.querySelectorAll("[data-tab]").forEach((button) => {
        if (button.dataset.tab !== "summary") button.hidden = true;
      });
      $("flow-detail-grid").hidden = true;
      $("flow-if-panel").hidden = true;
      $("flow-chart").hidden = true;
    }
    await loadExporters();
    await loadInterfaceOptions();
    activate(currentTab);
  });
})();
