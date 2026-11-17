/* Halaman Fiber Optik: port DDM, link & redaman, pengukuran, kalkulator, pengaturan.
 * Semua nilai dari API nyata; tanpa data tampil "—" / empty state, tidak pernah angka karangan.
 */
(function () {
  "use strict";

  const fmt = window.NMSFormat;
  const escapeHtml = fmt.escapeHtml;
  const apiFetch = window.apiFetch;

  /* ---------- util ---------- */

  function fmtNum(value, digits, unit) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    return `${Number(value).toFixed(digits == null ? 2 : digits)}${unit ? ` ${unit}` : ""}`;
  }
  const fmtDbm = (v) => fmtNum(v, 2, "dBm");
  const fmtDb = (v) => fmtNum(v, 2, "dB");
  const fmtTemp = (v) => fmtNum(v, 1, "°C");
  const fmtVolt = (v) => fmtNum(v, 3, "V");
  const fmtMa = (v) => fmtNum(v, 2, "mA");

  const AVAIL_OK = "avail avail--ok";
  const AVAIL_WARN = "avail avail--warn";
  const AVAIL_DOWN = "avail avail--down";
  const AVAIL_IDLE = "avail";

  const PORT_STATUS_META = {
    ok: { cls: AVAIL_OK, label: "Normal" },
    warn: { cls: AVAIL_WARN, label: "Waspada" },
    crit: { cls: AVAIL_DOWN, label: "Kritis" },
    los: { cls: AVAIL_DOWN, label: "LOS" },
    tx_fault: { cls: AVAIL_DOWN, label: "Tx fault" },
    no_module: { cls: AVAIL_IDLE, label: "Tidak ada modul" },
    no_ddm: { cls: AVAIL_IDLE, label: "DDM tidak didukung" },
    unknown: { cls: AVAIL_IDLE, label: "Tanpa profil" },
  };

  const LINK_STATUS_META = {
    ok: { cls: AVAIL_OK, label: "Normal" },
    warn: { cls: AVAIL_WARN, label: "Waspada" },
    crit: { cls: AVAIL_DOWN, label: "Kritis" },
    unknown: { cls: AVAIL_IDLE, label: "Tidak diketahui" },
  };

  function role() {
    return (window.__nmsMe && window.__nmsMe.role) || "viewer";
  }
  function canOperate() {
    return role() === "admin" || role() === "operator";
  }
  function isAdmin() {
    return role() === "admin";
  }

  function emptyRow(colspan, text) {
    return `<tr class="empty-row"><td colspan="${colspan}">${escapeHtml(text)}</td></tr>`;
  }

  function showError(el, message) {
    el.textContent = message || "Gagal memuat data.";
    el.hidden = false;
  }

  function toast(message) {
    window.NMSUi.toast(message);
  }

  /** Grafik garis SVG (pendekatan yang sama dengan halaman detail perangkat). */
  function renderLineChart(svgEl, lines, opts) {
    const W = 640, H = 190, PAD = 18;
    const titleText = (svgEl.querySelector("title") || {}).textContent || "Grafik";
    svgEl.innerHTML = "";
    const allValues = lines.flatMap((line) => line.values).filter((v) => v != null);
    const n = lines.reduce((acc, line) => Math.max(acc, line.values.length), 0);
    if (n < 2 || allValues.length < 2) {
      svgEl.innerHTML =
        `<title>${escapeHtml(titleText)}</title><text class="chart__empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">Belum cukup data untuk grafik.</text>`;
      return null;
    }
    const min = opts && opts.min != null ? opts.min : Math.min(...allValues);
    let max = opts && opts.max != null ? opts.max : Math.max(...allValues);
    if (max - min < 1e-9) max = min + 1;
    const xAt = (index) => PAD + ((W - 2 * PAD) * index) / (n - 1);
    const yAt = (value) => PAD + (H - 2 * PAD) * (1 - (value - min) / (max - min));

    const parts = [`<title>${escapeHtml(titleText)}</title>`];
    [0, 0.5, 1].forEach((fraction) => {
      const y = yAt(min + (max - min) * fraction);
      parts.push(`<line class="chart__grid" x1="${PAD}" y1="${y.toFixed(1)}" x2="${W - PAD}" y2="${y.toFixed(1)}" />`);
    });
    if (opts && opts.hlines) {
      for (const hline of opts.hlines) {
        if (hline.value == null || hline.value < min || hline.value > max) continue;
        const y = yAt(hline.value);
        parts.push(`<line class="chart__threshold ${escapeHtml(hline.cls || "")}" x1="${PAD}" y1="${y.toFixed(1)}" x2="${W - PAD}" y2="${y.toFixed(1)}" />`);
      }
    }
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
    const valid = (values || []).filter((v) => v != null);
    if (valid.length === 0) return "min — · rata-rata — · maks —";
    const min = Math.min(...valid);
    const max = Math.max(...valid);
    const avg = valid.reduce((sum, v) => sum + v, 0) / valid.length;
    return `Min ${format(min)} · Rata-rata ${format(avg)} · Maks ${format(max)}`;
  }

  /* ---------- routing & views ---------- */

  const VIEWS = {
    ringkasan: { title: "Ringkasan", desc: "Kondisi link fiber dari daya optik transceiver (DDM)." },
    ports: { title: "Port optik", desc: "Daya DDM per port optik (nilai nyata, “—” bila tidak terukur)." },
    links: { title: "Link & redaman", desc: "Anggaran rancangan vs redaman terukur per link." },
    measurements: { title: "Pengukuran manual", desc: "Catatan OTDR / power meter lapangan per link." },
    calculator: { title: "Kalkulator", desc: "Anggaran link budget interaktif." },
    settings: { title: "Pengaturan fiber", desc: "Parameter redaman & profil optik (khusus admin)." },
  };

  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");
  const views = document.querySelectorAll(".view");

  function parseHash() {
    const raw = window.location.hash.replace(/^#\/?/, "");
    const parts = raw.split("/");
    return { view: parts[0] || "ringkasan", param: parts[1] || null };
  }

  function switchView(name, param) {
    const hash = param ? `#${name}/${param}` : `#${name}`;
    if (window.location.hash !== hash) {
      window.location.hash = hash;
      return; // hashchange memicu aktivasi
    }
    activateView();
  }

  function activateView() {
    let { view, param } = parseHash();
    if (!VIEWS[view]) view = "ringkasan";
    if (view === "settings" && !isAdmin()) view = "ringkasan";
    views.forEach((section) => {
      section.hidden = section.getAttribute("data-view") !== view;
    });
    viewTitle.textContent = VIEWS[view].title;
    viewDesc.textContent = VIEWS[view].desc;
    const loader = VIEW_LOADERS[view];
    if (loader) loader(param);
  }

  /* ---------- cache & data ---------- */

  let devicesCache = null;
  let linksCache = [];
  let portsCache = [];
  let lastCalc = null;
  let currentLinkId = null;
  let currentPortId = null;
  let currentMeasurementId = null;
  let currentPortRange = "24h";
  let currentLinkRange = "30d";
  let currentMeasurementLinkId = null;

  async function loadDevicesOnce() {
    if (devicesCache) return devicesCache;
    const data = await apiFetch("/api/devices?per_page=100");
    devicesCache = data.devices || [];
    return devicesCache;
  }

  async function fillDeviceSelects() {
    const devices = await loadDevicesOnce();
    const selects = [document.getElementById("port-device"), document.getElementById("link-device-a"), document.getElementById("link-device-b")];
    for (const select of selects) {
      if (!select) continue;
      const keepFirst = select.id === "port-device" ? '<option value="">Semua perangkat</option>' : null;
      if (keepFirst != null) {
        select.innerHTML = keepFirst + devices.map((d) => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join("");
      } else if (select.id === "link-device-b") {
        select.innerHTML =
          '<option value="">Bukan perangkat terpantau (isi label)</option>' +
          devices.map((d) => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join("");
      } else {
        select.innerHTML =
          '<option value="">Pilih perangkat…</option>' +
          devices.map((d) => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join("");
      }
    }
  }

  async function fillPortDatalist(deviceId, datalistId) {
    const datalist = document.getElementById(datalistId);
    if (!datalist) return;
    datalist.innerHTML = "";
    if (!deviceId) return;
    try {
      const data = await apiFetch(`/api/devices/${deviceId}/interfaces`);
      datalist.innerHTML = (data.interfaces || [])
        .map((iface) => `<option value="${escapeHtml(iface.if_name)}"></option>`)
        .join("");
    } catch {
      /* datalist bersifat membantu — gagal memuat tidak menghalangi input manual */
    }
  }

  /* ---------- Ringkasan ---------- */

  async function loadRingkasan() {
    try {
      const summary = await apiFetch("/api/optical/summary");
      document.getElementById("fib-ports").textContent = String(summary.ports_total ?? 0);
      document.getElementById("fib-ports-meta").textContent =
        `${summary.ports_ddm ?? 0} dengan DDM · ${summary.ports_with_module ?? 0} modul terpasang`;
      document.getElementById("fib-normal").textContent = String(summary.links_normal ?? 0);
      document.getElementById("fib-warn").textContent = String(summary.links_warn ?? 0);
      document.getElementById("fib-crit").textContent = String(summary.links_crit ?? 0);
      document.getElementById("fib-total").textContent = String(summary.links_total ?? 0);
      document.getElementById("fib-avg-rx").textContent = summary.avg_rx_dbm == null ? "—" : fmtDbm(summary.avg_rx_dbm);
      document.getElementById("fib-max-att").textContent =
        summary.max_attenuation_db == null ? "—" : fmtDb(summary.max_attenuation_db);

      const rows = summary.problem_links || [];
      const tbody = document.getElementById("fib-problem-rows");
      const count = document.getElementById("fib-problem-count");
      if (rows.length === 0) {
        tbody.innerHTML = emptyRow(6, summary.links_total
          ? "Tidak ada link bermasalah. Semua link yang terpantau memenuhi ambang."
          : "Belum ada link fiber terdaftar. Tambahkan lewat tab Link & redaman.");
        count.textContent = summary.links_total ? "0 link bermasalah" : "Belum ada link";
        return;
      }
      count.textContent = `${rows.length} link bermasalah`;
      tbody.innerHTML = rows
        .map((row) => {
          const meta = LINK_STATUS_META[row.status] || LINK_STATUS_META.unknown;
          return (
            `<tr><td><a class="linklike" href="#links/${row.id}">${escapeHtml(row.name)}</a></td>` +
            `<td>${escapeHtml(row.device_a_name)} · ${escapeHtml(row.port_a)}</td>` +
            `<td>${escapeHtml(row.end_b)}</td>` +
            `<td class="mono">${fmtDb(row.attenuation_ab_db)}</td>` +
            `<td class="mono">${fmtDb(row.margin_b_db)}</td>` +
            `<td><span class="${meta.cls}">${escapeHtml(meta.label)}</span></td></tr>`
          );
        })
        .join("");
    } catch (error) {
      showError(document.getElementById("fib-problem-count"), error.message || "Gagal memuat ringkasan fiber.");
    }
  }

  /* ---------- Port optik ---------- */

  const portDevice = document.getElementById("port-device");
  const portStatus = document.getElementById("port-status");
  const portRows = document.getElementById("port-rows");
  const portCount = document.getElementById("port-count");
  const portDetail = document.getElementById("port-detail");
  const portDetailSub = document.getElementById("port-detail-sub");
  const portThresholds = document.getElementById("port-thresholds");
  const portChartSummary = document.getElementById("port-chart-summary");

  function rxBarHtml(rx, status) {
    // Skala tampilan tetap: -30..+3 dBm (batas wajar SFP). Warna mengikuti status port.
    const min = -30, max = 3;
    const pct = rx == null ? 0 : Math.max(0, Math.min(100, ((rx - min) / (max - min)) * 100));
    const cls = status === "crit" || status === "los" ? "rxbar__fill--crit"
      : status === "warn" ? "rxbar__fill--warn"
      : rx == null ? "rxbar__fill--unknown"
      : "";
    return (
      `<span class="rxbar-cell"><span class="rxbar" role="img" aria-label="Rx ${escapeHtml(fmtDbm(rx))}">` +
      `<span class="rxbar__fill ${cls}" style="width:${pct.toFixed(1)}%"></span></span>` +
      `<span class="mono">${escapeHtml(fmtDbm(rx))}</span></span>`
    );
  }

  function moduleBrief(module) {
    if (!module) return "Tidak ada modul";
    const parts = [module.vendor, module.part_no].filter(Boolean);
    const wl = module.wavelength_nm ? `${module.wavelength_nm} nm` : null;
    if (wl) parts.push(wl);
    return parts.length ? parts.join(" · ") : "Modul (info tidak tersedia)";
  }

  async function loadPorts() {
    try {
      await fillDeviceSelects();
      const params = new URLSearchParams();
      if (portDevice.value) params.set("device_id", portDevice.value);
      if (portStatus.value) params.set("status", portStatus.value);
      const data = await apiFetch(`/api/optical/ports?${params.toString()}`);
      portsCache = data.ports || [];
      portCount.textContent = `${portsCache.length} port optik`;
      if (portsCache.length === 0) {
        portRows.innerHTML = emptyRow(10, "Belum ada port optik terdeteksi. Pastikan perangkat MikroTik memiliki modul SFP dengan DDM dan collector API aktif.");
        return;
      }
      portRows.innerHTML = portsCache
        .map((port) => {
          const meta = PORT_STATUS_META[port.status] || PORT_STATUS_META.unknown;
          const detailId = port.id;
          const detailBtn = detailId
            ? `<button class="btn btn--ghost btn--sm" type="button" data-port-detail="${detailId}">Riwayat</button>`
            : "—";
          return (
            `<tr><td>${escapeHtml(port.device_name)}</td>` +
            `<td class="mono">${escapeHtml(port.if_name)}</td>` +
            `<td>${escapeHtml(moduleBrief(port.module))}</td>` +
            `<td>${rxBarHtml(port.rx_dbm, port.status)}</td>` +
            `<td class="mono">${escapeHtml(fmtDbm(port.tx_dbm))}</td>` +
            `<td class="mono">${escapeHtml(fmtTemp(port.temp_c))}</td>` +
            `<td class="mono">${escapeHtml(fmtVolt(port.voltage_v))}</td>` +
            `<td class="mono">${escapeHtml(fmtMa(port.bias_ma))}</td>` +
            `<td><span class="${meta.cls}">${escapeHtml(meta.label)}</span></td>` +
            `<td>${detailBtn}</td></tr>`
          );
        })
        .join("");
    } catch (error) {
      portCount.textContent = "Gagal memuat";
      portRows.innerHTML = emptyRow(10, error.message || "Gagal memuat port optik.");
    }
  }

  async function loadPortDetail(portId) {
    currentPortId = portId;
    const port = portsCache.find((item) => item.id === portId);
    portDetail.hidden = false;
    portDetailSub.textContent = port
      ? `${port.device_name} · ${port.if_name} · ${moduleBrief(port.module)}`
      : `Port #${portId}`;
    try {
      const data = await apiFetch(`/api/optical/ports/${portId}/history?range=${encodeURIComponent(currentPortRange)}`);
      const points = data.points || [];
      const rxValues = points.map((point) => point.rx_dbm);
      const txValues = points.map((point) => point.tx_dbm);
      const thresholds = data.thresholds || {};
      const hlines = [];
      if (thresholds.rx_warn_low != null) hlines.push({ value: thresholds.rx_warn_low, cls: "chart__threshold--warn" });
      if (thresholds.rx_crit_low != null) hlines.push({ value: thresholds.rx_crit_low, cls: "chart__threshold--crit" });
      const chartEl = document.getElementById("port-chart");
      renderLineChart(chartEl, [{ values: rxValues }, { values: txValues }], { noArea: true, hlines });
      portChartSummary.textContent =
        `Sumber ${data.source === "raw" ? "pembacaan mentah" : "rata-rata per jam"} · ` +
        chartSummary(rxValues, (v) => fmtDbm(v));
      if (thresholds.rx_warn_low != null || thresholds.rx_crit_low != null) {
        portThresholds.textContent =
          `Garis ambang profil: waspada < ${fmtDbm(thresholds.rx_warn_low)} · kritis < ${fmtDbm(thresholds.rx_crit_low)}. ` +
          "Suhu dan voltase mengikuti pembacaan DDM terakhir.";
      } else {
        portThresholds.textContent = "Port belum tergabung link/profil — ambang tidak digambar. Nilai tetap ditampilkan.";
      }
      portDetail.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      portChartSummary.textContent = error.message || "Gagal memuat riwayat port.";
    }
  }

  /* ---------- Link & redaman ---------- */

  const linkRows = document.getElementById("link-rows");
  const linkCount = document.getElementById("link-count");
  const linkSearch = document.getElementById("link-search");
  const linkAddBtn = document.getElementById("link-add");
  const linkFormPanel = document.getElementById("link-form-panel");
  const linkFormNote = document.getElementById("link-form-note");
  const linkDetail = document.getElementById("link-detail");
  const linkDetailSub = document.getElementById("link-detail-sub");
  const linkVerdict = document.getElementById("link-verdict");
  const linkSteps = document.getElementById("link-steps");
  const linkBaseline = document.getElementById("link-baseline");
  const linkChartSummary = document.getElementById("link-chart-summary");
  let editingLinkId = null;

  const SPLITTER_OPTIONS = ["", "1:2", "1:4", "1:8", "1:16", "1:32", "1:64"];

  function linkEndpointLabel(link, side) {
    if (side === "a") {
      return `${link.device_a_name || "?"} · ${link.port_a}`;
    }
    if (link.device_b_id) return `${link.device_b_name || "?"} · ${link.port_b}`;
    return link.end_b_label || "—";
  }

  function worstMargin(metrics) {
    const values = [metrics.margin_a_db, metrics.margin_b_db].filter((v) => v != null);
    return values.length ? Math.min(...values) : null;
  }

  function worstDeviation(metrics) {
    const values = [metrics.deviation_ab_db, metrics.deviation_ba_db].filter((v) => v != null);
    return values.length ? Math.max(...values) : null;
  }

  function renderLinks() {
    const query = (linkSearch.value || "").trim().toLowerCase();
    const visible = linksCache.filter((link) => {
      if (!query) return true;
      const haystack = `${link.name} ${link.device_a_name} ${link.port_a} ${link.device_b_name || ""} ${link.port_b || ""} ${link.end_b_label || ""} ${link.fiber_type}`.toLowerCase();
      return haystack.includes(query);
    });
    linkCount.textContent = `${visible.length} dari ${linksCache.length} link`;
    if (visible.length === 0) {
      linkRows.innerHTML = emptyRow(10, query
        ? "Tidak ada link yang cocok dengan pencarian."
        : "Belum ada link fiber terdaftar. Klik “Tambah link” untuk mendaftarkan rentang fiber.");
      return;
    }
    linkRows.innerHTML = visible
      .map((link) => {
        const metrics = link.metrics || {};
        const meta = LINK_STATUS_META[link.status] || LINK_STATUS_META.unknown;
        const budget = metrics.budget_ab_db;
        const deviation = worstDeviation(metrics);
        const margin = worstMargin(metrics);
        const actions = canOperate()
          ? `<button class="btn btn--ghost btn--sm" type="button" data-link-edit="${link.id}">Ubah</button>` +
            `<button class="btn btn--ghost btn--sm" type="button" data-link-delete="${link.id}">Hapus</button>`
          : "";
        return (
          `<tr><td><a class="linklike" href="#links/${link.id}">${escapeHtml(link.name)}</a></td>` +
          `<td>${escapeHtml(linkEndpointLabel(link, "a"))} → ${escapeHtml(linkEndpointLabel(link, "b"))}</td>` +
          `<td class="mono">${escapeHtml(fmtNum(link.length_km, link.length_km < 1 ? 3 : 2, "km"))}</td>` +
          `<td class="mono">${fmtDb(metrics.attenuation_ab_db)}</td>` +
          `<td class="mono">${fmtDb(metrics.attenuation_ba_db)}</td>` +
          `<td class="mono">${budget == null ? "—" : fmtDb(budget)}</td>` +
          `<td class="mono">${deviation == null ? "—" : fmtDb(deviation)}</td>` +
          `<td class="mono">${margin == null ? "—" : fmtDb(margin)}</td>` +
          `<td><span class="${meta.cls}">${escapeHtml(meta.label)}</span></td>` +
          `<td class="row-actions">${actions}</td></tr>`
        );
      })
      .join("");
  }

  async function loadLinks() {
    try {
      const data = await apiFetch("/api/fiber-links");
      linksCache = data.links || [];
      renderLinks();
    } catch (error) {
      linkCount.textContent = "Gagal memuat";
      linkRows.innerHTML = emptyRow(10, error.message || "Gagal memuat link fiber.");
    }
  }

  function syncLinkEndBFields() {
    const monitored = !!document.getElementById("link-device-b").value;
    document.getElementById("link-port-b-field").hidden = !monitored;
    document.getElementById("link-end-b-field").hidden = monitored;
  }

  function openLinkForm(link) {
    editingLinkId = link ? link.id : null;
    document.getElementById("link-form-title").textContent = link ? `Ubah link ${link.name}` : "Tambah link fiber";
    document.getElementById("link-name").value = link ? link.name : "";
    document.getElementById("link-length").value = link ? link.length_km : "";
    document.getElementById("link-device-a").value = link ? String(link.device_a_id) : "";
    document.getElementById("link-port-a").value = link ? link.port_a : "";
    document.getElementById("link-device-b").value = link && link.device_b_id ? String(link.device_b_id) : "";
    document.getElementById("link-port-b").value = link && link.port_b ? link.port_b : "";
    document.getElementById("link-end-b-label").value = link && link.end_b_label ? link.end_b_label : "";
    document.getElementById("link-fiber-type").value = link ? link.fiber_type : "SM G.652D";
    document.getElementById("link-wavelength").value = link ? link.wavelength_nm : 1310;
    document.getElementById("link-wavelength-b").value = link && link.wavelength_b_nm ? link.wavelength_b_nm : "";
    document.getElementById("link-splices").value = link ? link.splices : 0;
    document.getElementById("link-connectors").value = link ? link.connectors : 2;
    document.getElementById("link-splitter").value = link && link.splitter_ratio ? link.splitter_ratio : "";
    document.getElementById("link-profile").value = link && link.profile_id ? String(link.profile_id) : "";
    document.getElementById("link-notes").value = link ? link.notes || "" : "";
    syncLinkEndBFields();
    fillPortDatalist(document.getElementById("link-device-a").value, "link-ports-a");
    fillPortDatalist(document.getElementById("link-device-b").value, "link-ports-b");
    linkFormNote.hidden = true;
    linkFormPanel.hidden = false;
    linkFormPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    document.getElementById("link-name").focus({ preventScroll: true });
  }

  async function fillProfileSelect() {
    const select = document.getElementById("link-profile");
    try {
      const data = await apiFetch("/api/optical/profiles");
      const profiles = data.profiles || [];
      select.innerHTML =
        '<option value="">Tanpa profil (ambang tidak dihitung)</option>' +
        profiles.map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
    } catch {
      select.innerHTML = '<option value="">Gagal memuat profil</option>';
    }
  }

  async function submitLinkForm(event) {
    event.preventDefault();
    const deviceB = document.getElementById("link-device-b").value;
    const payload = {
      name: document.getElementById("link-name").value.trim(),
      device_a_id: Number(document.getElementById("link-device-a").value),
      port_a: document.getElementById("link-port-a").value.trim(),
      device_b_id: deviceB ? Number(deviceB) : null,
      port_b: deviceB ? document.getElementById("link-port-b").value.trim() || null : null,
      end_b_label: deviceB ? null : document.getElementById("link-end-b-label").value.trim(),
      length_km: Number(document.getElementById("link-length").value),
      fiber_type: document.getElementById("link-fiber-type").value.trim() || "SM G.652D",
      wavelength_nm: Number(document.getElementById("link-wavelength").value || 1310),
      wavelength_b_nm: document.getElementById("link-wavelength-b").value ? Number(document.getElementById("link-wavelength-b").value) : null,
      splices: Number(document.getElementById("link-splices").value || 0),
      connectors: Number(document.getElementById("link-connectors").value || 0),
      splitter_ratio: document.getElementById("link-splitter").value || null,
      profile_id: document.getElementById("link-profile").value ? Number(document.getElementById("link-profile").value) : null,
      notes: document.getElementById("link-notes").value.trim(),
    };
    linkFormNote.hidden = true;
    try {
      const url = editingLinkId ? `/api/fiber-links/${editingLinkId}` : "/api/fiber-links";
      await apiFetch(url, { method: editingLinkId ? "PUT" : "POST", body: payload });
      linkFormPanel.hidden = true;
      toast(editingLinkId ? "Link fiber diperbarui." : "Link fiber ditambahkan.");
      await loadLinks();
    } catch (error) {
      showError(linkFormNote, error.message || "Gagal menyimpan link.");
    }
  }

  async function openLinkDetail(linkId) {
    currentLinkId = linkId;
    linkDetail.hidden = false;
    try {
      const data = await apiFetch(`/api/fiber-links/${linkId}?range=${encodeURIComponent(currentLinkRange)}`);
      const link = data.link;
      const evaluation = data.evaluation || {};
      const metrics = evaluation;
      linkDetailSub.textContent =
        `${link.name} · ${linkEndpointLabel(link, "a")} → ${linkEndpointLabel(link, "b")} · ${link.fiber_type} · ${link.length_km} km · λ ${evaluation.wavelength_ab_nm ?? link.wavelength_nm} nm` +
        (link.wavelength_b_nm ? ` / λ B→A ${link.wavelength_b_nm} nm (BiDi)` : "");

      const status = evaluation.status || "unknown";
      const meta = LINK_STATUS_META[status] || LINK_STATUS_META.unknown;
      const marginMin = worstMargin(metrics);
      const deviationWorst = worstDeviation(metrics);
      linkVerdict.innerHTML =
        `<span class="${meta.cls}">${escapeHtml(meta.label)}</span> · ` +
        `Redaman A→B <b>${escapeHtml(fmtDb(metrics.attenuation_ab_db))}</b> · B→A <b>${escapeHtml(fmtDb(metrics.attenuation_ba_db))}</b> · ` +
        `Asimetri <b>${escapeHtml(fmtDb(metrics.asymmetry_db))}</b> · ` +
        `Deviasi <b>${deviationWorst == null ? "—" : escapeHtml(fmtDb(deviationWorst))}</b> · ` +
        `Margin min <b>${marginMin == null ? "—" : escapeHtml(fmtDb(marginMin))}</b>`;

      linkSteps.innerHTML = (evaluation.steps || [])
        .map((step) => {
          const value = step.ok
            ? `<span class="calc-steps__value">${escapeHtml(step.value)}</span>`
            : `<span class="calc-steps__missing">${escapeHtml(step.reason || "tidak bisa dihitung")}</span>`;
          const detail = (step.detail_steps || [])
            .map((d) => `<li>${escapeHtml(d.label)} = <span class="calc-steps__value">${escapeHtml(fmtDb(d.value_db))}</span></li>`)
            .join("");
          return `<li>${escapeHtml(step.label)} ${value}${detail ? `<ul class="calc-steps">${detail}</ul>` : ""}</li>`;
        })
        .join("");

      const history = data.history || [];
      const ab = history.map((row) => row.attenuation_ab_db ?? null);
      const ba = history.map((row) => row.attenuation_ba_db ?? null);
      renderLineChart(document.getElementById("link-chart"), [{ values: ab }, { values: ba }], { noArea: true });
      linkChartSummary.textContent =
        `${history.length} titik per jam · ` + chartSummary(ab.filter((v) => v != null), (v) => fmtDb(v));

      const baselineA = data.baseline_rx_a_dbm;
      const baselineB = data.baseline_rx_b_dbm;
      const trendA = data.trend_rx_a_db_per_week;
      const trendB = data.trend_rx_b_db_per_week;
      linkBaseline.textContent =
        `Baseline Rx (median ${data.baseline_samples_a || 0} jam, jendela 7 hari): A ${fmtDbm(baselineA)} · B ${fmtDbm(baselineB)} — penurunan > 3 dB dari baseline memicu problem fiber_drop. ` +
        `Tren (regresi 14 hari): A ${trendA == null ? "—" : `${trendA.toFixed(2)} dB/minggu`} · B ${trendB == null ? "—" : `${trendB.toFixed(2)} dB/minggu`} — memburuk ≥ 0,5 dB/minggu memicu problem fiber_trend.`;
      linkDetail.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      showError(linkVerdict, error.message || "Gagal memuat detail link.");
    }
  }

  /* ---------- Pengukuran manual ---------- */

  const measLink = document.getElementById("meas-link");
  const measAddBtn = document.getElementById("meas-add");
  const measRows = document.getElementById("meas-rows");
  const measFormPanel = document.getElementById("meas-form-panel");
  const measFormNote = document.getElementById("meas-form-note");
  const measLiveAb = document.getElementById("meas-live-ab");
  const measLiveBa = document.getElementById("meas-live-ba");
  let currentMeasurements = [];

  async function fillMeasurementLinks() {
    try {
      const data = await apiFetch("/api/fiber-links");
      const links = data.links || [];
      const keep = measLink.value;
      measLink.innerHTML =
        '<option value="">Pilih link…</option>' +
        links.map((link) => `<option value="${link.id}">${escapeHtml(link.name)}</option>`).join("");
      if (keep && links.some((link) => String(link.id) === keep)) measLink.value = keep;
      return links.length;
    } catch {
      measLink.innerHTML = '<option value="">Gagal memuat daftar link</option>';
      return 0;
    }
  }

  async function loadMeasurements() {
    const linkId = measLink.value;
    currentMeasurementLinkId = linkId ? Number(linkId) : null;
    const hasLink = !!currentMeasurementLinkId;
    measAddBtn.disabled = !hasLink || !canOperate();
    if (!hasLink) {
      measRows.innerHTML = emptyRow(8, "Pilih link untuk melihat/mencatat pengukuran.");
      measLiveAb.textContent = "—";
      measLiveBa.textContent = "—";
      return;
    }
    try {
      const [measData, detailData] = await Promise.all([
        apiFetch(`/api/fiber-links/${currentMeasurementLinkId}/measurements`),
        apiFetch(`/api/fiber-links/${currentMeasurementLinkId}`),
      ]);
      currentMeasurements = measData.measurements || [];
      const metrics = (detailData.evaluation || {});
      measLiveAb.textContent = fmtDb(metrics.attenuation_ab_db);
      measLiveBa.textContent = fmtDb(metrics.attenuation_ba_db);
      if (currentMeasurements.length === 0) {
        measRows.innerHTML = emptyRow(8, "Belum ada pengukuran untuk link ini. Catat hasil OTDR/power meter sebagai pembanding.");
        return;
      }
      measRows.innerHTML = currentMeasurements
        .map((row) => {
          const methodLabel = row.method === "otdr" ? "OTDR" : "Power meter";
          const actions = canOperate()
            ? `<button class="btn btn--ghost btn--sm" type="button" data-meas-edit="${row.id}">Ubah</button>` +
              `<button class="btn btn--ghost btn--sm" type="button" data-meas-delete="${row.id}">Hapus</button>`
            : "";
          return (
            `<tr><td>${escapeHtml(String(row.measured_at).slice(0, 10))}</td>` +
            `<td>${escapeHtml(row.technician || "—")}</td>` +
            `<td>${escapeHtml(row.point || "—")}</td>` +
            `<td>${escapeHtml(methodLabel)}</td>` +
            `<td class="mono">${fmtDb(row.value_db)}</td>` +
            `<td class="mono">${fmtDbm(row.value_dbm)}</td>` +
            `<td>${escapeHtml(row.notes || "—")}</td>` +
            `<td class="row-actions">${actions}</td></tr>`
          );
        })
        .join("");
    } catch (error) {
      measRows.innerHTML = emptyRow(8, error.message || "Gagal memuat pengukuran.");
    }
  }

  function openMeasurementForm(row) {
    if (!currentMeasurementLinkId) return;
    currentMeasurementId = row ? row.id : null;
    document.getElementById("meas-form-title").textContent = row ? "Ubah pengukuran" : "Catat pengukuran";
    document.getElementById("meas-measured-at").value = row ? String(row.measured_at).slice(0, 10) : "";
    document.getElementById("meas-technician").value = row ? row.technician || "" : "";
    document.getElementById("meas-point").value = row ? row.point || "" : "";
    document.getElementById("meas-method").value = row ? row.method : "otdr";
    document.getElementById("meas-value-db").value = row && row.value_db != null ? row.value_db : "";
    document.getElementById("meas-value-dbm").value = row && row.value_dbm != null ? row.value_dbm : "";
    document.getElementById("meas-notes").value = row ? row.notes || "" : "";
    measFormNote.hidden = true;
    measFormPanel.hidden = false;
    measFormPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    document.getElementById("meas-measured-at").focus({ preventScroll: true });
  }

  async function submitMeasurementForm(event) {
    event.preventDefault();
    if (!currentMeasurementLinkId) return;
    const payload = {
      measured_at: document.getElementById("meas-measured-at").value,
      technician: document.getElementById("meas-technician").value.trim(),
      point: document.getElementById("meas-point").value.trim(),
      method: document.getElementById("meas-method").value,
      value_db: document.getElementById("meas-value-db").value === "" ? null : Number(document.getElementById("meas-value-db").value),
      value_dbm: document.getElementById("meas-value-dbm").value === "" ? null : Number(document.getElementById("meas-value-dbm").value),
      notes: document.getElementById("meas-notes").value.trim(),
    };
    measFormNote.hidden = true;
    try {
      const base = `/api/fiber-links/${currentMeasurementLinkId}/measurements`;
      if (currentMeasurementId) {
        await apiFetch(`${base}/${currentMeasurementId}`, { method: "PUT", body: payload });
      } else {
        await apiFetch(base, { method: "POST", body: payload });
      }
      measFormPanel.hidden = true;
      toast(currentMeasurementId ? "Pengukuran diperbarui." : "Pengukuran tercatat.");
      await loadMeasurements();
    } catch (error) {
      showError(measFormNote, error.message || "Gagal menyimpan pengukuran.");
    }
  }

  async function loadMeasurementsView() {
    const count = await fillMeasurementLinks();
    if (count === 0) {
      measRows.innerHTML = emptyRow(8, "Belum ada link fiber terdaftar. Tambahkan link di tab Link & redaman terlebih dahulu.");
      measAddBtn.disabled = true;
      return;
    }
    await loadMeasurements();
  }

  /* ---------- Kalkulator ---------- */

  const calcForm = document.getElementById("calc-form");
  const calcNote = document.getElementById("calc-note");
  const calcEmpty = document.getElementById("calc-empty");
  const calcOutput = document.getElementById("calc-output");
  const calcSaveBtn = document.getElementById("calc-save-link");

  async function submitCalcForm(event) {
    event.preventDefault();
    calcNote.hidden = true;
    const tx = document.getElementById("calc-tx").value;
    const sensitivity = document.getElementById("calc-sensitivity").value;
    const payload = {
      length_km: Number(document.getElementById("calc-length").value),
      wavelength_nm: Number(document.getElementById("calc-wavelength").value),
      splices: Number(document.getElementById("calc-splices").value || 0),
      connectors: Number(document.getElementById("calc-connectors").value || 0),
      splitter_ratio: document.getElementById("calc-splitter").value || null,
      tx_power_dbm: tx === "" ? null : Number(tx),
      rx_sensitivity_dbm: sensitivity === "" ? null : Number(sensitivity),
    };
    try {
      const result = await apiFetch("/api/fiber/calc", { method: "POST", body: payload });
      lastCalc = { payload, result };
      calcEmpty.hidden = true;
      calcOutput.hidden = false;
      document.getElementById("calc-total").textContent = fmtDb(result.budget_db);
      document.getElementById("calc-alpha").textContent = `${result.alpha_db_per_km} dB/km`;
      document.getElementById("calc-rx").textContent =
        result.rx_estimate_dbm == null ? "—" : fmtDbm(result.rx_estimate_dbm);
      document.getElementById("calc-margin").textContent =
        result.margin_db == null ? "—" : fmtDb(result.margin_db);
      document.getElementById("calc-steps").innerHTML = (result.steps || [])
        .map((step) => `<li>${escapeHtml(step.label)}<span class="calc-steps__value">${escapeHtml(fmtDb(step.value_db))}</span></li>`)
        .join("");
      calcSaveBtn.disabled = false;
    } catch (error) {
      calcEmpty.hidden = false;
      calcOutput.hidden = true;
      calcSaveBtn.disabled = true;
      lastCalc = null;
      showError(calcNote, error.message || "Perhitungan gagal.");
    }
  }

  function applyCalcToLinkForm() {
    if (!lastCalc) return;
    switchView("links");
    fillProfileSelect().then(() => fillDeviceSelects()).then(() => {
      openLinkForm(null);
      document.getElementById("link-length").value = lastCalc.payload.length_km;
      document.getElementById("link-wavelength").value = lastCalc.payload.wavelength_nm;
      document.getElementById("link-splices").value = lastCalc.payload.splices;
      document.getElementById("link-connectors").value = lastCalc.payload.connectors;
      document.getElementById("link-splitter").value = lastCalc.payload.splitter_ratio || "";
      toast("Rancangan dari kalkulator disalin ke formulir link. Isi perangkat & port ujung.");
    });
  }

  /* ---------- Pengaturan fiber (admin) ---------- */

  const SETTINGS_FLOAT_IDS = {
    "set-fiber-alpha_1310": "fiber_alpha_1310",
    "set-fiber-alpha_1550": "fiber_alpha_1550",
    "set-fiber-splice_loss": "fiber_splice_loss",
    "set-fiber-connector_loss": "fiber_connector_loss",
    "set-fiber-deviation_warn": "fiber_deviation_warn",
    "set-fiber-deviation_crit": "fiber_deviation_crit",
    "set-fiber-margin_warn": "fiber_margin_warn",
    "set-fiber-margin_crit": "fiber_margin_crit",
    "set-fiber-asymmetry_warn": "fiber_asymmetry_warn",
    "set-fiber-drop_db": "fiber_drop_db",
    "set-fiber-trend_db_per_week": "fiber_trend_db_per_week",
    "set-fiber-hysteresis_db": "fiber_hysteresis_db",
  };
  const SETTINGS_INT_IDS = {
    "set-fiber-drop_baseline_days": "fiber_drop_baseline_days",
    "set-fiber-trend_days": "fiber_trend_days",
    "set-fiber-consec_required": "fiber_consec_required",
    "set-fiber-avg_readings": "fiber_avg_readings",
    "set-retention_optical_hourly_days": "retention_optical_hourly_days",
  };
  const SPLITTER_KEYS = ["1:2", "1:4", "1:8", "1:16", "1:32", "1:64"];

  const settingsNote = document.getElementById("settings-note");
  const profileRows = document.getElementById("profile-rows");
  const profileFormPanel = document.getElementById("profile-form-panel");
  const profileFormNote = document.getElementById("profile-form-note");
  let editingProfileId = null;
  let profilesCache = [];

  function renderSplitterInputs(table) {
    const container = document.getElementById("splitter-inputs");
    container.innerHTML = SPLITTER_KEYS.map((key) => {
      const value = table && table[key] != null ? table[key] : "";
      const id = `splitter-${key.replace(":", "-")}`;
      return (
        `<label class="admin-field"><span>${escapeHtml(key)}</span>` +
        `<input id="${id}" type="number" step="0.1" min="0.5" max="40" value="${escapeHtml(String(value))}" inputmode="decimal" /></label>`
      );
    }).join("");
  }

  async function loadSettings() {
    if (!isAdmin()) return;
    try {
      const [settingsData, profilesData] = await Promise.all([
        apiFetch("/api/optical/settings"),
        apiFetch("/api/optical/profiles"),
      ]);
      const settings = settingsData.settings || {};
      for (const [id, key] of Object.entries(SETTINGS_FLOAT_IDS)) {
        const el = document.getElementById(id);
        if (el && settings[key] != null) el.value = settings[key];
      }
      for (const [id, key] of Object.entries(SETTINGS_INT_IDS)) {
        const el = document.getElementById(id);
        if (el && settings[key] != null) el.value = settings[key];
      }
      renderSplitterInputs(settings.fiber_splitter_losses || {});
      profilesCache = profilesData.profiles || [];
      renderProfiles();
    } catch (error) {
      showError(settingsNote, error.message || "Gagal memuat pengaturan fiber.");
    }
  }

  function renderProfiles() {
    if (profilesCache.length === 0) {
      profileRows.innerHTML = emptyRow(8, "Belum ada profil optik. Tambahkan profil (mis. sesuai datasheet SFP Anda).");
      return;
    }
    profileRows.innerHTML = profilesCache
      .map((profile) => {
        const actions = canOperate()
          ? `<button class="btn btn--ghost btn--sm" type="button" data-profile-edit="${profile.id}">Ubah</button>` +
            `<button class="btn btn--ghost btn--sm" type="button" data-profile-delete="${profile.id}">Hapus</button>`
          : "";
        return (
          `<tr><td><strong>${escapeHtml(profile.name)}</strong>${profile.description ? `<br><span class="cell-sub">${escapeHtml(profile.description)}</span>` : ""}</td>` +
          `<td class="mono">${fmtDbm(profile.rx_warn_low)}</td>` +
          `<td class="mono">${fmtDbm(profile.rx_crit_low)}</td>` +
          `<td class="mono">${fmtDbm(profile.rx_warn_high)}</td>` +
          `<td class="mono">${fmtDbm(profile.rx_crit_high)}</td>` +
          `<td class="mono">${fmtDbm(profile.rx_sensitivity_dbm)}</td>` +
          `<td class="mono">${fmtDbm(profile.rx_overload_dbm)}</td>` +
          `<td class="row-actions">${actions}</td></tr>`
        );
      })
      .join("");
  }

  function openProfileForm(profile) {
    editingProfileId = profile ? profile.id : null;
    document.getElementById("profile-form-title").textContent = profile ? `Ubah profil ${profile.name}` : "Tambah profil optik";
    document.getElementById("profile-name").value = profile ? profile.name : "";
    document.getElementById("profile-description").value = profile ? profile.description || "" : "";
    for (const key of ["rx_warn_low", "rx_crit_low", "rx_warn_high", "rx_crit_high", "temp_warn_c", "temp_crit_c", "rx_sensitivity_dbm", "rx_overload_dbm"]) {
      document.getElementById(`profile-${key}`).value = profile && profile[key] != null ? profile[key] : "";
    }
    profileFormNote.hidden = true;
    profileFormPanel.hidden = false;
    profileFormPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    document.getElementById("profile-name").focus({ preventScroll: true });
  }

  async function submitSettingsForm(event) {
    event.preventDefault();
    settingsNote.hidden = true;
    const payload = {};
    for (const [id, key] of Object.entries(SETTINGS_FLOAT_IDS)) {
      const el = document.getElementById(id);
      if (el && el.value !== "") payload[key] = Number(el.value);
    }
    for (const [id, key] of Object.entries(SETTINGS_INT_IDS)) {
      const el = document.getElementById(id);
      if (el && el.value !== "") payload[key] = Number(el.value);
    }
    const splitter = {};
    let splitterComplete = true;
    for (const key of SPLITTER_KEYS) {
      const el = document.getElementById(`splitter-${key.replace(":", "-")}`);
      if (!el || el.value === "") {
        splitterComplete = false;
        continue;
      }
      splitter[key] = Number(el.value);
    }
    if (splitterComplete) payload.fiber_splitter_losses = splitter;
    try {
      await apiFetch("/api/optical/settings", { method: "PUT", body: payload });
      toast("Pengaturan fiber tersimpan.");
      await loadSettings();
    } catch (error) {
      showError(settingsNote, error.message || "Gagal menyimpan pengaturan.");
    }
  }

  async function submitProfileForm(event) {
    event.preventDefault();
    profileFormNote.hidden = true;
    const payload = {
      name: document.getElementById("profile-name").value.trim(),
      description: document.getElementById("profile-description").value.trim(),
    };
    for (const key of ["rx_warn_low", "rx_crit_low", "rx_warn_high", "rx_crit_high", "temp_warn_c", "temp_crit_c", "rx_sensitivity_dbm", "rx_overload_dbm"]) {
      const value = document.getElementById(`profile-${key}`).value;
      payload[key] = value === "" ? null : Number(value);
    }
    try {
      const url = editingProfileId ? `/api/optical/profiles/${editingProfileId}` : "/api/optical/profiles";
      await apiFetch(url, { method: editingProfileId ? "PUT" : "POST", body: payload });
      profileFormPanel.hidden = true;
      toast(editingProfileId ? "Profil diperbarui." : "Profil ditambahkan.");
      const data = await apiFetch("/api/optical/profiles");
      profilesCache = data.profiles || [];
      renderProfiles();
    } catch (error) {
      showError(profileFormNote, error.message || "Gagal menyimpan profil.");
    }
  }

  /* ---------- init & wiring ---------- */

  const VIEW_LOADERS = {
    ringkasan: loadRingkasan,
    ports: async (param) => {
      await loadPorts();
      if (param && /^\d+$/.test(param)) loadPortDetail(Number(param));
    },
    links: async (param) => {
      await fillDeviceSelects();
      await fillProfileSelect();
      await loadLinks();
      if (param && /^\d+$/.test(param)) openLinkDetail(Number(param));
    },
    measurements: loadMeasurementsView,
    calculator: () => {},
    settings: loadSettings,
  };

  function bindClicks() {
    document.getElementById("refresh").addEventListener("click", () => activateView());

    portDevice.addEventListener("change", loadPorts);
    portStatus.addEventListener("change", loadPorts);
    portRows.addEventListener("click", (event) => {
      const button = event.target.closest("[data-port-detail]");
      if (button) loadPortDetail(Number(button.getAttribute("data-port-detail")));
    });
    document.getElementById("port-detail-close").addEventListener("click", () => {
      portDetail.hidden = true;
    });
    document.getElementById("port-range").addEventListener("click", (event) => {
      const button = event.target.closest("[data-range]");
      if (!button) return;
      currentPortRange = button.getAttribute("data-range");
      document.querySelectorAll("#port-range .seg__btn").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      if (currentPortId != null) loadPortDetail(currentPortId);
    });

    linkSearch.addEventListener("input", renderLinks);
    linkAddBtn.addEventListener("click", async () => {
      await fillDeviceSelects();
      await fillProfileSelect();
      if (!canOperate()) return;
      openLinkForm(null);
    });
    linkRows.addEventListener("click", async (event) => {
      const edit = event.target.closest("[data-link-edit]");
      if (edit) {
        const link = linksCache.find((item) => item.id === Number(edit.getAttribute("data-link-edit")));
        if (link) { fillDeviceSelects().then(fillProfileSelect).then(() => openLinkForm(link)); }
        return;
      }
      const del = event.target.closest("[data-link-delete]");
      if (del) {
        const id = Number(del.getAttribute("data-link-delete"));
        const link = linksCache.find((item) => item.id === id);
        const name = link ? link.name : `#${id}`;
        if (await window.NMSUi.confirm(`Hapus link "${name}"? Masalah fiber terkait link ini juga terhapus.`, {
          title: "Hapus link",
          confirmLabel: "Hapus",
          danger: true,
        })) {
          apiFetch(`/api/fiber-links/${id}`, { method: "DELETE" })
            .then(() => { toast("Link dihapus."); return loadLinks(); })
            .catch((error) => toast(error.message || "Gagal menghapus link."));
        }
        return;
      }
      const row = event.target.closest("[data-link-open]");
      if (row) openLinkDetail(Number(row.getAttribute("data-link-open")));
    });
    document.getElementById("link-device-a").addEventListener("change", (event) => fillPortDatalist(event.target.value, "link-ports-a"));
    document.getElementById("link-device-b").addEventListener("change", (event) => {
      syncLinkEndBFields();
      fillPortDatalist(event.target.value, "link-ports-b");
    });
    document.getElementById("link-cancel").addEventListener("click", () => { linkFormPanel.hidden = true; });
    linkFormPanel.addEventListener("submit", submitLinkForm);
    document.getElementById("link-detail-close").addEventListener("click", () => { linkDetail.hidden = true; });
    document.getElementById("link-range").addEventListener("click", (event) => {
      const button = event.target.closest("[data-range]");
      if (!button) return;
      currentLinkRange = button.getAttribute("data-range");
      document.querySelectorAll("#link-range .seg__btn").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      if (currentLinkId != null) openLinkDetail(currentLinkId);
    });

    measLink.addEventListener("change", loadMeasurements);
    measAddBtn.addEventListener("click", () => openMeasurementForm(null));
    measRows.addEventListener("click", async (event) => {
      const edit = event.target.closest("[data-meas-edit]");
      if (edit) {
        const row = currentMeasurements.find((item) => item.id === Number(edit.getAttribute("data-meas-edit")));
        if (row) openMeasurementForm(row);
        return;
      }
      const del = event.target.closest("[data-meas-delete]");
      if (del) {
        const id = Number(del.getAttribute("data-meas-delete"));
        if (await window.NMSUi.confirm("Hapus catatan pengukuran ini?", {
          title: "Hapus pengukuran",
          confirmLabel: "Hapus",
          danger: true,
        })) {
          apiFetch(`/api/fiber-links/${currentMeasurementLinkId}/measurements/${id}`, { method: "DELETE" })
            .then(() => { toast("Pengukuran dihapus."); return loadMeasurements(); })
            .catch((error) => toast(error.message || "Gagal menghapus pengukuran."));
        }
      }
    });
    document.getElementById("meas-cancel").addEventListener("click", () => { measFormPanel.hidden = true; });
    measFormPanel.addEventListener("submit", submitMeasurementForm);

    calcForm.addEventListener("submit", submitCalcForm);
    calcSaveBtn.addEventListener("click", applyCalcToLinkForm);

    document.getElementById("settings-form").addEventListener("submit", submitSettingsForm);
    document.getElementById("profile-add").addEventListener("click", () => openProfileForm(null));
    profileRows.addEventListener("click", async (event) => {
      const edit = event.target.closest("[data-profile-edit]");
      if (edit) {
        const profile = profilesCache.find((item) => item.id === Number(edit.getAttribute("data-profile-edit")));
        if (profile) openProfileForm(profile);
        return;
      }
      const del = event.target.closest("[data-profile-delete]");
      if (del) {
        const id = Number(del.getAttribute("data-profile-delete"));
        const profile = profilesCache.find((item) => item.id === id);
        if (await window.NMSUi.confirm(`Hapus profil "${profile ? profile.name : id}"?`, {
          title: "Hapus profil",
          confirmLabel: "Hapus",
          danger: true,
        })) {
          apiFetch(`/api/optical/profiles/${id}`, { method: "DELETE" })
            .then(async () => {
              toast("Profil dihapus.");
              const data = await apiFetch("/api/optical/profiles");
              profilesCache = data.profiles || [];
              renderProfiles();
            })
            .catch((error) => toast(error.message || "Gagal menghapus profil."));
        }
      }
    });
    document.getElementById("profile-cancel").addEventListener("click", () => { profileFormPanel.hidden = true; });
    profileFormPanel.addEventListener("submit", submitProfileForm);
  }

  function applyRoleVisibility() {
    const operatorOnly = [linkAddBtn, measAddBtn, document.getElementById("profile-add"), document.getElementById("settings-save")];
    for (const el of operatorOnly) {
      if (el && !canOperate()) el.hidden = true;
    }
    if (!isAdmin()) {
      const settingsNav = document.querySelector('[data-nav-group] a[href^="fiber.html#settings"]');
      if (settingsNav) settingsNav.remove();
    }
  }

  function init() {
    bindClicks();
    if (!window.__nmsMe) {
      window.addEventListener("nms:me-ready", () => {
        applyRoleVisibility();
        activateView();
      }, { once: true });
    } else {
      applyRoleVisibility();
      activateView();
    }
    window.addEventListener("hashchange", activateView);
  }

  init();
})();
