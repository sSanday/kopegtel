/* Halaman Modul MikroTik (M1–M5).
 * Semua nilai berasal dari API nyata; tanpa data tampil "—" / empty state.
 * Tidak ada aksi tulis di luar allowlist dan hanya tampil bila kredensial tulis aktif.
 */
(function () {
  "use strict";

  const fmt = window.NMSFormat;
  const escapeHtml = fmt.escapeHtml;
  const apiFetch = window.apiFetch;
  const formatUptime = fmt.formatUptime;
  const formatPercent = fmt.formatPercent;
  const formatBytes = fmt.formatBytes;
  const formatBps = fmt.formatBps;
  const timeAgo = fmt.timeAgo;
  const STATUS_META = fmt.STATUS_META;
  const SEVERITY_META = fmt.SEVERITY_META;
  // Gabungan severity RouterOS (info/warning/error/critical) dan severity masalah (disaster..information).
  const MT_SEVERITY_META = Object.assign(
    {
      critical: { cls: "sev sev--high", label: "Critical", rank: 5 },
      fatal: { cls: "sev sev--high", label: "Fatal", rank: 5 },
      error: { cls: "sev sev--average", label: "Error", rank: 4 },
      warning: { cls: "sev sev--warning", label: "Warning", rank: 2 },
      warn: { cls: "sev sev--warning", label: "Warning", rank: 2 },
      info: { cls: "sev sev--information", label: "Info", rank: 1 },
      informational: { cls: "sev sev--information", label: "Info", rank: 1 },
      low: { cls: "sev sev--information", label: "Low", rank: 0 },
    },
    SEVERITY_META
  );

  const TABS = [
    ["ringkasan", "Ringkasan"],
    ["interfaces", "Interface"],
    ["routing", "Routing"],
    ["dhcp", "DHCP & ARP"],
    ["sessions", "Sesi"],
    ["wireless", "Wireless"],
    ["firewall", "Firewall & Queue"],
    ["logs", "Log"],
    ["neighbors", "Neighbor"],
    ["security", "Keamanan"],
  ];
  const TAB_LABELS = Object.fromEntries(TABS);

  const STATE_LABELS = {
    api_disabled: "API MikroTik dinonaktifkan pada perangkat ini.",
    auth_failed: "Autentikasi RouterOS gagal.",
    no_permission: "Pengguna API tidak memiliki izin membaca bagian ini.",
    timeout: "RouterOS tidak menjawab dalam batas waktu.",
    unsupported: "Fitur ini tidak aktif atau tidak didukung di perangkat ini.",
    disabled: "Modul ini dinonaktifkan pada pengaturan perangkat.",
    unknown: "Menunggu poll MikroTik pertama.",
    error: "Terjadi kesalahan saat membaca RouterOS.",
    ok: "Data tersedia.",
  };

  const ACTION_LABELS = {
    disconnect_ppp: "Putuskan sesi PPP",
    disconnect_hotspot: "Putuskan sesi hotspot",
    address_list_add: "Tambah entri address-list",
    address_list_remove: "Hapus entri address-list",
    interface_enable: "Aktifkan interface",
    interface_disable: "Nonaktifkan interface",
    reboot: "Reboot perangkat",
  };

  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");
  const views = document.querySelectorAll(".view");
  const fleetRows = document.getElementById("mt-fleet-rows");
  const fleetCount = document.getElementById("mt-fleet-count");
  const fleetPrev = document.getElementById("mt-fleet-prev");
  const fleetNext = document.getElementById("mt-fleet-next");
  const fleetPageInfo = document.getElementById("mt-fleet-page-info");
  const topologyPanel = document.getElementById("mt-topology-panel");
  const topologyRows = document.getElementById("mt-topology-rows");
  const deviceTitle = document.getElementById("mt-device-title");
  const deviceSub = document.getElementById("mt-device-sub");
  const deviceStatus = document.getElementById("mt-device-status");
  const detailError = document.getElementById("mt-detail-error");
  const detailErrorText = document.getElementById("mt-detail-error-text");
  const tabsEl = document.getElementById("mt-tabs");
  const tabPanel = document.getElementById("mt-tab-panel");
  const tabToolbar = document.getElementById("mt-tab-toolbar");
  const tabSearch = document.getElementById("mt-tab-search");
  const logSeverityWrap = document.getElementById("mt-log-severity-wrap");
  const logSeverity = document.getElementById("mt-log-severity");
  const tabState = document.getElementById("mt-tab-state");
  const tabSections = document.getElementById("mt-tab-sections");
  const tabFoot = document.getElementById("mt-tab-foot");
  const tabPageInfo = document.getElementById("mt-tab-page-info");
  const tabPrev = document.getElementById("mt-tab-prev");
  const tabNext = document.getElementById("mt-tab-next");
  const pollButton = document.getElementById("mt-poll");

  const state = {
    fleet: { page: 1, pages: 1, search: "", status: "", firmware: "", version: "", model: "" },
    traffic: { range: "1h", selected: "" },
    sessions: { range: "24h" },
    detail: {
      id: null,
      tab: "ringkasan",
      page: 1,
      search: "",
      severity: "",
      overview: null,
      token: 0,
    },
    filterOptionsLoaded: false,
  };

  /* ---------- util ---------- */

  function role() {
    return (window.__nmsMe && window.__nmsMe.role) || "viewer";
  }
  function canOperate() {
    return role() === "admin" || role() === "operator";
  }
  function isAdmin() {
    return role() === "admin";
  }
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
  function text(value) {
    return value == null || value === "" ? "—" : escapeHtml(value);
  }
  function number(value, digits) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString("id-ID", {
      minimumFractionDigits: digits || 0,
      maximumFractionDigits: digits || 0,
    });
  }
  function percentCell(value, warn, crit) {
    if (value == null || Number.isNaN(Number(value))) return '<td class="mono">—</td>';
    const n = Number(value);
    const cls = n >= crit ? "avail avail--down" : n >= warn ? "avail avail--warn" : "avail avail--ok";
    return `<td><span class="${cls}" title="${escapeHtml(formatPercent(n, 0))}">${escapeHtml(formatPercent(n, 0))}</span></td>`;
  }
  function statusBadge(running, disabled) {
    if (disabled) return '<span class="avail avail--warn">Dinonaktifkan</span>';
    if (running) return '<span class="avail avail--ok">Running</span>';
    return '<span class="avail avail--down">Tidak aktif</span>';
  }
  function boolCell(value) {
    return `<td>${value ? "Ya" : "Tidak"}</td>`;
  }
  function emptyRow(colspan, message) {
    return `<tr class="empty-row"><td colspan="${colspan}">${escapeHtml(message)}</td></tr>`;
  }
  function showError(message) {
    detailErrorText.textContent = message || "Gagal memuat data MikroTik.";
    detailError.hidden = false;
  }
  function hideError() {
    detailError.hidden = true;
  }
  function toast(message) {
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.innerHTML = `<span class="toast__dot" aria-hidden="true"></span>${escapeHtml(message)}`;
    document.body.appendChild(el);
    requestAnimationFrame(() => el.classList.add("is-visible"));
    window.setTimeout(() => {
      el.classList.remove("is-visible");
      window.setTimeout(() => el.remove(), 250);
    }, 4600);
  }
  function firmwareMeta(statusValue) {
    if (statusValue === "pending") return { cls: "status status--warn", label: "Tertunda" };
    if (statusValue === "up-to-date") return { cls: "status status--ok", label: "Terbaru" };
    return { cls: "status status--idle", label: "Tidak diketahui" };
  }
  function sectionMeta(section) {
    const parts = [`${number(section.total)} baris`];
    if (section.truncated) parts.push("dipotong (maks 5000 baris)");
    if (section.stale_count) parts.push(`${number(section.stale_count)} baris basi`);
    if (section.polled_at) parts.push(`poll ${timeAgo(section.polled_at)}`);
    return parts.join(" · ");
  }
  function writeAvailable() {
    const actions = state.detail.overview && state.detail.overview.write_actions;
    return Boolean(actions && actions.available && canOperate());
  }

  /* ---------- routing ---------- */

  function parseHash() {
    const raw = window.location.hash.replace(/^#\/?/, "");
    const match = raw.match(/^perangkat-(\d+)(?:-([a-z-]+))?$/);
    if (match) {
      const tab = TAB_LABELS[match[2]] ? match[2] : "ringkasan";
      return { view: "perangkat", id: Number(match[1]), tab };
    }
    return { view: "armada" };
  }

  function setHash(value) {
    if (window.location.hash === value) {
      activateView();
      return;
    }
    window.location.hash = value;
  }

  async function activateView() {
    const route = parseHash();
    views.forEach((section) => {
      section.hidden = section.getAttribute("data-view") !== route.view;
    });
    if (route.view === "armada") {
      viewTitle.textContent = "Armada";
      viewDesc.textContent = "Perangkat RouterOS terdaftar: versi, firmware, beban, sesi, dan masalah.";
      document.title = "MikroTik · NMS Kopegtel";
      loadSummary();
      loadFleet();
      loadTopology();
      return;
    }
    state.detail.tab = route.tab;
    const changed = state.detail.id !== route.id;
    state.detail.id = route.id;
    if (changed) {
      state.detail.page = 1;
      state.detail.search = "";
      state.detail.severity = "";
      state.detail.overview = null;
      tabSearch.value = "";
      logSeverity.value = "";
    }
    await ensureOverview();
    if (!state.detail.overview) return;
    renderDeviceHeader();
    renderTabs();
    if (route.tab === "ringkasan") {
      renderRingkasan();
    } else {
      loadTab();
    }
  }

  window.addEventListener("hashchange", activateView);
  document.getElementById("refresh").addEventListener("click", () => {
    if (parseHash().view === "perangkat") {
      state.detail.overview = null;
      activateView();
    } else {
      loadSummary();
      loadFleet();
      loadTopology();
    }
  });

  /* ---------- Armada ---------- */

  async function loadSummary() {
    try {
      const data = await apiFetch("/api/mikrotik/summary");
      const summary = data.summary || {};
      document.getElementById("mt-router-count").textContent = String(summary.routers ?? 0);
      document.getElementById("mt-firmware-pending").textContent = String(summary.firmware_pending ?? 0);
      document.getElementById("mt-security-open").textContent = String(summary.security_open ?? 0);
      document.getElementById("mt-active-sessions").textContent = String(summary.active_sessions ?? 0);
    } catch {
      ["mt-router-count", "mt-firmware-pending", "mt-security-open", "mt-active-sessions"].forEach((id) => {
        document.getElementById(id).textContent = "—";
      });
    }
  }

  async function loadFilterOptions() {
    if (state.filterOptionsLoaded) return;
    try {
      const data = await apiFetch("/api/mikrotik/filter-options");
      const version = document.getElementById("mt-version");
      const model = document.getElementById("mt-model");
      version.innerHTML =
        '<option value="">Semua versi</option>' +
        (data.versions || []).map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("");
      model.innerHTML =
        '<option value="">Semua model</option>' +
        (data.models || []).map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("");
      state.filterOptionsLoaded = true;
    } catch {
      /* filter opsional */
    }
  }

  async function loadFleet() {
    const params = new URLSearchParams({ page: String(state.fleet.page), per_page: "25" });
    if (state.fleet.search) params.set("search", state.fleet.search);
    if (state.fleet.status) params.set("status", state.fleet.status);
    if (state.fleet.firmware) params.set("firmware", state.fleet.firmware);
    if (state.fleet.version) params.set("version", state.fleet.version);
    if (state.fleet.model) params.set("model", state.fleet.model);
    fleetCount.textContent = "Memuat perangkat…";
    try {
      const data = await apiFetch(`/api/mikrotik/devices?${params.toString()}`);
      renderFleet(data);
      loadFilterOptions();
    } catch (error) {
      fleetRows.innerHTML = emptyRow(11, `Gagal memuat armada: ${error.message}`);
      fleetCount.textContent = "Gagal memuat";
      fleetPrev.disabled = true;
      fleetNext.disabled = true;
      fleetPageInfo.textContent = "Halaman —";
    }
  }

  function renderFleet(data) {
    const devices = data.devices || [];
    fleetCount.textContent =
      `${number(data.total)} perangkat MikroTik` +
      (state.fleet.firmware ? ` · firmware ${state.fleet.firmware}` : "");
    if (!devices.length) {
      fleetRows.innerHTML = emptyRow(11, "Tidak ada perangkat MikroTik yang cocok dengan filter saat ini.");
    } else {
      fleetRows.innerHTML = devices
        .map((device) => {
          const system = device.system || {};
          const meta = STATUS_META[device.status] || STATUS_META.unknown;
          const firmware = firmwareMeta(device.firmware_status);
          const model = system.model || device.mikrotik_model;
          const version = system.version || device.routeros_version;
          const iface = device.interfaces_total
            ? `${number(device.interfaces_up)}/${number(device.interfaces_total)}`
            : "—";
          const problems = device.open_problems || 0;
          return (
            `<tr>` +
            `<td><a class="cell-link" href="#perangkat-${device.id}-ringkasan">${escapeHtml(device.name)}</a>` +
            `<span class="cell-sub"><span class="status ${escapeHtml(meta.cls)}">${escapeHtml(meta.label)}</span> · ${escapeHtml(device.location || "tanpa lokasi")}</span></td>` +
            `<td class="mono">${escapeHtml(device.ip)}</td>` +
            `<td>${text(model)}</td>` +
            `<td class="mono">${text(version)}</td>` +
            `<td><span class="${firmware.cls}">${escapeHtml(firmware.label)}</span>` +
            (system.upgrade_firmware ? `<span class="cell-sub">→ ${escapeHtml(system.upgrade_firmware)}</span>` : "") +
            `</td>` +
            `<td>${text(device.uptime_s == null ? null : formatUptime(device.uptime_s))}</td>` +
            percentCell(device.cpu_pct, 70, 90) +
            percentCell(device.mem_pct, 70, 90) +
            `<td class="mono">${iface}</td>` +
            `<td class="mono">${number(device.active_sessions)}</td>` +
            `<td>${problems > 0 ? `<span class="avail avail--down">${number(problems)}</span>` : '<span class="avail">0</span>'}</td>` +
            `</tr>`
          );
        })
        .join("");
    }
    state.fleet.pages = data.pages || 1;
    fleetPageInfo.textContent = `Halaman ${data.page || 1} dari ${state.fleet.pages}`;
    fleetPrev.disabled = (data.page || 1) <= 1;
    fleetNext.disabled = (data.page || 1) >= state.fleet.pages;
  }

  document.getElementById("mt-search").addEventListener(
    "input",
    debounce((event) => {
      state.fleet.search = event.target.value.trim();
      state.fleet.page = 1;
      loadFleet();
    }, 300)
  );
  for (const [id, key] of [["mt-status", "status"], ["mt-firmware", "firmware"], ["mt-version", "version"], ["mt-model", "model"]]) {
    document.getElementById(id).addEventListener("change", (event) => {
      state.fleet[key] = event.target.value;
      state.fleet.page = 1;
      loadFleet();
    });
  }
  fleetPrev.addEventListener("click", () => {
    if (state.fleet.page > 1) {
      state.fleet.page -= 1;
      loadFleet();
    }
  });
  fleetNext.addEventListener("click", () => {
    if (state.fleet.page < state.fleet.pages) {
      state.fleet.page += 1;
      loadFleet();
    }
  });

  /* ---------- Saran topologi ---------- */

  async function loadTopology() {
    if (!canOperate()) {
      topologyPanel.hidden = true;
      return;
    }
    try {
      const data = await apiFetch("/api/mikrotik/topology/suggestions");
      const suggestions = data.suggestions || [];
      if (!suggestions.length) {
        topologyPanel.hidden = true;
        return;
      }
      topologyPanel.hidden = false;
      topologyRows.innerHTML = suggestions
        .map(
          (item) =>
            `<tr>` +
            `<td><strong>${escapeHtml(item.child_name)}</strong><span class="cell-sub mono">${escapeHtml(item.child_ip)}</span></td>` +
            `<td><strong>${escapeHtml(item.parent_name)}</strong><span class="cell-sub mono">${escapeHtml(item.parent_ip)}</span>` +
            (item.current_parent_name ? `<span class="cell-sub">sekarang: ${escapeHtml(item.current_parent_name)}</span>` : "") +
            `</td>` +
            `<td><span class="status ${item.uncertain ? "status--warn" : "status--ok"}">${escapeHtml(item.confidence)}</span></td>` +
            `<td>${escapeHtml(item.reason)}</td>` +
            `<td><button class="btn btn--sm btn--ghost" type="button" data-topology="apply" data-child="${item.child_id}" data-parent="${item.parent_id}">Terapkan</button></td>` +
            `</tr>`
        )
        .join("");
    } catch {
      topologyPanel.hidden = true;
    }
  }

  topologyRows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-topology]");
    if (!button) return;
    const child = Number(button.dataset.child);
    const parent = Number(button.dataset.parent);
    const childName = button.closest("tr").querySelector("td strong").textContent;
    if (!window.confirm(`Jadikan perangkat induk dari ${childName}? Relasi ini hanya saran dari neighbor, bukan konfirmasi fisik.`)) return;
    button.disabled = true;
    try {
      const result = await apiFetch("/api/mikrotik/topology/apply", {
        method: "POST",
        body: { child_id: child, parent_id: parent },
      });
      toast(result.message || "Relasi topologi diterapkan.");
      loadTopology();
      loadFleet();
    } catch (error) {
      alert(error.message || "Gagal menerapkan relasi topologi.");
    } finally {
      button.disabled = false;
    }
  });

  /* ---------- Detail: overview ---------- */

  async function ensureOverview() {
    if (state.detail.overview && state.detail.overview.device && state.detail.overview.device.id === state.detail.id) {
      return;
    }
    hideError();
    try {
      const data = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/overview`);
      state.detail.overview = data;
    } catch (error) {
      state.detail.overview = null;
      showError(error.message || "Perangkat MikroTik tidak ditemukan.");
      deviceTitle.textContent = "Detail perangkat";
      deviceSub.textContent = "—";
      tabsEl.innerHTML = "";
      tabSections.innerHTML = "";
      tabState.hidden = true;
      tabToolbar.hidden = true;
      tabFoot.hidden = true;
    }
  }

  function renderDeviceHeader() {
    const data = state.detail.overview;
    const device = data.device;
    const system = data.system || {};
    const meta = STATUS_META[device.status] || STATUS_META.unknown;
    viewTitle.textContent = device.name;
    viewDesc.textContent = `${device.ip} · ${system.model || device.mikrotik_model || "model —"} · RouterOS ${system.version || device.routeros_version || "—"}`;
    document.title = `${device.name} · MikroTik · NMS Kopegtel`;
    deviceTitle.textContent = device.name;
    deviceSub.textContent =
      `${device.ip} · ${device.location || "tanpa lokasi"} · ` +
      `poll terakhir ${data.poll_state && data.poll_state.length ? timeAgo(maxPoll(data.poll_state)) : "—"}`;
    deviceStatus.textContent = meta.label;
    deviceStatus.className = `status ${meta.cls}`;
    pollButton.hidden = !canOperate() || !device.mikrotik_enabled;
  }

  function maxPoll(pollState) {
    const times = pollState.map((item) => item.last_run).filter(Boolean).sort();
    return times.length ? times[times.length - 1] : null;
  }

  function renderTabs() {
    const tabs = state.detail.overview.tabs || {};
    tabsEl.innerHTML = TABS.map(([key, label]) => {
      const info = tabs[key] || { state: "unknown", detail: STATE_LABELS.unknown };
      const active = key === state.detail.tab;
      const bad = info.state !== "ok" && key !== "ringkasan";
      return (
        `<button class="mt-tab${active ? " is-active" : ""}${bad ? " is-limited" : ""}" type="button" role="tab"` +
        ` aria-selected="${active}" data-tab="${key}" title="${escapeHtml(info.detail || STATE_LABELS[info.state] || "")}">` +
        `${escapeHtml(label)}<span class="mt-tab__dot" aria-hidden="true"></span></button>`
      );
    }).join("");
    tabPanel.setAttribute("aria-labelledby", "mt-tabs");
  }

  tabsEl.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    const tab = button.dataset.tab;
    state.detail.page = 1;
    state.detail.search = "";
    state.detail.severity = "";
    tabSearch.value = "";
    logSeverity.value = "";
    setHash(`#perangkat-${state.detail.id}-${tab}`);
  });

  pollButton.addEventListener("click", async () => {
    if (!canOperate()) return;
    pollButton.disabled = true;
    try {
      await apiFetch(`/api/mikrotik/devices/${state.detail.id}/refresh`, { method: "POST" });
      toast("Poll MikroTik selesai. Data dimuat ulang.");
      state.detail.overview = null;
      await ensureOverview();
      if (state.detail.overview) {
        renderDeviceHeader();
        renderTabs();
        if (state.detail.tab === "ringkasan") renderRingkasan();
        else loadTab();
      }
    } catch (error) {
      alert(error.message || "Gagal menjalankan poll MikroTik.");
    } finally {
      pollButton.disabled = false;
    }
  });

  document.getElementById("mt-detail-retry").addEventListener("click", () => {
    state.detail.overview = null;
    activateView();
  });

  /* ---------- Detail: tab non-ringkasan ---------- */

  async function loadTab() {
    const tab = state.detail.tab;
    if (!TAB_LABELS[tab] || tab === "ringkasan") return;
    const token = ++state.detail.token;
    hideError();
    tabToolbar.hidden = false;
    logSeverityWrap.hidden = tab !== "logs";
    tabState.hidden = true;
    tabFoot.hidden = true;
    tabSections.innerHTML = '<p class="mt-loading">Memuat data…</p>';
    const params = new URLSearchParams({ page: String(state.detail.page), per_page: "50" });
    if (state.detail.search) params.set("search", state.detail.search);
    if (tab === "logs" && state.detail.severity) params.set("severity", state.detail.severity);
    try {
      const data = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/tab/${tab}?${params.toString()}`);
      if (token !== state.detail.token) return;
      renderTabData(data);
    } catch (error) {
      if (token !== state.detail.token) return;
      tabSections.innerHTML = "";
      tabState.hidden = false;
      tabState.textContent = error.message || "Gagal memuat tab.";
    }
  }

  function renderTabData(data) {
    const info = data.state || {};
    tabState.className = "mt-tab-state";
    if (info.state !== "ok") {
      tabState.hidden = false;
      tabState.classList.add("is-warn");
      tabState.innerHTML =
        `<strong>${escapeHtml(STATE_LABELS[info.state] || info.reason || "Tidak tersedia")}</strong>` +
        (info.detail ? `<span>${escapeHtml(info.detail)}</span>` : "") +
        (info.probes && info.probes.length
          ? `<span class="mt-tab-state__detail">${info.probes
              .map((probe) => `${escapeHtml(probe.probe)}: ${escapeHtml(probe.detail || probe.reason || "gagal")}`)
              .join(" · ")}</span>`
          : "");
      tabSections.innerHTML = "";
      tabFoot.hidden = true;
      return;
    }
    tabState.hidden = true;
    const sections = data.sections || [];
    if (!sections.length) {
      tabSections.innerHTML = '<p class="empty-state">Belum ada data untuk bagian ini.</p>';
      tabFoot.hidden = true;
      return;
    }
    tabSections.innerHTML = "";
    for (const section of sections) {
      tabSections.appendChild(buildSection(section));
    }
    if (state.detail.tab === "firewall" && writeAvailable()) {
      tabSections.appendChild(buildAddressListForm());
    }
    if (state.detail.tab === "interfaces") {
      tabSections.appendChild(buildTrafficPanel());
    }
    if (state.detail.tab === "sessions") {
      tabSections.appendChild(buildSessionsPanel());
    }
    const pages = Math.max(...sections.map((section) => section.pages || 1), 1);
    const page = sections[0] ? sections[0].page || 1 : 1;
    tabFoot.hidden = pages <= 1;
    tabPageInfo.textContent = `Halaman ${page} dari ${pages}`;
    tabPrev.disabled = page <= 1;
    tabNext.disabled = page >= pages;
  }

  tabSearch.addEventListener(
    "input",
    debounce((event) => {
      state.detail.search = event.target.value.trim();
      state.detail.page = 1;
      loadTab();
    }, 300)
  );
  logSeverity.addEventListener("change", (event) => {
    state.detail.severity = event.target.value;
    state.detail.page = 1;
    loadTab();
  });
  tabPrev.addEventListener("click", () => {
    if (state.detail.page > 1) {
      state.detail.page -= 1;
      loadTab();
    }
  });
  tabNext.addEventListener("click", () => {
    state.detail.page += 1;
    loadTab();
  });

  /* ---------- Rendering tabel section ---------- */

  function buildSection(section) {
    const panel = document.createElement("section");
    panel.className = "panel panel--nested mt-section";
    const head = document.createElement("div");
    head.className = "panel__head panel__head--stack";
    head.innerHTML =
      `<div><h3 class="panel__title">${escapeHtml(section.label)}</h3>` +
      `<p class="panel__sub">${escapeHtml(sectionMeta(section))}</p></div>`;
    panel.appendChild(head);

    if (section.redacted) {
      const note = document.createElement("p");
      note.className = "note-info note-info--sm";
      const summaryParts = Object.entries(section.summary || {}).map(
        ([key, value]) => `${escapeHtml(key)}: ${number(value)}`
      );
      note.innerHTML =
        `Detail pelanggan disembunyikan untuk peran viewer. Total ${number(section.total)} baris` +
        (summaryParts.length ? ` · ${summaryParts.join(" · ")}` : "") +
        ".";
      panel.appendChild(note);
      return panel;
    }

    const table = document.createElement("div");
    table.className = "table-wrap";
    const tableData = sectionTable(section);
    table.innerHTML =
      `<table class="mon-table"><thead><tr>${tableData.headers
        .map((header) => `<th scope="col">${escapeHtml(header)}</th>`)
        .join("")}</tr></thead><tbody>${tableData.rows.join("")}</tbody></table>`;
    panel.appendChild(table);
    return panel;
  }

  function sectionTable(section) {
    const rows = section.rows || [];
    const empty = (colspan, message) => [emptyRow(colspan, message)];

    switch (section.key) {
      case "mt_interfaces": {
        const headers = ["Interface", "Tipe", "Status", "MTU", "MAC", "RX", "TX", "Error", "Aksi"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada interface pada snapshot.") };
        return {
          headers,
          rows: rows.map((row) => {
            const actions = [];
            if (canOperate() && row.name) {
              actions.push(
                row.important
                  ? `<button class="btn btn--sm btn--ghost" type="button" data-mt-important="remove" data-name="${escapeHtml(row.name)}">Penting ✓</button>`
                  : `<button class="btn btn--sm btn--ghost" type="button" data-mt-important="add" data-name="${escapeHtml(row.name)}">Jadikan penting</button>`
              );
              if (isAdmin() && writeAvailable()) {
                actions.push(
                  row.disabled
                    ? `<button class="btn btn--sm btn--ghost" type="button" data-mt-action="interface_enable" data-target="${escapeHtml(row.name)}">Aktifkan</button>`
                    : `<button class="btn btn--sm btn--ghost" type="button" data-mt-action="interface_disable" data-target="${escapeHtml(row.name)}" data-typed="1">Nonaktifkan</button>`
                );
              }
            }
            const action = actions.length ? `<span class="admin-actions">${actions.join(" ")}</span>` : "—";
            return (
              `<tr><td><strong>${text(row.name)}</strong>${row.important ? '<span class="cell-sub">interface penting</span>' : ""}</td>` +
              `<td>${text(row.type)}</td>` +
              `<td>${statusBadge(row.running, row.disabled)}</td>` +
              `<td class="mono">${text(row.mtu)}</td>` +
              `<td class="mono">${text(row.mac)}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.rx_byte))}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.tx_byte))}</td>` +
              `<td class="mono">${number(row.rx_error || 0)} / ${number(row.tx_error || 0)}</td>` +
              `<td>${action}</td></tr>`
            );
          }),
        };
      }
      case "mt_ip_addresses": {
        const headers = ["Interface", "Alamat", "Network", "Dinamis", "Disabled"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada alamat IP pada snapshot.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td>${text(row.interface)}</td><td class="mono">${text(row.address)}</td>` +
              `<td class="mono">${text(row.network)}</td>` + boolCell(row.dynamic) + boolCell(row.disabled) + `</tr>`
          ),
        };
      }
      case "mt_routes": {
        const headers = ["Keluarga", "Tujuan", "Gateway", "Distance", "Aktif", "Dinamis", "Statis", "Disabled"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada rute pada snapshot.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td>${text(row.family)}</td><td class="mono">${text(row.dst_address)}</td>` +
              `<td class="mono">${text(row.gateway)}</td><td class="mono">${text(row.distance)}</td>` +
              boolCell(row.active) + boolCell(row.dynamic) + boolCell(row.static) + boolCell(row.disabled) +
              `</tr>`
          ),
        };
      }
      case "mt_routing_neighbors": {
        const headers = ["Protokol", "Peer", "State", "Alamat", "Interface", "Uptime", "Prefix"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada neighbor routing (BGP/OSPF).") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td>${text(row.protocol)}</td><td>${text(row.name || row.peer_id)}</td>` +
              `<td>${text(row.state)}</td><td class="mono">${text(row.remote_address || row.address)}</td>` +
              `<td>${text(row.interface)}</td><td>${text(row.uptime || row.dead_time)}</td>` +
              `<td class="mono">${text(row.prefix_count)}</td></tr>`
          ),
        };
      }
      case "mt_dhcp_pools": {
        const headers = ["Pool", "Rentang", "Kapasitas", "Terpakai", "Pemakaian"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada DHCP pool.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td><strong>${text(row.name)}</strong></td><td class="mono">${text(row.ranges)}</td>` +
              `<td class="mono">${text(row.capacity)}</td><td class="mono">${text(row.used)}</td>` +
              `<td>${row.usage_pct == null ? "—" : `<span class="${row.usage_pct >= 90 ? "avail avail--down" : row.usage_pct >= 75 ? "avail avail--warn" : "avail avail--ok"}">${escapeHtml(formatPercent(row.usage_pct, 1))}</span>`}</td></tr>`
          ),
        };
      }
      case "mt_dhcp_leases": {
        const headers = ["Alamat", "MAC", "Host", "Server", "Status", "Kedaluwarsa", "Dinamis", "Disabled"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada DHCP lease.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td class="mono">${text(row.address)}</td><td class="mono">${text(row.mac)}</td>` +
              `<td>${text(row.host_name)}</td><td>${text(row.server)}</td><td>${text(row.status)}</td>` +
              `<td>${text(row.expires_after || row.last_seen)}</td>` + boolCell(row.dynamic) + boolCell(row.disabled) +
              `</tr>`
          ),
        };
      }
      case "mt_arp": {
        const headers = ["Alamat", "MAC", "Interface", "Complete", "Dinamis", "Invalid"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada entri ARP.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td class="mono">${text(row.address)}</td><td class="mono">${text(row.mac)}</td>` +
              `<td>${text(row.interface)}</td>` + boolCell(row.complete) + boolCell(row.dynamic) + boolCell(row.invalid) + `</tr>`
          ),
        };
      }
      case "mt_ppp_sessions": {
        const headers = ["Pengguna", "Service", "Caller ID", "Alamat", "Uptime", "RX", "TX", "Aksi"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Tidak ada sesi PPP aktif.") };
        return {
          headers,
          rows: rows.map((row) => {
            const canDisconnect = writeAvailable() && row.router_id;
            return (
              `<tr><td><strong>${text(row.name)}</strong></td><td>${text(row.service)}</td>` +
              `<td class="mono">${text(row.caller_id)}</td><td class="mono">${text(row.address)}</td>` +
              `<td>${text(row.uptime)}</td><td class="mono">${escapeHtml(formatBytes(row.bytes_in))}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.bytes_out))}</td>` +
              `<td>${canDisconnect ? `<button class="btn btn--sm btn--ghost" type="button" data-mt-action="disconnect_ppp" data-target="${escapeHtml(row.router_id)}" data-label="${escapeHtml(row.name || row.router_id)}">Putuskan</button>` : "—"}</td></tr>`
            );
          }),
        };
      }
      case "mt_hotspot_sessions": {
        const headers = ["Pengguna", "Alamat", "MAC", "Server", "Uptime", "RX", "TX", "Aksi"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Tidak ada sesi hotspot aktif.") };
        return {
          headers,
          rows: rows.map((row) => {
            const canDisconnect = writeAvailable() && row.router_id;
            return (
              `<tr><td><strong>${text(row.user)}</strong></td><td class="mono">${text(row.address)}</td>` +
              `<td class="mono">${text(row.mac)}</td><td>${text(row.server)}</td><td>${text(row.uptime)}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.bytes_in))}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.bytes_out))}</td>` +
              `<td>${canDisconnect ? `<button class="btn btn--sm btn--ghost" type="button" data-mt-action="disconnect_hotspot" data-target="${escapeHtml(row.router_id)}" data-label="${escapeHtml(row.user || row.router_id)}">Putuskan</button>` : "—"}</td></tr>`
            );
          }),
        };
      }
      case "mt_wireless_clients": {
        const headers = ["Interface", "MAC", "Sinyal", "Tx rate", "Rx rate", "Uptime", "RX", "TX"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Tidak ada klien wireless terdaftar.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td>${text(row.interface)}</td><td class="mono">${text(row.mac)}</td>` +
              `<td class="mono">${text(row.signal_strength || (row.signal_dbm == null ? null : `${row.signal_dbm} dBm`))}</td>` +
              `<td>${text(row.tx_rate)}</td><td>${text(row.rx_rate)}</td><td>${text(row.uptime)}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.bytes_in))}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.bytes_out))}</td></tr>`
          ),
        };
      }
      case "mt_firewall_rules": {
        const headers = ["Jenis", "Chain", "Aksi", "Komentar", "Paket", "Byte", "Disabled"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada aturan firewall.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td>${text(row.kind)}</td><td>${text(row.chain)}</td><td>${text(row.action)}</td>` +
              `<td>${text(row.comment)}</td><td class="mono">${number(row.packets)}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.bytes))}</td>` + boolCell(row.disabled) + `</tr>`
          ),
        };
      }
      case "mt_address_lists": {
        const headers = ["List", "Alamat", "Timeout", "Komentar", "Dinamis", "Aksi"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada entri address-list.") };
        return {
          headers,
          rows: rows.map((row) => {
            const canRemove = writeAvailable() && row.router_id;
            return (
              `<tr><td>${text(row.list)}</td><td class="mono">${text(row.address)}</td>` +
              `<td>${text(row.timeout)}</td><td>${text(row.comment)}</td>` + boolCell(row.dynamic) +
              `<td>${canRemove ? `<button class="btn btn--sm btn--ghost" type="button" data-mt-action="address_list_remove" data-target="${escapeHtml(row.router_id)}" data-label="${escapeHtml(`${row.list || "?"}:${row.address || "?"}`)}">Hapus</button>` : "—"}</td></tr>`
            );
          }),
        };
      }
      case "mt_queues": {
        const headers = ["Jenis", "Nama", "Target", "Max limit", "Rate", "Byte", "Dropped", "Disabled"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada queue.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td>${text(row.kind)}</td><td><strong>${text(row.name)}</strong></td>` +
              `<td class="mono">${text(row.target)}</td><td>${text(row.max_limit)}</td><td>${text(row.rate)}</td>` +
              `<td class="mono">${escapeHtml(formatBytes(row.bytes))}</td><td class="mono">${number(row.dropped)}</td>` +
              boolCell(row.disabled) + `</tr>`
          ),
        };
      }
      case "mt_logs": {
        const headers = ["Waktu", "Severity", "Topik", "Pesan", "Pengguna", "Sumber IP", "Layanan"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada log yang tersimpan.") };
        return {
          headers,
          rows: rows.map((row) => {
            const sev = MT_SEVERITY_META[row.severity] || MT_SEVERITY_META.informational;
            return (
              `<tr><td class="mono">${text(row.log_time || row.polled_at)}</td>` +
              `<td><span class="${escapeHtml(sev.cls)}">${escapeHtml(sev.label)}</span></td>` +
              `<td>${text(row.topics)}</td><td>${text(row.message)}</td>` +
              `<td>${text(row.username)}${row.is_login_failure ? '<span class="cell-sub">login gagal</span>' : ""}</td>` +
              `<td class="mono">${text(row.source_ip)}</td><td>${text(row.service)}</td></tr>`
            );
          }),
        };
      }
      case "mt_neighbors": {
        const headers = ["Identitas", "Platform", "Versi", "Alamat", "MAC", "Interface", "Board"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada neighbor MNDP/LLDP.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td><strong>${text(row.identity)}</strong></td><td>${text(row.platform)}</td>` +
              `<td class="mono">${text(row.version)}</td><td class="mono">${text(row.address || row.address4 || row.address6)}</td>` +
              `<td class="mono">${text(row.mac)}</td><td>${text(row.interface)}</td><td>${text(row.board)}</td></tr>`
          ),
        };
      }
      case "mt_security_findings": {
        const headers = ["Severity", "Temuan", "Kategori", "Detail", "Saran perbaikan", "Status", "Aksi"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Tidak ada temuan keamanan aktif.") };
        return {
          headers,
          rows: rows.map((row) => {
            const sev = MT_SEVERITY_META[row.severity] || MT_SEVERITY_META.informational;
            const ignored = Boolean(row.ignored);
            const canIgnore = canOperate();
            return (
              `<tr><td><span class="${escapeHtml(sev.cls)}">${escapeHtml(sev.label)}</span></td>` +
              `<td><strong>${text(row.title)}</strong></td><td>${text(row.category)}</td>` +
              `<td>${text(row.detail)}</td><td>${text(row.remediation)}</td>` +
              `<td>${ignored ? `<span class="status status--idle">Diabaikan</span><span class="cell-sub">${text(row.ignore_reason)}</span>` : '<span class="status status--warn">Terbuka</span>'}</td>` +
              `<td>${canIgnore ? `<button class="btn btn--sm btn--ghost" type="button" data-mt-action="${ignored ? "finding_unignore" : "finding_ignore"}" data-target="${row.id}">${ignored ? "Aktifkan" : "Abaikan"}</button>` : "—"}</td></tr>`
            );
          }),
        };
      }
      case "mt_services": {
        const headers = ["Layanan", "Port", "Batas alamat", "Disabled", "Invalid"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada data layanan router.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td><strong>${text(row.name)}</strong></td><td class="mono">${text(row.port)}</td>` +
              `<td class="mono">${text(row.address || "semua alamat")}</td>` + boolCell(row.disabled) + boolCell(row.invalid) + `</tr>`
          ),
        };
      }
      case "mt_users": {
        const headers = ["Pengguna", "Grup", "Batas alamat", "Terakhir login", "Disabled"];
        if (!rows.length) return { headers, rows: empty(headers.length, "Belum ada data pengguna router.") };
        return {
          headers,
          rows: rows.map(
            (row) =>
              `<tr><td><strong>${text(row.name)}</strong></td><td>${text(row.group)}</td>` +
              `<td class="mono">${text(row.address || "semua alamat")}</td><td>${text(row.last_logged_in)}</td>` +
              boolCell(row.disabled) + `</tr>`
          ),
        };
      }
      default: {
        const headers = ["Data"];
        return {
          headers,
          rows: rows.length
            ? rows.map((row) => `<tr><td><pre class="mt-json">${escapeHtml(JSON.stringify(row, null, 2))}</pre></td></tr>`)
            : empty(1, "Belum ada data."),
        };
      }
    }
  }

  /* ---------- Detail: Ringkasan ---------- */

  function renderRingkasan() {
    const data = state.detail.overview;
    const device = data.device;
    const system = data.system || {};
    const counts = data.counts || {};
    const settings = data.settings || {};
    const write = data.write_actions || {};
    const findings = data.findings || {};
    tabToolbar.hidden = true;
    tabState.hidden = true;
    tabFoot.hidden = true;
    hideError();

    const cards = [
      ["RouterOS", text(system.version || device.routeros_version)],
      ["Model", text(system.model || device.mikrotik_model)],
      ["Uptime", text(device.uptime_s == null ? null : formatUptime(device.uptime_s))],
      ["CPU", device.cpu_pct == null ? "—" : formatPercent(device.cpu_pct, 0)],
      ["Memori", device.mem_pct == null ? "—" : formatPercent(device.mem_pct, 0)],
      ["Sesi aktif", number(counts.active_sessions)],
    ];
    const cardHtml = cards
      .map(
        ([label, value]) =>
          `<article class="metric-card"><p class="metric-card__label">${escapeHtml(label)}</p>` +
          `<p class="metric-card__value metric-card__value--text">${value}</p>` +
          `<p class="metric-card__sub"><span class="metric-card__detail">snapshot terakhir</span></p></article>`
      )
      .join("");

    const firmware = firmwareMeta(system.firmware_status);
    const packageList = (system.packages || []).map((item) => `${item.name} ${item.version || ""}`.trim()).join(", ");
    const infoItems = [
      ["Identitas", system.identity],
      ["Board / arsitektur", [system.board, system.architecture].filter(Boolean).join(" / ")],
      ["Serial", system.serial],
      ["Lisensi", system.license_level],
      ["Firmware sekarang", system.current_firmware],
      ["Firmware tersedia", system.upgrade_firmware],
      ["Status firmware", firmware.label],
      ["Jam router", [system.clock_date, system.clock_time].filter(Boolean).join(" ")],
      ["Zona / offset", [system.time_zone_name, system.gmt_offset].filter(Boolean).join(" / ")],
      ["Drift waktu", system.drift_seconds == null ? null : `${number(system.drift_seconds)} detik`],
      ["Paket", packageList],
      ["Kapabilitas diperiksa", data.capabilities && data.capabilities.checked_at ? timeAgo(data.capabilities.checked_at) : null],
    ];
    const infoHtml = infoItems
      .map(
        ([label, value]) =>
          `<div><dt>${escapeHtml(label)}</dt><dd>${text(value)}</dd></div>`
      )
      .join("");

    const problems = data.problems || [];
    const problemHtml = problems.length
      ? `<div class="table-wrap"><table class="mon-table"><thead><tr><th>Jenis</th><th>Severity</th><th>Pesan</th><th>Sejak</th><th>Status</th></tr></thead><tbody>${problems
          .map((row) => {
            const sev = MT_SEVERITY_META[row.severity] || MT_SEVERITY_META.informational;
            return (
              `<tr><td class="mono">${text(row.kind)}</td><td><span class="${escapeHtml(sev.cls)}">${escapeHtml(sev.label)}</span></td>` +
              `<td>${text(row.message)}</td><td>${text(timeAgo(row.started_at))}</td>` +
              `<td>${row.acked_by ? `Di-ack oleh ${escapeHtml(row.acked_by)}` : "Terbuka"}</td></tr>`
            );
          })
          .join("")}</tbody></table></div>`
      : '<p class="empty-state">Tidak ada masalah terbuka untuk perangkat ini.</p>';

    const findingRows = (findings.rows || []).slice(0, 8);
    const findingHtml = findingRows.length
      ? `<div class="table-wrap"><table class="mon-table"><thead><tr><th>Temuan</th><th>Severity</th><th>Status</th><th>Aksi</th></tr></thead><tbody>${findingRows
          .map((row) => {
            const sev = MT_SEVERITY_META[row.severity] || MT_SEVERITY_META.informational;
            const ignored = Boolean(row.ignored);
            return (
              `<tr><td><strong>${text(row.title)}</strong><span class="cell-sub">${text(row.detail)}</span></td>` +
              `<td><span class="${escapeHtml(sev.cls)}">${escapeHtml(sev.label)}</span></td>` +
              `<td>${ignored ? "Diabaikan" : "Terbuka"}</td>` +
              `<td>${canOperate() ? `<button class="btn btn--sm btn--ghost" type="button" data-mt-action="${ignored ? "finding_unignore" : "finding_ignore"}" data-target="${row.id}">${ignored ? "Aktifkan" : "Abaikan"}</button>` : "—"}</td></tr>`
            );
          })
          .join("")}</tbody></table></div>`
      : '<p class="empty-state">Tidak ada temuan keamanan aktif.</p>';

    const pollRows = (data.poll_state || [])
      .map(
        (row) =>
          `<tr><td>${text(row.layer)}</td><td>${text(row.last_run ? timeAgo(row.last_run) : null)}</td>` +
          `<td>${text(row.last_ok ? timeAgo(row.last_ok) : null)}</td><td>${text(row.last_error)}</td></tr>`
      )
      .join("");
    const probeRows = (data.probe_errors || [])
      .map(
        (row) =>
          `<tr><td class="mono">${text(row.probe)}</td><td>${text(row.reason)}</td>` +
          `<td>${text(row.detail)}</td><td>${text(row.retry_after ? timeAgo(row.retry_after) : null)}</td></tr>`
      )
      .join("");

    const actionRows = (data.actions || [])
      .slice(0, 10)
      .map(
        (row) =>
          `<tr><td>${text(timeAgo(row.ts))}</td><td>${text(row.username)}</td><td>${text(ACTION_LABELS[row.action] || row.action)}</td>` +
          `<td class="mono">${text(row.target)}</td><td>${text(row.result)}</td>` +
          `<td>${row.verified === true ? "Terverifikasi" : row.verified === false ? "Belum terverifikasi" : "—"}</td></tr>`
      )
      .join("");

    tabSections.innerHTML =
      `<div class="detail-grid">${cardHtml}</div>` +
      `<section class="panel panel--nested"><div class="panel__head"><div><h3 class="panel__title">Informasi RouterOS</h3>` +
      `<p class="panel__sub">Nilai dari /system/resource, /system/routerboard, /system/clock, dan paket.</p></div></div>` +
      `<dl class="def-list">${infoHtml}</dl></section>` +
      `<section class="panel panel--nested"><div class="panel__head"><div><h3 class="panel__title">Masalah terbuka</h3>` +
      `<p class="panel__sub">Terintegrasi dengan ack, pemeliharaan, dan notifikasi.</p></div></div>${problemHtml}</section>` +
      `<section class="panel panel--nested"><div class="panel__head"><div><h3 class="panel__title">Temuan keamanan</h3>` +
      `<p class="panel__sub">${number(findings.open)} terbuka · ${number(findings.ignored)} diabaikan</span></p></div></div>${findingHtml}</section>` +
      `<section class="panel panel--nested"><div class="panel__head"><div><h3 class="panel__title">Status poll &amp; kapabilitas</h3>` +
      `<p class="panel__sub">Layer fast/medium/slow dan probe yang tidak didukung.</p></div></div>` +
      `<div class="table-wrap"><table class="mon-table"><thead><tr><th>Layer</th><th>Terakhir jalan</th><th>Terakhir sukses</th><th>Error</th></tr></thead><tbody>${pollRows || emptyRow(4, "Belum ada poll MikroTik.")}</tbody></table></div>` +
      (probeRows
        ? `<div class="table-wrap"><table class="mon-table"><thead><tr><th>Probe</th><th>Alasan</th><th>Detail</th><th>Coba lagi</th></tr></thead><tbody>${probeRows}</tbody></table></div>`
        : '<p class="note-info note-info--sm">Semua probe yang dicoba berhasil.</p>') +
      `</section>` +
      buildSettingsPanel(settings, write) +
      (canOperate() && write.available && isAdmin()
        ? `<section class="panel panel--nested"><div class="panel__head"><div><h3 class="panel__title">Aksi tulis</h3>` +
          `<p class="panel__sub">Hanya aksi allowlist; setiap aksi dicatat di audit log.</p></div></div>` +
          `<div class="admin-actions"><button class="btn btn--danger" id="mt-reboot" type="button">Reboot perangkat</button></div></section>`
        : "") +
      (canOperate() && data.actions
        ? `<section class="panel panel--nested"><div class="panel__head"><div><h3 class="panel__title">Log aksi terakhir</h3>` +
          `<p class="panel__sub">Siapa, aksi, target, hasil, dan verifikasi.</p></div></div>` +
          `<div class="table-wrap"><table class="mon-table"><thead><tr><th>Waktu</th><th>Pengguna</th><th>Aksi</th><th>Target</th><th>Hasil</th><th>Verifikasi</th></tr></thead><tbody>${actionRows || emptyRow(6, "Belum ada aksi tulis.")}</tbody></table></div></section>`
        : "") +
      (isAdmin() ? buildBackupsPanelHtml() : "");

    if (isAdmin()) bindBackupsPanel();
    const reboot = document.getElementById("mt-reboot");
    if (reboot) reboot.addEventListener("click", () => runWriteAction("reboot", device.name, {}, { typed: true }));
    bindSettingsForm(settings);
  }

  function buildSettingsPanel(settings, write) {
    const modules = settings.modules || {};
    const intervals = settings.intervals || {};
    const moduleLabels = {
      ppp: "PPP", hotspot: "Hotspot", wireless: "Wireless", dhcp: "DHCP",
      queue: "Queue", routing: "Routing", log: "Log", security: "Keamanan",
    };
    const moduleHtml = Object.entries(moduleLabels)
      .map(
        ([key, label]) =>
          `<label class="check"><input type="checkbox" data-mt-module="${key}" ${modules[key] ? "checked" : ""} ${isAdmin() ? "" : "disabled"} /> ${escapeHtml(label)}</label>`
      )
      .join("");
    const intervalHtml = ["fast", "medium", "slow"]
      .map(
        (key) =>
          `<label class="admin-field"><span>Interval ${key} (detik)</span>` +
          `<input type="number" min="${{ fast: 30, medium: 60, slow: 300 }[key]}" max="604800" step="1" data-mt-interval="${key}" value="${escapeHtml(intervals[key] ?? "")}" ${isAdmin() ? "" : "disabled"} /></label>`
      )
      .join("");
    const writeHtml = isAdmin()
      ? `<h3 class="detail-subhead">Kredensial tulis (terpisah dari kredensial baca)</h3>` +
        `<div class="admin-form__grid">` +
        `<label class="check"><input type="checkbox" id="mt-write-enabled" ${settings.write_enabled ? "checked" : ""} /> Aktifkan kredensial tulis</label>` +
        `<label class="admin-field"><span>Pengguna tulis</span><input id="mt-write-user" type="text" value="${escapeHtml(settings.write_user || "")}" autocomplete="off" /></label>` +
        `<label class="admin-field"><span>Kata sandi tulis</span><input id="mt-write-password" type="password" placeholder="${settings.has_write_credentials ? "Kosongkan = pertahankan" : "Belum diatur"}" autocomplete="new-password" /></label>` +
        `<label class="admin-field"><span>Port API tulis</span><input id="mt-write-port" type="number" min="1" max="65535" value="${escapeHtml(settings.write_port || 8728)}" /></label>` +
        `<label class="check"><input type="checkbox" id="mt-write-ssl" ${settings.write_ssl ? "checked" : ""} /> API tulis memakai SSL/TLS</label>` +
        `</div>` +
        `<h3 class="detail-subhead">Kredensial SSH &amp; backup (M6)</h3>` +
        `<p class="admin-hint">SSH hanya untuk <code>/export hide-sensitive</code> (baca). Host key diverifikasi secara ketat; host yang tidak dikenal ditolak. Restore dari aplikasi tidak didukung.</p>` +
        `<div class="admin-form__grid">` +
        `<label class="check"><input type="checkbox" id="mt-ssh-enabled" ${settings.ssh_enabled ? "checked" : ""} /> Aktifkan SSH</label>` +
        `<label class="check"><input type="checkbox" id="mt-backup-enabled" ${settings.backup_enabled ? "checked" : ""} /> Backup harian (/export hide-sensitive)</label>` +
        `<label class="admin-field"><span>Pengguna SSH</span><input id="mt-ssh-user" type="text" value="${escapeHtml(settings.ssh_user || "")}" autocomplete="off" /></label>` +
        `<label class="admin-field"><span>Kata sandi SSH</span><input id="mt-ssh-password" type="password" placeholder="${settings.has_ssh_credentials ? "Kosongkan = pertahankan" : "Belum diatur"}" autocomplete="new-password" /></label>` +
        `<label class="admin-field"><span>Port SSH</span><input id="mt-ssh-port" type="number" min="1" max="65535" value="${escapeHtml(settings.ssh_port || 22)}" /></label>` +
        `<label class="admin-field"><span>Jam backup harian (0–23)</span><input id="mt-backup-hour" type="number" min="0" max="23" step="1" value="${escapeHtml(settings.backup_hour ?? 2)}" /></label>` +
        `<label class="admin-field admin-field--full"><span>Host key SSH * (SHA256:… atau baris public key)</span>` +
        `<input id="mt-ssh-host-key" type="text" value="${escapeHtml(settings.ssh_host_key || "")}" placeholder="SHA256:XXXX…" autocomplete="off" /></label>` +
        `</div>`
      : `<p class="note-info note-info--sm">Kredensial tulis dan SSH hanya dapat diatur oleh admin. Operator dapat menandai interface penting.</p>`;

    return (
      `<section class="panel panel--nested"><div class="panel__head"><div><h3 class="panel__title">Pengaturan modul</h3>` +
      `<p class="panel__sub">Modul yang dinonaktifkan tidak dipoll; interface penting memicu masalah mt_interface_down.</p></div></div>` +
      `<form class="admin-form" id="mt-settings-form" novalidate>` +
      `<h3 class="detail-subhead">Modul yang dipoll</h3><div class="mt-checks">${moduleHtml}</div>` +
      `<h3 class="detail-subhead">Interval layer</h3><div class="admin-form__grid">${intervalHtml}</div>` +
      `<label class="admin-field admin-field--full"><span>Interface penting (dipisah koma)</span>` +
      `<input id="mt-important" type="text" value="${escapeHtml((settings.important_interfaces || []).join(", "))}" placeholder="mis. ether1, sfp-sfpplus1" autocomplete="off" /></label>` +
      writeHtml +
      `<p class="form-note is-error" id="mt-settings-note" role="alert" hidden></p>` +
      `<div class="admin-actions"><button class="btn btn--primary" id="mt-settings-save" type="submit">Simpan pengaturan</button></div>` +
      `</form></section>`
    );
  }

  function bindSettingsForm(settings) {
    const form = document.getElementById("mt-settings-form");
    if (!form) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const note = document.getElementById("mt-settings-note");
      const save = document.getElementById("mt-settings-save");
      note.hidden = true;
      const body = {};
      const important = document.getElementById("mt-important").value.trim();
      body.important_interfaces = important
        ? important.split(",").map((item) => item.trim()).filter(Boolean)
        : [];
      if (isAdmin()) {
        body.modules = {};
        form.querySelectorAll("[data-mt-module]").forEach((input) => {
          body.modules[input.dataset.mtModule] = input.checked;
        });
        body.intervals = {};
        for (const key of ["fast", "medium", "slow"]) {
          const input = form.querySelector(`[data-mt-interval="${key}"]`);
          body.intervals[key] = Number(input.value);
        }
        body.write_enabled = document.getElementById("mt-write-enabled").checked;
        body.write_user = document.getElementById("mt-write-user").value.trim();
        const password = document.getElementById("mt-write-password").value;
        if (password) body.write_password = password;
        body.write_port = Number(document.getElementById("mt-write-port").value || 8728);
        body.write_ssl = document.getElementById("mt-write-ssl").checked;
        body.ssh_enabled = document.getElementById("mt-ssh-enabled").checked;
        body.backup_enabled = document.getElementById("mt-backup-enabled").checked;
        body.ssh_user = document.getElementById("mt-ssh-user").value.trim();
        const sshPassword = document.getElementById("mt-ssh-password").value;
        if (sshPassword) body.ssh_password = sshPassword;
        body.ssh_port = Number(document.getElementById("mt-ssh-port").value || 22);
        body.backup_hour = Number(document.getElementById("mt-backup-hour").value || 2);
        body.ssh_host_key = document.getElementById("mt-ssh-host-key").value.trim();
      }
      save.disabled = true;
      try {
        const result = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/settings`, {
          method: "PUT",
          body,
        });
        toast("Pengaturan MikroTik tersimpan.");
        state.detail.overview.settings = result.settings;
        state.detail.overview = null;
        await ensureOverview();
        if (state.detail.overview) renderRingkasan();
      } catch (error) {
        note.textContent = error.message || "Gagal menyimpan pengaturan.";
        note.hidden = false;
      } finally {
        save.disabled = false;
      }
    });
  }

  /* ---------- Grafik trafik interface ---------- */

  function buildTrafficPanel() {
    const panel = document.createElement("section");
    panel.className = "panel panel--nested";
    const ranges = [
      ["1h", "1 jam"],
      ["24h", "24 jam"],
      ["7d", "7 hari"],
    ];
    panel.innerHTML =
      `<div class="panel__head"><div><h3 class="panel__title">Trafik interface</h3>` +
      `<p class="panel__sub">rx/tx bps dari counter perangkat · rentang 1 jam / 24 jam / 7 hari</p></div>` +
      `<div class="seg" role="group" aria-label="Rentang waktu grafik trafik">` +
      ranges
        .map(
          ([value, label]) =>
            `<button class="seg__btn${state.traffic.range === value ? " is-active" : ""}" type="button" data-range="${value}" aria-pressed="${state.traffic.range === value}">${escapeHtml(label)}</button>`
        )
        .join("") +
      `</div></div>` +
      `<div class="chart-box"><label class="field"><span class="sr-only">Interface</span>` +
      `<select id="mt-traffic-if"><option value="">Memuat…</option></select></label>` +
      `<svg class="chart" id="mt-traffic-chart" viewBox="0 0 600 170" role="img" aria-label="Grafik trafik interface"><title>Grafik trafik interface</title></svg>` +
      `<p class="chart-summary" id="mt-traffic-summary">Memuat…</p></div>`;
    panel.querySelector(".seg").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-range]");
      if (!button) return;
      state.traffic.range = button.dataset.range;
      panel.querySelectorAll(".seg__btn").forEach((el) => {
        const active = el.dataset.range === state.traffic.range;
        el.classList.toggle("is-active", active);
        el.setAttribute("aria-pressed", String(active));
      });
      loadTraffic(panel);
    });
    panel.querySelector("#mt-traffic-if").addEventListener("change", (event) => {
      state.traffic.selected = event.target.value;
      loadTraffic(panel);
    });
    loadTraffic(panel);
    return panel;
  }

  async function loadTraffic(panel) {
    const svg = panel.querySelector("#mt-traffic-chart");
    const select = panel.querySelector("#mt-traffic-if");
    const summary = panel.querySelector("#mt-traffic-summary");
    if (!svg || !select || !summary) return;
    svg.innerHTML = '<title>Grafik trafik interface</title><text class="chart__empty" x="300" y="85" text-anchor="middle">Memuat data trafik…</text>';
    summary.textContent = "Memuat…";
    let data;
    try {
      data = await apiFetch(`/api/devices/${state.detail.id}/metrics?range=${state.traffic.range}`);
    } catch (error) {
      svg.innerHTML = '<title>Grafik trafik interface</title><text class="chart__empty" x="300" y="85" text-anchor="middle">Gagal memuat trafik.</text>';
      summary.textContent = error.message || "Gagal memuat trafik.";
      return;
    }
    const series = data.interfaces || [];
    if (!series.length) {
      select.innerHTML = '<option value="">Tidak ada interface</option>';
      svg.innerHTML = '<title>Grafik trafik interface</title><text class="chart__empty" x="300" y="85" text-anchor="middle">Belum ada data trafik untuk rentang ini.</text>';
      summary.textContent = "—";
      return;
    }
    const current =
      state.traffic.selected && series.some((item) => item.if_name === state.traffic.selected)
        ? series.find((item) => item.if_name === state.traffic.selected)
        : series[0];
    state.traffic.selected = current.if_name;
    select.innerHTML = series
      .map(
        (item) =>
          `<option value="${escapeHtml(item.if_name)}"${item.if_name === current.if_name ? " selected" : ""}>${escapeHtml(item.if_name)}</option>`
      )
      .join("");
    const rxValues = (current.points || []).map((p) => p.rx_bps);
    const txValues = (current.points || []).map((p) => p.tx_bps);
    renderTrafficChart(svg, rxValues, txValues);
    const peak = (values) => {
      const valid = values.filter((v) => v != null);
      return valid.length ? formatBps(Math.max(...valid)) : "—";
    };
    summary.textContent =
      `Rx puncak ${peak(rxValues)} · Tx puncak ${peak(txValues)} · ${series.length} interface terlacak`;
  }

  function renderTrafficChart(svg, rxValues, txValues) {
    const W = 600, H = 170, PAD = 16;
    const lines = [rxValues, txValues];
    const n = Math.max(...lines.map((line) => line.length), 0);
    const allValues = lines.flat().filter((value) => value != null);
    if (n < 2 || allValues.length < 2) {
      svg.innerHTML = '<title>Grafik trafik interface</title><text class="chart__empty" x="300" y="85" text-anchor="middle">Belum cukup data untuk grafik.</text>';
      return;
    }
    const min = 0;
    const max = Math.max(...allValues, 1);
    const xAt = (index) => PAD + ((W - 2 * PAD) * index) / (n - 1);
    const yAt = (value) => PAD + (H - 2 * PAD) * (1 - (value - min) / (max - min));
    const parts = ["<title>Grafik trafik interface</title>"];
    for (const fraction of [0, 0.5, 1]) {
      const y = yAt(min + (max - min) * fraction);
      parts.push(`<line class="chart__grid" x1="${PAD}" y1="${y.toFixed(1)}" x2="${W - PAD}" y2="${y.toFixed(1)}" />`);
    }
    lines.forEach((values, lineIndex) => {
      const coords = [];
      values.forEach((value, index) => {
        if (value == null) return;
        coords.push([xAt(index), yAt(value)]);
      });
      if (coords.length < 2) return;
      const pointString = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
      const cls = lineIndex === 0 ? "chart__line" : "chart__line chart__line--tx";
      parts.push(`<polyline class="${cls}" points="${pointString}" />`);
    });
    svg.innerHTML = parts.join("");
  }

  /* ---------- Grafik jumlah sesi (Sesi tab) ---------- */

  function buildSessionsPanel() {
    const panel = document.createElement("section");
    panel.className = "panel panel--nested";
    const ranges = [
      ["1h", "1 jam"],
      ["24h", "24 jam"],
      ["7d", "7 hari"],
    ];
    panel.innerHTML =
      `<div class="panel__head"><div><h3 class="panel__title">Jumlah sesi</h3>` +
      `<p class="panel__sub">Sesi PPP dan hotspot aktif dari snapshot tiap poll</p></div>` +
      `<div class="seg" role="group" aria-label="Rentang waktu grafik sesi">` +
      ranges
        .map(
          ([value, label]) =>
            `<button class="seg__btn${state.sessions.range === value ? " is-active" : ""}" type="button" data-range="${value}" aria-pressed="${state.sessions.range === value}">${escapeHtml(label)}</button>`
        )
        .join("") +
      `</div></div>` +
      `<div class="chart-box">` +
      `<svg class="chart" id="mt-sessions-chart" viewBox="0 0 600 170" role="img" aria-label="Grafik jumlah sesi"><title>Grafik jumlah sesi</title></svg>` +
      `<p class="chart-summary" id="mt-sessions-summary">Memuat…</p></div>`;
    panel.querySelector(".seg").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-range]");
      if (!button) return;
      state.sessions.range = button.dataset.range;
      panel.querySelectorAll(".seg__btn").forEach((el) => {
        const active = el.dataset.range === state.sessions.range;
        el.classList.toggle("is-active", active);
        el.setAttribute("aria-pressed", String(active));
      });
      loadSessionsPanel(panel);
    });
    loadSessionsPanel(panel);
    return panel;
  }

  async function loadSessionsPanel(panel) {
    const svg = panel.querySelector("#mt-sessions-chart");
    const summary = panel.querySelector("#mt-sessions-summary");
    if (!svg || !summary) return;
    svg.innerHTML = '<title>Grafik jumlah sesi</title><text class="chart__empty" x="300" y="85" text-anchor="middle">Memuat data sesi…</text>';
    summary.textContent = "Memuat…";
    let data;
    try {
      data = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/metrics?range=${state.sessions.range}`);
    } catch (error) {
      svg.innerHTML = '<title>Grafik jumlah sesi</title><text class="chart__empty" x="300" y="85" text-anchor="middle">Gagal memuat data sesi.</text>';
      summary.textContent = error.message || "Gagal memuat data sesi.";
      return;
    }
    const metrics = data.metrics || {};
    const points = (list) => (list || []).map((item) => item.value);
    const pppValues = points(metrics.ppp_active);
    const hotspotValues = points(metrics.hotspot_active);
    if (pppValues.length < 2 && hotspotValues.length < 2) {
      svg.innerHTML = '<title>Grafik jumlah sesi</title><text class="chart__empty" x="300" y="85" text-anchor="middle">Belum ada histori sesi untuk rentang ini.</text>';
      summary.textContent = "Histori sesi tersedia setelah minimal dua poll.";
      return;
    }
    renderTrafficChart(svg, pppValues, hotspotValues);
    const peak = (values) => {
      const valid = values.filter((v) => v != null);
      return valid.length ? number(Math.max(...valid)) : "—";
    };
    summary.textContent = `PPP maks ${peak(pppValues)} · Hotspot maks ${peak(hotspotValues)} · biru = PPP, kuning = hotspot`;
  }

  /* ---------- Backup konfigurasi (M6, admin) ---------- */

  function buildBackupsPanelHtml() {
    const ssh = state.detail.overview.settings || {};
    const sshReady = ssh.ssh_enabled && ssh.has_ssh_credentials && ssh.ssh_host_key;
    if (!sshReady) {
      return (
        `<section class="panel panel--nested"><div class="panel__head"><div><h3 class="panel__title">Backup konfigurasi (SSH)</h3>` +
        `<p class="panel__sub">Hanya admin; hanya perintah /export hide-sensitive; host key diverifikasi ketat.</p></div></div>` +
        `<p class="note-info note-info--sm">SSH atau host key belum diaktifkan untuk perangkat ini. Isi bagian “Kredensial SSH &amp; backup (M6)” di Pengaturan modul lalu simpan.</p></section>`
      );
    }
    return (
      `<section class="panel panel--nested" id="mt-backups-panel"><div class="panel__head"><div>` +
      `<h3 class="panel__title">Backup konfigurasi (SSH)</h3>` +
      `<p class="panel__sub">Isi ekspor terenkripsi di server; diff dibandingkan antar versi; restore tidak didukung.</p></div>` +
      `<div class="detail-head__actions"><button class="btn btn--primary" id="mt-backup-run" type="button">Backup sekarang</button></div></div>` +
      `<div id="mt-backup-list"><p class="mt-loading">Memuat daftar backup…</p></div>` +
      `<pre class="mt-json" id="mt-backup-diff" hidden></pre></section>`
    );
  }

  async function loadBackups() {
    const container = document.getElementById("mt-backup-list");
    if (!container) return;
    let data;
    try {
      data = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/backups`);
    } catch (error) {
      container.innerHTML = `<p class="note-info note-info--sm">Gagal memuat daftar backup: ${escapeHtml(error.message)}</p>`;
      return;
    }
    const rows = (data && data.backups) || [];
    if (!rows.length) {
      container.innerHTML = '<p class="empty-state">Belum ada backup. Jalankan “Backup sekarang” atau aktifkan backup harian di Pengaturan modul.</p>';
      return;
    }
    container.innerHTML =
      `<div class="table-wrap"><table class="mon-table"><thead><tr><th>Waktu</th><th>Sumber</th><th>Ukuran</th><th>SHA256</th><th>Aksi</th></tr></thead><tbody>` +
      rows.map((row) =>
        `<tr><td class="mono">${escapeHtml(timeAgo(row.created_at))}</td>` +
        `<td>${row.source === "scheduled" ? "Terjadwal" : "Manual"}</td>` +
        `<td class="mono">${escapeHtml(formatBytes(row.size))}</td>` +
        `<td class="mono" title="${escapeHtml(row.sha256 || "")}">${escapeHtml(row.sha256_short || (row.sha256 || "").slice(0, 12))}</td>` +
        `<td><button class="btn btn--sm btn--ghost" type="button" data-mt-backup="diff" data-id="${row.id}">Diff</button> ` +
        `<a class="btn btn--sm btn--ghost" href="/api/mikrotik/devices/${state.detail.id}/backups/${row.id}/download">Unduh</a></td>` +
        `</tr>`
      ).join("") +
      `</tbody></table></div>`;
  }

  function bindBackupsPanel() {
    const run = document.getElementById("mt-backup-run");
    if (run) {
      run.addEventListener("click", async () => {
        run.disabled = true;
        try {
          const result = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/backups`, { method: "POST" });
          const backup = result.backup || {};
          toast(`Backup tersimpan: ${backup.filename || ""} (${formatBytes(backup.size)})`);
          loadBackups();
        } catch (error) {
          alert(error.message || "Backup gagal.");
        } finally {
          run.disabled = false;
        }
      });
    }
    const container = document.getElementById("mt-backup-list");
    if (container) {
      container.addEventListener("click", async (event) => {
        const button = event.target.closest("button[data-mt-backup='diff']");
        if (!button) return;
        const backupId = Number(button.dataset.id);
        const diffEl = document.getElementById("mt-backup-diff");
        button.disabled = true;
        try {
          const result = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/backups/${backupId}/diff`);
          diffEl.hidden = false;
          diffEl.textContent =
            result.diff && result.diff.trim()
              ? `Diff: ${result.from} → ${result.to}\n\n${result.diff}`
              : result.note || "Tidak ada perbedaan.";
        } catch (error) {
          alert(error.message || "Gagal memuat diff.");
        } finally {
          button.disabled = false;
        }
      });
    }
    loadBackups();
  }

  /* ---------- Aksi tulis (M5) ---------- */

  tabSections.addEventListener("click", async (event) => {
    const importantBtn = event.target.closest("button[data-mt-important]");
    if (importantBtn) {
      const name = importantBtn.dataset.name;
      const remove = importantBtn.dataset.mtImportant === "remove";
      const settings = (state.detail.overview && state.detail.overview.settings) || {};
      const current = new Set((settings.important_interfaces || []).map((item) => String(item)));
      if (remove) current.delete(name);
      else current.add(name);
      importantBtn.disabled = true;
      try {
        const result = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/settings`, {
          method: "PUT",
          body: { important_interfaces: Array.from(current) },
        });
        toast(`Interface ${name} ${remove ? "dihapus dari daftar penting" : "ditandai penting"}.`);
        if (state.detail.overview) {
          state.detail.overview.settings = result.settings;
        }
        loadTab();
      } catch (error) {
        alert(error.message || "Gagal menyimpan interface penting.");
        importantBtn.disabled = false;
      }
      return;
    }
    const button = event.target.closest("button[data-mt-action]");
    if (!button) return;
    const action = button.dataset.mtAction;
    const target = button.dataset.target || "";
    const label = button.dataset.label || target;
    if (action === "finding_ignore") {
      const reason = window.prompt(`Alasan mengabaikan temuan ini (wajib, min. 3 karakter):`);
      if (reason == null) return;
      if (reason.trim().length < 3) {
        alert("Alasan minimal 3 karakter.");
        return;
      }
      button.disabled = true;
      try {
        await apiFetch(`/api/mikrotik/devices/${state.detail.id}/findings/${target}/ignore`, {
          method: "POST",
          body: { ignored: true, reason: reason.trim() },
        });
        toast("Temuan diabaikan.");
        refreshOverviewAndTab();
      } catch (error) {
        alert(error.message || "Gagal mengabaikan temuan.");
      } finally {
        button.disabled = false;
      }
      return;
    }
    if (action === "finding_unignore") {
      if (!window.confirm("Aktifkan kembali temuan ini?")) return;
      button.disabled = true;
      try {
        await apiFetch(`/api/mikrotik/devices/${state.detail.id}/findings/${target}/ignore`, {
          method: "POST",
          body: { ignored: false },
        });
        toast("Temuan diaktifkan kembali.");
        refreshOverviewAndTab();
      } catch (error) {
        alert(error.message || "Gagal mengaktifkan temuan.");
      } finally {
        button.disabled = false;
      }
      return;
    }
    const typed = button.dataset.typed === "1";
    await runWriteAction(action, target, {}, { typed, label });
  });

  async function runWriteAction(action, target, params, options) {
    const opts = options || {};
    const device = state.detail.overview.device;
    const actionLabel = ACTION_LABELS[action] || action;
    const targetLabel = opts.label || target || device.name;
    if (!window.confirm(`${actionLabel} pada "${targetLabel}" di perangkat ${device.name}? Tindakan ini dicatat di audit log.`)) {
      return;
    }
    if (opts.typed) {
      const typedValue = window.prompt(
        `Ketik nama perangkat "${device.name}" untuk mengonfirmasi: ${actionLabel} pada "${targetLabel}".`
      );
      if (typedValue == null) return;
      if (typedValue !== device.name) {
        alert("Nama perangkat tidak cocok. Aksi dibatalkan.");
        return;
      }
    }
    try {
      const result = await apiFetch(`/api/mikrotik/devices/${state.detail.id}/actions`, {
        method: "POST",
        body: {
          action,
          target,
          params,
          confirm_name: opts.typed ? device.name : undefined,
        },
      });
      toast(result.detail || `${actionLabel} selesai.`);
      refreshOverviewAndTab();
    } catch (error) {
      alert(error.message || `${actionLabel} gagal.`);
    }
  }

  async function refreshOverviewAndTab() {
    state.detail.overview = null;
    await ensureOverview();
    if (!state.detail.overview) return;
    renderDeviceHeader();
    renderTabs();
    if (state.detail.tab === "ringkasan") renderRingkasan();
    else loadTab();
  }

  function buildAddressListForm() {
    const panel = document.createElement("section");
    panel.className = "panel panel--nested";
    panel.innerHTML =
      `<div class="panel__head"><div><h3 class="panel__title">Tambah entri address-list</h3>` +
      `<p class="panel__sub">Aksi tulis allowlist dengan timeout dan komentar; diverifikasi dengan membaca ulang router.</p></div></div>` +
      `<form class="admin-form" id="mt-addr-form">` +
      `<div class="admin-form__grid">` +
      `<label class="admin-field"><span>Nama list *</span><input id="mt-addr-list" type="text" placeholder="mis. blocked" autocomplete="off" /></label>` +
      `<label class="admin-field"><span>Alamat *</span><input id="mt-addr-address" type="text" placeholder="mis. 203.0.113.9" autocomplete="off" /></label>` +
      `<label class="admin-field"><span>Timeout *</span><input id="mt-addr-timeout" type="text" value="1h" placeholder="mis. 30m, 1h, 1d" autocomplete="off" /></label>` +
      `<label class="admin-field"><span>Komentar</span><input id="mt-addr-comment" type="text" placeholder="Opsional" autocomplete="off" /></label>` +
      `</div>` +
      `<p class="form-note is-error" id="mt-addr-note" role="alert" hidden></p>` +
      `<div class="admin-actions"><button class="btn btn--primary" id="mt-addr-save" type="submit">Tambah entri</button></div>` +
      `</form>`;
    panel.querySelector("form").addEventListener("submit", (event) => {
      event.preventDefault();
      const note = panel.querySelector("#mt-addr-note");
      const list = panel.querySelector("#mt-addr-list").value.trim();
      const address = panel.querySelector("#mt-addr-address").value.trim();
      const timeout = panel.querySelector("#mt-addr-timeout").value.trim() || "1h";
      const comment = panel.querySelector("#mt-addr-comment").value.trim();
      if (!list || !address) {
        note.textContent = "Nama list dan alamat wajib diisi.";
        note.hidden = false;
        return;
      }
      if (!/^\d+[smhdw]?$/.test(timeout)) {
        note.textContent = "Timeout harus berupa angka + satuan (mis. 1h, 30m).";
        note.hidden = false;
        return;
      }
      runWriteAction(
        "address_list_add",
        address,
        { list, address, timeout, comment },
        { label: `${list}:${address}` }
      );
    });
    return panel;
  }

  /* ---------- start ---------- */

  whenReady(() => {
    activateView();
  });
})();
