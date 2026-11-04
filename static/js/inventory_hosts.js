/* ============================================================
   NMS Kopegtel — halaman Inventory > Hosts (gaya Zabbix).
   Daftar host beserta field inventory (sumber: host_inventory).
   ============================================================ */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  var state = {
    filterOpen: true,
    loaded: false,
    hostGroups: [],
    groups: [],
    sort: "asc",
    data: null,
    editRow: null
  };

  var ICON_EDIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" ' +
    'aria-hidden="true"><path d="M12 20h9"/>' +
    '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';

  function esc(value) {
    return window.svcEsc ? window.svcEsc(value) : String(value == null ? "" : value);
  }

  function setFilterOpen(open) {
    state.filterOpen = open;
    var panel = $("invFilterPanel");
    var body = $("invFilterBody");
    var button = $("btnToggleInvFilter");
    if (panel) panel.classList.toggle("is-collapsed", !open);
    if (body) body.hidden = !open;
    if (button) {
      button.classList.toggle("collapsed", !open);
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }

  function fillFieldSelect(fields) {
    var select = $("invField");
    if (!select || !fields || !fields.length) return;
    var current = select.value || "alias";
    var html = "";
    for (var i = 0; i < fields.length; i += 1) {
      html += '<option value="' + esc(fields[i].key) + '">' +
        esc(fields[i].label) + "</option>";
    }
    select.innerHTML = html;
    select.value = current;
    if (!select.value) select.value = "alias";
  }

  function selectedGroupsFromInput() {
    var input = $("invHostGroups");
    var raw = input ? input.value : "";
    var groups = [];
    var seen = {};
    raw.split(",").forEach(function (item) {
      var name = item.trim();
      var key = name.toLowerCase();
      if (name && !seen[key]) {
        seen[key] = true;
        groups.push(name);
      }
    });
    return groups;
  }

  function query() {
    var params = [];
    if (state.groups.length) {
      params.push("groups=" + encodeURIComponent(state.groups.join(",")));
    }
    var field = $("invField") ? $("invField").value : "alias";
    var operator = $("invOperator") ? $("invOperator").value : "contains";
    var value = $("invFieldValue") ? $("invFieldValue").value.trim() : "";
    if (field) params.push("field=" + encodeURIComponent(field));
    if (operator) params.push("operator=" + encodeURIComponent(operator));
    if (value) params.push("value=" + encodeURIComponent(value));
    return params.length ? "?" + params.join("&") : "";
  }

  function hostName(row) {
    return row.alias || row.ip || row.name || "";
  }

  function sortedRows(rows) {
    var out = rows.slice();
    out.sort(function (a, b) {
      return hostName(a).localeCompare(hostName(b), "id", { sensitivity: "base" });
    });
    if (state.sort === "desc") out.reverse();
    return out;
  }

  function cell(value) {
    return value ? esc(value) : "";
  }

  function renderRows(data) {
    var body = $("invBody");
    var info = $("invFootInfo");
    if (!body) return;
    var rows = sortedRows(data.rows || []);

    if (!rows.length) {
      body.innerHTML = '<tr class="inv-hosts-empty-row"><td colspan="8">' +
        "No data found</td></tr>";
    } else {
      var html = "";
      for (var i = 0; i < rows.length; i += 1) {
        var row = rows[i];
        var fields = row.fields || {};
        html += "<tr>" +
          '<td class="inv-host-cell"><a class="inv-host-name" href="' +
          esc(row.url) + '">' + esc(hostName(row)) + "</a>" +
          '<button class="inv-edit-btn" type="button" data-ip="' +
          esc(row.ip) + '" title="Edit inventory" aria-label="Edit inventory ' +
          esc(hostName(row)) + '">' + ICON_EDIT + "</button></td>" +
          "<td>" + cell(row.category) + "</td>" +
          "<td>" + cell(fields.name) + "</td>" +
          "<td>" + cell(fields.type) + "</td>" +
          '<td class="inv-os-cell">' + cell(fields.os) + "</td>" +
          "<td>" + cell(fields.serialno_a) + "</td>" +
          "<td>" + cell(fields.tag) + "</td>" +
          "<td>" + cell(fields.macaddress_a) + "</td>" +
          "</tr>";
      }
      body.innerHTML = html;
    }

    if (info) {
      info.textContent = "Displaying " + data.matched + " of " + data.total + " found";
    }
  }

  /* ---------- pemilih host groups ---------- */

  function groupChecks() {
    return document.querySelectorAll("#invGroupList .inv-group-check");
  }

  function syncSelectAll() {
    var all = $("invGroupSelectAll");
    if (!all) return;
    var checks = groupChecks();
    var selected = 0;
    for (var i = 0; i < checks.length; i += 1) {
      if (checks[i].checked) selected += 1;
    }
    all.checked = checks.length > 0 && selected === checks.length;
    all.indeterminate = selected > 0 && selected < checks.length;
  }

  function renderGroupList() {
    var list = $("invGroupList");
    if (!list) return;
    var current = selectedGroupsFromInput();
    var html = "";
    for (var i = 0; i < state.hostGroups.length; i += 1) {
      var name = state.hostGroups[i];
      var id = "invGroupCheck" + i;
      var selected = current.some(function (group) {
        return group.toLowerCase() === name.toLowerCase();
      });
      var checked = selected ? " checked" : "";
      html += '<tr class="inv-group-row">' +
        '<td class="inv-group-check-col">' +
        '<input type="checkbox" class="inv-group-check" id="' + id +
        '" value="' + esc(name) + '"' + checked + ">" +
        "</td>" +
        '<td><label class="inv-group-name" for="' + id + '">' +
        esc(name) + "</label></td></tr>";
    }
    if (!state.hostGroups.length) {
      html = '<tr class="inv-group-empty-row"><td colspan="2" class="inv-group-empty">' +
        "Belum ada host group (category) pada host.</td></tr>";
    }
    list.innerHTML = html;
    syncSelectAll();
  }

  function openGroupDialog() {
    var dialog = $("invGroupDialog");
    renderGroupList();
    if (dialog && dialog.showModal) dialog.showModal();
  }

  function commitGroupDialog() {
    var checked = document.querySelectorAll(".inv-group-check:checked");
    var names = [];
    for (var i = 0; i < checked.length; i += 1) names.push(checked[i].value);
    state.groups = names;
    var input = $("invHostGroups");
    if (input) input.value = names.join(", ");
    var dialog = $("invGroupDialog");
    if (dialog) dialog.close();
  }

  function updateSortUi() {
    var head = document.querySelector(".inv-col-host");
    var arrow = $("invSortArrow");
    if (head) {
      head.setAttribute("aria-sort", state.sort === "asc" ? "ascending" : "descending");
    }
    if (arrow) arrow.textContent = state.sort === "asc" ? "\u25B2" : "\u25BC";
  }

  /* ---------- dialog edit inventory ---------- */

  function editFieldEls() {
    return document.querySelectorAll("#invEditForm [data-inv-key]");
  }

  function openEditDialog(ip) {
    var rows = (state.data && state.data.rows) || [];
    var row = null;
    for (var i = 0; i < rows.length; i += 1) {
      if (rows[i].ip === ip) row = rows[i];
    }
    if (!row) {
      window.svcToast("Host tidak ditemukan di daftar", "error");
      return;
    }
    state.editRow = row;
    var host = $("invEditHost");
    if (host) host.textContent = hostName(row) + " (" + row.ip + ")";
    var fields = row.fields || {};
    editFieldEls().forEach(function (el) {
      var key = el.getAttribute("data-inv-key");
      el.value = fields[key] || "";
    });
    var dialog = $("invEditDialog");
    if (dialog && dialog.showModal) dialog.showModal();
    var first = $("invEditName");
    if (first) first.focus();
  }

  function closeEditDialog() {
    var dialog = $("invEditDialog");
    if (dialog && dialog.open) dialog.close();
    state.editRow = null;
  }

  function saveEditDialog(e) {
    if (e) e.preventDefault();
    if (!state.editRow) return;
    var fields = {};
    editFieldEls().forEach(function (el) {
      fields[el.getAttribute("data-inv-key")] = el.value.trim();
    });
    var save = $("btnSaveInvEdit");
    if (save) {
      save.disabled = true;
      save.textContent = "Menyimpan…";
    }
    var resetSave = function () {
      if (save) {
        save.disabled = false;
        save.textContent = "Save";
      }
    };
    window.svcJson(
      "/api/hosts/" + encodeURIComponent(state.editRow.ip) + "/inventory",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields: fields })
      }
    ).then(function () {
      closeEditDialog();
      window.svcToast("Inventory host disimpan", "success");
      return load();
    }).catch(function (err) {
      window.svcToast("Gagal menyimpan inventory: " + err.message, "error");
    }).then(resetSave);
  }

  /* ---------- muat data ---------- */

  function load() {
    var info = $("invFootInfo");
    if (info) info.textContent = "Memuat data…";
    return window.svcJson("/api/inventory/hosts" + query())
      .then(function (data) {
        state.data = data;
        state.hostGroups = data.host_groups || state.hostGroups;
        if (!state.loaded) {
          fillFieldSelect(data.fields || []);
          state.loaded = true;
        }
        renderRows(data);
      })
      .catch(function (err) {
        var body = $("invBody");
        if (body) {
          body.innerHTML = '<tr class="inv-hosts-empty-row"><td colspan="8">' +
            "No data found</td></tr>";
        }
        if (info) info.textContent = "Displaying 0 of 0 found";
        window.svcToast("Gagal memuat inventory host: " + err.message, "error");
      });
  }

  function init() {
    var form = $("invFilterForm");
    if (form) form.addEventListener("submit", function (e) {
      e.preventDefault();
      state.groups = selectedGroupsFromInput();
      load();
    });

    var reset = $("btnResetInvFilter");
    if (reset) reset.addEventListener("click", function () {
      state.groups = [];
      var input = $("invHostGroups");
      if (input) input.value = "";
      var field = $("invField");
      if (field) field.value = "alias";
      var operator = $("invOperator");
      if (operator) operator.value = "contains";
      var value = $("invFieldValue");
      if (value) value.value = "";
      load();
    });

    var toggle = $("btnToggleInvFilter");
    if (toggle) toggle.addEventListener("click", function () {
      setFilterOpen(!state.filterOpen);
    });

    var sortBtn = $("invSortHost");
    if (sortBtn) sortBtn.addEventListener("click", function () {
      state.sort = state.sort === "asc" ? "desc" : "asc";
      updateSortUi();
      if (state.data) renderRows(state.data);
    });

    var selectBtn = $("btnInvSelectGroups");
    if (selectBtn) selectBtn.addEventListener("click", openGroupDialog);

    var groupList = $("invGroupList");
    if (groupList) groupList.addEventListener("change", function (e) {
      if (e.target && e.target.classList.contains("inv-group-check")) syncSelectAll();
    });

    var selectAll = $("invGroupSelectAll");
    if (selectAll) selectAll.addEventListener("change", function () {
      var checks = groupChecks();
      for (var i = 0; i < checks.length; i += 1) checks[i].checked = selectAll.checked;
      syncSelectAll();
    });

    var groupApply = $("btnInvGroupApply");
    if (groupApply) groupApply.addEventListener("click", commitGroupDialog);
    var groupCancel = $("btnInvGroupCancel");
    if (groupCancel) groupCancel.addEventListener("click", function () {
      var dialog = $("invGroupDialog");
      if (dialog) dialog.close();
    });
    var groupClose = $("btnCloseInvGroupDialog");
    if (groupClose) groupClose.addEventListener("click", function () {
      var dialog = $("invGroupDialog");
      if (dialog) dialog.close();
    });

    var help = $("btnInvHostsHelp");
    if (help) help.addEventListener("click", function () {
      var dialog = $("invHelpDialog");
      if (dialog && dialog.showModal) dialog.showModal();
    });
    var closeHelp = $("btnCloseInvHelp");
    if (closeHelp) closeHelp.addEventListener("click", function () {
      var dialog = $("invHelpDialog");
      if (dialog) dialog.close();
    });

    var invBody = $("invBody");
    if (invBody) invBody.addEventListener("click", function (e) {
      var btn = e.target.closest(".inv-edit-btn");
      if (!btn) return;
      e.preventDefault();
      openEditDialog(btn.getAttribute("data-ip"));
    });

    var editForm = $("invEditForm");
    if (editForm) editForm.addEventListener("submit", saveEditDialog);
    var editCancel = $("btnCancelInvEdit");
    if (editCancel) editCancel.addEventListener("click", closeEditDialog);
    var editClose = $("btnCloseInvEdit");
    if (editClose) editClose.addEventListener("click", closeEditDialog);
    var editDialog = $("invEditDialog");
    if (editDialog) editDialog.addEventListener("close", function () {
      state.editRow = null;
    });

    updateSortUi();
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
