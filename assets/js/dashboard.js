/* Dasbor: statistik + status perangkat + aktivitas terbaru (semua dari API). */
(function () {
  "use strict";

  const { escapeHtml, formatDateTime, timeAgo, STATUS_META, availabilityClass, formatPercent } = window.NMSFormat;

  const greetingName = document.getElementById("greeting-name");
  const statDevicesTotal = document.getElementById("stat-devices-total");
  const statDevicesMeta = document.getElementById("stat-devices-meta");
  const statProblemsTotal = document.getElementById("stat-problems-total");
  const statProblemsMeta = document.getElementById("stat-problems-meta");
  const statAvailability = document.getElementById("stat-availability");
  const statAvailabilityMeta = document.getElementById("stat-availability-meta");
  const statMaintenance = document.getElementById("stat-maintenance");
  const statFiber = document.getElementById("stat-fiber");
  const statFiberValue = document.getElementById("stat-fiber-value");
  const statFiberMeta = document.getElementById("stat-fiber-meta");
  const statMikrotik = document.getElementById("stat-mikrotik");
  const statMikrotikValue = document.getElementById("stat-mikrotik-value");
  const statMikrotikMeta = document.getElementById("stat-mikrotik-meta");
  const dashboardEmpty = document.getElementById("dashboard-empty");
  const dashboardGrid = document.getElementById("dashboard-grid");
  const dashboardError = document.getElementById("dashboard-error");
  const dashboardErrorText = document.getElementById("dashboard-error-text");
  const dashboardRetry = document.getElementById("dashboard-retry");
  const deviceRows = document.getElementById("device-rows");
  const activityList = document.getElementById("activity-list");

  const STATUS_WEIGHT = { offline: 0, unknown: 1, maintenance: 2, degraded: 3, online: 4 };
  const DASHBOARD_ROWS = 8;
  // Status yang diprioritaskan; diambil server-side agar perangkat offline
  // tidak terlewat saat armada lebih dari satu halaman (per_page maks. 100).
  const ATTENTION_STATUSES = ["offline", "degraded", "unknown", "maintenance"];

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  /**
   * Jenis aktivitas untuk ikon. Utamakan field kind/severity dari API bila
   * tersedia (belum ada, lihat BACKEND-TODO.md); fallback eksplisit dari
   * transisi old_status/new_status, bukan menebak isi pesan.
   */
  function activityKind(entry) {
    const raw = String(entry.kind || entry.severity || "").toLowerCase();
    if (["critical", "danger", "disaster", "high", "error"].includes(raw)) return "down";
    if (["warning", "warn", "average", "degraded"].includes(raw)) return "warn";
    if (["info", "information", "ok", "online"].includes(raw)) return "ok";
    const status = String(entry.new_status || "").toLowerCase();
    if (status === "offline") return "down";
    if (status === "degraded") return "warn";
    if (status === "maintenance") return "idle";
    return "ok";
  }

  async function fetchAttentionDevices() {
    const results = await Promise.all(
      ATTENTION_STATUSES.map((status) =>
        window.NMSStore.cached(`devices:dashboard:${status}`, () =>
          window.apiFetch(`/api/devices?status=${status}&per_page=100`)
        )
      )
    );
    const byId = new Map();
    results.forEach((data) => {
      (data.devices || []).forEach((device) => byId.set(device.id, device));
    });
    return [...byId.values()];
  }

  /**
   * Kegagalan muat: tampilkan banner + tombol coba lagi, bersihkan nilai lama
   * agar tidak terlihat seperti data terbaru, dan isi panel dengan pesan galat.
   */
  function renderDashboardError(error) {
    const message = (error && error.message) || "Tidak dapat menghubungi server.";
    if (dashboardError && dashboardErrorText) {
      dashboardErrorText.textContent = `Gagal memuat data dasbor: ${message}`;
      dashboardError.hidden = false;
    }
    if (dashboardEmpty) dashboardEmpty.hidden = true;

    statDevicesTotal.textContent = "-";
    statDevicesMeta.textContent = "Data tidak tersedia";
    statProblemsTotal.textContent = "-";
    statProblemsMeta.textContent = "Data tidak tersedia";
    statAvailability.textContent = "-";
    statAvailabilityMeta.textContent = "Data tidak tersedia";
    statMaintenance.textContent = "-";
    if (statFiber) statFiber.hidden = true;
    if (statMikrotik) statMikrotik.hidden = true;

    if (deviceRows) {
      deviceRows.innerHTML = `<tr class="empty-row"><td colspan="4">Gagal memuat perangkat: ${escapeHtml(message)}</td></tr>`;
    }
    if (activityList) {
      activityList.innerHTML = `<li class="empty-row"><div class="activity__body"><p>Gagal memuat aktivitas: ${escapeHtml(message)}</p></div></li>`;
    }
  }

  async function refresh() {
    const me = window.__nmsMe;
    if (me && greetingName) greetingName.textContent = me.username;

    let summary = null;
    let devices = [];
    let activity = [];
    let mtData = null;
    try {
      const [summaryData, activityData, mikrotikData] = await Promise.all([
        window.NMSStore.cached("summary", () => window.apiFetch("/api/summary")),
        window.NMSStore.cached("activity", () => window.apiFetch("/api/activity?limit=6")),
        window.NMSStore.cached("mikrotik:summary", () => window.apiFetch("/api/mikrotik/summary")).catch(() => null),
      ]);
      summary = summaryData;
      activity = activityData.activity || [];
      mtData = mikrotikData;

      devices = await fetchAttentionDevices();
      if (devices.length < DASHBOARD_ROWS) {
        const online = await window.NMSStore.cached("devices:dashboard:online", () =>
          window.apiFetch("/api/devices?status=online&per_page=100")
        );
        const seen = new Set(devices.map((device) => device.id));
        devices = devices.concat((online.devices || []).filter((device) => !seen.has(device.id)));
      }
    } catch (error) {
      renderDashboardError(error);
      return;
    }
    dashboardError.hidden = true;

    const total = summary.total || 0;
    const problemCount = summary.problems_active || 0;
    const highCount = summary.problems_high || 0;

    statDevicesTotal.textContent = String(total);
    statDevicesMeta.textContent = `${summary.online || 0} tersambung · ${(summary.degraded || 0) + (summary.offline || 0)} perlu perhatian`;
    statProblemsTotal.textContent = String(problemCount);
    statProblemsMeta.textContent = problemCount === 0 ? "Tidak ada masalah terbuka" : `${highCount} prioritas tinggi`;
    statAvailability.textContent = summary.availability_24h == null ? "-" : formatPercent(summary.availability_24h);
    statAvailabilityMeta.textContent = summary.availability_24h == null ? "Belum ada pemeriksaan" : "Rata-rata semua perangkat";
    statMaintenance.textContent = String(summary.maintenance || 0);

    // Kartu Fiber: hanya tampil bila ada link terdaftar.
    const fiber = summary.fiber || {};
    const fiberLinks = fiber.links_total || 0;
    if (statFiber) {
      if (fiberLinks > 0) {
        const warn = fiber.links_warn || 0;
        const crit = fiber.links_crit || 0;
        statFiber.hidden = false;
        statFiberValue.textContent = String(warn + crit);
        statFiberMeta.textContent =
          `${warn} waspada · ${crit} kritis · redaman maks ${fiber.max_attenuation_db == null ? "-" : `${Number(fiber.max_attenuation_db).toFixed(2)} dB`} · dari ${fiberLinks} link`;
      } else {
        statFiber.hidden = true;
      }
    }

    // Kartu MikroTik: hanya tampil bila ada perangkat RouterOS terdaftar.
    const mt = (mtData && mtData.summary) || {};
    const mtRouters = Number(mt.routers || 0);
    if (statMikrotik) {
      if (mtRouters > 0) {
        statMikrotik.hidden = false;
        statMikrotikValue.textContent = String(mtRouters);
        statMikrotikMeta.textContent =
          `${Number(mt.firmware_pending || 0)} firmware tertunda · ` +
          `${Number(mt.security_open || 0)} temuan keamanan · ` +
          `${Number(mt.active_sessions || 0)} sesi aktif`;
      } else {
        statMikrotik.hidden = true;
      }
    }

    const hasDevices = total > 0;
    dashboardEmpty.hidden = hasDevices;
    dashboardGrid.hidden = !hasDevices;

    if (hasDevices) {
      // Urutkan: offline → unknown → maintenance → degraded → online; maks 8 baris.
      const sorted = devices
        .slice()
        .sort((a, b) => (STATUS_WEIGHT[a.status] ?? 9) - (STATUS_WEIGHT[b.status] ?? 9) || a.name.localeCompare(b.name))
        .slice(0, DASHBOARD_ROWS);
      deviceRows.innerHTML = sorted.length
        ? sorted
            .map((device) => {
              const meta = STATUS_META[device.status] || STATUS_META.unknown;
              return (
                `<tr><td><strong>${escapeHtml(device.name)}</strong></td>` +
                `<td>${escapeHtml(device.location || "-")}</td>` +
                `<td class="mono">${escapeHtml(device.ip)}</td>` +
                `<td><span class="status ${escapeHtml(meta.cls)}">${escapeHtml(meta.label)}</span></td></tr>`
              );
            })
            .join("")
        : '<tr class="empty-row"><td colspan="4">Tidak ada perangkat untuk ditampilkan saat ini.</td></tr>';

      const items = activity || [];
      activityList.innerHTML = items.length
        ? items
            .map((entry) => {
              const kind = activityKind(entry);
              const dotClass = kind === "warn"
                ? " activity__dot--warn"
                : kind === "down"
                  ? " activity__dot--down"
                  : kind === "idle"
                    ? " activity__dot--idle"
                    : "";
              return (
                `<li><span class="activity__dot${dotClass}" aria-hidden="true"></span>` +
                `<div class="activity__body"><p>${escapeHtml(entry.message)}</p>` +
                `<time datetime="${escapeHtml(entry.created_at)}">${escapeHtml(timeAgo(entry.created_at))}</time></div></li>`
              );
            })
            .join("")
        : '<li class="empty-row"><div class="activity__body"><p>Belum ada aktivitas. Aktivitas muncul setelah poller memeriksa perangkat.</p></div></li>';
    }
  }

  async function loadPollerStatus() {
    const panel = document.getElementById("poller-panel");
    if (!panel) return;
    const me = window.__nmsMe || {};
    if (me.role !== "admin" && me.role !== "operator") {
      panel.hidden = true;
      return;
    }
    try {
      const s = await window.apiFetch("/api/poller/status");
      document.getElementById("poller-workers").textContent = String(s.workers);
      document.getElementById("poller-interval").textContent = `${s.interval} dtk`;
      document.getElementById("poller-last").textContent = s.last_check_at ? timeAgo(s.last_check_at) : "Belum ada";
      document.getElementById("poller-error").textContent = s.last_error || "Tidak ada";
      panel.hidden = false;
    } catch {
      panel.hidden = true;
    }
  }

  whenReady(() => {
    document.getElementById("dash-add-first")?.addEventListener("click", () => window.openDeviceForm());
    loadPollerStatus();
    if (dashboardRetry) {
      dashboardRetry.addEventListener("click", async () => {
        dashboardRetry.disabled = true;
        window.NMSStore.invalidate();
        try {
          await refresh();
        } finally {
          dashboardRetry.disabled = false;
        }
      });
    }
    window.addEventListener("nms:devices-changed", () => {
      window.NMSStore.invalidate();
      refresh();
    });
    refresh();
    window.NMSStore.startAutoRefresh(refresh, 15000);
  });
})();
