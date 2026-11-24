/* Reports: informasi sistem, ketersediaan, masalah teratas, audit log, action log. */
(function () {
  "use strict";

  const {
    escapeHtml,
    formatBytes,
    formatDateTime,
    formatPercent,
    formatUptime,
    availabilityClass,
    timeAgo,
  } = window.NMSFormat;

  const VIEWS = {
    "system-information": { label: "Informasi sistem", desc: "Versi, waktu aktif, ukuran basis data, dan status poller." },
    "availability-report": { label: "Laporan ketersediaan", desc: "Ketersediaan tiap perangkat dan unduhan CSV." },
    "top-problems": { label: "Masalah teratas", desc: "Jenis masalah yang paling sering terbuka." },
    "fiber-attenuation": { label: "Laporan redaman", desc: "Margin Rx per link fiber, tren, dan unduhan CSV." },
    "mikrotik-fleet": { label: "Armada MikroTik", desc: "Versi RouterOS, firmware, waktu aktif, dan temuan keamanan armada." },
    "audit-log": { label: "Log audit", desc: "Jejak aksi pengguna." },
    "action-log": { label: "Log aksi", desc: "Hasil pengiriman notifikasi." },
  };

  const views = document.querySelectorAll("[data-view]");
  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");
  const isAdmin = () => window.__nmsMe && window.__nmsMe.role === "admin";

  function currentView() {
    const raw = window.location.hash.replace(/^#/, "");
    return Object.prototype.hasOwnProperty.call(VIEWS, raw) ? raw : "system-information";
  }

  let current = currentView();

  function activateView() {
    current = currentView();
    if (current === "audit-log" && !isAdmin()) {
      current = "system-information";
      window.location.hash = "#system-information";
    }
    const meta = VIEWS[current];
    views.forEach((view) => {
      view.hidden = view.dataset.view !== current;
    });
    viewTitle.textContent = meta.label;
    viewDesc.textContent = meta.desc;
    window.NMSFormat.setDocumentTitle(meta.label, "Laporan");
    if (current === "system-information") loadSystemInfo();
    else if (current === "availability-report") loadAvailability();
    else if (current === "top-problems") loadTopProblems();
    else if (current === "fiber-attenuation") loadFiberReport();
    else if (current === "mikrotik-fleet") loadMtFleetReport();
    else if (current === "audit-log") loadAuditLog(true);
    else if (current === "action-log") loadActionLog();
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

  const KIND_LABELS = {
    device_down: "Perangkat tidak merespons",
    high_latency: "Latensi tinggi / CPU tinggi",
    cpu_high: "CPU di atas ambang",
    mem_high: "Memori di atas ambang",
    disk_high: "Penyimpanan di atas ambang",
    temp_high: "Suhu di atas ambang",
    fiber_los: "LOS (kehilangan sinyal fiber)",
    fiber_rx_low: "Rx di bawah ambang",
    fiber_rx_high: "Rx di atas ambang",
    fiber_drop: "Penurunan mendadak Rx",
    fiber_trend: "Tren Rx memburuk",
    fiber_predicted: "Prediksi Rx melewati ambang",
    fiber_asymmetry: "Asimetri redaman",
    fiber_margin_low: "Margin operasi rendah",
    sfp_tx_fault: "Tx fault modul SFP",
    sfp_removed: "Modul SFP terlepas",
    sfp_changed: "Modul SFP berganti",
  };

  function formatUptimeLong(seconds) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${days} hari ${hours} jam ${minutes} menit`;
  }

  /* =====================================================
     System information
     ===================================================== */

  const sysinfoList = document.getElementById("sysinfo-list");
  const tableCountsRows = document.getElementById("table-counts-rows");

  async function loadSystemInfo() {
    let info;
    try {
      info = await window.apiFetch("/api/reports/system-info");
    } catch (error) {
      sysinfoList.innerHTML = `<div><dt>Gagal memuat</dt><dd>${escapeHtml(error.message)}</dd></div>`;
      return;
    }
    const items = [
      ["Versi aplikasi", info.app_version],
      ["Organisasi", info.org_name],
      ["Zona waktu", info.timezone],
      ["Waktu aktif server", formatUptimeLong(info.server_uptime_s || 0)],
      ["Waktu server (UTC)", info.time_utc],
    ];
    if (info.python_version) items.push(["Python", info.python_version]);
    if (info.flask_version) items.push(["Flask", info.flask_version]);
    if (info.db_path) items.push(["Basis data", `${info.db_path} (${formatBytes(info.db_size_bytes)})`]);
    items.push(["Poller", info.poller && info.poller.running ? `Berjalan (maks ${info.poller.max_workers} worker)` : "Tidak berjalan"]);
    sysinfoList.innerHTML = items
      .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value == null ? "-" : String(value))}</dd></div>`)
      .join("");

    const countsPanel = document.getElementById("table-counts-panel");
    const counts = info.table_counts;
    if (!counts || typeof counts !== "object") {
      // Peran non-admin tidak menerima hitungan tabel (M-7).
      if (countsPanel) countsPanel.hidden = true;
      tableCountsRows.innerHTML = "";
      return;
    }
    if (countsPanel) countsPanel.hidden = false;
    const labels = {
      devices: "Perangkat",
      device_checks: "Pemeriksaan",
      device_metrics: "Metrik (baris)",
      interface_stats: "Statistik antarmuka",
      events: "Kejadian",
      problems: "Masalah",
      audit_log: "Log audit",
      services: "Layanan",
      users: "Pengguna",
    };
    tableCountsRows.innerHTML = Object.entries(counts)
      .map(([table, n]) => `<tr><td>${escapeHtml(labels[table] || table)}</td><td class="mono">${n}</td></tr>`)
      .join("");
  }

  /* =====================================================
     Availability report
     ===================================================== */

  const availRows = document.getElementById("avail-rows");
  const availPeriod = document.getElementById("avail-period");
  const availCsv = document.getElementById("avail-csv");

  async function loadAvailability() {
    const period = availPeriod.value;
    availCsv.href = `/api/reports/availability.csv?period=${period}`;
    let report;
    try {
      report = await window.apiFetch(`/api/reports/availability?period=${period}`);
    } catch (error) {
      availRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat laporan: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const rows = report.rows || [];
    if (rows.length === 0) {
      availRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada perangkat. Tambahkan perangkat di Inventaris agar laporan terisi.</td></tr>';
      return;
    }
    availRows.innerHTML = rows
      .map((row) => (
        `<tr>` +
        `<td>${escapeHtml(row.device)}</td>` +
        `<td class="mono">${escapeHtml(row.ip)}</td>` +
        `<td>${escapeHtml(row.group_name || "-")}</td>` +
        `<td class="mono">${row.checks}</td>` +
        `<td class="mono">${row.up}</td>` +
        `<td><span class="${availabilityClass(row.pct)}">${row.pct == null ? "-" : formatPercent(row.pct, 2)}</span></td>` +
        `</tr>`
      ))
      .join("");
  }

  availPeriod.addEventListener("change", loadAvailability);

  /* =====================================================
     Top problems
     ===================================================== */

  const topRows = document.getElementById("top-rows");
  const topPeriod = document.getElementById("top-period");

  async function loadTopProblems() {
    const period = topPeriod.value;
    let report;
    try {
      report = await window.apiFetch(`/api/reports/top-problems?period=${period}`);
    } catch (error) {
      topRows.innerHTML = `<tr class="empty-row"><td colspan="4">Gagal memuat laporan: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const rows = report.rows || [];
    if (rows.length === 0) {
      topRows.innerHTML = '<tr class="empty-row"><td colspan="4">Tidak ada masalah dalam periode ini.</td></tr>';
      return;
    }
    topRows.innerHTML = rows
      .map((row) => (
        `<tr>` +
        `<td>${escapeHtml(KIND_LABELS[row.kind] || row.kind)}</td>` +
        `<td>${escapeHtml(row.device_name)} <span class="cell-sub mono">${escapeHtml(row.device_ip)}</span></td>` +
        `<td class="mono">${row.total}</td>` +
        `<td class="mono">${escapeHtml(formatDateTime(row.last_seen))}</td>` +
        `</tr>`
      ))
      .join("");
  }

  topPeriod.addEventListener("change", loadTopProblems);

  /* =====================================================
     Laporan redaman fiber
     ===================================================== */

  const fiberReportRows = document.getElementById("fiber-report-rows");

  async function loadFiberReport() {
    let report;
    try {
      report = await window.apiFetch("/api/reports/fiber-attenuation");
    } catch (error) {
      fiberReportRows.innerHTML = `<tr class="empty-row"><td colspan="10">Gagal memuat laporan: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const rows = report.rows || [];
    if (rows.length === 0) {
      fiberReportRows.innerHTML = '<tr class="empty-row"><td colspan="10">Belum ada link fiber terdaftar. Tambahkan lewat halaman Fiber → Link &amp; redaman.</td></tr>';
      return;
    }
    const fmtDb = (value) => (value == null ? "-" : `${Number(value).toFixed(2)} dB`);
    const fmtTrend = (value) => (value == null ? "-" : `${Number(value).toFixed(2)} dB/mgg`);
    const statusMeta = (status) => {
      if (status === "crit") return { cls: "avail avail--down", label: "Kritis" };
      if (status === "warn") return { cls: "avail avail--warn", label: "Waspada" };
      if (status === "ok") return { cls: "avail avail--ok", label: "Normal" };
      return { cls: "avail", label: "-" };
    };
    fiberReportRows.innerHTML = rows
      .map((row) => {
        const meta = statusMeta(row.status);
        const trends = [row.trend_rx_a_db_per_week, row.trend_rx_b_db_per_week].filter((v) => v != null);
        const trend = trends.length ? Math.min(...trends) : null;
        return (
          `<tr>` +
          `<td><a class="cell-link" href="fiber.html#links/${row.link_id}">${escapeHtml(row.link)}</a></td>` +
          `<td>${escapeHtml(row.device_a_name)} · ${escapeHtml(row.port_a)} → ${escapeHtml(row.end_b)}</td>` +
          `<td class="mono">${escapeHtml(String(row.length_km))} km</td>` +
          `<td class="mono">${fmtDb(row.attenuation_ab_db)}</td>` +
          `<td class="mono">${fmtDb(row.attenuation_ba_db)}</td>` +
          `<td class="mono">${fmtDb(row.budget_ab_db)}</td>` +
          `<td class="mono">${fmtDb(row.deviation_ab_db)}</td>` +
          `<td class="mono">${fmtDb(row.margin_min_db)}</td>` +
          `<td class="mono">${fmtTrend(trend)}</td>` +
          `<td><span class="${meta.cls}">${escapeHtml(meta.label)}</span></td>` +
          `</tr>`
        );
      })
      .join("");
  }

  /* =====================================================
     Laporan armada MikroTik
     ===================================================== */

  const mtFleetReportRows = document.getElementById("mt-fleet-report-rows");
  const mtFleetReportCount = document.getElementById("mt-fleet-report-count");

  const MT_FIRMWARE_META = {
    pending: { cls: "status status--warn", label: "Tertunda" },
    "up-to-date": { cls: "status status--ok", label: "Terbaru" },
    unknown: { cls: "status status--idle", label: "Tidak diketahui" },
  };

  async function loadMtFleetReport() {
    let report;
    try {
      report = await window.apiFetch("/api/mikrotik/reports/fleet");
    } catch (error) {
      mtFleetReportRows.innerHTML = `<tr class="empty-row"><td colspan="14">Gagal memuat laporan: ${escapeHtml(error.message)}</td></tr>`;
      mtFleetReportCount.textContent = "Gagal memuat laporan armada MikroTik.";
      return;
    }
    const rows = report.rows || [];
    mtFleetReportCount.textContent =
      `${rows.length} perangkat RouterOS · versi, firmware, uptime, dan temuan terbuka`;
    if (rows.length === 0) {
      mtFleetReportRows.innerHTML = '<tr class="empty-row"><td colspan="14">Belum ada perangkat MikroTik terdaftar. Tambahkan perangkat MikroTik di Inventaris.</td></tr>';
      return;
    }
    mtFleetReportRows.innerHTML = rows
      .map((row) => {
        const system = row.system || {};
        const firmware = MT_FIRMWARE_META[system.firmware_status || "unknown"] || MT_FIRMWARE_META.unknown;
        const version = system.version || row.routeros_version;
        const model = system.model || row.mikrotik_model;
        return (
          `<tr>` +
          `<td><a class="cell-link" href="mikrotik.html#perangkat-${row.id}-ringkasan">${escapeHtml(row.name)}</a>` +
          `<span class="cell-sub">${escapeHtml(row.location || "tanpa lokasi")}</span></td>` +
          `<td class="mono">${escapeHtml(row.ip)}</td>` +
          `<td>${escapeHtml(model || "-")}</td>` +
          `<td class="mono">${escapeHtml(version || "-")}</td>` +
          `<td class="mono">${escapeHtml(system.current_firmware || row.mikrotik_firmware || "-")}</td>` +
          `<td><span class="${firmware.cls}">${escapeHtml(firmware.label)}</span></td>` +
          `<td>${escapeHtml(formatUptime(row.uptime_s))}</td>` +
          `<td class="mono">${row.cpu_pct == null ? "-" : escapeHtml(formatPercent(row.cpu_pct, 0))}</td>` +
          `<td class="mono">${row.mem_pct == null ? "-" : escapeHtml(formatPercent(row.mem_pct, 0))}</td>` +
          `<td class="mono">${row.interfaces_total ? `${row.interfaces_up}/${row.interfaces_total}` : "-"}</td>` +
          `<td class="mono">${row.active_sessions || 0}</td>` +
          `<td class="mono">${row.security_open ? `<span class="avail avail--warn">${row.security_open}</span>` : "0"}</td>` +
          `<td class="mono">${row.open_problems ? `<span class="avail avail--down">${row.open_problems}</span>` : "0"}</td>` +
          `<td class="mono">${escapeHtml(row.metrics_ts ? formatDateTime(row.metrics_ts) : "-")}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  /* =====================================================
     Audit log (admin)
     ===================================================== */

  const auditRows = document.getElementById("audit-rows");
  const auditSearch = document.getElementById("audit-search");
  const auditCsv = document.getElementById("audit-csv");
  const auditPrev = document.getElementById("audit-prev");
  const auditNext = document.getElementById("audit-next");
  const auditPageInfo = document.getElementById("audit-page-info");
  const auditCount = document.getElementById("audit-count");

  const auditState = { page: 1, pages: 1, limit: 15 };

  async function loadAuditLog(resetPage) {
    if (resetPage) auditState.page = 1;
    const params = new URLSearchParams({ limit: String(auditState.limit), offset: String((auditState.page - 1) * auditState.limit) });
    if (auditSearch.value.trim()) params.set("search", auditSearch.value.trim());
    auditCsv.href = `/api/audit-log.csv${auditSearch.value.trim() ? `?search=${encodeURIComponent(auditSearch.value.trim())}` : ""}`;
    let data;
    try {
      data = await window.apiFetch(`/api/audit-log?${params.toString()}`);
    } catch (error) {
      auditRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat audit log: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const entries = data.entries || [];
    const total = data.total || 0;
    auditState.pages = Math.max(1, Math.ceil(total / auditState.limit));
    auditCount.textContent = `${total} catatan`;

    if (entries.length === 0) {
      auditRows.innerHTML = '<tr class="empty-row"><td colspan="6">Tidak ada catatan untuk pencarian ini.</td></tr>';
    } else {
      auditRows.innerHTML = entries
        .map((entry) => (
          `<tr>` +
          `<td class="mono">${escapeHtml(formatDateTime(entry.ts))}</td>` +
          `<td>${escapeHtml(entry.username)}</td>` +
          `<td class="mono">${escapeHtml(entry.ip)}</td>` +
          `<td class="mono">${escapeHtml(entry.action)}</td>` +
          `<td class="mono">${escapeHtml(entry.object)}</td>` +
          `<td>${escapeHtml(entry.summary)}</td>` +
          `</tr>`
        ))
        .join("");
    }
    auditPageInfo.textContent = `Halaman ${auditState.page} dari ${auditState.pages}`;
    auditPrev.disabled = auditState.page <= 1;
    auditNext.disabled = auditState.page >= auditState.pages;
  }

  auditSearch.addEventListener("input", debounce(() => loadAuditLog(true), 300));
  auditPrev.addEventListener("click", () => { if (auditState.page > 1) { auditState.page -= 1; loadAuditLog(false); } });
  auditNext.addEventListener("click", () => { if (auditState.page < auditState.pages) { auditState.page += 1; loadAuditLog(false); } });

  /* =====================================================
     Action log
     ===================================================== */

  const actionLogRows = document.getElementById("action-log-rows");
  const actionLogStatus = document.getElementById("action-log-status");

  const ACTION_STATUS = {
    sent: { label: "Terkirim", cls: "status status--ok" },
    failed: { label: "Gagal", cls: "status status--down" },
    skipped: { label: "Dilewati", cls: "status status--idle" },
  };

  async function loadActionLog() {
    const params = new URLSearchParams({ limit: "100" });
    if (actionLogStatus.value) params.set("status", actionLogStatus.value);
    let data;
    try {
      data = await window.apiFetch(`/api/action-log?${params.toString()}`);
    } catch (error) {
      actionLogRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat action log: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const entries = data.entries || [];
    if (entries.length === 0) {
      actionLogRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada pengiriman notifikasi. Atur aturan aksi di halaman Notifikasi agar pengiriman tercatat.</td></tr>';
      return;
    }
    actionLogRows.innerHTML = entries
      .map((entry) => {
        const status = ACTION_STATUS[entry.status] || { label: entry.status, cls: "status status--idle" };
        return (
          `<tr>` +
          `<td class="mono">${escapeHtml(formatDateTime(entry.ts))}</td>` +
          `<td>${escapeHtml(entry.rule_name || "-")}</td>` +
          `<td>${escapeHtml(entry.channel_name ? `${entry.channel_name} (${entry.channel_type || ""})` : "-")}</td>` +
          `<td>${escapeHtml(entry.device_name || "-")}</td>` +
          `<td><span class="${status.cls}">${escapeHtml(status.label)}</span></td>` +
          `<td>${escapeHtml(entry.detail)}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  actionLogStatus.addEventListener("change", loadActionLog);

  /* =====================================================
     Boot
     ===================================================== */

  whenReady(() => {
    if (!isAdmin()) {
      const auditNav = document.querySelector('[data-admin-only]');
      if (auditNav) auditNav.hidden = true;
    }
    activateView();
  });
})();
