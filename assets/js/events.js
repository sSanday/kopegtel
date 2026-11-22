/* Kejadian: syslog, SNMP trap, aturan, dan status penerima. */
(function () {
  "use strict";

  const { escapeHtml, formatDateTime, parseServerTs } = window.NMSFormat;
  const SEVERITIES = ["information", "warning", "average", "high", "disaster"];
  const SYSLOG_LEVELS = [
    { label: "Darurat", cls: "sev--emergency" },
    { label: "Siaga", cls: "sev--alert" },
    { label: "Kritis", cls: "sev--critical" },
    { label: "Galat", cls: "sev--error" },
    { label: "Peringatan", cls: "sev--warning" },
    { label: "Pemberitahuan", cls: "sev--notice" },
    { label: "Info", cls: "sev--information" },
    { label: "Debug", cls: "sev--debug" },
  ];

  const TABS = {
    syslog: { title: "Syslog", desc: "Pesan syslog terbaru dari perangkat terdaftar." },
    traps: { title: "Trap", desc: "SNMP trap/inform yang lolos verifikasi community." },
    rules: { title: "Aturan", desc: "Kondisi kejadian yang membuka masalah dan memicu poll cepat." },
    status: { title: "Status penerima", desc: "Berjalan/berhenti, paket terakhir, laju, dan jumlah dibuang." },
  };

  let currentTab = (window.location.hash || "#syslog").slice(1);
  if (!TABS[currentTab]) currentTab = "syslog";
  let syslogPage = 1;
  let trapsPage = 1;
  let devices = [];
  let groups = [];
  let rules = [];
  let editingRuleId = null;

  const $ = (id) => document.getElementById(id);
  const canEdit = () => window.__nmsMe && ["admin", "operator"].includes(window.__nmsMe.role);
  const tabButtons = Array.from(document.querySelectorAll("[data-tab]"));

  function activate(tab) {
    currentTab = TABS[tab] ? tab : "syslog";
    document.querySelectorAll("[data-view]").forEach((panel) => {
      panel.hidden = panel.dataset.view !== currentTab;
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
    window.NMSFormat.setDocumentTitle(meta.title, "Kejadian");
    if (currentTab === "syslog") loadSyslog();
    if (currentTab === "traps") loadTraps();
    if (currentTab === "rules") loadRules();
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
    event.preventDefault();
    const currentIndex = Math.max(0, tabButtons.indexOf(document.activeElement));
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabButtons.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabButtons.length) % tabButtons.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabButtons.length - 1;
    const next = tabButtons[nextIndex];
    next.focus();
    selectTab(next.dataset.tab);
    focusTabSoon(next);
  });
  window.addEventListener("hashchange", () => activate(window.location.hash.slice(1)));
  $("refresh").addEventListener("click", () => activate(currentTab));

  function inputToUtcIso(value) {
    if (!value) return "";
    const normalized = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value) ? `${value}:00` : value;
    const date = new Date(`${normalized}+07:00`);
    if (Number.isNaN(date.getTime())) return "";
    return date.toISOString().replace(/\.\d{3}Z$/, "Z");
  }

  function queryFrom(prefix) {
    const params = new URLSearchParams();
    const device = $(`${prefix}-device`) && $(`${prefix}-device`).value;
    const severity = $(`${prefix}-severity`) && $(`${prefix}-severity`).value;
    const q = $(`${prefix}-q`) && $(`${prefix}-q`).value.trim();
    const from = $(`${prefix}-from`) && $(`${prefix}-from`).value;
    const to = $(`${prefix}-to`) && $(`${prefix}-to`).value;
    if (device) params.set("device_id", device);
    if (severity) params.set("severity", severity);
    if (q) params.set("q", q);
    if (from) params.set("from", inputToUtcIso(from));
    if (to) params.set("to", inputToUtcIso(to));
    return params;
  }

  function timeZone() {
    return (window.__nmsMe && window.__nmsMe.timezone) || "Asia/Jakarta";
  }

  function formatLogTime(value) {
    const date = parseServerTs(value);
    if (!date) return null;
    const zone = timeZone();
    try {
      return {
        date: new Intl.DateTimeFormat("id-ID", {
          day: "2-digit", month: "short", year: "numeric", timeZone: zone,
        }).format(date),
        clock: new Intl.DateTimeFormat("id-ID", {
          hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: zone,
        }).format(date),
        full: new Intl.DateTimeFormat("id-ID", {
          dateStyle: "medium", timeStyle: "medium", timeZone: zone,
        }).format(date),
      };
    } catch {
      return { date: date.toISOString().slice(0, 10), clock: date.toISOString().slice(11, 19), full: date.toISOString() };
    }
  }

  function logTimeCell(value) {
    const parts = formatLogTime(value);
    if (!parts) return '<td class="log-time"><span class="cell-muted">-</span></td>';
    return `<td class="log-time" title="${escapeHtml(parts.full)}"><span class="log-time__date">${escapeHtml(parts.date)}</span><span class="log-time__clock">${escapeHtml(parts.clock)}</span></td>`;
  }

  function deviceCell(row) {
    const name = row.device_name || row.device_id || "-";
    const ip = row.device_ip ? `<span class="cell-sub">${escapeHtml(row.device_ip)}</span>` : "";
    return `<td class="log-device"><span class="log-device__name">${escapeHtml(name)}</span>${ip}</td>`;
  }

  function severityCell(value) {
    const level = value === null || value === undefined || value === "" ? NaN : Number(value);
    const meta = Number.isInteger(level) ? SYSLOG_LEVELS[level] : null;
    if (!meta) return '<td><span class="cell-muted">-</span></td>';
    return `<td><span class="sev ${meta.cls}" title="Keparahan ${level}: ${escapeHtml(meta.label)}">${escapeHtml(meta.label)}</span></td>`;
  }

  function tagCell(value) {
    const tag = String(value || "").trim();
    if (!tag) return '<td><span class="cell-muted">-</span></td>';
    return `<td><span class="log-tag">${escapeHtml(tag)}</span></td>`;
  }

  async function loadOptions() {
    const jobs = [];
    jobs.push(window.apiFetch("/api/devices?per_page=500").then((data) => {
      devices = data.devices || [];
    }).catch(() => { devices = []; }));
    jobs.push(window.apiFetch("/api/groups").then((data) => {
      groups = data.groups || [];
    }).catch(() => { groups = []; }));
    await Promise.allSettled(jobs);
    const options = devices.map((d) => `<option value="${d.id}">${escapeHtml(d.name)} (${escapeHtml(d.ip)})</option>`).join("");
    ["syslog-device", "traps-device"].forEach((id) => {
      const el = $(id);
      if (el) el.innerHTML = '<option value="">Semua perangkat</option>' + options;
    });
    const ruleDevice = $("rule-device");
    if (ruleDevice) ruleDevice.innerHTML = '<option value="">Semua</option>' + options;
    const ruleGroup = $("rule-group");
    if (ruleGroup) ruleGroup.innerHTML = '<option value="">Semua</option>' + groups.map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`).join("");
  }

  function fillSeveritySelect(id) {
    const el = $(id);
    if (el) el.innerHTML = SEVERITIES.map((s) => `<option value="${s}">${s}</option>`).join("");
  }
  fillSeveritySelect("rule-min-severity");
  fillSeveritySelect("rule-problem-severity");
  $("rule-problem-severity").value = "warning";

  function hasSyslogFilter() {
    return Boolean(
      ($("syslog-device") && $("syslog-device").value)
      || ($("syslog-severity") && $("syslog-severity").value)
      || ($("syslog-q") && $("syslog-q").value.trim())
      || ($("syslog-from") && $("syslog-from").value)
      || ($("syslog-to") && $("syslog-to").value)
    );
  }

  function syncSyslogEmpty(filtered) {
    $("syslog-empty-title").textContent = filtered ? "Tidak ada syslog yang cocok" : "Belum ada syslog masuk";
    $("syslog-empty-desc").textContent = filtered
      ? "Coba longgarkan kata kunci, keparahan, perangkat, atau rentang waktu."
      : "Pesan syslog akan tampil di sini setelah penerima aktif dan perangkat mengirimkannya.";
    $("syslog-setup").hidden = filtered;
    $("syslog-empty-cta").hidden = filtered;
    $("syslog-empty-reset").hidden = !filtered;
    $("syslog-reset").disabled = !filtered;
  }

  function showSyslogLoading() {
    $("syslog-table-wrap").hidden = false;
    if (!$("syslog-rows").querySelector("tr:not(.table-state)")) {
      $("syslog-rows").innerHTML = '<tr class="table-state"><td colspan="5"><span class="table-state__spinner" aria-hidden="true"></span>Memuat syslog…</td></tr>';
    }
  }

  async function loadSyslog() {
    const params = queryFrom("syslog");
    params.set("page", String(syslogPage));
    params.set("per_page", "50");
    $("syslog-error").hidden = true;
    $("syslog-count").textContent = "Memuat kejadian…";
    $("syslog-table-wrap").setAttribute("aria-busy", "true");
    showSyslogLoading();
    try {
      const data = await window.apiFetch(`/api/events/syslog?${params.toString()}`);
      const entries = data.entries || [];
      const total = Number(data.total) || 0;
      const page = Number(data.page) || 1;
      const perPage = Number(data.per_page) || 50;
      const pages = Math.max(1, Math.ceil(total / perPage));
      const filtered = hasSyslogFilter();
      $("syslog-count").textContent = `${total.toLocaleString("id-ID")} kejadian${filtered ? " · filter aktif" : ""}`;
      $("syslog-page").textContent = `Halaman ${page} dari ${pages}`;
      $("syslog-empty").hidden = total > 0;
      $("syslog-table-wrap").hidden = total === 0;
      $("syslog-pager").hidden = total === 0;
      $("syslog-export").disabled = total === 0;
      $("syslog-rows").innerHTML = entries.map((row) => `
        <tr>
          ${logTimeCell(row.ts)}
          ${deviceCell(row)}
          ${severityCell(row.severity)}
          ${tagCell(row.tag)}
          <td class="log-message">${escapeHtml(row.message || "-")}</td>
        </tr>`).join("");
      syncSyslogEmpty(filtered);
      $("syslog-prev").disabled = page <= 1;
      $("syslog-next").disabled = page >= pages || total === 0;
    } catch (error) {
      $("syslog-count").textContent = "Gagal memuat syslog.";
      $("syslog-error-text").textContent = error.message || "Syslog gagal dimuat.";
      $("syslog-error").hidden = false;
      $("syslog-empty").hidden = true;
      $("syslog-table-wrap").hidden = true;
      $("syslog-pager").hidden = true;
      $("syslog-export").disabled = true;
    } finally {
      $("syslog-table-wrap").removeAttribute("aria-busy");
    }
  }

  function resetSyslogFilter() {
    ["syslog-device", "syslog-severity", "syslog-q", "syslog-from", "syslog-to"].forEach((id) => {
      $(id).value = "";
    });
    syslogPage = 1;
    loadSyslog();
  }

  function hasTrapsFilter() {
    return Boolean(
      ($("traps-device") && $("traps-device").value)
      || ($("traps-q") && $("traps-q").value.trim())
    );
  }

  function syncTrapsEmpty(filtered) {
    $("traps-empty-title").textContent = filtered ? "Tidak ada trap yang cocok" : "Belum ada trap masuk";
    $("traps-empty-desc").textContent = filtered
      ? "Coba ubah perangkat atau kata kunci, lalu terapkan kembali."
      : "Trap SNMP akan tampil di sini setelah penerima aktif dan perangkat mengirimkannya.";
    $("traps-setup").hidden = filtered;
    $("traps-empty-cta").hidden = filtered;
    $("traps-empty-reset").hidden = !filtered;
    $("traps-reset").disabled = !filtered;
  }

  function trapVarbindCell(row) {
    const text = String(row.varbinds || "").trim();
    if (!text || text === "[]" || text === "null") return '<span class="cell-muted">-</span>';
    let pretty = escapeHtml(row.varbinds);
    let count = 0;
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) count = parsed.length;
      else if (parsed && typeof parsed === "object") count = Object.keys(parsed).length;
      pretty = escapeHtml(JSON.stringify(parsed, null, 2));
    } catch (_error) {
      // Bukan JSON: tampilkan varbind apa adanya.
    }
    const label = count ? `Lihat ${count} varbind` : "Lihat varbind";
    return `<details class="trap-varbind"><summary>${escapeHtml(label)}</summary><pre class="code-block code-block--compact">${pretty}</pre></details>`;
  }

  async function loadTraps() {
    const params = queryFrom("traps");
    params.set("page", String(trapsPage));
    params.set("per_page", "50");
    $("traps-error").hidden = true;
    $("traps-count").textContent = "Memuat trap…";
    $("traps-table-wrap").setAttribute("aria-busy", "true");
    try {
      const data = await window.apiFetch(`/api/events/traps?${params.toString()}`);
      const entries = data.entries || [];
      const total = Number(data.total) || 0;
      const page = Number(data.page) || 1;
      const perPage = Number(data.per_page) || 50;
      const pages = Math.max(1, Math.ceil(total / perPage));
      const filtered = hasTrapsFilter();
      $("traps-count").textContent = `${total.toLocaleString("id-ID")} trap${filtered ? " · filter aktif" : ""}`;
      $("traps-page").textContent = `Halaman ${page} dari ${pages}`;
      $("traps-empty").hidden = total > 0;
      $("traps-table-wrap").hidden = total === 0;
      $("traps-pager").hidden = total === 0;
      $("traps-export").disabled = total === 0;
      $("traps-rows").innerHTML = entries.map((row) => `
        <tr>
          ${logTimeCell(row.ts)}
          ${deviceCell(row)}
          <td class="mono traps-oid">${escapeHtml(row.trap_oid || "-")}</td>
          <td class="traps-name">${escapeHtml(row.trap_name || "-")}</td>
          <td class="traps-varbind">${trapVarbindCell(row)}</td>
        </tr>`).join("");
      syncTrapsEmpty(filtered);
      $("traps-prev").disabled = page <= 1;
      $("traps-next").disabled = page >= pages || total === 0;
    } catch (error) {
      $("traps-count").textContent = "Gagal memuat trap.";
      $("traps-error-text").textContent = error.message || "Trap gagal dimuat.";
      $("traps-error").hidden = false;
      $("traps-empty").hidden = true;
      $("traps-table-wrap").hidden = true;
      $("traps-pager").hidden = true;
      $("traps-export").disabled = true;
    } finally {
      $("traps-table-wrap").removeAttribute("aria-busy");
    }
  }

  function resetTrapsFilter() {
    $("traps-device").value = "";
    $("traps-q").value = "";
    trapsPage = 1;
    loadTraps();
  }

  function conditionLabel(rule) {
    if (rule.kind === "trap") return `OID diawali ${rule.oid_prefix}`;
    const labels = { contains: "mengandung", startswith: "diawali", regex: "regex" };
    return `${labels[rule.match_type] || rule.match_type}: ${rule.pattern}`;
  }

  async function loadRules() {
    try {
      const data = await window.apiFetch("/api/events/rules");
      rules = data.rules || [];
      $("rule-rows").innerHTML = rules.map((rule) => `
        <tr>
          <td>${escapeHtml(rule.name)}</td>
          <td>${escapeHtml(rule.kind)}</td>
          <td class="mono">${escapeHtml(conditionLabel(rule))}</td>
          <td>${escapeHtml(rule.problem_severity)}</td>
          <td>${Number(rule.cooldown_seconds)} dtk</td>
          <td>${Number(rule.auto_resolve_minutes) || "-"}${Number(rule.auto_resolve_minutes) ? " mnt" : ""}</td>
          <td>${rule.enabled ? "Ya" : "Tidak"}</td>
          <td>${canEdit() ? `<button class="ack-toggle" type="button" data-rule-action="edit" data-id="${rule.id}">Ubah</button> <button class="ack-toggle" type="button" data-rule-action="delete" data-id="${rule.id}">Hapus</button>` : "-"}</td>
        </tr>`).join("");
      const resolves = $("rule-resolves");
      resolves.innerHTML = '<option value="">Tidak ada</option>' + rules.map((rule) => `<option value="${rule.id}">${escapeHtml(rule.name)}</option>`).join("");
      document.querySelectorAll("[data-rule-action]").forEach((button) => {
        button.addEventListener("click", () => {
          const rule = rules.find((item) => String(item.id) === button.dataset.id);
          if (!rule) return;
          if (button.dataset.ruleAction === "edit") openRuleForm(rule);
          if (button.dataset.ruleAction === "delete") deleteRule(rule);
        });
      });
    } catch (error) {
      $("rule-rows").innerHTML = `<tr><td colspan="8">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  function openRuleForm(rule) {
    editingRuleId = rule ? rule.id : null;
    $("rule-form-title").textContent = rule ? "Ubah aturan" : "Tambah aturan";
    $("rule-name").value = rule ? rule.name : "";
    $("rule-kind").value = rule ? rule.kind : "syslog";
    $("rule-match").value = rule ? rule.match_type : "contains";
    $("rule-pattern").value = rule ? rule.pattern : "";
    $("rule-oid").value = rule ? rule.oid_prefix : "";
    $("rule-min-severity").value = rule ? rule.min_severity : "information";
    $("rule-problem-severity").value = rule ? rule.problem_severity : "warning";
    $("rule-device").value = rule && rule.device_id ? String(rule.device_id) : "";
    $("rule-group").value = rule && rule.group_id ? String(rule.group_id) : "";
    $("rule-cooldown").value = rule ? rule.cooldown_seconds : 60;
    $("rule-auto").value = rule ? rule.auto_resolve_minutes : 30;
    $("rule-recovery").value = rule ? rule.recovery_pattern : "";
    $("rule-resolves").value = rule && rule.resolves_rule_id ? String(rule.resolves_rule_id) : "";
    $("rule-enabled").value = rule && !rule.enabled ? "0" : "1";
    $("rule-note").textContent = "";
    window.NMSUi.openSheet("rule-form");
    syncKind();
    $("rule-name").focus({ preventScroll: true });
  }

  function syncKind() {
    const trap = $("rule-kind").value === "trap";
    $("rule-pattern-wrap").hidden = trap;
    $("rule-oid-wrap").hidden = !trap;
  }
  $("rule-kind").addEventListener("change", syncKind);
  $("rule-add").addEventListener("click", () => openRuleForm(null));
  $("rule-cancel").addEventListener("click", () => { window.NMSUi.closeSheet("rule-form"); editingRuleId = null; });

  async function deleteRule(rule) {
    if (!(await window.NMSUi.confirm(`Hapus aturan "${rule.name}"?`, {
      title: "Hapus aturan",
      confirmLabel: "Hapus",
      danger: true,
    }))) return;
    try {
      await window.apiFetch(`/api/events/rules/${rule.id}`, { method: "DELETE" });
      await loadRules();
    } catch (error) {
      await window.NMSUi.alert(error.message);
    }
  }

  $("rule-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      name: $("rule-name").value.trim(),
      kind: $("rule-kind").value,
      match_type: $("rule-match").value,
      pattern: $("rule-pattern").value,
      oid_prefix: $("rule-oid").value,
      min_severity: $("rule-min-severity").value,
      problem_severity: $("rule-problem-severity").value,
      device_id: $("rule-device").value || null,
      group_id: $("rule-group").value || null,
      cooldown_seconds: Number($("rule-cooldown").value || 0),
      auto_resolve_minutes: Number($("rule-auto").value || 0),
      recovery_pattern: $("rule-recovery").value,
      resolves_rule_id: $("rule-resolves").value || null,
      enabled: $("rule-enabled").value === "1",
    };
    try {
      const url = editingRuleId ? `/api/events/rules/${editingRuleId}` : "/api/events/rules";
      await window.apiFetch(url, { method: editingRuleId ? "PUT" : "POST", body: payload });
      window.NMSUi.closeSheet("rule-form");
      editingRuleId = null;
      await loadRules();
    } catch (error) {
      $("rule-note").textContent = error.message;
    }
  });

  async function loadStatus() {
    try {
      const data = await window.apiFetch("/api/events/status");
      const labels = { syslog_udp: "Syslog UDP", syslog_tcp: "Syslog TCP", snmp_trap: "SNMP trap UDP" };
      $("status-rows").innerHTML = Object.entries(data.receivers || {}).map(([kind, item]) => `
        <tr>
          <td>${escapeHtml(labels[kind] || kind)}</td>
          <td>${item.running ? "Berjalan" : "Berhenti"}</td>
          <td class="mono">${escapeHtml(item.bind_address || "-")}:${escapeHtml(item.port || "-")}</td>
          <td>${escapeHtml(item.last_packet_at ? formatDateTime(item.last_packet_at) : "-")}</td>
          <td>${Number(item.accepted || 0).toLocaleString("id-ID")}</td>
          <td>${Number(item.rate_per_sec || 0).toFixed(1)}</td>
          <td>${Number(item.dropped_unknown || 0)} / ${Number(item.dropped_rate || 0)} / ${Number(item.dropped_auth || 0)} / ${Number(item.dropped_queue || 0)}</td>
          <td>${Number((item.queue || {}).queue_size || 0)} / ditulis ${Number((item.queue || {}).written || 0)}</td>
        </tr>`).join("");
      const counts = data.counts || {};
      $("status-note").textContent = `Tersimpan: ${counts.syslog_events || 0} syslog, ${counts.snmp_traps || 0} trap. ${Object.values(data.receivers || {}).some((item) => item.last_error) ? "Ada galat bind. Periksa port." : ""}`;
    } catch (error) {
      $("status-note").textContent = error.message;
    }
  }

  $("syslog-filter").addEventListener("submit", (event) => { event.preventDefault(); syslogPage = 1; loadSyslog(); });
  $("traps-filter").addEventListener("submit", (event) => { event.preventDefault(); trapsPage = 1; loadTraps(); });
  ["syslog-device", "syslog-severity", "syslog-q", "syslog-from", "syslog-to"].forEach((id) => {
    const updateReset = () => { $("syslog-reset").disabled = !hasSyslogFilter(); };
    $(id).addEventListener("input", updateReset);
    $(id).addEventListener("change", updateReset);
  });
  ["traps-device", "traps-q"].forEach((id) => {
    const updateReset = () => { $("traps-reset").disabled = !hasTrapsFilter(); };
    $(id).addEventListener("input", updateReset);
    $(id).addEventListener("change", updateReset);
  });
  $("syslog-prev").addEventListener("click", () => { syslogPage = Math.max(1, syslogPage - 1); loadSyslog(); });
  $("syslog-next").addEventListener("click", () => { syslogPage += 1; loadSyslog(); });
  $("traps-prev").addEventListener("click", () => { trapsPage = Math.max(1, trapsPage - 1); loadTraps(); });
  $("traps-next").addEventListener("click", () => { trapsPage += 1; loadTraps(); });
  $("syslog-reset").addEventListener("click", resetSyslogFilter);
  $("syslog-empty-reset").addEventListener("click", resetSyslogFilter);
  $("syslog-retry").addEventListener("click", loadSyslog);
  $("traps-reset").addEventListener("click", resetTrapsFilter);
  $("traps-empty-reset").addEventListener("click", resetTrapsFilter);
  $("traps-retry").addEventListener("click", loadTraps);
  $("syslog-export").addEventListener("click", () => { window.location.href = `/api/events/syslog.csv?${queryFrom("syslog").toString()}`; });
  $("traps-export").addEventListener("click", () => { window.location.href = `/api/events/traps.csv?${queryFrom("traps").toString()}`; });

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  whenReady(async () => {
    if (!canEdit()) {
      document.querySelector(".main").innerHTML = '<section class="panel"><h2 class="panel__title">Akses ditolak</h2><p class="panel__sub">Halaman kejadian hanya untuk operator/admin.</p></section>';
      return;
    }
    await loadOptions();
    activate(currentTab);
  });
})();

