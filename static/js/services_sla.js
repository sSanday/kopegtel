/* ============================================================
   NMS Kopegtel — halaman SLA.
   Daftar SLA gaya konsol Zabbix, filter, dan dialog Create SLA.
   ============================================================ */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return window.svcEsc ? window.svcEsc(value) : String(value == null ? "" : value);
  }
  function norm(value) {
    return String(value == null ? "" : value).trim().toLowerCase();
  }

  var state = {
    filterOpen: true,
    status: "any",
    tagEval: "and_or",
    sort: "name_asc",
    rows: [],
    selected: {},
    editingId: null,
    pendingDelete: [],
    loading: false,
    error: "",
    filter: { name: "", status: "any", tags: [] }
  };

  function emptyHtml(filtered) {
    var title = filtered ? "No SLA match the filter." : "No SLA yet.";
    var hint = filtered
      ? "Ubah atau reset filter untuk melihat SLA lain."
      : "Buat SLA setelah service memiliki tag yang cocok.";
    var action = filtered
      ? '<button class="zbx-btn" type="button" data-act="reset-filter">Reset filter</button>'
      : '<button class="zbx-btn primary" type="button" data-act="create-sla">Create SLA</button>';
    return '<tr><td colspan="9" class="zbx-sla-empty-cell">' +
      '<div class="zbx-sla-empty">' +
        '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true">' +
          '<circle cx="20" cy="20" r="11.5"></circle>' +
          '<path d="M28.5 28.5 39 39"></path>' +
        '</svg>' +
        '<span class="zbx-sla-empty-title">' + esc(title) + '</span>' +
        '<span>' + esc(hint) + '</span>' +
        action +
      '</div>' +
    '</td></tr>';
  }

  function loadingHtml() {
    return '<tr><td colspan="9" class="zbx-sla-empty-cell">' +
      '<div class="zbx-sla-empty" role="status">' +
        '<span class="zbx-spinner" aria-hidden="true"></span>' +
        '<span>Memuat data…</span>' +
      '</div>' +
    '</td></tr>';
  }

  function errorHtml(message) {
    return '<tr><td colspan="9" class="zbx-sla-empty-cell">' +
      '<div class="zbx-sla-empty" role="alert">' +
        '<span class="zbx-sla-empty-title">Gagal memuat SLA</span>' +
        '<span>' + esc(message || "Terjadi kesalahan.") + '</span>' +
        '<button class="zbx-btn primary" type="button" data-act="retry">Coba lagi</button>' +
      '</div>' +
    '</td></tr>';
  }

  function formatSlo(value) {
    var number = Number(value);
    if (!isFinite(number)) return "—";
    return number.toFixed(2).replace(/0+$/, "").replace(/\.$/, "") + "%";
  }

  function formatDuration(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    var days = Math.floor(total / 86400);
    var hours = Math.floor((total % 86400) / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var secs = total % 60;
    var parts = [];
    if (days) parts.push(days + "d");
    if (hours) parts.push(hours + "h");
    if (minutes) parts.push(minutes + "m");
    return parts.join(" ") || (secs + "s");
  }

  function parseHoursRanges(text) {
    return String(text || "").split(",").map(function (item) {
      return item.trim();
    }).filter(function (item) {
      return item.length > 0;
    }).map(function (item) {
      var parts = item.split("-");
      return [String(parts[0] || "").trim(), String(parts[1] || "").trim()];
    });
  }

  var FILTER_TAG_OPTIONS =
    '<option value="exists">Exists</option>' +
    '<option value="equals">Equals</option>' +
    '<option value="contains" selected>Contains</option>' +
    '<option value="not_exists">Does not exist</option>' +
    '<option value="not_equal">Does not equal</option>' +
    '<option value="not_contains">Does not contain</option>';

  var FORM_TAG_OPTIONS =
    '<option value="equals" selected>Equals</option>' +
    '<option value="contains">Contains</option>';

  var REPORTING_LABELS = {
    daily: "Daily",
    weekly: "Weekly",
    monthly: "Monthly",
    quarterly: "Quarterly",
    annually: "Annually"
  };

  var SCHEDULE_DEFAULT_HOURS = "8:00-17:00";
  var SCHEDULE_DEFAULT_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"];
  var MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
  ];
  var calendarView = null;

  var filterTagOps = ["exists", "equals", "contains", "not_exists", "not_equal", "not_contains"];

  function isoFromDate(date) {
    return date.getFullYear() + "-" +
      String(date.getMonth() + 1).padStart(2, "0") + "-" +
      String(date.getDate()).padStart(2, "0");
  }

  function todayIso() { return isoFromDate(new Date()); }

  function isValidDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    var parts = value.split("-").map(Number);
    var d = new Date(parts[0], parts[1] - 1, parts[2]);
    return d.getFullYear() === parts[0] &&
      d.getMonth() === parts[1] - 1 &&
      d.getDate() === parts[2];
  }

  function filterTagRowHtml() {
    return '<div class="zbx-tag-row" data-sla-tag-row>' +
      '<input class="zbx-input zbx-tag-name" type="text" placeholder="tag" aria-label="Tag name" autocomplete="off">' +
      '<select class="zbx-input zbx-tag-op" aria-label="Tag operator">' + FILTER_TAG_OPTIONS + '</select>' +
      '<input class="zbx-input zbx-tag-value" type="text" placeholder="value" aria-label="Tag value" autocomplete="off">' +
      '<button type="button" class="zbx-text-link" data-remove-tag>Remove</button>' +
    '</div>';
  }

  function formTagRowHtml() {
    return '<tr data-sla-form-tag>' +
      '<td><input class="zbx-input" type="text" data-sla-tag-name maxlength="64" placeholder="tag" aria-label="Tag name" autocomplete="off"></td>' +
      '<td><select class="zbx-input" data-sla-tag-op aria-label="Tag operation">' + FORM_TAG_OPTIONS + '</select></td>' +
      '<td><input class="zbx-input" type="text" data-sla-tag-value maxlength="64" placeholder="value" aria-label="Tag value" autocomplete="off"></td>' +
      '<td><button type="button" class="zbx-text-link" data-remove-sla-tag>Remove</button></td>' +
    '</tr>';
  }

  function downtimeRowHtml(downtime) {
    var seconds = Number(downtime.duration_seconds || 0);
    var label = downtime.duration || formatDuration(seconds);
    return '<tr data-sla-downtime data-start="' + esc(downtime.start) + '" data-duration-seconds="' +
      seconds + '" data-name="' + esc(downtime.name) + '">' +
      '<td>' + esc(downtime.start) + '</td>' +
      '<td>' + esc(label) + '</td>' +
      '<td>' + esc(downtime.name) + '</td>' +
      '<td><button class="zbx-text-link" type="button" data-remove-downtime>Remove</button></td>' +
    '</tr>';
  }

  /* ---------- Filter ---------- */

  function syncSort() {
    var arrow = $("slaSortArrow");
    var button = $("slaSortName");
    if (arrow) arrow.classList.toggle("desc", state.sort === "name_desc");
    if (button) {
      button.setAttribute(
        "aria-label",
        "Urutkan berdasarkan nama SLA (kini " + (state.sort === "name_asc" ? "A-Z" : "Z-A") + ")"
      );
      var th = button.closest("th");
      if (th) th.setAttribute("aria-sort", state.sort === "name_asc" ? "ascending" : "descending");
    }
  }

  function setFilterOpen(open) {
    state.filterOpen = !!open;
    var panel = $("slaFilterPanel");
    var body = $("slaFilterForm");
    var button = $("btnToggleSlaFilter");
    if (panel) panel.classList.toggle("is-collapsed", !open);
    if (body) body.hidden = !open;
    if (button) {
      button.classList.toggle("collapsed", !open);
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }

  function setStatus(status) {
    state.status = status;
    var group = $("slaFilterStatus");
    if (!group) return;
    Array.prototype.forEach.call(group.querySelectorAll("button[data-status]"), function (button) {
      var on = button.getAttribute("data-status") === status;
      button.classList.toggle("on", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setTagEval(value) {
    state.tagEval = value;
    var group = $("slaFilterTagOp");
    if (!group) return;
    Array.prototype.forEach.call(group.querySelectorAll("button[data-eval]"), function (button) {
      var on = button.getAttribute("data-eval") === value;
      button.classList.toggle("on", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function addFilterTagRow() {
    var list = $("slaFilterTagList");
    if (!list) return null;
    list.insertAdjacentHTML("beforeend", filterTagRowHtml());
    var rows = list.querySelectorAll("[data-sla-tag-row]");
    return rows[rows.length - 1];
  }

  function resetFilters() {
    var name = $("slaFilterName");
    if (name) name.value = "";
    setStatus("any");
    setTagEval("and_or");
    var list = $("slaFilterTagList");
    if (list) list.innerHTML = filterTagRowHtml();
    state.filter = { name: "", status: "any", tags: [] };
    clearSelection();
    renderTable();
  }

  function readFilterTags() {
    var tags = [];
    Array.prototype.forEach.call(
      document.querySelectorAll("#slaFilterTagList [data-sla-tag-row]"),
      function (row) {
        var nameEl = row.querySelector(".zbx-tag-name");
        var opEl = row.querySelector(".zbx-tag-op");
        var valueEl = row.querySelector(".zbx-tag-value");
        var name = nameEl ? nameEl.value.trim() : "";
        if (!name) return;
        var op = opEl ? opEl.value : "contains";
        if (filterTagOps.indexOf(op) === -1) op = "contains";
        tags.push({
          name: name,
          op: op,
          value: valueEl ? valueEl.value.trim() : ""
        });
      }
    );
    return tags;
  }

  function applyFilters() {
    state.filter = {
      name: ($("slaFilterName") && $("slaFilterName").value.trim()) || "",
      status: state.status,
      tags: readFilterTags()
    };
    clearSelection();
    renderTable();
  }

  /* ---------- Tabel ---------- */
  function tagConditionMatches(condition, tags) {
    var op = condition.op;
    var positive = { not_equal: "equals", not_contains: "contains", not_exists: "exists" }[op] || op;
    var wantedName = norm(condition.name);
    var wantedValue = norm(condition.value);
    var matched = (tags || []).some(function (tag) {
      var tagName = norm(tag.name);
      var tagValue = norm(tag.value);
      if (tagName !== wantedName) return false;
      if (positive === "exists") return true;
      if (positive === "equals") return tagValue === wantedValue;
      if (positive === "contains") return tagValue.indexOf(wantedValue) !== -1;
      return false;
    });
    return op.indexOf("not_") === 0 ? !matched : matched;
  }

  function rowMatches(row) {
    var filter = state.filter;
    if (filter.name && norm(row.name).indexOf(norm(filter.name)) === -1) return false;
    if (filter.status === "enabled" && !row.enabled) return false;
    if (filter.status === "disabled" && row.enabled) return false;
    if (filter.tags.length) {
      var results = filter.tags.map(function (condition) {
        return tagConditionMatches(condition, row.tags || []);
      });
      var match = state.tagEval === "or"
        ? results.some(function (value) { return value; })
        : results.every(function (value) { return value; });
      if (!match) return false;
    }
    return true;
  }

  function visibleRows() {
    var rows = state.rows.filter(rowMatches);
    rows.sort(function (a, b) {
      var cmp = a.name.localeCompare(b.name, "id", { sensitivity: "base" });
      return state.sort === "name_asc" ? cmp : -cmp;
    });
    return rows;
  }

  function rowHtml(row) {
    return '<tr data-sla-id="' + row.id + '">' +
      '<td class="zbx-col-check"><input class="zbx-check" type="checkbox" data-sla-select="' + row.id + '"' +
        (state.selected[row.id] ? " checked" : "") + ' aria-label="Pilih SLA ' + esc(row.name) + '"></td>' +
      '<td><button class="zbx-sla-name zbx-sla-name-btn" type="button" data-sla-edit="' + row.id + '">' +
        esc(row.name) + '</button></td>' +
      '<td>' + esc(formatSlo(row.slo)) + '</td>' +
      '<td>' + esc(row.effective_date) + '</td>' +
      '<td>' + esc(row.reporting_label) + '</td>' +
      '<td>' + esc(row.timezone_label) + '</td>' +
      '<td>' + esc(row.schedule_label) + '</td>' +
      '<td><a class="zbx-sla-report-link" href="/services/sla/report?sla_id=' + row.id + '">SLA report</a></td>' +
      '<td><button class="zbx-sla-state ' + (row.enabled ? "enabled" : "disabled") + '"' +
        ' type="button" data-sla-toggle="' + row.id + '" aria-pressed="' + (row.enabled ? "true" : "false") + '"' +
        ' title="' + (row.enabled ? "Disable SLA" : "Enable SLA") + '">' +
        (row.enabled ? "Enabled" : "Disabled") + '</button></td>' +
    '</tr>';
  }

  function updateTableInfo(rows) {
    var info = $("slaTableInfo");
    if (!info) return;
    if (state.loading) {
      info.textContent = "Memuat data…";
    } else if (state.error) {
      info.textContent = "Displaying 0 of 0 found";
    } else {
      info.textContent = "Displaying " + rows.length + " of " + state.rows.length + " found";
    }
  }

  function renderTable() {
    var body = $("slaBody");
    if (!body) return;
    body.setAttribute("aria-busy", state.loading ? "true" : "false");
    var rows = visibleRows();
    if (state.loading && !state.rows.length) {
      body.innerHTML = loadingHtml();
    } else if (state.error && !state.rows.length) {
      body.innerHTML = errorHtml(state.error);
    } else if (!rows.length) {
      body.innerHTML = emptyHtml(state.rows.length > 0);
    } else {
      body.innerHTML = rows.map(rowHtml).join("");
    }
    updateTableInfo(rows);
    syncSelectionUi(rows);
  }

  function loadSlas() {
    state.loading = true;
    state.error = "";
    renderTable();
    return window.svcJson("/api/sla").then(function (data) {
      state.rows = (data && data.slas) || [];
      state.loading = false;
      renderTable();
      return state.rows;
    }).catch(function (err) {
      state.loading = false;
      state.error = (err && err.message) || "Gagal memuat SLA";
      renderTable();
    });
  }

  function clearSelection() {
    state.selected = {};
  }

  function selectedCount() {
    return state.rows.filter(function (row) { return state.selected[row.id]; }).length;
  }

  function syncSelectionUi(rows) {
    rows = rows || visibleRows();
    var count = selectedCount();
    var label = $("slaSelectedCount");
    if (label) label.textContent = count + " selected";
    ["btnSlaEnable", "btnSlaDisable", "btnSlaDelete"].forEach(function (id) {
      var button = $(id);
      if (button) button.disabled = count === 0;
    });
    var selectAll = $("slaSelectAll");
    if (selectAll) {
      var selectedVisible = rows.filter(function (row) { return state.selected[row.id]; }).length;
      selectAll.checked = rows.length > 0 && selectedVisible === rows.length;
      selectAll.indeterminate = selectedVisible > 0 && selectedVisible < rows.length;
    }
  }

  function setSlaTab(tab) {
    var general = tab !== "downtimes";
    var generalTab = $("tabSlaGeneral");
    var downTab = $("tabSlaDowntimes");
    var generalPane = $("slaGeneralPane");
    var downPane = $("slaDowntimesPane");
    if (generalTab) {
      generalTab.classList.toggle("on", general);
      generalTab.setAttribute("aria-selected", general ? "true" : "false");
      generalTab.tabIndex = general ? 0 : -1;
    }
    if (downTab) {
      downTab.classList.toggle("on", !general);
      downTab.setAttribute("aria-selected", general ? "false" : "true");
      downTab.tabIndex = general ? -1 : 0;
    }
    if (generalPane) generalPane.hidden = !general;
    if (downPane) downPane.hidden = general;
  }

  /* ---------- Dialog Create SLA ---------- */
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

  function setSegment(group, value) {
    if (!group) return;
    Array.prototype.forEach.call(group.querySelectorAll("button[data-value]"), function (button) {
      var on = button.getAttribute("data-value") === value;
      button.classList.toggle("on", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function segmentValue(group) {
    if (!group) return "";
    var on = group.querySelector("button[data-value].on");
    return on ? on.getAttribute("data-value") : "";
  }

  function scheduleDayBoxes() {
    var panel = $("slaScheduleCustom");
    return panel ? panel.querySelectorAll("[data-schedule-day]") : [];
  }

  function scheduleHoursInput(day) {
    var panel = $("slaScheduleCustom");
    return panel ? panel.querySelector('[data-schedule-hours="' + day + '"]') : null;
  }

  function setScheduleMode(mode) {
    setSegment($("slaSchedule"), mode);
    var panel = $("slaScheduleCustom");
    if (panel) panel.hidden = mode !== "custom";
  }

  function resetCustomSchedule() {
    Array.prototype.forEach.call(scheduleDayBoxes(), function (box) {
      var day = box.getAttribute("data-schedule-day");
      var checked = SCHEDULE_DEFAULT_DAYS.indexOf(day) !== -1;
      var input = scheduleHoursInput(day);
      box.checked = checked;
      if (!input) return;
      input.disabled = !checked;
      delete input.dataset.previousHours;
      input.value = checked ? SCHEDULE_DEFAULT_HOURS : "";
    });
  }

  function syncScheduleDay(box) {
    var day = box.getAttribute("data-schedule-day");
    var input = scheduleHoursInput(day);
    if (!input) return;
    if (box.checked) {
      input.disabled = false;
      input.value = input.dataset.previousHours || SCHEDULE_DEFAULT_HOURS;
      delete input.dataset.previousHours;
    } else {
      if (input.value) input.dataset.previousHours = input.value;
      input.value = "";
      input.disabled = true;
    }
  }

  function readCustomSchedule() {
    var days = [];
    var checked = 0;
    var missing = null;
    Array.prototype.forEach.call(scheduleDayBoxes(), function (box) {
      if (!box.checked) return;
      checked += 1;
      var day = box.getAttribute("data-schedule-day");
      var input = scheduleHoursInput(day);
      var hours = input ? input.value.trim() : "";
      if (!hours && !missing) missing = input;
      if (hours) days.push({ day: day, hours: hours });
    });
    return { days: days, checked: checked, missing: missing };
  }

  function readDowntimes() {
    var rows = [];
    Array.prototype.forEach.call(
      document.querySelectorAll("#slaDowntimeList [data-sla-downtime]"),
      function (row) {
        rows.push({
          start: row.getAttribute("data-start") || "",
          duration_seconds: Number(row.getAttribute("data-duration-seconds") || 0),
          name: row.getAttribute("data-name") || ""
        });
      }
    );
    return rows;
  }

  /* ---------- Dialog New excluded downtime ---------- */
  function fillDowntimeDurationOptions() {
    var hours = $("downtimeHours");
    var minutes = $("downtimeMinutes");
    var i;
    if (hours && !hours.options.length) {
      var hourHtml = "";
      for (i = 0; i < 24; i += 1) {
        hourHtml += '<option value="' + i + '">' + i + '</option>';
      }
      hours.innerHTML = hourHtml;
    }
    if (minutes && !minutes.options.length) {
      var minuteHtml = "";
      for (i = 0; i < 60; i += 1) {
        minuteHtml += '<option value="' + i + '">' + i + '</option>';
      }
      minutes.innerHTML = minuteHtml;
    }
  }

  function resetDowntimeForm() {
    fillDowntimeDurationOptions();
    if ($("downtimeName")) $("downtimeName").value = "";
    if ($("downtimeStart")) $("downtimeStart").value = todayIso() + " 00:00";
    if ($("downtimeDays")) $("downtimeDays").value = "0";
    if ($("downtimeHours")) $("downtimeHours").value = "1";
    if ($("downtimeMinutes")) $("downtimeMinutes").value = "0";
    hideDowntimeError();
  }

  function hideDowntimeError() {
    var error = $("downtimeError");
    if (error) {
      error.hidden = true;
      error.textContent = "";
    }
  }

  function showDowntimeError(message, focusId) {
    var error = $("downtimeError");
    if (error) {
      error.textContent = message;
      error.hidden = false;
    }
    if (focusId && $(focusId)) $(focusId).focus();
  }

  function openDowntimeDialog() {
    setCalendarOpen(false);
    resetDowntimeForm();
    showDialog($("downtimeDialog"));
    setTimeout(function () {
      if ($("downtimeName")) $("downtimeName").focus();
    }, 30);
  }

  function closeDowntimeDialog() {
    hideDialog($("downtimeDialog"));
    if ($("btnAddSlaDowntime")) $("btnAddSlaDowntime").focus();
  }

  function parseDowntimeStart(value) {
    var match = /^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})$/.exec(value);
    if (!match || !isValidDate(match[1])) return null;
    var hour = Number(match[2]);
    var minute = Number(match[3]);
    if (hour > 23 || minute > 59) return null;
    return match[1];
  }

  function formatDowntimeDuration(days, hours, minutes) {
    var parts = [];
    if (days) parts.push(days + "d");
    if (hours) parts.push(hours + "h");
    if (minutes) parts.push(minutes + "m");
    return parts.join(" ") || "0m";
  }

  function addDowntimeFromDialog() {
    var name = ($("downtimeName") && $("downtimeName").value.trim()) || "";
    if (!name) {
      showDowntimeError("Name wajib diisi.", "downtimeName");
      return;
    }

    var start = ($("downtimeStart") && $("downtimeStart").value.trim()) || "";
    if (!parseDowntimeStart(start)) {
      showDowntimeError("Start time harus format YYYY-MM-DD HH:MM.", "downtimeStart");
      return;
    }

    var days = parseInt($("downtimeDays") ? $("downtimeDays").value : "0", 10);
    var hours = parseInt($("downtimeHours") ? $("downtimeHours").value : "0", 10);
    var minutes = parseInt($("downtimeMinutes") ? $("downtimeMinutes").value : "0", 10);
    if (!isFinite(days) || days < 0 || !isFinite(hours) || !isFinite(minutes) ||
        (days === 0 && hours === 0 && minutes === 0)) {
      showDowntimeError("Duration harus lebih dari 0.", "downtimeDays");
      return;
    }

    var body = $("slaDowntimeList");
    if (!body) return;
    body.insertAdjacentHTML("beforeend", downtimeRowHtml({
      start: start,
      name: name,
      duration: formatDowntimeDuration(days, hours, minutes),
      duration_seconds: days * 86400 + hours * 3600 + minutes * 60
    }));
    closeDowntimeDialog();
  }

  function openDowntimeDatePicker() {
    var input = $("downtimeStart");
    var picker = $("downtimeDatePicker");
    if (!input || !picker) return;
    var datePart = input.value.trim().slice(0, 10);
    picker.value = isValidDate(datePart) ? datePart : todayIso();
    if (typeof picker.showPicker === "function") {
      try {
        picker.showPicker();
        return;
      } catch (_) {}
    }
    input.focus();
    input.setSelectionRange(0, 10);
  }

  /* ---------- Kalender Effective date ---------- */
  function parseIsoDate(value) {
    if (!isValidDate(value)) return null;
    var parts = value.split("-").map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  function renderCalendar() {
    var title = $("slaCalendarTitle");
    var grid = $("slaCalendarGrid");
    if (!title || !grid || !calendarView) return;
    var year = calendarView.year;
    var month = calendarView.month;
    title.textContent = MONTH_NAMES[month] + " " + year;

    var input = $("slaEffectiveDate");
    var selected = input ? parseIsoDate(input.value.trim()) : null;
    var today = new Date();
    var offset = (new Date(year, month, 1).getDay() + 6) % 7;
    var total = new Date(year, month + 1, 0).getDate();
    var html = "";
    var i;

    for (i = 0; i < offset; i += 1) {
      html += '<span class="zbx-sla-cal-empty" aria-hidden="true"></span>';
    }
    for (i = 1; i <= total; i += 1) {
      var isSelected = selected && selected.getFullYear() === year &&
        selected.getMonth() === month && selected.getDate() === i;
      var isToday = today.getFullYear() === year &&
        today.getMonth() === month && today.getDate() === i;
      html += '<button class="zbx-sla-cal-day" type="button" data-cal-day="' + i + '"' +
        (isSelected ? ' aria-pressed="true"' : "") +
        (isToday ? ' data-today="true"' : "") +
        ' aria-label="' + i + " " + MONTH_NAMES[month] + " " + year + '">' + i + '</button>';
    }
    grid.innerHTML = html;
  }

  function setCalendarOpen(open) {
    var popover = $("slaCalendarPopover");
    var button = $("btnSlaCalendar");
    if (!popover) return;
    popover.hidden = !open;
    if (button) button.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) return;
    var input = $("slaEffectiveDate");
    var selected = input ? parseIsoDate(input.value.trim()) : null;
    var base = selected || new Date();
    calendarView = { year: base.getFullYear(), month: base.getMonth() };
    renderCalendar();
  }

  function toggleCalendar() {
    var popover = $("slaCalendarPopover");
    if (popover) setCalendarOpen(popover.hidden);
  }

  function moveCalendarMonth(delta) {
    if (!calendarView) return;
    var next = new Date(calendarView.year, calendarView.month + delta, 1);
    calendarView = { year: next.getFullYear(), month: next.getMonth() };
    renderCalendar();
  }

  function selectCalendarDay(day) {
    if (!calendarView) return;
    var input = $("slaEffectiveDate");
    if (!input) return;
    input.value = isoFromDate(new Date(calendarView.year, calendarView.month, day));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    setCalendarOpen(false);
    input.focus();
  }

  function resetSlaForm() {
    state.editingId = null;
    setCalendarOpen(false);
    if ($("slaName")) $("slaName").value = "";
    if ($("slaSlo")) $("slaSlo").value = "99.9";
    setSegment($("slaReporting"), "monthly");
    if ($("slaTimezone")) $("slaTimezone").value = "Asia/Jakarta";
    setScheduleMode("24x7");
    resetCustomSchedule();
    if ($("slaDowntimeList")) $("slaDowntimeList").innerHTML = "";
    closeDowntimeDialog();
    if ($("slaEffectiveDate")) $("slaEffectiveDate").value = todayIso();
    var tagBody = $("slaFormTagList");
    if (tagBody) tagBody.innerHTML = formTagRowHtml();
    if ($("slaDescription")) $("slaDescription").value = "";
    if ($("slaEnabled")) $("slaEnabled").checked = true;
    if ($("slaDialogTitle")) $("slaDialogTitle").textContent = "New SLA";
    if ($("btnSlaDialogAdd")) $("btnSlaDialogAdd").textContent = "Add";
    var error = $("slaFormError");
    if (error) {
      error.hidden = true;
      error.textContent = "";
    }
    setSlaTab("general");
  }

  function populateSchedule(schedule) {
    schedule = schedule || { mode: "24x7" };
    setScheduleMode(schedule.mode || "24x7");
    var days = schedule.days || {};
    Array.prototype.forEach.call(scheduleDayBoxes(), function (box) {
      var day = box.getAttribute("data-schedule-day");
      var ranges = days[day] || null;
      var input = scheduleHoursInput(day);
      box.checked = !!ranges;
      if (!input) return;
      input.disabled = !ranges;
      input.value = ranges
        ? ranges.map(function (range) { return range[0] + "-" + range[1]; }).join(", ")
        : "";
    });
  }

  function populateSlaForm(row) {
    if ($("slaName")) $("slaName").value = row.name || "";
    if ($("slaSlo")) $("slaSlo").value = row.slo;
    setSegment($("slaReporting"), row.reporting_period || "monthly");
    if ($("slaTimezone")) $("slaTimezone").value = row.timezone || "Asia/Jakarta";
    if ($("slaEffectiveDate")) $("slaEffectiveDate").value = row.effective_date || todayIso();
    if ($("slaDescription")) $("slaDescription").value = row.description || "";
    if ($("slaEnabled")) $("slaEnabled").checked = !!row.enabled;
    populateSchedule(row.schedule);

    var tagBody = $("slaFormTagList");
    if (tagBody) {
      tagBody.innerHTML = "";
      (row.tags || []).forEach(function (tag) {
        tagBody.insertAdjacentHTML("beforeend", formTagRowHtml());
        var line = tagBody.querySelector("[data-sla-form-tag]:last-child");
        if (!line) return;
        var name = line.querySelector("[data-sla-tag-name]");
        var op = line.querySelector("[data-sla-tag-op]");
        var value = line.querySelector("[data-sla-tag-value]");
        if (name) name.value = tag.name || "";
        if (op) op.value = tag.op === "contains" ? "contains" : "equals";
        if (value) value.value = tag.value || "";
      });
      if (!(row.tags || []).length) addSlaFormTagRow();
    }

    var downtimeBody = $("slaDowntimeList");
    if (downtimeBody) {
      downtimeBody.innerHTML = "";
      (row.downtimes || []).forEach(function (item) {
        downtimeBody.insertAdjacentHTML("beforeend", downtimeRowHtml({
          start: item.start,
          name: item.name,
          duration_seconds: item.duration_seconds
        }));
      });
    }
  }

  function openSlaDialog(slaId) {
    var row = null;
    if (slaId !== undefined && slaId !== null) {
      row = state.rows.filter(function (item) { return String(item.id) === String(slaId); })[0] || null;
    }
    resetSlaForm();
    if (row) {
      populateSlaForm(row);
      state.editingId = row.id;
      if ($("slaDialogTitle")) $("slaDialogTitle").textContent = "Edit SLA";
      if ($("btnSlaDialogAdd")) $("btnSlaDialogAdd").textContent = "Update";
    }
    showDialog($("slaDialog"));
    setTimeout(function () {
      if ($("slaName")) $("slaName").focus();
    }, 30);
  }

  function closeSlaDialog() {
    setCalendarOpen(false);
    hideDialog($("downtimeDialog"));
    hideDialog($("slaDialog"));
  }

  function showFormError(message, focusId) {
    setSlaTab("general");
    var error = $("slaFormError");
    if (error) {
      error.textContent = message;
      error.hidden = false;
    }
    if (focusId && $(focusId)) $(focusId).focus();
  }

  function readFormTags() {
    var tags = [];
    var incomplete = false;
    Array.prototype.forEach.call(
      document.querySelectorAll("#slaFormTagList [data-sla-form-tag]"),
      function (row) {
        var nameEl = row.querySelector("[data-sla-tag-name]");
        var opEl = row.querySelector("[data-sla-tag-op]");
        var valueEl = row.querySelector("[data-sla-tag-value]");
        var name = nameEl ? nameEl.value.trim() : "";
        var value = valueEl ? valueEl.value.trim() : "";
        if (!name && !value) return;
        if (!name || !value) {
          incomplete = true;
          return;
        }
        tags.push({
          name: name,
          op: opEl ? opEl.value : "equals",
          value: value
        });
      }
    );
    return { tags: tags, incomplete: incomplete };
  }

  function addSlaFromForm() {
    var name = ($("slaName") && $("slaName").value.trim()) || "";
    if (!name) {
      showFormError("Name wajib diisi.", "slaName");
      return;
    }

    var slo = parseFloat($("slaSlo") ? $("slaSlo").value : "");
    if (isNaN(slo) || slo < 0 || slo > 100) {
      showFormError("SLO harus angka 0–100.", "slaSlo");
      return;
    }

    var effectiveDate = ($("slaEffectiveDate") && $("slaEffectiveDate").value.trim()) || "";
    if (!isValidDate(effectiveDate)) {
      showFormError("Effective date harus format YYYY-MM-DD.", "slaEffectiveDate");
      return;
    }

    var tagResult = readFormTags();
    if (tagResult.incomplete) {
      showFormError("Name dan Value service tag harus diisi.", "slaName");
      return;
    }
    if (!tagResult.tags.length) {
      showFormError("Minimal satu service tag harus diisi.", "slaName");
      return;
    }

    var schedule = { mode: "24x7" };
    var scheduleMode = segmentValue($("slaSchedule")) || "24x7";
    if (scheduleMode === "custom") {
      var customSchedule = readCustomSchedule();
      if (!customSchedule.checked) {
        showFormError("Pilih minimal satu hari untuk Custom schedule.");
        return;
      }
      if (customSchedule.missing) {
        showFormError("Jam untuk hari yang dipilih wajib diisi.", customSchedule.missing.id);
        return;
      }
      var days = {};
      customSchedule.days.forEach(function (item) {
        days[item.day] = parseHoursRanges(item.hours);
      });
      schedule = { mode: "custom", days: days };
    }

    var existing = state.editingId
      ? state.rows.filter(function (item) { return String(item.id) === String(state.editingId); })[0]
      : null;
    var payload = {
      name: name,
      slo: slo,
      reporting_period: segmentValue($("slaReporting")) || "monthly",
      timezone: ($("slaTimezone") && $("slaTimezone").value) || "Asia/Jakarta",
      schedule: schedule,
      effective_date: effectiveDate,
      description: $("slaDescription") ? $("slaDescription").value.trim() : "",
      enabled: $("slaEnabled") ? $("slaEnabled").checked : true,
      tags: tagResult.tags,
      downtimes: readDowntimes(),
      tag_eval: existing ? existing.tag_eval : "and"
    };

    var button = $("btnSlaDialogAdd");
    var oldText = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "Menyimpan…";
    }
    var url = state.editingId ? "/api/sla/" + encodeURIComponent(state.editingId) : "/api/sla";
    var method = state.editingId ? "PUT" : "POST";
    window.svcJson(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function () {
      closeSlaDialog();
      return loadSlas();
    }).then(function () {
      if (window.svcToast) window.svcToast(state.editingId ? "SLA diperbarui." : "SLA ditambahkan.");
    }).catch(function (err) {
      showFormError((err && err.message) || "Gagal menyimpan SLA.", "slaName");
    }).finally(function () {
      if (button) {
        button.disabled = false;
        button.textContent = state.editingId ? "Update" : oldText || "Add";
      }
    });
  }

  function addSlaFormTagRow() {
    var body = $("slaFormTagList");
    if (!body) return;
    body.insertAdjacentHTML("beforeend", formTagRowHtml());
    var rows = body.querySelectorAll("[data-sla-form-tag]");
    var row = rows[rows.length - 1];
    var input = row && row.querySelector("[data-sla-tag-name]");
    if (input) input.focus();
  }

  function setSelectedEnabled(enabled) {
    var ids = Object.keys(state.selected).map(Number);
    if (!ids.length) return;
    Promise.all(ids.map(function (id) {
      return window.svcJson("/api/sla/" + encodeURIComponent(id) + "/enabled", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: enabled })
      });
    })).then(function () {
      clearSelection();
      return loadSlas();
    }).then(function () {
      if (window.svcToast) window.svcToast(enabled ? "SLA diaktifkan." : "SLA dinonaktifkan.");
    }).catch(function (err) {
      if (window.svcToast) window.svcToast((err && err.message) || "Gagal mengubah status SLA.", "error");
    });
  }

  function toggleSla(slaId) {
    var row = state.rows.filter(function (item) { return String(item.id) === String(slaId); })[0];
    if (!row) return;
    window.svcJson("/api/sla/" + encodeURIComponent(slaId) + "/enabled", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !row.enabled })
    }).then(function () {
      return loadSlas();
    }).then(function () {
      if (window.svcToast) window.svcToast(row.enabled ? "SLA dinonaktifkan." : "SLA diaktifkan.");
    }).catch(function (err) {
      if (window.svcToast) window.svcToast((err && err.message) || "Gagal mengubah status SLA.", "error");
    });
  }

  function openDeleteConfirm() {
    var ids = Object.keys(state.selected).map(Number);
    if (!ids.length) return;
    var rows = state.rows.filter(function (row) { return ids.indexOf(Number(row.id)) >= 0; });
    if (!rows.length) return;
    state.pendingDelete = ids;
    var message = $("slaConfirmMessage");
    if (message) {
      message.textContent = rows.length === 1
        ? "Hapus SLA berikut?"
        : "Hapus " + rows.length + " SLA berikut?";
    }
    var list = $("slaConfirmList");
    if (list) {
      list.innerHTML = rows.map(function (row) { return "<li>" + esc(row.name) + "</li>"; }).join("");
    }
    var error = $("slaConfirmError");
    if (error) {
      error.hidden = true;
      error.textContent = "";
    }
    var button = $("btnConfirmSlaDelete");
    if (button) {
      button.disabled = false;
      button.textContent = "Delete";
    }
    showDialog($("slaConfirmDialog"));
    if (button) button.focus();
  }

  function closeDeleteConfirm() {
    state.pendingDelete = [];
    hideDialog($("slaConfirmDialog"));
  }

  function deleteSelected() {
    var ids = state.pendingDelete.slice();
    if (!ids.length) return;
    var button = $("btnConfirmSlaDelete");
    if (button) {
      button.disabled = true;
      button.textContent = "Menghapus…";
    }
    Promise.all(ids.map(function (id) {
      return window.svcJson("/api/sla/" + encodeURIComponent(id), { method: "DELETE" });
    })).then(function () {
      closeDeleteConfirm();
      clearSelection();
      return loadSlas();
    }).then(function () {
      if (window.svcToast) window.svcToast("SLA dihapus.");
    }).catch(function (err) {
      var error = $("slaConfirmError");
      if (error) {
        error.textContent = (err && err.message) || "Gagal menghapus SLA.";
        error.hidden = false;
      }
      if (button) {
        button.disabled = false;
        button.textContent = "Delete";
      }
    });
  }

  function bind() {
    var toggle = $("btnToggleSlaFilter");
    if (toggle) toggle.addEventListener("click", function () {
      setFilterOpen(!state.filterOpen);
    });

    var statusGroup = $("slaFilterStatus");
    if (statusGroup) statusGroup.addEventListener("click", function (e) {
      var button = e.target.closest("button[data-status]");
      if (button) setStatus(button.getAttribute("data-status"));
    });

    var tagGroup = $("slaFilterTagOp");
    if (tagGroup) tagGroup.addEventListener("click", function (e) {
      var button = e.target.closest("button[data-eval]");
      if (button) setTagEval(button.getAttribute("data-eval"));
    });

    var addTag = $("btnAddSlaTag");
    if (addTag) addTag.addEventListener("click", addFilterTagRow);

    var tagList = $("slaFilterTagList");
    if (tagList) tagList.addEventListener("click", function (e) {
      var button = e.target.closest("[data-remove-tag]");
      if (!button) return;
      var row = button.closest("[data-sla-tag-row]");
      if (!row) return;
      row.remove();
      if (!tagList.querySelector("[data-sla-tag-row]")) addFilterTagRow();
    });

    var form = $("slaFilterForm");
    if (form) form.addEventListener("submit", function (e) {
      e.preventDefault();
      applyFilters();
    });

    var reset = $("btnResetSlaFilter");
    if (reset) reset.addEventListener("click", resetFilters);

    var sort = $("slaSortName");
    if (sort) sort.addEventListener("click", function () {
      state.sort = state.sort === "name_asc" ? "name_desc" : "name_asc";
      syncSort();
      renderTable();
    });

    var body = $("slaBody");
    if (body) body.addEventListener("change", function (e) {
      var box = e.target.closest("[data-sla-select]");
      if (!box) return;
      var id = Number(box.getAttribute("data-sla-select"));
      if (box.checked) state.selected[id] = true;
      else delete state.selected[id];
      syncSelectionUi();
    });
    if (body) body.addEventListener("click", function (e) {
      var edit = e.target.closest("[data-sla-edit]");
      if (edit) {
        openSlaDialog(edit.getAttribute("data-sla-edit"));
        return;
      }
      var toggle = e.target.closest("[data-sla-toggle]");
      if (toggle) {
        toggleSla(toggle.getAttribute("data-sla-toggle"));
        return;
      }
      var action = e.target.closest("[data-act]");
      if (!action) return;
      var act = action.getAttribute("data-act");
      if (act === "create-sla") openSlaDialog();
      else if (act === "reset-filter") resetFilters();
      else if (act === "retry") loadSlas();
    });

    var selectAll = $("slaSelectAll");
    if (selectAll) selectAll.addEventListener("change", function () {
      var rows = visibleRows();
      if (this.checked) rows.forEach(function (row) { state.selected[row.id] = true; });
      else rows.forEach(function (row) { delete state.selected[row.id]; });
      renderTable();
    });

    var create = $("btnCreateSla");
    if (create) create.addEventListener("click", function () { openSlaDialog(); });

    var help = $("btnSlaHelp");
    if (help) help.addEventListener("click", function () {
      if (window.svcToast) window.svcToast("Buat SLA, lalu klik nama SLA untuk mengedit atau status Enabled untuk mengaktifkan/menonaktifkan.", "info");
    });
    var dialogHelp = $("btnSlaDialogHelp");
    if (dialogHelp) dialogHelp.addEventListener("click", function () {
      if (window.svcToast) window.svcToast("Isi SLO, periode, zona waktu, jadwal, dan minimal satu service tag.", "info");
    });

    var close = $("btnCloseSlaDialog");
    if (close) close.addEventListener("click", closeSlaDialog);
    var cancel = $("btnSlaDialogCancel");
    if (cancel) cancel.addEventListener("click", closeSlaDialog);
    var add = $("btnSlaDialogAdd");
    if (add) add.addEventListener("click", addSlaFromForm);

    var dialog = $("slaDialog");
    if (dialog) {
      dialog.addEventListener("click", function (e) {
        if (e.target === dialog) closeSlaDialog();
      });
      dialog.addEventListener("close", function () { setCalendarOpen(false); });
    }

    var tabs = document.querySelector(".zbx-sla-tabs");
    if (tabs) tabs.addEventListener("click", function (e) {
      var tab = e.target.closest("[data-sla-tab]");
      if (tab) setSlaTab(tab.getAttribute("data-sla-tab"));
    });

    var reporting = $("slaReporting");
    if (reporting) reporting.addEventListener("click", function (e) {
      var button = e.target.closest("button[data-value]");
      if (button) setSegment(reporting, button.getAttribute("data-value"));
    });

    var schedule = $("slaSchedule");
    if (schedule) schedule.addEventListener("click", function (e) {
      var button = e.target.closest("button[data-value]");
      if (button) setScheduleMode(button.getAttribute("data-value"));
    });

    var customSchedule = $("slaScheduleCustom");
    if (customSchedule) customSchedule.addEventListener("change", function (e) {
      var box = e.target.closest("[data-schedule-day]");
      if (box) syncScheduleDay(box);
    });

    var addFormTag = $("btnAddSlaFormTag");
    if (addFormTag) addFormTag.addEventListener("click", addSlaFormTagRow);

    var addDowntime = $("btnAddSlaDowntime");
    if (addDowntime) addDowntime.addEventListener("click", openDowntimeDialog);

    var downtimeList = $("slaDowntimeList");
    if (downtimeList) downtimeList.addEventListener("click", function (e) {
      var button = e.target.closest("[data-remove-downtime]");
      if (!button) return;
      var row = button.closest("[data-sla-downtime]");
      if (row) row.remove();
    });

    fillDowntimeDurationOptions();
    var closeDowntime = $("btnCloseDowntimeDialog");
    if (closeDowntime) closeDowntime.addEventListener("click", closeDowntimeDialog);
    var cancelDowntime = $("btnDowntimeCancel");
    if (cancelDowntime) cancelDowntime.addEventListener("click", closeDowntimeDialog);
    var confirmDowntime = $("btnDowntimeAdd");
    if (confirmDowntime) confirmDowntime.addEventListener("click", addDowntimeFromDialog);
    var downtimeCalendar = $("btnDowntimeCalendar");
    if (downtimeCalendar) downtimeCalendar.addEventListener("click", openDowntimeDatePicker);
    var downtimeDatePicker = $("downtimeDatePicker");
    if (downtimeDatePicker) downtimeDatePicker.addEventListener("change", function () {
      var start = $("downtimeStart");
      if (!start || !this.value) return;
      var time = (start.value.match(/(\d{2}:\d{2})$/) || [])[1] || "00:00";
      start.value = this.value + " " + time;
      hideDowntimeError();
    });
    var downtimeDialog = $("downtimeDialog");
    if (downtimeDialog) {
      downtimeDialog.addEventListener("click", function (e) {
        if (e.target === downtimeDialog) closeDowntimeDialog();
      });
      downtimeDialog.addEventListener("close", function () {
        if ($("btnAddSlaDowntime")) $("btnAddSlaDowntime").focus();
      });
    }
    ["downtimeName", "downtimeStart"].forEach(function (id) {
      var input = $(id);
      if (!input) return;
      input.addEventListener("input", hideDowntimeError);
    });

    var formTagList = $("slaFormTagList");
    if (formTagList) formTagList.addEventListener("click", function (e) {
      var button = e.target.closest("[data-remove-sla-tag]");
      if (!button) return;
      var row = button.closest("[data-sla-form-tag]");
      if (!row) return;
      row.remove();
      if (!formTagList.querySelector("[data-sla-form-tag]")) addSlaFormTagRow();
    });

    var calendar = $("btnSlaCalendar");
    if (calendar) calendar.addEventListener("click", toggleCalendar);

    var calendarPopover = $("slaCalendarPopover");
    if (calendarPopover) calendarPopover.addEventListener("click", function (e) {
      var nav = e.target.closest("[data-cal-nav]");
      if (nav) {
        moveCalendarMonth(Number(nav.getAttribute("data-cal-nav")));
        return;
      }
      var dayButton = e.target.closest("[data-cal-day]");
      if (dayButton) selectCalendarDay(Number(dayButton.getAttribute("data-cal-day")));
    });

    document.addEventListener("click", function (e) {
      var popover = $("slaCalendarPopover");
      if (!popover || popover.hidden) return;
      var control = popover.closest(".zbx-sla-date");
      if (control && control.contains(e.target)) return;
      setCalendarOpen(false);
    });

    document.addEventListener("keydown", function (e) {
      var popover = $("slaCalendarPopover");
      if (!popover || popover.hidden || e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      setCalendarOpen(false);
      if (calendar) calendar.focus();
    }, true);

    var effectiveDate = $("slaEffectiveDate");
    if (effectiveDate) effectiveDate.addEventListener("focus", function () {
      setCalendarOpen(false);
    });

    ["slaName", "slaSlo", "slaEffectiveDate"].forEach(function (id) {
      var input = $(id);
      if (!input) return;
      input.addEventListener("input", function () {
        var error = $("slaFormError");
        if (error) error.hidden = true;
      });
    });

    var enable = $("btnSlaEnable");
    if (enable) enable.addEventListener("click", function () { setSelectedEnabled(true); });
    var disable = $("btnSlaDisable");
    if (disable) disable.addEventListener("click", function () { setSelectedEnabled(false); });
    var remove = $("btnSlaDelete");
    if (remove) remove.addEventListener("click", openDeleteConfirm);
    var confirmRemove = $("btnConfirmSlaDelete");
    if (confirmRemove) confirmRemove.addEventListener("click", deleteSelected);
    var cancelRemove = $("btnCancelSlaDelete");
    if (cancelRemove) cancelRemove.addEventListener("click", closeDeleteConfirm);
    var closeRemove = $("btnCloseSlaConfirm");
    if (closeRemove) closeRemove.addEventListener("click", closeDeleteConfirm);
    var confirmDialog = $("slaConfirmDialog");
    if (confirmDialog) {
      confirmDialog.addEventListener("click", function (e) {
        if (e.target === confirmDialog) closeDeleteConfirm();
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    bind();
    resetFilters();
    syncSort();
    loadSlas();
  });
})();
