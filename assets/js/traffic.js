/* Traffic: NetFlow/IPFIX — ringkasan, talkers, aplikasi, percakapan, status. */
(function () {
  "use strict";

  const { escapeHtml, formatBytes, formatBps } = window.NMSFormat;

  const TABS = {
    summary: { title: "Ringkasan", desc: "Total trafik per eksporter dan top talkers." },
    talkers: { title: "Top Talkers", desc: "IP yang paling banyak mengirim/menerima." },
    apps: { title: "Aplikasi / Port", desc: "Pemetaan protokol+port lokal." },
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

  const $ = (id) => document.getElementById(id);

  function queryString() {
    const params = new URLSearchParams();
    if (currentExporter) params.set("exporter_id", currentExporter);
    params.set("range", currentRange);
    return params.toString();
  }

  function activate(tab) {
    currentTab = TABS[tab] ? tab : "summary";
    document.querySelectorAll("[data-view]").forEach((view) => {
      view.hidden = view.dataset.view !== currentTab;
    });
    document.querySelectorAll("[data-tab]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.tab === currentTab);
    });
    const meta = TABS[currentTab];
    $("view-title").textContent = meta.title;
    $("view-desc").textContent = meta.desc;
    document.title = `${meta.title} — Traffic — NMS Kopegtel`;
    if (currentTab === "summary") loadSummary();
    if (currentTab === "talkers") loadTalkers();
    if (currentTab === "apps") loadApps();
    if (currentTab === "conversations") loadConversations();
    if (currentTab === "status") loadStatus();
  }

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      const tab = button.dataset.tab;
      if (!TABS[tab]) return;
      // Muat hanya lewat satu jalur: set hash → hashchange → activate().
      // Klik tab yang sudah aktif tetap memuat ulang sekali (tanpa event hash).
      if (window.location.hash === `#${tab}`) {
        activate(tab);
        return;
      }
      window.location.hash = tab;
    });
  });
  window.addEventListener("hashchange", () => activate(window.location.hash.slice(1)));
  $("refresh").addEventListener("click", () => activate(currentTab));

  $("flow-exporter").addEventListener("change", async (event) => {
    currentExporter = event.target.value;
    // Opsi interface milik eksporter lama — muat ulang agar tidak menyesatkan.
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

  function renderBars(containerId, rows) {
    const container = $(containerId);
    if (!rows.length) {
      container.innerHTML = '<p class="panel__sub">Belum ada data.</p>';
      return;
    }
    const max = Math.max(...rows.map((row) => row.bytes || 0), 1);
    container.innerHTML = rows.map((row) => `
      <div class="flow-bar">
        <span class="mono" title="${escapeHtml(row.key)}">${escapeHtml(row.key)}</span>
        <span class="flow-bar__track"><span class="flow-bar__fill" style="width: ${Math.round(100 * row.bytes / max)}%"></span></span>
        <span class="mono">${formatBytes(row.bytes)}</span>
      </div>`).join("");
  }

  function renderExporters(data) {
    $("flow-summary-total").textContent =
      `Total: ${formatBytes(data.total_bytes)} · ${data.total_packets.toLocaleString("id-ID")} paket · ${data.total_flows.toLocaleString("id-ID")} flow`;
    $("flow-exporter-rows").innerHTML = (data.exporters || []).map((exporter) => `
      <tr>
        <td>${escapeHtml(exporter.name || exporter.device_id)}<br><span class="panel__sub">${escapeHtml(exporter.ip || "")}</span></td>
        <td>${formatBytes(exporter.bytes)}</td>
        <td>${(exporter.packets || 0).toLocaleString("id-ID")}</td>
        <td>${(exporter.flows || 0).toLocaleString("id-ID")}</td>
        <td class="mono">${exporter.series && exporter.series.length
          ? formatBps(Math.round((exporter.series[exporter.series.length - 1].bytes || 0) * 8 / (data.grain === "raw" ? 300 : 3600)))
          : "—"}</td>
      </tr>`).join("");
    $("flow-summary-empty").hidden = (data.exporters || []).length > 0;
    const chart = $("flow-chart");
    const selected = (data.exporters || []).find((exporter) => String(exporter.device_id) === String(currentExporter));
    if (selected && selected.series_bps && selected.series_bps.length > 1) {
      chart.hidden = false;
      drawChart($("flow-chart-svg"), selected.series_bps.map((point) => point.bps));
    } else {
      chart.hidden = true;
    }
  }

  function drawChart(svg, values) {
    const width = 600, height = 120, pad = 8;
    const max = Math.max(...values, 1);
    const step = (width - pad * 2) / (values.length - 1);
    const points = values.map((value, index) => {
      const x = pad + index * step;
      const y = height - pad - (value / max) * (height - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    svg.innerHTML = `<polyline fill="none" stroke="#2fe0c4" stroke-width="2" points="${points.join(" ")}" />`
      + `<line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="rgba(255,255,255,.15)" />`
      + `<text x="${pad}" y="12" fill="rgba(255,255,255,.6)" font-size="10">puncak ${escapeHtml(formatBps(max))}</text>`;
  }

  function renderInterfaceRows(entries) {
    const filtered = entries.filter((entry) => !currentInterface || entry.interface === currentInterface);
    $("flow-if-rows").innerHTML = filtered.map((entry) => `
      <tr>
        <td class="mono">${escapeHtml(entry.interface)}</td>
        <td>${escapeHtml((entry.direction || "").toUpperCase())}</td>
        <td>${formatBytes(entry.bytes)}</td>
        <td>${(entry.packets || 0).toLocaleString("id-ID")}</td>
        <td>${(entry.flows || 0).toLocaleString("id-ID")}</td>
      </tr>`).join("");
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

  async function loadSummary() {
    try {
      const data = await window.apiFetch(`/api/flow/summary?${queryString()}`);
      renderExporters(data);
      if (isViewer()) return; // viewer: hanya total, tanpa detail
      await loadInterfaceRows();
      const [talkers, apps] = await Promise.all([
        window.apiFetch(`/api/flow/talkers?kind=src&limit=10&${queryString()}`),
        window.apiFetch(`/api/flow/apps?limit=10&${queryString()}`),
      ]);
      renderBars("flow-talkers-bar", talkers.entries || []);
      renderBars("flow-apps-bar", apps.apps || []);
    } catch (error) {
      $("flow-summary-total").textContent = error.message;
    }
  }

  async function loadTalkers() {
    if (isViewer()) { renderDetailForbidden("flow-talkers-rows", 6); return; }
    const dir = $("flow-talkers-dir").value;
    try {
      const data = await window.apiFetch(`/api/flow/talkers?kind=${dir}&limit=100&${queryString()}`);
      $("flow-talkers-rows").innerHTML = (data.entries || []).map((entry, index) => `
        <tr>
          <td>${index + 1}</td>
          <td class="mono">${escapeHtml(entry.key)}</td>
          <td>${entry.host ? escapeHtml(entry.host) : "-"}</td>
          <td>${formatBytes(entry.bytes)}</td>
          <td>${(entry.packets || 0).toLocaleString("id-ID")}</td>
          <td>${(entry.flows || 0).toLocaleString("id-ID")}</td>
        </tr>`).join("");
    } catch (error) {
      $("flow-talkers-rows").innerHTML = `<tr class="empty-row"><td colspan="6">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  async function loadApps() {
    if (isViewer()) { renderDetailForbidden("flow-apps-rows", 5); renderDetailForbidden("flow-protos-rows", 5); return; }
    try {
      const data = await window.apiFetch(`/api/flow/apps?limit=100&${queryString()}`);
      $("flow-apps-rows").innerHTML = (data.apps || []).map((entry, index) => `
        <tr><td>${index + 1}</td><td>${escapeHtml(entry.key)}</td><td>${formatBytes(entry.bytes)}</td>
          <td>${(entry.packets || 0).toLocaleString("id-ID")}</td><td>${(entry.flows || 0).toLocaleString("id-ID")}</td></tr>`).join("");
      $("flow-protos-rows").innerHTML = (data.protos || []).map((entry, index) => `
        <tr><td>${index + 1}</td><td>${escapeHtml(entry.key)}</td><td>${formatBytes(entry.bytes)}</td>
          <td>${(entry.packets || 0).toLocaleString("id-ID")}</td><td>${(entry.flows || 0).toLocaleString("id-ID")}</td></tr>`).join("");
    } catch (error) {
      $("flow-apps-rows").innerHTML = `<tr class="empty-row"><td colspan="5">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  async function loadConversations() {
    if (isViewer()) { renderDetailForbidden("flow-conv-rows", 5); return; }
    try {
      const data = await window.apiFetch(`/api/flow/conversations?limit=100&${queryString()}`);
      $("flow-conv-rows").innerHTML = (data.entries || []).map((entry, index) => `
        <tr><td>${index + 1}</td><td class="mono">${escapeHtml(entry.key)}</td><td>${formatBytes(entry.bytes)}</td>
          <td>${(entry.packets || 0).toLocaleString("id-ID")}</td><td>${(entry.flows || 0).toLocaleString("id-ID")}</td></tr>`).join("");
    } catch (error) {
      $("flow-conv-rows").innerHTML = `<tr class="empty-row"><td colspan="5">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  function renderDetailForbidden(id, colspan) {
    $(id).innerHTML = `<tr class="empty-row"><td colspan="${colspan}">Detail hanya untuk operator/admin. Viewer hanya melihat total.</td></tr>`;
  }

  async function loadStatus() {
    try {
      const data = await window.apiFetch("/api/flow/status");
      const labels = { netflow_udp: "NetFlow UDP", ipfix_udp: "IPFIX UDP" };
      $("flow-status-rows").innerHTML = Object.entries(data.receivers || {}).map(([kind, item]) => {
        if (kind === "templates" || kind === "bucket") return "";
        return `
        <tr>
          <td>${escapeHtml(labels[kind] || kind)}</td>
          <td>${item.running ? "Berjalan" : "Berhenti"}</td>
          <td class="mono">${escapeHtml(item.bind_address || "-")}:${escapeHtml(item.port || "-")}</td>
          <td>${item.last_packet_at ? escapeHtml(window.NMSFormat.formatDateTime(item.last_packet_at)) : "—"}</td>
          <td>${(item.flows || 0).toLocaleString("id-ID")}</td>
          <td>${escapeHtml(formatBytes(item.bytes || 0))}</td>
          <td>${Number(item.dropped_unknown || 0)} / ${Number(item.dropped_rate || 0)} / ${Number(item.dropped_invalid || 0)} / ${Number(item.dropped_no_template || 0)}</td>
          <td>${Number((data.receivers.templates && Object.values(data.receivers.templates).reduce((a, b) => a + b, 0)) || 0)}</td>
        </tr>`;
      }).join("");
      const counts = data.counts || {};
      const anyRunning = Object.values(data.receivers || {}).some((item) => item && item.running);
      $("flow-status-empty").hidden = anyRunning;
      $("flow-status-note").textContent = `Bucket tersimpan: ${counts.flow_buckets || 0} · per jam: ${counts.flow_buckets_hourly || 0} · menunggu flush: ${(data.receivers.bucket && data.receivers.bucket.pending) || 0}`;
    } catch (error) {
      $("flow-status-note").textContent = error.message;
    }
  }

  async function loadInterfaceOptions() {
    try {
      const data = await window.apiFetch(`/api/flow/interfaces?${queryString()}`);
      const names = [...new Set((data.entries || []).map((entry) => entry.interface))].sort();
      $("flow-interface").innerHTML = '<option value="">Semua interface</option>' + names
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
  document.querySelectorAll("#flow-talkers-export, #flow-apps-export, #flow-conv-export").forEach((button) => {
    button.addEventListener("click", () => {
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
    }
    await loadExporters();
    await loadInterfaceOptions();
    activate(currentTab);
  });
})();
