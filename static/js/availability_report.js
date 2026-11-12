/* Zabbix-style controls for the availability report. */
(function () {
  "use strict";

  var panel = document.getElementById("availabilityTimePanel");
  var toggle = document.getElementById("availabilityTimeToggle");
  var filterPanel = document.getElementById("availabilityFilterPanel");
  var auditFilterPanel = document.getElementById("auditFilterPanel");
  var actionPanel = document.querySelector("[data-action-panel]");
  var actionFilterPanel = document.getElementById("actionFilterPanel");
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
    if (actionFilterPanel) {
      var actionOpen = panel.hidden;
      panel.hidden = !actionOpen;
      actionFilterPanel.hidden = true;
      toggle.setAttribute("aria-expanded", actionOpen ? "true" : "false");
      toggle.classList.toggle("active", actionOpen);
      if (filterButton) filterButton.setAttribute("aria-expanded", "false");
      return;
    }
    var open = panel.hidden;
    if (open) setAuditFilterPanel(false);
    setPanel(open);
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
    if (!open) closePicker();
  }

  if (filterButton && filterPanel) {
    filterButton.addEventListener("click", function () {
      setFilterPanel(filterPanel.hidden);
    });
  }

  // Audit log: panel filter terpisah dari panel rentang waktu.
  var auditUsersSelect = document.getElementById("auditUsersSelect");
  var auditUsersInput = document.getElementById("auditFilterUsers");
  var auditUsersDialog = document.getElementById("auditUsersDialog");
  var auditUsersList = document.getElementById("auditUsersList");
  var auditUsersAll = document.getElementById("auditUsersAll");

  function auditUserBoxes() {
    return auditUsersList ? auditUsersList.querySelectorAll("input") : [];
  }

  function syncAuditUsersAll() {
    if (!auditUsersAll) return;
    var boxes = auditUserBoxes();
    var checked = 0;
    Array.prototype.forEach.call(boxes, function (box) {
      if (box.checked) checked += 1;
    });
    auditUsersAll.checked = boxes.length > 0 && checked === boxes.length;
    auditUsersAll.indeterminate = checked > 0 && checked < boxes.length;
    auditUsersAll.disabled = boxes.length === 0;
  }

  function closeUsersPicker() {
    if (auditUsersDialog && auditUsersDialog.open) auditUsersDialog.close();
    if (auditUsersSelect) auditUsersSelect.setAttribute("aria-expanded", "false");
  }

  function setAuditFilterPanel(open) {
    if (!auditFilterPanel || !filterButton) return;
    auditFilterPanel.hidden = !open;
    filterButton.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) closeUsersPicker();
  }

  if (filterButton && auditFilterPanel) {
    filterButton.addEventListener("click", function () {
      var open = auditFilterPanel.hidden;
      if (open) setPanel(false);
      setAuditFilterPanel(open);
    });
  }

  // Action log: panel Filter terpisah dari panel rentang waktu.
  if (filterButton && actionFilterPanel) {
    filterButton.addEventListener("click", function () {
      var open = actionFilterPanel.hidden;
      actionFilterPanel.hidden = !open;
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      toggle.classList.remove("active");
      filterButton.setAttribute("aria-expanded", open ? "true" : "false");
      if (!open) closeActionPicker();
    });
  }

  // Jangan kirim field teks kosong agar URL filter tetap ringkas seperti Zabbix.
  var actionFilterForm = document.getElementById("actionFilterForm");
  if (actionFilterForm) {
    actionFilterForm.addEventListener("submit", function () {
      Array.prototype.forEach.call(
        actionFilterForm.querySelectorAll('input[type="text"][name]'),
        function (input) {
          if (!input.value.trim()) input.disabled = true;
        }
      );
    });
  }

  // Dialog Select untuk Recipients / Actions / Media types pada Action log.
  var actionPickerDialog = document.getElementById("actionFilterPicker");
  var actionPickerList = document.getElementById("actionFilterPickerList");
  var actionPickerTitle = document.getElementById("actionFilterPickerTitle");
  var actionPickerOptions = document.getElementById("actionFilterOptions");
  var actionPickerTarget = null;
  var actionPickerKind = "";
  var actionPickerValues = {};
  var actionPickerInputs = {
    recipients: "actionFilterRecipients",
    actions: "actionFilterActions",
    media: "actionFilterMedia"
  };
  var actionPickerLabels = {
    recipients: "Recipients",
    actions: "Actions",
    media: "Media types"
  };

  if (actionPickerOptions) {
    try {
      actionPickerValues = JSON.parse(actionPickerOptions.textContent) || {};
    } catch (error) {
      actionPickerValues = {};
    }
  }

  function closeActionPicker() {
    if (actionPickerDialog && actionPickerDialog.open) actionPickerDialog.close();
    actionPickerTarget = null;
  }

  function renderActionPickerList() {
    if (!actionPickerList) return;
    actionPickerList.innerHTML = "";
    var values = actionPickerValues[actionPickerKind] || [];
    var selected = actionPickerTarget
      ? actionPickerTarget.value.split(",").map(function (value) { return value.trim(); })
      : [];
    if (!values.length) {
      var empty = document.createElement("p");
      empty.className = "rpt-picker-empty";
      empty.textContent = "No data found";
      actionPickerList.appendChild(empty);
      return;
    }
    values.forEach(function (value) {
      var row = document.createElement("label");
      row.className = "rpt-picker-users-row";
      var cell = document.createElement("span");
      cell.className = "rpt-picker-users-cell";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.className = "rpt-picker-check";
      box.value = value;
      box.checked = selected.indexOf(value) !== -1;
      cell.appendChild(box);
      var name = document.createElement("span");
      name.className = "rpt-picker-users-name";
      name.textContent = value;
      row.appendChild(cell);
      row.appendChild(name);
      actionPickerList.appendChild(row);
    });
  }

  if (actionPickerDialog) {
    document.querySelectorAll("[data-action-picker]").forEach(function (button) {
      button.addEventListener("click", function () {
        actionPickerKind = button.getAttribute("data-action-picker");
        actionPickerTarget = document.getElementById(actionPickerInputs[actionPickerKind]);
        if (!actionPickerTarget) return;
        actionPickerTitle.textContent = actionPickerLabels[actionPickerKind] || "Select";
        renderActionPickerList();
        button.setAttribute("aria-expanded", "true");
        if (!actionPickerDialog.open) {
          actionPickerDialog.showModal();
          actionPickerDialog.focus();
        }
      });
    });

    document.getElementById("actionFilterPickerClose").addEventListener("click", closeActionPicker);
    document.getElementById("actionFilterPickerCancel").addEventListener("click", closeActionPicker);
    document.getElementById("actionFilterPickerSelect").addEventListener("click", function () {
      if (!actionPickerTarget || !actionPickerList) {
        closeActionPicker();
        return;
      }
      var picked = [];
      Array.prototype.forEach.call(
        actionPickerList.querySelectorAll('input[type="checkbox"]'),
        function (box) {
          if (box.checked) picked.push(box.value);
        }
      );
      actionPickerTarget.value = picked.join(", ");
      actionPickerTarget.focus();
      closeActionPicker();
    });
    actionPickerDialog.addEventListener("click", function (event) {
      if (event.target === actionPickerDialog) closeActionPicker();
    });
    actionPickerDialog.addEventListener("close", function () {
      document.querySelectorAll("[data-action-picker]").forEach(function (button) {
        button.setAttribute("aria-expanded", "false");
      });
    });
  }

  if (auditUsersSelect && auditUsersDialog) {
    auditUsersSelect.addEventListener("click", function () {
      syncAuditUsersAll();
      if (!auditUsersDialog.open) {
        auditUsersDialog.showModal();
        // Fokus ke dialog, bukan tombol Close, agar tidak ada focus ring.
        auditUsersDialog.focus();
      }
      auditUsersSelect.setAttribute("aria-expanded", "true");
    });
    auditUsersDialog.addEventListener("close", function () {
      auditUsersSelect.setAttribute("aria-expanded", "false");
    });
    document.getElementById("auditUsersDialogClose").addEventListener("click", closeUsersPicker);
    document.getElementById("auditUsersDialogCancel").addEventListener("click", closeUsersPicker);
    document.getElementById("auditUsersDialogSelect").addEventListener("click", function () {
      var values = [];
      Array.prototype.forEach.call(auditUserBoxes(), function (box) {
        if (box.checked) values.push(box.value);
      });
      if (auditUsersInput) {
        auditUsersInput.value = values.join(", ");
        auditUsersInput.focus();
      }
      closeUsersPicker();
    });
    if (auditUsersAll) {
      auditUsersAll.addEventListener("change", function () {
        Array.prototype.forEach.call(auditUserBoxes(), function (box) {
          box.checked = auditUsersAll.checked;
        });
        syncAuditUsersAll();
      });
    }
    if (auditUsersList) {
      auditUsersList.addEventListener("change", syncAuditUsersAll);
    }
    auditUsersDialog.addEventListener("click", function (event) {
      if (event.target !== auditUsersDialog) return;
      var rect = auditUsersDialog.getBoundingClientRect();
      var inside = event.clientX >= rect.left && event.clientX <= rect.right &&
        event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) closeUsersPicker();
    });
  }

  var auditFilterForm = document.getElementById("auditFilterForm");
  if (auditFilterForm) {
    auditFilterForm.addEventListener("submit", function () {
      Array.prototype.forEach.call(auditFilterForm.elements, function (element) {
        if (element.type === "text" || element.tagName === "SELECT") {
          if (!String(element.value || "").trim()) element.disabled = true;
        }
      });
    });
  }

  if (auditFilterPanel && filterButton) {
    document.addEventListener("click", function (event) {
      if (auditFilterPanel.hidden) return;
      // Klik dari dialog Users (termasuk Select/Cancel yang sudah menutup
      // dialog) bukan klik di luar panel; jangan ikut menutup panel.
      if (auditUsersDialog && (auditUsersDialog.open || auditUsersDialog.contains(event.target))) return;
      if (auditFilterPanel.contains(event.target) || filterButton.contains(event.target)) return;
      setAuditFilterPanel(false);
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
        return function (event) {
          // Jangan biarkan handler document menutup popover/panel setelah
          // tombol hari ini digambar ulang (target sudah lepas dari DOM).
          if (event) event.stopPropagation();
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

  function triggerPickerValues(trigger) {
    var raw = trigger.getAttribute("data-picker-values");
    if (!raw) return [];
    try {
      var parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.map(function (value) { return String(value || "").trim(); }).filter(Boolean);
    } catch (err) {
      return [];
    }
  }

  function parseJson(value, fallback) {
    if (!value) return fallback;
    try {
      var parsed = JSON.parse(value);
      return parsed == null ? fallback : parsed;
    } catch (err) {
      return fallback;
    }
  }

  function stringList(raw) {
    if (!Array.isArray(raw)) return [];
    var seen = {};
    var values = [];
    raw.forEach(function (entry) {
      var value = String(entry == null ? "" : entry).trim();
      var key = value.toLowerCase();
      if (!value || seen[key]) return;
      seen[key] = true;
      values.push(value);
    });
    return values;
  }

  function pickerTerms(value) {
    return (value || "").split(",").map(function (term) { return term.trim().toLowerCase(); }).filter(Boolean);
  }

  function compareValues(a, b) {
    return a.localeCompare(b, undefined, { numeric: true });
  }

  /* Peta "host group" -> daftar host untuk dialog Hosts. Data utama berasal
     dari atribut tombol (tabel hosts), dilengkapi baris tabel yang tampil. */
  function triggerHostGroupData(trigger) {
    var map = {};
    function add(group, host) {
      if (!group || !host || group === "—") return;
      map[group] = map[group] || [];
      if (map[group].indexOf(host) === -1) map[group].push(host);
    }
    if (trigger) {
      var rawMap = parseJson(trigger.getAttribute("data-picker-map"), {});
      if (rawMap && typeof rawMap === "object" && !Array.isArray(rawMap)) {
        Object.keys(rawMap).forEach(function (group) {
          stringList(rawMap[group]).forEach(function (host) {
            add(String(group).trim(), host);
          });
        });
      }
    }
    if (table) {
      Array.prototype.forEach.call(table.querySelectorAll("tbody tr[data-category]"), function (row) {
        add(
          (row.getAttribute("data-category") || "").trim(),
          (row.getAttribute("data-host-alias") || row.getAttribute("data-host-ip") || "").trim()
        );
      });
    }
    var groups = Object.keys(map);
    if (trigger) groups = groups.concat(stringList(parseJson(trigger.getAttribute("data-picker-groups"), [])));
    var seen = {};
    groups = groups.filter(function (group) {
      var key = group.toLowerCase();
      if (seen[key]) return false;
      seen[key] = true;
      return true;
    }).sort(compareValues);
    groups.forEach(function (group) {
      map[group] = (map[group] || []).slice().sort(compareValues);
    });
    return { groups: groups, map: map };
  }

  function hostValuesForGroups(groups, data) {
    var seen = {};
    var values = [];
    groups.forEach(function (group) {
      (data.map[group] || []).forEach(function (host) {
        var key = host.toLowerCase();
        if (seen[key]) return;
        seen[key] = true;
        values.push(host);
      });
    });
    return values.sort(compareValues);
  }

  function fillPickerList(list, values, selected) {
    if (!list) return 0;
    list.textContent = "";
    values.forEach(function (value) {
      var item = document.createElement("label");
      item.className = "rpt-picker-item";
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
    return list.childElementCount;
  }

  function syncPickerAll(allBox, list) {
    if (!allBox || !list) return;
    var boxes = list.querySelectorAll("input");
    var checked = 0;
    Array.prototype.forEach.call(boxes, function (box) {
      if (box.checked) checked += 1;
    });
    allBox.checked = boxes.length > 0 && checked === boxes.length;
    allBox.indeterminate = checked > 0 && checked < boxes.length;
    allBox.disabled = boxes.length === 0;
  }

  var pickerDialog = document.getElementById("rptPickerDialog");
  var pickerTitle = document.getElementById("rptPickerTitle");
  var pickerList = document.getElementById("rptPickerList");
  var pickerAll = document.getElementById("rptPickerAll");
  var pickerEmpty = document.getElementById("rptPickerEmpty");
  var pickerFilterRow = document.getElementById("rptPickerFilter");
  var pickerFilterInput = document.getElementById("rptPickerFilterInput");
  var pickerFilterButton = document.getElementById("rptPickerFilterSelect");
  var pickerPlaceholder = document.getElementById("rptPickerPlaceholder");
  var groupDialog = document.getElementById("rptGroupDialog");
  var groupList = document.getElementById("rptGroupList");
  var groupAll = document.getElementById("rptGroupAll");
  var groupEmpty = document.getElementById("rptGroupEmpty");
  var pickerInput = null;
  var pickerTrigger = null;
  var pickerMode = "list";
  var pickerGroups = [];
  var pickerGroupData = { groups: [], map: {} };

  function closeGroupPicker() {
    if (groupDialog && groupDialog.open) groupDialog.close();
  }

  function closePicker() {
    if (groupDialog && groupDialog.open) {
      groupDialog.close();
      return;
    }
    if (pickerDialog && pickerDialog.open) pickerDialog.close();
  }

  function setPickerEmpty(state) {
    if (pickerPlaceholder) pickerPlaceholder.hidden = state !== "filter";
    if (pickerEmpty) pickerEmpty.hidden = state !== "data";
    if (pickerList) pickerList.hidden = Boolean(state);
  }

  function renderHosts(selected) {
    if (!pickerList) return;
    if (!pickerGroups.length) {
      fillPickerList(pickerList, [], selected);
      setPickerEmpty("filter");
    } else {
      var values = hostValuesForGroups(pickerGroups, pickerGroupData);
      var count = fillPickerList(pickerList, values, selected);
      setPickerEmpty(count ? null : "data");
    }
    syncPickerAll(pickerAll, pickerList);
  }

  function openPicker(input, attribute, trigger) {
    if (!pickerDialog || !pickerList) return;
    pickerInput = input;
    pickerTrigger = trigger;
    pickerMode = (trigger && trigger.getAttribute("data-picker-mode")) || "list";
    pickerGroupData = triggerHostGroupData(trigger);
    if (pickerMode === "hosts" && !pickerGroupData.groups.length) pickerMode = "list";
    pickerGroups = [];
    if (pickerMode === "hosts") {
      pickerGroups = stringList(parseJson(input.getAttribute("data-picker-groups"), [])).map(function (group) {
        var key = group.toLowerCase();
        for (var i = 0; i < pickerGroupData.groups.length; i += 1) {
          if (pickerGroupData.groups[i].toLowerCase() === key) return pickerGroupData.groups[i];
        }
        return "";
      }).filter(Boolean);
    }
    var showFilter = pickerMode === "hosts";
    pickerDialog.classList.toggle("rpt-picker--hosts", showFilter);
    if (pickerFilterRow) pickerFilterRow.hidden = !showFilter;
    if (pickerFilterInput) pickerFilterInput.value = pickerGroups.join(", ");
    if (pickerTitle) {
      pickerTitle.textContent = (trigger && trigger.getAttribute("data-picker-title")) || "Select";
    }
    var selected = pickerTerms(input.value);
    if (showFilter) {
      renderHosts(selected);
    } else {
      var values = pickerValues(attribute);
      if (!values.length && trigger) values = triggerPickerValues(trigger);
      var count = fillPickerList(pickerList, values, selected);
      setPickerEmpty(count ? null : "data");
      syncPickerAll(pickerAll, pickerList);
    }
    if (trigger) trigger.setAttribute("aria-expanded", "true");
    if (!pickerDialog.open) pickerDialog.showModal();
    pickerDialog.focus();
  }

  function openGroupPicker() {
    if (!groupDialog || !groupList || pickerMode !== "hosts" || !pickerGroupData.groups.length) return;
    var selected = pickerGroups.map(function (group) { return group.toLowerCase(); });
    var count = fillPickerList(groupList, pickerGroupData.groups, selected);
    if (groupEmpty) groupEmpty.hidden = count > 0;
    if (groupList) groupList.hidden = count === 0;
    syncPickerAll(groupAll, groupList);
    if (!groupDialog.open) groupDialog.showModal();
    groupDialog.focus();
  }

  function applyGroupPicker() {
    if (!groupList || !pickerInput) return;
    pickerGroups = Array.prototype.map.call(groupList.querySelectorAll("input:checked"), function (box) {
      return box.value;
    });
    pickerInput.setAttribute("data-picker-groups", JSON.stringify(pickerGroups));
    if (pickerFilterInput) pickerFilterInput.value = pickerGroups.join(", ");
    closeGroupPicker();
    renderHosts(pickerTerms(pickerInput.value));
    pickerDialog.focus();
  }

  if (pickerDialog) {
    pickerDialog.addEventListener("close", function () {
      closeGroupPicker();
      pickerInput = null;
      pickerMode = "list";
      pickerGroups = [];
      if (pickerTrigger) pickerTrigger.setAttribute("aria-expanded", "false");
      pickerTrigger = null;
    });
    pickerDialog.addEventListener("click", function (event) {
      if (event.target !== pickerDialog) return;
      var rect = pickerDialog.getBoundingClientRect();
      var inside = event.clientX >= rect.left && event.clientX <= rect.right &&
        event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) closePicker();
    });
    document.getElementById("rptPickerClose").addEventListener("click", closePicker);
    document.getElementById("rptPickerCancel").addEventListener("click", closePicker);
    document.getElementById("rptPickerSelect").addEventListener("click", function () {
      if (!pickerInput) return;
      var input = pickerInput;
      var values = [];
      Array.prototype.forEach.call(pickerList.querySelectorAll("input:checked"), function (box) {
        values.push(box.value);
      });
      input.value = values.join(", ");
      closePicker();
      applyTableFilters();
    });
    if (pickerAll) {
      pickerAll.addEventListener("change", function () {
        Array.prototype.forEach.call(pickerBoxes(), function (box) {
          box.checked = pickerAll.checked;
        });
        syncPickerAll(pickerAll, pickerList);
      });
    }
    pickerList.addEventListener("change", function () {
      syncPickerAll(pickerAll, pickerList);
    });
    if (pickerFilterInput) pickerFilterInput.addEventListener("click", openGroupPicker);
    if (pickerFilterButton) pickerFilterButton.addEventListener("click", openGroupPicker);
  }

  function pickerBoxes() {
    return pickerList ? pickerList.querySelectorAll("input") : [];
  }

  if (groupDialog) {
    groupDialog.addEventListener("click", function (event) {
      if (event.target !== groupDialog) return;
      var rect = groupDialog.getBoundingClientRect();
      var inside = event.clientX >= rect.left && event.clientX <= rect.right &&
        event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) groupDialog.close();
    });
    document.getElementById("rptGroupClose").addEventListener("click", closeGroupPicker);
    document.getElementById("rptGroupCancel").addEventListener("click", closeGroupPicker);
    document.getElementById("rptGroupSelect").addEventListener("click", applyGroupPicker);
    if (groupAll) {
      groupAll.addEventListener("change", function () {
        Array.prototype.forEach.call(groupList.querySelectorAll("input"), function (box) {
          box.checked = groupAll.checked;
        });
        syncPickerAll(groupAll, groupList);
      });
    }
    groupList.addEventListener("change", function () {
      syncPickerAll(groupAll, groupList);
    });
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

  function severityGroupValues() {
    var boxes = document.querySelectorAll("[data-rpt-severity]:checked");
    return Array.prototype.map.call(boxes, function (box) { return box.value; });
  }

  function applyTableFilters() {
    if (!table) return;
    var hostTerms = filterTerms("availabilityHostFilter");
    var nameTerms = filterTerms("availabilityNameFilter");
    var categoryTerms = filterTerms("availabilityCategoryFilter");
    var severityQuery = filterValue("availabilitySeverityFilter");
    var severitySet = severityGroupValues();
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
      if (show && severitySet.length && severitySet.indexOf(severity) === -1) show = false;
      if (show && !severitySet.length && severityQuery && severity !== severityQuery) show = false;
      row.hidden = !show;
      if (show) visible += 1;
    });
    var counter = document.getElementById("availabilityDisplayCount");
    if (counter) counter.textContent = "Displaying " + visible + " of " + total + " found";
    updateFilterEmptyRow(visible, total);
  }

  document.querySelectorAll("[data-rpt-severity]").forEach(function (box) {
    box.addEventListener("change", applyTableFilters);
  });

  filterIds.forEach(function (id) {
    var field = document.getElementById(id);
    if (!field) return;
    field.addEventListener(field.tagName === "SELECT" ? "change" : "input", applyTableFilters);
  });

  document.querySelectorAll(".rpt-availability-select, .rpt-trigger-select").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = document.getElementById(button.getAttribute("data-picker"));
      if (!input) return;
      openPicker(input, button.getAttribute("data-picker-attr"), button);
    });
  });

  var filterReset = document.getElementById("availabilityFilterReset");
  if (filterReset) {
    filterReset.addEventListener("click", function () {
      ["availabilityHostFilter", "availabilityCategoryFilter", "availabilityNameFilter", "availabilitySeverityFilter"].forEach(function (id) {
        var field = document.getElementById(id);
        if (field) field.value = "";
      });
      document.querySelectorAll("[data-rpt-severity]").forEach(function (box) {
        box.checked = false;
      });
      closePicker();
      applyTableFilters();
    });
  }

  var filterApply = document.getElementById("availabilityFilterApply");
  if (filterApply) {
    filterApply.addEventListener("click", function () {
      closePicker();
      applyTableFilters();
      if (filterPanel && !filterPanel.hasAttribute("data-persist")) setFilterPanel(false);
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
      var secondsByKey = {
        "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "3h": 10800,
        "6h": 21600, "12h": 43200, "1d": 86400, "2d": 172800, "7d": 604800,
        "30d": 2592000, "3m": 7776000, "6m": 15552000, "1y": 31536000, "2y": 63072000
      };
      var index = -1;
      var rangeSeconds = Number(toggle.getAttribute("data-time-seconds")) || 0;
      if (rangeSeconds > 0) {
        // Rentang kustom (mis. now-2d..now) di-zoom berdasarkan durasi terdekat.
        var bestDiff = null;
        sequence.forEach(function (candidate, position) {
          var diff = Math.abs(secondsByKey[candidate] - rangeSeconds);
          if (bestDiff === null || diff < bestDiff) {
            bestDiff = diff;
            index = position;
          }
        });
      } else {
        index = sequence.indexOf(key);
      }
      if (index < 0) index = 3;
      var direction = button.getAttribute("data-zoom") === "in" ? -1 : 1;
      index = Math.max(0, Math.min(sequence.length - 1, index + direction));
      current.searchParams.set("time", sequence[index]);
      current.searchParams.delete("from");
      current.searchParams.delete("to");
      window.location.href = current.pathname + current.search;
    });
  });

  var panelButtons = [toggle];
  if (filterButton && !filterPanel && !auditFilterPanel) panelButtons.push(filterButton);

  document.addEventListener("click", function (event) {
    if (panel.hidden) return;
    if (panel.contains(event.target)) return;
    var calendar = document.getElementById("availabilityCalendar");
    if (calendar && calendar.contains(event.target)) return;
    if (calendar && !calendar.hidden) {
      // Klik pada hari menggambar ulang kalender, jadi target bisa sudah
      // terlepas dari DOM; pakai posisi klik agar panel tidak ikut menutup.
      var rect = calendar.getBoundingClientRect();
      if (event.clientX >= rect.left && event.clientX <= rect.right &&
          event.clientY >= rect.top && event.clientY <= rect.bottom) return;
    }
    for (var i = 0; i < panelButtons.length; i++) {
      if (panelButtons[i].contains(event.target)) return;
    }
    setPanel(false);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    // <dialog> menutup dirinya sendiri; jangan sekaligus menutup panel filter.
    if (auditUsersDialog && auditUsersDialog.open) return;
    closePicker();
    if (!panel.hidden) {
      setPanel(false);
      toggle.focus();
    }
    if (filterPanel && !filterPanel.hidden) {
      setFilterPanel(false);
      filterButton.focus();
    }
    if (auditFilterPanel && !auditFilterPanel.hidden) {
      setAuditFilterPanel(false);
      filterButton.focus();
    }
  });

  // Problem tags (Top 100 triggers): tambah/hapus baris tag dan mode And/Or.
  var tagRows = document.getElementById("ttTagRows");
  var tagAdd = document.getElementById("ttTagAdd");
  if (tagRows && tagAdd) {
    function clearTagRow(row) {
      Array.prototype.forEach.call(row.querySelectorAll("input"), function (input) {
        input.value = "";
      });
      var op = row.querySelector("select");
      if (op) op.selectedIndex = 0;
    }

    tagAdd.addEventListener("click", function () {
      var template = tagRows.querySelector(".rpt-trigger-tag-row");
      if (!template) return;
      var row = template.cloneNode(true);
      clearTagRow(row);
      tagRows.appendChild(row);
      var first = row.querySelector("input");
      if (first) first.focus();
    });

    tagRows.addEventListener("click", function (event) {
      var button = event.target.closest(".rpt-trigger-tag-remove");
      if (!button) return;
      var row = button.closest(".rpt-trigger-tag-row");
      if (!row) return;
      if (tagRows.querySelectorAll(".rpt-trigger-tag-row").length > 1) row.remove();
      else clearTagRow(row);
    });

    document.querySelectorAll(".rpt-trigger-andor-btn").forEach(function (button) {
      button.addEventListener("click", function () {
        document.querySelectorAll(".rpt-trigger-andor-btn").forEach(function (other) {
          var active = other === button;
          other.classList.toggle("active", active);
          other.setAttribute("aria-pressed", active ? "true" : "false");
        });
      });
    });
  }
})();
