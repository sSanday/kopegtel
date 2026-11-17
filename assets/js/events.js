/* Kejadian: syslog, SNMP trap, aturan, dan status penerima. */
(function () {
  "use strict";

  const { escapeHtml, formatDateTime } = window.NMSFormat;
  const SEVERITIES = ["information", "warning", "average", "high", "disaster"];
  const SYSLOG_SEVERITY = ["Emergency", "Alert", "Critical", "Error", "Warning", "Notice", "Info", "Debug"];

  const TABS = {
    syslog: { title: "Syslog", desc: "Pesan syslog terbaru dari perangkat terdaftar." },
    traps: { title: "Trap", desc: "SNMP trap/inform yang lolos verifikasi community." },
    rules: { title: "Aturan", desc: "Kondisi kejadian yang membuka problem dan memicu poll cepat." },
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

  function activate(tab) {
    currentTab = TABS[tab] ? tab : "syslog";
    document.querySelectorAll("[data-view]").forEach((panel) => {
      panel.hidden = panel.dataset.view !== currentTab;
    });
    document.querySelectorAll("[data-tab]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.tab === currentTab);
    });
    const meta = TABS[currentTab];
    $("view-title").textContent = meta.title;
    $("view-desc").textContent = meta.desc;
    document.title = `${meta.title} — Kejadian — NMS Kopegtel`;
    if (currentTab === "syslog") loadSyslog();
    if (currentTab === "traps") loadTraps();
    if (currentTab === "rules") loadRules();
    if (currentTab === "status") loadStatus();
  }

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      const tab = button.dataset.tab;
      if (!TABS[tab]) return;
      // Muat hanya lewat satu jalur: set hash → hashchange → activate().
      if (window.location.hash === `#${tab}`) {
        activate(tab);
        return;
      }
      window.location.hash = tab;
    });
  });
  window.addEventListener("hashchange", () => activate(window.location.hash.slice(1)));
  $("refresh").addEventListener("click", () => activate(currentTab));

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
    if (from) params.set("from", from.replace("T", "T") + ":00Z");
    if (to) params.set("to", to + ":00Z");
    return params;
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

  async function loadSyslog() {
    const params = queryFrom("syslog");
    params.set("page", String(syslogPage));
    params.set("per_page", "50");
    try {
      const data = await window.apiFetch(`/api/events/syslog?${params.toString()}`);
      $("syslog-count").textContent = `${data.total.toLocaleString("id-ID")} kejadian`;
      $("syslog-page").textContent = `Halaman ${data.page}`;
      $("syslog-empty").hidden = data.total > 0;
      $("syslog-rows").innerHTML = (data.entries || []).map((row) => `
        <tr>
          <td>${escapeHtml(formatDateTime(row.ts))}</td>
          <td>${escapeHtml(row.device_name || row.device_id)}<br><span class="panel__sub">${escapeHtml(row.device_ip || "")}</span></td>
          <td>${escapeHtml(SYSLOG_SEVERITY[row.severity] || row.severity || "-")}</td>
          <td>${escapeHtml(row.tag || "-")}</td>
          <td class="mono">${escapeHtml(row.message || "")}</td>
        </tr>`).join("");
      $("syslog-prev").disabled = data.page <= 1;
      $("syslog-next").disabled = data.entries.length < data.per_page;
    } catch (error) {
      $("syslog-count").textContent = error.message;
    }
  }

  async function loadTraps() {
    const params = queryFrom("traps");
    params.set("page", String(trapsPage));
    params.set("per_page", "50");
    try {
      const data = await window.apiFetch(`/api/events/traps?${params.toString()}`);
      $("traps-count").textContent = `${data.total.toLocaleString("id-ID")} trap`;
      $("traps-page").textContent = `Halaman ${data.page}`;
      $("traps-empty").hidden = data.total > 0;
      $("traps-rows").innerHTML = (data.entries || []).map((row) => `
        <tr>
          <td>${escapeHtml(formatDateTime(row.ts))}</td>
          <td>${escapeHtml(row.device_name || row.device_id)}<br><span class="panel__sub">${escapeHtml(row.device_ip || "")}</span></td>
          <td class="mono">${escapeHtml(row.trap_oid || "")}</td>
          <td>${escapeHtml(row.trap_name || "-")}</td>
          <td class="mono">${escapeHtml(row.varbinds || "[]")}</td>
        </tr>`).join("");
      $("traps-prev").disabled = data.page <= 1;
      $("traps-next").disabled = data.entries.length < data.per_page;
    } catch (error) {
      $("traps-count").textContent = error.message;
    }
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
          <td>${Number(rule.auto_resolve_minutes) || "—"}${Number(rule.auto_resolve_minutes) ? " mnt" : ""}</td>
          <td>${rule.enabled ? "Ya" : "Tidak"}</td>
          <td>${canEdit() ? `<button class="ack-toggle" type="button" data-rule-action="edit" data-id="${rule.id}">Ubah</button> <button class="ack-toggle" type="button" data-rule-action="delete" data-id="${rule.id}">Hapus</button>` : "-"}</td>
        </tr>`).join("");
      const resolves = $("rule-resolves");
      resolves.innerHTML = '<option value="">—</option>' + rules.map((rule) => `<option value="${rule.id}">${escapeHtml(rule.name)}</option>`).join("");
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
    $("rule-form").hidden = false;
    $("rule-form").scrollIntoView({ behavior: "smooth", block: "start" });
    syncKind();
  }

  function syncKind() {
    const trap = $("rule-kind").value === "trap";
    $("rule-pattern-wrap").hidden = trap;
    $("rule-oid-wrap").hidden = !trap;
  }
  $("rule-kind").addEventListener("change", syncKind);
  $("rule-add").addEventListener("click", () => openRuleForm(null));
  $("rule-cancel").addEventListener("click", () => { $("rule-form").hidden = true; editingRuleId = null; });

  async function deleteRule(rule) {
    if (!window.confirm(`Hapus aturan "${rule.name}"?`)) return;
    try {
      await window.apiFetch(`/api/events/rules/${rule.id}`, { method: "DELETE" });
      await loadRules();
    } catch (error) {
      window.alert(error.message);
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
      $("rule-form").hidden = true;
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
          <td>${escapeHtml(item.last_packet_at ? formatDateTime(item.last_packet_at) : "—")}</td>
          <td>${Number(item.accepted || 0).toLocaleString("id-ID")}</td>
          <td>${Number(item.rate_per_sec || 0).toFixed(1)}</td>
          <td>${Number(item.dropped_unknown || 0)} / ${Number(item.dropped_rate || 0)} / ${Number(item.dropped_auth || 0)} / ${Number(item.dropped_queue || 0)}</td>
          <td>${Number((item.queue || {}).queue_size || 0)} / ditulis ${Number((item.queue || {}).written || 0)}</td>
        </tr>`).join("");
      const counts = data.counts || {};
      $("status-note").textContent = `Tersimpan: ${counts.syslog_events || 0} syslog, ${counts.snmp_traps || 0} trap. ${Object.values(data.receivers || {}).some((item) => item.last_error) ? "Ada galat bind — periksa port." : ""}`;
    } catch (error) {
      $("status-note").textContent = error.message;
    }
  }

  $("syslog-filter").addEventListener("submit", (event) => { event.preventDefault(); syslogPage = 1; loadSyslog(); });
  $("traps-filter").addEventListener("submit", (event) => { event.preventDefault(); trapsPage = 1; loadTraps(); });
  $("syslog-prev").addEventListener("click", () => { syslogPage = Math.max(1, syslogPage - 1); loadSyslog(); });
  $("syslog-next").addEventListener("click", () => { syslogPage += 1; loadSyslog(); });
  $("traps-prev").addEventListener("click", () => { trapsPage = Math.max(1, trapsPage - 1); loadTraps(); });
  $("traps-next").addEventListener("click", () => { trapsPage += 1; loadTraps(); });
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

