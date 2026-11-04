/* Zabbix-style controls for the availability report. */
(function () {
  "use strict";

  var panel = document.getElementById("availabilityTimePanel");
  var toggle = document.getElementById("availabilityTimeToggle");
  var filterPanel = document.getElementById("availabilityFilterPanel");
  var filterButton = document.getElementById("availabilityFilterButton");
  var table = document.getElementById("availabilityTable");
  var modeSelect = document.getElementById("availabilityMode");
  if (!panel || !toggle) return;

  function setPanel(open) {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.classList.toggle("active", open);
  }

  toggle.addEventListener("click", function () {
    setPanel(panel.hidden);
  });

  document.querySelectorAll(".rpt-availability-preset").forEach(function (link) {
    link.addEventListener("click", function () {
      var url = new URL(link.href);
      var current = new URL(window.location.href);
      if (url.searchParams.has("mode")) return;
      var mode = current.searchParams.get("mode") || (modeSelect ? modeSelect.value : "host");
      if (mode !== "host") url.searchParams.set("mode", mode);
      link.href = url.pathname + url.search;
    });
  });

  if (modeSelect) {
    modeSelect.addEventListener("change", function () {
      var url = new URL(window.location.href);
      url.searchParams.set("mode", modeSelect.value);
      if (!url.searchParams.has("from") && !url.searchParams.has("to") && !url.searchParams.has("time") && !url.searchParams.has("range")) {
        url.searchParams.set("time", "1h");
      }
      window.location.href = url.pathname + url.search;
    });
  }

  function setFilterPanel(open) {
    if (!filterPanel || !filterButton) return;
    filterPanel.hidden = !open;
    filterButton.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) closeFilterDialog();
  }

  if (filterButton && filterPanel) {
    filterButton.addEventListener("click", function () {
      setFilterPanel(filterPanel.hidden);
    });
  }

  function pad(value) { return String(value).padStart(2, "0"); }

  function parseDateValue(value) {
    var match = value.trim().match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?$/);
    if (match) return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4] || 0), Number(match[5] || 0));
    var relative = value.trim().match(/^now-(\d+)([mhd])$/i);
    if (relative) {
      var date = new Date();
      var amount = Number(relative[1]);
      if (relative[2].toLowerCase() === "m") date.setMinutes(date.getMinutes() - amount);
      if (relative[2].toLowerCase() === "h") date.setHours(date.getHours() - amount);
      if (relative[2].toLowerCase() === "d") date.setDate(date.getDate() - amount);
      return date;
    }
    return new Date();
  }

  function formatDate(date) {
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  function ensureCalendar() {
    var calendar = document.getElementById("availabilityCalendar");
    if (calendar) return calendar;
    calendar = document.createElement("div");
    calendar.id = "availabilityCalendar";
    calendar.className = "rpt-availability-calendar-popover";
    calendar.hidden = true;
    calendar.setAttribute("role", "dialog");
    calendar.setAttribute("aria-label", "Choose date and time");
    calendar.innerHTML = '<div class="rpt-av-cal-head"><button type="button" data-cal-action="prev-year" aria-label="Previous year">‹</button><strong data-cal-year></strong><button type="button" data-cal-action="next-year" aria-label="Next year">›</button><button type="button" data-cal-action="prev-month" aria-label="Previous month">‹</button><strong data-cal-month></strong><button type="button" data-cal-action="next-month" aria-label="Next month">›</button></div><div class="rpt-av-cal-weekdays"><span>M</span><span>T</span><span>W</span><span>T</span><span>F</span><span>S</span><span>S</span></div><div class="rpt-av-cal-days"></div><div class="rpt-av-cal-time"><label>Time <input type="time" data-cal-time value="00:00"></label><button type="button" data-cal-action="now">Now</button><button type="button" data-cal-action="apply">Apply</button></div>';
    document.body.appendChild(calendar);
    return calendar;
  }

  var activeCalendarInput = null;
  var calendarDate = new Date();

  function drawCalendar() {
    var calendar = ensureCalendar();
    var year = calendarDate.getFullYear();
    var month = calendarDate.getMonth();
    calendar.querySelector("[data-cal-year]").textContent = year;
    calendar.querySelector("[data-cal-month]").textContent = calendarDate.toLocaleString(undefined, { month: "long" });
    var days = calendar.querySelector(".rpt-av-cal-days");
    days.textContent = "";
    var firstDay = new Date(year, month, 1);
    var offset = (firstDay.getDay() + 6) % 7;
    var start = new Date(year, month, 1 - offset);
    var selected = parseDateValue(activeCalendarInput.value);
    for (var i = 0; i < 42; i++) {
      var day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      var button = document.createElement("button");
      button.type = "button";
      button.className = "rpt-av-cal-day" + (day.getMonth() === month ? "" : " outside") + (day.toDateString() === selected.toDateString() ? " selected" : "");
      button.textContent = day.getDate();
      button.addEventListener("click", function (picked) {
        return function () {
          var time = calendar.querySelector("[data-cal-time]").value || "00:00";
          activeCalendarInput.value = formatDate(new Date(picked.getFullYear(), picked.getMonth(), picked.getDate(), Number(time.slice(0, 2)), Number(time.slice(3, 5))));
          calendarDate = new Date(picked.getFullYear(), picked.getMonth(), 1);
          drawCalendar();
        };
      }(day));
      days.appendChild(button);
    }
    calendar.querySelector("[data-cal-time]").value = pad(selected.getHours()) + ":" + pad(selected.getMinutes());
  }

  document.querySelectorAll(".rpt-availability-calendar").forEach(function (button) {
    button.addEventListener("click", function () {
      activeCalendarInput = button.parentElement.querySelector("input");
      if (!activeCalendarInput) return;
      calendarDate = parseDateValue(activeCalendarInput.value);
      var calendar = ensureCalendar();
      drawCalendar();
      var rect = button.getBoundingClientRect();
      calendar.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - calendar.offsetWidth - 8)) + "px";
      calendar.style.top = Math.min(rect.bottom + 4, window.innerHeight - calendar.offsetHeight - 8) + window.scrollY + "px";
      calendar.hidden = false;
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !event.target.matches("#availabilityFrom, #availabilityTo")) return;
    event.preventDefault();
    event.target.form.requestSubmit();
  });

  document.addEventListener("click", function (event) {
    var calendar = document.getElementById("availabilityCalendar");
    if (calendar && !calendar.hidden && !calendar.contains(event.target) && !event.target.closest(".rpt-availability-calendar")) calendar.hidden = true;
    if (!calendar || !calendar.contains(event.target)) return;
    var action = event.target.closest("[data-cal-action]");
    if (!action) return;
    var name = action.getAttribute("data-cal-action");
    if (name === "prev-year") calendarDate.setFullYear(calendarDate.getFullYear() - 1);
    if (name === "next-year") calendarDate.setFullYear(calendarDate.getFullYear() + 1);
    if (name === "prev-month") calendarDate.setMonth(calendarDate.getMonth() - 1);
    if (name === "next-month") calendarDate.setMonth(calendarDate.getMonth() + 1);
    if (name === "now") {
      activeCalendarInput.value = formatDate(new Date());
      calendarDate = new Date();
    }
    if (name === "apply") calendar.hidden = true;
    drawCalendar();
  });

  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-cal-time]") && activeCalendarInput) {
      var selected = parseDateValue(activeCalendarInput.value);
      var time = event.target.value.split(":");
      activeCalendarInput.value = formatDate(new Date(selected.getFullYear(), selected.getMonth(), selected.getDate(), Number(time[0]), Number(time[1])));
      drawCalendar();
    }
  });

  document.querySelectorAll("#availabilityFrom, #availabilityTo").forEach(function (input) {
    input.addEventListener("blur", function () {
      if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(input.value)) input.value = input.value.replace("T", " ");
    });
  });

  var filterIds = [
    "availabilityHostFilter",
    "availabilityNameFilter",
    "availabilityCategoryFilter",
    "availabilitySeverityFilter",
  ];

  function filterValue(id) {
    var field = document.getElementById(id);
    return field ? (field.value || "").trim().toLowerCase() : "";
  }

  function updateFilterEmptyRow(visible, total) {
    if (!table) return;
    var body = table.querySelector("tbody");
    if (!body) return;
    var emptyRow = body.querySelector("tr.rpt-availability-filter-empty");
    if (total > 0 && visible === 0) {
      if (!emptyRow) {
        emptyRow = document.createElement("tr");
        emptyRow.className = "rpt-availability-filter-empty";
        var cell = document.createElement("td");
        cell.colSpan = table.querySelectorAll("thead th").length;
        cell.textContent = "No data found — no rows match the current filter.";
        emptyRow.appendChild(cell);
        body.appendChild(emptyRow);
      }
    } else if (emptyRow) {
      emptyRow.remove();
    }
  }

  function pickerValues(attribute) {
    var seen = {};
    var values = [];
    if (!table) return values;
    Array.prototype.forEach.call(table.querySelectorAll("tbody tr[data-host]"), function (row) {
      var value = (row.getAttribute(attribute) || "").trim();
      if (!value || seen[value.toLowerCase()]) return;
      seen[value.toLowerCase()] = true;
      values.push(value);
    });
    values.sort(function (a, b) { return a.localeCompare(b, undefined, { numeric: true }); });
    return values;
  }

  var filterDialog = null;
  var filterDialogInput = null;
  var filterDialogTrigger = null;

  function ensureFilterDialog() {
    if (filterDialog) return filterDialog;
    filterDialog = document.createElement("div");
    filterDialog.id = "availabilityFilterDialog";
    filterDialog.className = "rpt-av-modal";
    filterDialog.hidden = true;
    filterDialog.innerHTML = '<div class="rpt-av-modal-window" role="dialog" aria-modal="true" aria-labelledby="availabilityFilterDialogTitle">' +
      '<div class="rpt-av-modal-head"><h2 class="rpt-av-modal-title" id="availabilityFilterDialogTitle"></h2><button type="button" class="rpt-av-modal-close" aria-label="Close">×</button></div>' +
      '<div class="rpt-av-modal-body"><div class="rpt-av-modal-list" role="group"></div><div class="rpt-av-modal-empty" hidden>No data found</div></div>' +
      '<div class="rpt-av-modal-foot"><button type="button" class="rpt-av-modal-select" data-dialog-action="select">Select</button><button type="button" class="rpt-av-modal-cancel" data-dialog-action="cancel">Cancel</button></div>' +
      '</div>';
    document.body.appendChild(filterDialog);

    filterDialog.querySelector("[data-dialog-action=cancel]").addEventListener("click", closeFilterDialog);
    filterDialog.querySelector(".rpt-av-modal-close").addEventListener("click", closeFilterDialog);
    filterDialog.querySelector("[data-dialog-action=select]").addEventListener("click", function () {
      if (!filterDialogInput) return;
      var input = filterDialogInput;
      var values = [];
      filterDialog.querySelectorAll(".rpt-av-modal-list input:checked").forEach(function (box) {
        values.push(box.value);
      });
      input.value = values.join(", ");
      applyTableFilters();
      closeFilterDialog();
      input.focus();
    });
    filterDialog.addEventListener("click", function (event) {
      if (event.target === filterDialog) closeFilterDialog();
    });
    return filterDialog;
  }

  function closeFilterDialog() {
    if (!filterDialog || filterDialog.hidden) return;
    filterDialog.hidden = true;
    if (filterDialogTrigger) {
      filterDialogTrigger.setAttribute("aria-expanded", "false");
      if (filterDialogTrigger.offsetParent !== null) filterDialogTrigger.focus();
    }
    filterDialogInput = null;
    filterDialogTrigger = null;
  }

  function openFilterDialog(input, attribute, trigger) {
    var dialog = ensureFilterDialog();
    var list = dialog.querySelector(".rpt-av-modal-list");
    var empty = dialog.querySelector(".rpt-av-modal-empty");
    var selectButton = dialog.querySelector("[data-dialog-action=select]");
    list.textContent = "";
    dialog.querySelector(".rpt-av-modal-title").textContent = (trigger && trigger.getAttribute("data-picker-title")) || "Select";
    var selected = input.value.split(",").map(function (term) { return term.trim().toLowerCase(); }).filter(Boolean);
    pickerValues(attribute).forEach(function (value) {
      var item = document.createElement("label");
      item.className = "rpt-av-modal-item";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.value = value;
      box.checked = selected.indexOf(value.toLowerCase()) !== -1;
      var text = document.createElement("span");
      text.textContent = value;
      item.appendChild(box);
      item.appendChild(text);
      list.appendChild(item);
    });
    var hasValues = list.childElementCount > 0;
    empty.hidden = hasValues;
    selectButton.disabled = !hasValues;
    filterDialogInput = input;
    filterDialogTrigger = trigger;
    if (trigger) trigger.setAttribute("aria-expanded", "true");
    dialog.hidden = false;
    var first = list.querySelector("input") || dialog.querySelector("[data-dialog-action=cancel]");
    if (first) first.focus();
  }

  function filterTerms(id) {
    var raw = filterValue(id);
    if (!raw) return [];
    return raw.split(",").map(function (term) { return term.trim(); }).filter(Boolean);
  }

  function matchesTerms(terms, values) {
    if (!terms.length) return true;
    return terms.some(function (term) {
      return values.some(function (value) { return value.indexOf(term) !== -1; });
    });
  }

  function applyTableFilters() {
    if (!table) return;
    var hostTerms = filterTerms("availabilityHostFilter");
    var nameTerms = filterTerms("availabilityNameFilter");
    var categoryTerms = filterTerms("availabilityCategoryFilter");
    var severityQuery = filterValue("availabilitySeverityFilter");
    var visible = 0;
    var total = 0;
    Array.prototype.forEach.call(table.querySelectorAll("tbody tr[data-host]"), function (row) {
      total += 1;
      var host = (row.getAttribute("data-host") || "").toLowerCase();
      var hostIp = (row.getAttribute("data-host-ip") || "").toLowerCase();
      var hostAlias = (row.getAttribute("data-host-alias") || "").toLowerCase();
      var name = (row.getAttribute("data-name") || "").toLowerCase();
      var category = (row.getAttribute("data-category") || "").toLowerCase();
      var severity = (row.getAttribute("data-severity") || "").toLowerCase();
      var show = matchesTerms(hostTerms, [host, hostIp, hostAlias]);
      if (show && !matchesTerms(nameTerms, [name])) show = false;
      if (show && !matchesTerms(categoryTerms, [category])) show = false;
      if (show && severityQuery && severity !== severityQuery) show = false;
      row.hidden = !show;
      if (show) visible += 1;
    });
    var counter = document.getElementById("availabilityDisplayCount");
    if (counter) counter.textContent = "Displaying " + visible + " of " + total + " found";
    updateFilterEmptyRow(visible, total);
  }

  filterIds.forEach(function (id) {
    var field = document.getElementById(id);
    if (!field) return;
    field.addEventListener(field.tagName === "SELECT" ? "change" : "input", applyTableFilters);
  });

  document.querySelectorAll(".rpt-availability-select").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = document.getElementById(button.getAttribute("data-picker"));
      if (!input) return;
      if (filterDialog && !filterDialog.hidden && filterDialogInput === input) {
        closeFilterDialog();
        return;
      }
      openFilterDialog(input, button.getAttribute("data-picker-attr"), button);
    });
  });

  var filterReset = document.getElementById("availabilityFilterReset");
  if (filterReset) {
    filterReset.addEventListener("click", function () {
      ["availabilityHostFilter", "availabilityCategoryFilter", "availabilityNameFilter", "availabilitySeverityFilter"].forEach(function (id) {
        var field = document.getElementById(id);
        if (field) field.value = "";
      });
      closeFilterDialog();
      applyTableFilters();
    });
  }

  var filterApply = document.getElementById("availabilityFilterApply");
  if (filterApply) {
    filterApply.addEventListener("click", function () {
      closeFilterDialog();
      applyTableFilters();
      setFilterPanel(false);
    });
  }

  applyTableFilters();

  var sortState = { key: null, direction: 1 };

  function rowSortValue(row, key) {
    if (key === "problems") return Number(row.getAttribute("data-problems")) || 0;
    if (key === "availability") {
      var value = Number(row.getAttribute("data-availability"));
      return isNaN(value) ? -1 : value;
    }
    if (key === "name") return (row.getAttribute("data-name") || "").toLowerCase();
    if (key === "trigger") return (row.getAttribute("data-trigger") || "").toLowerCase();
    if (key === "host") {
      return (row.getAttribute("data-host-alias") || row.getAttribute("data-host-ip") || row.getAttribute("data-host") || "").toLowerCase();
    }
    return (row.getAttribute("data-name") || row.getAttribute("data-host-ip") || "").toLowerCase();
  }

  function markSort(key, direction) {
    if (!table) return;
    Array.prototype.forEach.call(table.querySelectorAll("th"), function (th) {
      var button = th.querySelector(".rpt-availability-sort");
      if (!button) return;
      var active = button.getAttribute("data-sort-key") === key;
      th.setAttribute("aria-sort", active ? (direction === 1 ? "ascending" : "descending") : "none");
      var arrow = button.querySelector(".rpt-availability-sort-arrow");
      if (arrow) arrow.textContent = active ? (direction === 1 ? "↑" : "↓") : "↕";
    });
  }

  if (table) {
    Array.prototype.forEach.call(table.querySelectorAll(".rpt-availability-sort"), function (button) {
      button.addEventListener("click", function () {
        var key = button.getAttribute("data-sort-key");
        var type = button.getAttribute("data-sort-type") || "text";
        var direction = sortState.key === key && sortState.direction === 1 ? -1 : 1;
        sortState = { key: key, direction: direction };
        var body = table.querySelector("tbody");
        if (!body) return;
        var rows = Array.prototype.slice.call(body.querySelectorAll("tr[data-host]"));
        rows.sort(function (a, b) {
          var valueA = rowSortValue(a, key);
          var valueB = rowSortValue(b, key);
          if (type === "number") {
            if (valueA === valueB) return 0;
            return valueA < valueB ? -direction : direction;
          }
          return direction * String(valueA).localeCompare(String(valueB), undefined, { numeric: true });
        });
        rows.forEach(function (row) { body.appendChild(row); });
        markSort(key, direction);
      });
    });
    if (table.querySelector('.rpt-availability-sort[data-sort-key="availability"]') && table.querySelector("tbody tr[data-host]")) {
      sortState = { key: "availability", direction: 1 };
      markSort("availability", 1);
    }
  }

  document.querySelectorAll(".rpt-availability-zoom, .rpt-availability-zoom-label").forEach(function (button) {
    button.addEventListener("click", function () {
      var current = new URL(window.location.href);
      var key = current.searchParams.get("time") || "1h";
      var sequence = ["5m", "15m", "30m", "1h", "3h", "6h", "12h", "1d", "2d", "7d", "30d", "3m", "6m", "1y", "2y"];
      var index = sequence.indexOf(key);
      if (index < 0) index = 3;
      var direction = button.getAttribute("data-zoom") === "in" ? -1 : 1;
      index = Math.max(0, Math.min(sequence.length - 1, index + direction));
      current.searchParams.set("time", sequence[index]);
      current.searchParams.delete("from");
      current.searchParams.delete("to");
      window.location.href = current.pathname + current.search;
    });
  });

  document.addEventListener("click", function (event) {
    if (!panel.hidden && !panel.contains(event.target) && !toggle.contains(event.target)) setPanel(false);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    closeFilterDialog();
    if (!panel.hidden) {
      setPanel(false);
      toggle.focus();
    }
    if (filterPanel && !filterPanel.hidden) {
      setFilterPanel(false);
      filterButton.focus();
    }
  });
})();
