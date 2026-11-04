/* ============================================================
   NMS Kopegtel — halaman SLA report.
   Filter, pemilih SLA/service, dan render SLI/error budget.
   ============================================================ */
(function () {
  "use strict";

  var state = {
    slas: [], slaId: null, serviceId: null, report: null, loading: false,
    slasLoaded: false, pendingServiceId: null
  };

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return window.svcEsc ? window.svcEsc(value) : String(value == null ? "" : value);
  }
  function isValidDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    var parts = value.split("-").map(Number);
    var d = new Date(parts[0], parts[1] - 1, parts[2]);
    return d.getFullYear() === parts[0] &&
      d.getMonth() === parts[1] - 1 &&
      d.getDate() === parts[2];
  }
  function formatSlo(value) {
    var number = Number(value);
    if (!isFinite(number)) return "—";
    return number.toFixed(2).replace(/0+$/, "").replace(/\.$/, "") + "%";
  }
  function formatPct(value) {
    if (value === null || value === undefined || isNaN(Number(value))) return "—";
    return Number(value).toFixed(2) + "%";
  }
  function formatDuration(seconds) {
    var total = Math.round(Number(seconds) || 0);
    var sign = total < 0 ? "-" : "";
    total = Math.abs(total);
    var days = Math.floor(total / 86400);
    var hours = Math.floor((total % 86400) / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var secs = total % 60;
    var parts = [];
    if (days) parts.push(days + "d");
    if (hours) parts.push(hours + "h");
    if (minutes) parts.push(minutes + "m");
    return sign + (parts.join(" ") || (secs + "s"));
  }
  function formatBudget(seconds) {
    var value = Number(seconds);
    if (!isFinite(value)) return "—";
    if (value < 0) return formatDuration(Math.abs(value)) + " over";
    return formatDuration(value);
  }
  function statusLabel(status) {
    return {
      met: "Met",
      at_risk: "At risk",
      breached: "Breached",
      no_data: "No data",
      no_services: "No services",
      not_started: "Not started"
    }[status] || "No data";
  }
  function statusClass(status) {
    return ["met", "at_risk", "breached", "no_data", "no_services", "not_started"].indexOf(status) === -1
      ? "no_data" : status;
  }
  function statusHtml(status) {
    return '<span class="zbx-sla-status ' + statusClass(status) + '">' + esc(statusLabel(status)) + '</span>';
  }
  function setMessage(text) {
    var message = $("slaReportMessage");
    if (!message) return;
    message.textContent = text || "";
    message.hidden = !text;
    if (text) hideEmpty();
  }

  function hideEmpty() {
    var box = $("slaReportEmpty");
    if (box) box.hidden = true;
  }

  function showEmpty(title, text, actions) {
    var box = $("slaReportEmpty");
    var wrap = $("slaReportEmptyActions");
    if (!box || !wrap) return;
    if ($("slaReportEmptyTitle")) $("slaReportEmptyTitle").textContent = title || "";
    if ($("slaReportEmptyText")) $("slaReportEmptyText").textContent = text || "";
    wrap.innerHTML = "";
    (actions || []).forEach(function (item) {
      var element;
      if (item.href) {
        element = document.createElement("a");
        element.href = item.href;
        element.className = "zbx-btn";
      } else {
        element = document.createElement("button");
        element.type = "button";
        element.className = "zbx-btn primary";
        if (item.action) element.addEventListener("click", item.action);
      }
      element.textContent = item.label;
      wrap.appendChild(element);
    });
    box.hidden = false;
  }

  function showPrompt() {
    setMessage("");
    var summary = $("slaReportSummary");
    var card = $("slaReportCard");
    if (summary) summary.hidden = true;
    if (card) card.hidden = true;
    if (!state.slasLoaded) {
      setMessage("Memuat daftar SLA…");
      return;
    }
    if (!state.slas.length) {
      showEmpty(
        "Belum ada SLA",
        "SLA report butuh SLA dengan service tag yang cocok. Buat SLA dulu, lalu pastikan service punya tag yang sama.",
        [
          { label: "Create SLA", href: "/services/sla" },
          { label: "Buka Services", href: "/services" }
        ]
      );
      return;
    }
    showEmpty(
      "Pilih SLA",
      "Pilih SLA pada filter untuk menampilkan laporan. Biarkan From dan To kosong untuk memakai periode berjalan SLA.",
      [{ label: "Select SLA", action: openSlaPicker }]
    );
  }

  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  function isoDate(date) {
    return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate());
  }

  function setDateInputs(from, to) {
    if ($("slaReportFrom")) $("slaReportFrom").value = from || "";
    if ($("slaReportTo")) $("slaReportTo").value = to || "";
  }

  function readUrlFilters() {
    var params = new URLSearchParams(window.location.search);
    var from = params.get("from") || "";
    var to = params.get("to") || "";
    if (!isValidDate(from) || !isValidDate(to)) {
      from = "";
      to = "";
    }
    return {
      slaId: params.get("sla_id") || "",
      serviceId: params.get("service_id") || "",
      from: from,
      to: to
    };
  }

  function syncUrl() {
    var url = new URL(window.location.href);
    var params = url.searchParams;
    if (state.slaId) params.set("sla_id", state.slaId);
    else params.delete("sla_id");
    if (state.serviceId) params.set("service_id", state.serviceId);
    else params.delete("service_id");
    var from = ($("slaReportFrom") && $("slaReportFrom").value.trim()) || "";
    var to = ($("slaReportTo") && $("slaReportTo").value.trim()) || "";
    if (isValidDate(from) && isValidDate(to)) {
      params.set("from", from);
      params.set("to", to);
    } else {
      params.delete("from");
      params.delete("to");
    }
    try {
      window.history.replaceState({}, "", url.toString());
    } catch (_) {}
    return url.toString();
  }
  function showDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }
  function hideDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }
  function setFilterOpen(open) {
    var panel = $("slaReportFilter");
    var form = $("slaReportFilterForm");
    var button = $("btnToggleSlaReportFilter");
    if (panel) panel.classList.toggle("is-collapsed", !open);
    if (form) form.hidden = !open;
    if (button) {
      button.classList.toggle("collapsed", !open);
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }

  function slaById(id) {
    return state.slas.filter(function (item) { return String(item.id) === String(id); })[0] || null;
  }

  function renderSlaPicker() {
    var body = $("slaPickerBody");
    if (!body) return;
    if (!state.slas.length) {
      body.innerHTML = '<tr><td class="zbx-sla-picker-empty-cell"><div class="zbx-sla-picker-empty">' +
        '<span>No SLA found. Create an SLA first.</span></div></td></tr>';
      return;
    }
    body.innerHTML = state.slas.map(function (sla) {
      return '<tr><td><button class="zbx-text-link zbx-sla-picker-link" type="button" data-sla-choice="' +
        sla.id + '">' + esc(sla.name) + '</button>' +
        '<span class="zbx-muted"> · SLO ' + esc(formatSlo(sla.slo)) + '</span></td></tr>';
    }).join("");
  }

  function renderServicePicker() {
    var body = $("servicePickerBody");
    if (!body) return;
    var services = (state.report && state.report.services) || [];
    if (!services.length) {
      body.innerHTML = '<tr><td colspan="3" class="zbx-service-picker-empty-cell"><div class="zbx-service-picker-empty">' +
        '<span>No service matches this SLA.</span></div></td></tr>';
      return;
    }
    body.innerHTML = services.map(function (svc) {
      return '<tr data-service-name="' + esc(String(svc.name || "").toLowerCase()) + '">' +
        '<td><button class="zbx-text-link" type="button" data-service-choice="' + svc.id + '">' +
        esc(svc.name) + '</button></td>' +
        '<td>' + esc(svc.current_status || "—") + '</td>' +
        '<td>' + esc(formatPct(svc.sli)) + '</td></tr>';
    }).join("");
  }

  function openSlaPicker() {
    renderSlaPicker();
    showDialog($("slaPickerDialog"));
  }
  function closeSlaPicker() {
    hideDialog($("slaPickerDialog"));
    var select = $("btnSelectSla");
    if (select) select.focus();
  }
  function openServicePicker() {
    if (!state.slaId) {
      setMessage("Pilih SLA terlebih dahulu.");
      return;
    }
    if (!state.report) {
      setMessage("Tekan Apply untuk memuat report sebelum memilih service.");
      return;
    }
    var input = $("servicePickerName");
    if (input) input.value = "";
    renderServicePicker();
    filterServicePicker();
    showDialog($("servicePickerDialog"));
    if (input) input.focus();
  }
  function closeServicePicker() {
    hideDialog($("servicePickerDialog"));
    var select = $("btnSelectService");
    if (select) select.focus();
  }
  function filterServicePicker() {
    var body = $("servicePickerBody");
    if (!body) return;
    var query = (($("servicePickerName") && $("servicePickerName").value) || "").trim().toLowerCase();
    var visible = 0;
    Array.prototype.forEach.call(body.querySelectorAll("tr[data-service-name]"), function (row) {
      var match = !query || (row.getAttribute("data-service-name") || "").indexOf(query) !== -1;
      row.hidden = !match;
      if (match) visible += 1;
    });
    var empty = body.querySelector(".zbx-service-picker-empty-cell");
    if (empty) empty.parentNode.hidden = visible > 0;
  }
  function resetServicePicker() {
    var input = $("servicePickerName");
    if (input) {
      input.value = "";
      input.focus();
    }
    filterServicePicker();
  }

  function selectSla(id, load) {
    var sla = slaById(id);
    if (!sla) return;
    state.slaId = sla.id;
    state.serviceId = null;
    if ($("slaReportSla")) $("slaReportSla").value = sla.name;
    if ($("slaReportService")) $("slaReportService").value = "";
    closeSlaPicker();
    syncUrl();
    if (load !== false) loadReport();
  }

  function selectService(id) {
    var services = (state.report && state.report.services) || [];
    var svc = services.filter(function (item) { return String(item.id) === String(id); })[0];
    if (!svc) return;
    state.serviceId = svc.id;
    if ($("slaReportService")) $("slaReportService").value = svc.name;
    closeServicePicker();
    syncUrl();
    loadReport();
  }

  function loadSlas() {
    return window.svcJson("/api/sla").then(function (data) {
      state.slas = (data && data.slas) || [];
      state.slasLoaded = true;
      renderSlaPicker();
      var initial = readUrlFilters();
      if (initial.from && initial.to) setDateInputs(initial.from, initial.to);
      state.pendingServiceId = initial.serviceId || null;
      if (initial.slaId) {
        var sla = slaById(initial.slaId);
        if (sla) {
          selectSla(sla.id, false);
          loadReport();
          return;
        }
      }
      showPrompt();
    }).catch(function (err) {
      setMessage((err && err.message) || "Gagal memuat daftar SLA.");
    });
  }

  function loadReport() {
    if (!state.slaId) {
      showPrompt();
      return;
    }
    var from = (($("slaReportFrom") && $("slaReportFrom").value) || "").trim();
    var to = (($("slaReportTo") && $("slaReportTo").value) || "").trim();
    if (from && !isValidDate(from)) {
      window.svcToast("Tanggal From harus format YYYY-MM-DD.", "error");
      return;
    }
    if (to && !isValidDate(to)) {
      window.svcToast("Tanggal To harus format YYYY-MM-DD.", "error");
      return;
    }
    if (!!from !== !!to) {
      window.svcToast("Isi From dan To sekaligus.", "error");
      return;
    }
    if (from && to && from > to) {
      window.svcToast("Tanggal From tidak boleh setelah To.", "error");
      return;
    }
    var params = [];
    if (from && to) {
      params.push("from=" + encodeURIComponent(from));
      params.push("to=" + encodeURIComponent(to));
    }
    if (state.serviceId) params.push("service_id=" + encodeURIComponent(state.serviceId));
    var url = "/api/sla/" + encodeURIComponent(state.slaId) + "/report";
    if (params.length) url += "?" + params.join("&");
    state.loading = true;
    setMessage("Memuat report…");
    var card = $("slaReportCard");
    if (card) card.hidden = true;
    var summaryEl = $("slaReportSummary");
    if (summaryEl) summaryEl.hidden = true;
    window.svcJson(url).then(function (data) {
      state.loading = false;
      state.report = data;
      renderReport(data);
    }).catch(function (err) {
      state.loading = false;
      var summary = $("slaReportSummary");
      if (summary) summary.hidden = true;
      setMessage((err && err.message) || "Gagal memuat SLA report.");
    });
  }

  function renderReport(data) {
    var summary = $("slaReportSummary");
    var card = $("slaReportCard");
    setMessage("");
    hideEmpty();
    if (summary) summary.hidden = false;
    if (card) card.hidden = false;

    if ($("slaReportName")) {
      $("slaReportName").textContent = data.sla.name + " · SLO " + formatSlo(data.sla.slo);
    }
    if ($("slaReportRange")) {
      $("slaReportRange").textContent = data.from + " — " + data.to +
        " · " + (data.sla.reporting_label || "") +
        " · " + (data.sla.timezone_label || "") +
        " · " + (data.sla.schedule_label || "");
    }
    var statusEl = $("slaReportStatus");
    if (statusEl) {
      statusEl.textContent = statusLabel(data.status);
      statusEl.className = "zbx-sla-report-status " + statusClass(data.status);
    }
    if ($("slaReportSli")) $("slaReportSli").textContent = formatPct(data.sli);
    if ($("slaReportUptime")) $("slaReportUptime").textContent = formatDuration(data.online_seconds);
    if ($("slaReportDowntime")) $("slaReportDowntime").textContent = formatDuration(data.downtime_seconds);
    var budgetEl = $("slaReportBudget");
    if (budgetEl) {
      var overBudget = Number(data.remaining_budget_seconds) < 0;
      budgetEl.textContent = formatBudget(data.remaining_budget_seconds);
      budgetEl.title = overBudget
        ? "Error budget terlampaui " + formatDuration(Math.abs(data.remaining_budget_seconds)) + " dari " + formatDuration(data.allowed_downtime_seconds) + " yang diizinkan."
        : "Sisa error budget dari " + formatDuration(data.allowed_downtime_seconds) + " yang diizinkan.";
      budgetEl.classList.toggle("is-over", overBudget);
    }
    if ($("slaReportCoverage")) {
      $("slaReportCoverage").textContent = formatPct(data.coverage_pct);
      $("slaReportCoverage").title = "Persentase waktu terjadwal yang punya data riwayat untuk dihitung.";
    }
    if ($("slaReportServices")) {
      $("slaReportServices").textContent = data.services_with_data + " / " + data.services_count;
    }

    var body = $("slaReportBody");
    if (body) {
      if (!data.services.length) {
        body.innerHTML = '<tr><td colspan="7" class="zbx-sla-empty-cell"><div class="zbx-sla-empty">' +
          '<span>Tidak ada service dengan tag yang cocok.</span>' +
          '<a class="zbx-text-link" href="/services">Buka Services</a></div></td></tr>';
      } else {
        body.innerHTML = data.services.map(function (svc) {
          return '<tr>' +
            '<td><strong>' + esc(svc.name) + '</strong></td>' +
            '<td>' + esc(svc.current_status || "—") + '</td>' +
            '<td>' + esc(formatPct(svc.sli)) + '</td>' +
            '<td>' + esc(formatDuration(svc.online_seconds)) + '</td>' +
            '<td>' + esc(formatDuration(svc.downtime_seconds)) + '</td>' +
            '<td>' + esc(formatBudget(svc.remaining_budget_seconds)) + '</td>' +
            '<td>' + statusHtml(svc.status) + '</td>' +
          '</tr>';
        }).join("");
      }
    }
    if ($("slaReportInfo")) {
      $("slaReportInfo").textContent = "Displaying " + data.services.length + " of " + data.services_count + " services";
    }
    var fromInput = $("slaReportFrom");
    var toInput = $("slaReportTo");
    if (fromInput && toInput && !fromInput.value.trim() && !toInput.value.trim()) {
      setDateInputs(data.from, data.to);
    }
    if (data.status === "no_data") {
      setMessage("Belum ada riwayat pada rentang ini. SLI tidak dihitung sampai ada data pemeriksaan.");
    } else if (data.status === "no_services") {
      setMessage("Tidak ada service dengan tag yang cocok. Tambahkan tag service di halaman Services.");
    } else if (data.status === "not_started") {
      setMessage("SLA berlaku mulai " + (data.sla.effective_date || "—") + ", di luar rentang ini.");
    }
    renderServicePicker();
    if (state.pendingServiceId) {
      var wanted = String(state.pendingServiceId);
      state.pendingServiceId = null;
      var pending = (data.services || []).filter(function (item) {
        return String(item.id) === wanted;
      })[0];
      if (pending) {
        state.serviceId = pending.id;
        if ($("slaReportService")) $("slaReportService").value = pending.name;
        syncUrl();
        loadReport();
        return;
      }
      if (window.svcToast) window.svcToast("Service pada tautan tidak termasuk SLA ini.", "error");
    }
    syncUrl();
  }

  function applyFilter(e) {
    if (e) e.preventDefault();
    if (!state.slaId) {
      showPrompt();
      if ($("slaReportSla")) $("slaReportSla").focus();
      return;
    }
    loadReport();
  }

  function resetFilter() {
    ["slaReportSla", "slaReportService", "slaReportFrom", "slaReportTo"].forEach(function (id) {
      var input = $(id);
      if (input) input.value = "";
    });
    state.slaId = null;
    state.serviceId = null;
    state.pendingServiceId = null;
    state.report = null;
    var summary = $("slaReportSummary");
    var card = $("slaReportCard");
    if (summary) summary.hidden = true;
    if (card) card.hidden = true;
    markPreset(null);
    setFilterOpen(true);
    try {
      var url = new URL(window.location.href);
      ["sla_id", "service_id", "from", "to"].forEach(function (key) {
        url.searchParams.delete(key);
      });
      window.history.replaceState({}, "", url.toString());
    } catch (_) {}
    showPrompt();
  }

  function markPreset(active) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-sla-preset]"), function (button) {
      button.setAttribute("aria-pressed", button === active ? "true" : "false");
    });
  }

  function applyPreset(kind, button) {
    var now = new Date();
    var year = now.getFullYear();
    var month = now.getMonth();
    var range = null;
    if (kind === "this_month") range = [new Date(year, month, 1), now];
    else if (kind === "last_month") range = [new Date(year, month - 1, 1), new Date(year, month, 0)];
    else if (kind === "last_3_months") range = [new Date(year, month - 2, 1), now];
    else if (kind === "this_year") range = [new Date(year, 0, 1), now];
    if (!range) return;
    setDateInputs(isoDate(range[0]), isoDate(range[1]));
    markPreset(button);
    if (!state.slaId) {
      showPrompt();
      if (window.svcToast) window.svcToast("Pilih SLA dulu, lalu Apply.", "info");
      return;
    }
    loadReport();
  }

  function copyReportLink() {
    if (!state.slaId) {
      if (window.svcToast) window.svcToast("Pilih SLA dulu untuk membuat tautan laporan.", "error");
      return;
    }
    var link = syncUrl();
    function done() {
      if (window.svcToast) window.svcToast("Tautan laporan disalin.");
    }
    function fallback() {
      var area = document.createElement("textarea");
      area.value = link;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        done();
      } catch (_) {
        if (window.svcToast) window.svcToast("Gagal menyalin tautan.", "error");
      }
      area.remove();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(link).then(done).catch(fallback);
    } else {
      fallback();
    }
  }

  function bindDateField(textId, pickerId, buttonId) {
    var input = $(textId);
    var picker = $(pickerId);
    var button = $(buttonId);
    if (!input || !picker || !button) return;
    button.addEventListener("click", function () {
      var current = input.value.trim();
      picker.value = isValidDate(current) ? current : "";
      if (typeof picker.showPicker === "function") {
        try {
          picker.showPicker();
          return;
        } catch (_) {}
      }
      input.focus();
      input.select();
    });
    picker.addEventListener("change", function () {
      if (picker.value) input.value = picker.value;
    });
  }

  function focusField(id) {
    var input = $(id);
    if (!input) return;
    input.focus();
    input.select();
  }

  function bind() {
    var toggle = $("btnToggleSlaReportFilter");
    var form = $("slaReportFilterForm");
    if (toggle && form) toggle.addEventListener("click", function () {
      setFilterOpen(form.hidden);
    });
    if (form) form.addEventListener("submit", applyFilter);
    var reset = $("btnResetSlaReport");
    if (reset) reset.addEventListener("click", resetFilter);
    var presets = document.querySelector(".zbx-sla-report-presets");
    if (presets) presets.addEventListener("click", function (e) {
      var button = e.target.closest("[data-sla-preset]");
      if (button) applyPreset(button.getAttribute("data-sla-preset"), button);
    });
    var copy = $("btnCopySlaReportLink");
    if (copy) copy.addEventListener("click", copyReportLink);
    ["slaReportFrom", "slaReportTo"].forEach(function (id) {
      var input = $(id);
      if (input) input.addEventListener("input", function () { markPreset(null); });
    });
    var selectSlaBtn = $("btnSelectSla");
    if (selectSlaBtn) selectSlaBtn.addEventListener("click", openSlaPicker);
    var selectServiceBtn = $("btnSelectService");
    if (selectServiceBtn) selectServiceBtn.addEventListener("click", openServicePicker);

    var slaPicker = $("slaPickerDialog");
    if (slaPicker) {
      slaPicker.addEventListener("click", function (e) {
        if (e.target === slaPicker) closeSlaPicker();
        var choice = e.target.closest("[data-sla-choice]");
        if (choice) selectSla(choice.getAttribute("data-sla-choice"));
      });
      slaPicker.addEventListener("close", closeSlaPicker);
    }
    var closeSlaPickerBtn = $("btnCloseSlaPicker");
    if (closeSlaPickerBtn) closeSlaPickerBtn.addEventListener("click", closeSlaPicker);
    var cancelSlaPicker = $("btnSlaPickerCancel");
    if (cancelSlaPicker) cancelSlaPicker.addEventListener("click", closeSlaPicker);

    var servicePicker = $("servicePickerDialog");
    if (servicePicker) {
      servicePicker.addEventListener("click", function (e) {
        if (e.target === servicePicker) closeServicePicker();
        var choice = e.target.closest("[data-service-choice]");
        if (choice) selectService(choice.getAttribute("data-service-choice"));
      });
      servicePicker.addEventListener("close", closeServicePicker);
    }
    var closeServicePickerBtn = $("btnCloseServicePicker");
    if (closeServicePickerBtn) closeServicePickerBtn.addEventListener("click", closeServicePicker);
    var cancelServicePicker = $("btnServicePickerCancel");
    if (cancelServicePicker) cancelServicePicker.addEventListener("click", closeServicePicker);
    var filterService = $("btnFilterServicePicker");
    if (filterService) filterService.addEventListener("click", filterServicePicker);
    var resetService = $("btnResetServicePicker");
    if (resetService) resetService.addEventListener("click", resetServicePicker);
    var serviceName = $("servicePickerName");
    if (serviceName) serviceName.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        filterServicePicker();
      }
    });

    bindDateField("slaReportFrom", "slaReportFromPicker", "btnSlaReportFromCalendar");
    bindDateField("slaReportTo", "slaReportToPicker", "btnSlaReportToCalendar");

    var slaInput = $("slaReportSla");
    if (slaInput) slaInput.addEventListener("input", function () {
      var selected = state.slaId ? slaById(state.slaId) : null;
      if (!selected || slaInput.value.trim() !== selected.name) state.slaId = null;
    });
    var serviceInput = $("slaReportService");
    if (serviceInput) serviceInput.addEventListener("input", function () {
      state.serviceId = null;
    });

    var help = $("btnSlaReportHelp");
    if (help) help.addEventListener("click", function () {
      window.svcToast("Pilih SLA, atur rentang tanggal, lalu Apply. Error budget dihitung dari SLO dan downtime tertimbang waktu.", "info");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    bind();
    setFilterOpen(true);
    loadSlas();
  });
})();
