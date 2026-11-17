/* Monitoring: problems, hosts, latest data, maps — semua dari API. */
(function () {
  "use strict";

  const {
    escapeHtml,
    timeAgo,
    formatPercent,
    formatBytes,
    formatUptime,
    STATUS_META,
    SEVERITY_META,
    availabilityClass,
    TYPE_LABELS,
  } = window.NMSFormat;

  const VIEWS = {
    problems: { label: "Masalah", desc: "Pantau seluruh masalah aktif di jaringan." },
    hosts: { label: "Perangkat", desc: "Daftar perangkat terdaftar beserta ketersediaan 24 jam." },
    "latest-data": { label: "Data terbaru", desc: "Nilai terakhir yang dikumpulkan dari seluruh perangkat." },
    maps: { label: "Peta", desc: "Topologi jaringan berdasarkan uplink (induk)." },
  };

  const views = document.querySelectorAll("[data-view]");
  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");

  function currentView() {
    const raw = window.location.hash.replace(/^#/, "");
    return Object.prototype.hasOwnProperty.call(VIEWS, raw) ? raw : "problems";
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
    window.NMSFormat.setDocumentTitle(meta.label, "Pemantauan");
    if (current === "problems") loadProblems();
    else if (current === "hosts") loadHosts();
    else if (current === "latest-data") loadLatest();
    else if (current === "maps") loadMaps();
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
     Problems
     ===================================================== */

  const problemRows = document.getElementById("problem-rows");
  const problemCount = document.getElementById("problem-count");
  const problemSearch = document.getElementById("problem-search");
  const problemSeverity = document.getElementById("problem-severity");
  const problemUnacked = document.getElementById("problem-unacked");
  const sevCards = document.querySelectorAll(".stat--filter[data-sev]");

  let problemsCache = [];
  let severityFilter = ""; // kosong = semua

  function problemDuration(startedAt) {
    const start = window.NMSFormat.parseServerTs(startedAt);
    if (!start) return "—";
    const minutes = Math.max(0, Math.floor((Date.now() - start.getTime()) / 60000));
    const days = Math.floor(minutes / 1440);
    const hours = Math.floor((minutes % 1440) / 60);
    const mins = minutes % 60;
    if (days > 0) return `${days}h ${hours}j`;
    if (hours > 0) return `${hours}j ${mins}m`;
    return `${mins}m`;
  }

  function renderProblems() {
    const query = problemSearch.value.trim().toLowerCase();
    const severity = problemSeverity.value;
    const onlyUnacked = problemUnacked.checked;

    const visible = problemsCache.filter((problem) => {
      if (severityFilter && problem.severity !== severityFilter) return false;
      if (severity !== "all" && problem.severity !== severity) return false;
      if (onlyUnacked && problem.acked_at) return false;
      if (query) {
        const haystack = `${problem.message} ${problem.device_name} ${problem.device_ip} ${problem.kind}`.toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      return true;
    });

    const unackedCount = problemsCache.filter((p) => !p.acked_at).length;
    problemCount.textContent = problemsCache.length === 0
      ? "Tidak ada masalah terbuka"
      : `${problemsCache.length} masalah · ${unackedCount} belum diakui`;

    // Kartu severity selalu dari data nyata.
    for (const sev of Object.keys(SEVERITY_META)) {
      const open = problemsCache.filter((p) => p.severity === sev);
      const item = document.getElementById(`sev-${sev}`);
      const meta = document.getElementById(`sev-${sev}-meta`);
      if (item) {
        item.textContent = String(open.length);
        const unacked = open.filter((p) => !p.acked_at).length;
        meta.textContent = open.length === 0
          ? "Tidak ada"
          : unacked === 0 ? "Semua diakui" : `${unacked} belum diakui`;
      }
    }

    if (visible.length === 0) {
      problemRows.innerHTML = problemsCache.length === 0
        ? '<tr class="empty-row"><td colspan="6">Tidak ada masalah terbuka. Jaringan sehat.</td></tr>'
        : '<tr class="empty-row"><td colspan="6">Tidak ada masalah yang cocok dengan filter saat ini.</td></tr>';
      return;
    }

    problemRows.innerHTML = visible
      .map((problem) => {
        const sev = SEVERITY_META[problem.severity] || SEVERITY_META.information;
        const acked = !!problem.acked_at;
        return (
          `<tr data-severity="${escapeHtml(problem.severity)}">` +
          `<td><span class="${escapeHtml(sev.cls)}">${escapeHtml(sev.label)}</span></td>` +
          `<td><strong>${escapeHtml(problem.message)}</strong></td>` +
          `<td><a class="cell-link" href="inventory.html#host/${problem.device_id}">${escapeHtml(problem.device_name)}</a><span class="cell-sub mono">${escapeHtml(problem.device_ip)}</span></td>` +
          `<td class="mono">${escapeHtml(window.NMSFormat.formatDateTime(problem.started_at))}</td>` +
          `<td class="mono">${escapeHtml(problemDuration(problem.started_at))}</td>` +
          `<td><button class="ack-toggle${acked ? " is-ack" : ""}" type="button" data-ack="${acked ? "true" : "false"}" data-id="${problem.id}" aria-pressed="${acked}">${acked ? "Diakui" : "Belum diakui"}</button></td>` +
          `</tr>`
        );
      })
      .join("");
  }

  async function loadProblems() {
    try {
      const data = await window.apiFetch("/api/problems?status=open&limit=200");
      problemsCache = data.problems || [];
    } catch (error) {
      problemRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat masalah: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    renderProblems();
  }

  sevCards.forEach((card) => {
    card.addEventListener("click", () => {
      const sev = card.dataset.sev;
      const next = severityFilter === sev ? "" : sev;
      severityFilter = next;
      sevCards.forEach((candidate) => {
        const active = candidate.dataset.sev === next;
        candidate.setAttribute("aria-pressed", String(active));
        candidate.classList.toggle("is-active", active);
      });
      renderProblems();
    });
  });

  problemSearch.addEventListener("input", debounce(renderProblems, 200));
  problemSeverity.addEventListener("change", renderProblems);
  problemUnacked.addEventListener("change", renderProblems);

  problemRows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-id]");
    if (!button) return;
    const id = Number(button.dataset.id);
    const acked = button.dataset.ack !== "true";
    try {
      await window.apiFetch(`/api/problems/${id}/ack`, { method: "POST", body: { acked } });
      const problem = problemsCache.find((item) => item.id === id);
      if (problem) {
        problem.acked_at = acked ? new Date().toISOString() : null;
        problem.acked_by = acked ? (window.__nmsMe || {}).username : null;
      }
      renderProblems();
      window.dispatchEvent(new CustomEvent("nms:problems-changed"));
    } catch (error) {
      button.textContent = error.message || "Gagal";
    }
  });

  /* =====================================================
     Hosts
     ===================================================== */

  const hostRows = document.getElementById("host-rows");
  const hostCount = document.getElementById("host-count");
  const hostInterval = document.getElementById("host-interval");
  const hostSearch = document.getElementById("host-search");
  const hostStatus = document.getElementById("host-status");
  const hostGroup = document.getElementById("host-group");
  const hostPrev = document.getElementById("host-prev");
  const hostNext = document.getElementById("host-next");
  const hostPageInfo = document.getElementById("host-page-info");

  const hostState = { page: 1, total: 0, pages: 1, interval: 60 };

  async function loadHosts() {
    hostInterval.textContent = String(hostState.interval);
    const params = new URLSearchParams();
    params.set("page", String(hostState.page));
    params.set("per_page", "10");
    if (hostSearch.value.trim()) params.set("search", hostSearch.value.trim());
    if (hostStatus.value) params.set("status", hostStatus.value);
    if (hostGroup.value) params.set("group", hostGroup.value);

    let data;
    try {
      data = await window.apiFetch(`/api/devices?${params.toString()}`);
    } catch (error) {
      hostRows.innerHTML = `<tr class="empty-row"><td colspan="7">Gagal memuat host: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    hostState.total = data.total || 0;
    hostState.pages = data.pages || 1;
    hostCount.textContent = `${hostState.total} host`;

    document.getElementById("host-total").textContent = String(hostState.total);
    const devices = data.devices || [];
    const settings = window.__nmsSettings || {};
    hostState.interval = Number(settings.default_poll_interval || 60);
    hostInterval.textContent = String(hostState.interval);

    if (devices.length === 0) {
      hostRows.innerHTML = '<tr class="empty-row"><td colspan="7">Tidak ada host yang cocok dengan filter saat ini.</td></tr>';
    } else {
      hostRows.innerHTML = devices
        .map((device) => {
          const meta = STATUS_META[device.status] || STATUS_META.unknown;
          return (
            `<tr data-status="${escapeHtml(device.status)}">` +
            `<td><a class="cell-link" href="inventory.html#host/${device.id}">${escapeHtml(device.name)}</a><span class="cell-sub">${escapeHtml(TYPE_LABELS[device.type] || device.type)}</span></td>` +
            `<td data-avail="${device.availability_24h == null ? "" : device.availability_24h}"><span class="avail ${availabilityClass(device.availability_24h)}">${device.availability_24h == null ? "—" : formatPercent(device.availability_24h)}</span></td>` +
            `<td class="mono">${escapeHtml(device.ip)}</td>` +
            `<td>${escapeHtml(device.group_name || "—")}</td>` +
            `<td>${escapeHtml(device.location || "—")}</td>` +
            `<td><span class="status ${escapeHtml(meta.cls)}">${escapeHtml(meta.label)}</span></td>` +
            `<td class="mono">${escapeHtml(timeAgo(device.last_seen))}</td>` +
            `</tr>`
          );
        })
        .join("");
    }
    hostPageInfo.textContent = `Halaman ${data.page} dari ${hostState.pages}`;
    hostPrev.disabled = data.page <= 1;
    hostNext.disabled = data.page >= hostState.pages;
  }

  async function loadHostStats() {
    try {
      const summary = await window.apiFetch("/api/summary");
      document.getElementById("host-online").textContent = String(summary.online || 0);
      document.getElementById("host-online-meta").textContent = summary.total
        ? `${formatPercent((summary.online / summary.total) * 100, 1)} ketersediaan`
        : "—";
      document.getElementById("host-down").textContent = String((summary.offline || 0) + (summary.degraded || 0));
      document.getElementById("host-maintenance").textContent = String(summary.maintenance || 0);
    } catch {
      /* kartu stats opsional */
    }
  }

  async function loadGroupOptions() {
    try {
      const data = await window.apiFetch("/api/groups");
      const options = (data.groups || []).map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`).join("");
      hostGroup.innerHTML = `<option value="">Semua grup</option>${options}`;
      document.getElementById("map-group").innerHTML = `<option value="all">Semua grup</option>${options}`;
    } catch {
      /* biarkan hanya "Semua grup" */
    }
  }

  hostSearch.addEventListener("input", debounce(() => { hostState.page = 1; loadHosts(); }, 250));
  hostStatus.addEventListener("change", () => { hostState.page = 1; loadHosts(); });
  hostGroup.addEventListener("change", () => { hostState.page = 1; loadHosts(); });
  hostPrev.addEventListener("click", () => { if (hostState.page > 1) { hostState.page -= 1; loadHosts(); } });
  hostNext.addEventListener("click", () => { if (hostState.page < hostState.pages) { hostState.page += 1; loadHosts(); } });
  document.getElementById("host-add").addEventListener("click", () => window.openDeviceForm());

  /* =====================================================
     Latest data
     ===================================================== */

  const latestRows = document.getElementById("latest-rows");

  async function loadLatest() {
    let data;
    try {
      data = await window.apiFetch("/api/devices?per_page=100");
    } catch (error) {
      latestRows.innerHTML = `<tr class="empty-row"><td colspan="8">Gagal memuat data: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    const devices = data.devices || [];
    if (devices.length === 0) {
      latestRows.innerHTML = '<tr class="empty-row"><td colspan="8">Belum ada perangkat. Tambahkan perangkat untuk melihat data terbaru.</td></tr>';
      return;
    }
    latestRows.innerHTML = devices
      .map((device) => {
        const cpu = device.cpu_pct == null ? "—" : formatPercent(device.cpu_pct, 0);
        const mem = device.mem_pct == null ? "—" : formatPercent(device.mem_pct, 0);
        const storage = device.storage_pct == null ? "—" : formatPercent(device.storage_pct, 0);
        const temp = device.temp_c == null ? "—" : `${Number(device.temp_c).toFixed(1)} °C`;
        const latency = device.latency_ms == null ? "—" : `${Number(device.latency_ms).toFixed(0)} ms`;
        return (
          `<tr>` +
          `<td><a class="cell-link" href="inventory.html#host/${device.id}">${escapeHtml(device.name)}</a><span class="cell-sub">${escapeHtml(TYPE_LABELS[device.type] || device.type)}</span></td>` +
          `<td class="mono">${escapeHtml(latency)}</td>` +
          `<td class="mono">${escapeHtml(cpu)}</td>` +
          `<td class="mono">${escapeHtml(mem)}</td>` +
          `<td class="mono">${escapeHtml(storage)}</td>` +
          `<td class="mono">${escapeHtml(temp)}</td>` +
          `<td class="mono">${escapeHtml(formatUptime(device.uptime_s))}</td>` +
          `<td><a class="cell-link" href="inventory.html#host/${device.id}">Detail</a></td>` +
          `</tr>`
        );
      })
      .join("");
  }

  /* =====================================================
     Maps — topologi dinamis dari /api/topology
     ===================================================== */

  const svg = document.getElementById("topology-svg");
  const mapGroup = document.getElementById("map-group");
  const mapTruncation = document.getElementById("map-truncation");
  const mapEmptyHint = document.getElementById("map-empty-hint");
  const mapDetail = document.getElementById("map-detail");
  const NS = "http://www.w3.org/2000/svg";

  function statusClass(status) {
    if (status === "online") return "map-node--ok";
    if (status === "degraded") return "map-node--warn";
    if (status === "offline") return "map-node--down";
    return "map-node--idle";
  }

  function el(name, attrs, parent) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attrs || {})) {
      node.setAttribute(key, value);
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  function layoutTopology(nodes) {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const children = new Map();
    const roots = [];
    for (const node of nodes) {
      if (node.parent_id && byId.has(node.parent_id)) {
        if (!children.has(node.parent_id)) children.set(node.parent_id, []);
        children.get(node.parent_id).push(node);
      } else {
        roots.push(node);
      }
    }
    // Penjenjangan BFS dari akar; simpul tanpa akar masuk level 0.
    const level = new Map();
    const queue = roots.map((root) => [root, 0]);
    const visited = new Set();
    while (queue.length) {
      const [node, lvl] = queue.shift();
      if (visited.has(node.id)) continue;
      visited.add(node.id);
      level.set(node.id, lvl);
      for (const child of children.get(node.id) || []) {
        queue.push([child, lvl + 1]);
      }
    }
    for (const node of nodes) {
      if (!level.has(node.id)) level.set(node.id, 0);
    }

    const levels = new Map();
    for (const node of nodes) {
      const lvl = level.get(node.id);
      if (!levels.has(lvl)) levels.set(lvl, []);
      levels.get(lvl).push(node);
    }
    for (const list of levels.values()) {
      list.sort((a, b) => a.name.localeCompare(b.name));
    }
    return { levels, children: children, byId };
  }

  const NODE_W = 170;
  const NODE_H = 56;
  const GAP_X = 26;
  const GAP_Y = 84;
  const PAD = 30;

  let topologyData = null;
  let selectedId = null;

  function renderTopology(data) {
    topologyData = data;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const nodes = data.nodes || [];
    document.getElementById("map-total").textContent = String(data.total);
    document.getElementById("map-total-meta").textContent = data.truncated
      ? `Menampilkan 60 dari ${data.total}`
      : "Pada topologi";
    const online = nodes.filter((n) => n.status === "online").length;
    document.getElementById("map-online").textContent = String(online);
    document.getElementById("map-online-pct").textContent = nodes.length ? `${Math.round((online / nodes.length) * 100)}%` : "—";
    document.getElementById("map-degraded").textContent = String(nodes.filter((n) => n.status === "degraded").length);
    document.getElementById("map-offline").textContent = String(nodes.filter((n) => n.status === "offline").length);

    mapTruncation.hidden = !data.truncated;
    if (data.truncated) {
      mapTruncation.textContent = `Topologi dipotong: menampilkan 60 dari ${data.total} perangkat. Pilih grup untuk menyaring.`;
    }
    mapEmptyHint.hidden = nodes.length > 0;

    if (nodes.length === 0) {
      svg.setAttribute("viewBox", "0 0 400 120");
      const text = el("text", { x: 200, y: 60, "text-anchor": "middle", class: "map-node__meta" }, svg);
      text.textContent = "Belum ada perangkat";
      return;
    }

    const { levels } = layoutTopology(nodes);
    const levelKeys = [...levels.keys()].sort((a, b) => a - b);
    const maxPerRow = Math.max(...levelKeys.map((lvl) => levels.get(lvl).length), 1);
    const width = PAD * 2 + maxPerRow * NODE_W + (maxPerRow - 1) * GAP_X;
    const height = PAD * 2 + levelKeys.length * NODE_H + (levelKeys.length - 1) * GAP_Y;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.style.minWidth = `${Math.min(width, 1400)}px`;

    const positions = new Map();
    levelKeys.forEach((lvl, rowIndex) => {
      const list = levels.get(lvl);
      const rowWidth = list.length * NODE_W + (list.length - 1) * GAP_X;
      let x = (width - rowWidth) / 2;
      const y = PAD + rowIndex * (NODE_H + GAP_Y);
      for (const node of list) {
        positions.set(node.id, { x, y });
        x += NODE_W + GAP_X;
      }
    });

    const byId = new Map(nodes.map((n) => [n.id, n]));
    for (const edge of data.edges || []) {
      const from = positions.get(edge.from);
      const to = positions.get(edge.to);
      if (!from || !to) continue;
      const parent = byId.get(edge.from);
      const linkCls = parent && parent.status === "offline" ? "map-link map-link--down"
        : parent && parent.status === "degraded" ? "map-link map-link--warn"
        : "map-link";
      el(
        "path",
        {
          class: linkCls,
          d: `M${from.x + NODE_W / 2} ${from.y + NODE_H} C ${from.x + NODE_W / 2} ${from.y + NODE_H + GAP_Y / 2}, ${to.x + NODE_W / 2} ${to.y - GAP_Y / 2}, ${to.x + NODE_W / 2} ${to.y}`,
        },
        svg
      );
    }

    // Garis fiber digambar di atas garis parent–child, berwarna sesuai status link.
    for (const fiber of data.fiber_links || []) {
      const from = positions.get(fiber.device_a_id);
      const to = positions.get(fiber.device_b_id);
      if (!from || !to) continue;
      const fiberCls =
        fiber.status === "crit" ? "map-fiber-link map-fiber-link--crit"
        : fiber.status === "warn" ? "map-fiber-link map-fiber-link--warn"
        : fiber.status === "unknown" ? "map-fiber-link map-fiber-link--unknown"
        : "map-fiber-link";
      const path = el(
        "path",
        {
          class: fiberCls,
          d: `M${from.x + NODE_W / 2} ${from.y + NODE_H / 2} C ${from.x + NODE_W / 2 + 70} ${from.y - 70}, ${to.x + NODE_W / 2 - 70} ${to.y + NODE_H / 2 - 70}, ${to.x + NODE_W / 2} ${to.y + NODE_H / 2}`,
        },
        svg
      );
      el("title", {}, path).textContent =
        `${fiber.name} · A→B ${fiber.attenuation_ab_db == null ? "—" : `${Number(fiber.attenuation_ab_db).toFixed(2)} dB`}`;
    }

    for (const node of nodes) {
      const pos = positions.get(node.id);
      const group = el(
        "g",
        {
          class: `map-node ${statusClass(node.status)}`,
          tabindex: "0",
          role: "button",
          "aria-label": `${node.name}, ${node.status}`,
          "data-id": String(node.id),
        },
        svg
      );
      el("rect", { x: pos.x, y: pos.y, width: NODE_W, height: NODE_H, rx: 12 }, group);
      el("circle", { class: "map-node__dot", cx: pos.x + 16, cy: pos.y + NODE_H / 2, r: 5 }, group);
      const nameText = el("text", { class: "map-node__name", x: pos.x + 28, y: pos.y + 23 }, group);
      nameText.textContent = node.name.length > 22 ? `${node.name.slice(0, 21)}…` : node.name;
      const metaText = el("text", { class: "map-node__meta", x: pos.x + 28, y: pos.y + 40 }, group);
      metaText.textContent = `${node.ip}${node.group_name ? ` · ${node.group_name}` : ""}`;

      group.addEventListener("click", () => selectNode(node.id));
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectNode(node.id);
        }
      });
    }
  }

  function selectNode(id) {
    selectedId = id;
    svg.querySelectorAll(".map-node").forEach((node) => {
      node.classList.toggle("is-selected", Number(node.dataset.id) === id);
    });
    const node = (topologyData && topologyData.nodes || []).find((item) => item.id === id);
    if (!node) return;
    mapDetail.hidden = false;
    const meta = STATUS_META[node.status] || STATUS_META.unknown;
    document.getElementById("map-detail-title").textContent = node.name;
    document.getElementById("map-detail-type").textContent = `${TYPE_LABELS[node.type] || node.type}${node.location ? ` · ${node.location}` : ""}`;
    document.getElementById("map-detail-ip").textContent = node.ip;
    document.getElementById("map-detail-status").textContent = meta.label;
    document.getElementById("map-detail-latency").textContent = node.latency_ms == null ? "—" : `${Number(node.latency_ms).toFixed(0)} ms`;
    document.getElementById("map-detail-group").textContent = node.group_name || "—";
    document.getElementById("map-detail-location").textContent = node.location || "—";
    document.getElementById("map-detail-link").href = `inventory.html#host/${node.id}`;

    // Link fiber yang melibatkan perangkat ini (redaman A→B / B→A), termasuk ujung B non-perangkat.
    const fiberBlock = document.getElementById("map-fiber");
    const fiberList = document.getElementById("map-fiber-list");
    const fiberLinks = ((topologyData && (topologyData.fiber_links_all || topologyData.fiber_links)) || []).filter(
      (fiber) => fiber.device_a_id === id || fiber.device_b_id === id
    );
    if (fiberLinks.length === 0) {
      fiberBlock.hidden = true;
      fiberList.innerHTML = "";
    } else {
      fiberBlock.hidden = false;
      fiberList.innerHTML = fiberLinks
        .map((fiber) => {
          const isA = fiber.device_a_id === id;
          const direction = isA ? "ujung A → B" : "ujung B → A";
          const bLabel = fiber.device_b_name
            ? `${fiber.device_b_name} · ${fiber.port_b || "?"}`
            : (fiber.end_b_label || "Ujung B tidak terpantau");
          const att = isA ? fiber.attenuation_ab_db : fiber.attenuation_ba_db;
          const meta = fiber.status === "crit" ? "Kritis" : fiber.status === "warn" ? "Waspada" : fiber.status === "unknown" ? "Tidak diketahui" : "Normal";
          const statusCls = fiber.status === "crit" ? "avail avail--down" : fiber.status === "warn" ? "avail avail--warn" : fiber.status === "unknown" ? "avail" : "avail avail--ok";
          return (
            `<li><a class="linklike" href="fiber.html#links/${fiber.id}">${escapeHtml(fiber.name)}</a>` +
            `<span class="fiber-link-meta">${escapeHtml(direction)} · lawan: ${escapeHtml(bLabel)} · redaman <b>${att == null ? "—" : `${Number(att).toFixed(2)} dB`}</b> · ${escapeHtml(String(fiber.length_km))} km</span>` +
            `<span class="${statusCls}">${escapeHtml(meta)}</span></li>`
          );
        })
        .join("");
    }
  }

  async function loadMaps() {
    const params = new URLSearchParams();
    if (mapGroup.value && mapGroup.value !== "all") params.set("group", mapGroup.value);
    try {
      const data = await window.apiFetch(`/api/topology?${params.toString()}`);
      renderTopology(data);
    } catch (error) {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      const text = el("text", { x: 200, y: 60, "text-anchor": "middle", class: "map-node__meta" }, svg);
      text.textContent = `Gagal memuat topologi: ${error.message}`;
      svg.setAttribute("viewBox", "0 0 400 120");
    }
  }

  mapGroup.addEventListener("change", loadMaps);

  /* =====================================================
     Boot
     ===================================================== */

  whenReady(() => {
    const role = (window.__nmsMe || {}).role;
    if (role !== "admin" && role !== "operator") {
      const add = document.getElementById("host-add");
      if (add) add.hidden = true;
    }
    activateView();
    loadGroupOptions();
    loadHostStats();
    // Pengaturan (interval default) — opsional.
    window.apiFetch("/api/settings").then((res) => { window.__nmsSettings = res.settings || {}; }).catch(() => {});
    window.NMSStore.startAutoRefresh(() => {
      if (current === "problems") loadProblems();
      else if (current === "hosts") { loadHosts(); loadHostStats(); }
      else if (current === "latest-data") loadLatest();
      else if (current === "maps") loadMaps();
    }, 15000);
    window.addEventListener("nms:devices-changed", () => {
      window.NMSStore.invalidate();
      activateView();
    });
  });
})();
